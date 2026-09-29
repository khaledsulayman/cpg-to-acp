"""Reviewer evaluation: seed defects, run the semantic reviewer, score it.

Builds the minimal state the ``dmn_semantic_reviewer`` node expects and invokes
it directly (no subgraph), once per (model, source-section) pair. Clean goldens
must pass (a flag on a clean model is a false escalation); defective variants
should be flagged (recall). Metrics are computed per defect class.

A subset of the corpus is designated *holdout* (never used to drive prompt
tuning) to guard against overfitting the reviewer to the synthetic injectors —
see ``config.yaml``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import mlflow

from cpg_ingester.generation import _extract_section_text
from cpg_ingester.nodes.dmn_semantic_reviewer import dmn_semantic_reviewer

from defects import INJECTORS, DefectDescriptor, DefectNotApplicable, _parse, _serialize, _strip_comments

logger = logging.getLogger(__name__)


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
class ReviewerCase:
    """One reviewer invocation and its outcome."""

    decision: str
    kind: str            # "clean" or a defect class
    holdout: bool
    flagged: bool
    discrepancies: list = field(default_factory=list)
    defect_detail: str = ""
    targeted: bool = False
    error: str = ""


@mlflow.trace(name="dmn_benchmark_run_reviewer")
def _run_reviewer(dmn_xml: str, name: str, source_text: str, llm_config: dict,
                  output_dir: str) -> dict:
    clean_tree = _parse(dmn_xml)
    state = {
        "dmn_xml": _serialize(clean_tree),
        "item": {"name": name},
        "source_pages": source_text,
        "output_dir": output_dir,
        "semantic_retry_count": 0,
        **llm_config,
    }
    return dmn_semantic_reviewer(state)


def _targets(desc: DefectDescriptor, discrepancies: list[str]) -> bool:
    """Return whether reviewer feedback names the seeded defect."""
    text = "\n".join(discrepancies).lower()
    location = desc.location
    if desc.defect_class == "threshold_shift":
        return any(str(location.get(key, "")).lower() in text
                   for key in ("was", "now") if location.get(key) is not None)
    if desc.defect_class == "drop_rule":
        return any(str(value).lower() in text for value in
                   [location.get("rule_id", ""), *location.get("inputs", [])]
                   if value)
    if desc.defect_class == "fabricate_input":
        return str(location.get("input", "")).lower() in text
    if desc.defect_class == "wrong_output":
        return any(str(location.get(key, "")).lower() in text
                   for key in ("was", "now") if location.get(key) is not None)
    if desc.defect_class == "wrong_hit_policy":
        return "hit policy" in text or str(location.get("now", "")).lower() in text
    return False


@mlflow.trace(name="dmn_benchmark_reviewer_suite")
def run_reviewer_suite(corpus: dict, markdown: str, llm_config: dict,
                       output_dir: str, holdout_classes: set[str],
                       repo_root, return_cases: bool = False) -> dict:
    """Run the reviewer over clean + defective variants of every golden.

    ``corpus`` is one corpus entry (``corpora.<name>``) from ``corpus.yaml``.
    Returns a metrics dict plus the raw case list.
    """
    cases: list[ReviewerCase] = []
    not_applicable: dict[str, list[str]] = {}

    for dec in corpus.get("decisions", []):
        name = dec["name"]
        source_section = dec.get("source_section", {})
        heading = source_section.get("heading", "")
        source_text = _source_text(markdown, source_section)
        if not source_text:
            logger.warning("No source text for '%s' (heading %r)", name, heading)
        golden_xml = (repo_root / dec["golden"]).read_text()

        # Clean model — should NOT be flagged.
        clean_result = ({"force_escalate": True, "escalation_reason": "no-source-text"}
                        if not source_text else
                        _run_reviewer(golden_xml, name, source_text, llm_config, output_dir))
        clean_flags = clean_result.get("semantic_discrepancies", []) or []
        if clean_result.get("force_escalate") or not source_text:
            cases.append(ReviewerCase(decision=name, kind="error", holdout=False,
                                      flagged=False, error=clean_result.get(
                                          "escalation_reason", "reviewer escalation")))
        else:
            cases.append(ReviewerCase(decision=name, kind="clean", holdout=False,
                                      flagged=bool(clean_flags), discrepancies=clean_flags))

        # Defective variants — should be flagged.
        for defect_class, injector in INJECTORS.items():
            try:
                mutated_xml, desc = injector(golden_xml)
            except DefectNotApplicable as e:
                logger.warning("Defect %s not applicable to '%s': %s", defect_class, name, e)
                not_applicable.setdefault(defect_class, []).append(name)
                continue
            result = ({"force_escalate": True, "escalation_reason": "no-source-text"}
                      if not source_text else
                      _run_reviewer(mutated_xml, name, source_text, llm_config, output_dir))
            flags = result.get("semantic_discrepancies", []) or []
            if result.get("force_escalate") or not source_text:
                cases.append(ReviewerCase(
                    decision=name, kind="error", holdout=defect_class in holdout_classes,
                    flagged=False, error=result.get("escalation_reason", "reviewer escalation"),
                ))
            else:
                cases.append(ReviewerCase(
                    decision=name, kind=defect_class,
                    holdout=defect_class in holdout_classes,
                    flagged=bool(flags), discrepancies=flags, defect_detail=desc.detail,
                    targeted=_targets(desc, flags),
                ))

    if not cases or all(case.kind == "error" for case in cases):
        raise RuntimeError("reviewer suite produced no usable cases")
    scored = _score(cases)
    scored["not_applicable"] = not_applicable
    return (scored, cases) if return_cases else scored


def _score(cases: list[ReviewerCase]) -> dict:
    defect_cases = [c for c in cases if c.kind not in {"clean", "error"}]
    clean_cases = [c for c in cases if c.kind == "clean"]

    # Per defect-class recall.
    per_class: dict[str, dict] = {}
    for c in defect_cases:
        d = per_class.setdefault(c.kind, {"seeded": 0, "flagged": 0, "targeted": 0})
        d["seeded"] += 1
        d["flagged"] += int(c.flagged)
        d["targeted"] += int(c.flagged and c.targeted)
    for k, d in per_class.items():
        d["recall"] = round(d["flagged"] / d["seeded"], 4) if d["seeded"] else 0.0
        d["targeted_recall"] = round(d["targeted"] / d["seeded"], 4) if d["seeded"] else 0.0

    seeded_total = len(defect_cases)
    caught_total = sum(int(c.flagged) for c in defect_cases)
    clean_flagged = sum(int(c.flagged) for c in clean_cases)

    # Precision over the whole clean+defective set: true flags / all flags.
    all_flags = caught_total + clean_flagged
    precision = round(caught_total / all_flags, 4) if all_flags else 0.0

    def split_metrics(subset):
        seeded = [c for c in subset if c.kind not in {"clean", "error"}]
        n = len(seeded)
        caught = sum(int(c.flagged) for c in seeded)
        targeted = sum(int(c.flagged and c.targeted) for c in seeded)
        return {
            "seeded": n,
            "caught": caught,
            "recall": round(caught / n, 4) if n else 0.0,
            "targeted_recall": round(targeted / n, 4) if n else 0.0,
            "missed_defect_rate": round((n - caught) / n, 4) if n else 0.0,
        }

    return {
        "overall_recall": round(caught_total / seeded_total, 4) if seeded_total else 0.0,
        "targeted_recall": round(
            sum(int(c.flagged and c.targeted) for c in defect_cases) / seeded_total, 4
        ) if seeded_total else 0.0,
        "overall_precision": precision,
        "false_escalation_rate": round(clean_flagged / len(clean_cases), 4) if clean_cases else 0.0,
        "missed_defect_rate": round((seeded_total - caught_total) / seeded_total, 4) if seeded_total else 0.0,
        "clean_models": len(clean_cases),
        "clean_flagged": clean_flagged,
        "seeded_defects": seeded_total,
        "caught_defects": caught_total,
        "error_cases": sum(int(c.kind == "error") for c in cases),
        "per_defect_class": per_class,
        "tuning_set": split_metrics([c for c in cases if not c.holdout]),
        "holdout_set": split_metrics([c for c in cases if c.holdout]),
        "cases": [c.__dict__ for c in cases],
    }
