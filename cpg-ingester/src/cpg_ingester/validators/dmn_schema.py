"""DMN 1.4 XML Schema validation using vendored OMG schemas."""

from functools import lru_cache
from io import BytesIO
from pathlib import Path

import mlflow
from lxml import etree

_SCHEMA_PATH = Path(__file__).parent.parent / "resources" / "schemas" / "DMN14.xsd"


@lru_cache(maxsize=1)
def _dmn_schema() -> etree.XMLSchema:
    """Load and compile the DMN 1.4 schema once per process."""
    return etree.XMLSchema(etree.parse(str(_SCHEMA_PATH)))


@mlflow.trace(name="validate_dmn_schema")
def validate_dmn_schema(dmn_xml: str) -> list[str]:
    """Return line-numbered DMN 1.4 schema errors for well-formed XML."""
    try:
        document = etree.parse(BytesIO(dmn_xml.encode("utf-8")))
    except etree.XMLSyntaxError as exc:
        return [f"XML parse error: {exc}"]

    schema = _dmn_schema()
    if schema.validate(document):
        return []
    return [f"XSD line {error.line}: {error.message}" for error in schema.error_log]
