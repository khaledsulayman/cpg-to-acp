"""Recommendation Semantic Reviewer — LLM review of content faithfulness and certainty accuracy."""

import json
import logging
import time
from collections import Counter

import mlflow
from cpg_contracts import content_to_text, get_llm
from cpg_ingester.nodes.structure_analyzer import _parse_llm_json
from cpg_ingester.output import write_artifact
from cpg_ingester.prompts.rec_semantic_reviewer import (
    REC_SEMANTIC_REVIEWER_SYSTEM,
    REC_SEMANTIC_REVIEWER_USER,
)

logger = logging.getLogger(__name__)


def _issue_severity(issue: str) -> str | None:
    severity, separator, description = issue.partition(":")
    if not separator or not description.strip():
        return None
    severity = severity.strip().upper()
    return severity if severity in {"CRITICAL", "MINOR"} else None


def _validate_review_result(result: dict, recommendations: list[dict]) -> dict:
    """Validate the reviewer contract before using its result for routing."""
    if not isinstance(result, dict):
        raise ValueError("reviewer response must be a JSON object")

    if not isinstance(recommendations, list):
        raise ValueError("recommendations must be a list")
    recommendation_titles = Counter()
    for index, recommendation in enumerate(recommendations):
        if not isinstance(recommendation, dict):
            raise ValueError(f"recommendation {index} must be an object")
        title = recommendation.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"recommendation {index} must include a non-blank title")
        recommendation_titles[" ".join(title.split()).lower()] += 1

    discrepancies_found = result.get("discrepancies_found")
    if not isinstance(discrepancies_found, bool):
        raise ValueError("reviewer response must include a boolean discrepancies_found")

    discrepancies = result.get("discrepancies")
    if not isinstance(discrepancies, list) or not all(
        isinstance(discrepancy, str) and discrepancy.strip()
        for discrepancy in discrepancies
    ):
        raise ValueError("reviewer response must include a list of non-blank string discrepancies")
    if discrepancies_found and not discrepancies:
        raise ValueError("discrepancies must not be empty when discrepancies_found is true")
    if not discrepancies_found and discrepancies:
        raise ValueError("discrepancies must be empty when discrepancies_found is false")

    checks = result.get("checks")
    if not isinstance(checks, list):
        raise ValueError("reviewer response must include a checks list")
    check_titles = Counter()
    issue_severities = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ValueError(f"reviewer check {index} must be an object")
        title = check.get("recommendation_title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"reviewer check {index} must include a non-blank recommendation_title")
        check_titles[" ".join(title.split()).lower()] += 1

        check_fields = ("content_faithful", "certainty_accurate", "type_correct")
        for field in check_fields:
            if not isinstance(check.get(field), bool):
                raise ValueError(f"reviewer check {index} must include boolean {field}")
        issues = check.get("issues")
        if not isinstance(issues, list) or not all(
            isinstance(issue, str) and issue.strip() for issue in issues
        ):
            raise ValueError(f"reviewer check {index} must include a list of non-blank string issues")
        for issue in issues:
            severity = _issue_severity(issue)
            if severity is None:
                raise ValueError(
                    f"reviewer check {index} issues must have a CRITICAL or MINOR tag "
                    "and a non-blank description"
                )
            issue_severities.append(severity)
        failed_fields = [field for field in check_fields if not check[field]]
        if failed_fields and not issues:
            raise ValueError(
                f"reviewer check {index} reports failed fields {failed_fields} without issue evidence"
            )

    missing_titles = list((recommendation_titles - check_titles).elements())
    extra_titles = list((check_titles - recommendation_titles).elements())
    if missing_titles or extra_titles:
        raise ValueError(
            "reviewer check titles must match recommendation titles; "
            f"missing titles: {missing_titles}; extra titles: {extra_titles}"
        )

    missing = result.get("missing_recommendations")
    if not isinstance(missing, list) or not all(
        isinstance(item, str) and item.strip() for item in missing
    ):
        raise ValueError(
            "reviewer response must include a list of non-blank string missing_recommendations"
        )

    has_critical_evidence = bool(missing) or "CRITICAL" in issue_severities
    if discrepancies_found != has_critical_evidence:
        raise ValueError(
            "discrepancies_found must agree with CRITICAL check issues and "
            "missing_recommendations"
        )

    return result


@mlflow.trace(name="rec_semantic_reviewer")
def rec_semantic_reviewer(state: dict) -> dict:
    """Review extracted recommendations against source material."""
    logger.info("── Rec Semantic Reviewer ──")
    recommendations = state.get("recommendations", [])
    source_pages = state.get("source_pages", "")
    output_dir = state.get("output_dir", "output")
    review_count = state.get("review_count", 0)
    items = state.get("items", [])

    if not recommendations:
        return {"semantic_discrepancies": ["No recommendations to review"]}

    if not source_pages.strip():
        logger.warning("No source pages for semantic review — escalating")
        return {
            "semantic_discrepancies": [
                "No source text available to verify these recommendations against the CPG"
            ],
            "force_escalate": True,
            "escalation_reason": "no-source-text",
        }

    llm = get_llm(state)

    recs_str = json.dumps(recommendations, indent=2, default=str)

    messages = [
        {"role": "system", "content": REC_SEMANTIC_REVIEWER_SYSTEM},
        {"role": "user", "content": REC_SEMANTIC_REVIEWER_USER.format(
            recommendations=recs_str,
            source_pages=source_pages,
        )},
    ]

    logger.info("Calling LLM...")
    t0 = time.time()
    response = llm.invoke(messages)
    logger.info("LLM responded in %.1fs", time.time() - t0)

    try:
        result = _validate_review_result(
            _parse_llm_json(content_to_text(response.content)), recommendations
        )
    except json.JSONDecodeError:
        logger.warning("Semantic review for recommendations was not valid JSON — re-asking")
        messages.append({"role": "assistant", "content": content_to_text(response.content)})
        messages.append({"role": "user", "content":
                         "Your previous reply was not valid JSON. Reply with only "
                         "the JSON object, no prose and no code fences."})
        response = llm.invoke(messages)
    except ValueError as exc:
        logger.warning("Semantic review for recommendations failed schema validation — re-asking")
        messages.append({"role": "assistant", "content": content_to_text(response.content)})
        messages.append({"role": "user", "content":
                         f"Your previous reply was valid JSON but did not match the required "
                         f"schema: {exc}. Reply with only the corrected JSON object, no prose "
                         "and no code fences."})
        response = llm.invoke(messages)

    try:
        result = _validate_review_result(
            _parse_llm_json(content_to_text(response.content)), recommendations
        )
    except (json.JSONDecodeError, ValueError):
        logger.warning("Recommendation semantic review still invalid after a re-ask — escalating")
        return {
            "semantic_discrepancies": [
                "Recommendation reviewer did not return valid JSON after a re-ask"
            ],
            "force_escalate": True,
            "escalation_reason": "reviewer-unparseable",
        }

    discrepancies_found = result.get("discrepancies_found", False)
    discrepancies = result.get("discrepancies", [])
    checks = result.get("checks", [])
    missing = result.get("missing_recommendations", [])

    passed = sum(1 for c in checks if not c.get("issues"))
    failed = sum(1 for c in checks if c.get("issues"))

    section = items[0].get("section", "unknown") if items else "unknown"
    review_report = {
        "section": section,
        "review_iteration": review_count + 1,
        "recommendations_checked": len(checks),
        "passed": passed,
        "with_issues": failed,
        "missing_recommendations": missing,
        "checks": checks,
        "summary": result.get("summary", ""),
    }
    write_artifact(output_dir, f"rec-review-{section}-{review_count + 1}.json", review_report)

    if discrepancies_found:
        logger.warning(
            "Rec semantic review for section %s: %d/%d recs have issues, %d missing",
            section, failed, len(checks), len(missing),
        )
        for d in discrepancies:
            logger.warning("  %s", d)
    else:
        logger.info(
            "Rec semantic review passed for section %s: %d/%d recs OK",
            section, passed, len(checks),
        )

    return {"semantic_discrepancies": discrepancies if discrepancies_found else []}
