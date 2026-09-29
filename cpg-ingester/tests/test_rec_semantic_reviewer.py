"""Tests for the Recommendation Semantic Reviewer node."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
            "issues": ["MINOR: Content says 'must engage' but source says 'engage in at least' — language strengthened"],
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


def _check_result(recommendation_title: str, **overrides) -> dict:
    check = {
        "recommendation_title": recommendation_title,
        "content_faithful": True,
        "certainty_accurate": True,
        "type_correct": True,
        "issues": [],
    }
    check.update(overrides)
    return check


def _review_result(*, checks=None, missing_recommendations=None,
                   discrepancies_found=False, discrepancies=None) -> dict:
    return {
        "checks": checks if checks is not None else [
            _check_result(recommendation["title"]) for recommendation in SAMPLE_RECS
        ],
        "missing_recommendations": missing_recommendations or [],
        "discrepancies_found": discrepancies_found,
        "discrepancies": discrepancies or [],
        "summary": "",
    }


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
            assert mock_llm.invoke.call_count == 1

    @pytest.mark.parametrize(
        ("review", "retry_fragments"),
        [
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet", content_faithful=False,
                                  issues=["CRITICAL : wrong dose"]),
                    _check_result("Physical Activity"),
                ]),
                (),
                id="critical-space-before-colon",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet", content_faithful=False,
                                  issues=["critical:wrong dose"]),
                    _check_result("Physical Activity"),
                ]),
                (),
                id="lowercase-critical-tag",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet", content_faithful=False,
                                  issues=["Critical: wrong dose"]),
                    _check_result("Physical Activity"),
                ]),
                (),
                id="mixed-case-critical-tag",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet", issues=["CRITICAL: wrong dose"]),
                    _check_result("Physical Activity"),
                ]),
                (),
                id="critical-issue-flag-false",
            ),
            pytest.param(
                _review_result(missing_recommendations=["alcohol"]),
                (),
                id="missing-recommendation-flag-false",
            ),
            pytest.param(
                _review_result(discrepancies_found=True, discrepancies=[""]),
                (),
                id="blank-discrepancy",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet", issues=["CRITICAL:"]),
                    _check_result("Physical Activity"),
                ]),
                (),
                id="critical-tag-without-description",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet", issues=["wrong dose"]),
                    _check_result("Physical Activity"),
                ]),
                (),
                id="untagged-issue",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet"),
                    _check_result("DASH Diet"),
                ]),
                ("missing titles: ['physical activity']", "extra titles: ['dash diet']"),
                id="duplicate-check-title",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet"),
                    _check_result("Physical Activity", content_faithful=False),
                ]),
                (),
                id="failed-check-without-issue",
            ),
            pytest.param(
                _review_result(missing_recommendations=["  "]),
                (),
                id="blank-missing-recommendation",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet", issues=["  "]),
                    _check_result("Physical Activity"),
                ]),
                (),
                id="blank-check-issue",
            ),
        ],
    )
    def test_invalid_review_responses_hard_escalate_after_reask(self, review, retry_fragments):
        response_text = json.dumps(review)
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content=response_text),
            MagicMock(content=response_text),
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
        assert result["semantic_discrepancies"] != []
        assert mock_llm.invoke.call_count == 2
        retry_prompt = mock_llm.invoke.call_args_list[1].args[0][-1]["content"]
        for fragment in retry_fragments:
            assert fragment in retry_prompt

    @pytest.mark.parametrize(
        ("review", "expected_discrepancies"),
        [
            pytest.param(_review_result(), [], id="clean"),
            pytest.param(
                _review_result(checks=[
                    _check_result("DASH Diet", content_faithful=False,
                                  issues=["critical: reversed direction"]),
                    _check_result("Physical Activity"),
                ], discrepancies_found=True, discrepancies=["reversed direction"]),
                ["reversed direction"],
                id="lowercase-critical-with-flag-true",
            ),
            pytest.param(
                _review_result(checks=[
                    _check_result("  dash   DIET "),
                    _check_result("physical activity  "),
                ]),
                [],
                id="normalized-recommendation-titles",
            ),
        ],
    )
    def test_valid_review_responses_do_not_reask(self, review, expected_discrepancies):
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=json.dumps(review)))

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

        assert result["semantic_discrepancies"] == expected_discrepancies
        assert mock_llm.invoke.call_count == 1

    def test_whitespace_only_source_pages_escalate_without_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = {
                "recommendations": SAMPLE_RECS,
                "source_pages": " \n\t ",
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

    @pytest.mark.parametrize(
        ("critical_issues", "missing_recommendations"),
        [
            (["CRITICAL: Wrong dose could harm the patient"], []),
            ([], ["An alcohol limitation recommendation is missing"]),
            (["CRITICAL: Wrong dose could harm the patient"],
             ["An alcohol limitation recommendation is missing"]),
        ],
        ids=["critical-check-issue", "missing-recommendation", "both"],
    )
    def test_critical_evidence_with_false_discrepancy_flag_escalates_after_reask(
        self, critical_issues, missing_recommendations,
    ):
        contradictory_response = json.dumps({
            "checks": [{
                "recommendation_title": "DASH Diet",
                "content_faithful": True,
                "certainty_accurate": True,
                "type_correct": True,
                "issues": [],
            }, {
                "recommendation_title": "Physical Activity",
                "content_faithful": not critical_issues,
                "certainty_accurate": True,
                "type_correct": True,
                "issues": critical_issues,
            }],
            "missing_recommendations": missing_recommendations,
            "discrepancies_found": False,
            "summary": "",
            "discrepancies": [],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content=contradictory_response),
            MagicMock(content=contradictory_response),
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
        assert result["semantic_discrepancies"]
        assert mock_llm.invoke.call_count == 2
        retry_prompt = mock_llm.invoke.call_args_list[1].args[0][-1]["content"]
        assert "discrepancies_found must agree" in retry_prompt

    @pytest.mark.parametrize(
        ("issues", "retry_error"),
        [([], "without issue evidence"), (["Wrong dose"], "CRITICAL or MINOR tag")],
        ids=["no-issue", "untagged-issue"],
    )
    def test_failed_check_without_valid_issue_evidence_escalates_after_reask(
        self, issues, retry_error,
    ):
        contradictory_response = json.dumps({
            "checks": [{
                "recommendation_title": "DASH Diet",
                "content_faithful": True,
                "certainty_accurate": True,
                "type_correct": True,
                "issues": [],
            }, {
                "recommendation_title": "Physical Activity",
                "content_faithful": False,
                "certainty_accurate": True,
                "type_correct": True,
                "issues": issues,
            }],
            "missing_recommendations": [],
            "discrepancies_found": False,
            "summary": "",
            "discrepancies": [],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(side_effect=[
            MagicMock(content=contradictory_response),
            MagicMock(content=contradictory_response),
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
        assert result["semantic_discrepancies"]
        assert mock_llm.invoke.call_count == 2
        retry_prompt = mock_llm.invoke.call_args_list[1].args[0][-1]["content"]
        assert retry_error in retry_prompt

    def test_minor_only_failed_check_routes_to_accept_by_design(self):
        minor_response = json.dumps({
            "checks": [{
                "recommendation_title": "DASH Diet",
                "content_faithful": True,
                "certainty_accurate": False,
                "type_correct": True,
                "issues": ["MINOR: Certainty grade is more specific than the source"],
            }, {
                "recommendation_title": "Physical Activity",
                "content_faithful": True,
                "certainty_accurate": True,
                "type_correct": True,
                "issues": [],
            }],
            "missing_recommendations": [],
            "discrepancies_found": False,
            "summary": "",
            "discrepancies": [],
        })
        mock_llm = MagicMock()
        mock_llm.invoke = MagicMock(return_value=MagicMock(content=minor_response))

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
        assert mock_llm.invoke.call_count == 1

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
