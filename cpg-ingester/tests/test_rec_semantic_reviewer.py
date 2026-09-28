"""Tests for the Recommendation Semantic Reviewer node."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from cpg_ingester.nodes.rec_semantic_reviewer import rec_semantic_reviewer


MOCK_PASSED_RESPONSE = json.dumps({
    "checks": [
        {"recommendation_title": "DASH Diet", "content_faithful": True, "certainty_accurate": True, "type_correct": True, "issues": []},
        {"recommendation_title": "Physical Activity", "content_faithful": True, "certainty_accurate": True, "type_correct": True, "issues": []},
    ],
    "missing_recommendations": [],
    "discrepancies_found": False,
    "summary": "",
    "discrepancies": [],
})

MOCK_FAILED_RESPONSE = json.dumps({
    "checks": [
        {"recommendation_title": "DASH Diet", "content_faithful": True, "certainty_accurate": True, "type_correct": True, "issues": []},
        {
            "recommendation_title": "Physical Activity",
            "content_faithful": False,
            "certainty_accurate": True,
            "type_correct": True,
            "issues": ["Content says 'must engage' but source says 'engage in at least' — language strengthened"],
        },
    ],
    "missing_recommendations": [
        "Source mentions alcohol limitation but no recommendation was extracted",
    ],
    "discrepancies_found": True,
    "summary": "Language strengthened in Physical Activity rec; alcohol limitation rec missing",
    "discrepancies": [
        "Physical Activity: content says 'must engage' but source says 'engage in at least'",
        "Missing recommendation for alcohol limitation from section 3.4",
    ],
})

SAMPLE_RECS = [
    {"id": "r1", "title": "DASH Diet", "content": "Adopt the DASH diet.", "recommendation_type": "lifestyle"},
    {"id": "r2", "title": "Physical Activity", "content": "Must engage in 150 min/week.", "recommendation_type": "lifestyle"},
]


class TestRecSemanticReviewer:

    def test_passes_valid_recs(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_PASSED_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": "source text about DASH and exercise",
                "output_dir": tmpdir,
                "review_count": 0,
                "items": [{"section": "3.4"}],
            }
            with patch("cpg_ingester.nodes.rec_semantic_reviewer.get_llm", return_value=mock_llm):
                result = rec_semantic_reviewer(state)

            assert result["semantic_discrepancies"] == []

    def test_finds_discrepancies(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_FAILED_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": "source text",
                "output_dir": tmpdir,
                "review_count": 0,
                "items": [{"section": "3.4"}],
            }
            with patch("cpg_ingester.nodes.rec_semantic_reviewer.get_llm", return_value=mock_llm):
                result = rec_semantic_reviewer(state)

            assert len(result["semantic_discrepancies"]) == 2
            assert any("must engage" in d for d in result["semantic_discrepancies"])
            assert any("alcohol" in d for d in result["semantic_discrepancies"])

    def test_writes_review_report(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_PASSED_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": "source text",
                "output_dir": tmpdir,
                "review_count": 0,
                "items": [{"section": "3.4"}],
            }
            with patch("cpg_ingester.nodes.rec_semantic_reviewer.get_llm", return_value=mock_llm):
                rec_semantic_reviewer(state)

            reports = list(Path(tmpdir).glob("rec-review-*.json"))
            assert len(reports) == 1
            report = json.loads(reports[0].read_text())
            assert report["recommendations_checked"] == 2
            assert report["passed"] == 2
            assert report["with_issues"] == 0

    def test_no_source_pages_force_escalates_without_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": "",
                "output_dir": tmpdir,
                "review_count": 0,
                "items": [],
            }
            with patch("cpg_ingester.nodes.rec_semantic_reviewer.get_llm") as mock_get_llm:
                result = rec_semantic_reviewer(state)

            assert result["force_escalate"] is True
            assert result["escalation_reason"] == "no-source-text"
            assert result["semantic_discrepancies"]
            mock_get_llm.assert_not_called()

    def test_no_recs_returns_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": [],
                "source_pages": "source",
                "output_dir": tmpdir,
                "items": [],
            }
            result = rec_semantic_reviewer(state)
            assert len(result["semantic_discrepancies"]) > 0

    def test_reasks_then_escalates_after_parse_failure(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content="not json"),
            MagicMock(content="still not json"),
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": "source",
                "output_dir": tmpdir,
                "review_count": 0,
                "items": [{"section": "3.4"}],
            }
            with patch("cpg_ingester.nodes.rec_semantic_reviewer.get_llm", return_value=mock_llm):
                result = rec_semantic_reviewer(state)

            assert result["force_escalate"] is True
            assert result["escalation_reason"] == "reviewer-unparseable"
            assert result["semantic_discrepancies"]
            assert mock_llm.invoke.call_count == 2
            second_call_messages = mock_llm.invoke.call_args_list[1].args[0]
            assert "not valid JSON" in second_call_messages[-1]["content"]

    def test_reasks_on_invalid_schema_then_accepts_valid_reply(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content=json.dumps({
                "discrepancies_found": False,
                "discrepancies": [],
                "missing_recommendations": [],
            })),
            MagicMock(content=MOCK_PASSED_RESPONSE),
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": "source text",
                "output_dir": tmpdir,
                "review_count": 0,
                "items": [{"section": "3.4"}],
            }
            with patch("cpg_ingester.nodes.rec_semantic_reviewer.get_llm", return_value=mock_llm):
                result = rec_semantic_reviewer(state)

        assert result["semantic_discrepancies"] == []
        assert "schema" in mock_llm.invoke.call_args_list[1].args[0][-1]["content"].lower()
        assert "checks" in mock_llm.invoke.call_args_list[1].args[0][-1]["content"]

    def test_invalid_json_shape_after_reask_escalates(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content="{}"),
            MagicMock(content="{}"),
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": "source text",
                "output_dir": tmpdir,
                "review_count": 0,
                "items": [{"section": "3.4"}],
            }
            with patch("cpg_ingester.nodes.rec_semantic_reviewer.get_llm", return_value=mock_llm):
                result = rec_semantic_reviewer(state)

        assert result["force_escalate"] is True
        assert result["escalation_reason"] == "reviewer-unparseable"
        assert "valid JSON" in result["semantic_discrepancies"][0]

    def test_discrepancy_flag_requires_discrepancy_details(self):
        invalid_response = json.dumps({
            "checks": [{
                "recommendation_title": "DASH Diet",
                "content_faithful": True,
                "certainty_accurate": True,
                "type_correct": True,
                "issues": [],
            }, {
                "recommendation_title": "Physical Activity",
                "content_faithful": True,
                "certainty_accurate": True,
                "type_correct": True,
                "issues": [],
            }],
            "missing_recommendations": [],
            "discrepancies_found": True,
            "discrepancies": [],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content=invalid_response),
            MagicMock(content=MOCK_PASSED_RESPONSE),
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": "source text",
                "output_dir": tmpdir,
                "review_count": 0,
                "items": [{"section": "3.4"}],
            }
            with patch("cpg_ingester.nodes.rec_semantic_reviewer.get_llm", return_value=mock_llm):
                result = rec_semantic_reviewer(state)

        assert result["semantic_discrepancies"] == []
        assert "discrepancies must not be empty" in mock_llm.invoke.call_args_list[1].args[0][-1]["content"]

    def test_uses_editor_persona(self):
        from cpg_ingester.prompts.rec_semantic_reviewer import REC_SEMANTIC_REVIEWER_SYSTEM
        assert "editor" in REC_SEMANTIC_REVIEWER_SYSTEM.lower()

    def test_checks_content_faithfulness(self):
        from cpg_ingester.prompts.rec_semantic_reviewer import REC_SEMANTIC_REVIEWER_SYSTEM
        assert "faithful" in REC_SEMANTIC_REVIEWER_SYSTEM.lower()
        assert "critical" in REC_SEMANTIC_REVIEWER_SYSTEM.lower()
        assert "minor" in REC_SEMANTIC_REVIEWER_SYSTEM.lower()
