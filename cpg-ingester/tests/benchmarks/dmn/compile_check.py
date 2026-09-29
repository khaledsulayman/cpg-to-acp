"""Classify DMN compilation and execution against the decision service.

The validation endpoint is the source of truth for compilation. The execution
endpoint is kept separate because it also exercises a model with representative
inputs and can therefore return evaluation errors after successful compilation.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import mlflow
import requests

@dataclass
class CompileResult:
    status: str          # "COMPILE_OK" | "COMPILE_FAIL" | "INFRA" | "SKIPPED"
    http_status: int | None = None
    messages: list = None
    detail: str = ""
    outputs: dict | None = None

    @property
    def compiled(self) -> bool:
        return self.status == "COMPILE_OK"


def kogito_url() -> str:
    """Read ``KOGITO_URL`` (the acp-writer/compose variable), defaulting to ``http://localhost:8081``."""
    return os.environ.get("KOGITO_URL", "http://localhost:8081").rstrip("/")


def _response_json(response) -> dict | None:
    """Return a JSON object response, or ``None`` when it is not one."""
    try:
        body = response.json()
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


@mlflow.trace(name="dmn_validate_check")
def validate_check(dmn_xml: str, base_url: str | None = None,
                   timeout: float = 30) -> CompileResult:
    """Ask the decision service whether a DMN model compiles."""
    url = f"{base_url or kogito_url()}/jit/dmn/validate"
    payload = {
        "dmn_xml_base64": base64.b64encode(dmn_xml.encode("utf-8")).decode("ascii"),
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException:
        return CompileResult(status="SKIPPED", detail="decision-service unreachable")

    if resp.status_code != 200:
        if resp.status_code == 400:
            return CompileResult(status="INFRA", http_status=400, detail="bad request")
        return CompileResult(status="INFRA", http_status=resp.status_code,
                             detail=f"unexpected {resp.status_code}")

    body = _response_json(resp)
    if body is None or not isinstance(body.get("valid"), bool):
        return CompileResult(status="INFRA", http_status=200,
                             detail="non-JSON validation response")
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        messages = [messages]
    return CompileResult(
        status="COMPILE_OK" if body["valid"] else "COMPILE_FAIL",
        http_status=200,
        messages=messages,
    )


@mlflow.trace(name="dmn_compile_check")
def compile_check(dmn_xml: str, inputs: dict | None = None,
                  base_url: str | None = None, timeout: float = 30) -> CompileResult:
    """Execute a DMN with inputs and classify compilation separately from errors."""
    url = f"{base_url or kogito_url()}/jit/dmn"
    payload = {
        "dmn_xml_base64": base64.b64encode(dmn_xml.encode("utf-8")).decode("ascii"),
        "inputs": inputs or {},
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        return CompileResult(status="SKIPPED", detail=f"decision-service unreachable: {e}")

    body_json = _response_json(resp)
    if resp.status_code == 200:
        if body_json is None:
            return CompileResult(status="INFRA", http_status=200,
                                 detail="non-JSON execution response")
        return CompileResult(status="COMPILE_OK", http_status=200, outputs=body_json)

    if resp.status_code == 422:
        error = body_json.get("error") if body_json else None
        messages = body_json.get("messages", []) if body_json else []
        if error == "DMN compilation errors":
            return CompileResult(status="COMPILE_FAIL", http_status=422,
                                 messages=messages, detail="compile failure")
        if error == "DMN evaluation errors":
            return CompileResult(status="COMPILE_OK", http_status=422,
                                 messages=messages, detail="evaluation failure")
        return CompileResult(status="INFRA", http_status=422,
                             messages=messages, detail="unexpected 422")

    if resp.status_code == 400:
        error = body_json.get("error") if body_json else None
        if error == "No DMN models found in the provided XML":
            return CompileResult(status="COMPILE_FAIL", http_status=400,
                                 messages=[error], detail="bad model")
        return CompileResult(status="INFRA", http_status=400,
                             messages=[error] if error else [], detail="bad request")
    body = resp.text or ""
    return CompileResult(status="INFRA", http_status=resp.status_code,
                         messages=[body], detail=f"unexpected {resp.status_code}")
