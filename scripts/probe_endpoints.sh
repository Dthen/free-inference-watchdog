#!/usr/bin/env bash
# Live endpoint probe for Free Inference Monitor (plan Task 2).
# Prints ONLY: provider name, HTTP status, id count, sample pricing SHAPE (type/truncated).
# NEVER prints tokens or full response bodies.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 - <<'PY'
import json, re, urllib.request, urllib.error
from pathlib import Path

HERMES = Path.home() / ".hermes"

def parse_env(path):
    out = {}
    try:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            k = k.strip(); v = v.strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1]
            if k:
                out[k] = v
    except FileNotFoundError:
        pass
    return out

ENV = parse_env(HERMES / ".env")
try:
    AUTH = json.loads((HERMES / "auth.json").read_text(encoding="utf-8"))
except Exception:
    AUTH = {}

cfg_text = ""
try:
    cfg_text = (HERMES / "config.yaml").read_text(encoding="utf-8", errors="replace")
except Exception:
    pass
CFG_URLS = re.findall(r"https?://[^\s\"']+", cfg_text)

def discover(hint):
    seen, hits = set(), []
    for u in CFG_URLS:
        if hint.lower() in u.lower() and u not in seen:
            seen.add(u); hits.append(u.rstrip("/"))
    return hits

def get(url, headers=None, timeout=15):
    if not url:
        return None, "(no url)"
    req = urllib.request.Request(
        url, headers={"User-Agent": "free-inference-monitor-probe/1.0", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        snippet = re.sub(r"\s+", " ", body)[:160]
        redacted = snippet.replace(ENV.get("OPENCODE_ZEN_API_KEY",""), "***").replace(
            ENV.get("KILOCODE_API_KEY",""), "***").replace(ENV.get("OLLAMA_API_KEY",""), "***")
        return e.code, f"err-body: {redacted}"
    except Exception as e:
        return None, f"{type(e).__name__}"

def summarize(body):
    try:
        j = json.loads(body)
    except Exception:
        return 0, "non-json"
    items = j.get("data", j) if isinstance(j, dict) else j
    if isinstance(items, dict):
        items = [items]
    n = 0
    pricing_shape = None
    if isinstance(items, list):
        for it in items:
            n += 1
            if pricing_shape is None and isinstance(it, dict) and "pricing" in it:
                pr = it["pricing"]
                pricing_shape = f"type={type(pr).__name__} sample={repr(pr)[:100]}"
            if n >= 3000:
                break
    return n, pricing_shape or "no-pricing-field"

nous = ((AUTH.get("providers") or {}).get("nous") or {})
nous_base = (nous.get("inference_base_url") or "").rstrip("/")
nous_tok = nous.get("access_token") or ""

CANDIDATES = [
    ("nous", f"{nous_base}/v1/models",
     {"Authorization": f"Bearer {nous_tok}"} if nous_tok else {}),
    ("openrouter", "https://openrouter.ai/api/v1/models", {}),
    ("zen[guess]", "https://opencode.ai/zen/v1/models",
     {"Authorization": f"Bearer {ENV.get('OPENCODE_ZEN_API_KEY','')}"}),
    *[("zen[cfg]", u, {"Authorization": f"Bearer {ENV.get('OPENCODE_ZEN_API_KEY','')}"})
      for u in discover("zen")],
    ("kilo[guess]", "https://api.kilo.ai/api/gateway/v1/models",
     {"Authorization": f"Bearer {ENV.get('KILOCODE_API_KEY','')}"}),
    *[("kilo[cfg]", u, {"Authorization": f"Bearer {ENV.get('KILOCODE_API_KEY','')}"})
      for u in discover("kilo")],
    ("ollama-cloud[guess]", "https://ollama.com/v1/models",
     {"Authorization": f"Bearer {ENV.get('OLLAMA_API_KEY','')}"}),
    *[("ollama[cfg]", u, {"Authorization": f"Bearer {ENV.get('OLLAMA_API_KEY','')}"})
      for u in discover("ollama")],
    ("cline-docs-index", "https://docs.cline.bot/llms.txt", {}),
]

print("== free-inference-monitor endpoint probe ==")
print(f"(config urls discovered: {len(CFG_URLS)}; secrets never printed)")
for name, url, hdrs in CANDIDATES:
    status, body = get(url, hdrs)
    if status == 200:
        n, shape = summarize(body)
        print(f"{name:22} | HTTP {status} | ids={n} | {shape}")
    else:
        print(f"{name:22} | HTTP {status} | {body}")
PY
