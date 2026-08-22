# free-inference-monitor

Zero-token cron watchdog for 6 free-tier LLM gateways. Alerts Discord when a
free model appears or disappears. Stdlib-only Python, one tick per invocation,
no LLM calls ever.

## What it does

Every 6 hours (`--recheck-delay` determines the recheck nap), the monitor:

1. Fetches free-model rosters from **Nous**, **OpenRouter**, **OpenCode Zen**,
   **Kilo**, **Ollama Cloud**, and **Cline** (docs-watcher — see blind spot).
2. Carries forward last-known-good IDs on provider failure (sticky silence —
   an outage never looks like a mass removal).
3. Set-diffs against the previous `roster.json`.
4. Re-fetches affected providers ~3 min later to confirm (kills transient flaps).
5. Applies a 12h dedup cooldown per `(provider, model, event)`.
6. Alerts Discord via stdout (Hermes cron → Discord home channel) AND webhook
   (kennel channel). Both or either — webhook failure never suppresses stdout.
7. Writes an alive ping to silence the "is it dead?" question.

## Quick start

```bash
cd ~/projects/free-inference-monitor
python3 inference_monitor.py --dry-run          # see what would happen
python3 inference_monitor.py --init             # bootstrap roster.json
python3 inference_monitor.py                    # one tick (cron does this)
```

## Cron registration

Register as a Hermes cron job (`no_agent=True` mode — stdout IS the delivery):

```bash
hermes cron register \
  --schedule "17 */6 * * *" \
  --no_agent \
  --command "cd /home/kimbo/projects/free-inference-monitor || { echo \"inference-monitor FAILED (cannot cd)\"; exit 1; }; python3 inference_monitor.py || { c=\$?; [ \$c -eq 1 ] || echo \"inference-monitor FAILED (exit \$c)\"; }" \
  --deliver discord-home
```

The fail-wrapper is **mandatory**: exit-2 crashes emit no stdout, and without
it a deterministically crashing monitor would be indistinguishable from
healthy silence forever. The two stages are deliberately split — under bash a
failed `cd` IS exit 1, so chaining `cd && python3` into one exemption clause
would silence a moved/renamed install dir forever; here a missing project dir
pages immediately (`cannot cd`). A bare exit 1 from the monitor itself is a
routine partial outage — the carried-forward "fetch failed" line in that
tick's alert already says which provider flaked — so the wrapper stays silent
on it; anything else still pages.

## Environment variables

All secrets live in `~/.hermes/.env`:

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_INFERENCE_MONITOR` | no | Kennel/alerts channel webhook. Alerts also go to stdout (Discord home). |
| `OPENCODE_ZEN_API_KEY` | yes | OpenCode Zen fetcher |
| `KILOCODE_API_KEY` | yes | Kilo fetcher |
| `OLLAMA_API_KEY` | yes | Ollama Cloud fetcher |

OpenRouter needs no key — its models endpoint is public. Cline is watched as
a public docs change-detector; no key is read for it either (F8a).

### Webhook rotation

1. Update `DISCORD_WEBHOOK_INFERENCE_MONITOR` in `~/.hermes/.env`.
2. Undelivered alerts queue in `state/pending_alerts.json` — drain manually:

```bash
python3 -m json.tool state/pending_alerts.json    # inspect queue
```

The queue auto-drains on the next successful tick.

### Nous auth

`~/.hermes/auth.json` holds `providers.nous.access_token` and
`providers.nous.inference_base_url`. Hermes refreshes the token automatically;
the monitor reads it at tick time, so a mid-token expiry just looks like a
provider failure (sticky carry-forward) — the next tick picks up the fresh
token naturally.

## Roster.json fields

| Field | Meaning |
|---|---|
| `providers` | `{name: [ids]}` — current known-free model IDs per provider |
| `stale_providers` | Provider names whose fetch failed this tick (carried forward) |
| `transients` | Rebuilt every tick. Diffs that appeared then vanished on recheck. |
| `unconfirmed` | Rebuilt every tick. Diffs whose recheck itself failed (signal may resurface). |
| `nous_ratelimit` | Passive x-ratelimit headers from Nous (`{}` if Nous failed) |
| `tick_epoch` | Unix epoch of this tick |

All per-tick fields are **rebuilt** (never appended to). The only persistent
counter is `dropped_alerts_total` in `alive.json`, surfaced by the alive ping.

## Drop-a-provider / Zen eviction

The roster is filtered to the PROVIDERS registry keys on load. To drop a
provider (e.g. Zen emitting phantom pairs), remove its entry from
`providers.PROVIDERS` in `providers.py`. The registry filter in
`diffing.load_filtered_roster()` silently drops the zombie entry on the next
tick — no manual state surgery needed.

## Cadence change

The tick cadence defaults to 6 hours. To change it:

1. Update the cron schedule to match (`hermes cron update ...`).
2. Pass `--cadence-hours N` (default 6) on the invocation — it drives the
   ⚠️ missed-tick warning, which fires when the last tick is older than
   cadence + 2h slack. Keep the flag in step with the cron schedule or the
   warning will cry wolf.

The 💚 alive ping fires on output-age ≥ 20h regardless of cadence. Both
derive from `alive.json` — no restart needed.

## Manual lockfile recovery

If the monitor crashes without releasing its PID lock:

```bash
# Verify no monitor is actually running:
pgrep -f inference_monitor.py

# If nothing holds it:
rm state/monitor.lock
```

Stale locks (>30 min old) are automatically broken on the next invocation.

## --init re-baseline

Running `--init` archives any existing `roster.json` to `roster.json.bak`
before clean-rebaselining. It always prints "initialized, no diff" and never
alerts. Safe to re-run at any time.

## Cline docs-watcher blind spot

Cline has no public models API (all probed endpoints 404). The monitor watches
two docs pages for backticked model IDs. **Promo rotations that don't touch
these pages are invisible** — no API, no ID list exists publicly. This is an
accepted blind spot, not a bug.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Normal (incl. alerts sent, no diffs, alive ping) |
| 1 | Partial provider failures — any mode, incl. `--init` / first-run (still completes) — or bootstrap refused |
| 2 | Fatal/unhandled exception — check stderr |

## Testing

```bash
python3 -m pytest tests/ -v
```

Tests across envfile, providers, state, diffing, cooldown, notify, confirm,
and full integration (stubbed providers through the complete tick loop).