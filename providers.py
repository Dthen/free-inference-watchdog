"""Provider fetchers — config-driven, stdlib only.

This module stays the ONLY HTTP discovery fetcher: URL selection, the injected
getter, timeout, User-Agent, ignored exact ids and passive rate-limit headers
all live here. Format-specific envelope, advertised-price and metadata rules
live in :mod:`catalog_adapters`; :func:`fetch_provider` only dispatches to them.

Two optional config members change the discovery request, never the published
inference URL:

``catalog_url``
    An absolute literal HTTPS discovery URL (query allowed). Absent means the
    accepted ``base_url.rstrip('/') + '/models'``.
``catalog_format``
    One registered catalogue family. Absent means ``openai-models``, whose
    envelope/identity handling is untouched.
"""
import json
import urllib.request
import urllib.error

import catalog_adapters
from detection import detect_free

USER_AGENT = "free-inference-watchdog/1.0"
TIMEOUT_S = 15

class FetchError(Exception):
    pass

def _default_getter(url, headers=None, timeout=TIMEOUT_S):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} from {url}") from exc
    except Exception as exc:
        raise FetchError(f"{type(exc).__name__} fetching {url}") from exc

def _loads_or_fetcherror(text, ctx_msg):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(ctx_msg) from exc


def _parse_model_list(body):
    payload = _loads_or_fetcherror(body, "response was not valid JSON")
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise FetchError("unexpected models payload shape")
    return items

def _extract_ids(items):
    return [
        str(it.get("id")) if isinstance(it, dict) else str(it)
        for it in items
        if isinstance(it, dict) and it.get("id") is not None
    ]

def fetch_provider(config, getter=_default_getter):
    """Fetch free models for a provider config.

    Returns (sorted ids, meta). meta carries PASSIVE extras only:
      models     — {model_id: raw API model object} for exactly the ids
                   returned (never a normalized schema)
      ratelimit  — x-ratelimit response headers, when the gateway sends any

    Optional ignored_slugs: a list of exact model ids excluded AFTER
    detection — the free-marker rule itself is untouched; this is purely
    an exclusion layer for ids the operator does not want tracked.
    Excluded ids drop silently (operator decision 2026-09-13).

    Optional catalog_url/catalog_format select the discovery URL and the
    registered adapter. A nonempty credential never crosses origins: a
    cross-origin override raises FetchError BEFORE the getter runs, so no
    Authorization header can leak to another scheme/host/port.
    """
    base_url = config["base_url"].rstrip("/")
    token = config.get("_token")
    ignored = set(config.get("ignored_slugs", []))
    catalog_format = (config.get("catalog_format")
                      or catalog_adapters.LEGACY_FORMAT)
    discovery_url = config.get("catalog_url") or f"{base_url}/models"

    if token and (catalog_adapters.url_origin(discovery_url)
                  != catalog_adapters.url_origin(config["base_url"])):
        # The credential belongs to its own origin. Refuse rather than forward
        # it: config validation rejects this per-file too, but a hand-built
        # config must not be able to smuggle a token across origins either.
        raise FetchError(
            f"refusing cross-origin credentialed catalog fetch: {discovery_url}")

    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    status, body, resp_headers = getter(
        discovery_url, headers=headers, timeout=TIMEOUT_S)
    if status != 200:
        raise FetchError(f"HTTP {status} from {discovery_url}")

    detection = config["detection"]
    if catalog_adapters.is_legacy_format(catalog_format):
        items = _parse_model_list(body)
    else:
        try:
            items = catalog_adapters.parse_catalog(body, catalog_format)
        except catalog_adapters.AdapterError as exc:
            raise FetchError(str(exc)) from exc

    if detection == "zero-credit-probe":
        free_ids = _extract_ids(items)
    else:
        try:
            # One identity selection for every new format (`id` for the Vercel
            # and Felo families, the literal `model_name` for TokenRA); the
            # legacy expression stays exactly `i["id"]`.
            free_ids = [
                catalog_adapters.selected_identity(i, catalog_format)
                for i in items
                if detect_free(i, detection, catalog_format)]
        except catalog_adapters.AdapterError as exc:
            raise FetchError(str(exc)) from exc
    free_ids = [i for i in free_ids if i not in ignored]
    selected = set(free_ids)
    # Preserve raw API objects; metadata never participates in detection.
    if catalog_adapters.is_legacy_format(catalog_format):
        meta = {"models": {
            str(item["id"]): item for item in items
            if isinstance(item, dict) and item.get("id") is not None
            and str(item["id"]) in selected
        }}
    else:
        # The SAME adapter identity that admitted an id keys its raw record, so
        # a `newapi-pricing` row is stored under `model_name` and never gains
        # an `id` member.
        models = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            model_id = catalog_adapters.selected_identity(item, catalog_format)
            if model_id in selected:
                models[model_id] = item
        meta = {"models": models}
    # Capture ratelimit headers for passive telemetry.
    ratelimit = {k: v for k, v in resp_headers.items() if "ratelimit" in k.lower()}
    if ratelimit:
        meta["ratelimit"] = ratelimit
    return sorted(free_ids), meta