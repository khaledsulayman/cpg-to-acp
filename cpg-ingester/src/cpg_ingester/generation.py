"""DMN and Recommendation generation subgraphs with review loops.

Extracted from pipeline.py to avoid importing Docling in the
LLM Analysis pod. This module only imports DMN/Rec nodes —
no Docling, no structure analyzer, no assembly/delivery.
"""

import logging
import os
import re

import mlflow
import requests
from langgraph.graph import END, START, StateGraph
from lxml import etree
from cpg_contracts import DecisionCategory, DecisionModelSummary, DecisionVariable, SourceLocation, decision_model_id

from cpg_ingester.nodes.dmn_creator import dmn_creator
from cpg_ingester.nodes.dmn_semantic_reviewer import dmn_semantic_reviewer
from cpg_ingester.nodes.dmn_syntax_validator import dmn_syntax_validator
from cpg_ingester.nodes.rec_extractor import rec_extractor
from cpg_ingester.nodes.rec_schema_validator import rec_schema_validator
from cpg_ingester.nodes.rec_semantic_reviewer import rec_semantic_reviewer
from cpg_ingester.state import DMNPipelineState, RecPipelineState

logger = logging.getLogger(__name__)

# Separate retry budgets for the two failure modes so a run of syntax retries
# cannot starve the semantic review (and vice versa).
MAX_DMN_SYNTAX_RETRIES = 3
MAX_DMN_SEMANTIC_RETRIES = 2
MAX_REC_REVIEWS = 2


def _extract_section_text(markdown: str, section_map: list, section_id: str) -> str:
    """Extract the markdown text for a section by matching its heading.

    Finds the section heading in the markdown and returns all text from that
    heading to the next heading of equal or higher level.
    """
    if not markdown or not section_id:
        return ""

    matching = [s for s in section_map if section_id in s.get("heading", "")]
    if not matching:
        return ""

    heading = matching[0].get("heading", "")
    lines = markdown.split("\n")

    start_idx = None
    heading_level = 0
    for i, line in enumerate(lines):
        if heading in line and line.strip().startswith("#"):
            start_idx = i
            heading_level = len(line) - len(line.lstrip("#"))
            break

    if start_idx is None:
        return ""

    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        line = lines[i].strip()
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            if level <= heading_level:
                text_after_hash = line.lstrip("#").strip()
                if re.match(r"\d", text_after_hash):
                    end_idx = i
                    break

    return "\n".join(lines[start_idx:end_idx]).strip()


# --- DMN subgraph routing ---

def _route_after_dmn_syntax(state: DMNPipelineState) -> str:
    if state.get("syntax_errors"):
        if state.get("syntax_retry_count", 0) >= MAX_DMN_SYNTAX_RETRIES:
            return "dmn_escalate"
        return "dmn_creator"
    return "dmn_semantic_reviewer"


def _route_after_dmn_semantic(state: DMNPipelineState) -> str:
    # A hard escalation from the reviewer (no source text, unparseable output)
    # bypasses the retry budget — retrying cannot fix either condition.
    if state.get("force_escalate"):
        return "dmn_escalate"
    if state.get("semantic_discrepancies"):
        if state.get("semantic_retry_count", 0) >= MAX_DMN_SEMANTIC_RETRIES:
            return "dmn_escalate"
        return "dmn_creator"
    return "dmn_accept"


def _dmn_accept(state: DMNPipelineState) -> dict:
    logger.info("DMN accepted: %s", state.get("item", {}).get("name", "unknown"))
    return {"escalated": False}


def _dmn_escalate(state: DMNPipelineState) -> dict:
    name = state.get("item", {}).get("name", "unknown")
    reason = state.get("escalation_reason")
    if not reason:
        if state.get("syntax_errors"):
            reason = "syntax-budget-exhausted"
        elif state.get("engine_errors"):
            reason = "engine-validation-budget-exhausted"
        elif state.get("semantic_discrepancies"):
            reason = "semantic-budget-exhausted"
        else:
            reason = "unknown"
    errors = (
        state.get("syntax_errors")
        or state.get("engine_errors")
        or state.get("semantic_discrepancies")
        or []
    )
    logger.warning("DMN escalated for human review: %s (%s)", name, reason)
    return {"escalated": True, "escalation_reason": reason, "escalation_errors": list(errors)}


