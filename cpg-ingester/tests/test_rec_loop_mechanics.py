"""Tests for recommendation review routing and hard escalation behavior."""

from unittest.mock import MagicMock, patch

from cpg_ingester import generation
from cpg_ingester.generation import (
    MAX_REC_REVIEWS,
    _rec_escalate,
    _route_after_rec_semantic,
)


class TestRecommendationSemanticRouting:
    def test_force_escalate_bypasses_retry_budget(self):
        assert _route_after_rec_semantic({
            "force_escalate": True,
            "semantic_discrepancies": [],
            "review_count": 0,
        }) == "rec_escalate"

    def test_semantic_discrepancies_retry_until_budget_then_escalate(self):
        assert _route_after_rec_semantic({
            "semantic_discrepancies": ["wrong recommendation"],
            "review_count": MAX_REC_REVIEWS - 1,
        }) == "rec_extractor"
        assert _route_after_rec_semantic({
            "semantic_discrepancies": ["wrong recommendation"],
            "review_count": MAX_REC_REVIEWS,
        }) == "rec_escalate"

    def test_clean_review_accepts(self):
        assert _route_after_rec_semantic({"semantic_discrepancies": []}) == "rec_accept"

    def test_explicit_reviewer_reason_and_errors_take_precedence(self):
        result = _rec_escalate({
            "schema_errors": ["schema budget exhausted"],
            "semantic_discrepancies": ["No source text available"],
            "escalation_reason": "no-source-text",
        })
        assert result == {
            "escalated": True,
            "escalation_reason": "no-source-text",
            "escalation_errors": ["No source text available"],
        }

    def test_empty_source_hard_escalation_does_not_rerun_extractor(self):
        recommendations = [{"id": "r1", "title": "Exercise", "content": "Exercise regularly"}]
        extractor = MagicMock(return_value={
            "recommendations": recommendations,
            "schema_errors": [],
            "semantic_discrepancies": [],
            "review_count": 0,
        })
        schema_validator = MagicMock(return_value={"schema_errors": []})

        with patch.object(generation, "rec_extractor", extractor), \
                patch.object(generation, "rec_schema_validator", schema_validator):
            graph = generation._build_rec_subgraph().compile()
            result = graph.invoke({
                "items": [{"id": "r1", "section": "3.4"}],
                "source_pages": "",
                "output_dir": "/tmp",
                "review_count": 0,
            })

        assert extractor.call_count == 1
        assert result["escalated"] is True
        assert result["escalation_reason"] == "no-source-text"
        assert result["escalation_errors"]
