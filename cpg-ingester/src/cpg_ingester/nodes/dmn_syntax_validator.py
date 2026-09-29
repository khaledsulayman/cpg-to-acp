"""DMN Syntax Validator — deterministic XML/namespace/structure/FEEL validation."""

import logging

import mlflow

from cpg_ingester.validators.dmn_syntax import validate_dmn, validate_dmn_preflight
from cpg_ingester.validators.dmn_schema import validate_dmn_schema

logger = logging.getLogger(__name__)


@mlflow.trace(name="dmn_syntax_validator")
def dmn_syntax_validator(state: dict) -> dict:
    """Validate DMN XML syntax. Returns errors in state for retry routing."""
    logger.info("── DMN Syntax Validator ──")
    dmn_xml = state.get("dmn_xml", "")
    item = state.get("item", {})
    name = item.get("name", "unknown")

    if not dmn_xml:
        return {"syntax_errors": ["No DMN XML produced"], "syntax_warnings": []}

    preflight_errors = validate_dmn_preflight(dmn_xml)
    if preflight_errors:
        return {"syntax_errors": preflight_errors, "syntax_warnings": []}

    # XSD validation follows cheap XML well-formedness and namespace checks.
    # It yields precise element-order and structural feedback for the repair loop.
    schema_errors = validate_dmn_schema(dmn_xml)
    if schema_errors:
        return {"syntax_errors": schema_errors, "syntax_warnings": []}

    errors, warnings = validate_dmn(dmn_xml)

    if errors:
        logger.warning("DMN syntax validation failed for '%s': %d errors", name, len(errors))
        for err in errors:
            logger.warning("  %s", err)
    else:
        logger.info("DMN syntax validation passed for '%s'", name)

    for warning in warnings:
        logger.warning("DMN syntax warning for '%s': %s", name, warning)

    return {"syntax_errors": errors, "syntax_warnings": warnings}
