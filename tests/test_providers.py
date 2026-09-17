"""Tests for providers.py — config-driven fetcher against canned responses.

Fetchers take an injectable `getter` returning (status:int, body:str, headers:dict)
and raising providers.FetchError on transport failure. No network in tests.
"""

import json

import pytest

import providers
from providers import FetchError


# ---------- shared helpers ----------

def ok(body, headers=None):
    return (200, body, headers or {})


class _MockGetter:
    """A mock getter recording the last call and returning a canned response."""
    def __init__(self):
        self.last_url = None
        self.last_headers = None
        self.response = (200, "{}", {})

    def __call__(self, url, headers=None, timeout=15):
        self.last_url = url
        self.last_headers = headers or {}
        return self.response


@pytest.fixture
def mock_getter():
    return _MockGetter()


# ---------- _parse_model_list ----------

def test_parse_model_list_wraps_data_key():
    items = providers._parse_model_list('{"data": [{"id": "a"}]}')
    assert items == [{"id": "a"}]


def test_parse_model_list_bare_list():
    items = providers._parse_model_list('[{"id": "a"}]')
    assert items == [{"id": "a"}]


def test_parse_model_list_unexpected_shape():
    with pytest.raises(FetchError, match="unexpected models payload shape"):
        providers._parse_model_list('{"foo": "bar"}')


# ---------- _extract_ids ----------

def test_extract_ids_dict_items():
    items = [{"id": "model-1"}, {"id": "model-2"}]
    ids = providers._extract_ids(items)
    assert ids == ["model-1", "model-2"]


def test_extract_ids_filters_none_id():
    items = [{"id": "model-1"}, {"id": None}, {"id": "model-2"}]
    ids = providers._extract_ids(items)
    assert ids == ["model-1", "model-2"]


def test_extract_ids_coerces_non_str():
    items = [{"id": 0}, {"id": 42}]
    ids = providers._extract_ids(items)
    assert ids == ["0", "42"]


def test_extract_ids_ignores_non_dict():
    items = [{"id": "model-1"}, "bare-string", {"id": "model-2"}]
    ids = providers._extract_ids(items)
    assert ids == ["model-1", "model-2"]


# ---------- _require_ok ----------

def test_require_ok_200():
    providers._require_ok(200, "https://example.com")


def test_require_ok_non_200():
    with pytest.raises(FetchError, match="HTTP 404 from https://example.com"):
        providers._require_ok(404, "https://example.com")


# ---------- fetch_provider (config-driven) ----------

