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

ZEN_PAID_AND_FREE = {
    "data": [
        {"id": "claude-opus-5"},                       # paid, no marker -> OUT
        {"id": "gpt-5.4-pro"},                         # paid -> OUT
        {"id": "deepseek-v4-flash-free"},              # marker -> IN
        {"id": "x-preview-f-free"},                    # Ox Alpha -> IN
        {"id": "big-pickle"},                          # stealth allowlist -> IN
        {"id": "nemotron-3-ultra-free"},               # marker -> IN
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
    pinning zen=[] forever. Dicts yield their id; strings/ints stay verbatim;
    missing/empty/null id => skipped. Still bare-ID diffing (decision #3) —
    we extract the id field, no alias mapping.
    Free-only filter (decision 2026-08-25): extracted ids must ALSO carry the
    'free' marker — the unmarked int 42 still exercises str-coercion through
    _extract_ids but is correctly filtered out of the roster."""
    body = json.dumps([
        {"id": "claude-fable-5-free", "object": "model", "owned_by": "opencode"},
        "bare-string-model-free",
        {"object": "model"},             # missing id -> skipped
        {"id": "", "object": "model"},   # empty id -> skipped
        {"id": None},                    # null id -> skipped
        42,                              # int coerced, then filtered (no marker)
    ])
    g = fake_getter({"/zen/v1/models": ok(body)})
    ids, _ = providers._fetch_zen(getter=g)
    assert ids == ["bare-string-model-free", "claude-fable-5-free"]


def test_fetch_zen_keeps_only_free_marked_and_allowlisted():
    g = fake_getter({"/zen/v1/models": ok(json.dumps(ZEN_PAID_AND_FREE))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == ["big-pickle", "deepseek-v4-flash-free",
                   "nemotron-3-ultra-free", "x-preview-f-free"]


def test_fetch_zen_empty_after_filter_is_real_data():
    """Healthy 200 whose every model is paid -> [] roster (never an error)."""
    g = fake_getter({"/zen/v1/models": ok(json.dumps(
        {"data": [{"id": "claude-opus-5"}]}))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == []


def test_fetch_zen_case_insensitive_and_position_independent_marker():
    """'free' may appear anywhere, any case; allowlist match is case-insensitive."""
    g = fake_getter({"/zen/v1/models": ok(json.dumps({"data": [
        {"id": "BIG-PICKLE"},            # allowlist, uppercase -> IN
        {"id": "Model-FREE"},            # suffix marker, uppercase -> IN
        {"id": "free-experiment-tier"},  # PREFIX marker -> IN
        {"id": "model-free-preview"},    # middle marker -> IN
        {"id": "freetier"},              # substring but NOT the marker word boundary we track -> IN (rule is literal substring)
        {"id": "claude-opus-5"},         # no marker -> OUT
    ]}))})
    ids, _ = providers._fetch_zen(getter=g, key="k")
    assert ids == ["BIG-PICKLE", "Model-FREE", "free-experiment-tier",
                   "freetier", "model-free-preview"]


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
    and FATALs the whole tick, repeating every tick. Mirrors ollama's
    existing str(it["id"]) wrap."""
    g = fake_getter({url_frag: ok(json.dumps(S5_MIXED_IDS))})
    ids, _ = providers.PROVIDERS[name](getter=g, **kw)
    assert ids == ["1", "a-model"]


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
    # F-R2-1: `provider/name` is a docs EXAMPLE span, not a model — rejected.
    ids = providers._extract_cline_ids(CLINE_PAGE_A)
    assert ids == ["minimax/minimax-m2.5"]


def test_extract_cline_ids_rejects_placeholder_spans():
    """F-R2-1: docs pages carry placeholder/example spans that match the ID
    shape (`provider/model-name` verified live on docs.cline.bot/api/models.md).
    Ingesting them poisons the roster and later fires a false 🔴 removal."""
    md = ("Real: `minimax/minimax-m2.5`. Placeholders: `provider/model-name`, "
          "`provider/name`, `example/model`, `your/api-key`, "
          "case-variant `Provider/Model-Name`.\n")
    ids = providers._extract_cline_ids(md)
    assert ids == ["minimax/minimax-m2.5"]


def test_extract_cline_ids_rejects_doc_file_spans():
    """F5: backticked doc-file paths (`.md`/`.html`/`.pdf`) match the ID
    shape but are links, not model IDs — they must never be ingested."""
    md = ("See `getting-started/free-models.md`, `api/models.md`, "
          "`guide/page.html`, `whitepaper.pdf`, but real: `Qwen/Qwen3-32B`.\n")
    ids = providers._extract_cline_ids(md)
    assert ids == ["Qwen/Qwen3-32B"]


def test_fetch_cline_union_dedupe_sorted():
    def g(url, headers=None, timeout=15):
        if "free-models" in url:
            return ok(CLINE_PAGE_A)
        return ok(CLINE_PAGE_B)

    ids, _ = providers._fetch_cline(getter=g)
    assert ids == [
        "anthropic/claude-sonnet-4-6",
        "minimax/minimax-m2.5",
    ]


def test_fetch_cline_live_page_placeholder_span_rejected():
    """F-R2-1 regression: the LIVE docs.cline.bot/api/models.md page carries
    the example span `provider/model-name` (verified HTTP 200 on 2026-08-22,
    where it had already leaked into state/roster.json). A page shaped like
    that must yield only real IDs — never the placeholder."""
    live_page = (
        "# Models\n\n"
        "Configure your provider:\n\n"
        "```json\n"
        '{"apiModelId": "provider/model-name"}\n'
        "```\n\n"
        "For example: `provider/model-name` or `provider/name`.\n\n"
        "| Model | ID |\n|---|---|\n"
        "| Claude Sonnet | `anthropic/claude-sonnet-4-6` |\n"
        "| MiniMax M2.5 | `minimax/minimax-m2.5` |\n"
    )

    def g(url, headers=None, timeout=15):
        return ok(live_page)

    ids, _ = providers._fetch_cline(getter=g)
    assert ids == ["anthropic/claude-sonnet-4-6", "minimax/minimax-m2.5"]
    assert "provider/model-name" not in ids
    assert not any(i.lower().startswith("provider/") for i in ids)


def test_fetch_cline_empty_parse_raises():
    """CHANGE 2/3 contract: the DOCS fallback (and the endpoint) may only
    fail LOUD — an outage is a sticky FetchError, never a mass removal. The
    five API fetchers have NO such empty-parse rule (empty 200 = real data,
    pinned by test_fetch_empty_200_roster_is_real_data_not_error)."""
    def g(url, headers=None, timeout=15):
        return ok("<html>error page</html>")

    with pytest.raises(FetchError):
        providers._fetch_cline(getter=g)


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
            {"id": "anthropic/claude-sonnet-4-6", "name": "Claude Sonnet",
             "description": "flagship", "tags": ["paid"]},
            {"id": "minimax/minimax-m2.5", "name": "MiniMax M2.5",
             "description": "", "tags": ["recommended"]},
        ],
        "free": [
            {"id": "qwen/qwen3-coder", "name": "Qwen3 Coder",
             "description": "free tier", "tags": ["free"]},
            {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat",
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
    assert ids == ["deepseek/deepseek-chat", "qwen/qwen3-coder"]
    assert seen["url"] == providers.CLINE_ENDPOINT
    assert "Authorization" not in seen["headers"]          # public, NO auth
    assert "User-Agent" in seen["headers"]                 # UA always sent


def test_fetch_cline_endpoint_empty_free_list_is_real_data_not_error():
    """CHANGE 2+3 together: a healthy endpoint 200 with an EMPTY free[] is
    real data (all free tiers deleted) — returns [] honestly, never raises,
    never falls back to docs."""
    body = json.dumps({"recommended": [], "free": [], "clinePass": [],
                       "clineCloud": []})
    g = fake_getter({"/api/v1/ai/cline/recommended-models": ok(body)})
    ids, _ = providers._fetch_cline(getter=g)
    assert ids == []


def test_fetch_cline_docs_fallback_when_endpoint_fails():
    """CHANGE 3: when the endpoint RAISES FetchError / HTTP-fails, the two
    docs pages (free-models.md + api/models.md) become the SECONDARY fallback;
    their extracted IDs merge as the fallback roster that ticks."""
    def g(url, headers=None, timeout=15):
        if "api.cline.bot" in url:
            raise FetchError("HTTP 503 from " + url)
        if "free-models" in url:
            return ok(CLINE_PAGE_A)
        return ok(CLINE_PAGE_B)

    ids, _ = providers._fetch_cline(getter=g)
    assert ids == [
        "anthropic/claude-sonnet-4-6",
        "minimax/minimax-m2.5",
    ]


def test_fetch_cline_http_error_endpoint_uses_docs_fallback():
    """CHANGE 3: an endpoint non-200 (not just a transport raise) must also
    fall back to the docs pages."""
    def g(url, headers=None, timeout=15):
        if "api.cline.bot" in url:
            return (500, "server error", {})
        return ok(CLINE_PAGE_A + CLINE_PAGE_B)

    ids, _ = providers._fetch_cline(getter=g)
    assert ids == [
        "anthropic/claude-sonnet-4-6",
        "minimax/minimax-m2.5",
    ]


def test_fetch_cline_both_sources_fail_is_loud_fetcherror():
    """CHANGE 3: endpoint AND docs both failing is an honest loud failure —
    sticky FetchError (carry-forward), never a silent empty roster that would
    fire a mass 🔴 removal alert."""
    def g(url, headers=None, timeout=15):
        raise FetchError(f"dead: {url}")

    with pytest.raises(FetchError):
        providers._fetch_cline(getter=g)


def test_fetch_cline_both_sources_fail_non_200_is_loud_fetcherror():
    def g(url, headers=None, timeout=15):
        return (404, "not found", {})

    with pytest.raises(FetchError):
        providers._fetch_cline(getter=g)


def test_fetch_cline_endpoint_ok_docs_never_polled():
    """CHANGE 3: docs pages are FALLBACK ONLY — a healthy endpoint means the
    docs watcher is not consulted at all."""
    calls = []

    def g(url, headers=None, timeout=15):
        calls.append(url)
        return ok(json.dumps({"free": [{"id": "only/free-model"}]}))

    ids, _ = providers._fetch_cline(getter=g)
    assert ids == ["only/free-model"]
    assert len(calls) == 1 and "api.cline.bot" in calls[0]


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


def test_fetch_ollama_accepts_string_and_dict_items():
    """CHANGE 1: ollama (no pricing field) tolerates string items too."""
    g = fake_getter({"ollama.com": ok(json.dumps(SHAPE_MIXED_ITEMS))})
    ids, _ = providers._fetch_ollama(getter=g, key="k")
    assert ids == ["bare/string-model", "dict-only/model", "mixed/second"]


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
        assert ids == ["kilo-auto/free", "stepfun/free:free"]


# ---------- CHANGE 2 (fix-round-9): empty 200 roster is REAL data ----------

EMPTY_PAYLOADS = ['{"data": []}', "[]", '{"object": "list", "data": []}']

EMPTY_CASES = [
    ("nous", "/v1/models", {"auth": {"token": "t", "base": "https://x/v1"}}),
    ("openrouter", "openrouter.ai", {}),
    ("zen", "/zen/v1/models", {"key": "k"}),
    ("kilo", "api.kilo.ai", {"key": "k"}),
    ("ollama", "ollama.com", {"key": "k"}),
]


@pytest.mark.parametrize("payload", EMPTY_PAYLOADS)
@pytest.mark.parametrize("name,url_frag,kw", EMPTY_CASES)
def test_fetch_empty_200_roster_is_real_data_not_error(name, url_frag, kw,
                                                       payload):
    """CHANGE 2: an empty result from a HEALTHY 200 is real data (all free
    tiers deleted) — every API fetcher must return ([], meta), NEVER raise,
    so it can diff honestly into alerts downstream. No special-casing may be
    (re-)introduced on the five API fetchers."""
    g = fake_getter({url_frag: ok(payload)})
    ids, _meta = providers.PROVIDERS[name](getter=g, **kw)
    assert ids == []


def test_registry_has_six_providers():
    assert set(providers.PROVIDERS) == {
        "nous", "openrouter", "zen", "kilo", "ollama", "cline"
    }
