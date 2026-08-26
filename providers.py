"""Five provider fetchers for the Free Inference Watchdog. Stdlib only.

Contract: each fetcher returns (ids: list[str], meta: dict) with ids SORTED,
or raises FetchError. Transport is injectable via `getter(url, headers, timeout)
-> (status:int, body:str, headers:dict)` so tests run without network.
The real getter ALWAYS sends a User-Agent (Nous 403s bare urllib — probed).
"""

import json
import re
import urllib.error
import urllib.request

USER_AGENT = "free-inference-watchdog/1.0"
TIMEOUT_S = 15


class FetchError(Exception):
    """Any failure to obtain a usable roster from a provider."""


# ---------- shared helpers ----------

def is_free(pricing):
    """True iff prompt AND completion price are exactly zero.

    Type-safe by design (probe facts): prices arrive as STRINGS on nous/
    openrouter/kilo; kilo uses '-1' as unknown sentinel; absent/malformed
    pricing means NOT free (never KeyError, never false mass-removal).
    """
    if not isinstance(pricing, dict):
        return False
    try:
        prompt_zero = float(pricing.get("prompt", 1)) == 0.0
        completion_zero = float(pricing.get("completion", 1)) == 0.0
    except (TypeError, ValueError):
        return False
    return prompt_zero and completion_zero


def _default_getter(url, headers=None, timeout=TIMEOUT_S):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} from {url}") from exc
    except Exception as exc:  # URLError, timeouts, sockets
        raise FetchError(f"{type(exc).__name__} fetching {url}") from exc


def _headers(extra=None):
    """Every request carries UA regardless of getter injection (Nous 403s bare urllib)."""
    return {"User-Agent": USER_AGENT, **(extra or {})}


