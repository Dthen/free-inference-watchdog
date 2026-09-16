# free-inference-watchdog

Zero-token cron watchdog for the free-tier LLM gateways in `providers/`.
Alerts Discord when a free model appears or disappears. Stdlib-only Python,
one tick per invocation, no LLM calls ever.

Providers are **config-driven**: each gateway is a JSON file under
[`providers/`](providers/). Adding or removing a provider is a file drop, not a
code change.

## What it does

Every hour (cadence comes from the cron schedule; `--recheck-delay` only
sets the ~3-minute confirm nap before a diff is believed), the monitor:

1. Loads every `providers/*.json` config and fetches free-model rosters
   from every configured gateway. Which ids count as free is decided per
   provider by its `detection` method (see [Architecture](#architecture)).
2. Carries forward last-known-good IDs on provider failure (sticky silence —
   an outage never looks like a mass removal).
3. Set-diffs against the previous `roster.json`.
4. Re-fetches affected providers ~3 min later to confirm (kills transient flaps).
5. Delivers the alert via the `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` webhook
   (kennel channel). Failed POSTs queue in `state/pending_alerts.json` and
   retry automatically on the next tick. Stdout stays local — the process is
   silent unless something goes catastrophically wrong (stderr).
6. Writes an alive ping to silence the "is it dead?" question.

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
cp .env.example .env                    # then fill in your keys
python3 inference_watchdog.py --dry-run          # see what would happen
python3 inference_watchdog.py --init             # bootstrap roster.json
python3 inference_watchdog.py                    # one tick (cron does this)
```

## Cron registration

The monitor runs as a silent Hermes cron script-mode job (`--no-agent`: no LLM
is woken — the wrapper script IS the job). It is **not** a delivery channel:
the webhook in `.env` delivers alerts; cron stdout stays local.

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

Secrets live in a **project-local `.env`** (gitignored) — not in a shared
home/profile env file. This keeps the repo agent-agnostic: copy
[`.env.example`](.env.example) to `.env` and fill in your keys the same way on
any host.

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` | yes (for alerts) | Kennel/alerts channel webhook — the only delivery path. |
| `NOUS_AUTH_FILE` | yes (for Nous) | Path to the auth JSON file Hermes refreshes; `providers.nous.access_token` is read from it at tick time (see [Nous auth](#nous-auth)). |
| `TOKENROUTER_API_KEY` | no | TokenRouter gateway auth. |
| `KILOCODE_API_KEY` | no | Kilo fetcher — endpoint also serves its roster keyless; a key buys authenticated/higher-limit access. |
| `AMD_API_KEY` | no | AMD Radeon gateway auth. |
| `BAI_API_KEY` | no | B.AI gateway auth — required for the `zero-credit-probe` detection method. |
| `NVIDIA_API_KEY` | no | NVIDIA NIM gateway auth — on this box the key lives in `~/.hermes/.env`, not the project `.env`. |

OpenRouter needs no key — its models endpoint is public. The code treats the
TokenRouter/Kilo/AMD/NIM keys as optional too (missing key ⇒ fetch with no
auth header), so those watchdog paths work with neither set.

### Webhook rotation

1. Update `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` in `.env`.
2. Undelivered alerts queue in `state/pending_alerts.json` — drain manually:

```bash
python3 -m json.tool state/pending_alerts.json    # inspect queue
```

The queue auto-drains on the next successful tick.

### Nous auth

Nous auth is a **path pointer, never a token copy**. The watchdog reads the
token from a JSON file at tick time — the file Hermes keeps refreshed — so the
token is always fresh and never stale-copied into this repo. Set the env var
`NOUS_AUTH_FILE` to the path of that file, and the committed `providers/nous.json` resolves
`providers.nous.access_token` from it:

```bash
# in .env
NOUS_AUTH_FILE=~/.hermes/auth.json
```

The pointer is read at tick time, so a mid-token expiry just looks like a
provider failure (sticky carry-forward) — the next tick picks up the fresh
token naturally. A different deployment points `NOUS_AUTH_FILE` elsewhere and
the repo stays agent-agnostic.

## Roster.json fields

| Field | Meaning |
|---|---|
| `providers` | `{name: [ids]}` — current known-free model IDs per provider |
| `stale_providers` | Provider names whose fetch failed this tick (carried forward) |
| `transients` | Rebuilt every tick. Diffs that appeared then vanished on recheck. |
| `unconfirmed` | Rebuilt every tick. Diffs whose recheck itself failed (signal may resurface). |
| `ratelimits` | Per-gateway passive x-ratelimit headers from this tick (`{}` for succeeded gateways with none; absent = fetch failed) |
| `tick_epoch` | Unix epoch of this tick |

All per-tick fields are **rebuilt** (never appended to). The only persistent
counter is `dropped_alerts_total` in `alive.json`, surfaced by the alive ping.

## State layout (state/, gitignored)

- `roster.json`: providers + tick_epoch + stale_providers + transients + unconfirmed + ratelimits. Never hand-edit — use --init.
- `alive.json`: last_tick_epoch + last_output_epoch + dropped_alerts_total.
- `pending_alerts.json`: bounded retry queue (MAX_ATTEMPTS 5 per alert).
- `pending_removals.json`: held provider removals waiting to settle (shared by both settle paths).
- `probe_state.json`: per-provider probe verdicts (`{provider: {model_id: {"verdict": "free"|"paid", "epoch": int}}}`). Written once per tick after the serial probe loop. Never hand-edit.
- `state/monitor.lock`: PID lockfile; stale locks (>30 min old) are auto-broken on the next invocation (crash recovery).

## Held removals (opt-in per providers)

A provider config may set `removal_hold_seconds`. When present, a confirmed removal is held that long in `state/pending_removals.json` before an alert is emitted — letting a transient fetch failure or a quick re-add self-silence. Absent, removals alert instantly.

Two settle paths drain the queue; both share `pending_removals.settle` (never forked):
- **Resolver accelerates**: an in-pass resolver resolves holds as soon as their deadline elapses within the tick.
- **Hourly tick is the durable fallback**: if the resolver missed the window (crash, long fetch), the next hourly pass resolves the remaining holds.

Recovery is silent in both directions: a model that returns before the hold expires settles as RECOVERED with no announcement. News alerts are bounded at `hold + 15 min` — a removal past its deadline cannot wait longer.

Expiry is at-least-once. The one edge case: expiry fired-but-unconsumed (crash between the two saves) + model returns before the next pass → the return settles as RECOVERED, silently. An announced removal whose return goes unannounced. Accepted: bounded by one process crash in a ~emit-sized window.

## Drop-a-provider / managing providers

Providers are plain JSON config files in `providers/`. The watchdog loads every
`*.json` at startup.

- **To add a provider**: drop a new JSON config file into `providers/`.
- **To remove a provider**: delete its JSON file — it silently disappears on
  the next tick (the loader only materializes configs that exist on disk).

See [`providers/README.md`](providers/README.md) for the full schema and
detection-method reference.

## Architecture

The watchdog is **config-driven**: none of the gateways are hard-coded in the
monitor logic. Each provider is a `providers/*.json` file whose schema the
loader validates at startup:

| Field | Meaning |
|---|---|
| `name` | Human-readable name |
| `base_url` | API base URL (no trailing `/`) |
| `detection` | Which free-model detection method to apply |
| `auth.method` | `env_var`, `token_file`, or `none` |
| `auth.env_key` | Env var name (when `auth.method` is `env_var`) |
| `auth.path_env` | Env var holding a path to a JSON token file (when `auth.method` is `token_file`; takes precedence over `auth.path`) |
| `auth.path` | Literal path to a JSON token file (legacy `token_file` alternative to `path_env`) |
| `auth.key` | Dot-separated JSON path to the token inside the file (e.g. `providers.nous.access_token`) |
| `display` | Column order (0 = first) |
| `removal_hold_seconds` | Optional; when present, confirmed removals are held this many seconds before alerting. Absent = instant. Opt-in per provider. |

Detection methods (dispatched by string key, so a provider can pick any):

- `api-pricing` — model is free when `pricing.prompt == "0"` AND `pricing.completion == "0"`.
- `api-flag` — model is free when `isFree == true`.
- `id-suffix` — model id ends with `:free` / `-free`, or contains `free`.
- `all-free` — every model in the catalog is treated as free.
- `zero-credit-probe` — fire a minimal 3-token completion per model and classify by the
  response. Probes run **serially with 5s spacing** (12 RPM) to stay
  under b.ai's undocumented rate limits (operator pacing choice
  2026-09-13); verdicts are persisted to
  `probe_state.json` and the roster is verdict-filtered (ONLY FREE-verdict
  models). A DEFER never overwrites a prior verdict (sticky roster survives
  burst 429s). The probe phase is capped at 260s
  (`PROBE_PHASE_BUDGET_S`). The budget clock starts at the top of
  `fetch_all()`, so catalog fetches run INSIDE the 260s; worst case to the
  `save_probe_state` persist point is ~295s (the last probe can start at
  259.9s and overshoot by sleep(5) + 30s timeout). The cron runner SIGKILLs
  the wrapper at 300s (briefly raised to 1800s on 2026-09-13, then restored
  to 300s the same day). One tick carries ~45 of 47 models at 5s operator
  pacing (46×5s sleeps + 47×~1s probes ≈ 280s exceeds the 260s budget);
  the short tail self-heals next tick via `probe_state.json` persistence
  (the deferred models re-queue as tier-1 arrivals). Even the pathological
  overshoot (~295s) lands the spiral-critical persist under the 300s kill.
  The full
  tick (persist + the unconditional 180s recheck nap) can exceed 300s — the
  invariant is that the persist precedes any kill: a kill during the
  recheck costs that tick's roster write (roster lags one tick), never
  probe progress. A truncated pass (the normal every-tick tail at 5s
  pacing, or deeper if probes burn their 30s
  timeouts) persists its probed subset and the next tick resumes
  (self-healing).

### Modules

- **`config_loader.py`** — loads and validates every `providers/*.json`,
  resolves auth (`env_var` / `token_file` / `none`) into an in-memory token,
  and sorts configs by `display` order. Exposes the `PROVIDERS` dict used by the
  rest of the watchdog, plus `build_gateway_wiring()`.
- **`detection.py`** — string-keyed `detect_free(model, method)` dispatch used
  at fetch time to decide which ids count as free.
- **`probe_zero_credit.py`** — the `zero-credit-probe` backend: fires a
  minimal 3-token completion per model and classifies it as `free` / `paid` / `defer`
  based on the HTTP response (a `403` mentioning "deposit" ⇒ paid).
- **`probe_state.py`** — persisted zero-credit probe verdicts. Atomic write
  pattern: `load_probe_state`, `record_verdict`, `get_verdict`,
  `drop_missing`, `save_probe_state`. DEFER never recorded as a verdict.
- **`probe_select.py`** — pure probe-queue selection: `select_queue(catalog_ids,
  provider_state, now, stale_hours=24)` returns ordered list [new arrivals →
  free (every tick) → stale paid (>=24h, oldest first)].

To add a provider, drop in a JSON config (see `providers/README.md`); to change
a detection strategy, edit the JSON — no Python changes required.

### Zero-credit probe (B.AI)

Black-box gateways with no free-tier metadata get probed, not parsed. The
`zero-credit-probe` bullet above and the `probe_*.py` docstrings carry the
rationale; this is the operator cheat-sheet.

- **State file** — `state/probe_state.json`; schema as in the State layout
  above (verdict + epoch per model, optional `defer_epoch`); corrupt/missing
  reads as `{}`, written atomically (temp file + `os.replace`).
- **Verdicts** — HTTP 200 ⇒ `free`. `403` whose body mentions "deposit", or
  `400` carrying an insufficient-balance/quota marker ⇒ `paid`. Everything
  else — `429`, 404, 5xx, an unmarked `400`, network errors — ⇒ `DEFER`: no
  verdict is recorded, any prior verdict stands (sticky), and a model left
  without one re-queues next tick. Rate-limit pain shows up only as
  transient 429 defers, never as a verdict flip.
- **Queue (`probe_select.select_queue`)** — each tick probes, in order:
  tier 1 new arrivals (no usable verdict yet), tier 2 every `free` model
  (a quiet flip to paid must be caught fast), tier 3 `paid` models last
  probed ≥ 24h ago (`stale_hours`, oldest first — a daily pass catches a
  promo going free). Paid models inside the 24h window are skipped.
- **Pacing/budget** — `PROBE_INTERVAL_S = 5` serial spacing (12 RPM),
  `PROBE_PHASE_BUDGET_S = 260` counted from the top of `fetch_all()`. At 5s
  one tick carries ~45 of 47 models; the tail resumes next tick as tier 1.
- **Where it runs** — the probe phase inside `fetch_all()`, before the
  verdict-filtered roster is returned. `save_probe_state` persists verdicts
  and catalog prunes once per tick, before the unconditional 180s recheck
  nap — the invariant is "persist precedes the 300s cron kill", so a crash
  costs at most one roster write, never probe progress. Note: `--dry-run`
  skips this persist too (probe verdicts from a dry run are not saved).

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

Tests across envfile, providers, state, diffing, notify, confirm,
and full integration (stubbed providers through the complete tick loop).