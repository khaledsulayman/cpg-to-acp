"""Creator evaluation: drive the real DMN loop with instrumentation.

Rather than re-implement the subgraph, this reuses the actual nodes
(``dmn_creator``, ``dmn_syntax_validator``, ``dmn_semantic_reviewer``) and the
actual router functions from ``generation.py`` so the measured behavior matches
production exactly — it just records what happens at each step (first-attempt
validity, attempts-to-valid, escalation reason, final DMN) that the compiled
subgraph does not surface.

Each accepted/escalated model is then scored against its golden (structural F1,
threshold exactness) and compile-checked against the decision-service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import mlflow

from cpg_ingester import generation
from cpg_ingester.generation import (
    _extract_section_text,
    _route_after_dmn_semantic,
    _route_after_dmn_syntax,
)
from cpg_ingester.nodes.dmn_creator import dmn_creator
from cpg_ingester.nodes.dmn_semantic_reviewer import dmn_semantic_reviewer
from cpg_ingester.nodes.dmn_syntax_validator import dmn_syntax_validator
from cpg_ingester.validators.dmn_syntax import validate_dmn_xml

try:
    from cpg_ingester.validators.dmn_schema import validate_dmn_schema
except ImportError:
    def validate_dmn_schema(_dmn_xml: str) -> list[str]:
        return []

from compile_check import compile_check, validate_check
from dmn_diff import diff_models
from dmn_model import normalize_output

logger = logging.getLogger(__name__)

_MAX_LOOP_ITERATIONS = 12  # safety cap; the real routers stop well before this


def _source_text(markdown: str, source_section: dict) -> str:
    """Use an explicit source line range when a heading is broader than one decision."""
    line_range = source_section.get("markdown_lines")
    if isinstance(line_range, str) and "-" in line_range:
        try:
            start, end = (int(part) for part in line_range.split("-", 1))
            lines = markdown.splitlines()
            return "\n".join(lines[start - 1:end]).strip()
        except (TypeError, ValueError):
            logger.warning("Invalid source markdown_lines %r", line_range)
    heading = source_section.get("heading", "")
    return _extract_section_text(markdown, [{"heading": heading}], heading)


@dataclass
class CreatorResult:
    decision: str
    first_attempt_l0_pass: bool = False
    creator_calls: int = 0
    attempts_to_valid: int | None = None
    final_l0_valid: bool = False
    escalated: bool = False
    escalation_reason: str = ""
    section_text_present: bool = False
    final_dmn: str = ""
    golden_diff: dict = field(default_factory=dict)
    compile_status: str = "SKIPPED"
    execution_match_rate: float | None = None
    execution_results: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d.pop("final_dmn", None)  # keep the report readable; DMN saved separately
        return d


@mlflow.trace(name="dmn_benchmark_drive_loop")
def _drive_loop(item: dict, source_text: str, llm_config: dict, output_dir: str) -> CreatorResult:
    """Run creator→validate→(retry|review)→(retry|accept|escalate), instrumented."""
    res = CreatorResult(decision=item.get("name", "unknown"),
                        section_text_present=bool(source_text))
    state: dict = {
        "item": item,
        "source_pages": source_text,
        "output_dir": output_dir,
        "syntax_retry_count": 0,
        "semantic_retry_count": 0,
        **llm_config,
    }

    for _ in range(_MAX_LOOP_ITERATIONS):
        state.update(dmn_creator(state))
        res.creator_calls += 1
        state.update(dmn_syntax_validator(state))

        if res.creator_calls == 1:
            res.first_attempt_l0_pass = not state.get("syntax_errors")

        route = _route_after_dmn_syntax(state)
        if route == "dmn_creator":
            continue  # syntax retry
        if route == "dmn_escalate":
            res.escalated = True
            res.escalation_reason = "syntax-budget-exhausted"
            break

        # Syntax clean at this point.
        if res.attempts_to_valid is None:
            res.attempts_to_valid = res.creator_calls

        state.update(dmn_semantic_reviewer(state))
        route = _route_after_dmn_semantic(state)
        if route == "dmn_accept":
            preflight = getattr(generation, "dmn_engine_preflight", None)
            engine_route = "dmn_complete"
            if preflight is not None:
                state.update(preflight(state))
                engine_route = generation._route_after_dmn_engine(state)
            if engine_route == "dmn_complete":
                break
            if engine_route == "dmn_escalate":
                res.escalated = True
                res.escalation_reason = state.get(
                    "escalation_reason", "engine-validation-budget-exhausted")
                break
            continue
        if route == "dmn_escalate":
            res.escalated = True
            res.escalation_reason = state.get("escalation_reason") or "semantic-budget-exhausted"
            break
        # else route == dmn_creator -> semantic retry, loop again

    res.final_dmn = state.get("dmn_xml", "")
    res.final_l0_valid = bool(
        res.final_dmn
        and not validate_dmn_schema(res.final_dmn)
        and not validate_dmn_xml(res.final_dmn)
    )
    return res


def _execution_outputs(outputs: dict | None, decision_name: str) -> dict | None:
    """Flatten the engine's decision-shaped response for corpus expectations."""
    if not isinstance(outputs, dict):
        return None
    nested = outputs.get(decision_name)
    if isinstance(nested, dict):
        return nested
    return outputs


