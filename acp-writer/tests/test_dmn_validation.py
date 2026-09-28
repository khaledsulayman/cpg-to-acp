"""Tests for engine-backed DMN validation during model deployment."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from acp_writer.api import _dynamic_models, app
from acp_writer.api import _validate_dmn_with_engine
from acp_writer.tools.dmn_evaluation import DmnEngineError
from acp_writer.services.decision_engine import app as decision_engine_app


client = TestClient(app)
decision_engine_client = TestClient(decision_engine_app)

VALIDATION_OK = {"valid": True, "messages": []}
VALIDATION_FAILED = {
    "valid": False,
    "messages": [{"severity": "ERROR", "text": "FEEL compilation failed"}],
}
DMN = """<?xml version="1.0"?><definitions xmlns="https://www.omg.org/spec/DMN/20211108/MODEL/" name="Test Model" id="test" namespace="https://example.test"><decision id="d" name="Decision"><variable name="Decision" typeRef="string"/><literalExpression><text>\"ok\"</text></literalExpression></decision></definitions>"""


@pytest.fixture(autouse=True)
def clear_models():
    _dynamic_models.clear()
    yield
    _dynamic_models.clear()


class TestApiDeploymentValidation:
    @patch("acp_writer.api._validate_dmn_with_engine", return_value=VALIDATION_FAILED)
    def test_invalid_model_returns_422_with_engine_messages(self, _validate):
        response = client.post("/api/v1/decisions/models", content=DMN)

        assert response.status_code == 422
        assert response.json() == {
            "error": "DMN engine validation failed",
            "messages": VALIDATION_FAILED["messages"],
        }
        assert _dynamic_models == {}

    @patch("acp_writer.api._validate_dmn_with_engine", return_value=VALIDATION_OK)
    def test_valid_model_is_deployed(self, _validate):
        response = client.post("/api/v1/decisions/models", content=DMN)

        assert response.status_code == 201
        assert response.json()["id"] == "test-model"
        _validate.assert_called_once_with(DMN)

    @patch("acp_writer.api._validate_dmn_with_engine", return_value=VALIDATION_OK)
    def test_validate_only_does_not_store_model(self, _validate):
        response = client.post("/api/v1/decisions/models?validate_only=true", content=DMN)

        assert response.status_code == 200
        assert response.json() == VALIDATION_OK
        assert _dynamic_models == {}

    @patch("acp_writer.api._validate_dmn_with_engine", return_value=None)
    def test_engine_unavailable_fails_open_for_deployment(self, _validate):
        response = client.post("/api/v1/decisions/models", content=DMN)

        assert response.status_code == 201
        assert response.json()["id"] == "test-model"

    @patch("acp_writer.api._validate_dmn_with_engine", return_value=None)
    def test_validate_only_reports_engine_unavailable(self, _validate):
        response = client.post("/api/v1/decisions/models?validate_only=true", content=DMN)

        assert response.status_code == 503
        assert _dynamic_models == {}


