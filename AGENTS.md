# AGENTS.md — Free Inference Watchdog

## Hard rules
- Python stdlib only (the guarded `mcp` SDK import in mcp_server.py is the sole exception).
- Zero LLM tokens: the cron job is script-mode `--no-agent`; no LLM is ever woken.
- One tick per invocation: `python3 inference_watchdog.py` fetches, diffs, exits.
- Empty stdout = healthy-silent; diagnostics go to stderr. Exit codes: 0 normal, 1 partial provider failure / bootstrap refused, 2 fatal.

## Providers
- The roster is the set of `providers/*.json` files; user-visible order is each config's `display` field, surfaced via `config_loader.PROVIDERS` (build_site's `DISPLAY_ORDER` and mcp_server's `PROVIDERS` derive from it — no duplicate list lives in module code).
- Provider order everywhere user-visible is DISPLAY order — the `display` number in providers/*.json (nim last); there is no separate providers.py ordering to conflict with.
- Free-only rule per provider: an id is tracked iff `"free" in id.lower()`. No alias map, no allowlist, no normalized-name matching — exact ids only. A new stealth arrival ships under whatever id the gateway assigns; if that id doesn't contain "free", it's not tracked.
- Ollama is gone BY DESIGN (GPU-time metering, no free-model concept) — do not re-add it.

## Tests & README
- `python3 -m pytest tests/ -q` fully green before ANY commit.
- tests/test_readme.py PINS README wording (cron wrapper block, --init/.bak language, silent-cron `--deliver local`). Editing README.md is a code change.

## Deploy ritual
- Behavior-changing roster edits require a manual `python3 inference_watchdog.py --init` rebaseline (archives roster.json to roster.json.bak), then verify the next tick is SILENT.

## State layout (state/, gitignored)
- roster.json: providers + tick_epoch + stale_providers + transients + unconfirmed + ratelimits. Never hand-edit — use --init.
- alive.json: last_tick_epoch + last_output_epoch + dropped_alerts_total.
- pending_alerts.json: bounded retry queue (MAX_ATTEMPTS 5 per alert).
- Lockfile recovery per README (state/monitor.lock; stale >30 min auto-broken).

## Alert hygiene — both exist so noise never reaches Discord; preserve them
- Fetch failure = sticky carry-forward of last-known-good ids, never a mass removal.
- Every diff is re-fetched after a ~3-minute delay (recheck_delay 180) before it is believed.

## Delivery topology (operator decision 2026-08-26)
- The Discord webhook `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` in the project-local `.env` is the ONLY alert path.
- The cron job is silent (`--deliver local`); wrapper failures go to stderr, never stdout.

## Dashboard & MCP
- site/index.html is rebuilt from state/roster.json by build_site.py each tick and COMMITTED+PUSHED by the cron wrapper — GitHub Pages deploys it to models.dthen.xyz via .github/workflows/deploy-dashboard.yml. Other files under site/ stay gitignored.
- mcp_server.py exposes list_free_models / get_model / watchdog_status read-only over state/.

## Update cadence
- Cron schedule `"17 */1 * * *"` and `--cadence-hours` (default 1) must stay in step — the flag drives the ⚠️ missed-tick warning.
