"""Tests for the seeded-defect injectors.

Each injector must (a) produce well-formed XML, (b) differ from the original
only as intended, verified via the golden-diff tool.
"""

import sys
from pathlib import Path

import pytest
import yaml
from lxml import etree

# DMN benchmark lives in tests/benchmarks/dmn (non-package); import by bare name.
_BENCH = Path(__file__).parent / "benchmarks" / "dmn"
sys.path.insert(0, str(_BENCH))

from defects import (
    DefectNotApplicable,
    INJECTORS,
    drop_rule,
    fabricate_input,
    threshold_shift,
    wrong_hit_policy,
    wrong_output,
    _parse,
    _serialize,
    _strip_comments,
)
from dmn_diff import diff_models
from dmn_model import parse_dmn
from cpg_ingester.validators.dmn_schema import validate_dmn_schema
from cpg_ingester.validators.dmn_syntax import validate_dmn

GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden"
TREATMENT = (GOLDEN_DIR / "treatment-recommendation.dmn").read_text()
MONITORING = (GOLDEN_DIR / "monitoring-plan.dmn").read_text()


def _well_formed(xml: str):
    etree.fromstring(xml.encode("utf-8"))  # raises on malformed


class TestInjectorsProduceWellFormedXml:
    @pytest.mark.parametrize("name,injector", list(INJECTORS.items()))
    def test_well_formed(self, name, injector):
        mutated, desc = injector(TREATMENT)
        _well_formed(mutated)
        assert desc.defect_class == name
        assert desc.decision


class TestThresholdShift:
    def test_shifts_one_bound(self):
        mutated, desc = threshold_shift(TREATMENT, delta=5)
        report = diff_models(TREATMENT, mutated)
        # Rule still overlaps -> matched, but exactness drops by exactly one.
        assert report["threshold_exact_rules"] == report["matched_rules"] - 1
        assert "delta 5" in desc.detail

    def test_shifts_both_bounds_of_a_range(self):
        ranged = TREATMENT.replace("<![CDATA[>= 140]]>", "<![CDATA[[130..139]]]>", 1)
        mutated, _desc = threshold_shift(ranged, delta=5)
        assert "[135..144]" in mutated

    def test_reversing_threshold_shift_restores_clean_tree(self):
        shifted, _desc = threshold_shift(TREATMENT, delta=5)
        restored, _desc = threshold_shift(shifted, delta=-5)
        expected = _serialize(_strip_comments(_parse(TREATMENT)))
        assert restored == expected


class TestDropRule:
    def test_removes_exactly_one_rule(self):
        before = len(parse_dmn(TREATMENT).decisions[0].rules)
        mutated, desc = drop_rule(TREATMENT)
        after = len(parse_dmn(mutated).decisions[0].rules)
        assert after == before - 1
        report = diff_models(TREATMENT, mutated)
        assert len(report["decisions"][0]["missing_rules"]) == 1


class TestFabricateInput:
    def test_adds_one_input_column(self):
        before = len(parse_dmn(TREATMENT).decisions[0].input_columns)
        mutated, desc = fabricate_input(TREATMENT)
        gen = parse_dmn(mutated)
        assert len(gen.decisions[0].input_columns) == before + 1
        assert "Patient Zodiac Sign" in gen.inputs
        # Every rule got a matching (any) entry so entry counts stay aligned.
        for rule in gen.decisions[0].rules:
            assert len(rule.inputs) == before + 1


class TestWrongOutput:
    def test_swaps_action(self):
        mutated, desc = wrong_output(TREATMENT)
        report = diff_models(TREATMENT, mutated)
        assert len(report["decisions"][0]["output_deltas"]) >= 1

    def test_is_corpus_agnostic(self):
        diabetes = (GOLDEN_DIR / "diabetes-treatment.dmn").read_text()
        mutated, desc = wrong_output(diabetes)
        report = diff_models(diabetes, mutated)
        assert desc.location["was"] == "Combination therapy"
        assert desc.location["now"] == "Metformin plus lifestyle"
        assert len(report["decisions"][0]["output_deltas"]) == 1

    def test_numeric_only_outputs_are_not_applicable(self):
        diabetes = (GOLDEN_DIR / "diabetes-monitoring.dmn").read_text()
        with pytest.raises(DefectNotApplicable):
            wrong_output(diabetes)


class TestProductionGates:
    def test_every_applicable_mutant_passes_local_gates(self):
        manifest = yaml.safe_load(
            (Path(__file__).parent / "benchmarks" / "dmn" / "corpus.yaml").read_text()
        )
        for corpus in manifest["corpora"].values():
            for decision in corpus["decisions"]:
                golden = (Path(__file__).parent.parent / decision["golden"]).read_text()
                for injector in INJECTORS.values():
                    try:
                        mutated, _desc = injector(golden)
                    except DefectNotApplicable:
                        continue
                    assert validate_dmn_schema(mutated) == []
                    assert validate_dmn(mutated)[0] == []


class TestWrongHitPolicy:
    def test_changes_hit_policy_only(self):
        mutated, desc = wrong_hit_policy(TREATMENT)
        gen = parse_dmn(mutated)
        assert gen.decisions[0].hit_policy != "FIRST"
        # Rules and I/O unchanged.
        report = diff_models(TREATMENT, mutated)
        assert report["structural_f1"] == 1.0
        assert report["decisions"][0]["hit_policy_match"] is False
        assert report["decisions"][0]["decision_exact"] is False
