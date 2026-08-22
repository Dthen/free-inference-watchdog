"""Tests for providers.py — six fetchers against canned responses.

Fetchers take an injectable `getter` returning (status:int, body:str, headers:dict)
and raising providers.FetchError on transport failure. No network in tests.
"""

import json

import pytest

import providers
from providers import FetchError, is_free


# ---------- shared helpers ----------

def ok(body, headers=None):
    return (200, body, headers or {})


def fake_getter(resps):
    """resps: {url_substring: (status, body, headers)} — first matching key wins."""
    def get(url, headers=None, timeout=15):
        for frag, resp in resps.items():
            if frag in url:
                return resp
        raise FetchError(f"no fixture for {url}")
    return get


# ---------- is_free ----------

def test_is_free_string_zero():
    assert is_free({"prompt": "0", "completion": "0"}) is True


def test_is_free_int_zero():
    assert is_free({"prompt": 0, "completion": 0}) is True


def test_is_free_priced():
    assert is_free({"prompt": "0.0000001", "completion": "0.0000002"}) is False


def test_is_free_kilo_unknown_sentinel():
    assert is_free({"prompt": "-1", "completion": "-1"}) is False


def test_is_free_missing_or_malformed():
    assert is_free({}) is False
    assert is_free({"prompt": None, "completion": None}) is False
    assert is_free("garbage") is False


# ---------- nous ----------

NOUS_MODELS = {
    "data": [
        {"id": "stealth/ox-alpha", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "paid/model", "pricing": {"prompt": "0.00001", "completion": "0.00002"}},
        {"id": "nopricing/model"},
        {"id": "weird/model", "pricing": {"prompt": None, "completion": None}},
    ]
}


def test_fetch_nous_filters_to_free_and_captures_headers():
    hdrs = {"x-ratelimit-remaining-requests": "2099"}
    g = fake_getter({"/v1/models": ok(json.dumps(NOUS_MODELS), hdrs)})
    ids, meta = providers._fetch_nous(getter=g, auth={"token": "t", "base": "https://x/v1"})
    assert ids == ["stealth/ox-alpha"]
    assert meta["ratelimit"] == {"x-ratelimit-remaining-requests": "2099"}


def test_fetch_nous_uses_correct_path_and_auth():
    seen = {}

    def g(url, headers=None, timeout=15):
        seen.update(url=url, headers=headers or {})
        return ok(json.dumps(NOUS_MODELS))

    providers._fetch_nous(getter=g, auth={"token": "tok123", "base": "https://x/v1"})
    assert seen["url"] == "https://x/v1/models"
    assert seen["headers"]["Authorization"] == "Bearer tok123"
    assert "User-Agent" in seen["headers"]


def test_fetch_nous_non_200_raises():
    def g(url, headers=None, timeout=15):
        return (403, "Forbidden", {})

    with pytest.raises(FetchError):
        providers._fetch_nous(getter=g, auth={"token": "t", "base": "https://x/v1"})


# ---------- openrouter ----------

OR_MODELS = {
    "data": [
        {"id": "a/free:free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "b/paid", "pricing": {"prompt": "0.0000001", "completion": "0.0000002"}},
    ]
}


def test_fetch_openrouter():
    g = fake_getter({"openrouter.ai": ok(json.dumps(OR_MODELS))})
    ids, _ = providers._fetch_openrouter(getter=g)
    assert ids == ["a/free:free"]


# ---------- zen ----------

def test_fetch_zen_bare_ids_verbatim():
    g = fake_getter({"/zen/v1/models": ok(json.dumps(["zz-model", "aa-model"]))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == ["aa-model", "zz-model"]  # sorted for stable diffs, values untouched


# ---------- kilo ----------

KILO_MODELS = {
    "data": [
        {"id": "kilo-auto/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "unknown/-1", "pricing": {"prompt": "-1", "completion": "-1"}},
        {"id": "stepfun/free:free", "pricing": {"prompt": "0", "completion": "0"}},
    ]
}


def test_fetch_kilo_sentinel_excluded():
    g = fake_getter({"api.kilo.ai": ok(json.dumps(KILO_MODELS))})
    ids, _ = providers._fetch_kilo(getter=g, key="k")
    assert ids == ["kilo-auto/free", "stepfun/free:free"]


# ---------- ollama ----------

OLLAMA_MODELS = {"data": [{"id": "gpt-oss:20b"}, {"id": "llama3"}]}


def test_fetch_ollama_passthrough():
    g = fake_getter({"ollama.com": ok(json.dumps(OLLAMA_MODELS))})
    ids, _ = providers._fetch_ollama(getter=g, key="k")
    assert ids == ["gpt-oss:20b", "llama3"]


# ---------- cline (docs change-detector) ----------

CLINE_PAGE_A = (
    "# Free Models\n\nLook for models tagged FREE.\n"
    "| Free experimentation | `minimax/minimax-m2.5` |\n"
    "Run `provider/name` locally if you like.\n"
)
CLINE_PAGE_B = "Examples:\n\n* `anthropic/claude-sonnet-4-6` - Claude\n* `minimax/minimax-m2.5` - dup\n"


def test_extract_cline_ids_accepts_mixed_case_ids():
    # R2-3: real-world IDs arrive mixed-case (Qwen/Qwen3-32B) — accept
    # uppercase on BOTH sides of '/', keep lowercase behaviour unchanged.
    md = "Try `Qwen/Qwen3-32B` or `Meta-Llama/Llama-3.1:70B`; skip `Not_An/ID!`.\n"
    ids = providers._extract_cline_ids(md)
    assert ids == ["Meta-Llama/Llama-3.1:70B", "Qwen/Qwen3-32B"]


def test_extract_cline_ids_backticked_only():
    ids = providers._extract_cline_ids(CLINE_PAGE_A)
    assert ids == ["minimax/minimax-m2.5", "provider/name"]


def test_fetch_cline_union_dedupe_sorted():
    def g(url, headers=None, timeout=15):
        if "free-models" in url:
            return ok(CLINE_PAGE_A)
        return ok(CLINE_PAGE_B)

    ids, _ = providers._fetch_cline(getter=g)
    assert ids == [
        "anthropic/claude-sonnet-4-6",
        "minimax/minimax-m2.5",
        "provider/name",
    ]


def test_fetch_cline_empty_parse_raises():
    def g(url, headers=None, timeout=15):
        return ok("<html>error page</html>")

    with pytest.raises(FetchError):
        providers._fetch_cline(getter=g)


def test_registry_has_six_providers():
    assert set(providers.PROVIDERS) == {
        "nous", "openrouter", "zen", "kilo", "ollama", "cline"
    }
