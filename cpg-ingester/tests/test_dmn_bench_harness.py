"""Tests for harness scoring/classification logic (no network, no LLM)."""

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

# The DMN benchmark is a non-package folder under tests/benchmarks/dmn (mirrors
# tests/benchmarks/parsing); put it on sys.path so its modules import by bare name.
_BENCH = Path(__file__).parent / "benchmarks" / "dmn"
sys.path.insert(0, str(_BENCH))

import compile_check as cc
import creator_eval
import run_benchmark as rb
from creator_eval import CreatorResult, _aggregate, _score_execution, _source_text
from reviewer_eval import ReviewerCase, _score
from cpg_ingester.validators.dmn_schema import validate_dmn_schema
from cpg_ingester.validators.dmn_syntax import validate_dmn
from dmn_model import Interval, UNIVERSAL, ValueSet, normalize_unary, parse_dmn

INGESTER_ROOT = Path(__file__).parent.parent


class TestCompileClassification:
    def _resp(self, status, text="", json_body=None):
        r = MagicMock()
        r.status_code = status
        r.text = text
        r.json.return_value = json_body
        return r

    def test_validate_true_is_compile_ok(self):
        response = self._resp(200, json_body={"valid": True, "messages": []})
        with patch("compile_check.requests.post", return_value=response):
            result = cc.validate_check("<x/>")
        assert result.status == "COMPILE_OK"
        assert result.messages == []

    def test_validate_false_preserves_messages(self):
        messages = [{"severity": "ERROR", "text": "bad FEEL"}]
        response = self._resp(200, json_body={"valid": False, "messages": messages})
        with patch("compile_check.requests.post", return_value=response):
            result = cc.validate_check("<x/>")
        assert result.status == "COMPILE_FAIL"
        assert result.messages == messages

    def test_validate_500_is_infra(self):
        with patch("compile_check.requests.post", return_value=self._resp(500, "OOM")):
            assert cc.validate_check("<x/>").status == "INFRA"

    def test_validate_unreachable_is_skipped(self):
        with patch("compile_check.requests.post", side_effect=cc.requests.ConnectionError()):
            assert cc.validate_check("<x/>").status == "SKIPPED"

    def test_execute_200_is_compile_ok_with_outputs(self):
        outputs = {"Treatment Recommendation": {"Action": "Start medication"}}
        response = self._resp(200, json_body=outputs)
        with patch("compile_check.requests.post", return_value=response):
            result = cc.compile_check("<x/>")
        assert result.status == "COMPILE_OK"
        assert result.outputs == outputs

    def test_execute_compile_422_is_compile_fail(self):
        response = self._resp(422, json_body={"error": "DMN compilation errors", "messages": []})
        with patch("compile_check.requests.post", return_value=response):
            assert cc.compile_check("<x/>").status == "COMPILE_FAIL"

    def test_execute_evaluation_422_is_compile_ok(self):
        response = self._resp(422, json_body={"error": "DMN evaluation errors", "messages": []})
        with patch("compile_check.requests.post", return_value=response):
            assert cc.compile_check("<x/>").status == "COMPILE_OK"

    def test_execute_empty_model_400_is_compile_fail(self):
        response = self._resp(400, json_body={"error": "No DMN models found in the provided XML"})
        with patch("compile_check.requests.post", return_value=response):
            assert cc.compile_check("<x/>").status == "COMPILE_FAIL"

    def test_execute_bad_request_400_is_infra(self):
        response = self._resp(400, json_body={"error": "dmn_xml_base64 and inputs are required"})
        with patch("compile_check.requests.post", return_value=response):
            assert cc.compile_check("<x/>").status == "INFRA"

    def test_execute_500_is_infra(self):
        with patch("compile_check.requests.post", return_value=self._resp(500, "OOM")):
            assert cc.compile_check("<x/>").status == "INFRA"

    def test_execute_unreachable_is_skipped(self):
        with patch("compile_check.requests.post", side_effect=cc.requests.ConnectionError()):
            assert cc.compile_check("<x/>").status == "SKIPPED"


