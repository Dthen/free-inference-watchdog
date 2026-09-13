# tests/test_probe_zero_credit.py
import pytest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from probe_zero_credit import probe_model, Result

# Live evidence bodies captured from the real b.ai API (zero-credit key),
# 2026-09 P5 verification. Keep these verbatim — they pin the classifier.
REAL_403_DEPOSIT = (
    b'{"error":{"code":"access_denied","message":"Access restricted. '
    b'Deposit required to unlock premium models. ..."}}'
)
REAL_400_INSUFFICIENT_BALANCE = (
    b'{"error":{"message":"credit insufficient balance: balance=0 required=102 '
    b'(request id: ...)","type":"api_error","param":"",'
    b'"code":"insufficient_user_quota"}}'
)
REAL_400_QUOTA_CODE_ONLY = (
    b'{"error":{"message":"The requested model requires additional quota.",'
    b'"type":"api_error","param":"","code":"insufficient_user_quota"}}'
)
REAL_400_PROBE_SHAPE_BUG = (
    b'{"error":{"message":"max_tokens must be greater than 2",'
    b'"type":"invalid_request_error","param":"max_tokens","code":null}}'
)


def test_probe_free():
    mock_resp = MagicMock()
    mock_resp.status = 200
    with patch("urllib.request.urlopen", return_value=mock_resp):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.FREE


def test_probe_paid():
    error = HTTPError("url", 403, "Forbidden", {}, None)
    error.read = MagicMock(return_value=REAL_403_DEPOSIT)
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
        assert body["max_tokens"] == 3
        assert body["messages"][0]["content"] == "hi"


def test_probe_deposit_case_insensitive():
    """'DEPOSIT', 'Deposit' etc should match."""
    error = HTTPError("url", 403, "Forbidden", {}, None)
    error.read = MagicMock(return_value=b'{"error": "DEPOSIT required"}')
    with patch("urllib.request.urlopen", side_effect=error):
        result, meta = probe_model("https://example.com/v1", "token", "model")
        assert result == Result.PAID


@pytest.mark.parametrize("body,expected", [
    # b.ai's PAID signal for this model class: HTTP 400 insufficient-balance/quota
    (REAL_400_INSUFFICIENT_BALANCE, Result.PAID),
    (REAL_400_QUOTA_CODE_ONLY, Result.PAID),
    # A 400 without those markers is a probe-shape bug — stay DEFER so it
    # self-heals rather than misclassifying the model as PAID.
    (REAL_400_PROBE_SHAPE_BUG, Result.DEFER),
])
def test_probe_400_classification(body, expected):
    """HTTP 400: insufficient balance/quota → PAID; other 400s → DEFER."""
    error = HTTPError("url", 400, "Bad Request", {}, None)
    error.read = MagicMock(return_value=body)
    with patch("urllib.request.urlopen", side_effect=error):
        result, meta = probe_model("https://example.com/v1", "token", "glm-5.3-flash")
        assert result == expected
        assert meta["http"] == 400