#!/usr/bin/env bash
# Live endpoint probe for Free Inference Watchdog (plan Task 2).
# Prints ONLY: provider name, HTTP status, id count, sample pricing SHAPE (type/truncated).
# NEVER prints tokens or full response bodies.
# Reads ONLY ~/.hermes/.env + ~/.hermes/auth.json — never ~/.hermes/config.yaml
# (out-of-scope reference dropped, fix-round-2 S5).
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 - <<'PY'
import json, re, urllib.request, urllib.error
from pathlib import Path

HERMES = Path.home() / ".hermes"

def parse_env(path):
    # Mirror of envfile.parse_envfile (fix-round-2 S5): skips blank lines,
    # '#' comments, lines without '=' and empty keys; a shell-style
    # 'export KEY=v' prefix on the KEY is stripped (the keyword is not part
    # of the name); strips ONE pair of surrounding double quotes from
    # values; splits on the FIRST '=' only (URLs survive); opened
    # utf-8-sig so an editor-written leading BOM can't poison the first
    # key; missing file -> {}.
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return out
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip()
        if k.startswith("export ") or k.startswith("export\t"):
            k = k[len("export"):].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
            v = v[1:-1]
        if k:
            out[k] = v
    return out

ENV = parse_env(HERMES / ".env")
try:
    AUTH = json.loads((HERMES / "auth.json").read_text(encoding="utf-8"))
except Exception:
    AUTH = {}

nous = ((AUTH.get("providers") or {}).get("nous") or {})
nous_base = (nous.get("inference_base_url") or "").rstrip("/")
nous_tok = nous.get("access_token") or ""

# Redaction secrets, built AFTER both credential loads so all three families
# are covered: zen + kilo API keys AND the Nous bearer token. Error bodies are
# the only response text that ever reaches stdout; they must never carry a
# live token. Empty values are filtered out ("":.replace would interleave
# '***' between every character of the error body).
SECRETS = [s for s in (
    ENV.get("OPENCODE_ZEN_API_KEY", ""),
    ENV.get("KILOCODE_API_KEY", ""),
    nous_tok,
) if s]

def get(url, headers=None, timeout=15):
    if not url:
        return None, "(no url)"
    req = urllib.request.Request(
        url, headers={"User-Agent": "free-inference-watchdog-probe/1.0", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        snippet = re.sub(r"\s+", " ", body)[:160]
        redacted = snippet
        for secret in SECRETS:
            redacted = redacted.replace(secret, "***")
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

CANDIDATES = [
    ("nous", f"{nous_base}/models",   # base already ends /v1 (probe fact); {base}/v1/models 404s
     {"Authorization": f"Bearer {nous_tok}"} if nous_tok else {}),
    ("openrouter", "https://openrouter.ai/api/v1/models", {}),
    ("zen[guess]", "https://opencode.ai/zen/v1/models",
     {"Authorization": f"Bearer {ENV.get('OPENCODE_ZEN_API_KEY','')}"}),
    ("kilo[guess]", "https://api.kilo.ai/api/gateway/v1/models",
     {"Authorization": f"Bearer {ENV.get('KILOCODE_API_KEY','')}"}),
    ("cline-docs-index", "https://docs.cline.bot/llms.txt", {}),
]

print("== free-inference-watchdog endpoint probe ==")
print("(sources: ~/.hermes/.env + auth.json only; secrets never printed)")
for name, url, hdrs in CANDIDATES:
    status, body = get(url, hdrs)
    if status == 200:
        n, shape = summarize(body)
        print(f"{name:22} | HTTP {status} | ids={n} | {shape}")
    else:
        print(f"{name:22} | HTTP {status} | {body}")
PY