class TestCreatorAggregate:
    def test_l0_pass_but_compile_fail_counted(self):
        results = [
            CreatorResult(decision="A", first_attempt_l0_pass=True, attempts_to_valid=1,
                          final_l0_valid=True, compile_status="COMPILE_FAIL"),
            CreatorResult(decision="B", first_attempt_l0_pass=False, attempts_to_valid=2,
                          final_l0_valid=True, compile_status="COMPILE_OK"),
        ]
        agg = _aggregate(results)
        assert agg["l0_pass_but_compile_fail"] == 1
        assert agg["first_attempt_validity_rate"] == 0.5
        assert agg["compile_pass_rate"] == 0.5
        assert agg["mean_attempts_to_valid"] == 1.5

    def test_execution_match_rate_uses_compile_outputs(self):
        result = CreatorResult(
            decision="Treatment Recommendation",
            final_dmn="<xml/>",
            compile_status="COMPILE_OK",
        )
        decision = {
            "name": "Treatment Recommendation",
            "representative_inputs": [
                {"inputs": {"Systolic BP": 145}, "expect": {"Action": "Start medication"}},
                {"inputs": {"Systolic BP": 125}, "expect": {"Action": "Lifestyle modification only"}},
            ],
        }
        outputs = {"Treatment Recommendation": {"Action": "Start medication"}}
        with patch("creator_eval.compile_check",
                   return_value=cc.CompileResult(status="COMPILE_OK", outputs=outputs)):
            _score_execution(result, decision, run_compile=True)
        assert result.execution_match_rate == 0.5
        assert result.execution_results[0]["match"] is True
        assert result.execution_results[1]["match"] is False

    def test_assumption_representative_input_is_not_scored(self):
        result = CreatorResult(
            decision="Treatment Recommendation",
            final_dmn="<xml/>",
            compile_status="COMPILE_OK",
        )
        decision = {
            "name": "Treatment Recommendation",
            "representative_inputs": [
                {"inputs": {}, "expect": {"Action": "Assumption"}, "assumption": True},
                {"inputs": {}, "expect": {"Action": "Observed"}},
            ],
        }
        with patch("creator_eval.compile_check", return_value=cc.CompileResult(
                status="COMPILE_OK", outputs={"Action": "Observed"})):
            _score_execution(result, decision, run_compile=True)
        assert result.execution_match_rate == 1.0


class TestReviewerScore:
    def test_recall_precision_false_escalation(self):
        cases = [
            ReviewerCase("A", "clean", False, flagged=False),
            ReviewerCase("A", "threshold_shift", False, flagged=True),
            ReviewerCase("A", "drop_rule", False, flagged=False),   # missed
            ReviewerCase("A", "wrong_hit_policy", True, flagged=True),  # holdout, caught
        ]
        m = _score(cases)
        assert m["seeded_defects"] == 3
        assert m["caught_defects"] == 2
        assert m["overall_recall"] == round(2 / 3, 4)
        assert m["false_escalation_rate"] == 0.0
        assert m["per_defect_class"]["drop_rule"]["recall"] == 0.0
        assert m["holdout_set"]["seeded"] == 1
        assert m["tuning_set"]["seeded"] == 2

    def test_false_escalation_counts_clean_flags(self):
        cases = [
            ReviewerCase("A", "clean", False, flagged=True),   # false escalation
            ReviewerCase("A", "threshold_shift", False, flagged=True),
        ]
        m = _score(cases)
        assert m["false_escalation_rate"] == 1.0
        # precision = true flags / all flags = 1/2
        assert m["overall_precision"] == 0.5

    def test_error_cases_are_excluded_from_scoring(self):
        cases = [
            ReviewerCase("A", "error", False, flagged=False, error="no-source-text"),
            ReviewerCase("A", "clean", False, flagged=False),
            ReviewerCase("A", "threshold_shift", False, flagged=True, targeted=True),
        ]
        m = _score(cases)
        assert m["error_cases"] == 1
        assert m["seeded_defects"] == 1
        assert m["false_escalation_rate"] == 0.0

    def test_targeted_recall_requires_the_seeded_defect_to_be_named(self):
        cases = [
            ReviewerCase("A", "threshold_shift", False, flagged=True, targeted=False),
            ReviewerCase("A", "threshold_shift", False, flagged=False, targeted=True),
        ]
        m = _score(cases)
        assert m["overall_recall"] == 0.5
        assert m["targeted_recall"] < m["overall_recall"]


