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