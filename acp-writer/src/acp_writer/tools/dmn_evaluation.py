"""DMN evaluation client abstraction.

Separates "resolve inputs" (concept pipeline, LLM) from
"evaluate the DMN model" (Kogito). Two implementations:

- InProcessEvaluationClient: monolith/local-dev, wraps _evaluate_jit
- HttpEvaluationClient: pods mode, calls the decision-engine service

Selection: DECISION_ENGINE_URL in state/env → HTTP; absent → in-process.
"""

import base64
import logging
from typing import Any, Protocol

import mlflow
import requests

logger = logging.getLogger(__name__)


class ModelNotDeployed(Exception):
    """Raised when a DMN model is not deployed on the evaluation service."""


class DmnEngineError(Exception):
    """A structured 4xx response from the DMN engine."""

    def __init__(self, status_code: int, messages: list[dict], error: str = "DMN engine error"):
        self.status_code = status_code
        self.messages = messages
        self.error = error
        super().__init__(f"{error} (HTTP {status_code})")

    @classmethod
    def from_response(cls, response: requests.Response) -> "DmnEngineError":
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        if not isinstance(messages, list):
            messages = [{"severity": "ERROR", "text": str(messages)}]
        if not messages and isinstance(payload, dict) and payload.get("error"):
            messages = [{"severity": "ERROR", "text": str(payload["error"])}]
        return cls(
            status_code=response.status_code,
            messages=messages,
            error=str(payload.get("error", "DMN engine error"))
            if isinstance(payload, dict) else "DMN engine error",
        )


class DmnEvaluationClient(Protocol):
    def evaluate(self, model_id: str, inputs: dict) -> dict:
        """Evaluate a DMN model with pre-resolved inputs.

        Returns the DMN outputs dict.
        Raises ModelNotDeployed if the model is unknown.
        """
        ...


class InProcessEvaluationClient:
    """Monolith mode — wraps _dynamic_models lookup + _evaluate_jit."""

    @mlflow.trace(name="dmn_evaluate_inprocess")
    def evaluate(self, model_id: str, inputs: dict) -> dict:
        from acp_writer.api import _dynamic_models, _evaluate_jit

        deployed = _dynamic_models.get(model_id)
        if not deployed:
            raise ModelNotDeployed(f"Model {model_id} not deployed")

        return _evaluate_jit(deployed["dmn_xml"], inputs)


class HttpEvaluationClient:
    """Pods mode — POST to the decision-engine service."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    @mlflow.trace(name="dmn_evaluate_http")
    def evaluate(self, model_id: str, inputs: dict) -> dict:
        url = f"{self.base_url}/api/v1/evaluate"
        try:
            r = requests.post(
                url,
                json={"model_id": model_id, "inputs": inputs},
                timeout=30,
            )
        except requests.RequestException as exc:
            logger.error("Decision service call failed: %s", exc)
            raise

        if r.status_code == 404:
            raise ModelNotDeployed(f"Model {model_id} not deployed on {self.base_url}")

        if 400 <= r.status_code < 500:
            raise DmnEngineError.from_response(r)

        r.raise_for_status()
        data = r.json()
        return data.get("outputs", data)


def get_evaluation_client(state: dict | None = None) -> DmnEvaluationClient:
    """Build the right evaluation client from state/env config.

    DECISION_ENGINE_URL set → HttpEvaluationClient
    Absent → InProcessEvaluationClient (monolith default)
    """
    import os

    url = None
    if state:
        url = state.get("decision_engine_url")
    if not url:
        url = os.environ.get("DECISION_ENGINE_URL")

    if url:
        logger.info("Using HTTP DMN evaluation client: %s", url)
        return HttpEvaluationClient(url)

    return InProcessEvaluationClient()
