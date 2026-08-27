"""Tests for providers.py — five fetchers against canned responses.

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


# ---------- _load_nous_auth (F3: malformed shape => FetchError, never AttributeError) ----------

def _write_auth(tmp_path, nous_value):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"providers": {"nous": nous_value}}),
                    encoding="utf-8")
    return str(auth)


def test_load_nous_auth_happy_path_strips_trailing_slash(tmp_path):
    path = _write_auth(tmp_path, {"access_token": "tok",
                                  "inference_base_url": "https://x.y/v1/"})
    auth = providers._load_nous_auth(path)
    assert auth == {"token": "tok", "base": "https://x.y/v1"}


def test_load_nous_auth_null_base_is_fetch_error(tmp_path):
    path = _write_auth(tmp_path, {"access_token": "tok",
                                  "inference_base_url": None})
    with pytest.raises(FetchError, match="cannot load nous auth"):
        providers._load_nous_auth(path)


def test_load_nous_auth_null_token_is_fetch_error(tmp_path):
    path = _write_auth(tmp_path, {"access_token": None,
                                  "inference_base_url": "https://x.y/v1"})
    with pytest.raises(FetchError, match="cannot load nous auth"):
        providers._load_nous_auth(path)


def test_load_nous_auth_empty_or_nonstring_base_is_fetch_error(tmp_path):
    for bad in ("", 42):
        path = _write_auth(tmp_path, {"access_token": "tok",
                                      "inference_base_url": bad})
        with pytest.raises(FetchError, match="cannot load nous auth"):
            providers._load_nous_auth(path)


# ---------- F1: RecursionError meets the FetchError contract ----------

DEEP_NEST_JSON = "[" * 120000 + "]" * 120000


@pytest.mark.parametrize("site", ["parse_model_list", "cline_endpoint",
                                  "nous_auth"])
def test_deeply_nested_json_raises_fetcherror_not_recursionerror(site,
                                                                 tmp_path):
    """F1: CPython's json decoder raises RecursionError on deeply nested
    documents ('['*N + ']'*N). RecursionError is NOT a FetchError, so it
    escapes build_fetch_all's `except providers.FetchError`, reaches
    run_tick's fatal handler and exits 2 EVERY tick until the provider is
    fixed. All three parse sites (_parse_model_list, _fetch_cline,
    _load_nous_auth) must convert it. pytest.raises(FetchError) doubles as
    the nothing-else-escapes assertion — a leaking RecursionError fails."""
    if site == "parse_model_list":
        with pytest.raises(FetchError):
            providers._parse_model_list(DEEP_NEST_JSON)
    elif site == "cline_endpoint":
        def g(url, headers=None, timeout=15):
            return (200, DEEP_NEST_JSON, {})

        with pytest.raises(FetchError):
            providers._fetch_cline(g)
    else:
        p = tmp_path / "auth.json"
        p.write_text('{"providers": {"nous": ' + DEEP_NEST_JSON + '}}',
                     encoding="utf-8")
        with pytest.raises(FetchError):
            providers._load_nous_auth(str(p))


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
        {"id": "vendor-z/zero-priced-model", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "paid/model", "pricing": {"prompt": "0.00001", "completion": "0.00002"}},
        {"id": "nopricing/model"},
        {"id": "weird/model", "pricing": {"prompt": None, "completion": None}},
    ]
}


def test_fetch_nous_filters_to_free_and_captures_headers():
    hdrs = {"x-ratelimit-remaining-requests": "2099"}
    g = fake_getter({"/v1/models": ok(json.dumps(NOUS_MODELS), hdrs)})
    ids, meta = providers._fetch_nous(getter=g, auth={"token": "t", "base": "https://x/v1"})
    assert ids == ["vendor-z/zero-priced-model"]
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

ZEN_PAID_AND_FREE = {
    "data": [
        {"id": "vendor-a/paid-model-1"},                       # paid, no marker -> OUT
        {"id": "vendor-b/paid-model-2"},                         # paid -> OUT
        {"id": "vendor-a/model-1-free"},              # marker -> IN
        {"id": "vendor-x/preview-free"},                    # marker -> IN
        {"id": "vendor-x/no-marker-model"},                          # no marker -> OUT
        {"id": "vendor-b/model-2-free"},               # marker -> IN
    ]
}


def test_fetch_zen_bare_ids_verbatim():
    # Free-only filter (decision 2026-08-25): fixture ids carry the 'free'
    # marker; bare-string ITEMS are still extracted verbatim and sorted.
    g = fake_getter({"/zen/v1/models": ok(json.dumps(
        ["zz-free-model", "aa-free-model"]))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == ["aa-free-model", "zz-free-model"]  # sorted for stable diffs, values untouched


def test_fetch_zen_model_dict_objects_extract_id():
    """Item 5 (live bug): zen's /v1/models returns MODEL OBJECTS
    ({'id': ..., 'object': 'model', ...}), NOT bare strings — verified live
    (64 items). The old isinstance((str, int)) filter dropped all of them,
    pinning zen=[] forever. Dicts yield their id; strings stay verbatim.
    Free-only filter (decision 2026-08-25): surviving ids must ALSO carry
    the 'free' marker or sit on the stealth allowlist. Non-str ids
    (int/dict/list/None) are dropped outright by _fetch_zen's single-point
    type gate — never coerced; _extract_ids remains other fetchers'
    coerce-contract helper. Still bare-ID diffing (decision #3) — we extract
    the id field, no alias mapping."""
    body = json.dumps([
        {"id": "vendor-c/model-3-free", "object": "model", "owned_by": "opencode"},
        "bare-string-model-free",
        {"object": "model"},             # missing id (yields None) -> OUT: type gate drops non-str
        {"id": "", "object": "model"},   # empty str id -> OUT: passes type gate, fails free-marker filter
        {"id": None},                    # null id -> OUT: type gate drops non-str
        42,                              # int item -> OUT: type gate drops non-str, never coerced
    ])
    g = fake_getter({"/zen/v1/models": ok(body)})
    ids, _ = providers._fetch_zen(getter=g)
    assert ids == ["bare-string-model-free", "vendor-c/model-3-free"]
    # Each rejection mechanism independently visible:
    assert "" not in ids            # empty str: fails the free-marker filter
    assert "None" not in ids        # null/missing id: dropped by type gate, never repr-coerced
    assert "42" not in ids          # int: dropped by type gate, never coerced


def test_fetch_zen_keeps_only_free_marked():
    g = fake_getter({"/zen/v1/models": ok(json.dumps(ZEN_PAID_AND_FREE))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == ["vendor-a/model-1-free",
                   "vendor-b/model-2-free", "vendor-x/preview-free"]


def test_fetch_zen_empty_after_filter_is_real_data():
    """Healthy 200 whose every model is paid -> [] roster (never an error)."""
    g = fake_getter({"/zen/v1/models": ok(json.dumps(
        {"data": [{"id": "vendor-a/paid-model-1"}]}))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == []


def test_fetch_zen_case_insensitive_and_position_independent_marker():
    """'free' may appear anywhere, any case; no marker means not tracked."""
    g = fake_getter({"/zen/v1/models": ok(json.dumps({"data": [
        {"id": "BIG-PICKLE"},            # no free marker -> OUT
        {"id": "Model-FREE"},            # suffix marker, uppercase -> IN
        {"id": "free-experiment-tier"},  # PREFIX marker -> IN
        {"id": "model-free-preview"},    # middle marker -> IN
        {"id": "freetier"},              # substring anywhere counts -> IN (rule is literal substring)
        {"id": "vendor-a/paid-model-1"},         # no marker -> OUT
    ]}))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == ["Model-FREE", "free-experiment-tier",
                   "freetier", "model-free-preview"]


def test_fetch_zen_dedupes_and_rejects_non_string_ids():
    """Dedupe is intentional (duplicate ids must not double-fire alerts) and
    the single-point gate covers BOTH item shapes: non-str ids AND non-str
    bare items are rejected outright, never repr-coerced into the roster."""
    g = fake_getter({"/zen/v1/models": ok(json.dumps({"data": [
        {"id": "dup-free-model"},
        {"id": "dup-free-model"},            # duplicate -> appears ONCE
        {"id": {"note": "free tier"}},       # dict id -> OUT (no repr coercion)
        {"id": ["free"]},                    # list id -> OUT
        {"id": 42},                          # int id -> OUT
        {"id": True},                        # bool id -> OUT
        {"id": None},                        # null id -> OUT
        {"id": ""},                          # empty str id -> OUT
        ["free"],                            # bare ARRAY item -> OUT (no repr coercion)
        {"note": "free tier"},               # bare dict item WITHOUT id -> OUT
        "bare-free-string",                  # bare string WITH marker -> IN verbatim
        "plain-unmarked-id",                 # bare string WITHOUT marker -> OUT (same predicate as dicts)
    ]}))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == ["bare-free-string", "dup-free-model"]


# ---------- kilo ----------

KILO_MODELS = {
    "data": [
        {"id": "kilo-auto/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "unknown/-1", "pricing": {"prompt": "-1", "completion": "-1"}},
        {"id": "vendor-g/free:free", "pricing": {"prompt": "0", "completion": "0"}},
    ]
}


def test_fetch_kilo_sentinel_excluded():
    g = fake_getter({"api.kilo.ai": ok(json.dumps(KILO_MODELS))})
    ids, _ = providers._fetch_kilo(getter=g, key="k")
    assert ids == ["kilo-auto/free", "vendor-g/free:free"]


# ---------- S5-1: mixed str/int ids must coerce to str, never FATAL ----------

S5_MIXED_IDS = {
    "data": [
        {"id": 1, "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "a-model", "pricing": {"prompt": "0", "completion": "0"}},
    ]
}


@pytest.mark.parametrize("name,url_frag,kw", [
    ("nous", "/v1/models", {"auth": {"token": "t", "base": "https://x/v1"}}),
    ("openrouter", "openrouter.ai", {}),
    ("kilo", "api.kilo.ai", {"key": "k"}),
])
def test_fetch_mixed_int_and_str_ids_coerced(name, url_frag, kw):
    """S5-1: a provider drifting to MIXED str/int model ids must yield
    coerced, sorted STRINGS — sorting raw values raises TypeError, which
    escapes build_fetch_all's FetchError-only catch (inference_watchdog.py)
    and FATALs the whole tick, repeating every tick. Mirrors the str(id)
    coercion nous/openrouter/kilo have applied since fix-round S5-1 (which
    unified the then-mixed wrapping). Cline is excluded because its endpoint
    path shares this helper's coercion by construction — it does not fit
    this fixture's {"data": [...]} shape."""
    g = fake_getter({url_frag: ok(json.dumps(S5_MIXED_IDS))})
    ids, _ = providers.PROVIDERS[name](getter=g, **kw)
    assert ids == ["1", "a-model"]


# ---------- F4: dict-id gate is None-check, not truthiness ----------

ZERO_ID_ITEMS = {
    "data": [
        {"id": 0, "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "normal/free-model", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": None, "pricing": {"prompt": "0", "completion": "0"}},
    ]
}


@pytest.mark.parametrize("name,url_frag,kw", [
    ("nous", "/v1/models", {"auth": {"token": "t", "base": "https://x/v1"}}),
    ("openrouter", "openrouter.ai", {}),
    ("kilo", "api.kilo.ai", {"key": "k"}),
])
def test_fetch_zero_id_dict_item_yields_string_zero(name, url_frag, kw):
    """F4: the dict-item gate is `it.get('id') is not None`, never truthiness
    — numeric id 0 is a REAL id and coerces to "0" exactly like S5-1 pins
    {"id":1}->"1". Null ids stay dropped (no 'None' leak the other way)."""
    g = fake_getter({url_frag: ok(json.dumps(ZERO_ID_ITEMS))})
    ids, _ = providers.PROVIDERS[name](getter=g, **kw)
    assert ids == ["0", "normal/free-model"]


def test_cline_endpoint_zero_id_dict_item_yields_string_zero():
    """Fix-round-2 S3: _fetch_cline's keep-lambda still used bare
    truthiness (`lambda it: it.get("id")`), so a numeric id 0 in free[] was
    dropped — violating its own docstring and every sibling fetcher (F4).
    Same gate as the others: `is not None`, with None-ids never leaking."""
    body = json.dumps({
        "recommended": [],
        "free": [{"id": 0}, {"id": "real/free"}],
        "clinePass": [], "clineCloud": [],
    })
    g = fake_getter({"/api/v1/ai/cline/recommended-models": ok(body)})
    ids, _ = providers._fetch_cline(getter=g)
    assert ids == ["0", "real/free"]


# ---------- cline (endpoint-only) ----------


def test_fetch_cline_500_raises_fetcherror_no_docs_fallback():
    """Task 1 (Dthen 2026-08-26): Cline is endpoint-only. A 500 from the
    endpoint must raise FetchError AND must NOT request any docs.cline.bot
    URL — the docs-watcher fallback is deleted entirely."""
    calls = []

    def g(url, headers=None, timeout=15):
        calls.append(url)
        if providers.CLINE_ENDPOINT in url:
            return (500, "server error", {})
        raise FetchError(f"no docs fallback expected, saw: {url}")

    with pytest.raises(FetchError):
        providers._fetch_cline(getter=g)
    assert all(providers.CLINE_ENDPOINT in u for u in calls)
    assert len(calls) == 1  # endpoint only, no docs page


def test_fetch_cline_endpoint_free_ids_extracted():
    """CHANGE 3: the NEW primary source is GET
    https://api.cline.bot/api/v1/ai/cline/recommended-models (public, NO auth
    header). Response JSON:
      {"recommended":[{id,name,description,tags[]}], "free":[...],
       "clinePass":[...], "clineCloud":[]}
    Extract ids from free[] ONLY (that's the free-roster we track) — items are
    dicts with an id field; recommended/clinePass/clineCloud NEVER leak in."""
    body = json.dumps({
        "recommended": [
            {"id": "vendor-c/paid-model-4", "name": "Claude Sonnet",
             "description": "flagship", "tags": ["paid"]},
            {"id": "vendor-e/paid-model-3", "name": "MiniMax M2.5",
             "description": "", "tags": ["recommended"]},
        ],
        "free": [
            {"id": "qwen/qwen3-coder", "name": "Qwen3 Coder",
             "description": "free tier", "tags": ["free"]},
            {"id": "vendor-a/paid-chat", "name": "DeepSeek Chat",
             "description": "free tier", "tags": ["free"]},
        ],
        "clinePass": [{"id": "pass/only-model", "name": "Pass",
                       "description": "", "tags": []}],
        "clineCloud": [],
    })
    seen = {}

    def g(url, headers=None, timeout=15):
        seen.update(url=url, headers=headers or {})
        return ok(body)

    ids, _ = providers._fetch_cline(getter=g)
    assert ids == ["qwen/qwen3-coder", "vendor-a/paid-chat"]
    assert seen["url"] == providers.CLINE_ENDPOINT
    assert "Authorization" not in seen["headers"]          # public, NO auth
    assert "User-Agent" in seen["headers"]                 # UA always sent


def test_fetch_cline_endpoint_empty_free_list_is_real_data_not_error():
    """Healthy endpoint 200 with an EMPTY free[] is real data (all free tiers
    deleted) — returns [] honestly, never raises."""
    body = json.dumps({"recommended": [], "free": [], "clinePass": [],
                       "clineCloud": []})
    g = fake_getter({"/api/v1/ai/cline/recommended-models": ok(body)})
    ids, _ = providers._fetch_cline(getter=g)
    assert ids == []


# ---------- CHANGE 1 (fix-round-9): shape-tolerant parsers ----------
# Real captured payload shapes: nous/openrouter/kilo ship DICT items
# {"id": ...}; zen ships MIXED bare strings + dicts. A provider drifting
# between shapes must never blank its own roster (zen's original live bug).
SHAPE_MIXED_ITEMS = {
    "data": [
        {"id": "dict-only/model", "pricing": {"prompt": "0", "completion": "0"}},
        "bare/string-model",
        {"id": "mixed/second", "pricing": {"prompt": "0", "completion": "0"}},
    ]
}


@pytest.mark.parametrize("name,url_frag,kw,expected_extra", [
    # dict-only real shape (nous/openrouter/kilo): strings now ALSO extracted
    ("nous", "/v1/models", {"auth": {"token": "t", "base": "https://x/v1"}},
     ["bare/string-model"]),
    ("openrouter", "openrouter.ai", {}, ["bare/string-model"]),
    ("kilo", "api.kilo.ai", {"key": "k"}, ["bare/string-model"]),
])
def test_fetch_accepts_string_and_dict_items(name, url_frag, kw, expected_extra):
    """CHANGE 1: every API roster fetcher extracts ids from BOTH plain string
    items AND dict items via it.get('id'). No behavior change for well-formed
    dict input — the free/pricing filter still applies to dicts."""
    g = fake_getter({url_frag: ok(json.dumps(SHAPE_MIXED_ITEMS))})
    ids, _ = providers.PROVIDERS[name](getter=g, **kw)
    assert ids == sorted(["dict-only/model", "mixed/second"] + expected_extra)


@pytest.mark.parametrize("name,url_frag,kw", [
    ("nous", "/v1/models", {"auth": {"token": "t", "base": "https://x/v1"}}),
    ("openrouter", "openrouter.ai", {}),
    ("kilo", "api.kilo.ai", {"key": "k"}),
])
def test_fetch_wellformed_dict_input_unchanged(name, url_frag, kw):
    """CHANGE 1 no-regression pin: well-formed dict payloads yield exactly the
    same ids as before (free-filtered, sorted) — shape tolerance adds string
    extraction without touching dict behavior."""
    g = fake_getter({url_frag: ok(json.dumps(OR_MODELS if name == "openrouter"
                                             else KILO_MODELS))})
    ids, _ = providers.PROVIDERS[name](getter=g, **kw)
    if name == "openrouter":
        assert ids == ["a/free:free"]
    else:
        assert ids == ["kilo-auto/free", "vendor-g/free:free"]


# ---------- CHANGE 2 (fix-round-9): empty 200 roster is REAL data ----------

EMPTY_PAYLOADS = ['{"data": []}', "[]", '{"object": "list", "data": []}']

EMPTY_CASES = [
    ("nous", "/v1/models", {"auth": {"token": "t", "base": "https://x/v1"}}),
    ("openrouter", "openrouter.ai", {}),
    ("zen", "/zen/v1/models", {"key": "k"}),
    ("kilo", "api.kilo.ai", {"key": "k"}),
]


@pytest.mark.parametrize("payload", EMPTY_PAYLOADS)
@pytest.mark.parametrize("name,url_frag,kw", EMPTY_CASES)
def test_fetch_empty_200_roster_is_real_data_not_error(name, url_frag, kw,
                                                       payload):
    """CHANGE 2: an empty result from a HEALTHY 200 is real data (all free
    tiers deleted) — every API fetcher must return ([], meta), NEVER raise,
    so it can diff honestly into alerts downstream. No special-casing may be
    (re-)introduced on the four API fetchers."""
    g = fake_getter({url_frag: ok(payload)})
    ids, _meta = providers.PROVIDERS[name](getter=g, **kw)
    assert ids == []


def test_registry_has_five_providers():
    assert set(providers.PROVIDERS) == {
        "nous", "openrouter", "zen", "kilo", "cline", "command_code"
    }


def test_gateway_wiring_keys_match_providers():
    """GATEWAY_WIRING is the single source of truth for gateway wiring and
    must stay pinned to PROVIDERS — same six keys, no drift."""
    assert set(providers.GATEWAY_WIRING) == set(providers.PROVIDERS)
