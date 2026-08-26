# free-inference-watchdog

Zero-token cron watchdog for six free-tier LLM gateways. Alerts Discord when a
free model appears or disappears. Stdlib-only Python, one tick per invocation,
no LLM calls ever.

## What it does

Every hour (cadence comes from the cron schedule; `--recheck-delay` only
sets the ~3-minute confirm nap before a diff is believed), the monitor:

1. Fetches free-model rosters from **Nous**, **OpenRouter**, **OpenCode Zen**
   (only ids containing "free", plus a small hand-maintained stealth
   allowlist — see [Zen free-only rule](#zen-free-only-rule)), **Kilo**,
   **Cline** (endpoint-primary, docs fallback), and **Command Code** (only
   ids containing "free" — the free lane is deal-structured).
2. Carries forward last-known-good IDs on provider failure (sticky silence —
   an outage never looks like a mass removal).
3. Set-diffs against the previous `roster.json`.
4. Re-fetches affected providers ~3 min later to confirm (kills transient flaps).
5. Applies a 12h dedup cooldown per `(provider, model, event)`.
6. Delivers the alert via the `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` webhook
   (kennel channel). Failed POSTs queue in `state/pending_alerts.json` and
   retry automatically on the next tick. Stdout stays local — the process is
   silent unless something goes catastrophically wrong (stderr).
7. Writes an alive ping to silence the "is it dead?" question.

## Why no Ollama?

Ollama Cloud has no free-model concept to track. Cloud usage is metered by
GPU-time against account plans ($0 Free / $20 Pro / $100 Max) rather than
per-model pricing — every cloud model burns the same quota currency, larger
models are gated behind paid plans, and none of this is exposed via the API.
A "free roster" is therefore undefinable for Ollama, so the provider was
dropped entirely (2026-08-25).

## Quick start

```bash
cd ~/projects/free-inference-watchdog
python3 inference_watchdog.py --dry-run          # see what would happen
python3 inference_watchdog.py --init             # bootstrap roster.json
python3 inference_watchdog.py                    # one tick (cron does this)
```

## Cron registration

The monitor runs as a silent Hermes cron script-mode job (`--no-agent`: no LLM
is woken — the wrapper script IS the job). It is **not** a delivery channel:
the webhook in `~/.hermes/.env` delivers alerts; cron stdout stays local.

Create `~/.hermes/scripts/inference-watchdog-tick.sh`:

```bash
cd /home/kimbo/projects/free-inference-watchdog || { echo "inference-watchdog FAILED (cannot cd)" >&2; exit 1; }
python3 inference_watchdog.py || { c=$?; [ "$c" -eq 1 ] || { echo "inference-watchdog FAILED (exit $c)" >&2; exit "$c"; }; }
```

Then register it on the cadence (the schedule below is what sets the 1-hour
tick — there is no cadence flag on the job itself):

```bash
hermes cron create "17 */1 * * *" \
  --name inference-watchdog-tick \
  --script inference-watchdog-tick.sh \
  --no-agent \
  --deliver local
```

The wrapper's two stages exist to keep failure diagnosable without spamming
Discord: under bash a failed `cd` IS exit 1, so chaining `cd && python3` into
one exemption clause would hide a moved/renamed install dir forever — hence
the split. A bare exit 1 from the monitor itself is a routine partial outage
(the carried-forward "fetch failed" line in that tick's alert already says
which provider flaked), so it stays silent; anything else exits non-zero and
lands on stderr for the operator to find.

## Environment variables

All secrets live in `~/.hermes/.env`:

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` | yes (for alerts) | Kennel/alerts channel webhook — the only delivery path. |
| `OPENCODE_ZEN_API_KEY` | no | OpenCode Zen fetcher — endpoint serves its roster keyless (verified HTTP 200); a key buys higher rate limits. |
| `KILOCODE_API_KEY` | no | Kilo fetcher — endpoint also serves its roster keyless (verified HTTP 200); a key buys authenticated/higher-limit access. |

OpenRouter needs no key — its models endpoint is public. Cline's roster
endpoint is also public (no auth header); no key is read for it either. The
code treats the Zen/Kilo keys as optional too (missing key ⇒ fetch with no
auth header), so both watchdog paths work with neither set.

### Webhook rotation

1. Update `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` in `~/.hermes/.env`.
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

### Zen free-only rule

Zen exposes no pricing metadata, so its roster is filtered at fetch time: an id
is tracked iff it contains `free` (case-insensitive, anywhere in the id) OR is
on `ZEN_STEALTH_ALLOWLIST` in `providers.py` (currently just `big-pickle`).
Stealth models ship under opaque ids — a NEW stealth arrival needs a one-line
allowlist addition. Everything else on Zen is a paid tier and must never be
tracked. (This also supersedes the fetch-time passthrough older zen tests
pinned; their fixtures now carry marked ids.) Duplicate ids are deduplicated
at fetch time (a repeated id must not double-fire alerts), and non-string ids
are dropped outright rather than coerced — unlike nous, openrouter, and kilo,
whose coerce-then-filter contract is pinned by
test_fetch_mixed_int_and_str_ids_coerced.

## Cadence change

The tick cadence defaults to 1 hour. To change it:

1. Update the cron schedule to match (`hermes cron edit <job_id> --schedule "..."`).
2. Pass `--cadence-hours N` (default 1) on the invocation — it drives the
   ⚠️ missed-tick warning, which fires when the last tick is older than
   cadence + 2h slack. Keep the flag in step with the cron schedule or the
   warning will cry wolf.

The 💚 alive ping fires on output-age ≥ 20h regardless of cadence. Both
derive from `alive.json` — no restart needed.

## Manual lockfile recovery

If the monitor crashes without releasing its PID lock:

```bash
# Verify no monitor is actually running:
pgrep -f inference_watchdog.py

# If nothing holds it:
rm state/monitor.lock
```

Stale locks (>30 min old) are automatically broken on the next invocation.

## --init re-baseline

Running `--init` archives any existing `roster.json` to `roster.json.bak`
before clean-rebaselining. It always prints "initialized, no diff" and never
alerts. A prior `roster.json.bak` is overwritten by each successful init.
Safe to re-run at any time.

## Cline endpoint-primary with docs fallback

Cline's primary source is the public roster endpoint
`GET https://api.cline.bot/api/v1/ai/cline/recommended-models` (no auth
header); the free-roster tracked is the endpoint's `free[]` id list. The
free-models docs page (`getting-started/free-models.md`) remains a SECONDARY
fallback used only when the endpoint fails; if both sources fail the tick
reports a loud fetch failure (sticky carry-forward), never a mass removal.
(The all-models catalog page is deliberately NOT a fallback: it documents
paid tiers, and scraping it would swap paid ids into the roster on every
endpoint outage.)

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