class TestDecisionEngineDeploymentValidation:
    @patch("acp_writer.services.decision_engine._validate_dmn_with_engine", return_value=VALIDATION_FAILED)
    def test_pods_mode_returns_engine_messages(self, _validate):
        response = decision_engine_client.post("/api/v1/decisions/models", content=DMN)

        assert response.status_code == 422
        assert response.json()["messages"] == VALIDATION_FAILED["messages"]
        assert _dynamic_models == {}

    @patch("acp_writer.services.decision_engine._validate_dmn_with_engine", return_value=VALIDATION_OK)
    def test_pods_mode_validate_only_does_not_store(self, _validate):
        response = decision_engine_client.post(
            "/api/v1/decisions/models?validate_only=true", content=DMN)

        assert response.status_code == 200
        assert response.json() == VALIDATION_OK
        assert _dynamic_models == {}

    @patch("acp_writer.services.decision_engine._validate_dmn_with_engine", return_value=VALIDATION_OK)
    def test_pods_mode_source_cpg_collision_requires_replace(self, _validate):
        first = decision_engine_client.post(
            "/api/v1/decisions/models?source_cpg=CPG-X", content=DMN)
        assert first.status_code == 201
        assert first.json()["source_cpg"] == "CPG-X"

        collision = decision_engine_client.post(
            "/api/v1/decisions/models?source_cpg=CPG-Y", content=DMN)
        assert collision.status_code == 409
        assert collision.json()["existing_source_cpg"] == "CPG-X"
        assert collision.json()["source_cpg"] == "CPG-Y"

        replaced = decision_engine_client.post(
            "/api/v1/decisions/models?source_cpg=CPG-Y&replace=true", content=DMN)
        assert replaced.status_code == 201
        assert replaced.json()["source_cpg"] == "CPG-Y"
        stored_models = decision_engine_client.get("/api/v1/decisions/models").json()
        assert stored_models[0]["source_cpg"] == "CPG-Y"


class TestEvaluationErrors:
    def setup_method(self):
        _dynamic_models["test-model"] = {"dmn_xml": DMN}

    def test_monolith_preserves_engine_messages(self):
        error = DmnEngineError(422, [{"severity": "ERROR", "text": "unknown variable"}],
                               "DMN evaluation errors")
        with patch("acp_writer.api._evaluate_jit", side_effect=error):
            response = client.post("/api/v1/decisions/evaluate/test-model", json={})
        assert response.status_code == 422
        assert response.json()["messages"] == error.messages

    def test_decision_engine_preserves_engine_messages(self):
        error = DmnEngineError(422, [{"severity": "ERROR", "text": "unknown variable"}],
                               "DMN evaluation errors")
        with patch("acp_writer.services.decision_engine._evaluate_jit", side_effect=error):
            response = decision_engine_client.post("/api/v1/evaluate", json={
                "model_id": "test-model", "inputs": {},
            })
        assert response.status_code == 422
        assert response.json()["messages"] == error.messages


class TestEngineValidationTransport:
    @staticmethod
    def response(status_code, body=None, text=""):
        response = MagicMock()
        response.status_code = status_code
        response.text = text
        response.json.return_value = body
        return response

    def test_http_failures_are_rejections(self):
        for status in (404, 500):
            with patch("acp_writer.api.requests.post", return_value=self.response(status, text="old service")):
                result = _validate_dmn_with_engine(DMN)
            assert result["valid"] is False
            assert str(status) in result["messages"][0]["text"]

    def test_invalid_json_is_rejection(self):
        with patch("acp_writer.api.requests.post", return_value=self.response(200, ValueError(), "plain text")):
            result = _validate_dmn_with_engine(DMN)
        assert result["valid"] is False

    def test_valid_response_is_returned_unchanged(self):
        body = {"valid": True, "messages": [{"severity": "WARN", "text": "gap"}]}
        with patch("acp_writer.api.requests.post", return_value=self.response(200, body)):
            assert _validate_dmn_with_engine(DMN) == body

    def test_invalid_response_is_returned_unchanged(self):
        body = {"valid": False, "messages": [{"severity": "ERROR", "text": "bad"}]}
        with patch("acp_writer.api.requests.post", return_value=self.response(200, body)):
            assert _validate_dmn_with_engine(DMN) == body

    def test_connection_and_timeout_fail_open(self):
        import requests
        for exc in (requests.ConnectionError(), requests.Timeout()):
            with patch("acp_writer.api.requests.post", side_effect=exc):
                assert _validate_dmn_with_engine(DMN) is None

    def test_other_request_errors_are_rejections(self):
        from requests.exceptions import ChunkedEncodingError
        with patch("acp_writer.api.requests.post", side_effect=ChunkedEncodingError("truncated")):
            result = _validate_dmn_with_engine(DMN)
        assert result["valid"] is False
        assert "truncated" in result["messages"][0]["text"]