def _outputs_match(actual: dict | None, expected: dict) -> bool:
    if actual is None or not isinstance(expected, dict):
        return False
    return all(key in actual
               and normalize_output(str(actual[key])) == normalize_output(str(value))
               for key, value in expected.items())


def _score_execution(res: CreatorResult, decision: dict, run_compile: bool) -> None:
    """Evaluate every representative input and record output equivalence."""
    if not run_compile or res.compile_status != "COMPILE_OK" or not res.final_dmn:
        return
    outcomes = []
    scored = []
    assumption_indices = {
        index for index, representative in enumerate(decision.get("representative_inputs", []))
        if representative.get("assumption")
    }
    for index, representative in enumerate(decision.get("representative_inputs", [])):
        inputs = representative.get("inputs", {})
        expected = representative.get("expect", {})
        result = compile_check(res.final_dmn, inputs)
        actual = _execution_outputs(result.outputs, decision["name"])
        matches = _outputs_match(actual, expected) if result.status == "COMPILE_OK" else False
        row = {
            "inputs": inputs,
            "expect": expected,
            "outputs": actual,
            "status": result.status,
            "match": matches,
            "assumption": index in assumption_indices,
        }
        outcomes.append(row)
        if index not in assumption_indices:
            scored.append(matches)
    res.execution_results = outcomes
    res.execution_match_rate = round(sum(scored) / len(scored), 4) if scored else None


@mlflow.trace(name="dmn_benchmark_creator_suite")
def run_creator_suite(corpus: dict, markdown: str, llm_config: dict, output_dir: str,
                      repo_root, run_compile: bool = True,
                      return_records: bool = False) -> dict:
    """Generate DMN for each decision, score vs golden, compile-check."""
    results: list[CreatorResult] = []
    for dec in corpus.get("decisions", []):
        source_section = dec.get("source_section", {})
        heading = source_section.get("heading", "")
        source_text = _source_text(markdown, source_section)
        item = {
            "name": dec["name"],
            "type": "decision",
            "category": dec.get("category", "treatment"),
            "hit_policy": dec.get("hit_policy", "UNIQUE"),
            "inputs": dec.get("inputs", []),
            "outputs": [o["name"] for o in dec.get("outputs", [])],
            "section": heading,
        }
        res = _drive_loop(item, source_text, llm_config, output_dir)

        golden_xml = (repo_root / dec["golden"]).read_text()
        if res.final_dmn:
            try:
                res.golden_diff = diff_models(
                    golden_xml,
                    res.final_dmn,
                    {dec["name"]: set(dec.get("assumption_rules", []))},
                )
            except Exception as e:  # malformed final DMN
                res.golden_diff = {"error": str(e)}
            if run_compile:
                res.compile_status = validate_check(res.final_dmn).status
                _score_execution(res, dec, run_compile)
        results.append(res)

    report = _aggregate(results)
    return (report, results) if return_records else report