class TestCorpusManifest:
    def test_manifest_loads_and_paths_exist(self):
        manifest = yaml.safe_load((_BENCH / "corpus.yaml").read_text())
        for corpus in manifest["corpora"].values():
            assert (INGESTER_ROOT / corpus["source_cpg"]).exists()
            assert corpus["decisions"]
            for dec in corpus["decisions"]:
                assert (INGESTER_ROOT / dec["golden"]).exists()
                assert dec["representative_inputs"]
                assert all(input_spec.get("description") for input_spec in dec["inputs"])

    def test_source_line_ranges_narrow_broad_sections(self):
        markdown = "one\ntwo\nthree\nfour"
        assert _source_text(markdown, {"markdown_lines": "2-3"}) == "two\nthree"

    def test_all_corpus_selector_expands_configured_names(self):
        config = {"corpora": ["hypertension", "diabetes", "atp_iii"]}
        assert rb._expand_corpora("all", config) == config["corpora"]
        assert rb._expand_corpora("diabetes", config) == ["diabetes"]

    def test_mlflow_falls_back_to_local_store(self):
        config = {"mlflow_experiment": "test"}
        with patch.object(rb.mlflow, "set_tracking_uri") as set_uri, \
                patch.object(rb.mlflow, "set_experiment",
                             side_effect=[rb.MlflowException("offline"), None]):
            rb._configure_mlflow(config)
        assert set_uri.call_args.args[0].startswith("file://")

    def test_all_manifest_goldens_pass_local_gates(self):
        manifest = yaml.safe_load((_BENCH / "corpus.yaml").read_text())
        for corpus in manifest["corpora"].values():
            for dec in corpus["decisions"]:
                xml = (INGESTER_ROOT / dec["golden"]).read_text()
                assert validate_dmn_schema(xml) == []
                errors, _warnings = validate_dmn(xml)
                assert errors == []

    def test_assumptions_are_declared_and_representatives_match_golden_rules(self):
        manifest = yaml.safe_load((_BENCH / "corpus.yaml").read_text())
        derivations = (INGESTER_ROOT / "data/golden/README.md").read_text().splitlines()
        section = None
        documented = {}
        for line in derivations:
            heading = re.match(r"^## .* \(`([^`]+)`\)$", line)
            if heading:
                section = heading.group(1)
                documented.setdefault(section, set())
                continue
            row = re.match(r"^\|\s*([^|]+?)\s*\|.*\|\s*(Assumption:.*?)\s*\|$", line)
            if row and section:
                documented[section].add(row.group(1).strip())

        for corpus in manifest["corpora"].values():
            for decision in corpus["decisions"]:
                golden_path = INGESTER_ROOT / decision["golden"]
                model = parse_dmn(golden_path.read_text())
                parsed_decision = next(d for d in model.decisions if d.name == decision["name"])
                rule_ids = {rule.rule_id for rule in parsed_decision.rules}
                assumption_ids = set(decision.get("assumption_rules", []))
                assert assumption_ids <= rule_ids, decision["id"]
                assert documented.get(golden_path.name, set()) == assumption_ids

                for representative in decision["representative_inputs"]:
                    matching = next(
                        (rule for rule in parsed_decision.rules
                         if all(_cell_matches(value, cell, raw)
                                for value, cell, raw in zip(
                                    (representative["inputs"].get(name)
                                     for name in parsed_decision.input_columns),
                                    rule.inputs, rule.raw_inputs))),
                        None,
                    )
                    assert matching is not None, (
                        decision["id"], representative["inputs"], parsed_decision.input_columns
                    )
                    actual = dict(zip(parsed_decision.output_columns, matching.outputs))
                    for name, expected in representative["expect"].items():
                        assert actual[name] == expected, (decision["id"], name, actual, expected)


def _cell_matches(value, cell, raw):
    """Evaluate the subset of FEEL unary cells used by the golden corpus."""
    raw = raw.strip()
    if raw.startswith("not(") and raw.endswith(")"):
        return not _cell_matches(value, normalize_unary(raw[4:-1]), raw[4:-1])
    if cell is UNIVERSAL:
        return True
    if isinstance(cell, Interval):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        return ((value > cell.lo or (cell.lo_inc and value == cell.lo)) and
                (value < cell.hi or (cell.hi_inc and value == cell.hi)))
    if isinstance(cell, ValueSet):
        return value in cell.values
    if isinstance(cell, tuple):
        return any(_cell_matches(value, candidate, "") for candidate in cell)
    return value == cell
