"""Tests for the semantic golden-diff tool and its DMN model parser."""

import sys
from pathlib import Path

import pytest
from lxml import etree

# DMN benchmark lives in tests/benchmarks/dmn (non-package); import by bare name.
_BENCH = Path(__file__).parent / "benchmarks" / "dmn"
sys.path.insert(0, str(_BENCH))

from dmn_diff import _column_alignment, diff_decision, diff_models
from dmn_model import (
    UNIVERSAL,
    Interval,
    ValueSet,
    normalize_output,
    normalize_unary,
    parse_dmn,
)

GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden"
TREATMENT = (GOLDEN_DIR / "treatment-recommendation.dmn").read_text()
MONITORING = (GOLDEN_DIR / "monitoring-plan.dmn").read_text()
DIABETES_TREATMENT = (GOLDEN_DIR / "diabetes-treatment.dmn").read_text()


def _swap_first_two_input_columns(xml: str) -> str:
    root = etree.fromstring(xml.encode("utf-8"))
    table = next(element for element in root.iter()
                 if isinstance(element.tag, str)
                 and element.tag.rsplit("}", 1)[-1] == "decisionTable")
    inputs = [element for element in table
              if isinstance(element.tag, str)
              and element.tag.rsplit("}", 1)[-1] == "input"]
    table.remove(inputs[0])
    table.remove(inputs[1])
    table.insert(0, inputs[1])
    table.insert(1, inputs[0])
    for rule in [element for element in table
                 if isinstance(element.tag, str)
                 and element.tag.rsplit("}", 1)[-1] == "rule"]:
        entries = [element for element in rule
                   if isinstance(element.tag, str)
                   and element.tag.rsplit("}", 1)[-1] == "inputEntry"]
        rule.remove(entries[0])
        rule.remove(entries[1])
        rule.insert(0, entries[1])
        rule.insert(1, entries[0])
    return etree.tostring(root, encoding="unicode")


class TestNormalizeUnary:
    def test_universal(self):
        assert normalize_unary("-") is UNIVERSAL
        assert normalize_unary("") is UNIVERSAL

    def test_comparison_to_interval(self):
        iv = normalize_unary(">= 140")
        assert isinstance(iv, Interval)
        assert iv.lo == 140 and iv.lo_inc and iv.hi == float("inf")

    def test_range_and_conjunction_equivalent(self):
        # [140..180) must equal ">= 140 and < 180"
        a = normalize_unary("[140..180)")
        b = normalize_unary(">= 140 and < 180")
        assert isinstance(a, Interval) and isinstance(b, Interval)
        assert a.bounds_equal(b)

    def test_entity_style_comparison(self):
        # lxml decodes &lt; before we see it, but confirm the operator parses
        assert normalize_unary("< 130").hi == 130

    def test_value_set(self):
        vs = normalize_unary('"A","B"')
        assert isinstance(vs, ValueSet)
        assert vs.values == frozenset({"A", "B"})

    def test_boolean_and_string(self):
        assert normalize_unary("true") is True
        assert normalize_unary('"Start medication"') == "Start medication"


class TestNormalizeOutput:
    def test_strip_quotes(self):
        assert normalize_output('"Lisinopril"') == "Lisinopril"

    def test_number_and_bool_and_null(self):
        assert normalize_output("4") == 4
        assert normalize_output("true") is True
        assert normalize_output("null") is None

    def test_dash(self):
        assert normalize_output('"-"') == "-"
        assert normalize_output("-") == "-"


class TestParse:
    def test_parses_golden(self):
        m = parse_dmn(TREATMENT)
        assert m.name == "Treatment Recommendation"
        assert "Systolic BP" in m.inputs
        assert len(m.decisions) == 1
        dec = m.decisions[0]
        assert dec.hit_policy == "FIRST"
        assert len(dec.rules) == 9
        assert dec.input_columns == ["Systolic BP", "Has Diabetes", "Has Kidney Disease"]


