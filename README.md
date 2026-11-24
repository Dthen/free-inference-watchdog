# free-inference-watchdog

A free-tier LLM model catalogue for MCP clients — plus the zero-token watchdog
that keeps it honest. The catalogue is a credential-free snapshot of which
models each gateway currently gives away; the watchdog is the cron monitor that
tracks the changes and alerts on them.

**Dashboard:** <https://models.dthen.xyz> — the catalogue, rendered, rebuilt
each tick.

Stdlib-only Python, one tick per invocation, no LLM calls ever. Providers are
**config-driven**: each gateway is a JSON file under [`providers/`](providers/),
so adding or removing one is a file drop, not a code change.

| You want to… | Start at |
|---|---|
| query the free-model catalogue from an MCP client | [Query the catalogue over MCP](#query-the-catalogue-over-mcp) — no keys, no cron, no local roster |
| use the same three tools inside a browser page | [Browser (WebMCP) tools in the dashboard](#browser-webmcp-tools-in-the-dashboard) — the page's loaded snapshot |
| run the watchdog yourself, alert on changes, self-host | [Self-hosting the watchdog](#self-hosting-the-watchdog) — provider keys, Discord webhook, cron host |

## Query the catalogue over MCP

`free-inference-watchdog-mcp` is a read-only MCP server over the published
catalogue, speaking MCP **stdio**. It exposes three tools — `search_models`,
`get_model` and `watchdog_status` — and answers from one validated snapshot.

A consumer needs Python 3.11+ and an MCP client that can launch a local stdio
server. It does **not** need provider API keys, a Discord webhook, a cron job,
`providers/`, `state/`, the watchdog tick, this checkout, or the Hermes agent:
the installed package carries the query core, the reviewed tool contract, the
stdio server and the one-shot native adapter, and nothing else.

Querying is not inference. Reading the catalogue never calls a provider, never
spends a credit and never grants access to a model; to run one you need your
own credentials with that gateway. This is also not a hosted endpoint — the
server is a local process your client launches over stdio, and this ref exposes
no remotely connectable MCP HTTP URL. Browser agents are served by a different
artifact: the generated page registers the same three read-only tools with the
browser's in-page WebMCP API, within the limits in
[the browser (WebMCP) tools](#browser-webmcp-tools-in-the-dashboard). That page
is rendered HTML, not the JSON feed.

### Install (0.2.0)

Build 0.2.0 from this checkout. The legacy 0.1.0 release has a different API.

```bash
# from this checkout root
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
python -m watchdog_catalog --help          # MCP stdio server
python -m watchdog_catalog.cli --help      # one-shot native command
```

The wheel declares two console commands: `free-inference-watchdog-mcp`
(equivalently `python3 -m watchdog_catalog`) and the one-shot
`free-inference-watchdog` (equivalently `python3 -m watchdog_catalog.cli`; see
[Native command](#native-command)). `--help` needs no catalogue, credentials or
network. A client that does not inherit this shell environment needs an absolute
`command` path: expand the checkout-relative
`.venv/bin/free-inference-watchdog-mcp`.

**Legacy release — a different API.** Published `0.1.0` at commit
`bd03dcbd8edcf72ceab0a322891fffc6ecb53ecc` has five tools over v1 snapshots:
`list_free_models`, `list_endpoints`, marker-group `get_model(model_id)`,
`watchdog_status` and `search_models` (default page: 50). The instructions below
are for 0.2.0, not that release.

### Choose the catalogue source

One source is selected per launch, by a plain flag:

| Selection | Meaning |
|---|---|
| `--catalog-url URL` | read the catalogue from this literal `http(s)` URL |
| `--catalog-file PATH` | read the catalogue from this literal local JSON file |
| neither | the published remote feed — the default source, rebuilt each tick |
| `--cache-dir DIR` | optional; where the validated disk cache is kept |

`--catalog-url` and `--catalog-file` are mutually exclusive, and the path or URL
is used literally — no tilde expansion, no trimming, no substitution. With no
source flag the server reads
`https://raw.githubusercontent.com/Dthen/free-inference-watchdog/master/site/catalog.json`.
The Pages copy at <https://models.dthen.xyz/catalog.json> is the same document;
pass it to `--catalog-url` explicitly if you prefer it. It is a mirror, never an
implicit failover — the server does not fall back to a second URL.

"Live" names the default URL; it is not a promise that the URL always answers.
With no validated snapshot cached yet, a failed fetch answers every tool call
with the fixed `source_unavailable` error — a missing feed is never reported as
an empty catalogue. A cached, validated last snapshot is served instead, with
its real age plus `refresh_failed` and `cache_fallback` warnings. The live
document also carries the producer's own `warnings` list (for example
`metadata_missing`), so read `source.warnings` before trusting freshness. A
published snapshot says nothing about provider health at the moment you read it.

**Version caution.** 0.2.x requires v2; 0.1.x requires v1. The validator reports
`unsupported_schema_version` for a mismatch; a consumer without a compatible
cached snapshot reports `source_unavailable`. Use `--catalog-file` to query a
local snapshot without contacting the public feed.

The portable part of every stdio client config is a named server with a
`command` and an `args` array; the outer key and file format vary per client.
The live default needs no source flag:

```json
{
  "mcpServers": {
    "free-inference-watchdog": {
      "command": "free-inference-watchdog-mcp",
      "args": []
    }
  }
}
```

To reproduce the synthetic walkthrough below against fixed bytes — or to work
offline — point the same server at this checkout's v2 fixture. It is a small
hand-authored catalogue with fake gateways (`synthetic-a`, `synthetic-b`,
`synthetic-c`): **synthetic preview, not live data**, and it does not describe
real models. The path is literal (no tilde expansion or substitution), so
replace it with the absolute path to this checkout's
`tests/fixtures/catalog/lookup.json`.

```json
{
  "mcpServers": {
    "free-inference-watchdog": {
      "command": "free-inference-watchdog-mcp",
      "args": [
        "--catalog-file",
        "/absolute/path/to/free-inference-watchdog/tests/fixtures/catalog/lookup.json"
      ]
    }
  }
}
```

If your client does not inherit your shell `PATH`, give `command` as the
absolute path to the installed console. To use a different v2 snapshot, swap
the path; never pass `--catalog-file` and `--catalog-url` together.

### Native command

`free-inference-watchdog` runs one operation per process over the same source
and query core. It parses the command first, acquires one snapshot, answers once
and exits; it never starts the stdio transport or imports the MCP SDK.

| Command | Operation |
|---|---|
| `free-inference-watchdog search [QUERY] [OPTIONS]` | `search_models` |
| `free-inference-watchdog get PROVIDER MODEL_ID [OPTIONS]` | `get_model` |
| `free-inference-watchdog status [OPTIONS]` | `watchdog_status` |

`--json`, the mutually exclusive `--catalog-file`/`--catalog-url` pair and
`--cache-dir` apply to all three. `search` also takes `--provider`,
`--min-context-length`, repeatable `--input-modality` and
`--supported-parameter`, `--limit` and `--offset`. Values are literal: omit a
filter to leave it unconstrained, and pass `--` before a positional value that
begins with a dash.

By default the command prints plain text rendering exactly the same result —
every returned row or detail field, the real source freshness and any
cache-fallback warnings, with `unknown` distinct from a known empty value and
terminal control characters escaped for display only. `--json` prints the
complete UTF-8 JSON document and also selects JSON errors; without it, errors
are diagnostics on stderr. Exit status is 0 for every successful query
(including no matches or a stale snapshot), 2 for a malformed invocation or an
invalid/unknown argument, 1 for `not_found`, an unavailable source or an output
failure, and 130 when interrupted.

```bash
python3 -m watchdog_catalog.cli search "reasoning" --catalog-file tests/fixtures/catalog/search-text.json --limit 1
python3 -m watchdog_catalog.cli search "reasoning" --catalog-file tests/fixtures/catalog/search-text.json --limit 1 --json
```

`free-inference-watchdog` is the installed spelling of these commands;
`python3 -m watchdog_catalog.cli` is the checkout spelling. Both accept the same
operations, flags and argument order.

Every invocation is a fresh process and always **attempts a refresh** of the
selected source. With a URL source, a slow or unreachable feed is only found at
run time: the attempt may wait for the existing **socket-operation timeout** (10
seconds by default) before a validated snapshot from `--cache-dir` is served as
last-good fallback with `refresh_failed` and `cache_fallback`. A cached snapshot
is therefore **not a guaranteed offline read** — no flag skips the refresh — and
that timeout bounds individual blocking socket operations, not the whole
command.

### A worked example (synthetic preview, not live data)

The output below is `search_models({"query": "shared"})` from this checkout's
0.2.0 candidate over a real MCP stdio session against that fixture
(`source.mode` is `file`). Values are
**selected fields, not a complete document**: `source` is trimmed to its stable
provenance fields and the per-source timestamps are omitted.

```json
{
  "ok": true,
  "total_matches": 2,
  "next_offset": null,
  "provider_staleness": {
    "synthetic-a": false,
    "synthetic-b": false,
    "synthetic-c": false
  },
  "endpoints": [
    {
      "provider": "synthetic-a",
      "model_id": "vendor/shared",
      "name": "Vendor Shared",
      "context_length": 131072,
      "input_modalities": ["text"],
      "output_modalities": ["text"],
      "supported_parameters": ["temperature", "max_tokens"]
    },
    {
      "provider": "synthetic-b",
      "model_id": "vendor/shared",
      "name": "Vendor Shared",
      "context_length": 32768,
      "input_modalities": ["text", "image"],
      "output_modalities": ["text"],
      "supported_parameters": ["temperature", "top_p"]
    }
  ],
  "source": {
    "mode": "file",
    "cached": false,
    "observation_fresh": false,
    "warnings": ["tick_stale"]
  }
}
```

One raw id tracked by two gateways is one search row per gateway: two endpoints,
one distinct raw id. Rows are compact by design; call `get_model` for a
description or wiring URL. Read the `source` block as provenance, not payload:
`ok: true` plus a `source` block means a validated snapshot was returned — not
that this launch's refresh succeeded. A last-known-good replay also returns
`ok: true`, but it is not the same block: `source.cached` turns true,
`source.not_modified` stays false, the fetch and observation ages keep counting
from the original fetch and observation, and `refresh_failed` plus
`cache_fallback` mark a failed refresh served from memory. A stale observation
is still answered; it is just labelled.

`get_model(provider, model_id)` requires **both** an exact registered provider
key and a **literal raw id** tracked by that provider. It returns one flat
details endpoint — the same capability values plus `description`,
`chat_completions_url` and `api_type` — and scopes `provider_staleness` to that
provider. `get_model("synthetic-a", "vendor/model")` is `not_found` even though
synthetic-a tracks `vendor/model:free`: there is no marker stripping and no
cross-provider lookup. A known provider with an absent exact id is `not_found`;
an unregistered provider is `unknown_provider`.

### Metadata search semantics

`search_models` takes seven optional arguments; the active ones combine with
**AND**, tested on each `(provider, raw model_id)` pair on its own, never on a
merged view of two gateways.

- `query` matches a **literal substring** of the raw id, name or description,
  ASCII case-insensitive only (A–Z folds to a–z; every other code point stays
  literal). There is no regular expression, fuzzy or alias matching; capability
  arrays, provider keys and object keys are never text-searched. Omitted or `""`
  applies no text constraint; a whitespace-only query is a literal one. Queries
  and ids are never trimmed.
- `provider` takes **one exact key** from the snapshot's `provider_order`. A
  registered gateway that tracks zero ids is valid and answers with an empty
  page; an empty, padded, wrong-case or unknown key is the `unknown_provider`
  argument error — keys are never repaired.
- `min_context_length` compares **inclusively** (`>=`) against each pair's own
  known context. An unknown (`null`) context fails the filter rather than
  counting as zero; `0` keeps a known `0` and still rejects the unknown one.
- `input_modalities` and `supported_parameters` take arrays of non-empty
  strings. **Every** requested entry must be present in that same endpoint's
  validated array — AND, never OR — compared ASCII case-insensitively, with
  order and duplicates irrelevant. A missing or malformed array is unknown and
  fails a non-empty request; a known `[]` satisfies none; `[]` applies no
  constraint.
- Unknown metadata fails a filter that tests it, but never removes an endpoint
  from a search that does not test that field.
- `limit` is an integer 1–200 (default **5**) and `offset` an integer `>= 0`
  (default 0). Sorting happens first and the page is a slice of the sorted
  matches, so paging narrows what is *returned*, never what is *matched*:
  `total_matches` counts every match before the slice, and `next_offset` is
  `offset + len(endpoints)` while matches remain, else `null`.
- Rows are compact: `provider`, `model_id`, `name`, `context_length`,
  `input_modalities`, `output_modalities` and `supported_parameters`, with
  `description`, `chat_completions_url` and `api_type` only on `get_model`
  details. There are no per-row `stale` flags, `metadata` objects, `counts`,
  `caveats` or request echoes.

A worked filter progression over `lookup.json`, one filter added per row.
`none` means no endpoint satisfied every argument in that row:

| Arguments | Matched endpoints (`provider / raw id`) |
|---|---|
| `{"query": "shared"}` | `synthetic-a / vendor/shared`, `synthetic-b / vendor/shared` |
| `{"query": "shared", "min_context_length": 65536}` | `synthetic-a / vendor/shared` |
| `{"query": "shared", "input_modalities": ["image"]}` | `synthetic-b / vendor/shared` |
| `{"query": "shared", "min_context_length": 65536, "input_modalities": ["image"]}` | `none` |
| `{"query": "renamed", "min_context_length": 65536}` | `synthetic-a / vendor/model-renamed` |
| `{"query": "renamed", "min_context_length": 65537}` | `none` |

Row four is the point: synthetic-a serves `vendor/shared` with a 131072-token
context but text-only input, while synthetic-b serves the same raw id with image
input and only 32768 tokens. Each filter matches a different gateway and both
together match neither — a merged result would describe a model nobody serves.
The last two rows differ by one token and show the threshold is inclusive.

The same fixture, one page into a three-match search:

```json
{
  "ok": true,
  "total_matches": 3,
  "next_offset": 2,
  "endpoints": [
    {
      "provider": "synthetic-a",
      "model_id": "vendor/model:free",
      "name": "Vendor Model",
      "context_length": 131072,
      "input_modalities": ["text", "image"],
      "output_modalities": ["text"],
      "supported_parameters": ["temperature", "max_tokens"]
    }
  ]
}
```

Three endpoints match `model`, this page carries the second (`limit: 1,
offset: 1`), and `next_offset: 2` is where the next page starts. Ask for
`offset: 4` instead and the answer is still a success — empty `endpoints`,
`next_offset: null`. A default search returns at most five rows.

Full envelopes, ordering, warning vocabulary and error shapes are in
[Published catalogue snapshot (v2)](#published-catalogue-snapshot-v2).

### What the catalogue cannot tell you

- **Observation age comes from the snapshot; retrieval age comes from your
  process.** `watchdog_status()` derives `last_tick_age_s` and `tick_fresh`
  from `tick_epoch` and the snapshot's own `cadence_s`, while `source.fetched_at`
  and `source.fetch_age_s` only say how long ago this process fetched the bytes.
  A fresh download of an old `tick_epoch` is still stale.
- **Provider staleness is not tick age.** A provider can be stale during a
  fresh tick (its fetch failed and last-known-good ids were carried forward),
  and every provider can be healthy during an old one. "Not fresh" has three
  shapes: a `null` `tick_epoch` gives `last_tick_age_s: null` plus
  `tick_epoch_unknown`, a future one gives `tick_epoch_future`, and only a known
  age beyond `cadence_s` gives `tick_stale`. `ok: true` from
  `watchdog_status()` means the snapshot was queried, never that its
  observations are fresh.
- **Unknown is not zero, and not empty.** A `null` `context_length`, modality
  list or `supported_parameters` array means the provider did not say — it is
  never rendered as `0` or `[]`. A known `0` and a known `[]` are real values;
  neither payload adds an `unknown_fields` list, so the value itself is the only
  signal.
- **Identity is exact.** `get_model` requires one exact registered provider and
  one literal raw id: case, padding and delimiters matter. There is no marker
  stripping and no cross-provider relay — `vendor/model` does not resolve to
  `vendor/model:free`. Endpoint identity is `(provider, raw model_id)`, so the
  same raw id on two providers is two independent rows.
- **Marker labels remain a status count.** `watchdog_status.counts.
  marker_group_labels` counts distinct delimiter-stripped labels catalogue-wide;
  `vendor/model:free` and `vendor/model-free` contribute one label between them.
  It is a labelling count, never a count of verified underlying models, and no
  tool payload resolves a label back to endpoints.
- **Absence is weak evidence, and not an error.** Gateways rename models
  inconsistently, so a missing exact id does not prove the model is absent
  elsewhere. `get_model` says only `not_found` for that provider and never
  echoes the caller's value.
- **A cached answer says so.** `source.cached` is `true` whenever the body came
  from retained memory instead of a fresh download, `source.not_modified` marks
  a successful 304 revalidation, `cache_write_failed` marks a snapshot served
  but not persisted, and `refresh_failed` + `cache_fallback` mark a **failed**
  refresh served from the last good copy. There is no implicit fallback to
  another URL or to a local file, and a failure is never reported as an empty
  catalogue.
- **Operator state is not observable here.** `watchdog_status()` reports
  `pending_alerts`, `site_published` and `site_publish_age_s` as null: a
  published or browser source cannot see the operator's alert queue or site, and
  the payload never guesses. `metadata_coverage` counts stated metadata fields
  only, never provider health.

### Public JSON feed

The catalogue is one public JSON document (`site/catalog.json`):
`schema_version` (2 in this checkout), an integrity-hashed `snapshot_id`, the
observation epoch `tick_epoch`, the `cadence_s` it was measured against,
`provider_order`, a `providers` map of
credential-free projected metadata, and a frozen `warnings` list. It is the
contract the MCP server and the dashboard's in-page tools consume. Both routes
serve the same document, rebuilt and published by the tick:

| Route | Role |
|---|---|
| GitHub raw, `site/catalog.json` | the default: what a launch with no source flag reads |
| <https://models.dthen.xyz/catalog.json> | explicit `--catalog-url` alternative; never an automatic fallback |

The dashboard at <https://models.dthen.xyz> is rendered HTML, not the feed —
same catalogue, different artifact. `snapshot_id` and `tick_epoch` move with
every publication, so read them from the document you fetched. Any URL serving a
valid snapshot works with `--catalog-url`, so your own served copy behaves the
same.

Reachability is not guaranteed: with no validated snapshot cached the tool call
answers `source_unavailable`, and a validated last snapshot is served from cache
with `refresh_failed`/`cache_fallback` and its real age. No provider is
contacted on any of those paths, and a readable snapshot is never a statement
about current gateway health.

### Browser (WebMCP) tools in the dashboard

The dashboard page carries the catalogue and the shared tool definitions as two
inert JSON islands and — when the browser exposes the in-page WebMCP API
(`document.modelContext.registerTool`) — registers the **same three read-only
tools**: `search_models`, `get_model` and `watchdog_status`. The maintained
`web/catalog-query.js` engine and `web/webmcp.js` adapter plus one bootstrap
call are inlined into `site/index.html` by the page builder. It is a progressive
enhancement: with no API, with JavaScript disabled, or after a failed
registration the ordinary matrix and its row expansion render as before.

**Availability is conditional — there is no universal browser support.**

| Situation | What is required |
|---|---|
| local development, or an ordinary visitor | Chrome with `chrome://flags/#enable-webmcp-testing` enabled and the browser relaunched |
| a public origin, without that flag | the origin enrolled in the WebMCP origin trial (from Chrome 149) |
| every case | a secure context: `https://`, `http://localhost`/`127.0.0.1`, or a `file://` open of the built page |
| a cross-origin iframe | the `tools` Permissions Policy delegated (`<iframe allow="tools">`); the default is `self` |

Enrolling a public origin in the trial is optional and separately authorized:
nothing in this repository, its build or its deploy path enrols one. Chrome
calls WebMCP a proposed standard under active change; see
[WebMCP](https://developer.chrome.com/docs/ai/webmcp), the
[Imperative API](https://developer.chrome.com/docs/ai/webmcp/imperative-api),
[tool security](https://developer.chrome.com/docs/ai/webmcp/secure-tools) and
the [DevTools WebMCP panel](https://developer.chrome.com/docs/devtools/application/webmcp).
The version you run may differ from the one described here.

Setup: open a page built from this ref (run the builder and open
`site/index.html`; a `file://` open works because the page is self-contained),
enable `chrome://flags/#enable-webmcp-testing` → **Enabled**, relaunch Chrome,
then confirm in DevTools → **Application** → **WebMCP** that *Available Tools*
lists the three tools. A page published from an older build has none; see
*Publication status*.

The browser tools answer from the snapshot embedded in the page when it loaded.
Arguments, payloads, ordering and errors are the ones documented above — no
browser-only fields and no browser-only truncation — with one addition: every
returned payload, success or argument error, carries `source: {"mode": "browser"}`.
Browser mode is not an HTTP refresh, a cache hit or a monitor tick: opening a
tab starts no monitoring job, no tick and no provider request, and the page
never asks for a provider key. Each descriptor also carries three advisory
hints — `readOnlyHint: true`, `untrustedContentHint: true` (upstream model text
is untrusted data) and `consequentialHint: false`. Hints inform the agent; they
never sanitize upstream text.

**Output size: a recommendation, not a cap.** Chrome's security guidance
*recommends* about 1.5K characters per individual tool output; it is not a
protocol limit. This page never truncates, drops fields or invents pagination to
fit it, and a `get_model` details payload with a large upstream description can
exceed that recommendation. Routine agent lookups belong in `search_models`
with a small `limit` (1–5), a provider filter where useful, then `get_model` for
details; `limit` bounds rows, never serialized characters.

WebMCP is an **in-page** browser API: it is **not** a remotely connectable MCP HTTP URL
and does not make this package a hosted server. The only transport this ref ships
is local stdio; the two routes share tool definitions and payload semantics, not
a process.

**Publication status.** The tracked `site/index.html` and the deployed dashboard
show the last page that was PUBLISHED; publishing a regenerated page is a
separate, separately authorized step, and a local build is never publication.
Every build from this ref carries the inlined `catalog-query`, `webmcp` and
bootstrap scripts as `data-watchdog` script islands, so read the copy in front
of you rather than this paragraph.

---

## Self-hosting the watchdog

Everything here is the operator path: run the monitor yourself, page a Discord
channel on changes, keep the schedule healthy. It needs provider credentials, a
webhook and a cron host; a catalogue consumer needs none of them.

### What it does

Every tick (hourly by default; the cron schedule sets the cadence and
`--recheck-delay` only sets the ~3-minute confirm nap):

1. Loads every `providers/*.json` config and fetches free-model rosters from
   each gateway. Which ids count as free is decided **per provider** by that
   provider's `detection` method.
2. Carries forward last-known-good ids on provider failure: a failed provider
   retains its last-known models; it does not create mass removals.
3. Set-diffs against the previous `roster.json`.
4. Re-fetches affected providers ~3 minutes later to confirm, killing
   transient flaps.
5. Delivers the alert through `DISCORD_WEBHOOK_INFERENCE_WATCHDOG`. Failed
   POSTs queue in `state/pending_alerts.json` and retry on the next tick.
6. Writes an alive ping. Stdout stays empty; diagnostics go to stderr.

### Quick start

Self-hosting steps, run from the clone — `.env`, `providers/` and `state/`
resolve from the working directory, not from wherever the shell started:

```bash
# EDIT ME: path to your clone. Quoted, so a path containing spaces works.
WATCHDOG_HOME="$HOME/projects/free-inference-watchdog"
cd "$WATCHDOG_HOME" || exit 1
cp .env.example .env                    # then fill in your keys
python3 inference_watchdog.py --dry-run          # see what would happen
python3 inference_watchdog.py --init             # bootstrap roster.json
python3 inference_watchdog.py                    # one tick (cron does this)
```

Change that one quoted line for a different checkout. Keep the quotes: an
unquoted path containing spaces splits into several arguments and the `cd`
fails. `WATCHDOG_HOME` is a shell variable in these snippets, not a watchdog
option — there is no path flag on the CLI, and `--state-dir` only relocates
`state/`.

### Cron registration

The monitor runs as a silent script-mode job (`--no-agent`: no LLM is woken —
the wrapper script IS the job). It is not a delivery channel: the webhook
delivers alerts, and a job's stdout would be delivered verbatim, so the wrappers
write nothing to stdout and every diagnostic goes to stderr.

Hermes cron is one scheduler that can run these wrappers; it is not a
prerequisite for consumers, and both wrappers are plain bash, so any scheduler
that runs a shell script on a clock drives the same jobs. A Hermes wrapper lives
in the **active Hermes home's** scripts directory — `$HERMES_HOME/scripts/`
(`~/.hermes/scripts/` on a default install) — and is registered by bare
filename: `--script` takes a name that must resolve inside that directory, never
a command line or a shell-relative path. Each wrapper therefore carries its own
clone path and interpreter; do not export them from your shell instead, because
the scheduler sanitizes the environment it hands a script.

Create `~/.hermes/scripts/inference-watchdog-tick.sh`:

```bash
#!/usr/bin/env bash
# inference-watchdog-tick.sh — one hourly tick. Silent on success and on the
# routine partial-outage status; diagnostics go to stderr, never stdout.
set -u

# EDIT ME: absolute path to your clone. Quoted, so a path containing spaces works.
WATCHDOG_HOME="$HOME/projects/free-inference-watchdog"
# EDIT ME only if `python3` on the scheduler's PATH is not the one you want.
PYTHON="python3"

# Stage 1 — directory selection. Under bash a failed `cd` is ALREADY exit 1, so
# folding it into stage 2's exit-1 exemption would report success for a moved or
# renamed checkout, forever. Diagnose here and never start the monitor.
cd "$WATCHDOG_HOME" || { echo "inference-watchdog FAILED (cannot cd to $WATCHDOG_HOME)" >&2; exit 1; }

# Stage 2 — one tick. Exit 0 is success; the routine partial-outage exit 1 is
# exempted and adds NO wrapper output (that tick's alert already names the
# provider that flaked); any other status is re-raised unchanged, with a note on
# stderr.
"$PYTHON" inference_watchdog.py || { c=$?; [ "$c" -eq 1 ] || { echo "inference-watchdog FAILED (exit $c)" >&2; exit "$c"; }; }
```

Register it, hourly for example:

```bash
hermes cron create "17 */1 * * *" \
  --name inference-watchdog-tick \
  --script inference-watchdog-tick.sh \
  --no-agent \
  --deliver local
```

`--deliver local` keeps job output in local files only, so the webhook stays the
single alert path. The two stages exist because bash reports a failed `cd` as
exit 1 — the same status a routine partial outage uses. One combined
`cd … && python3 …` clause could not tell them apart, so a moved checkout would
look like a normal quiet tick forever.

### Environment variables

Secrets live in a project-local `.env` (gitignored), not a shared profile file:

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_WEBHOOK_INFERENCE_WATCHDOG` | yes (for alerts) | Alerts channel webhook — the only delivery path. |
| `NOUS_AUTH_FILE` | yes (for Nous) | Path to the auth JSON file; `providers.nous.access_token` is read from it at tick time. |
| `TOKENROUTER_API_KEY` | no | TokenRouter gateway auth. |
| `KILOCODE_API_KEY` | no | Kilo auth; the endpoint also serves its roster keyless. |
| `AMD_API_KEY` | no | AMD Radeon gateway auth. |
| `BAI_API_KEY` | no | B.AI auth — required for the `zero-credit-probe` method. |
| `NVIDIA_API_KEY` | no | NVIDIA NIM auth. |

OpenRouter, Vercel AI Gateway, Felo, TokenRA, Infron and AI/ML API need no key. Missing TokenRouter/Kilo/AMD/NIM
keys mean the fetch sends no auth header, so those paths work unauthenticated.

Nous auth is a **path pointer, never a token copy**: `NOUS_AUTH_FILE` names a
JSON file Hermes keeps refreshed, and the token is read at tick time. A
mid-token expiry looks like a provider failure (sticky carry-forward) and the
next tick picks up the fresh token.

After rotating the webhook, inspect the queue with
`python3 -m json.tool state/pending_alerts.json`; it drains on the next
successful tick.

### Provider configuration

Each `providers/*.json` is validated at startup. A file the loader cannot use is
skipped with a `config: skipping ...` stderr warning and costs exactly that
provider.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `name` | string | yes | Human-readable name |
| `base_url` | string | yes | API base URL (no trailing `/`) |
| `detection` | string | yes | Free-model detection method for this provider |
| `catalog_url` | string | no | Absolute literal `https://` discovery URL (query allowed). Defaults to `base_url` + `/models`; never changes the published inference URL |
| `catalog_format` | string | no | Catalogue family used by `catalog-pricing`: `openai-models` (the default), `input-output-models`, `channel-catalog`, `newapi-pricing`, `provider-models` or `typed-unit-models`. Any other name is rejected |
| `auth` | object | yes | `{method: env_var\|token_file\|none, ...}` |
| `display` | int | no | Column order (0 = first; default 0) |
| `signup_url` | string | no | Signup link shown on the dashboard (only `https://` is linked) |
| `limits_note` | string | no | Free-tier limits footnote shown as the dashboard tooltip |
| `roster_key` | string | no | Roster-key override; defaults to the config file's stem |
| `ignored_slugs` | list of strings | no | Exact model ids excluded after detection |
| `probe` | object | no | Zero-credit-probe dialect: `max_tokens` + `paid_signals` |
| `removal_hold_seconds` | int | no | Hold confirmed removals this many seconds before alerting |
| `enabled` | bool | no | `false` untracks the provider while keeping the file. Must be a real JSON boolean |

Detection is per provider, dispatched by string key:

- `api-pricing` — free when `pricing.prompt` and `pricing.completion` are both
  zero.
- `api-flag` — free when `isFree == true`.
- `id-suffix` — free when the id ends with `:free`/`-free` or contains `free`.
- `all-free` — every model in the catalog is free.
- `zero-credit-probe` — fire a minimal completion per model and classify by
  response; only `free` verdicts enter the roster.
- `catalog-pricing` — tracks chat offers only when the selected adapter proves
  ordinary token and request charges are zero across applicable tiers and
  runnable routes. Missing or variable pricing is not a free quote.

| Gateway | `catalog_format` |
|---|---|
| [Vercel AI Gateway](providers/vercel.json) | `input-output-models` |
| [Felo](providers/felo.json) | `channel-catalog` |
| [TokenRA](providers/tokenra.json) | `newapi-pricing` |
| [Infron](providers/infron.json) | `provider-models` |
| [AI/ML API](providers/aimlapi.json) | `typed-unit-models` |

These five gateways use public JSON catalogues; monitoring needs no key or
completion. Zero-priced inference may still require funding or carry account
limits. Dated terms and known protocol/availability caveats are in each
config's `limits_note`, also shown in dashboard tooltips. A listing is not
proof of callability or unlimited use. AI/ML API's variable-priced campaigns
are not detected by its fixed-zero rule. Enabled gateways with no qualifying
models remain monitored but have no dashboard column.

`ignored_slugs` drops exact ids after detection (auto-router endpoints such as
`kilo-auto/free` that pass the rule but are not real models). `probe` moves a
gateway's probe dialect into its JSON: `max_tokens` (default 3) and
`paid_signals`, a list of `{status, all_of, any_of}` bodies meaning "no credit";
a matching signal verdicts the model PAID, anything else DEFERs and self-heals.
Probes run serially with 5s spacing, capped by `PROBE_PHASE_BUDGET_S` (260s)
counted from the top of `fetch_all()`. Verdicts persist to
`state/probe_state.json` before the recheck nap, so a truncated pass resumes
next tick and a kill costs at most one roster write.

`catalog_url` and `catalog_format` are validated per file when present. The URL
is stored and fetched literally — bytes, case and any query preserved — and is
used only for discovery: `chat_completions_url` is still derived from
`base_url`. A credential-bearing config (`env_var` or `token_file`) whose
`catalog_url` leaves the `base_url` origin is rejected before the credential is
resolved, and the fetcher refuses a nonempty token crossing scheme, host or
effective port even for a hand-built config. `ignored_slugs` still applies
after detection. Only `openai-models`, `input-output-models`,
`channel-catalog`, `newapi-pricing`, `provider-models` and `typed-unit-models`
are implemented today;
other gateway families are not registered, and naming one skips the config file
rather than polling it with the wrong rule.

Providers are file drops. Add one by adding a JSON file; untrack one with
`"enabled": false` (it disappears from the dashboard, alerts and provider list
on the next tick); remove one for good by deleting the file. A registry change
that only adds or removes a provider needs no `--init` — the roster loader drops
keys outside the registry and a new provider's models surface as additions.

### State and recovery

`state/` is gitignored. Files: `roster.json` (providers, `provider_models` raw
per-model metadata, tick epoch, stale/transient/unconfirmed sets, ratelimits —
never hand-edit),
`alive.json` (tick and output epochs, dropped-alert counter),
`pending_alerts.json` (bounded retry queue, 5 attempts per alert),
`pending_removals.json` (held removals), `probe_state.json` (probe verdicts) and
`monitor.lock` (PID lock).

A provider config may set `removal_hold_seconds`: a confirmed removal then waits
that long in `pending_removals.json` before alerting, so a transient failure or
a quick re-add self-silences. The hourly tick drains the queue; a 15-minute
`--resolve` pass accelerates it. Both paths share `pending_removals.settle` —
never fork it — and the resolver never touches `alive.json`, never rebaselines
and never fetches a provider. A model that returns before the hold expires
settles as recovered with no announcement. An expired hold alerts at hold + 15
minutes while the 15-minute resolve job is installed and scheduled; without
that job, the next tick drains it at the tick cadence. Orphaned provider entries
are dropped with a stderr note.

Create `~/.hermes/scripts/inference-watchdog-resolve.sh` — the tick wrapper with
`--resolve` added:

```bash
#!/usr/bin/env bash
# inference-watchdog-resolve.sh — drain the removal-hold queue every 15 minutes.
set -u

# EDIT ME: absolute path to your clone. Quoted, so a path containing spaces works.
WATCHDOG_HOME="$HOME/projects/free-inference-watchdog"
# EDIT ME only if `python3` on the scheduler's PATH is not the one you want.
PYTHON="python3"

# Stage 1 — directory selection, exactly as in the tick wrapper: a failed `cd`
# must diagnose on stderr and never launch the resolver.
cd "$WATCHDOG_HOME" || { echo "inference-watchdog resolve FAILED (cannot cd to $WATCHDOG_HOME)" >&2; exit 1; }

# Stage 2 — one resolver pass. `--resolve` itself returns 0 or 2, so the exit-1
# exemption is inherited uniformity rather than a live path here; the stage
# split still matters because a failed `cd` must never be read as a routine
# status.
"$PYTHON" inference_watchdog.py --resolve || { c=$?; [ "$c" -eq 1 ] || { echo "inference-watchdog resolve FAILED (exit $c)" >&2; exit "$c"; }; }
```

```bash
hermes cron create --name inference-watchdog-resolve --no-agent \
  --script inference-watchdog-resolve.sh --deliver local "*/15 * * * *"
```

Recovery notes: a stale lock older than 30 minutes is broken automatically; to
clear one by hand, confirm `pgrep -f inference_watchdog.py` is empty and remove
`state/monitor.lock`. `--init` archives an existing `roster.json` to
`roster.json.bak` before clean-rebaselining; each successful init overwrites a
prior `.bak`, and the command always prints "initialized, no diff" and never
alerts. A behaviour-changing roster edit needs a manual `--init` rebaseline,
then verify the next tick is silent.

### Cadence and exit codes

The tick cadence defaults to 1 hour. Pass `--cadence-hours N` (default 1) and
keep the cron schedule in step: the flag drives the missed-tick warning, which
fires when the last tick is older than cadence + 2h slack. The alive ping fires
on output-age ≥ 20h regardless of cadence; both derive from `alive.json` and
need no restart.

| Code | Meaning |
|---|---|
| 0 | Normal (alerts sent, no diffs, alive ping) |
| 1 | Partial provider failures — any mode, including `--init` — or bootstrap refused |
| 2 | Fatal/unhandled exception — check stderr |

### Testing

The default run is the fast, hermetic suite; genuine wheel/CLI/browser-process/
publication cases carry the single `integration` marker and are excluded:

```bash
python3 -m pytest tests/ -q                    # fast suite (default)
python3 -m pytest tests/ -q -m integration     # integration cases only
python3 -m pytest -o addopts='' tests/ -q      # explicit full run
```

The fast suite needs pytest plus the SDK pins (`mcp`, `pydantic`, `anyio`)
because collection imports them. The integration cases additionally need the
explicit interpreter overrides, `FREE_INFERENCE_WATCHDOG_UV`, a prepared
dependency wheelhouse named by `FREE_INFERENCE_WATCHDOG_WHEELHOUSE`, and
`CONTRACT_NODE`; a missing prerequisite fails with its preparation command, and
the two operator read-backs skip unless `--operator-tick-wrapper=PATH` is
passed. Nothing discovers a provider, a credential or the network.

`tests/consumer_smoke.py` exercises an actually installed wheel and takes the
exact CPython minor it must prove (`3.11` for the floor lane, or the current
stable minor):

```bash
python -I tests/consumer_smoke.py \
  --fixtures tests/fixtures/catalog \
  --console <venv>/bin/free-inference-watchdog-mcp \
  --work <fresh-empty-owned-dir> \
  --python-version 3.11
```

---

## Published catalogue snapshot (v2)

Version 2 removes projected `pricing` and is incompatible with v1 consumers
and caches. See [Version caution](#choose-the-catalogue-source) when upgrading.

### Snapshot fields

| Field | Type | Definition |
|---|---|---|
| `schema_version` | int | Exactly `2`. |
| `snapshot_id` | string | SHA-256 of the canonical snapshot body excluding this field. |
| `tick_epoch` | int or null | Observation time; `null` (with `tick_epoch_unknown`) when unknown. Never inferred from download time. |
| `cadence_s` | int | Positive observation cadence (initially 3600), exposed rather than duplicated in clients. |
| `provider_order` | list of string | Active provider keys in display order, including zero-ID providers. |
| `providers` | map | One entry per active provider, keyed by provider key. |
| `warnings` | list of string | Frozen warning codes; never paths or exception dumps. |

Provider entry: `display_name`, `chat_completions_url`, `api_type`,
`model_ids` (raw tracked ids, sorted and deduplicated by code-point string
order), `models` (projected objects keyed by raw id) and `stale` (from the
roster's stale-provider list).

Projected model fields: `id` (the raw id, equal to the map key), `name`,
`description` (string or null), `context_length` (safe non-negative integer or
null; `0` is a known zero), `architecture` (exactly `input_modalities` +
`output_modalities`, each a list of nonempty strings or null) and
`supported_parameters` (list of nonempty strings or null).

Unknown is never empty: missing, null or malformed optional upstream metadata
projects to `null`; a valid empty upstream array projects to `[]` and stays
known-empty. Context must be a safe non-negative integer (at most 2^53-1,
booleans excluded); missing, numeric-string, boolean, negative and unsafe
values all project to `null`, never coerced. A malformed array is rejected whole,
never partially salvaged. Retained public metadata keeps original casing, order
and duplicates; normalized query values attach separately (ASCII-only lowercase,
first-occurrence deduplication, no whitespace trimming). A missing or null model
metadata container is `metadata_missing`; a non-object container is
`metadata_invalid`. New public metadata fields need a deliberate
schema/projection test — arbitrary upstream objects are never exposed.

| Warning | Meaning |
|---|---|
| `tick_epoch_unknown` | Observation epoch missing or invalid — tick age unknown, never a healthy tick. |
| `metadata_missing` | Optional metadata field absent upstream. |
| `metadata_invalid` | Optional metadata present but malformed or unusable. |

### Validation and errors

A published snapshot is validated strictly and independently of the producer,
after a raw-JSON pass. Structural failures use one envelope: exactly `ok` and
`error`, `ok` strictly `false`, and `error` with exactly `code` and `message`.
Two literals exist: `invalid_snapshot` / `Invalid catalogue snapshot.` and
`unsupported_schema_version` / `Unsupported catalogue schema version.`. Raw-JSON
failures use `invalid_json` / `Invalid catalogue JSON.`. No parameter, raw value,
path, source datum or partial payload is ever carried.

- Order: raw JSON, then object/required-field/version-type, then the version
  value (`unsupported_schema_version` only for a well-typed integer other than
  `2`), then v2 body checks.
- `snapshot_id` must equal the SHA-256 of the canonical body; a well-shaped but
  wrong value is rejected.
- Raw JSON rejects empty or malformed text, comments, trailing data or commas,
  unquoted names, leading-zero and plus-prefixed numbers, malformed escapes, raw
  control characters, a UTF-8 BOM, `NaN`/`Infinity`, and overflowing numbers.
- Every integer token is range-checked exactly at every depth against
  ±9007199254740991 before any precision-losing conversion; numeric-looking
  strings are untouched.
- Duplicate member names are rejected at every object level, including
  escape-equivalent keys. `MAX_JSON_DEPTH` is 64 inclusive; a root scalar is
  depth 0.
- `chat_completions_url` must be a nonempty absolute `http`/`https` URL with a
  hostname, no userinfo, no non-empty fragment and no whitespace. This is
  syntactic only: no DNS lookup, no IP allowlist, no rewriting.
- `unsupported_schema_version` is the validator's code. A consumer acquisition
  with no compatible last-good snapshot collapses it into generic
  `source_unavailable` — indistinguishable from a network failure — while
  compatible cached data is served with `refresh_failed`/`cache_fallback`.
- Structural errors fail export and retain the last published artifact;
  optional metadata degradation never loses an endpoint.

### Tool surface and payloads

Three tools, names stable. Schemas are static; provider membership is validated
against each snapshot, so no stale provider enum is embedded in a package.

These tables list the **core** payload. Every served result also carries a
sibling `source` object — mode, retrieval and observation ages, cache and
refresh warnings — attached outside the pure payload and omitted from these
field lists. Over stdio every result produced after a successful acquisition
carries `source`, including `unknown_provider` and `not_found`; only a failed
acquisition/source-error envelope lacks it. Browser results add
`source: {"mode": "browser"}` to successes and argument errors alike.

| Tool | Success top-level fields |
|---|---|
| `get_model(provider, model_id)` | `ok`, `snapshot_id`, `tick_epoch`, `provider_staleness`, `endpoint` |
| `search_models(...)` | `ok`, `snapshot_id`, `tick_epoch`, `provider_staleness`, `total_matches`, `next_offset`, `endpoints` |
| `watchdog_status()` | `ok`, `snapshot_id`, `tick_epoch`, `cadence_s`, `last_tick_age_s`, `tick_fresh`, `provider_staleness`, `provider_counts`, `metadata_coverage`, `counts`, `pending_alerts`, `site_published`, `site_publish_age_s`, `warnings` |

A `get_model` endpoint carries `provider`, `model_id`, `name`, `description`,
`context_length`, `input_modalities`, `output_modalities`,
`supported_parameters`, `chat_completions_url` and `api_type`; a `search_models`
row omits `description`, `chat_completions_url` and `api_type`.
`watchdog_status` reports `counts` (`gateways`,
`endpoints`, `distinct_raw_ids`, `marker_group_labels`) and
`metadata_coverage` (the `endpoints` denominator plus
`context_length_known`, `input_modalities_known`, `output_modalities_known`,
`supported_parameters_known`, `description_known`). Coverage counts stated
metadata fields only, never provider health or price.

`last_tick_age_s` is `now - tick_epoch` for a known non-future tick and `null`
otherwise, never clamped to zero. `tick_fresh` is true exactly when the age is
known and `<= cadence_s`. Status adds `tick_epoch_future` and `tick_stale` after
the snapshot's three codes in that fixed order; a null or future epoch is never
`tick_stale`. `pending_alerts`, `site_published` and `site_publish_age_s` are
strictly `null` on the published and browser sources.

Argument errors share one envelope: exactly `ok` and `error`, with `error`
carrying exactly `code`, `message` and `parameter`. Codes are `invalid_arguments`
(`Invalid tool arguments.`) and `unknown_provider` (`Unknown provider.`);
`parameter` is the offending known parameter name, or `null` for non-object
arguments and unknown keys. Any unknown key is rejected first, then parameters
are validated in signature order and the first error wins. The PURE CORE's
argument envelope carries no `tool`, path, `source`, cache, staleness or count
field; a served result attaches its `source` sibling outside that envelope.
`not_found` is
`{"ok": false, "error": {"code": "not_found", "message": "Model not found."}}`
with no `parameter` and no echoed value.

Integer tool arguments are value-based JSON Schema integers: `1` and `1.0` are
the same integer, while `true`, `"1"`, `1.5`, unsafe integers and non-finite
numbers are rejected. That boundary is separate from snapshot integer tokens,
where a float spelling such as `1.0` is invalid.

### Identity and ordering

Endpoint identity is `(provider, raw model_id)`: case-sensitive, unpadded,
never marker-stripped or renamed. Result arrays order by provider precedence in
`provider_order`, then by natural raw-ID order within a provider. The natural
comparator tokenizes only maximal ASCII `[0-9]+` runs: numeric runs compare by
original digit count, then lexically, so `vendor/item1` < `vendor/item2` <
`vendor/item02` < `vendor/item10`; text compares case-sensitively by code point
with the shorter prefix first, and non-ASCII digits stay text. Snapshot
`model_ids` lists use plain code-point order, which genuinely differs from query
order; neither is relaxed to hide a defect.

### Shared tool definitions (frozen resource)

`watchdog_catalog/tools.json` is the transport-independent, data-only contract:
a bare JSON array of exactly three objects, each with exactly `name`,
`description` and `inputSchema`. It is the reviewed cross-runtime source for the
shared descriptions and the browser descriptors, packaged as the one MCP
resource and embedded in the page as the `tool-contract` island. Each
`inputSchema` is a plain JSON Schema 2020-12 subset object with `type`,
`properties`, `required` and `additionalProperties: false`, using only portable
keywords — no `$ref`, no `$defs`, no provider enums, no executable validators.
`search_models.provider` is nullable and unconstrained; `get_model.provider` is
a required unconstrained string, so empty, padded and unknown keys pass static
typing and are rejected at runtime against the snapshot. `limit` defaults to 5
and `offset` to 0.

`watchdog_catalog/server.py` derives the advertised schema from its Python
signatures and constrained aliases, proves the generated property-name set
equals the reviewed one, and copies only the reviewed description text — no
type, bound, default, requiredness or pattern is read from this file, and it is
never installed as a replacement `inputSchema`. `web/webmcp.js` builds each
browser descriptor from a defensive copy, with no second schema table in the
page. Packaging, embedding and browser registration are shipped behaviour, not
later work. Local text budgets (500 characters per tool description, 150 per
parameter, 30 per name) are quality gates, not protocol limits.
