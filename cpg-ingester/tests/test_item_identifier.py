"""Tests for the Item Identifier node."""

import json
import tempfile
from unittest.mock import MagicMock, patch

from cpg_ingester.nodes.item_identifier import (
    _assign_guids,
    _validate_decision,
    _validate_recommendation,
    item_identifier,
)


MOCK_LLM_RESPONSE = json.dumps({
    "decisions": [
        {
            "name": "Treatment Recommendation",
            "description": "Determines initial treatment based on BP and comorbidities",
            "section": "3.2",
            "page_start": 3,
            "page_end": 4,
            "category": "treatment",
            "tier": 1,
            "inputs": [
                {"name": "Systolic BP", "type": "number", "description": "mmHg"},
                {"name": "Has Diabetes", "type": "boolean", "description": "Type 2 DM"},
                {"name": "Has CKD", "type": "boolean", "description": "Chronic kidney disease"},
            ],
            "outputs": ["Start Medication", "Lifestyle Modification Only", "Monitor Only"],
            "hit_policy": "FIRST",
            "cross_references": ["Monitoring Plan"],
        },
        {
            "name": "Monitoring Plan",
            "description": "Determines monitoring schedule based on treatment decision",
            "section": "3.3",
            "page_start": 4,
            "page_end": 5,
            "category": "monitoring",
            "tier": 1,
            "inputs": [
                {"name": "Treatment Action", "type": "string", "description": "From treatment decision"},
                {"name": "Has CKD", "type": "boolean", "description": "Chronic kidney disease"},
            ],
            "outputs": ["BMP in 2 weeks", "BMP in 4 weeks", "Routine follow-up"],
            "hit_policy": "FIRST",
            "cross_references": ["Treatment Recommendation"],
        },
    ],
    "recommendations": [
        {
            "title": "Patient Assessment",
            "description": "Required assessment for all hypertension patients",
            "section": "3.1",
            "page_start": 3,
            "page_end": 3,
            "recommendation_type": "process",
            "certainty_strength": None,
            "certainty_evidence": None,
            "modifies": None,
            "cross_references": [],
        },
        {
            "title": "DASH Diet",
            "description": "Adopt DASH diet for blood pressure reduction",
            "section": "3.4",
            "page_start": 5,
            "page_end": 5,
            "recommendation_type": "lifestyle",
            "certainty_strength": "strong-for",
            "certainty_evidence": "high",
            "modifies": None,
            "cross_references": [],
        },
        {
            "title": "Physical Activity",
            "description": "150 min/week moderate-intensity aerobic exercise",
            "section": "3.4",
            "page_start": 5,
            "page_end": 5,
            "recommendation_type": "lifestyle",
            "certainty_strength": "strong-for",
            "certainty_evidence": "high",
            "modifies": None,
            "cross_references": [],
        },
        {
            "title": "Smoking Cessation",
            "description": "Provide cessation counseling if applicable",
            "section": "3.4",
            "page_start": 5,
            "page_end": 5,
            "recommendation_type": "lifestyle",
            "certainty_strength": None,
            "certainty_evidence": None,
            "modifies": None,
            "cross_references": [],
        },
    ],
})


class TestValidation:

    def test_valid_decision(self):
        item = {
            "name": "Test Decision",
            "category": "treatment",
            "hit_policy": "FIRST",
            "inputs": [{"name": "BP", "type": "number"}],
        }
        assert _validate_decision(item) == []

    def test_missing_name(self):
        item = {"category": "treatment", "inputs": [{"name": "BP"}]}
        issues = _validate_decision(item)
        assert any("name" in i for i in issues)

    def test_invalid_category(self):
        item = {"name": "Test", "category": "invalid", "inputs": [{"name": "BP"}]}
        issues = _validate_decision(item)
        assert any("category" in i for i in issues)

    def test_missing_inputs(self):
        item = {"name": "Test", "category": "treatment"}
        issues = _validate_decision(item)
        assert any("inputs" in i for i in issues)

    def test_accepts_explicit_code_tokens(self):
        item = {
            "name": "Test Decision",
            "inputs": [{
                "name": "Systolic BP",
                "type": "number",
                "codes": ["http://loinc.org|8480-6"],
            }],
        }
        assert _validate_decision(item) == []

    def test_rejects_malformed_code_tokens(self):
        item = {
            "name": "Test Decision",
            "inputs": [{"name": "Systolic BP", "codes": ["not-a-code"]}],
        }
        assert any("invalid codes" in issue for issue in _validate_decision(item))

    def test_valid_recommendation(self):
        item = {
            "title": "Test Rec",
            "recommendation_type": "treatment",
            "certainty_strength": "strong-for",
            "certainty_evidence": "high",
        }
        assert _validate_recommendation(item) == []

    def test_invalid_rec_type(self):
        item = {"title": "Test", "recommendation_type": "invalid"}
        issues = _validate_recommendation(item)
        assert any("recommendation_type" in i for i in issues)

    def test_invalid_strength(self):
        item = {"title": "Test", "certainty_strength": "very-strong"}
        issues = _validate_recommendation(item)
        assert any("certainty_strength" in i for i in issues)


