"""Tests for DMN Creator and Syntax Validator."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cpg_ingester.nodes.dmn_creator import _format_inputs, _strip_markdown_fences, dmn_creator
from cpg_ingester.prompts.dmn_creator import DMN_CREATOR_SYSTEM
from cpg_ingester.reference.dmn_examples import REFERENCE_EXAMPLES
from cpg_ingester.nodes.dmn_syntax_validator import dmn_syntax_validator
from cpg_ingester.validators.dmn_syntax import validate_dmn_xml

GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden"
TREATMENT_DMN = GOLDEN_DIR / "treatment-recommendation.dmn"
MONITORING_DMN = GOLDEN_DIR / "monitoring-plan.dmn"


class TestDMNSyntaxValidator:

    @pytest.mark.skipif(not TREATMENT_DMN.exists(), reason="Golden DMN not found")
    def test_golden_treatment_dmn_is_valid(self):
        errors = validate_dmn_xml(TREATMENT_DMN.read_text())
        assert errors == [], f"Golden treatment DMN has errors: {errors}"

    @pytest.mark.skipif(not MONITORING_DMN.exists(), reason="Golden DMN not found")
    def test_golden_monitoring_dmn_is_valid(self):
        errors = validate_dmn_xml(MONITORING_DMN.read_text())
        assert errors == [], f"Golden monitoring DMN has errors: {errors}"

    def test_catches_malformed_xml(self):
        errors = validate_dmn_xml("<not-closed>")
        assert any("XML parse" in e for e in errors)

    def test_catches_wrong_namespace(self):
        xml = '''<?xml version="1.0"?>
        <definitions xmlns="http://wrong.namespace/DMN/"
                     id="test" name="Test" namespace="http://wrong/">
          <decision id="d1" name="D1">
            <variable id="v1" name="D1" typeRef="string"/>
            <decisionTable id="dt1" hitPolicy="FIRST">
              <input id="i1"><inputExpression id="ie1" typeRef="number"><text>X</text></inputExpression></input>
              <output id="o1" name="Y" typeRef="string"/>
              <rule id="r1"><inputEntry id="r1i1"><text>>= 1</text></inputEntry>
              <outputEntry id="r1o1"><text>"A"</text></outputEntry></rule>
            </decisionTable>
          </decision>
        </definitions>'''
        errors = validate_dmn_xml(xml)
        assert any("namespace" in e.lower() for e in errors)

    def test_catches_missing_hit_policy(self):
        xml = '''<?xml version="1.0"?>
        <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                     id="test" name="Test" namespace="https://www.omg.org/spec/DMN/20211108/MODEL/">
          <decision id="d1" name="D1">
            <variable id="v1" name="D1" typeRef="string"/>
            <decisionTable id="dt1">
              <input id="i1"><inputExpression id="ie1" typeRef="number"><text>X</text></inputExpression></input>
              <output id="o1" name="Y" typeRef="string"/>
              <rule id="r1"><inputEntry id="r1i1"><text>>= 1</text></inputEntry>
              <outputEntry id="r1o1"><text>"A"</text></outputEntry></rule>
            </decisionTable>
          </decision>
        </definitions>'''
        errors = validate_dmn_xml(xml)
        assert any("hitPolicy" in e for e in errors)

    def test_catches_missing_type_ref(self):
        xml = '''<?xml version="1.0"?>
        <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                     id="test" name="Test" namespace="https://www.omg.org/spec/DMN/20211108/MODEL/">
          <decision id="d1" name="D1">
            <variable id="v1" name="D1" typeRef="string"/>
            <decisionTable id="dt1" hitPolicy="FIRST">
              <input id="i1"><inputExpression id="ie1"><text>X</text></inputExpression></input>
              <output id="o1" name="Y" typeRef="string"/>
              <rule id="r1"><inputEntry id="r1i1"><text>>= 1</text></inputEntry>
              <outputEntry id="r1o1"><text>"A"</text></outputEntry></rule>
            </decisionTable>
          </decision>
        </definitions>'''
        errors = validate_dmn_xml(xml)
        assert any("typeRef" in e for e in errors)

    def test_catches_wrong_entry_count(self):
        xml = '''<?xml version="1.0"?>
        <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                     id="test" name="Test" namespace="https://www.omg.org/spec/DMN/20211108/MODEL/">
          <decision id="d1" name="D1">
            <variable id="v1" name="D1" typeRef="string"/>
            <decisionTable id="dt1" hitPolicy="FIRST">
              <input id="i1"><inputExpression id="ie1" typeRef="number"><text>X</text></inputExpression></input>
              <input id="i2"><inputExpression id="ie2" typeRef="boolean"><text>Y</text></inputExpression></input>
              <output id="o1" name="Z" typeRef="string"/>
              <rule id="r1">
                <inputEntry id="r1i1"><text>>= 1</text></inputEntry>
                <outputEntry id="r1o1"><text>"A"</text></outputEntry>
              </rule>
            </decisionTable>
          </decision>
        </definitions>'''
        errors = validate_dmn_xml(xml)
        assert any("inputEntries" in e for e in errors)

    def test_catches_empty_text(self):
        xml = '''<?xml version="1.0"?>
        <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                     id="test" name="Test" namespace="https://www.omg.org/spec/DMN/20211108/MODEL/">
          <decision id="d1" name="D1">
            <variable id="v1" name="D1" typeRef="string"/>
            <decisionTable id="dt1" hitPolicy="FIRST">
              <input id="i1"><inputExpression id="ie1" typeRef="number"><text>X</text></inputExpression></input>
              <output id="o1" name="Y" typeRef="string"/>
              <rule id="r1">
                <inputEntry id="r1i1"><text></text></inputEntry>
                <outputEntry id="r1o1"><text>"A"</text></outputEntry>
              </rule>
            </decisionTable>
          </decision>
        </definitions>'''
        errors = validate_dmn_xml(xml)
        assert any("empty text" in e for e in errors)

    def test_catches_no_rules(self):
        xml = '''<?xml version="1.0"?>
        <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                     id="test" name="Test" namespace="https://www.omg.org/spec/DMN/20211108/MODEL/">
          <decision id="d1" name="D1">
            <variable id="v1" name="D1" typeRef="string"/>
            <decisionTable id="dt1" hitPolicy="FIRST">
              <input id="i1"><inputExpression id="ie1" typeRef="number"><text>X</text></inputExpression></input>
              <output id="o1" name="Y" typeRef="string"/>
            </decisionTable>
          </decision>
        </definitions>'''
        errors = validate_dmn_xml(xml)
        assert any("no rules" in e for e in errors)

    def test_catches_missing_input_data_variable(self):
        xml = '''<?xml version="1.0"?>
        <definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/"
                     id="test" name="Test" namespace="https://www.omg.org/spec/DMN/20211108/MODEL/">
          <inputData id="id1" name="X"/>
          <decision id="d1" name="D1">
            <variable id="v1" name="D1" typeRef="string"/>
            <informationRequirement id="ir1"><requiredInput href="#id1"/></informationRequirement>
            <decisionTable id="dt1" hitPolicy="FIRST">
              <input id="i1"><inputExpression id="ie1" typeRef="number"><text>X</text></inputExpression></input>
              <output id="o1" name="Y" typeRef="string"/>
              <rule id="r1"><inputEntry id="r1i1"><text>>= 1</text></inputEntry>
              <outputEntry id="r1o1"><text>"A"</text></outputEntry></rule>
            </decisionTable>
          </decision>
        </definitions>'''
        errors = validate_dmn_xml(xml)
        assert any("variable" in e.lower() for e in errors)


class TestDMNSyntaxValidatorNode:

    def test_passes_valid_dmn(self):
        state = {"dmn_xml": TREATMENT_DMN.read_text(), "item": {"name": "Test"}}
        result = dmn_syntax_validator(state)
        assert result["syntax_errors"] == []

    def test_fails_empty_xml(self):
        state = {"dmn_xml": "", "item": {"name": "Test"}}
        result = dmn_syntax_validator(state)
        assert len(result["syntax_errors"]) > 0

    def test_returns_errors_for_bad_xml(self):
        state = {"dmn_xml": "<broken>", "item": {"name": "Test"}}
        result = dmn_syntax_validator(state)
        assert any("XML" in e for e in result["syntax_errors"])

    def test_control_character_returns_only_targeted_error_and_hint(self):
        from cpg_ingester.reference.dmn_error_patterns import format_error_pattern_hints

        result = dmn_syntax_validator({
            "dmn_xml": TREATMENT_DMN.read_text() + "\x01",
            "item": {"name": "Test"},
        })
        assert len(result["syntax_errors"]) == 1
        assert "forbidden control character" in result["syntax_errors"][0]
        hint = format_error_pattern_hints(result["syntax_errors"])
        assert "control characters" in hint
        assert "CDATA" not in hint

    def test_dmn_13_namespace_returns_targeted_error_and_hint(self):
        from cpg_ingester.reference.dmn_error_patterns import format_error_pattern_hints

        result = dmn_syntax_validator({
            "dmn_xml": TREATMENT_DMN.read_text().replace(
                "https://www.omg.org/spec/DMN/20211108/MODEL/",
                "http://www.omg.org/spec/DMN/20191111/MODEL/",
            ),
            "item": {"name": "Test"},
        })
        assert any("Wrong namespace" in error for error in result["syntax_errors"])
        assert "namespace" in format_error_pattern_hints(result["syntax_errors"]).lower()


class TestStripMarkdownFences:

    def test_strips_xml_fence(self):
        text = "```xml\n<root/>\n```"
        assert _strip_markdown_fences(text) == "<root/>"

    def test_strips_plain_fence(self):
        text = "```\n<root/>\n```"
        assert _strip_markdown_fences(text) == "<root/>"

    def test_no_fence(self):
        text = "<root/>"
        assert _strip_markdown_fences(text) == "<root/>"


class TestDMNCreatorReference:

    def test_reference_template_has_no_unsubstituted_placeholders(self):
        assert "{" not in REFERENCE_EXAMPLES
        assert "}" not in REFERENCE_EXAMPLES

    def test_creator_prompt_teaches_code_annotations_and_ordering(self):
        prompt = DMN_CREATOR_SYSTEM.format(reference=REFERENCE_EXAMPLES)
        assert "<acp:clinicalCode system=" in prompt
        assert "element order" in prompt

    def test_reference_allows_first_for_ordered_rules(self):
        assert "Avoid it" not in REFERENCE_EXAMPLES
        assert "PRIORITY or FIRST" in REFERENCE_EXAMPLES

    def test_input_format_includes_explicit_codes(self):
        rendered = _format_inputs([{
            "name": "Systolic BP",
            "type": "number",
            "codes": ["http://loinc.org|8480-6"],
        }])
        assert "http://loinc.org|8480-6" in rendered


class TestDMNCreatorNode:

    def test_produces_xml(self):
        mock_dmn = TREATMENT_DMN.read_text()
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=mock_dmn))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item": {
                    "name": "Treatment Recommendation",
                    "description": "Determines treatment",
                    "category": "treatment",
                    "hit_policy": "FIRST",
                    "inputs": [{"name": "Systolic BP", "type": "number"}],
                    "outputs": ["Start medication"],
                },
                "source_pages": "Patients with SBP >= 140...",
                "abbreviations": {},
                "output_dir": tmpdir,
            }
            with patch("cpg_ingester.nodes.dmn_creator.get_llm", return_value=mock_llm):
                result = dmn_creator(state)

            assert result["dmn_xml"]
            assert "definitions" in result["dmn_xml"]
            assert result["syntax_errors"] == []

    def test_writes_dmn_artifact(self):
        mock_dmn = TREATMENT_DMN.read_text()
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=mock_dmn))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item": {"name": "Treatment Recommendation", "category": "treatment", "hit_policy": "FIRST", "inputs": [], "outputs": []},
                "source_pages": "",
                "abbreviations": {},
                "output_dir": tmpdir,
            }
            with patch("cpg_ingester.nodes.dmn_creator.get_llm", return_value=mock_llm):
                dmn_creator(state)

            dmn_files = list(Path(tmpdir).rglob("*.dmn"))
            assert len(dmn_files) == 1

    def test_includes_feedback_on_retry(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content="<definitions/>"))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "item": {"name": "Test", "category": "treatment", "hit_policy": "FIRST", "inputs": [], "outputs": []},
                "source_pages": "",
                "abbreviations": {},
                "output_dir": tmpdir,
                "dmn_xml": "<definitions>previous broken attempt</definitions>",
                "syntax_errors": ["Missing hitPolicy attribute"],
                "syntax_retry_count": 0,
            }
            with patch("cpg_ingester.nodes.dmn_creator.get_llm", return_value=mock_llm):
                result = dmn_creator(state)

            assert result["syntax_retry_count"] == 1
            call_args = mock_llm.invoke.call_args[0][0]
            user_msg = call_args[1]["content"]
            assert "Syntax errors to fix" in user_msg
            # Repair mode: the previous attempt is included so the model corrects it.
            assert "previous broken attempt" in user_msg
