# free-inference-watchdog

Zero-token cron watchdog for six free-tier LLM gateways. Alerts Discord when a
free model appears or disappears. Stdlib-only Python, one tick per invocation,
no LLM calls ever.

Providers are **config-driven**: each gateway is a JSON file under
[`providers/`](providers/). Adding or removing a provider is a file drop, not a
code change.

## What it does

Every hour (cadence comes from the cron schedule; `--recheck-delay` only
sets the ~3-minute confirm nap before a diff is believed), the monitor:

1. Loads every `providers/*.json` config and fetches free-model rosters from
   **Nous**, **TokenRouter**, **Kilo**, **OpenRouter**, **AMD**, and **B.AI**
   (in that display order). Which ids count as free is decided per provider by
   its `detection` method (see [Architecture](#architecture)).
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

OpenRouter needs no key — its models endpoint is public. The code treats the
TokenRouter/Kilo/AMD keys as optional too (missing key ⇒ fetch with no auth
header), so those watchdog paths work with neither set.

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
| `nous_ratelimit` | Passive x-ratelimit headers from Nous (`{}` if Nous failed) |
| `tick_epoch` | Unix epoch of this tick |

All per-tick fields are **rebuilt** (never appended to). The only persistent
counter is `dropped_alerts_total` in `alive.json`, surfaced by the alive ping.

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

Detection methods (dispatched by string key, so a provider can pick any):

- `api-pricing` — model is free when `pricing.prompt == "0"` AND `pricing.completion == "0"` (Nous, OpenRouter).
- `api-flag` — model is free when `isFree == true` (Kilo).
- `id-suffix` — model id ends with `:free` / `-free`, or contains `free` (TokenRouter).
- `all-free` — every model in the catalog is treated as free (AMD).
- `zero-credit-probe` — fire a 1-token completion per model and classify by the
  response (B.AI). This is slow, so probes run concurrently (3 workers, 30s
  timeout) and results are deferred rather than blocking a tick.

### Modules

- **`config_loader.py`** — loads and validates every `providers/*.json`,
  resolves auth (`env_var` / `token_file` / `none`) into an in-memory token,
  and sorts configs by `display` order. Exposes the `PROVIDERS` dict used by the
  rest of the watchdog, plus `build_gateway_wiring()`.
- **`detection.py`** — string-keyed `detect_free(model, method)` dispatch used
  at fetch time to decide which ids count as free.
- **`probe_zero_credit.py`** — the `zero-credit-probe` backend: fires a
  1-token completion per model and classifies it as `free` / `paid` / `defer`
  based on the HTTP response (a `403` mentioning "deposit" ⇒ paid).

To add a provider, drop in a JSON config (see `providers/README.md`); to change
a detection strategy, edit the JSON — no Python changes required.

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

Tests across envfile, providers, state, diffing, cooldown, notify, confirm,
and full integration (stubbed providers through the complete tick loop).