class TestAssignGUIDs:

    def test_assigns_unique_guids(self):
        items = [
            {"type": "decision", "name": "A", "cross_references": []},
            {"type": "decision", "name": "B", "cross_references": []},
            {"type": "recommendation", "title": "C", "cross_references": []},
        ]
        result = _assign_guids(items)
        ids = [i["id"] for i in result]
        assert len(ids) == 3
        assert len(set(ids)) == 3

    def test_resolves_cross_references(self):
        items = [
            {"type": "decision", "name": "Treatment", "cross_references": ["Monitoring"]},
            {"type": "decision", "name": "Monitoring", "cross_references": ["Treatment"]},
        ]
        result = _assign_guids(items)
        assert result[0]["cross_references"][0] == result[1]["id"]
        assert result[1]["cross_references"][0] == result[0]["id"]

    def test_decisions_get_stable_model_ids_without_replacing_item_guids(self):
        items = [
            {"type": "decision", "name": "Treatment Recommendation", "cross_references": []},
            {"type": "recommendation", "title": "DASH Diet", "cross_references": []},
        ]
        result = _assign_guids(items)
        assert result[0]["model_id"] == "treatment-recommendation"
        assert len(result[0]["id"]) == 36
        assert "model_id" not in result[1]

    def test_resolves_modifies(self):
        items = [
            {"type": "recommendation", "title": "Base Rec", "cross_references": []},
            {"type": "recommendation", "title": "Override", "modifies": "Base Rec", "cross_references": []},
        ]
        result = _assign_guids(items)
        assert result[1]["modifies"] == result[0]["id"]

    def test_unresolvable_refs_kept_as_strings(self):
        items = [
            {"type": "decision", "name": "A", "cross_references": ["NonExistent"]},
        ]
        result = _assign_guids(items)
        assert result[0]["cross_references"][0] == "NonExistent"


class TestItemIdentifier:

    def test_with_mocked_llm(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "markdown": "# Recommendations\nSome content",
                "section_map": [{"heading": "Recommendations", "classification": "both", "page_start": 3, "page_end": 5}],
                "abbreviations": {"BP": "Blood Pressure"},
                "output_dir": tmpdir,
            }

            with patch("cpg_ingester.nodes.item_identifier.get_llm", return_value=mock_llm):
                result = item_identifier(state)

            manifest = result["item_manifest"]
            assert len(manifest) == 6
            decisions = [i for i in manifest if i["type"] == "decision"]
            recs = [i for i in manifest if i["type"] == "recommendation"]
            assert len(decisions) == 2
            assert len(recs) == 4

    def test_all_items_have_guids(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "section_map": [], "abbreviations": {}, "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.item_identifier.get_llm", return_value=mock_llm):
                result = item_identifier(state)

            for item in result["item_manifest"]:
                assert "id" in item
                assert len(item["id"]) == 36

    def test_cross_references_resolved_to_guids(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "section_map": [], "abbreviations": {}, "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.item_identifier.get_llm", return_value=mock_llm):
                result = item_identifier(state)

            manifest = result["item_manifest"]
            treatment = next(i for i in manifest if i.get("name") == "Treatment Recommendation")
            monitoring = next(i for i in manifest if i.get("name") == "Monitoring Plan")
            assert treatment["cross_references"][0] == monitoring["id"]
            assert monitoring["cross_references"][0] == treatment["id"]

    def test_writes_manifest_artifact(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "section_map": [], "abbreviations": {}, "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.item_identifier.get_llm", return_value=mock_llm):
                item_identifier(state)

            from pathlib import Path
            manifest_file = Path(tmpdir) / "manifest.json"
            assert manifest_file.exists()
            data = json.loads(manifest_file.read_text())
            assert len(data) == 6

    def test_handles_review_feedback(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_LLM_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "markdown": "content",
                "section_map": [],
                "abbreviations": {},
                "output_dir": tmpdir,
                "classification_review_feedback": "Section 4.3 contains thresholds — should be decision",
                "classification_review_count": 0,
            }
            with patch("cpg_ingester.nodes.item_identifier.get_llm", return_value=mock_llm):
                result = item_identifier(state)

            assert result["classification_review_count"] == 1
            assert result["classification_review_feedback"] == ""

    def test_handles_parse_failure(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content="not valid json"))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {"markdown": "content", "section_map": [], "abbreviations": {}, "output_dir": tmpdir}
            with patch("cpg_ingester.nodes.item_identifier.get_llm", return_value=mock_llm):
                result = item_identifier(state)

            assert result["item_manifest"] == []
