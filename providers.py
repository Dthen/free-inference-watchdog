"""Provider fetchers — config-driven, stdlib only."""
import json
import urllib.request
import urllib.error
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

def _require_ok(status, url):
    if status != 200:
        raise FetchError(f"HTTP {status} from {url}")

def fetch_provider(config, getter=_default_getter):
    """Fetch free models for a provider config."""
    base_url = config["base_url"].rstrip("/")
    token = config.get("_token")

    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    status, body, resp_headers = getter(
        f"{base_url}/models", headers=headers, timeout=TIMEOUT_S)
    if status != 200:
        raise FetchError(f"HTTP {status} from {base_url}/models")

    items = _parse_model_list(body)
    detection = config["detection"]

    if detection == "zero-credit-probe":
        return sorted(_extract_ids(items)), {}

    free_ids = [i["id"] for i in items if detect_free(i, detection)]
    return sorted(free_ids), {}