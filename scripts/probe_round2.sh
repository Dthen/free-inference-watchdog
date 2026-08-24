#!/usr/bin/env bash
# Probe round 2: fix nous URL, find cline free-models page, sanity-check free-filters.
# Prints URLs and counts only — never tokens.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 - <<'PY'
import json, re, urllib.request, urllib.error
from pathlib import Path

HERMES = Path.home() / ".hermes"
AUTH = json.loads((HERMES / "auth.json").read_text(encoding="utf-8"))
nous = AUTH.get("providers", {}).get("nous", {})
BASE = (nous.get("inference_base_url") or "").rstrip("/")
TOK = nous.get("access_token") or ""

def get(url, headers=None, timeout=15):
    req = urllib.request.Request(
        url, headers={"User-Agent": "free-inference-watchdog-probe/1.0", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200], {}
    except Exception as e:
        return None, f"{type(e).__name__}", {}

print(f"nous inference_base_url = {BASE or '(none!)'}")
variants = []
if BASE:
    b = BASE.rstrip("/")
    variants.append(f"{b}/v1/models")
    if b.endswith("/v1"):
        variants.append(f"{b}/models")
    variants.append(f"{b}/api/v1/models")
nous_ok = None
for u in variants:
    st, body, hdrs = get(u, {"Authorization": f"Bearer {TOK}"})
    n = "?"
    if st == 200:
        try:
            j = json.loads(body)
            items = j.get("data", j) if isinstance(j, dict) else j
            n = len(items) if isinstance(items, list) else "?"
            nous_ok = u
        except Exception:
            n = "non-json"
    rl = {k: v for k, v in hdrs.items() if "ratelimit" in k.lower()}
    print(f"nous try {u} -> HTTP {st} ids={n} ratelimit-hdrs={rl or '-'}")
    if nous_ok:
        break

print()
st, body, _ = get("https://docs.cline.bot/llms.txt")
print(f"cline llms.txt HTTP {st}, scanning for free-models links:")
for line in body.splitlines():
    if re.search(r"free|model", line, re.I) and ("http" in line or line.strip().startswith("-")):
        print("   ", line.strip()[:160])

print()
for name, url in [("openrouter", "https://openrouter.ai/api/v1/models"),
                  ("kilo", "https://api.kilo.ai/api/gateway/v1/models")]:
    st, body, _ = get(url)
    j = json.loads(body)
    items = j.get("data", j) if isinstance(j, dict) else j
    free = []
    for it in items:
        pr = it.get("pricing") or {}
        def f(x):
            try:
                return float(pr.get(x, 1))
            except (TypeError, ValueError):
                return 1.0
        if f("prompt") == 0.0 and f("completion") == 0.0:
            free.append(it.get("id", "?"))
    print(f"{name}: total={len(items)} FREE(prompt&completion==0)={len(free)}")
    print(f"   sample free ids: {free[:5]}")
PY
