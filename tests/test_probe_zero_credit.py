# tests/test_probe_zero_credit.py
import pytest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from probe_zero_credit import probe_model, Result


def test_probe_free():
    mock_resp = MagicMock()
    mock_resp.status = 200
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.FREE


def test_probe_paid():
    error = HTTPError("url", 403, "Forbidden", {}, None)
    error.read = MagicMock(return_value=b'{"error": "deposit required"}')
    with patch("urllib.request.urlopen", side_effect=error):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.PAID


def test_probe_rate_limited():
    error = HTTPError("url", 429, "Rate limited", {}, None)
    error.read = MagicMock(return_value=b"")
    with patch("urllib.request.urlopen", side_effect=error):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.DEFER


def test_probe_server_error():
    error = HTTPError("url", 500, "Server error", {}, None)
    error.read = MagicMock(return_value=b"")
    with patch("urllib.request.urlopen", side_effect=error):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.DEFER


def test_probe_not_found():
    error = HTTPError("url", 404, "Not found", {}, None)
    error.read = MagicMock(return_value=b"")
    with patch("urllib.request.urlopen", side_effect=error):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.DEFER


def test_probe_network_error():
    with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.DEFER
        assert "timeout" in meta["error"]


def test_probe_403_without_deposit():
    """HTTP 403 without 'deposit' in body → DEFER (not PAID)."""
    error = HTTPError("url", 403, "Forbidden", {}, None)
    error.read = MagicMock(return_value=b'{"error": "access denied"}')
    with patch("urllib.request.urlopen", side_effect=error):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.DEFER


def test_probe_request_construction():
    """Verify request URL, headers, and body are correctly constructed."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        probe_model("https://example.com/v1", "my-token", "test-model")
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert req.full_url == "https://example.com/v1/chat/completions"
        assert req.headers["Authorization"] == "Bearer my-token"
        assert req.headers["Content-type"] == "application/json" or req.get_header("Content-type") == "application/json"
        import json as json_mod
        body = json_mod.loads(req.data)
        assert body["model"] == "test-model"
        assert body["max_tokens"] == 1
        assert body["messages"][0]["content"] == "hi"


def test_probe_deposit_case_insensitive():
    """'DEPOSIT', 'Deposit' etc should match."""
    error = HTTPError("url", 403, "Forbidden", {}, None)
    error.read = MagicMock(return_value=b'{"error": "DEPOSIT required"}')
    with patch("urllib.request.urlopen", side_effect=error):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.PAID