def test_fetch_provider_config_driven(mock_getter):
    """fetch_provider uses config for URL, auth, and detection."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "id-suffix",
        "_token": "test-token",
    }
    mock_getter.response = (200, json.dumps({"data": [
        {"id": "model-free"}, {"id": "model-paid"}
    ]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["model-free"]
    assert mock_getter.last_headers["Authorization"] == "Bearer test-token"


def test_fetch_provider_no_auth(mock_getter):
    """fetch_provider with none auth doesn't send Authorization."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "all-free",
        "_token": None,
    }
    mock_getter.response = (200, json.dumps({"data": [{"id": "model"}]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["model"]
    assert "Authorization" not in mock_getter.last_headers


def test_fetch_provider_zero_credit_probe(mock_getter):
    """zero-credit-probe detection returns all ids without filtering."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "zero-credit-probe",
        "_token": "test-token",
    }
    mock_getter.response = (200, json.dumps({"data": [
        {"id": "model-free"}, {"id": "model-paid"}
    ]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["model-free", "model-paid"]


def test_fetch_provider_non_200_raises(mock_getter):
    """fetch_provider raises FetchError on non-200."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "all-free",
        "_token": None,
    }
    mock_getter.response = (403, "Forbidden", {})
    with pytest.raises(FetchError, match="HTTP 403 from https://example.com/v1/models"):
        providers.fetch_provider(config, getter=mock_getter)


def test_fetch_provider_malformed_json_raises(mock_getter):
    """fetch_provider raises FetchError on malformed JSON."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "all-free",
        "_token": None,
    }
    mock_getter.response = (200, "not json", {})
    with pytest.raises(FetchError, match="response was not valid JSON"):
        providers.fetch_provider(config, getter=mock_getter)


def test_fetch_provider_empty_200_is_real_data(mock_getter):
    """Healthy 200 with empty data returns [] not an error."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "all-free",
        "_token": None,
    }
    for payload in ['{"data": []}', "[]", '{"object": "list", "data": []}']:
        mock_getter.response = (200, payload, {})
        ids, meta = providers.fetch_provider(config, getter=mock_getter)
        assert ids == []


def test_fetch_provider_sends_user_agent(mock_getter):
    """fetch_provider always sends User-Agent."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "all-free",
        "_token": None,
    }
    mock_getter.response = (200, json.dumps({"data": [{"id": "model"}]}), {})
    providers.fetch_provider(config, getter=mock_getter)
    assert mock_getter.last_headers["User-Agent"] == "free-inference-watchdog/1.0"


def test_fetch_provider_url_uses_base_url_config(mock_getter):
    """fetch_provider constructs URL from config base_url."""
    config = {
        "name": "Test",
        "base_url": "https://api.example.com/v1",
        "detection": "all-free",
        "_token": None,
    }
    mock_getter.response = (200, json.dumps({"data": [{"id": "model"}]}), {})
    providers.fetch_provider(config, getter=mock_getter)
    assert mock_getter.last_url == "https://api.example.com/v1/models"


def test_fetch_provider_strips_trailing_slash(mock_getter):
    """fetch_provider strips trailing slash from base_url."""
    config = {
        "name": "Test",
        "base_url": "https://api.example.com/v1/",
        "detection": "all-free",
        "_token": None,
    }
    mock_getter.response = (200, json.dumps({"data": [{"id": "model"}]}), {})
    providers.fetch_provider(config, getter=mock_getter)
    assert mock_getter.last_url == "https://api.example.com/v1/models"


def test_fetch_provider_captures_ratelimit_headers():
    """x-ratelimit headers land in meta dict (nous telemetry contract)."""
    def getter(url, headers=None, timeout=15):
        body = json.dumps({"data": [{"id": "m1", "pricing": {"prompt": "0", "completion": "0"}}]})
        return 200, body, {"x-ratelimit-remaining-requests": "42", "x-ratelimit-limit-requests": "100"}
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "api-pricing", "_token": None}
    ids, meta = providers.fetch_provider(config, getter=getter)
    assert ids == ["m1"]
    assert meta.get("ratelimit", {}).get("x-ratelimit-remaining-requests") == "42"


@pytest.mark.parametrize("detection", ["id-suffix", "zero-credit-probe"])
def test_fetch_provider_preserves_raw_metadata(detection):
    model = {"id": "vendor/model:free", "context_length": 128000,
             "architecture": {"modalities": ["text", "image"]},
             "pricing": {"prompt": "0"}, "future_field": {"value": None}}
    config = {"base_url": "https://example.com/v1", "detection": detection,
              "ignored_slugs": ["ignored-free"]}
    items = [model, {"id": "ignored-free"}, {"id": "paid"}]
    ids, meta = providers.fetch_provider(
        config, getter=lambda *a, **kw: ok(json.dumps({"data": items}),
                                          {"X-Ratelimit-Remaining": "42"}))
    assert meta["models"][model["id"]] == model
    assert set(meta["models"]) == set(ids)
    assert "ignored-free" not in meta["models"]
    assert meta["ratelimit"] == {"X-Ratelimit-Remaining": "42"}


def test_fetch_provider_no_ratelimit_headers_omits_ratelimit():
    """No ratelimit headers -> model metadata only, no ratelimit key."""
    def getter(url, headers=None, timeout=15):
        body = json.dumps({"data": [{"id": "m1", "pricing": {"prompt": "0", "completion": "0"}}]})
        return 200, body, {}
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "api-pricing", "_token": None}
    ids, meta = providers.fetch_provider(config, getter=getter)
    assert ids == ["m1"]
    assert "ratelimit" not in meta
    assert meta["models"]["m1"]["pricing"] == {"prompt": "0", "completion": "0"}


# ---------- ignored_slugs: config-driven exclusions at the detection layer ----------


def test_fetch_provider_ignored_slugs_excludes_on_detect_free(mock_getter):
    """An id in ignored_slugs is dropped even though it passes the free-marker
    detection — the exclusion is applied AFTER detection, which itself stays
    exactly as-is (openrouter case: openrouter/free matches the free rule)."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "id-suffix",
        "_token": None,
        "ignored_slugs": ["test-co/router-free"],
    }
    mock_getter.response = (200, json.dumps({"data": [
        {"id": "vendor/model:free"}, {"id": "test-co/router-free"},
    ]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["vendor/model:free"]


def test_fetch_provider_ignored_slugs_excludes_on_zero_credit_probe(mock_getter):
    """The zero-credit-probe catalog branch applies ignored_slugs too — the
    exclusion covers BOTH return paths (kilo case: kilo-auto/free)."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "zero-credit-probe",
        "_token": None,
        "ignored_slugs": ["kilo-auto/free"],
    }
    mock_getter.response = (200, json.dumps({"data": [
        {"id": "kilo-auto/free"}, {"id": "cohere/north-mini-code:free"},
    ]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["cohere/north-mini-code:free"]


def test_fetch_provider_ignored_slugs_absent_is_noop(mock_getter):
    """Configs without the optional field behave exactly as before — the
    feature is purely additive."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "id-suffix",
        "_token": None,
    }
    mock_getter.response = (200, json.dumps({"data": [
        {"id": "vendor/model:free"}, {"id": "test-co/router-free"},
    ]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["test-co/router-free", "vendor/model:free"]