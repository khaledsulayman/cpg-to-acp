"""Tests for the DMN evaluation client abstraction."""

from unittest.mock import MagicMock, patch
import pytest

from acp_writer.tools.dmn_evaluation import (
    DmnEngineError,
    InProcessEvaluationClient,
    HttpEvaluationClient,
    ModelNotDeployed,
    get_evaluation_client,
)


class TestInProcessClient:
    def test_evaluate_deployed_model(self):
        client = InProcessEvaluationClient()
        with patch("acp_writer.api._dynamic_models", {"test-model": {
            "dmn_xml": "<dmn/>",
        }}), patch("acp_writer.api._evaluate_jit", return_value={"Action": "Test"}):
            result = client.evaluate("test-model", {"input": 1})
            assert result == {"Action": "Test"}

    def test_evaluate_unknown_model_raises(self):
        client = InProcessEvaluationClient()
        with patch("acp_writer.api._dynamic_models", {}):
            with pytest.raises(ModelNotDeployed):
                client.evaluate("nonexistent", {})

    def test_engine_422_from_jit_preserves_messages(self):
        from acp_writer.api import _evaluate_jit

        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.json.return_value = {
            "error": "DMN evaluation errors",
            "messages": [{"severity": "ERROR", "text": "missing input"}],
        }
        with patch("acp_writer.api.requests.post", return_value=mock_response):
            with pytest.raises(DmnEngineError) as exc_info:
                _evaluate_jit("<definitions/>", {})

        assert exc_info.value.status_code == 422
        assert exc_info.value.messages[0]["text"] == "missing input"


class TestHttpClient:
    def test_evaluate_success(self):
        client = HttpEvaluationClient("http://decision-engine:8080")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"outputs": {"Action": "Start medication"}}
        mock_response.raise_for_status = MagicMock()

        with patch("acp_writer.tools.dmn_evaluation.requests.post", return_value=mock_response):
            result = client.evaluate("treatment-recommendation", {"Systolic BP": 142})
            assert result == {"Action": "Start medication"}

    def test_evaluate_404_raises_model_not_deployed(self):
        client = HttpEvaluationClient("http://decision-engine:8080")
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("acp_writer.tools.dmn_evaluation.requests.post", return_value=mock_response):
            with pytest.raises(ModelNotDeployed):
                client.evaluate("unknown-model", {})

    def test_evaluate_422_preserves_engine_messages(self):
        client = HttpEvaluationClient("http://decision-engine:8080")
        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.json.return_value = {
            "error": "DMN compilation errors",
            "messages": [{"severity": "ERROR", "text": "unknown variable"}],
        }

        with patch("acp_writer.tools.dmn_evaluation.requests.post", return_value=mock_response):
            with pytest.raises(DmnEngineError) as exc_info:
                client.evaluate("bad-model", {})

        assert exc_info.value.status_code == 422
        assert exc_info.value.messages == [
            {"severity": "ERROR", "text": "unknown variable"},
        ]


class TestClientFactory:
    def test_url_set_returns_http_client(self):
        client = get_evaluation_client({"decision_engine_url": "http://svc:8080"})
        assert isinstance(client, HttpEvaluationClient)

    def test_url_absent_returns_inprocess_client(self):
        with patch.dict("os.environ", {}, clear=True):
            client = get_evaluation_client({})
            assert isinstance(client, InProcessEvaluationClient)

    def test_env_var_returns_http_client(self):
        with patch.dict("os.environ", {"DECISION_ENGINE_URL": "http://svc:8080"}):
            client = get_evaluation_client(None)
            assert isinstance(client, HttpEvaluationClient)