def _loads_or_fetcherror(text, ctx_msg):
    """json.loads under the FetchError contract (fix-round F1).

    Malformed bytes raise json.JSONDecodeError, and DEEPLY NESTED documents
    ('['*120000 + ']'*120000) raise RecursionError straight out of CPython's
    json scanner. Neither subclasses FetchError, so either escaping a fetcher
    would blow through build_fetch_all's `except providers.FetchError` into
    run_tick's fatal handler (exit 2, every tick). Both convert here."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise FetchError(ctx_msg) from exc


def _parse_model_list(body):
    """OpenAI-style {"data":[...]} or bare list -> list[dict|str]."""
    payload = _loads_or_fetcherror(body, "response was not valid JSON")
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise FetchError("unexpected models payload shape")
    return items


def _extract_ids(items, keep):
    """Shape-tolerant id extraction (fix-round-9 CHANGE 1).

    Real captured roster shapes: nous/openrouter/kilo ship dict items
    {"id": ...}; zen ships MIXED bare strings + dicts. Every API fetcher must
    accept BOTH: dicts yield their `id` field, plain strings/ints are kept
    verbatim (str'd). Whether a dict item survives at all is `keep(dict)`'s
    call — per-provider `id is not None` + pricing/free filter (F4: gated on
    None, never truthiness, so a numeric id 0 survives like {"id":1}->"1").
    Zen previously shared this helper but now intentionally diverges — see
    _fetch_zen's own type-gated extraction."""
    return [
        str(it.get("id")) if isinstance(it, dict) else str(it)
        for it in items
        if (keep(it) if isinstance(it, dict) else it)
    ]


def _require_ok(status, url):
    if status != 200:
        raise FetchError(f"HTTP {status} from {url}")


# ---------- nous ----------

def _load_nous_auth(auth_path="~/.hermes/auth.json"):
    """Named owner of auth.json parsing (plan round-1 finding #8).

    F3: a malformed VALUE (null/empty/non-string token or base) must surface
    as FetchError — same contract as every other fetch failure — never as an
    AttributeError escaping to a whole-tick FATAL.
    F1: a deeply nested document raises RecursionError inside the json
    decoder; it converts to FetchError here via _loads_or_fetcherror too."""
    import os
    path = os.path.expanduser(auth_path)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise FetchError(f"cannot load nous auth from {path}") from exc
    try:
        nous = _loads_or_fetcherror(
            text, f"cannot load nous auth from {path}")["providers"]["nous"]
        token, base = nous["access_token"], nous["inference_base_url"]
    except (KeyError, TypeError) as exc:
        raise FetchError(f"cannot load nous auth from {path}") from exc
    if not isinstance(token, str) or not token:
        raise FetchError(
            f"cannot load nous auth from {path}: access_token null/malformed")
    if not isinstance(base, str) or not base:
        raise FetchError(
            f"cannot load nous auth from {path}: inference_base_url null/malformed")
    return {"token": token, "base": base.rstrip("/")}


def _fetch_nous(getter=_default_getter, auth=None):
    """Probe fact: inference_base_url ALREADY ends in /v1 -> path is {base}/models."""
    auth = auth or {}
    url = f"{auth['base']}/models"
    status, body, hdrs = getter(
        url,
        headers=_headers({"Authorization": f"Bearer {auth['token']}"}),
        timeout=TIMEOUT_S,
    )
    _require_ok(status, url)
    ids = sorted(_extract_ids(
        _parse_model_list(body),
        keep=lambda it: (it.get("id") is not None
                         and is_free(it.get("pricing")))))
    ratelimit = {k: v for k, v in (hdrs or {}).items() if "ratelimit" in k.lower()}
    return ids, {"ratelimit": ratelimit}


# ---------- openrouter ----------

def _fetch_openrouter(getter=_default_getter):
    url = "https://openrouter.ai/api/v1/models"  # public, no auth
    status, body, _hdrs = getter(url, headers=_headers(), timeout=TIMEOUT_S)
    _require_ok(status, url)
    ids = sorted(_extract_ids(
        _parse_model_list(body),
        keep=lambda it: (it.get("id") is not None
                         and is_free(it.get("pricing")))))
    return ids, {}


# ---------- zen ----------

ZEN_URL = "https://opencode.ai/zen/v1/models"

# Zen ships NO pricing metadata (probed 2026-08-25: objects carry only
# id/object/created/owned_by). Free-roster rule: explicit "free" name marker
# ONLY — no alias map, no allowlist, no normalized-name matching. A new
# stealth arrival ships under whatever id the gateway assigns; if that id
# doesn't contain "free", it is not tracked.
# Deploy note: a persisted roster.json written BEFORE this filter holds the
# paid tiers; the first good tick without a manual `python3 inference_watchdog.py
# --init` rebaseline computes removals for every persisted unmarked id (dozens
# at time of writing) and fires one mass-removal alert
# (by design — honesty over silence). See README "--init re-baseline".


def _zen_is_free(model_id):
    if not isinstance(model_id, str):
        return False
    return "free" in model_id.lower()


def _fetch_zen(getter=_default_getter, key=None):
    """Model ids only, FREE-ONLY (decision 2026-08-25): keep ids carrying the
    'free' marker. Everything else on Zen is a
    paid tier (claude/gpt/gemini/grok/kimi/...) and must never be tracked.
    Type-gated at a single point over BOTH item shapes — dict items
    contribute their 'id', bare items themselves; anything that is not a str
    is dropped, never repr-coerced. Uniqueness comes from set ∘ type-gate ∘
    free-filter together; the set ensures duplicate ids cannot double-fire
    alerts."""
    extra = {"Authorization": f"Bearer {key}"} if key else {}
    status, body, _hdrs = getter(ZEN_URL, headers=_headers(extra), timeout=TIMEOUT_S)
    _require_ok(status, ZEN_URL)
    items = _parse_model_list(body)
    candidates = (it.get("id") if isinstance(it, dict) else it for it in items)
    ids = sorted({i for i in candidates
                  if isinstance(i, str) and _zen_is_free(i)})
    return ids, {}


# ---------- kilo ----------

KILO_URL = "https://api.kilo.ai/api/gateway/v1/models"


def _fetch_kilo(getter=_default_getter, key=None):
    """Listed-$0 filter only; '-1' sentinels excluded by is_free(); plain report."""
    extra = {"Authorization": f"Bearer {key}"} if key else {}
    status, body, _hdrs = getter(KILO_URL, headers=_headers(extra), timeout=TIMEOUT_S)
    _require_ok(status, KILO_URL)
    ids = sorted(_extract_ids(
        _parse_model_list(body),
        keep=lambda it: (it.get("id") is not None
                         and is_free(it.get("pricing")))))
    return ids, {}


# ---------- cline (endpoint-primary, docs-fallback) ----------

# CHANGE 3 (fix-round-9): Cline DOES expose a public roster endpoint (probed
# live, no auth header required) — it is now the PRIMARY source. Docs remain
# a SECONDARY FALLBACK used only when the endpoint fails.
# F2 (this round): that fallback is the FREE-MODELS page ONLY. The old
# second entry (api/models.md) is the ALL-models catalog — live-verified as
# a PAID roster (claude-sonnet-4-6, deepseek-chat, gemini-2.5-pro,
# minimax-m2.5, gpt-4o; zero overlap with the endpoint's true free[]), so an
# endpoint outage used to swap the free roster for paid ids. An outage now
# yields either this page's ids or an honest loud FetchError (empty-parse
# rule in _fetch_cline_docs_fallback) — sticky carry-forward, never a
# paid-roster swap.
CLINE_ENDPOINT = "https://api.cline.bot/api/v1/ai/cline/recommended-models"
CLINE_PAGES = (
    "https://docs.cline.bot/getting-started/free-models.md",
)

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_MODEL_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+$")  # R2-3: case-insensitive both sides
# F5/F3: backticked DOC-file links, not model IDs. The tail may stack
# (.md.txt, .html.tmp) and every stacked segment is still a file, so accept
# extra dotted segments after a doc extension; IGNORECASE kept from F5.
# Guard rails: dotted VERSION tails (`qwen/qwen3-0.6b`, `model-name.v2`)
# don't start with a doc extension, so legitimate ids survive untouched.
_DOC_SUFFIX = re.compile(r"\.(md|html|pdf|mdx|txt)(\.\w+)*$", re.IGNORECASE)

# F-R2-1: placeholder spans docs pages use in examples (provider/model-name
# verified live on docs.cline.bot/api/models.md). They match the ID shape but
# are not models — ingesting them poisons the roster and later fires a false
# 🔴 removal when Cline edits the example.
_PLACEHOLDER_PROVIDERS = {"provider", "example", "your"}
_PLACEHOLDER_SPANS = {"provider/model-name", "provider/name"}


def _extract_cline_ids(markdown_text):
    """Backticked inline-code spans that look like provider/model IDs.

    F5: spans ending .md/.html/.pdf are documentation file paths that happen
    to match the ID shape — rejected so docs edits can't churn the roster.
    F3: stacked-extension tails (.md.txt & friends) are rejected the same way.
    F-R2-1: placeholder/example spans (`provider/model-name` & friends) are
    rejected the same way."""
    found = set()
    for span in _CODE_SPAN.findall(markdown_text):
        candidate = span.strip()
        if _DOC_SUFFIX.search(candidate):
            continue
        if not _MODEL_ID.match(candidate):
            continue
        lowered = candidate.lower()
        if lowered in _PLACEHOLDER_SPANS:
            continue
        if lowered.split("/", 1)[0] in _PLACEHOLDER_PROVIDERS:
            continue
        found.add(candidate)
    return sorted(found)


def _fetch_cline_endpoint(getter):
    """PRIMARY: GET recommended-models, extract ids from free[] ONLY.

    Response shape: {recommended:[{id,...}], free:[...], clinePass:[...],
    clineCloud:[]} — free[] is the free-roster we track; recommended/
    clinePass/clineCloud are PAID tiers and must never leak into the roster.
    An empty free[] on a healthy 200 is REAL data (CHANGE 2): returned as [],
    never an error, never a fallback trigger. Any transport/HTTP failure
    raises FetchError so the caller can fall back."""
    status, body, _hdrs = getter(CLINE_ENDPOINT, headers=_headers(),
                                 timeout=TIMEOUT_S)
    _require_ok(status, CLINE_ENDPOINT)
    payload = _loads_or_fetcherror(
        body, "cline endpoint response was not valid JSON")
    free = payload.get("free") if isinstance(payload, dict) else None
    if not isinstance(free, list):
        raise FetchError("unexpected cline endpoint payload shape")
    ids = sorted(_extract_ids(free, keep=lambda it: it.get("id") is not None))
    return ids


def _fetch_cline_docs_fallback(getter):
    """SECONDARY FALLBACK (docs change-detector): watch the free-models docs
    page's backticked IDs (F2: that page ONLY — api/models.md is a paid
    catalog and is never requested). Used ONLY when the primary endpoint
    fails."""
    collected = set()
    got_any_page = False
    for page in CLINE_PAGES:
        try:
            status, body, _hdrs = getter(page, headers=_headers(), timeout=TIMEOUT_S)
        except FetchError:
            continue  # one dead page shouldn't erase the other's signal
        if status != 200:
            continue
        got_any_page = True
        collected.update(_extract_cline_ids(body))
    if not got_any_page or not collected:
        # Moved/renamed/error pages read as OUTAGE (sticky carry-forward),
        # never as mass removal — plan's empty-parse rule.
        raise FetchError("empty parse: no cline docs pages yielded model ids")
    return sorted(collected)


def _fetch_cline(getter=_default_getter):
    """Endpoint-primary with docs fallback (CHANGE 3).

    Primary source GET api.cline.bot/api/v1/ai/cline/recommended-models
    (public, NO auth header): diff free[].id like every other provider.
    The free-models docs page (plus the placeholder/denylist and doc-suffix
    span rules) remains SECONDARY fallback only — used when the endpoint
    raises FetchError / HTTP-fails; F2 removed the api/models.md catalog from
    that fallback so an outage can never surface PAID ids. Both sources
    failing is an honest loud FetchError (sticky carry-forward), never a
    silent empty roster."""
    try:
        return _fetch_cline_endpoint(getter), {}
    except FetchError:
        pass  # fall through to the docs change-detector
    return _fetch_cline_docs_fallback(getter), {}


# ---------- command code ----------

COMMAND_CODE_URL = "https://api.commandcode.ai/provider/v1/models"


def _command_code_is_free(model_id):
    """Command Code ships NO pricing metadata (probed 2026-08-26: objects carry
    only id/object/created/owned_by/name/context_length). Free-roster rule:
    explicit "free" name marker only — no alias map, no allowlist, no
    normalized-name matching (the free lane is small and deal-structured:
    minimax-m3-free, minimax-m2.7-free, laguna-s-2.1-free). A NEW free
    arrival needs its id to carry the "free" marker; if Command Code ever
    ships a free model under an opaque id, it is simply not tracked."""
    if not isinstance(model_id, str):
        return False
    return "free" in model_id.lower()


def _fetch_command_code(getter=_default_getter):
    """Command Code free-only filter: endpoint serves the full catalog (60+
    models, NO pricing field) — only ids carrying the "free" marker are
    tracked. Paid tiers (claude/gpt/gemini/grok/...) must never leak into
    the roster. Same type-gated extraction as zen: dict items contribute
    their 'id', non-dicts are dropped, never repr-coerced."""
    status, body, _hdrs = getter(COMMAND_CODE_URL, headers=_headers(),
                                  timeout=TIMEOUT_S)
    _require_ok(status, COMMAND_CODE_URL)
    items = _parse_model_list(body)
    candidates = (it.get("id") if isinstance(it, dict) else it for it in items)
    ids = sorted({i for i in candidates
                  if isinstance(i, str) and _command_code_is_free(i)})
    return ids, {}


# ---------- registry (order = display order) ----------

PROVIDERS = {
    "nous": _fetch_nous,
    "openrouter": _fetch_openrouter,
    "zen": _fetch_zen,
    "kilo": _fetch_kilo,
    "cline": _fetch_cline,
    "command_code": _fetch_command_code,
}
