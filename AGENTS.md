# AGENTS.md — Free Inference Watchdog

## Hard rules
- Python stdlib only (the guarded `mcp` SDK import in mcp_server.py is the sole exception).
- Zero LLM tokens: the cron job is script-mode `--no-agent`; no LLM is ever woken.
- One tick per invocation: `python3 inference_watchdog.py` fetches, diffs, exits.
- Empty stdout = healthy-silent; diagnostics go to stderr. Exit codes: 0 normal, 1 partial provider failure / bootstrap refused, 2 fatal.

## Providers
- Six gateways in DISPLAY order `nous, zen, kilo, cline, openrouter, command_code` — canonical for UI/MCP surfaces (build_site.py `DISPLAY_ORDER`, mcp_server.py `PROVIDERS` tuple).
- providers.PROVIDERS dict order differs (nous, openrouter, zen, kilo, cline, command_code); display order above wins everywhere user-visible.
- Free-only rule per provider: an id is tracked iff `"free" in id.lower()`. No alias map, no allowlist, no normalized-name matching — exact ids only. A new stealth arrival ships under whatever id the gateway assigns; if that id doesn't contain "free", it's not tracked.
- Ollama is gone BY DESIGN (GPU-time metering, no free-model concept) — do not re-add it.

## Tests & README
- `python3 -m pytest tests/ -q` fully green before ANY commit.
- tests/test_readme.py PINS README wording (cron wrapper block, --init/.bak language, silent-cron `--deliver local`). Editing README.md is a code change.

## Deploy ritual
- Behavior-changing roster edits require a manual `python3 inference_watchdog.py --init` rebaseline (archives roster.json to roster.json.bak), then verify the next tick is SILENT.

## State layout (state/, gitignored)
- roster.json: providers + tick_epoch + stale_providers + transients + unconfirmed + nous_ratelimit. Never hand-edit — use --init.
- alive.json: last_tick_epoch + last_output_epoch + dropped_alerts_total.
- cooldowns.json: `provider|model|kind` epoch stamps, pruned at persist (12h TTL).
- pending_alerts.json: bounded retry queue (MAX_ATTEMPTS 5 per alert).
- Lockfile recovery per README (state/monitor.lock; stale >30 min auto-broken).

## Alert hygiene — all three exist so noise never reaches Discord; preserve them
- Fetch failure = sticky carry-forward of last-known-good ids, never a mass removal.
- Every diff is re-fetched after a ~3-minute delay (recheck_delay 180) before it is believed.
- 12h dedup cooldown per (provider, model, event).

## Delivery topology (operator decision 2026-08-26)
- The Discord webhook `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` in ~/.hermes/.env is the ONLY alert path.
- The cron job is silent (`--deliver local`); wrapper failures go to stderr, never stdout.

## Dashboard & MCP
- site/index.html is rebuilt from state/roster.json by build_site.py each tick and COMMITTED+PUSHED by the cron wrapper — GitHub Pages deploys it to models.dthen.xyz via .github/workflows/deploy-dashboard.yml. Other files under site/ stay gitignored.
- mcp_server.py exposes list_free_models / get_model / watchdog_status read-only over state/.

## Update cadence
- Cron schedule `"17 */1 * * *"` and `--cadence-hours` (default 1) must stay in step — the flag drives the ⚠️ missed-tick warning.
