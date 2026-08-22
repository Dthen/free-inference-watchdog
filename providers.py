"""Six provider fetchers for the Free Inference Monitor. Stdlib only.

Contract: each fetcher returns (ids: list[str], meta: dict) with ids SORTED,
or raises FetchError. Transport is injectable via `getter(url, headers, timeout)
-> (status:int, body:str, headers:dict)` so tests run without network.
The real getter ALWAYS sends a User-Agent (Nous 403s bare urllib — probed).
"""

import json
import re
import urllib.error
import urllib.request

USER_AGENT = "free-inference-monitor/1.0"
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


def _parse_model_list(body):
    """OpenAI-style {"data":[...]} or bare list -> list[dict|str]."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError("response was not valid JSON") from exc
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise FetchError("unexpected models payload shape")
    return items


def _require_ok(status, url):
    if status != 200:
        raise FetchError(f"HTTP {status} from {url}")


# ---------- nous ----------

def _load_nous_auth(auth_path="~/.hermes/auth.json"):
    """Named owner of auth.json parsing (plan round-1 finding #8)."""
    import os
    path = os.path.expanduser(auth_path)
    try:
        with open(path, encoding="utf-8") as fh:
            nous = json.load(fh)["providers"]["nous"]
        token, base = nous["access_token"], nous["inference_base_url"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise FetchError(f"cannot load nous auth from {path}") from exc
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
    ids = sorted(
        it["id"] for it in _parse_model_list(body)
        if isinstance(it, dict) and it.get("id") and is_free(it.get("pricing"))
    )
    ratelimit = {k: v for k, v in (hdrs or {}).items() if "ratelimit" in k.lower()}
    return ids, {"ratelimit": ratelimit}


# ---------- openrouter ----------

def _fetch_openrouter(getter=_default_getter):
    url = "https://openrouter.ai/api/v1/models"  # public, no auth
    status, body, _hdrs = getter(url, headers=_headers(), timeout=TIMEOUT_S)
    _require_ok(status, url)
    ids = sorted(
        it["id"] for it in _parse_model_list(body)
        if isinstance(it, dict) and it.get("id") and is_free(it.get("pricing"))
    )
    return ids, {}


# ---------- zen ----------

ZEN_URL = "https://opencode.ai/zen/v1/models"


def _fetch_zen(getter=_default_getter, key=None):
    """Bare ID strings, zero metadata — diffed verbatim (decision #3)."""
    extra = {"Authorization": f"Bearer {key}"} if key else {}
    status, body, _hdrs = getter(ZEN_URL, headers=_headers(extra), timeout=TIMEOUT_S)
    _require_ok(status, ZEN_URL)
    items = _parse_model_list(body)
    ids = sorted(str(it) for it in items if isinstance(it, (str, int)))
    return ids, {}


# ---------- kilo ----------

KILO_URL = "https://api.kilo.ai/api/gateway/v1/models"


def _fetch_kilo(getter=_default_getter, key=None):
    """Listed-$0 filter only; '-1' sentinels excluded by is_free(); plain report."""
    extra = {"Authorization": f"Bearer {key}"} if key else {}
    status, body, _hdrs = getter(KILO_URL, headers=_headers(extra), timeout=TIMEOUT_S)
    _require_ok(status, KILO_URL)
    ids = sorted(
        it["id"] for it in _parse_model_list(body)
        if isinstance(it, dict) and it.get("id") and is_free(it.get("pricing"))
    )
    return ids, {}


# ---------- ollama cloud ----------

OLLAMA_URL = "https://ollama.com/v1/models"


def _fetch_ollama(getter=_default_getter, key=None):
    """No pricing field (probed) — roster passed through as-is."""
    extra = {"Authorization": f"Bearer {key}"} if key else {}
    status, body, _hdrs = getter(OLLAMA_URL, headers=_headers(extra), timeout=TIMEOUT_S)
    _require_ok(status, OLLAMA_URL)
    ids = sorted(
        str(it["id"]) for it in _parse_model_list(body)
        if isinstance(it, dict) and it.get("id")
    )
    return ids, {}


# ---------- cline (docs change-detector) ----------

CLINE_PAGES = (
    "https://docs.cline.bot/getting-started/free-models.md",
    "https://docs.cline.bot/api/models.md",
)

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_MODEL_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.:-]+$")  # R2-3: case-insensitive both sides


def _extract_cline_ids(markdown_text):
    """Backticked inline-code spans that look like provider/model IDs."""
    found = set()
    for span in _CODE_SPAN.findall(markdown_text):
        candidate = span.strip()
        if _MODEL_ID.match(candidate):
            found.add(candidate)
    return sorted(found)


def _fetch_cline(getter=_default_getter):
    """DOCS CHANGE-DETECTOR: no Cline API exists (probed 404s); we watch the
    two docs pages' backticked IDs. Known blind spot (documented in plan +
    README): promo rotations that never touch these pages are invisible."""
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
    return sorted(collected), {}


# ---------- registry (order = display order) ----------

PROVIDERS = {
    "nous": _fetch_nous,
    "openrouter": _fetch_openrouter,
    "zen": _fetch_zen,
    "kilo": _fetch_kilo,
    "ollama": _fetch_ollama,
    "cline": _fetch_cline,
}