class TestDiffModels:
    def test_identical_scores_one(self):
        report = diff_models(TREATMENT, TREATMENT)
        assert report["structural_f1"] == 1.0
        assert report["structural_recall"] == 1.0
        assert report["structural_precision"] == 1.0
        assert report["threshold_exactness"] == 1.0

    def test_serialized_differently_still_one(self):
        # The diff is namespace-agnostic: a legacy DMN 1.3 serialization of the
        # same model must still score 1.0 against the migrated 1.4 golden.
        variant = TREATMENT.replace(
            "https://www.omg.org/spec/DMN/20211108/MODEL/",
            "https://www.omg.org/spec/DMN/20191111/MODEL/",
        ).replace(
            "https://www.omg.org/spec/DMN/20211108/FEEL/",
            "https://www.omg.org/spec/DMN/20191111/FEEL/",
        )
        report = diff_models(TREATMENT, variant)
        assert report["structural_f1"] == 1.0

    def test_threshold_off_by_five_keeps_f1_drops_exactness(self):
        # Shift rule 1's ">= 140" to ">= 145": rule still overlaps -> matched,
        # F1 stays 1.0, but threshold-exactness drops and a delta is reported.
        mutated = TREATMENT.replace(
            "<text><![CDATA[>= 140]]></text>", "<text><![CDATA[>= 145]]></text>"
        )
        report = diff_models(TREATMENT, mutated)
        assert report["structural_f1"] == 1.0
        assert report["threshold_exact_rules"] == report["matched_rules"] - 1
        deltas = report["decisions"][0]["threshold_deltas"]
        assert len(deltas) == 1

    def test_dropped_rule_lowers_recall(self):
        mutated = TREATMENT.replace(
            """      <!-- Rule 5: SBP 130-139, No Diabetes, No Kidney Disease -->
      <rule id="rule_5">
        <description>Stage 1 hypertension without comorbidities - lifestyle only</description>
        <inputEntry id="r5_ie1"><text><![CDATA[[130..139]]]></text></inputEntry>
        <inputEntry id="r5_ie2"><text><![CDATA[false]]></text></inputEntry>
        <inputEntry id="r5_ie3"><text><![CDATA[false]]></text></inputEntry>
        <outputEntry id="r5_oe1"><text><![CDATA["Lifestyle modification only"]]></text></outputEntry>
        <outputEntry id="r5_oe2"><text><![CDATA["-"]]></text></outputEntry>
        <outputEntry id="r5_oe3"><text><![CDATA["-"]]></text></outputEntry>
        <outputEntry id="r5_oe4"><text><![CDATA[8]]></text></outputEntry>
      </rule>
""", "")
        report = diff_models(TREATMENT, mutated)
        assert report["structural_recall"] < 1.0
        assert report["generated_rule_count"] == 8
        assert len(report["decisions"][0]["missing_rules"]) == 1

    def test_extra_rule_lowers_precision(self):
        extra = """      <rule id="rule_extra">
        <inputEntry id="rx_ie1"><text>>= 200</text></inputEntry>
        <inputEntry id="rx_ie2"><text>-</text></inputEntry>
        <inputEntry id="rx_ie3"><text>-</text></inputEntry>
        <outputEntry id="rx_oe1"><text>"Emergency referral"</text></outputEntry>
        <outputEntry id="rx_oe2"><text>"-"</text></outputEntry>
        <outputEntry id="rx_oe3"><text>"-"</text></outputEntry>
        <outputEntry id="rx_oe4"><text>1</text></outputEntry>
      </rule>
    </decisionTable>"""
        mutated = TREATMENT.replace("    </decisionTable>", extra)
        report = diff_models(TREATMENT, mutated)
        assert report["structural_precision"] < 1.0
        assert report["generated_rule_count"] == 10
        assert len(report["decisions"][0]["extra_rules"]) == 1

    def test_wrong_output_reported_as_output_delta(self):
        mutated = MONITORING.replace(
            '<outputEntry id="r1_oe1"><text><![CDATA["Basic Metabolic Panel"]]></text></outputEntry>',
            '<outputEntry id="r1_oe1"><text><![CDATA["No labs required"]]></text></outputEntry>',
        )
        report = diff_models(MONITORING, mutated)
        # Same conditions -> rule matched, but output differs.
        assert report["structural_f1"] == 1.0
        assert len(report["decisions"][0]["output_deltas"]) == 1
        assert report["decisions"][0]["output_exactness"] < 1.0
        assert report["decisions"][0]["decision_exact"] is False

    def test_column_reorder_is_aligned_by_name(self):
        report = diff_models(TREATMENT, _swap_first_two_input_columns(TREATMENT))
        assert report["structural_f1"] == 1.0
        assert report["decisions"][0]["decision_exact"] is True

    def test_column_names_are_normalized_for_case_and_whitespace(self):
        mutated = TREATMENT.replace(
            "<text><![CDATA[Systolic BP]]></text>",
            "<text><![CDATA[  systolic   BP ]]></text>",
            1,
        )
        report = diff_models(TREATMENT, mutated)
        assert report["structural_f1"] == 1.0
        assert report["decisions"][0]["inputs_match"] is True

    def test_positional_fallback_requires_all_column_names_to_be_blank(self):
        golden = parse_dmn(TREATMENT).decisions[0]
        generated = parse_dmn(TREATMENT).decisions[0]
        for columns_attribute in ("input_columns", "output_columns"):
            golden_columns = getattr(golden, columns_attribute)
            generated_columns = getattr(generated, columns_attribute)
            setattr(golden, columns_attribute, ["  "] * len(golden_columns))
            setattr(generated, columns_attribute, [""] * len(generated_columns))

        result = diff_decision(golden, generated)

        assert result.inputs_match is True
        assert result.outputs_match is True
        assert result.f1 == 1.0
        assert result.decision_exact is True
        assert result.unmatched_golden_columns == []
        assert result.unmatched_generated_columns == []

    def test_blank_column_is_not_aligned_when_other_names_are_present(self):
        mapping, missing, extra = _column_alignment(
            ["Systolic BP", ""],
            ["Temperature", ""],
        )

        assert mapping == [None, None]
        assert missing == ["Systolic BP", ""]
        assert extra == ["Temperature", ""]

    def test_wrong_column_identity_makes_identical_rules_unmatched(self):
        mutated = TREATMENT.replace(
            "<text><![CDATA[Systolic BP]]></text>",
            "<text><![CDATA[Temperature]]></text>",
            1,
        ).replace(
            "<text><![CDATA[Has Diabetes]]></text>",
            "<text><![CDATA[Smoking]]></text>",
            1,
        )
        report = diff_models(TREATMENT, mutated)
        decision = report["decisions"][0]

        assert decision["inputs_match"] is False
        assert decision["unmatched_golden_columns"] == ["Systolic BP", "Has Diabetes"]
        assert decision["unmatched_generated_columns"] == ["Temperature", "Smoking"]
        assert decision["decision_exact"] is False
        assert report["structural_f1"] == 0.0

    def test_hit_policy_is_part_of_decision_exactness(self):
        mutated = TREATMENT.replace('hitPolicy="FIRST"', 'hitPolicy="UNIQUE"', 1)
        report = diff_models(TREATMENT, mutated)
        assert report["decisions"][0]["hit_policy_match"] is False
        assert report["decisions"][0]["decision_exact"] is False

    def test_renamed_column_is_not_silently_dropped(self):
        mutated = TREATMENT.replace("<text><![CDATA[Systolic BP]]></text>",
                                    "<text><![CDATA[Diastolic BP]]></text>", 1)
        report = diff_models(TREATMENT, mutated)
        decision = report["decisions"][0]
        assert decision["inputs_match"] is False
        assert decision["unmatched_golden_columns"] == ["Systolic BP"]
        assert decision["unmatched_generated_columns"] == ["Diastolic BP"]
        assert report["structural_f1"] == 0.0

    def test_single_renamed_hba1c_column_has_no_positional_fallback(self):
        mutated = DIABETES_TREATMENT.replace(
            "<text><![CDATA[HbA1c]]></text>",
            "<text><![CDATA[HbA1c Percent]]></text>",
            1,
        )
        report = diff_models(DIABETES_TREATMENT, mutated)
        decision = report["decisions"][0]

        assert decision["inputs_match"] is False
        assert decision["unmatched_golden_columns"] == ["HbA1c"]
        assert decision["unmatched_generated_columns"] == ["HbA1c Percent"]
        assert decision["decision_exact"] is False
        assert report["structural_f1"] == 0.0

    def test_assumption_rule_can_be_omitted_without_lowering_recall(self):
        mutated = TREATMENT.replace(
            """      <!-- Rule 9: SBP < 130, Diabetes, Kidney Disease -->
      <rule id="rule_9">""", """      <!-- Rule 9: SBP < 130, Diabetes, Kidney Disease -->
      <rule id="rule_9">""", 1)
        # Remove the final rule while retaining the rest of the golden.
        start = mutated.index('      <rule id="rule_9">')
        end = mutated.index("      </rule>", start) + len("      </rule>\n")
        mutated = mutated[:start] + mutated[end:]
        report = diff_models(TREATMENT, mutated,
                             {"Treatment Recommendation": {"rule_9"}})
        decision = report["decisions"][0]
        assert report["structural_recall"] == 1.0
        assert decision["missing_rules"] == []
        assert decision["unmatched_assumption_rules"][0]["rule_id"] == "rule_9"

    def test_assumption_rule_is_consumed_from_precision(self):
        report = diff_models(TREATMENT, TREATMENT,
                             {"Treatment Recommendation": {"rule_9"}})
        decision = report["decisions"][0]
        assert report["structural_precision"] == 1.0
        assert decision["extra_rules"] == []
        assert decision["assumption_rules_matched"] == 1

    def test_assumption_output_difference_is_not_scored(self):
        mutated = TREATMENT.replace(
            '<outputEntry id="r9_oe1"><text><![CDATA["Lifestyle modification only"]]></text></outputEntry>',
            '<outputEntry id="r9_oe1"><text><![CDATA["Different assumption"]]></text></outputEntry>',
        )
        report = diff_models(TREATMENT, mutated,
                             {"Treatment Recommendation": {"rule_9"}})
        decision = report["decisions"][0]
        assert decision["extra_rules"] == []
        assert decision["output_deltas"] == []

    def test_unknown_assumption_rule_is_rejected(self):
        with pytest.raises(ValueError, match="assumption rule ids"):
            diff_models(TREATMENT, TREATMENT,
                        {"Treatment Recommendation": {"not-a-rule"}})
