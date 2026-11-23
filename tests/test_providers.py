"""Tests for providers.py — config-driven fetcher against synthetic responses.

Fetchers take an injectable `getter` returning (status:int, body:str, headers:dict)
and raising providers.FetchError on transport failure. No network in tests, and
no captured upstream bodies: every catalogue payload below is hand-authored in
this file, so each case asserts a rule rather than one frozen capture.
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
    exactly as-is."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "id-suffix",
        "_token": None,
        "ignored_slugs": ["synthetic/router-free"],
    }
    mock_getter.response = (200, json.dumps({"data": [
        {"id": "vendor/model:free"}, {"id": "synthetic/router-free"},
    ]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["vendor/model:free"]


def test_fetch_provider_ignored_slugs_excludes_on_zero_credit_probe(mock_getter):
    """The zero-credit-probe catalog branch applies ignored_slugs too — the
    exclusion covers BOTH return paths."""
    config = {
        "name": "Test",
        "base_url": "https://example.com/v1",
        "detection": "zero-credit-probe",
        "_token": None,
        "ignored_slugs": ["synthetic/auto-free"],
    }
    mock_getter.response = (200, json.dumps({"data": [
        {"id": "synthetic/auto-free"}, {"id": "vendor/other:free"},
    ]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["vendor/other:free"]


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
        {"id": "vendor/model:free"}, {"id": "synthetic/router-free"},
    ]}), {})
    ids, meta = providers.fetch_provider(config, getter=mock_getter)
    assert ids == ["synthetic/router-free", "vendor/model:free"]

# ---------- catalogue seam: catalog_url / catalog_format (P1, Vercel) ----------
#
# Hand-authored catalogue bodies, built here rather than read from a capture:
# ids are synthetic and each row publishes only the members the adapter
# contract reads, so these cases pin behaviour instead of one frozen payload.

VERCEL_ROWS = [
    {"id": "synthetic/alpha-free", "type": "language",
     "name": "Synthetic Alpha", "description": "hand-authored free chat row",
     "context_window": 8192,
     "modalities": {"input": ["text"], "output": ["text"]},
     "supported_parameters": ["temperature"],
     "pricing": {"input": "0", "output": "0"}},
    {"id": "synthetic/beta-free", "type": "language",
     "name": "Synthetic Beta",
     "pricing": {"input": "0", "output": "0", "request": "0"}},
    {"id": "synthetic/gamma-paid", "type": "language",
     "name": "Synthetic Gamma",
     "pricing": {"input": "0.5", "output": "0"}},
]
VERCEL_ROWS_BY_ID = {row["id"]: row for row in VERCEL_ROWS}
VERCEL_FREE_IDS = ["synthetic/alpha-free", "synthetic/beta-free"]
VERCEL_TEXT = json.dumps({"object": "list", "data": VERCEL_ROWS})


def _vercel_config(**overrides):
    config = {
        "name": "Vercel AI Gateway",
        "base_url": "https://ai-gateway.vercel.sh/v1",
        "catalog_url": "https://ai-gateway.vercel.sh/v1/models",
        "catalog_format": "input-output-models",
        "detection": "catalog-pricing",
        "auth": {"method": "none"},
        "_token": None,
    }
    config.update(overrides)
    return config


class _RecordingGetter:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, headers=None, timeout=15):
        self.calls.append({"url": url, "headers": headers or {},
                           "timeout": timeout})
        return self.response


def test_catalog_url_is_fetched_literally_with_its_query():
    getter = _RecordingGetter((200, VERCEL_TEXT, {}))
    config = _vercel_config(
        catalog_url=("https://ai-gateway.vercel.sh/v1/models"
                     "?include=pricing%2Cmodalities&x=1"))
    providers.fetch_provider(config, getter=getter)
    assert getter.calls[0]["url"] == (
        "https://ai-gateway.vercel.sh/v1/models"
        "?include=pricing%2Cmodalities&x=1")


def test_catalog_url_never_derives_the_inference_url():
    config = _vercel_config()
    before = json.loads(json.dumps(config))
    getter = _RecordingGetter((200, VERCEL_TEXT, {}))
    providers.fetch_provider(config, getter=getter)
    assert config == before, "fetch must not rewrite base_url or add fields"
    assert getter.calls[0]["url"] == "https://ai-gateway.vercel.sh/v1/models"


def test_catalog_format_selects_exactly_the_free_rows_and_keeps_them_raw():
    getter = _RecordingGetter((200, VERCEL_TEXT, {}))
    ids, meta = providers.fetch_provider(_vercel_config(), getter=getter)
    assert ids == sorted(VERCEL_FREE_IDS)
    assert "synthetic/gamma-paid" not in ids, "a positive rate is not free"
    assert set(meta["models"]) == set(VERCEL_FREE_IDS)
    for model_id in VERCEL_FREE_IDS:
        assert meta["models"][model_id] == VERCEL_ROWS_BY_ID[model_id]
        assert "pricing" in meta["models"][model_id], "raw row, not a view"


def test_catalog_format_sends_the_user_agent_and_no_authorization():
    getter = _RecordingGetter((200, VERCEL_TEXT, {}))
    providers.fetch_provider(_vercel_config(), getter=getter)
    headers = getter.calls[0]["headers"]
    assert headers["User-Agent"] == "free-inference-watchdog/1.0"
    assert "Authorization" not in headers


def test_cross_origin_token_override_is_refused_before_the_getter():
    def exploding_getter(url, headers=None, timeout=15):
        raise AssertionError("getter must not run for a refused credential")

    config = _vercel_config(
        _token="synthetic-token",
        catalog_url="https://mirror.example.net/v1/models")
    with pytest.raises(FetchError, match="cross-origin"):
        providers.fetch_provider(config, getter=exploding_getter)


@pytest.mark.parametrize("catalog_url", [
    "http://ai-gateway.vercel.sh/v1/models",
    "https://ai-gateway.vercel.sh:8443/v1/models",
    "https://other.vercel.sh/v1/models",
])
def test_cross_origin_combinations_with_a_token_are_all_refused(catalog_url):
    seen = []

    def getter(url, headers=None, timeout=15):
        seen.append(url)
        return 200, VERCEL_TEXT, {}

    config = _vercel_config(_token="synthetic-token", catalog_url=catalog_url)
    with pytest.raises(FetchError, match="cross-origin"):
        providers.fetch_provider(config, getter=getter)
    assert seen == []


def test_same_origin_effective_port_override_keeps_authentication():
    getter = _RecordingGetter((200, VERCEL_TEXT, {}))
    config = _vercel_config(
        _token="synthetic-token",
        catalog_url="https://ai-gateway.vercel.sh:443/v1/models")
    ids, _meta = providers.fetch_provider(config, getter=getter)
    assert ids == sorted(VERCEL_FREE_IDS)
    assert getter.calls[0]["headers"]["Authorization"] == "Bearer synthetic-token"


def test_anonymous_cross_origin_override_is_allowed():
    getter = _RecordingGetter((200, VERCEL_TEXT, {}))
    config = _vercel_config(catalog_url="https://mirror.example.net/v1/models")
    ids, _meta = providers.fetch_provider(config, getter=getter)
    assert ids == sorted(VERCEL_FREE_IDS)
    assert "Authorization" not in getter.calls[0]["headers"]


def test_conflicting_duplicate_ids_are_a_fetch_error():
    body = json.dumps({"data": [
        {"id": "dup/model", "type": "language",
         "pricing": {"input": "0", "output": "0"}},
        {"id": "dup/model", "type": "language",
         "pricing": {"input": "1", "output": "0"}},
    ]})
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(_vercel_config(),
                                 getter=_RecordingGetter((200, body, {})))


@pytest.mark.parametrize("body", [
    '{"data": {}}',
    "not json",
    "[]",
    '{"data": [{"type": "language"}]}',
])
def test_malformed_new_envelopes_are_fetch_errors(body):
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(_vercel_config(),
                                 getter=_RecordingGetter((200, body, {})))


@pytest.mark.parametrize("pricing", [
    {"input": True, "output": "0"},
    {"input": "0", "output": "nan"},
    {"input": ["0"], "output": "0"},
])
def test_malformed_new_billing_fields_are_fetch_errors(pricing):
    body = json.dumps({"data": [{"id": "bad/model", "type": "language",
                                 "pricing": pricing}]})
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(_vercel_config(),
                                 getter=_RecordingGetter((200, body, {})))


def test_valid_empty_and_paid_only_new_catalogues_stay_successes():
    for body, expected in [('{"data": []}', []),
                           ('{"data": [{"id": "paid/model", '
                            '"type": "language", "pricing": '
                            '{"input": "1", "output": "1"}}]}', [])]:
        ids, meta = providers.fetch_provider(
            _vercel_config(), getter=_RecordingGetter((200, body, {})))
        assert ids == expected and meta["models"] == {}


def test_new_format_preserves_literal_ids_and_raw_rows():
    rows = [
        {"id": "  Vendor/Padded  ", "type": "language",
         "pricing": {"input": "0", "output": "0"}},
        {"id": "vendor/Ünicode-Δ", "type": "language",
         "pricing": {"input": "0", "output": "0"}},
    ]
    body = json.dumps({"data": rows})
    ids, meta = providers.fetch_provider(
        _vercel_config(), getter=_RecordingGetter((200, body, {})))
    assert ids == ["  Vendor/Padded  ", "vendor/Ünicode-Δ"]
    assert meta["models"]["  Vendor/Padded  "] == rows[0]
    assert meta["models"]["vendor/Ünicode-Δ"] == rows[1]


def test_new_format_still_applies_ignored_slugs_after_detection():
    config = _vercel_config(ignored_slugs=["synthetic/beta-free"])
    ids, meta = providers.fetch_provider(
        config, getter=_RecordingGetter((200, VERCEL_TEXT, {})))
    assert ids == ["synthetic/alpha-free"]
    assert "synthetic/beta-free" not in meta["models"]
    assert set(meta["models"]) == {"synthetic/alpha-free"}


def test_url_only_override_keeps_the_legacy_shape_and_detector():
    config = {
        "name": "Legacy",
        "base_url": "https://api.example.com/v1",
        "catalog_url": "https://mirror.example.net/models?page=1",
        "detection": "api-pricing",
        "_token": None,
    }
    body = json.dumps({"data": [
        {"id": "m-free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "m-paid", "pricing": {"prompt": "0.1", "completion": "0"}},
    ]})
    getter = _RecordingGetter((200, body, {}))
    ids, meta = providers.fetch_provider(config, getter=getter)
    assert ids == ["m-free"]
    assert getter.calls[0]["url"] == "https://mirror.example.net/models?page=1"
    assert meta["models"]["m-free"] == json.loads(body)["data"][0]


# ---------- peak_pricing closure through the real fetcher ----------

OBSERVED_PEAK = {"multiplier": 2,
                 "windows": [{"start_minute_utc": 540,
                              "end_minute_utc": 90}]}

PEAK_LAUNDERING_SHAPES = [
    {"multiplier": 2, "input": "0.5"},
    {"multiplier": 2, "output": "0.5"},
    {"multiplier": 2, "request": "0.75"},
    {"multiplier": 2, "additive_fee": "0.01"},
    {"multiplier": 2, "windows": [{"start_minute_utc": 540,
                                   "end_minute_utc": 90,
                                   "input": "0.5"}]},
]


@pytest.mark.parametrize("peak", PEAK_LAUNDERING_SHAPES)
def test_peak_priced_members_never_reach_ids_or_raw_metadata(peak):
    """A peak branch carrying a rate/fee cannot launder a zero base.

    The clean sibling still qualifies, so this is a per-row exclusion rather
    than a whole-feed failure.
    """
    peak_row = {"id": "vendor/peak-row", "type": "language",
                "pricing": {"input": "0", "output": "0",
                            "peak_pricing": peak}}
    clean_row = {"id": "vendor/clean-free", "type": "language",
                 "pricing": {"input": "0", "output": "0"}}
    body = json.dumps({"object": "list", "data": [peak_row, clean_row]})
    ids, meta = providers.fetch_provider(
        _vercel_config(), getter=_RecordingGetter((200, body, {})))
    assert ids == ["vendor/clean-free"]
    assert set(meta["models"]) == {"vendor/clean-free"}
    assert meta["models"]["vendor/clean-free"] == clean_row


def test_observed_peak_shape_still_admits_the_zero_row():
    row = {"id": "vendor/peak-row", "type": "language",
           "pricing": {"input": "0", "output": "0",
                       "peak_pricing": OBSERVED_PEAK}}
    body = json.dumps({"object": "list", "data": [row]})
    ids, meta = providers.fetch_provider(
        _vercel_config(), getter=_RecordingGetter((200, body, {})))
    assert ids == ["vendor/peak-row"]
    assert meta["models"]["vendor/peak-row"] == row


# ---------- explicit upstream failure envelope through the real fetcher ----------

FREE_ROW = {"id": "vendor/free", "type": "language",
            "pricing": {"input": "0", "output": "0"}}
PAID_ROW = {"id": "vendor/paid", "type": "language",
            "pricing": {"input": "1", "output": "1"}}


@pytest.mark.parametrize("data", [[], [FREE_ROW], [PAID_ROW],
                                  [FREE_ROW, PAID_ROW]])
@pytest.mark.parametrize("extra", [{}, {"object": "list"}])
def test_success_false_body_is_a_fetch_error_not_an_empty_success(data, extra):
    """`success: false` is a failed observation even with a valid data list.

    The request is made once and the body is rejected: no ids, no metadata, so
    the caller sees the existing sticky carry-forward path, not a mass removal.
    """
    body = json.dumps({"success": False, "data": data, **extra})
    getter = _RecordingGetter((200, body, {}))
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(_vercel_config(), getter=getter)
    assert len(getter.calls) == 1, "the body, not the request, is the fault"


@pytest.mark.parametrize("envelope,expected_ids", [
    ({"data": []}, []),
    ({"success": True, "data": []}, []),
    ({"success": True, "data": [FREE_ROW]}, ["vendor/free"]),
    ({"success": True, "data": [PAID_ROW]}, []),
    ({"success": None, "data": []}, []),
    ({"success": 0, "data": []}, []),
    ({"success": "false", "data": []}, []),
])
def test_absent_true_or_non_boolean_success_keeps_success_semantics(
        envelope, expected_ids):
    body = json.dumps(envelope)
    ids, meta = providers.fetch_provider(
        _vercel_config(), getter=_RecordingGetter((200, body, {})))
    assert ids == sorted(expected_ids)
    assert set(meta["models"]) == set(expected_ids)


# ---------- catalogue seam: Felo channel-catalog (P2) ----------

_UNSET = object()


def _felo_channel(prompt="0", completion="0", *, runnable=True,
                  pricing_mode="configured_rates", discount="1"):
    """One channel on the documented configured-rate token shape."""
    return {"runnable": runnable, "pricing_mode": pricing_mode,
            "discount_multiplier": discount,
            "standard_pricing": {"billing_kind": "token", "prompt": prompt,
                                 "completion": completion, "tiers": []}}


def _felo_row(model_id, *, channels=None, **extra):
    row = {"id": model_id, "deprecated": False, "model_kind": "chat",
           "output_modalities": ["text"],
           "channels": [_felo_channel()] if channels is None else channels}
    row.update(extra)
    return row


def _felo_body(models, total=_UNSET):
    models = list(models)
    return json.dumps({"data": {"models": models,
                                "total": len(models) if total is _UNSET
                                else total}})


FELO_FREE_ROWS = [
    _felo_row("synthetic/felo-alpha:free", name="Synthetic Alpha",
              description="hand-authored free chat row", context_length=32768,
              input_modalities=["text"]),
    _felo_row("synthetic/felo-beta:free",
              channels=[_felo_channel(), _felo_channel(prompt="0")]),
    _felo_row("synthetic/felo-gamma:free"),
]
FELO_FREE_IDS = [row["id"] for row in FELO_FREE_ROWS]
FELO_TEXT = _felo_body(FELO_FREE_ROWS)


def _felo_config(**overrides):
    config = {
        "name": "Felo",
        "base_url": "https://openapi.felo.ai/api/v1",
        "catalog_url": "https://openapi.felo.ai/api/platform/model-catalog",
        "catalog_format": "channel-catalog",
        "detection": "catalog-pricing",
        "auth": {"method": "none"},
        "_token": None,
    }
    config.update(overrides)
    return config


def test_felo_fetch_uses_the_literal_catalogue_url_and_anonymous_headers():
    getter = _RecordingGetter((200, FELO_TEXT, {}))
    providers.fetch_provider(_felo_config(), getter=getter)
    call = getter.calls[0]
    assert call["url"] == "https://openapi.felo.ai/api/platform/model-catalog"
    assert call["headers"]["User-Agent"] == "free-inference-watchdog/1.0"
    assert "Authorization" not in call["headers"]


def test_felo_fetch_returns_the_free_rows_with_their_raw_records():
    expected_rows = {row["id"]: row for row in FELO_FREE_ROWS}
    ids, meta = providers.fetch_provider(
        _felo_config(), getter=_RecordingGetter((200, FELO_TEXT, {})))
    assert ids == sorted(FELO_FREE_IDS)
    assert set(meta["models"]) == set(FELO_FREE_IDS)
    for model_id in FELO_FREE_IDS:
        assert meta["models"][model_id] == expected_rows[model_id]
        assert "channels" in meta["models"][model_id], "raw row, not a view"


def test_felo_fetch_applies_ignored_ids_and_keeps_passive_ratelimits():
    getter = _RecordingGetter((200, FELO_TEXT,
                               {"X-RateLimit-Limit": "60",
                                "Content-Type": "application/json"}))
    config = _felo_config(ignored_slugs=["synthetic/felo-gamma:free"])
    ids, meta = providers.fetch_provider(config, getter=getter)
    assert ids == ["synthetic/felo-alpha:free", "synthetic/felo-beta:free"]
    assert "synthetic/felo-gamma:free" not in meta["models"]
    assert meta["ratelimit"] == {"X-RateLimit-Limit": "60"}


@pytest.mark.parametrize("total", [None, True, False, -1, 1.0, "3", 2, 4])
def test_felo_missing_or_wrong_total_is_a_fetch_error(total):
    body = _felo_body(FELO_FREE_ROWS, total=total)
    with pytest.raises(FetchError, match="total"):
        providers.fetch_provider(_felo_config(),
                                 getter=_RecordingGetter((200, body, {})))


def test_felo_missing_total_member_is_a_fetch_error():
    payload = json.loads(FELO_TEXT)
    del payload["data"]["total"]
    with pytest.raises(FetchError, match="total"):
        providers.fetch_provider(_felo_config(),
                                 getter=_RecordingGetter(
                                     (200, json.dumps(payload), {})))


def test_felo_success_false_is_a_fetch_error_not_an_empty_success():
    payload = json.loads(FELO_TEXT)
    payload["success"] = False
    getter = _RecordingGetter((200, json.dumps(payload), {}))
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(_felo_config(), getter=getter)
    assert len(getter.calls) == 1


def test_felo_valid_empty_and_paid_only_feeds_stay_successes():
    paid_row = _felo_row("synthetic/paid-model",
                         channels=[_felo_channel(prompt="0.000001")])
    for body in (_felo_body([]), _felo_body([paid_row])):
        ids, meta = providers.fetch_provider(
            _felo_config(), getter=_RecordingGetter((200, body, {})))
        assert ids == [] and meta["models"] == {}


def test_felo_duplicate_conflict_is_a_fetch_error():
    row = _felo_row("synthetic/dup/model")
    changed = _felo_row("synthetic/dup/model",
                        channels=[_felo_channel(prompt="0.5")])
    body = _felo_body([row, changed])
    with pytest.raises(FetchError, match="duplicate"):
        providers.fetch_provider(_felo_config(),
                                 getter=_RecordingGetter((200, body, {})))


def _felo_raw_duplicate_body(*prompts):
    rows = [
        ('{"id":"dup/x","deprecated":false,"model_kind":"chat",'
         '"output_modalities":["text"],"channels":[{"runnable":true,'
         '"pricing_mode":"configured_rates","discount_multiplier":"1",'
         '"standard_pricing":{"billing_kind":"token","prompt":%s,'
         '"completion":0,"tiers":[]}}]}' % prompt)
        for prompt in prompts]
    return '{"data":{"total":%d,"models":[%s]}}' % (
        len(rows), ",".join(rows))


@pytest.mark.parametrize("order", [("0", "1e-400"), ("1e-400", "0")])
def test_felo_conflicting_numeric_duplicate_is_a_fetch_error_in_both_orders(
        order):
    body = _felo_raw_duplicate_body(*order)
    assert '"prompt":1e-400' in body and '"1e-400"' not in body
    ordinary = [row["channels"][0]["standard_pricing"]["prompt"]
                for row in json.loads(body)["data"]["models"]]
    assert ordinary == [0.0, 0.0], "setup: the raw token underflows to zero"
    getter = _RecordingGetter((200, body, {}))
    with pytest.raises(FetchError, match="duplicate"):
        providers.fetch_provider(_felo_config(), getter=getter)
    assert len(getter.calls) == 1, "the body, not the request, is the fault"


def _vercel_raw_duplicate_body(*inputs):
    rows = [
        ('{"id":"dup/v","type":"language",'
         '"pricing":{"input":%s,"output":"0"}}' % token)
        for token in inputs]
    return '{"data":[%s]}' % ",".join(rows)


@pytest.mark.parametrize("order", [("0", "false"), ("false", "0")])
def test_vercel_conflicting_boolean_numeric_duplicate_is_a_fetch_error(
        order):
    """The same duplicate-equivalence class on the sibling format.

    `0 == False` in ordinary Python equality; the raw records differ, and the
    boolean spelling is a malformed price, so this is a conflict, not an
    equivalent duplicate.
    """
    body = _vercel_raw_duplicate_body(*order)
    ordinary = [row["pricing"]["input"] for row in json.loads(body)["data"]]
    assert ordinary[0] == ordinary[1], "setup: 0 == False collapses the pair"
    getter = _RecordingGetter((200, body, {}))
    with pytest.raises(FetchError, match="duplicate"):
        providers.fetch_provider(_vercel_config(), getter=getter)
    assert len(getter.calls) == 1, "the body, not the request, is the fault"


def test_registered_family_without_a_predicate_is_a_fetch_error(monkeypatch):
    """P1 QUALITY Q2: a registered family missing its predicate must not look
    like a healthy empty observation. This is a synthetic, test-only family.
    """
    import catalog_adapters

    monkeypatch.setattr(
        catalog_adapters, "REGISTERED_FORMATS",
        catalog_adapters.REGISTERED_FORMATS + ("synthetic-registered-family",))
    monkeypatch.setattr(catalog_adapters, "parse_catalog",
                        lambda body, fmt: [{"id": "vendor/model"}])
    getter = _RecordingGetter((200, '{"data": {"total": 1, "models": []}}', {}))
    config = _felo_config(catalog_format="synthetic-registered-family")
    with pytest.raises(FetchError, match="free-row rule"):
        providers.fetch_provider(config, getter=getter)
    assert len(getter.calls) == 1


# ---------- catalogue seam: TokenRA newapi-pricing (P3) ----------

TOKENRA_FREE_ROWS = [
    {"model_name": "synthetic/crouton-alpha", "quota_type": 0,
     "model_ratio": 0, "model_price": 0, "enable_groups": ["default"],
     "supported_endpoint_types": ["openai"],
     "description": "hand-authored free chat row"},
    {"model_name": "synthetic/nemotron-vl-free", "quota_type": 0,
     "model_ratio": 0, "model_price": 0, "enable_groups": ["default"],
     "supported_endpoint_types": ["openai"]},
]
TOKENRA_FREE_IDS = [row["model_name"] for row in TOKENRA_FREE_ROWS]
TOKENRA_TEXT = json.dumps({"success": True, "data": TOKENRA_FREE_ROWS})


def _tokenra_config(**overrides):
    config = {
        "name": "TokenRA",
        "base_url": "https://tokenra.io/v1",
        "catalog_url": "https://tokenra.io/api/pricing",
        "catalog_format": "newapi-pricing",
        "detection": "catalog-pricing",
        "auth": {"method": "none"},
        "_token": None,
    }
    config.update(overrides)
    return config


def test_tokenra_fetch_uses_the_literal_url_timeout_and_anonymous_headers():
    getter = _RecordingGetter((200, TOKENRA_TEXT, {}))
    providers.fetch_provider(_tokenra_config(), getter=getter)
    call = getter.calls[0]
    assert call["url"] == "https://tokenra.io/api/pricing"
    assert call["timeout"] == 15
    assert call["headers"]["User-Agent"] == "free-inference-watchdog/1.0"
    assert "Authorization" not in call["headers"]


def test_tokenra_fetch_returns_the_free_rows_and_raw_records():
    ids, meta = providers.fetch_provider(
        _tokenra_config(), getter=_RecordingGetter((200, TOKENRA_TEXT, {})))
    assert ids == TOKENRA_FREE_IDS
    expected_rows = {row["model_name"]: row for row in TOKENRA_FREE_ROWS}
    assert set(meta["models"]) == set(TOKENRA_FREE_IDS)
    for model_name in TOKENRA_FREE_IDS:
        assert meta["models"][model_name] == expected_rows[model_name]
        assert "id" not in meta["models"][model_name], "no inserted id"
        assert "model_ratio" in meta["models"][model_name], (
            "the raw row, not an ephemeral view")


def test_tokenra_model_name_keys_both_outputs_and_no_id_is_invented():
    """Identity selection is SHARED: ids and raw metadata use one key."""
    ids, meta = providers.fetch_provider(
        _tokenra_config(), getter=_RecordingGetter((200, TOKENRA_TEXT, {})))
    assert ids == sorted(meta["models"])
    for row in meta["models"].values():
        assert "id" not in row
        assert isinstance(row["model_name"], str)


def test_tokenra_ignored_ids_drop_after_detection():
    config = _tokenra_config(ignored_slugs=["synthetic/crouton-alpha"])
    ids, meta = providers.fetch_provider(
        config, getter=_RecordingGetter((200, TOKENRA_TEXT, {})))
    assert ids == ["synthetic/nemotron-vl-free"]
    assert set(meta["models"]) == {"synthetic/nemotron-vl-free"}


def test_tokenra_passive_ratelimit_headers_are_captured():
    headers = {"X-RateLimit-Remaining": "41",
               "Content-Type": "application/json"}
    _ids, meta = providers.fetch_provider(
        _tokenra_config(), getter=_RecordingGetter((200, TOKENRA_TEXT, headers)))
    assert meta["ratelimit"] == {"X-RateLimit-Remaining": "41"}


def test_tokenra_success_false_is_a_fetch_error():
    body = '{"success": false, "data": []}'
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(_tokenra_config(),
                                 getter=_RecordingGetter((200, body, {})))


@pytest.mark.parametrize("order", [("0", "1"), ("1", "0")])
def test_tokenra_conflicting_duplicates_are_fetch_errors_in_both_orders(order):
    rows = [('{"model_name":"dup/x","quota_type":0,"model_ratio":%s,'
             '"model_price":0,"enable_groups":["default"],'
             '"supported_endpoint_types":["openai"]}' % ratio)
            for ratio in order]
    body = '{"success": true, "data": [%s]}' % ",".join(rows)
    getter = _RecordingGetter((200, body, {}))
    with pytest.raises(FetchError, match="duplicate"):
        providers.fetch_provider(_tokenra_config(), getter=getter)
    assert len(getter.calls) == 1, "the body, not the request, is the fault"


@pytest.mark.parametrize("order", [("0", "1e-400"), ("1e-400", "0")])
def test_tokenra_conflicting_numeric_duplicate_is_a_fetch_error(order):
    """The exact JSON comparator is effective on the NEW identity field too."""
    rows = [('{"model_name":"dup/x","quota_type":0,"model_ratio":%s,'
             '"model_price":0,"enable_groups":["default"],'
             '"supported_endpoint_types":["openai"]}' % ratio)
            for ratio in order]
    body = '{"success": true, "data": [%s]}' % ",".join(rows)
    ordinary = [row["model_ratio"] for row in json.loads(body)["data"]]
    assert ordinary == [0.0, 0.0], "setup: the raw token underflows to zero"
    with pytest.raises(FetchError, match="duplicate"):
        providers.fetch_provider(_tokenra_config(),
                                 getter=_RecordingGetter((200, body, {})))


@pytest.mark.parametrize("row", [
    '{"model_name":"bad/x","quota_type":true,"model_ratio":0,'
    '"model_price":0,"enable_groups":["default"],'
    '"supported_endpoint_types":["openai"]}',
    '{"model_name":"bad/x","quota_type":0,"model_ratio":"0",'
    '"model_price":0,"enable_groups":["default"],'
    '"supported_endpoint_types":["openai"]}',
])
def test_tokenra_malformed_billing_row_is_a_fetch_error(row):
    body = '{"success": true, "data": [%s]}' % row
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(_tokenra_config(),
                                 getter=_RecordingGetter((200, body, {})))


def test_tokenra_valid_empty_and_paid_only_feeds_stay_successes():
    paid_only = ('{"success": true, "data": [{"model_name":"image/call",'
                  '"quota_type":1,"model_ratio":0,"model_price":0.012,'
                  '"enable_groups":["default"],'
                  '"supported_endpoint_types":["openai"]}]}')
    for body in ('{"success": true, "data": []}', paid_only):
        ids, meta = providers.fetch_provider(
            _tokenra_config(), getter=_RecordingGetter((200, body, {})))
        assert ids == [] and meta["models"] == {}


def test_tokenra_literal_identity_and_raw_row_are_preserved():
    rows = [
        {"model_name": "  Vendor/Padded  ", "quota_type": 0,
         "model_ratio": 0, "model_price": 0, "enable_groups": ["default"],
         "supported_endpoint_types": ["openai"]},
        {"model_name": "vendor/\u00dcnicode-\u0394", "quota_type": 0,
         "model_ratio": 0, "model_price": 0, "enable_groups": ["default"],
         "supported_endpoint_types": ["openai"]},
    ]
    body = json.dumps({"success": True, "data": rows})
    ids, meta = providers.fetch_provider(
        _tokenra_config(), getter=_RecordingGetter((200, body, {})))
    assert ids == ["  Vendor/Padded  ", "vendor/\u00dcnicode-\u0394"]
    assert meta["models"]["  Vendor/Padded  "] == rows[0]
    assert meta["models"]["vendor/\u00dcnicode-\u0394"] == rows[1]


# ---------- catalogue seam: Infron provider-models (P4) ----------


def _infron_row(model_id, *, min_prompt=0, min_completion=0, providers=None,
                **extra):
    row = {"id": model_id, "category_type": "LLM",
           "supported_endpoint_types": ["openai"],
           "output_modalities": ["text"], "deprecated": False,
           "is_display_only": False,
           "min_prompt_price": min_prompt,
           "min_completion_price": min_completion,
           "providers": ([{"prompt_price": 0, "completion_price": 0}]
                         if providers is None else providers)}
    row.update(extra)
    return row


INFRON_FREE_ROWS = [
    _infron_row("synthetic/deepseek-flash:free",
                display_name="Synthetic Flash",
                description="hand-authored free chat row",
                context_length=65536),
    _infron_row("synthetic/qwen-small:free", display_name="Synthetic Qwen"),
    _infron_row("synthetic/mixed-route",
                providers=[{"prompt_price": 0, "completion_price": 0},
                           {"prompt_price": 0.5, "completion_price": 0.5}]),
]
INFRON_FREE_IDS = ["synthetic/deepseek-flash:free",
                   "synthetic/qwen-small:free"]
INFRON_ROWS_BY_ID = {row["id"]: row for row in INFRON_FREE_ROWS}
INFRON_TEXT = json.dumps({"success": True, "data": INFRON_FREE_ROWS})


def _infron_config(**overrides):
    config = {
        "name": "Infron",
        "base_url": "https://llm.onerouter.pro/v1",
        "catalog_url": "https://llm.onerouter.pro/v1/models",
        "catalog_format": "provider-models",
        "detection": "catalog-pricing",
        "auth": {"method": "none"},
        "_token": None,
    }
    config.update(overrides)
    return config


def test_infron_fetch_uses_the_literal_url_timeout_and_anonymous_headers():
    getter = _RecordingGetter((200, INFRON_TEXT, {}))
    providers.fetch_provider(_infron_config(), getter=getter)
    call = getter.calls[0]
    assert call["url"] == "https://llm.onerouter.pro/v1/models"
    assert call["timeout"] == 15
    assert call["headers"]["User-Agent"] == "free-inference-watchdog/1.0"
    assert "Authorization" not in call["headers"]


def test_infron_base_derived_discovery_url_matches_the_literal_override():
    """`catalog_url` absent means the accepted `base_url + '/models'` path.

    The shipped config carries the literal URL, so both must select the SAME
    discovery request; `chat_completions_url` is always derived from `base_url`.
    """
    literal = _RecordingGetter((200, INFRON_TEXT, {}))
    derived = _RecordingGetter((200, INFRON_TEXT, {}))
    config = _infron_config()
    without_override = {key: value for key, value in config.items()
                        if key != "catalog_url"}
    providers.fetch_provider(config, getter=literal)
    providers.fetch_provider(without_override, getter=derived)
    assert literal.calls[0]["url"] == "https://llm.onerouter.pro/v1/models"
    assert derived.calls[0]["url"] == literal.calls[0]["url"]
    assert derived.calls[0]["headers"] == literal.calls[0]["headers"]


def test_infron_fetch_returns_the_free_rows_and_raw_records():
    ids, meta = providers.fetch_provider(
        _infron_config(), getter=_RecordingGetter((200, INFRON_TEXT, {})))
    assert ids == INFRON_FREE_IDS
    assert "synthetic/mixed-route" not in ids, "one paid offer disqualifies"
    assert set(meta["models"]) == set(INFRON_FREE_IDS)
    for model_id in INFRON_FREE_IDS:
        stored = meta["models"][model_id]
        assert stored == INFRON_ROWS_BY_ID[model_id]
        assert stored is not INFRON_ROWS_BY_ID[model_id], (
            "no aliasing of the source")
        assert "providers" in stored, "the raw row, not an ephemeral view"
        # Nothing is lifted, renamed, wrapped or injected.
        assert "endpoint_name" not in stored
        assert "upstream" not in stored and "_upstream" not in stored
        assert "name" not in stored, "display_name stays raw"


def test_infron_unsuffixed_launch_is_tracked_without_a_free_marker():
    row = _infron_row("synthetic/stealth-launch")
    body = json.dumps({"success": True, "data": [row]})
    ids, meta = providers.fetch_provider(
        _infron_config(), getter=_RecordingGetter((200, body, {})))
    assert ids == ["synthetic/stealth-launch"]
    assert meta["models"] == {"synthetic/stealth-launch": row}


def test_infron_free_named_tts_and_paid_route_rows_are_not_tracked():
    """`:free` in an id is evidence, never the rule: scope and offers decide."""
    tts = _infron_row("synthetic/tts-free/text-to-audio",
                      category_type="TTS", output_modalities=["audio"])
    paid_route = _infron_row("synthetic/video/text-to-video",
                             providers=[{"prompt_price": 0.04,
                                         "completion_price": 0}])
    body = json.dumps({"success": True, "data": [tts, paid_route]})
    ids, meta = providers.fetch_provider(
        _infron_config(ignored_slugs=[]),
        getter=_RecordingGetter((200, body, {})))
    assert ids == [] and meta["models"] == {}


def test_infron_ignored_ids_drop_after_detection():
    config = _infron_config(ignored_slugs=["synthetic/qwen-small:free"])
    ids, meta = providers.fetch_provider(
        config, getter=_RecordingGetter((200, INFRON_TEXT, {})))
    assert ids == ["synthetic/deepseek-flash:free"]
    assert set(meta["models"]) == {"synthetic/deepseek-flash:free"}


def test_infron_passive_ratelimit_headers_are_captured():
    headers = {"X-RateLimit-Remaining": "12", "Content-Type": "application/json"}
    _ids, meta = providers.fetch_provider(
        _infron_config(), getter=_RecordingGetter((200, INFRON_TEXT, headers)))
    assert meta["ratelimit"] == {"X-RateLimit-Remaining": "12"}


def test_infron_success_false_and_html_are_fetch_errors():
    for body in ('{"success": false, "data": []}',
                 "<html><body>upstream maintenance</body></html>"):
        getter = _RecordingGetter((200, body, {}))
        with pytest.raises(FetchError):
            providers.fetch_provider(_infron_config(), getter=getter)
        assert len(getter.calls) == 1, "the body, not the request, is the fault"


@pytest.mark.parametrize("order", [("0", "0.5"), ("0.5", "0")])
def test_infron_conflicting_duplicates_are_fetch_errors_in_both_orders(order):
    rows = [('{"id":"dup/x","category_type":"LLM",'
             '"supported_endpoint_types":["openai"],'
             '"output_modalities":["text"],"deprecated":false,'
             '"is_display_only":false,"min_prompt_price":%s,'
             '"min_completion_price":0,"providers":[{"prompt_price":0,'
             '"completion_price":0}]}' % minimum) for minimum in order]
    body = '{"success": true, "data": [%s]}' % ",".join(rows)
    getter = _RecordingGetter((200, body, {}))
    with pytest.raises(FetchError, match="duplicate"):
        providers.fetch_provider(_infron_config(), getter=getter)
    assert len(getter.calls) == 1


@pytest.mark.parametrize("row", [
    '{"id":"bad/x","category_type":"LLM",'
    '"supported_endpoint_types":["openai"],"output_modalities":["text"],'
    '"deprecated":false,"is_display_only":false,'
    '"min_prompt_price":"0","min_completion_price":0,'
    '"providers":[{"prompt_price":0,"completion_price":0}]}',
    '{"id":"bad/x","category_type":"LLM",'
    '"supported_endpoint_types":["openai"],"output_modalities":["text"],'
    '"deprecated":false,"is_display_only":false,"min_prompt_price":0,'
    '"min_completion_price":0,"providers":[{"prompt_price":0,'
    '"completion_price":0},"not-a-provider"]}',
])
def test_infron_malformed_required_row_is_a_fetch_error(row):
    body = '{"success": true, "data": [%s]}' % row
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(_infron_config(),
                                 getter=_RecordingGetter((200, body, {})))


def test_infron_valid_empty_and_paid_only_feeds_stay_successes():
    paid_only = _infron_row("synthetic/paid-model", min_prompt=0.05,
                            min_completion=0.25,
                            providers=[{"prompt_price": 0.05,
                                        "completion_price": 0.25}])
    for rows in ([], [paid_only]):
        body = json.dumps({"success": True, "data": rows})
        ids, meta = providers.fetch_provider(
            _infron_config(), getter=_RecordingGetter((200, body, {})))
        assert ids == [] and meta["models"] == {}


# ---------- catalogue seam: AI/ML API typed-unit-models (P5) ----------

AIMLAPI_URL = ("https://api.aimlapi.com/v1/models?include=pricing,modalities"
               "&type=openai/chat-completions")


def _aimlapi_body(*rows):
    return json.dumps({"object": "list", "data": list(rows)})


def _aimlapi_config(**overrides):
    config = {
        "name": "AI/ML API",
        "base_url": "https://api.aimlapi.com/v1",
        "catalog_url": AIMLAPI_URL,
        "catalog_format": "typed-unit-models",
        "detection": "catalog-pricing",
        "auth": {"method": "none"},
        "_token": None,
    }
    config.update(overrides)
    return config


def _aimlapi_future_row(model_id="vendor/future-fixed-zero"):
    return {
        "id": model_id,
        "info": {"name": "Future Zero", "description": "synthetic future row",
                 "contextLength": 1000},
        "type": "openai/chat-completions",
        "modalities": {"input": ["text"], "output": ["text"]},
        "pricing": {
            "currency": "USD", "kind": "fixed", "variable": False,
            "thresholds": [], "dimensions": [], "variants": [],
            "units": [{"name": "token", "price": 0, "per": 1000000,
                       "variable": False}],
        },
    }


def _aimlapi_variable_row(model_id="synthetic/future-variable"):
    """A valid row whose zeros are the absence of a quote, not a free price."""
    row = _aimlapi_future_row(model_id)
    row["pricing"]["kind"] = "variable"
    row["pricing"]["variable"] = True
    return row


def test_aimlapi_fetch_uses_the_literal_query_and_anonymous_headers():
    getter = _RecordingGetter((200, _aimlapi_body(_aimlapi_future_row()), {}))
    providers.fetch_provider(_aimlapi_config(), getter=getter)
    call = getter.calls[0]
    assert call["url"] == AIMLAPI_URL
    assert call["timeout"] == 15
    assert call["headers"]["User-Agent"] == "free-inference-watchdog/1.0"
    assert "Authorization" not in call["headers"]


def test_aimlapi_base_derived_discovery_url_and_base_wiring_are_unchanged():
    """The override carries a query; the base-derived path is still `/models`."""
    body = _aimlapi_body(_aimlapi_future_row())
    literal = _RecordingGetter((200, body, {}))
    derived = _RecordingGetter((200, body, {}))
    config = _aimlapi_config()
    without_override = {key: value for key, value in config.items()
                        if key != "catalog_url"}
    providers.fetch_provider(config, getter=literal)
    providers.fetch_provider(without_override, getter=derived)
    assert literal.calls[0]["url"] == AIMLAPI_URL
    assert derived.calls[0]["url"] == "https://api.aimlapi.com/v1/models"
    assert derived.calls[0]["headers"] == literal.calls[0]["headers"]
    import config_loader
    wiring = config_loader.build_gateway_wiring(
        {"aimlapi": config})
    assert wiring["aimlapi"]["chat_completions_url"] == (
        "https://api.aimlapi.com/v1/chat/completions")


def test_aimlapi_non_chat_variable_and_paid_rows_are_empty_successes():
    """Valid nonqualifying classes are observations, never fetch errors."""
    non_chat = _aimlapi_future_row("synthetic/responses-row")
    non_chat["type"] = "openai/responses"
    variable = _aimlapi_variable_row()
    paid = _aimlapi_future_row("synthetic/paid-row")
    paid["pricing"]["units"][0]["price"] = 2.5
    for body in (_aimlapi_body(non_chat, variable), _aimlapi_body(paid),
                 _aimlapi_body()):
        ids, meta = providers.fetch_provider(
            _aimlapi_config(ignored_slugs=[]),
            getter=_RecordingGetter((200, body, {})))
        assert ids == []
        assert meta["models"] == {}


def test_aimlapi_future_fixed_zero_chat_row_is_tracked_with_its_raw_record():
    row = _aimlapi_future_row()
    body = json.dumps({"object": "list", "data": [row]})
    ids, meta = providers.fetch_provider(
        _aimlapi_config(), getter=_RecordingGetter((200, body, {})))
    assert ids == ["vendor/future-fixed-zero"]
    stored = meta["models"]["vendor/future-fixed-zero"]
    assert stored == row
    assert stored is not row, "no aliasing of the parsed source"
    assert "pricing" in stored, "the raw row, not an ephemeral view"
    assert "name" not in stored, "info stays nested, nothing is lifted"
    assert "upstream" not in stored and "_upstream" not in stored


def test_aimlapi_ignored_id_drops_after_detection():
    row = _aimlapi_future_row()
    body = json.dumps({"object": "list", "data": [row]})
    ids, meta = providers.fetch_provider(
        _aimlapi_config(ignored_slugs=["vendor/future-fixed-zero"]),
        getter=_RecordingGetter((200, body, {})))
    assert ids == [] and meta["models"] == {}


def test_aimlapi_passive_ratelimit_headers_are_captured():
    headers = {"X-RateLimit-Remaining": "12", "Content-Type": "application/json"}
    _ids, meta = providers.fetch_provider(
        _aimlapi_config(),
        getter=_RecordingGetter((200, _aimlapi_body(_aimlapi_future_row()),
                                 headers)))
    assert meta["ratelimit"] == {"X-RateLimit-Remaining": "12"}


def test_aimlapi_success_false_html_and_missing_pricing_are_fetch_errors():
    missing_pricing = (
        '{"object":"list","data":[{"id":"vendor/x",'
        '"type":"openai/chat-completions","info":{"name":"X"}}]}')
    for body in ('{"object":"list","success":false,"data":[]}',
                 "<html><body>upstream maintenance</body></html>",
                 missing_pricing):
        getter = _RecordingGetter((200, body, {}))
        with pytest.raises(FetchError):
            providers.fetch_provider(_aimlapi_config(), getter=getter)
        assert len(getter.calls) == 1, "the body, not the request, is the fault"


@pytest.mark.parametrize("order", [(0, 0.5), (0.5, 0)])
def test_aimlapi_conflicting_duplicates_are_fetch_errors_in_both_orders(order):
    rows = [('{"id":"dup/x","type":"openai/chat-completions",'
             '"pricing":{"kind":"fixed","variable":false,"thresholds":[],'
             '"dimensions":[],"variants":[],"units":[{"name":"token",'
             '"price":%s,"per":1000000,"variable":false}]}}' % price)
            for price in order]
    body = '{"object":"list","data":[%s]}' % ",".join(rows)
    getter = _RecordingGetter((200, body, {}))
    with pytest.raises(FetchError, match="duplicate"):
        providers.fetch_provider(_aimlapi_config(), getter=getter)
    assert len(getter.calls) == 1


@pytest.mark.parametrize("row", [
    '{"id":"bad/x","pricing":{"kind":"fixed","variable":false}}',
    '{"id":"bad/x","type":null,"pricing":{"kind":"fixed"}}',
    '{"id":"bad/x","type":"openai/chat-completions","pricing":{"kind":"fixed",'
    '"variable":false,"thresholds":[],"dimensions":[],"variants":[],'
    '"units":[{"name":"token","price":"0","per":1000000,"variable":false}]}}',
])
def test_aimlapi_malformed_required_row_is_a_fetch_error(row):
    body = '{"object":"list","data":[%s]}' % row
    with pytest.raises(FetchError, match="catalogue"):
        providers.fetch_provider(
            _aimlapi_config(), getter=_RecordingGetter((200, body, {})))


def test_aimlapi_valid_empty_and_paid_only_feeds_stay_successes():
    paid_only = (
        '{"object":"list","data":[{"id":"paid/model",'
        '"type":"openai/chat-completions","pricing":{"kind":"fixed",'
        '"variable":false,"thresholds":[],"dimensions":[],"variants":[],'
        '"units":[{"name":"token","price":2.5,"per":1000000,'
        '"variable":false}]}}]}')
    for body in ('{"object":"list","data":[]}', paid_only):
        ids, meta = providers.fetch_provider(
            _aimlapi_config(), getter=_RecordingGetter((200, body, {})))
        assert ids == [] and meta["models"] == {}
