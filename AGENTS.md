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

## Config gotchas (read before adding a field to providers/*.json)
- The full field list lives in README.md's schema table. The traps below are what that table cannot say.
- Optional fields are validated PER-FILE and the type is STRICT: present means valid, and valid means exactly that type. A bad value skips that one config with a `config: skipping ...` stderr warning — it never takes the roster with it. Follow the existing `display` / `removal_hold_seconds` guards.
- bools must be real JSON booleans. `bool("false")` is `True` and `bool(0)` is `False`, so a quoted `"false"` or a bare `0` silently inverts the operator's intent with no error anywhere. If you add a boolean field, reject anything that is not `isinstance(x, bool)`.
- Model metadata is stored as the RAW API object under `roster.json` → `provider_models`, nested per gateway. Only three of its fields are ever read: `name` (dashboard row labels), and `context_length` + `architecture.input_modalities` (hover tooltip).

## Tests & README
- `python3 -m pytest tests/ -q` fully green before ANY commit.
- tests/test_readme.py PINS README wording (cron wrapper block, --init/.bak language, silent-cron `--deliver local`). Editing README.md is a code change.
- One README, not two. providers/README.md was folded into the root README's schema section; the repo carries exactly README.md, AGENTS.md and LICENSE.md.

## Deploy ritual
- Behavior-changing roster edits require a manual `python3 inference_watchdog.py --init` rebaseline (archives roster.json to roster.json.bak), then verify the next tick is SILENT.
- EXCEPTION (operator ruling 2026-09-27): a registry change that only ADDS or REMOVES a provider — adding `providers/*.json`, deleting one, or setting `"enabled": false` — needs NO `--init`. Such a change is already silent: `load_filtered_roster` drops keys outside the registry and `compute_events` only walks the fetched map, so no removal alert is produced. Running `--init` anyway would rebaseline all seven providers and suppress their real diffs for a tick. Re-enabling a provider repopulates the roster and surfaces its models as additions, which is correct and self-healing.

## State layout (state/, gitignored)
- roster.json: providers + tick_epoch + stale_providers + transients + unconfirmed + ratelimits. Never hand-edit — use --init.
- alive.json: last_tick_epoch + last_output_epoch + dropped_alerts_total.
- pending_alerts.json: bounded retry queue (MAX_ATTEMPTS 5 per alert).
- pending_removals.json: held provider removals waiting to settle (shared by both settle paths).
- Lockfile recovery per README (state/monitor.lock; stale >30 min auto-broken).

## Alert hygiene — both exist so noise never reaches Discord; preserve them
- Fetch failure = sticky carry-forward of last-known-good ids, never a mass removal.
- Every diff is re-fetched after a ~3-minute delay (recheck_delay 180) before it is believed.
- Held providers' confirmed removals wait in `state/pending_removals.json`; BOTH settle paths share `pending_removals.settle` — never fork the logic; resolve passes never touch alive.json.

## Delivery topology (operator decision 2026-08-26)
- The Discord webhook `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` in the project-local `.env` is the ONLY alert path.
- The cron job is silent (`--deliver local`); wrapper failures go to stderr, never stdout.

## Dashboard & MCP
- site/index.html is rebuilt from state/roster.json by build_site.py each tick and COMMITTED+PUSHED by the cron wrapper — GitHub Pages deploys it to models.dthen.xyz via .github/workflows/deploy-dashboard.yml. Other files under site/ stay gitignored.
- mcp_server.py exposes list_free_models / get_model / watchdog_status read-only over state/.

## Update cadence
- Cron schedule `"17 */1 * * *"` and `--cadence-hours` (default 1) must stay in step — the flag drives the ⚠️ missed-tick warning.
- Second cron `*/15 * * * *` in step with the 3600s timeout.