def _aggregate(results: list[CreatorResult]) -> dict:
    n = len(results)
    if not n:
        return {"decisions": 0}
    first_pass = sum(int(r.first_attempt_l0_pass) for r in results)
    escalated = sum(int(r.escalated) for r in results)
    section_hits = sum(int(r.section_text_present) for r in results)
    valid_attempts = [r.attempts_to_valid for r in results if r.attempts_to_valid]
    compiled = sum(1 for r in results if r.compile_status == "COMPILE_OK")
    compile_fail = sum(1 for r in results if r.compile_status == "COMPILE_FAIL")
    compile_measured = sum(1 for r in results if r.compile_status in ("COMPILE_OK", "COMPILE_FAIL"))
    l0_pass_compile_fail = sum(1 for r in results
                               if r.final_l0_valid and r.compile_status == "COMPILE_FAIL")
    f1s = [r.golden_diff.get("structural_f1") for r in results
           if isinstance(r.golden_diff, dict) and "structural_f1" in r.golden_diff]
    output_exactness = [r.golden_diff.get("output_exactness") for r in results
                        if isinstance(r.golden_diff, dict)
                        and "output_exactness" in r.golden_diff]
    decision_exact = [r.golden_diff.get("decision_exact_rate") for r in results
                     if isinstance(r.golden_diff, dict)
                     and "decision_exact_rate" in r.golden_diff]
    hit_policy = [r.golden_diff.get("hit_policy_match_rate") for r in results
                  if isinstance(r.golden_diff, dict)
                  and "hit_policy_match_rate" in r.golden_diff]
    input_match = [r.golden_diff.get("inputs_match_rate") for r in results
                   if isinstance(r.golden_diff, dict)
                   and "inputs_match_rate" in r.golden_diff]
    output_match = [r.golden_diff.get("outputs_match_rate") for r in results
                    if isinstance(r.golden_diff, dict)
                    and "outputs_match_rate" in r.golden_diff]
    execution_rates = [r.execution_match_rate for r in results
                       if r.execution_match_rate is not None]

    return {
        "decisions": n,
        "first_attempt_validity_rate": round(first_pass / n, 4),
        "mean_attempts_to_valid": round(sum(valid_attempts) / len(valid_attempts), 3) if valid_attempts else None,
        "escalation_rate": round(escalated / n, 4),
        "section_extraction_hit_rate": round(section_hits / n, 4),
        "compile_pass_rate": round(compiled / compile_measured, 4) if compile_measured else None,
        "compile_measured": compile_measured,
        "compile_fail": compile_fail,
        "l0_pass_but_compile_fail": l0_pass_compile_fail,
        "mean_structural_f1": round(sum(f1s) / len(f1s), 4) if f1s else None,
        "mean_output_exactness": round(sum(output_exactness) / len(output_exactness), 4)
        if output_exactness else None,
        "decision_exact_rate": round(sum(decision_exact) / len(decision_exact), 4)
        if decision_exact else None,
        "hit_policy_match_rate": round(sum(hit_policy) / len(hit_policy), 4)
        if hit_policy else None,
        "inputs_match_rate": round(sum(input_match) / len(input_match), 4)
        if input_match else None,
        "outputs_match_rate": round(sum(output_match) / len(output_match), 4)
        if output_match else None,
        "mean_execution_match_rate": round(sum(execution_rates) / len(execution_rates), 4)
        if execution_rates else None,
        "per_decision": [r.to_dict() for r in results],
    }


@mlflow.trace(name="dmn_benchmark_score_generated_corpus")
def score_generated_corpus(dmn_paths: list, run_compile: bool = True) -> dict:
    """L0 + compile validity for a directory of generated DMN (no goldens)."""
    rows = []
    for path in dmn_paths:
        xml = path.read_text()
        l0_errors = validate_dmn_xml(xml)
        compile_status = validate_check(xml).status if run_compile else "SKIPPED"
        rows.append({
            "file": path.name,
            "l0_valid": not l0_errors,
            "l0_error_count": len(l0_errors),
            "compile_status": compile_status,
        })
    n = len(rows) or 1
    l0_valid = sum(int(r["l0_valid"]) for r in rows)
    compiled = sum(1 for r in rows if r["compile_status"] == "COMPILE_OK")
    measured = sum(1 for r in rows if r["compile_status"] in ("COMPILE_OK", "COMPILE_FAIL"))
    l0_pass_compile_fail = sum(1 for r in rows
                               if r["l0_valid"] and r["compile_status"] == "COMPILE_FAIL")
    return {
        "files": len(rows),
        "l0_validity_rate": round(l0_valid / n, 4),
        "compile_pass_rate": round(compiled / measured, 4) if measured else None,
        "l0_pass_but_compile_fail": l0_pass_compile_fail,
        "per_file": rows,
    }
