"""Tests for vendored DMN 1.4 XML Schema validation."""

from pathlib import Path

from cpg_ingester.reference.dmn_examples import DMN_TEMPLATE
from cpg_ingester.validators.dmn_schema import validate_dmn_schema

GOLDEN_DIR = Path(__file__).parent.parent / "data" / "golden"


def test_goldens_and_reference_template_pass_dmn_14_schema():
    for model in [*GOLDEN_DIR.glob("*.dmn")]:
        assert validate_dmn_schema(model.read_text()) == [], model.name
    assert validate_dmn_schema(DMN_TEMPLATE) == []


def test_schema_reports_line_numbers_for_structural_violations():
    fixtures = [
        DMN_TEMPLATE.replace('namespace="https://redhat.com/cpg-to-acp/dmn/treatment-recommendation"', ""),
        DMN_TEMPLATE.replace('id="input_systolic"', 'id="input systolic"'),
        DMN_TEMPLATE.replace('href="#input_systolic"', ""),
        DMN_TEMPLATE.replace(
            '<output id="out_1" name="Recommendation" typeRef="string">',
            '<unexpectedElement/>\n      <output id="out_1" name="Recommendation" typeRef="string">',
        ),
        DMN_TEMPLATE.replace(
            'https://www.omg.org/spec/DMN/20211108/MODEL/',
            'https://www.omg.org/spec/DMN/20191111/MODEL/',
        ),
    ]
    for fixture in fixtures:
        errors = validate_dmn_schema(fixture)
        assert errors
        assert all(error.startswith("XSD line ") for error in errors)