def _coerce_page_number(value):
    """Return an integer page number, ignoring compound/non-numeric labels."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _decision_summary(item: dict, state: dict, decision_items: list[dict], dmn_xml: str = "") -> dict:
    """Build the stable decision contract from the approved manifest item."""
    decision_by_guid = {entry.get("id"): entry for entry in decision_items}
    modifies: list[str] = []
    explicit = item.get("modifies")
    target = decision_by_guid.get(explicit)
    if target and target.get("model_id"):
        modifies.append(target["model_id"])

    inputs = [DecisionVariable.model_validate({
        "name": value.get("name", ""),
        "type": value.get("type", "string"),
        "description": value.get("description"),
        "codes": value.get("codes"),
        "extraction": value.get("extraction"),
    }) for value in item.get("inputs", [])]
    outputs = []
    if dmn_xml:
        root = etree.fromstring(dmn_xml.encode("utf-8"))
        output_columns = root.xpath(
            './/*[local-name()="decisionTable"]/*[local-name()="output"]'
        )
        outputs = [
            DecisionVariable(
                name=output.get("name", ""),
                type=output.get("typeRef", "string"),
            )
            for output in output_columns
        ]
    location = None
    page_start = _coerce_page_number(item.get("page_start"))
    page_end = _coerce_page_number(item.get("page_end"))
    if page_start is not None:
        location = SourceLocation(page_start=page_start, page_end=page_end)
    category = None
    try:
        category = DecisionCategory(item.get("category")) if item.get("category") else None
    except ValueError:
        logger.warning("Unknown decision category for %s: %s", item.get("name"), item.get("category"))

    summary = DecisionModelSummary(
        id=item.get("model_id") or decision_model_id(item.get("name", "")),
        name=item.get("name", ""),
        inputs=inputs,
        outputs=outputs,
        source_cpg=(state.get("cpg_metadata") or {}).get("cpg_id"),
        category=category,
        modifies=modifies or None,
        source_location=location,
    )
    return summary.model_dump(mode="json", exclude_none=True)


@mlflow.trace(name="dmn_engine_preflight")
def dmn_engine_preflight(state: DMNPipelineState) -> dict:
    """Optionally validate an accepted DMN against the KIE engine."""
    preflight_url = os.environ.get("DMN_PREFLIGHT_URL", "").strip()
    if not preflight_url:
        return {"engine_errors": [], "engine_validation_warnings": []}

    separator = "&" if "?" in preflight_url else "?"
    url = f"{preflight_url}{separator}validate_only=true"
    try:
        response = requests.post(
            url,
            data=state.get("dmn_xml", ""),
            headers={"Content-Type": "application/xml"},
            timeout=15,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        errors = []
        warnings = []
        for message in messages:
            if isinstance(message, dict):
                text = message.get("text", str(message))
                (errors if message.get("severity", "ERROR") == "ERROR" else warnings).append(text)
            else:
                errors.append(str(message))

        if response.status_code == 422 or (
            response.ok and isinstance(payload, dict) and not payload.get("valid", True)
        ):
            return {
                "engine_errors": errors,
                "engine_validation_warnings": warnings,
            }

        if response.ok and messages:
            return {
                "engine_errors": errors,
                "engine_validation_warnings": warnings,
            }

        if not response.ok:
            logger.warning(
                "DMN preflight unavailable (%s); continuing without engine validation",
                response.status_code,
            )
            return {
                "engine_errors": [],
                "engine_validation_warnings": [
                    f"DMN preflight unavailable (HTTP {response.status_code})"
                ],
            }
    except requests.RequestException as exc:
        logger.warning("DMN preflight unavailable; continuing without engine validation: %s", exc)
        return {
            "engine_errors": [],
            "engine_validation_warnings": [f"DMN preflight unavailable: {exc}"],
        }

    return {"engine_errors": [], "engine_validation_warnings": []}


def _route_after_dmn_engine(state: DMNPipelineState) -> str:
    if not state.get("engine_errors"):
        return "dmn_complete"
    if state.get("syntax_retry_count", 0) >= MAX_DMN_SYNTAX_RETRIES:
        return "dmn_escalate"
    return "dmn_creator"


# --- Rec subgraph routing ---

def _route_after_rec_schema(state: RecPipelineState) -> str:
    if state.get("schema_errors"):
        if state.get("review_count", 0) >= MAX_REC_REVIEWS:
            return "rec_escalate"
        return "rec_extractor"
    return "rec_semantic_reviewer"


def _route_after_rec_semantic(state: RecPipelineState) -> str:
    if state.get("force_escalate"):
        return "rec_escalate"
    if state.get("semantic_discrepancies"):
        if state.get("review_count", 0) >= MAX_REC_REVIEWS:
            return "rec_escalate"
        return "rec_extractor"
    return "rec_accept"


def _rec_accept(state: RecPipelineState) -> dict:
    logger.info("Recommendations accepted for section")
    return {"escalated": False}


def _rec_escalate(state: RecPipelineState) -> dict:
    logger.warning("Recommendations escalated for human review")
    reason = state.get("escalation_reason")
    if reason:
        errors = (
            state.get("escalation_errors")
            or state.get("semantic_discrepancies")
            or state.get("schema_errors")
            or []
        )
    elif state.get("schema_errors"):
        reason = "schema-budget-exhausted"
        errors = state["schema_errors"]
    elif state.get("semantic_discrepancies"):
        reason = "semantic-budget-exhausted"
        errors = state["semantic_discrepancies"]
    else:
        reason = "unknown"
        errors = []
    return {
        "escalated": True,
        "escalation_reason": reason,
        "escalation_errors": list(errors),
    }


# --- Subgraph builders ---

def _build_dmn_subgraph() -> StateGraph:
    graph = StateGraph(DMNPipelineState)

    graph.add_node("dmn_creator", dmn_creator)
    graph.add_node("dmn_syntax_validator", dmn_syntax_validator)
    graph.add_node("dmn_semantic_reviewer", dmn_semantic_reviewer)
    graph.add_node("dmn_engine_preflight", dmn_engine_preflight)
    graph.add_node("dmn_accept", _dmn_accept)
    graph.add_node("dmn_escalate", _dmn_escalate)

    graph.add_edge(START, "dmn_creator")
    graph.add_edge("dmn_creator", "dmn_syntax_validator")
    graph.add_conditional_edges("dmn_syntax_validator", _route_after_dmn_syntax, {
        "dmn_semantic_reviewer": "dmn_semantic_reviewer",
        "dmn_creator": "dmn_creator",
        "dmn_escalate": "dmn_escalate",
    })
    graph.add_conditional_edges("dmn_semantic_reviewer", _route_after_dmn_semantic, {
        "dmn_accept": "dmn_accept",
        "dmn_creator": "dmn_creator",
        "dmn_escalate": "dmn_escalate",
    })
    graph.add_edge("dmn_accept", "dmn_engine_preflight")
    graph.add_conditional_edges("dmn_engine_preflight", _route_after_dmn_engine, {
        "dmn_complete": END,
        "dmn_creator": "dmn_creator",
        "dmn_escalate": "dmn_escalate",
    })
    graph.add_edge("dmn_escalate", END)

    return graph


def _build_rec_subgraph() -> StateGraph:
    graph = StateGraph(RecPipelineState)

    graph.add_node("rec_extractor", rec_extractor)
    graph.add_node("rec_schema_validator", rec_schema_validator)
    graph.add_node("rec_semantic_reviewer", rec_semantic_reviewer)
    graph.add_node("rec_accept", _rec_accept)
    graph.add_node("rec_escalate", _rec_escalate)

    graph.add_edge(START, "rec_extractor")
    graph.add_edge("rec_extractor", "rec_schema_validator")
    graph.add_conditional_edges("rec_schema_validator", _route_after_rec_schema, {
        "rec_semantic_reviewer": "rec_semantic_reviewer",
        "rec_extractor": "rec_extractor",
        "rec_escalate": "rec_escalate",
    })
    graph.add_conditional_edges("rec_semantic_reviewer", _route_after_rec_semantic, {
        "rec_accept": "rec_accept",
        "rec_extractor": "rec_extractor",
        "rec_escalate": "rec_escalate",
    })
    graph.add_edge("rec_accept", END)
    graph.add_edge("rec_escalate", END)

    return graph


# --- Generation orchestrator ---

def generate_all(state: dict) -> dict:
    """Run DMN and Rec subgraphs for each manifest item.

    Results are stored in state for downstream use (assembly, delivery).
    """
    manifest = state.get("item_manifest", [])
    if not manifest:
        logger.warning("Empty manifest — skipping generation")
        return {}

    markdown = state.get("markdown", "")
    section_map = state.get("section_map", [])

    shared = {
        "abbreviations": state.get("abbreviations", {}),
        "litellm_url": state.get("litellm_url", ""),
        "llm_model": state.get("llm_model", ""),
        "llm_api_key": state.get("llm_api_key", ""),
        "output_dir": state.get("output_dir", ""),
    }

    dmn_graph = _build_dmn_subgraph().compile()
    rec_graph = _build_rec_subgraph().compile()

    dmn_results = []
    decisions = [i for i in manifest if i.get("type") == "decision"]
    for item in decisions:
        logger.info("Generating DMN for: %s", item.get("name", "?"))
        source_text = _extract_section_text(markdown, section_map, item.get("section", ""))
        try:
            result = dmn_graph.invoke({
                "item": item,
                "source_pages": source_text,
                "cpg_metadata": state.get("cpg_metadata", {}),
                **shared,
            })
        except Exception as e:
            # A crash must not make the decision silently disappear from the
            # output and the counts — flag it for human review instead.
            logger.error("DMN generation failed for '%s': %s", item.get("name"), e)
            dmn_results.append({
                "dmn_xml": "",
                "item": item,
                "decision_model_summary": {},
                "escalated": True,
                "escalation_reason": "generation-exception",
                "escalation_errors": [str(e)],
            })
            continue

        if not result.get("dmn_xml"):
            # Same rule for a run that finished but produced no XML.
            logger.error("DMN generation produced no XML for '%s'", item.get("name"))
            dmn_results.append({
                "dmn_xml": "",
                "item": item,
                "decision_model_summary": result.get("decision_model_summary", {}),
                "escalated": True,
                "escalation_reason": result.get("escalation_reason") or "empty-result",
                "escalation_errors": result.get("escalation_errors") or [
                    "Subgraph returned no DMN XML"
                ],
            })
            continue

        entry = {
            "dmn_xml": result["dmn_xml"],
            "item": item,
            "decision_model_summary": {},
        }
        try:
            entry["decision_model_summary"] = _decision_summary(
                item, state, decisions, result["dmn_xml"]
            )
            if ((item.get("page_start") is not None or item.get("page_end") is not None)
                    and "source_location" not in entry["decision_model_summary"]):
                entry.setdefault("validation_warnings", []).append(
                    "summary-build-failed: invalid source page range"
                )
        except Exception as exc:
            logger.error("Decision summary failed for '%s': %s", item.get("name"), exc)
            entry.setdefault("validation_warnings", []).append(
                f"summary-build-failed: {exc}"
            )
        if result.get("syntax_warnings"):
            entry.setdefault("validation_warnings", []).extend(result["syntax_warnings"])
        if result.get("engine_validation_warnings"):
            entry.setdefault("validation_warnings", []).extend(
                result["engine_validation_warnings"]
            )
        if result.get("escalated"):
            entry["escalated"] = True
            entry["escalation_reason"] = result.get("escalation_reason", "")
            entry["escalation_errors"] = (
                result.get("escalation_errors")
                or result.get("syntax_errors")
                or result.get("semantic_discrepancies")
                or []
            )
        dmn_results.append(entry)

    all_recs = []
    recommendation_escalations = []
    seen_sections = set()
    recommendations = [i for i in manifest if i.get("type") == "recommendation"]
    for item in recommendations:
        section = item.get("section", "default")
        if section in seen_sections:
            continue
        seen_sections.add(section)
        section_items = [i for i in recommendations if i.get("section") == section]
        logger.info("Extracting recommendations for section: %s (%d items)", section, len(section_items))
        source_text = _extract_section_text(markdown, section_map, section)
        try:
            result = rec_graph.invoke({
                "items": section_items,
                "source_pages": source_text,
                "grading_definitions": state.get("grading_definitions", ""),
                **shared,
            })
            recs = result.get("recommendations", [])
            if recs:
                if result.get("escalated"):
                    for rec in recs:
                        if isinstance(rec, dict):
                            rec["escalated"] = True
                            rec["escalation_reason"] = result.get("escalation_reason", "")
                            rec["escalation_errors"] = result.get("escalation_errors", [])
                all_recs.extend(recs)
            elif result.get("escalated"):
                recommendation_escalations.append({
                    "type": "recommendation",
                    "id": section,
                    "name": f"Section: {section}",
                    "section": section,
                    "escalation_reason": result.get("escalation_reason") or "empty-result",
                    "escalation_errors": result.get("escalation_errors") or [
                        "Recommendation loop returned no recommendations"
                    ],
                })
        except Exception as e:
            logger.error("Rec extraction failed for section '%s': %s", section, e)
            recommendation_escalations.append({
                "type": "recommendation",
                "id": section,
                "name": f"Section: {section}",
                "section": section,
                "escalation_reason": "generation-exception",
                "escalation_errors": [str(e)],
            })

    return {
        "dmn_results": dmn_results,
        "recommendation_results": all_recs,
        "recommendation_escalations": recommendation_escalations,
    }
