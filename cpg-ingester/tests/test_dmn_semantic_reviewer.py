"""Tests for the DMN Semantic Reviewer node."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from cpg_ingester.nodes.dmn_semantic_reviewer import dmn_semantic_reviewer


MOCK_PASSED_RESPONSE = json.dumps({
    "claims_checked": [
        {"claim": "Source specifies BP threshold of 140", "verdict": "VERIFIED", "severity": "CRITICAL", "evidence": "SBP >= 140"},
        {"claim": "DMN includes Has Diabetes input", "verdict": "VERIFIED", "severity": "CRITICAL", "evidence": "comorbid diabetes"},
        {"claim": "Output 'Start medication' matches source", "verdict": "VERIFIED", "severity": "CRITICAL", "evidence": "begin pharmacological therapy"},
    ],
    "discrepancies_found": False,
    "summary": "",
    "discrepancies": [],
})

MOCK_FAILED_RESPONSE = json.dumps({
    "claims_checked": [
        {"claim": "Source specifies BP threshold of 140", "verdict": "DISCREPANCY", "severity": "CRITICAL", "feedback": "BP threshold in DMN is 135, source specifies 140", "evidence": "Source says 140 but DMN uses 135"},
        {"claim": "DMN includes eGFR input", "verdict": "DISCREPANCY", "severity": "CRITICAL", "feedback": "Source mentions eGFR-based dosing adjustments but DMN has no eGFR input variable", "evidence": "Source mentions eGFR but DMN omits it"},
        {"claim": "Output values match source", "verdict": "VERIFIED", "severity": "CRITICAL", "evidence": "matches"},
    ],
    "discrepancies_found": True,
    "summary": "Wrong BP threshold and missing eGFR input",
    "discrepancies": [
        "BP threshold in DMN is 135, source specifies 140",
        "Source mentions eGFR-based dosing adjustments but DMN has no eGFR input variable",
    ],
})


class TestDMNSemanticReviewer:

    def test_passes_valid_dmn(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_PASSED_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Treatment Recommendation"},
                "source_pages": "Patients with SBP >= 140...",
                "output_dir": tmpdir,
                "review_count": 0,
            }
            with patch("cpg_ingester.nodes.dmn_semantic_reviewer.get_llm", return_value=mock_llm):
                result = dmn_semantic_reviewer(state)

            assert result["semantic_discrepancies"] == []

    def test_finds_discrepancies(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_FAILED_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Treatment Recommendation"},
                "source_pages": "Patients with SBP >= 140...",
                "output_dir": tmpdir,
                "review_count": 0,
            }
            with patch("cpg_ingester.nodes.dmn_semantic_reviewer.get_llm", return_value=mock_llm):
                result = dmn_semantic_reviewer(state)

            assert len(result["semantic_discrepancies"]) == 2
            assert any("135" in d and "140" in d for d in result["semantic_discrepancies"])
            assert any("eGFR" in d for d in result["semantic_discrepancies"])

    def test_writes_review_report(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=MOCK_PASSED_RESPONSE))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Treatment Recommendation"},
                "source_pages": "source text",
                "output_dir": tmpdir,
                "review_count": 0,
            }
            with patch("cpg_ingester.nodes.dmn_semantic_reviewer.get_llm", return_value=mock_llm):
                dmn_semantic_reviewer(state)

            reports = list(Path(tmpdir).glob("dmn-review-*.json"))
            assert len(reports) == 1
            report = json.loads(reports[0].read_text())
            assert report["claims_checked"] == 3
            assert report["verified"] == 3
            assert report["discrepancies"] == 0

    def test_no_source_pages_escalates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Test"},
                "source_pages": "",
                "output_dir": tmpdir,
                "semantic_retry_count": 0,
            }
            result = dmn_semantic_reviewer(state)
            # No source text can't be verified and can't be fixed by retrying:
            # it is a hard escalation, not a silent pass.
            assert result["force_escalate"] is True
            assert result["escalation_reason"] == "no-source-text"
            assert result["semantic_discrepancies"]

    def test_whitespace_only_source_pages_escalate_without_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Test"},
                "source_pages": " \n\t ",
                "output_dir": tmpdir,
                "semantic_retry_count": 0,
            }
            with patch("cpg_ingester.nodes.dmn_semantic_reviewer.get_llm") as mock_get_llm:
                result = dmn_semantic_reviewer(state)

        assert result["force_escalate"] is True
        assert result["escalation_reason"] == "no-source-text"
        assert result["semantic_discrepancies"]
        mock_get_llm.assert_not_called()

    def test_no_dmn_xml_returns_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "",
                "item": {"name": "Test"},
                "source_pages": "source",
                "output_dir": tmpdir,
            }
            result = dmn_semantic_reviewer(state)
            assert len(result["semantic_discrepancies"]) > 0

    def test_unparseable_reask_then_escalate(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content="not json"))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Test"},
                "source_pages": "source",
                "output_dir": tmpdir,
                "semantic_retry_count": 0,
            }
            with patch("cpg_ingester.nodes.dmn_semantic_reviewer.get_llm", return_value=mock_llm):
                result = dmn_semantic_reviewer(state)

            # Unparseable output triggers exactly one structured re-ask; if that
            # also fails, escalate rather than silently pass.
            assert mock_llm.invoke.call_count == 2
            assert result["force_escalate"] is True
            assert result["escalation_reason"] == "reviewer-unparseable"

    def test_unparseable_then_valid_on_reask(self):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content="not json"),
            MagicMock(content=MOCK_PASSED_RESPONSE),
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Test"},
                "source_pages": "source",
                "output_dir": tmpdir,
                "semantic_retry_count": 0,
            }
            with patch("cpg_ingester.nodes.dmn_semantic_reviewer.get_llm", return_value=mock_llm):
                result = dmn_semantic_reviewer(state)

            assert mock_llm.invoke.call_count == 2
            reask = mock_llm.invoke.call_args_list[1].args[0][-1]["content"]
            assert "not valid JSON" in reask
            assert "schema" not in reask
            assert result.get("force_escalate") is None
            assert result["semantic_discrepancies"] == []

    def test_missing_severity_reasks_then_accepts(self):
        missing_severity = json.dumps({
            "claims_checked": [{
                "claim": "threshold is grounded",
                "verdict": "VERIFIED",
                "evidence": "source",
            }],
            "discrepancies_found": False,
            "summary": "",
            "discrepancies": [],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content=missing_severity),
            MagicMock(content=MOCK_PASSED_RESPONSE),
        ])

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Test"},
                "source_pages": "source",
                "output_dir": tmpdir,
            }
            with patch("cpg_ingester.nodes.dmn_semantic_reviewer.get_llm", return_value=mock_llm):
                result = dmn_semantic_reviewer(state)

            assert mock_llm.invoke.call_count == 2
            reask = mock_llm.invoke.call_args_list[1].args[0][-1]["content"]
            assert "schema" in reask
            assert "severity" in reask
            assert result["semantic_discrepancies"] == []

    def test_minor_discrepancy_does_not_trigger_repair(self):
        minor = json.dumps({
            "claims_checked": [{
                "claim": "column naming differs",
                "verdict": "DISCREPANCY",
                "severity": "MINOR",
                "feedback": "Naming differs from source wording",
                "evidence": "same logic",
            }],
            "discrepancies_found": True,
            "summary": "Minor naming issue",
            "discrepancies": ["Minor naming issue"],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=minor))

        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "dmn_xml": "<definitions/>",
                "item": {"name": "Test"},
                "source_pages": "source",
                "output_dir": tmpdir,
            }
            with patch("cpg_ingester.nodes.dmn_semantic_reviewer.get_llm", return_value=mock_llm):
                result = dmn_semantic_reviewer(state)

            assert result["semantic_discrepancies"] == []

    def test_uses_clinical_pharmacist_persona(self):
        from cpg_ingester.prompts.dmn_semantic_reviewer import DMN_SEMANTIC_REVIEWER_SYSTEM
        assert "pharmacist" in DMN_SEMANTIC_REVIEWER_SYSTEM.lower()

    def test_uses_claim_level_decomposition(self):
        from cpg_ingester.prompts.dmn_semantic_reviewer import DMN_SEMANTIC_REVIEWER_SYSTEM
        assert "claim" in DMN_SEMANTIC_REVIEWER_SYSTEM.lower()
        assert "atomic" in DMN_SEMANTIC_REVIEWER_SYSTEM.lower()
        assert "need first" not in DMN_SEMANTIC_REVIEWER_SYSTEM.lower()
        assert "first or priority" in DMN_SEMANTIC_REVIEWER_SYSTEM.lower()
