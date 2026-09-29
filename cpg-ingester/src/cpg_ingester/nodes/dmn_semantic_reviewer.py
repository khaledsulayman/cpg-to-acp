"""DMN Semantic Reviewer — adversarial LLM review using claim-level decomposition."""

import json
import logging
import time

import mlflow
from cpg_contracts import content_to_text, get_llm
from cpg_ingester.nodes.structure_analyzer import _parse_llm_json
from cpg_ingester.output import write_artifact
from cpg_ingester.prompts.dmn_semantic_reviewer import (
    DMN_SEMANTIC_REVIEWER_SYSTEM,
    DMN_SEMANTIC_REVIEWER_USER,
)

logger = logging.getLogger(__name__)


def _validate_review_result(result: dict) -> dict:
    """Validate the reviewer contract before using its claims for routing."""
    if not isinstance(result, dict):
        raise ValueError("reviewer response must be a JSON object")
    claims = result.get("claims_checked")
    if not isinstance(claims, list):
        raise ValueError("reviewer response must include a claims_checked list")
    for index, claim in enumerate(claims):
        if not isinstance(claim, dict):
            raise ValueError(f"reviewer claim {index} is not an object")
        if claim.get("severity") not in {"CRITICAL", "MINOR"}:
            raise ValueError(
                f"reviewer claim {index} must include severity CRITICAL or MINOR"
            )
        if claim.get("verdict") not in {"VERIFIED", "DISCREPANCY"}:
            raise ValueError(
                f"reviewer claim {index} must include verdict VERIFIED or DISCREPANCY"
            )
    return result


@mlflow.trace(name="dmn_semantic_reviewer")
def dmn_semantic_reviewer(state: dict) -> dict:
    """Adversarial review of generated DMN against source material."""
    logger.info("── DMN Semantic Reviewer ──")
    dmn_xml = state.get("dmn_xml", "")
    item = state.get("item", {})
    source_pages = state.get("source_pages", "")
    output_dir = state.get("output_dir", "output")
    review_count = state.get("semantic_retry_count", 0)

    name = item.get("name", "unknown")

    if not dmn_xml:
        return {"semantic_discrepancies": ["No DMN XML to review"]}

    # No source text means there is nothing to verify the model against. Retrying
    # the creator will not conjure source text, so this is a hard escalation, not
    # a silent pass and not a retry.
    if not source_pages.strip():
        logger.warning("No source text for semantic review of '%s' — escalating", name)
        return {
            "semantic_discrepancies": [
                "No source text available to verify this decision against the CPG"
            ],
            "force_escalate": True,
            "escalation_reason": "no-source-text",
        }

    llm = get_llm(state)

    messages = [
        {"role": "system", "content": DMN_SEMANTIC_REVIEWER_SYSTEM},
        {"role": "user", "content": DMN_SEMANTIC_REVIEWER_USER.format(
            name=name,
            dmn_xml=dmn_xml,
            source_pages=source_pages,
        )},
    ]

    logger.info("Calling LLM...")
    t0 = time.time()
    response = llm.invoke(messages)
    logger.info("LLM responded in %.1fs", time.time() - t0)

    try:
        result = _validate_review_result(_parse_llm_json(content_to_text(response.content)))
    except json.JSONDecodeError:
        # One structured re-ask before giving up — the model often recovers when
        # explicitly told its previous reply was not valid JSON.
        logger.warning("Semantic review for '%s' was not valid JSON — re-asking", name)
        messages.append({"role": "assistant", "content": content_to_text(response.content)})
        messages.append({"role": "user", "content":
                         "Your previous reply was not valid JSON. Reply with only "
                         "the JSON object, no prose and no code fences."})
        response = llm.invoke(messages)
    except ValueError as exc:
        logger.warning("Semantic review for '%s' failed schema validation — re-asking", name)
        messages.append({"role": "assistant", "content": content_to_text(response.content)})
        messages.append({"role": "user", "content":
                         f"Your previous reply was valid JSON but did not match the required "
                         f"schema: {exc}. Reply with only the corrected JSON object, no prose "
                         "and no code fences."})
        response = llm.invoke(messages)

    try:
        result = _validate_review_result(_parse_llm_json(content_to_text(response.content)))
    except (json.JSONDecodeError, ValueError):
        logger.warning("Semantic review for '%s' still unparseable — escalating", name)
        return {
            "semantic_discrepancies": [
                "Semantic reviewer did not return valid JSON after a re-ask"
            ],
            "force_escalate": True,
            "escalation_reason": "reviewer-unparseable",
        }

    claims = result.get("claims_checked", [])

    critical_discrepancies = [
        claim for claim in claims
        if claim.get("verdict") == "DISCREPANCY" and claim.get("severity") == "CRITICAL"
    ]
    discrepancies = [
        claim.get("feedback") or claim.get("evidence") or claim.get("claim", "")
        for claim in critical_discrepancies
    ]
    discrepancies_found = bool(discrepancies)

    verified = sum(1 for c in claims if c.get("verdict") == "VERIFIED")
    failed = sum(1 for c in claims if c.get("verdict") == "DISCREPANCY")

    safe_name = name.lower().replace(" ", "-").replace("/", "-")[:50]
    review_report = {
        "decision": name,
        "review_iteration": review_count + 1,
        "claims_checked": len(claims),
        "verified": verified,
        "discrepancies": failed,
        "claims": claims,
        "summary": result.get("summary", ""),
    }
    write_artifact(output_dir, f"dmn-review-{safe_name}-{review_count + 1}.json", review_report)

    if discrepancies_found:
        logger.warning(
            "DMN semantic review for '%s': %d/%d claims have discrepancies",
            name, failed, len(claims),
        )
        for d in discrepancies:
            logger.warning("  %s", d)
    else:
        logger.info(
            "DMN semantic review passed for '%s': %d/%d claims verified",
            name, verified, len(claims),
        )

    return {"semantic_discrepancies": discrepancies if discrepancies_found else []}
