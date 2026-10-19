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
| `provider_models` | `{name: {id: <raw API model object>}}` — full metadata for tracked ids only (passive; legacy rosters omit it) |
| `stale_providers` | Provider names whose fetch failed this tick (carried forward) |
| `transients` | Rebuilt every tick. Diffs that appeared then vanished on recheck. |
| `unconfirmed` | Rebuilt every tick. Diffs whose recheck itself failed (signal may resurface). |
| `ratelimits` | Per-gateway passive x-ratelimit headers from this tick (`{}` for succeeded gateways with none; absent = fetch failed) |
| `tick_epoch` | Unix epoch of this tick |

All per-tick fields are **rebuilt** (never appended to). The only persistent
counter is `dropped_alerts_total` in `alive.json`, surfaced by the alive ping.

## Published catalogue snapshot (v1) — contract draft

**Status: frozen for contract review (unit 1a; the `get_model` success
envelope and its count vocabulary were frozen by the envelope-freeze
correction).** This section is the human half of the executable
specification: `tests/test_catalog.py` pins it against synthetic fixtures
under `tests/fixtures/catalog/`. Query and tool behaviour freezes with its
implementation units (Tasks 2/3); nothing here is shipped code.
`site/catalog.json` (declared in AGENTS.md) is the produced artifact: a
generated, tracked, credential-free public snapshot consumed by the
standalone `watchdog_catalog` package and the browser adapters. Provider
config is used only at export time.

### Top-level fields (exact set)

| Field | Type | Definition |
|---|---|---|
| `schema_version` | int | Exactly `1`. |
| `snapshot_id` | string | SHA-256 (64 lowercase hex chars) of the canonical snapshot body excluding this field. Canonical encoding: UTF-8, sorted object keys, compact separators (`,`/`:`), finite JSON values, `ensure_ascii=True`. The raw observation epoch is part of the hashed body; wall-clock build times and machine paths are never included. |
| `tick_epoch` | int or null | Non-negative integer observation time; `null` (with a `tick_epoch_unknown` warning) when unknown. Freshness is never inferred from HTTP download time. |
| `cadence_s` | int | Positive integer observation cadence (initially 3600), exposed explicitly rather than duplicated inside clients. |
| `provider_order` | list of string | Active configured provider keys in display order; includes providers with zero tracked IDs. |
| `providers` | map | One entry per active provider, keyed by provider key (below). |
| `warnings` | list of string | Stable warning codes (frozen vocabulary below); never private paths or exception dumps. |

### Provider entry (exact set)

| Field | Type | Definition |
|---|---|---|
| `display_name` | string | Human-readable provider name. |
| `chat_completions_url` | string | Chat-completions endpoint URL. |
| `api_type` | string | API dialect (e.g. `openai`). |
| `model_ids` | list of string | Raw tracked IDs, sorted and deduplicated by literal code-point string order (never natural order). |
| `models` | map | Projected model objects keyed by tracked raw ID (exact field set below). |
| `stale` | bool | Derived from the roster's stale-provider list. |

### Projected model fields (exact set)

Only these fields are exposed. Provider-returned values are preserved
for retained fields; validated search values attach separately at query
time. New public metadata fields require a deliberate schema/projection
test — arbitrary upstream objects are never exposed automatically.

| Field | Type | Definition |
|---|---|---|
| `id` | string | The raw model ID; equals the enclosing map key. |
| `name` | string | Display name. |
| `description` | string or null | `null` when unknown. |
| `context_length` | int or null | Non-negative safe integer (at most 2^53-1; booleans excluded); `null` when missing, malformed or otherwise unknown. `0` is a KNOWN zero. |
| `architecture` | object | Exactly `input_modalities` + `output_modalities`, each a list of nonempty strings or `null` when unknown. |
| `supported_parameters` | list of nonempty string or null | e.g. `temperature`, `max_tokens`; `null` when unknown. |
| `pricing` | object or null | `null` as the **whole object** when pricing is unknown overall. Otherwise **exactly** `prompt` + `completion`, each a finite scalar preserved as returned (number or numeric string) or `null` when that scalar is **individually unknown**. Booleans and non-finite numbers are invalid. |

### Optional capability nullability and normalization (unit 1c-b1 amendment)

This deliberately amends the optional capability types before publication:
`architecture.input_modalities`, `architecture.output_modalities` and
`supported_parameters` are each a list of nonempty strings or `null`.
Null means UNKNOWN, never empty: unknown, missing or malformed optional
upstream metadata projects to `null`; a valid empty upstream array projects
to `[]` and is KNOWN empty; and `[]` is never used to conceal unknown data or
to drop an endpoint. The snapshot/model/provider field sets and the
`get_model` success envelope are unchanged; no schema-version bump is needed
for this unpublished draft, and this nullable-type clarification is recorded
here so future code cannot silently revert it.

- **Context** — a valid context is a safe non-negative integer (at most
  2^53-1), booleans excluded; `0` is known. Missing, `null`, numeric-string,
  boolean, negative and unsafe-integer contexts all project to `null`
  (unknown), never coerced to a number.
- **Arrays** — a missing or `null` array is unknown. A bare string, a
  mixed-type array, or an array containing an empty string is malformed and
  rejected whole (never partially salvaged) and becomes `null`. An invalid
  `architecture` parent (not an object) makes both its `input_modalities` and
  `output_modalities` children unknown. Valid retained array strings keep
  their original casing, order and duplicates in public metadata; normalized
  query values attach separately.
- **Warnings** — absent or explicit-`null` optional capability data uses
  `metadata_missing`; malformed non-`null` data uses `metadata_invalid`;
  codes are deduplicated in frozen vocabulary order (`tick_epoch_unknown`,
  `metadata_missing`, `metadata_invalid`).
- **Normalization** — normalized capability fields are `context_length`, `input_modalities`, `output_modalities`, `supported_parameters` plus `unknown_fields`. Unknown values stay `null`, never `0` or `[]`. Known arrays are ASCII-only lowercased and first-occurrence deduplicated after folding; non-ASCII code points remain literal — neither whitespace trimming nor Unicode casefolding repairs a token. `unknown_fields` lists only these four capability fields (in that stated order) whose normalized value is `null`; it does not enumerate description or pricing metadata, and a known empty `[]` produces no unknown flag.
- **Filter semantics** — an empty FILTER array applies no constraint; a
  known empty upstream array satisfies no nonempty requested capability list.

`tests/fixtures/catalog/metadata-normalization.json` is the independent
raw-input to expected-projection to expected-normalized data table for these
rules (an upstream capability fragment, never a valid snapshot or public
query response); `tests/fixtures/catalog/capability-unknown.json` is the
valid synthetic snapshot that distinguishes unknown from known-empty.

### Warning codes (frozen vocabulary)

| Code | Meaning |
|---|---|
| `tick_epoch_unknown` | Observation epoch missing or invalid — tick age unknown, never a healthy tick. |
| `metadata_missing` | Optional metadata field absent upstream. |
| `metadata_invalid` | Optional metadata present but malformed or unusable. |

### Error and degradation rules

- **Structural errors fail export** and retain the last published artifact:
  wrong provider map, invalid ID lists, invalid wiring, duplicate registry keys.
- **Optional degradation is unknown, never loss:** missing or malformed
  OPTIONAL metadata degrades to `null`/unknown, not loss of the endpoint;
  invalid optional timestamps become unknown, not a healthy tick.
- **Public JSON parsing rejections** (Python and JS share these
  expectations): `NaN`/`Infinity`, unreasonable nesting, unsafe integers.
- **Never serialized:** config dicts, `_token`, auth blocks, token-file
  paths, local env, pending alerts, ratelimit headers, probe internals,
  untracked IDs, and unknown/disabled provider entries. The root-script
  local compatibility adapter still **PRESERVES raw tracked metadata**
  (including unknown upstream fields) for tracked IDs in its legacy `models`
  and endpoint `metadata` objects — that local boundary is unchanged and
  never weakened. Those raw objects never appear in the public JSON,
  embedded page, consumer package, or parity fixtures: in the frozen public
  payload the name `metadata` carries **only the projected public model
  object** of the exact `(provider, raw model_id)` pair, **never the legacy
  raw metadata**.

### Structural validation of published snapshots (frozen rules)

A published snapshot **object** is validated strictly, independently of the
producer. Every structural failure uses one shared envelope: top-level fields
are **exactly** `ok` and `error`, `ok` strictly `false`, and `error` carrying
**exactly** `code` and `message` — never a `parameter`, raw value, local path,
source/cache datum or partial payload. Two literals exist: `invalid_snapshot` /
`Invalid catalogue snapshot.` and `unsupported_schema_version` /
`Unsupported catalogue schema version.`

- **Order of checks.** A non-object snapshot, a wrong required-field set and a
  non-strict-integer `schema_version` (bool, string or `null`) are all
  `invalid_snapshot`. `unsupported_schema_version` applies **only** when a
  well-typed strict integer version differs from `1`. The top-level
  object/required-field/version-TYPE checks precede the version VALUE check,
  which precedes every v1-specific body check.
- **Strict published-object rejection versus producer projection.** An
  already-PUBLISHED snapshot object that is malformed is rejected whole, never
  repaired, coerced or degraded. Missing or malformed OPTIONAL upstream
  metadata is a SEPARATE, producer-side boundary: it already projects to
  `null`/unknown as documented above, and that projection is unchanged.
- **Hash verification stays load-bearing.** `snapshot_id` must still equal the
  SHA-256 of the canonical body excluding the field. A well-shaped but wrong
  64-hex value, and a malformed value, are both rejected; `snapshot_id` is
  never treated as a merely well-shaped opaque string.
- **Generic schema versus synthetic fixture policy.** The PUBLIC schema itself
  permits a legitimately empty registry (`provider_order` empty with
  `providers` empty), provider keys that are arbitrary nonempty strings (not
  only `synthetic-*`), and registries in which every provider tracks at least
  one id. `provider_order` stays unique and must match the provider map keys
  exactly, and zero-ID providers are permitted and retained (never required).
  The `synthetic-` namespace, a nonempty registry and at least one zero-ID
  provider are TEST-FIXTURE POLICY, not public schema rules.
- **Wiring.** `chat_completions_url` must be a nonempty absolute
  `http`/`https` URL with a hostname, containing no userinfo, no fragment and
  no ASCII whitespace or control character; the original string is preserved
  verbatim. This is purely syntactic — no DNS/network probing, no public-IP
  allowlist, no provider inference and no URL rewriting. Query strings are
  not automatically forbidden.
- **Warning order.** Snapshot warning codes are deduplicated in the frozen
  vocabulary order; a `null` tick still requires `tick_epoch_unknown`.

This structural contract does NOT settle raw-JSON numeric lexeme handling,
duplicate-member handling or canonical-language/JS-parity questions: those
belong to a separate raw-JSON/canonical-boundary unit. No runtime validator,
package or JS implementation is claimed here.

### Raw JSON validation of published payloads (frozen rules)

Raw JSON validation runs **BEFORE** any structural schema check. A published
catalogue document is first checked as raw JSON text and, only if it is
lexically accepted, checked against the structural contract above. An intended
raw failure uses one shared envelope: top-level fields are **exactly** `ok` and
`error`, `ok` is strictly `false`, and `error` carries **exactly** `code` and
`message` with the single literal `invalid_json` / `Invalid catalogue JSON.`
No parameter, offset, exception text, reflected raw content, local path,
source/cache datum or partial result is ever carried, and several simultaneous
raw faults still produce that one literal — there is **no** public subtype
precedence. The existing `invalid_snapshot` and `unsupported_schema_version`
envelopes are unchanged.

- **Lexical acceptance is not public-snapshot acceptance.** Valid JSON that is
  a scalar, array or `null` is **not** a raw error: it is lexically accepted
  and later classified `invalid_snapshot` by the structural layer. Root
  primitives never claim a valid public catalogue.
- **Strict JSON syntax.** Empty or whitespace-only text, malformed or
  truncated documents, comments, trailing data or a trailing comma, unquoted
  member names, a leading-zero integer, a plus-prefixed number, malformed
  escapes and raw control characters inside strings, and a UTF-8 BOM at the
  start of the text are all rejected. No BOM stripping, trimming or other
  silent preprocessing or sanitizing is performed, and the native JSON decoder
  remains the syntax authority.
- **Non-finite and overflowing numbers.** The `NaN`, `Infinity` and
  `-Infinity` constants are rejected, and a syntactically valid number that
  overflows binary64 (`1e400`, `-1e400`) is rejected as non-finite.
- **Notation-independent numeric safety (at EVERY depth).** Every integer
  token is checked **exactly**, before any precision-losing conversion,
  against `[-9007199254740991, 9007199254740991]`; floating and exponent
  tokens convert with normal binary64 semantics, and a non-finite result or an
  integer-valued result outside that same range is rejected. Unsafe integers
  therefore reject in **every notation** — plain digits, a `.0` spelling and
  an exponent spelling such as `1e16` — positive and negative alike, wherever
  they appear (including pricing or unused extra fields), before any schema
  check. A lexical-integer-only safety policy is expressly **not** the
  contract, and numeric-looking **strings** are untouched and never inspected
  as numbers.
- **Python-compatible finite rounding and underflow.** Normal finite
  fractions, signed zero, subnormal values, rounded finite decimals
  (`1.0000000000000001` becomes `1.0`, `9007199254740991.1` becomes
  `9007199254740991.0`) and underflow (`1e-400` becomes `0.0`) remain
  accepted at the raw boundary, and an accepted float token keeps **float**
  type even when its value is integral — it is never coerced into an int or a
  string. No arbitrary decimal-precision reinterpretation is performed.
- **Duplicate member names.** A repeated object member name is rejected at
  **every** object nesting level, including decoded escape-equivalent keys
  (`"a"` versus `"\u0061"`), even when the repeated values are equal. Names
  are compared after decoding and **before** any dict conversion, so there is
  no last-write-wins repair; different case is distinct, and repeating a name
  in two separate objects is fine.
- **Container depth.** `MAX_JSON_DEPTH` is `64`, inclusive: a root container
  is depth `1`, each nested array or object adds `1`, and a root scalar is
  depth `0`. Arrays and objects **both** count, including mixed nesting;
  quoted brackets and braces and escaped quotes or backslashes never affect the
  container depth. The bound is enforced before an unbounded recursive decode
  could exceed runtime limits; the interpreter recursion limit is never raised
  as a substitute.
- **No raw-value repair.** Values are never rewritten: duplicates, unsafe
  numbers, malformed lexemes and over-deep nesting are rejected outright,
  never partially salvaged, clamped, re-spelled or silently preprocessed.

`tests/fixtures/catalog/raw-json-cases.json` is the clearly-labelled raw-JSON
rule **TABLE** (explicitly **not** a snapshot and **not** a query result)
holding the adversarial literal lexemes as STRINGS plus their handwritten
complete expected envelopes or decoded values; `tests/test_catalog.py`
restates these rules as a test-local stdlib checker. No runtime parser or
public success envelope is claimed here: the canonical encoding/hash vectors
and the strict snapshot integer-token boundary are frozen separately in the
typed-encoding subsection below, and JavaScript parity remains a separate
concern with no JS implementation claimed anywhere.

### Typed canonical encoding and strict snapshot integer tokens (frozen rules)

The public `snapshot_id` remains the SHA-256 of the **canonical body** — the
complete snapshot object with **only** the root `snapshot_id` excluded — and
hash equality stays **load-bearing**: a well-shaped but wrong 64-hex value is
never accepted as a merely opaque id. The canonical body is produced from the
**decoded typed values**, never from the raw file bytes or their numeric
spellings: UTF-8, object keys sorted by Unicode **code point**, compact `,`/`:`
separators, finite JSON values and `ensure_ascii=True`; Python's `json.dumps`
with those flags is the reference encoder. A lexical difference that parses to
identical typed values — re-spelled floats such as `1e0` versus `1.0`,
harmless whitespace, object-member order, or equivalent string escapes — yields
an **identical** canonical body and `snapshot_id`, while an integer token `1`
and a float token `1.0` are **different** typed values and hash differently.
String values, including numeric-looking strings, are never coerced.

Published snapshot integer-token fields (`schema_version`, `tick_epoch`,
`cadence_s` and a model's `context_length`) stay **strict integer tokens**:
booleans are excluded, and an integral **float** token such as `1.0` or `1e0`
is **not** a valid strict snapshot integer. Such a document is
`invalid_snapshot`, never `unsupported_schema_version`, which applies only to a
well-typed strict integer version other than `1`. This is a **different**
boundary from the `c1` tool argument table, which still accepts `1.0` for
integer arguments; pricing keeps its frozen finite scalar numbers, numeric
strings and `null`, and is never dropped, stringified or coerced to an int.
Unknown capability metadata stays unknown.

`tests/fixtures/catalog/canonical-cases.json` is the clearly-labelled canonical
rule **TABLE** (explicitly **not** a snapshot and **not** a query result). Its
`canonical_vectors` rows carry an explicit `scope`: a `general_value` row is a
general raw-JSON **encoding specification** whose whole value is hashed (a
nested `snapshot_id` key is ordinary retained content), never a valid public
snapshot; a `snapshot` row is a complete re-sealed snapshot document hashed
with only the root `snapshot_id` excluded. The `canonical_text` and `sha256`
values are literal hand-authored FILE CONTENT, re-derived independently by the
test-local stdlib checker; the `snapshot_cases` rows carry complete literal
documents and either `accepted: true` or the complete literal error envelope.
No runtime encoder, package implementation or JavaScript parity is claimed
here.

### Tool surface (frozen names)

Five tools, names stable; `watchdog_status` reports **published catalogue
health**, not private operator health. Schemas stay static with provider
validation against each snapshot (no stale provider enums embedded in an
installed package):

- `list_free_models(provider=None)`
- `get_model(model_id)`
- `list_endpoints(provider=None)`
- `watchdog_status()`
- `search_models(query=None, provider=None, min_context_length=None, input_modalities=None, supported_parameters=None, limit=50, offset=0)`

### Tool arguments and argument errors (frozen rules)

Every known tool shares one semantic argument-error envelope: top-level
fields are **exactly** `ok` and `error`, `ok` is strictly `false`, and
`error` carries **exactly** `code`, `message` and `parameter`. Two codes
exist: `invalid_arguments` with the literal message `Invalid tool
arguments.`, and `unknown_provider` with the literal message `Unknown
provider.` `parameter` is the offending **known signature parameter** name,
or `null` for non-object arguments and unknown argument names; the response
never reflects an arbitrary supplied key or hostile value. An argument error
carries **no** `tool`, `state_root`, path, `source`, cache, snapshot
provenance, staleness, partial-data or count field, and every success
envelope is unchanged.

Argument validation is deterministic. The arguments must be a JSON object;
then **ANY** unknown argument key is rejected (`parameter` `null`); then the
parameters are validated in signature order and the first error wins.
`search_models` validates `query`, `provider`, `min_context_length`,
`input_modalities`, `supported_parameters`, `limit`, `offset`, and
`get_model` checks its required `model_id` in its signature position. A
provider of the wrong non-null type is `invalid_arguments`; a string absent
from the current snapshot's `provider_order` is `unknown_provider`. Empty,
padded and wrong-case provider keys are never repaired. Only the five known
tools are scoped here: unknown-tool protocol dispatch and invalid-snapshot or
source precedence are separate concerns.

- `list_free_models(provider=None)` and `list_endpoints(provider=None)`
  accept an absent or `null` `provider`; otherwise it must be one exact
  registered key.
- `get_model(model_id)` requires a string containing at least one character
  outside ASCII whitespace (U+0009..U+000D and U+0020); empty or
  ASCII-blank-only strings are `invalid_arguments(model_id)`. The retained
  identifier is never trimmed: `" vendor/model "` remains a literal nonblank
  query, and non-ASCII code points stay literal. The ASCII blank set is never
  widened with Python `str.strip` or JavaScript `trim`.
- `watchdog_status()` has no public arguments; the fixed test clock is
  case-level harness input, never a tool argument.
- `search_models` accepts an absent or `null` `query` and **any** string,
  including empty, whitespace-only, non-ASCII and regex-looking values; it is
  literal and untrimmed. Any other query type is `invalid_arguments(query)`.
  `min_context_length` accepts absent/`null` or a JSON safe integer `>= 0`.
  `limit` accepts omission (default `50`) or a JSON safe integer in `1..200`,
  never `null`; `offset` accepts omission (default `0`) or a JSON safe
  integer in `0..2^53 - 1`, never `null`. `input_modalities` and
  `supported_parameters` accept absent/`null` or arrays of nonempty strings;
  an empty `[]` applies no constraint, and duplicates, casing and surrounding
  whitespace inside nonempty entries are preserved, not trimmed. A bare
  string, an object, a mixed or `null` member, or an empty string member
  rejects the whole parameter; arrays are never partially salvaged.
- **JSON integer input boundary:** a JSON Schema integer is a finite numeric
  value with zero fractional part, so numeric `1` and `1.0` are the same
  integer value and JavaScript `Number.isSafeInteger(1.0)` is `true`.
  Integral numeric tool inputs such as Python `1.0` are therefore accepted;
  `true`, `"1"`, `1.5`, unsafe integers and non-finite numbers are rejected.
  This is **not** coercion of a string or boolean, and it does not change the
  separately frozen strict-int metadata projection or payload-count rules.

These argument errors are **separate from published snapshot degradation**:
missing or malformed OPTIONAL upstream metadata still degrades to
`null`/unknown inside a success payload, and the root-script local
compatibility adapter still preserves raw tracked metadata at its own
boundary. Neither is changed by this argument contract.

### Shared tool definitions (frozen resource)

`watchdog_catalog/tools.json` is the transport-independent, data-only
shared definition of the five known tools: a bare JSON array of exactly
five objects, each with exactly `name`, `description` and `inputSchema`.
It is the reviewed cross-runtime contract and the single source for the
shared tool descriptions and the browser tool definitions; it is **not**
an `input_schema` argument to any SDK `add_tool` call, and no wrapper
metadata, annotations, titles, output schemas, callbacks, sources or
provider list appears anywhere in the resource. No per-tool private input
(the clock, source, URL, path, cache or config) is ever exposed as a tool
argument.

Each `inputSchema` is a plain JSON Schema 2020-12 subset object — an
object schema with an explicit `type` of `object`, a `properties` map, a
`required` list and `additionalProperties` **false** — using only the
portable keywords `type` (including nullable type arrays), `properties`,
`required`, `additionalProperties`, `items`, `minLength`, `pattern`,
`minimum`, `maximum`, `default` and `description`. There are no `$ref`
or `$defs`, no custom executable validators, no provider enums and no
`oneOf` boilerplate. Unknown schema keywords are never silently ignored
by the schema checker.

The schemas are **static** while provider membership stays **dynamic**:
`provider` is a nullable, unconstrained string with `default` `null` and
**no** `enum`, `minLength` or `pattern`, so empty, padded and unknown
provider strings pass static typing and are rejected at runtime by the
existing `unknown_provider` query check against each snapshot's
`provider_order` — no stale provider enum is embedded in an installed
package. `limit` defaults to `50` and `offset` to `0`, must be present
integers (never `null`) and are never repaired. Omitted and `null` stay
distinct: an omission may apply a default, but the default is never
injected into, or coerced onto, the retained arguments.

The **JSON Schema integer** input boundary is value based — a finite
numeric value with zero fractional part, so `1.0` is the integer `1` —
and is **separate** from the strict snapshot integer lexeme rule, where
an integral float token such as `1.0` is not a valid strict snapshot
integer. `true`, `"1"`, `1.5`, unsafe integers and non-finite numbers
are rejected tool arguments.

The local text budgets — **500** characters per tool description,
**150** per parameter description and **30** per tool or parameter name
— are inclusive local quality gates, **not** protocol hard limits: the
official tool-definition API does not reject larger text, so these are
revisited if that API later adds normative limits.

The `list_free_models` and `list_endpoints` descriptions state that they
are **bulk** exports and advise routine use of `search_models` with an
explicit small `limit` **1-5** followed by `get_model` for details; they
never imply a hard universal output cap or truncation, so bulk output may
exceed that guidance. `watchdog_status` describes **published catalogue
health** only — never live or private operator or cron health, and never a
provider probe.

This subsection scopes to the shared **data** resource alone: packaging
the file as package data, embedding it in HTML, comparing it against MCP
SDK schema generation, and the native browser registration annotations
remain their own later units. No runtime, package, SDK or browser
integration is claimed here.

### Identity, lookup, and ordering (frozen rules)

- `get_model.exact_matches` is **literal raw-ID membership** — case-sensitive,
  unpadded, exact. `get_model("vendor/model:free")` matches only providers
  tracking that raw id, never marker-stripped or renamed variants.
- The separate marker-insensitive lookup strips **only** a delimiter-bounded
  `free` at the start or end of an id (delimiters `[-:_]`, case-insensitive),
  with an **empty-result guard**: a strip that would empty the id returns it
  unchanged. `get_model("vendor/model")` lists every endpoint whose stripped
  name equals the stripped query, raw ids unchanged.
- A marker match is **not proof of cross-gateway model identity**; the absence
  caveat is preserved on every `get_model` payload.
- Identifiers are **never silently trimmed or repaired**: `" vendor/model "` is
  not trimmed into a match.
- Endpoint identity is **`(provider, raw model_id)`**: a raw id shared by two
  providers is two endpoints, and metadata stays attached to its own pair.
  Result arrays order by provider display order, then natural raw-ID order
  (ASCII digit runs compared by digit length and lexical order).
- **Total ordering rule (frozen):** provider precedence is the index in
  `provider_order`, never alphabetic key order, provider-map insertion order or
  global raw-ID order. Identical raw IDs on different providers remain two pairs
  and order by provider precedence; within one provider, raw IDs compare by the
  natural comparator below.
- **Natural comparator:** tokenize only maximal ASCII `[0-9]+` runs, preserving
  intervening text and string boundaries. Numeric runs compare by original digit
  count, then lexical ASCII digits — no integer/float conversion of long digit
  sequences and no stripping of leading zeroes, so `vendor/item1` <
  `vendor/item2` < `vendor/item02` < `vendor/item10`. Text segments compare
  case-sensitively by Unicode code point: no case folding, no locale collation,
  no Unicode normalization and never UTF-16 code-unit comparison; on a strict
  prefix the shorter sequence sorts first. Non-ASCII numeric-looking characters
  are TEXT, never digit runs; Arabic-Indic digits (`١`) and superscript digits
  (`²`) stay text (generic `isdigit`/`\d` classification is not this contract).
  Digit-run tokens precede text tokens, empty boundary text tokens are preserved,
  and literal raw IDs are never rewritten or replaced by normalized labels.
- **Snapshot order versus query order:** snapshot `model_ids` stay sorted and
  deduplicated by literal code-point string order (frozen), while query endpoint
  result arrays use provider precedence plus NATURAL raw-ID order. The two orders
  genuinely differ — `vendor/item02` precedes `vendor/item1` in a snapshot
  `model_ids` list but follows `vendor/item2` in a query result. Input
  `model_ids` are never reordered to hide a query sort defect, and snapshot
  integrity is never relaxed to accept an adversarial unsorted `model_ids` list.

### Success envelope — `get_model` (frozen field sets)

The pure query payload is the **semantic** payload: SDK and browser
wrappers may differ in protocol envelopes, but this object is identical
across them. It exposes no local filesystem path and no source
retrieval/cache metadata — a source layer adds a separate `source` object
(URL/file/browser mode, retrieval/cache information, refresh warnings)
OUTSIDE this pure payload, and the pure payload never invents freshness
from HTTP download time.

`get_model(model_id)` success payload — exact top-level fields:

| Field | Type | Definition |
|---|---|---|
| `ok` | bool | Exactly `true` on success; a lookup that finds nothing is still a success. |
| `query` | string | The exact raw query as passed, never trimmed or repaired. |
| `snapshot_id` | string | Observation provenance: the consumed snapshot's `snapshot_id`. |
| `tick_epoch` | int or null | Observation provenance: the consumed snapshot's `tick_epoch`; `null` means unknown, never fresh. |
| `exact_matches` | map of bool | Literal raw-ID membership — one entry per snapshot provider in `provider_order`, **including zero-ID providers**. |
| `exact_gateways` | list of string | Provider keys whose `exact_matches` is `true`, in provider display order. |
| `provider_staleness` | map of bool | Staleness of **every** snapshot provider relevant to the response. Exact membership covers all snapshot providers, so this map carries one entry per `provider_order` provider even when no endpoint matches. Each value is that gateway's carried-forward stale flag — **provider staleness, never tick age** (tick freshness is derived from `tick_epoch` against the snapshot's `cadence_s`). |
| `endpoints` | list of object | Matched endpoint pairs, ordered by provider display order then natural raw-ID order. |
| `caveats` | list of string | Always carries the absence caveat. |
| `counts` | object | Explicit cardinalities of the returned lookup result; exact set below. |

Endpoint object — exact fields:

| Field | Type | Definition |
|---|---|---|
| `provider` | string | Provider key of the `(provider, raw model_id)` pair. |
| `model_id` | string | Raw tracked model ID, unchanged. A raw id shared by two providers yields two endpoints. |
| `chat_completions_url` | string | The provider's chat-completions URL. |
| `api_type` | string | The provider's API dialect. |
| `stale` | bool | This provider's staleness — equals `provider_staleness[provider]`. |
| `metadata` | object | The credential-free **projected public model object** of exactly this `(provider, raw model_id)` pair — the same exact field set as a snapshot projected model, and **never the legacy raw metadata** object. Not nullable: an endpoint exists only for a tracked raw id and the projection always carries the exact field set, with unknown optional fields `null`. |

`counts` object — exact fields (each a non-negative int; booleans are invalid):

| Field | Definition |
|---|---|
| `exact_matches` | **Matching gateways**: providers with literal raw-ID membership (`len(exact_gateways)`). |
| `endpoints` | Endpoint pairs returned (`len(endpoints)`). |
| `distinct_raw_ids` | Distinct raw `model_id` strings among the returned endpoints: a raw id shared by two providers is **two endpoints but one distinct raw id**. |
| `marker_group_labels` | Distinct **marker group** labels among the returned endpoints (the delimiter-stripped query label they matched); `0` when `endpoints` is empty. A marker group is a labelling convenience, **never** a count of verified underlying models — marker grouping is not proof of cross-gateway identity. |

Every `counts` value describes only the returned lookup result —
never the global registry. A no-match lookup is an explicit success:
`exact_gateways` and `endpoints` are `[]`, `provider_staleness` still lists
every snapshot provider, and `counts` is **all zeros** — there is no
no-match error.

### Success envelope — `search_models` (frozen field sets)

`search_models(query=None, provider=None, min_context_length=None,
input_modalities=None, supported_parameters=None, limit=50, offset=0)`
returns the same PURE semantic payload across SDK and browser wrappers; like
`get_model` it exposes no local filesystem path and no source retrieval/cache
data (a source layer adds a separate `source` object OUTSIDE this payload).
Text search is **literal substring** matching over raw ID, name and
description, folded **ASCII case-insensitive** only; there is
**no regular expression** support, no fuzzy alias and no JSON-string/metadata
search. Exact top-level fields:

| Field | Type | Definition |
|---|---|---|
| `ok` | bool | Exactly `true` on success — a no-match or beyond-end page is still a success. |
| `query` | string or null | The supplied raw query echoed unchanged, or `null` when omitted; it is **never trimmed**. |
| `filters` | object | Echoed raw filter values (exact set below); each is the supplied value or `null` when omitted. |
| `limit` | int | Effective page size: the supplied `limit` or the default `50`; integer in `1..200`. |
| `offset` | int | Effective page offset: the supplied `offset` or the default `0`; integer `>= 0`. |
| `snapshot_id` | string | Observation provenance: the consumed snapshot's `snapshot_id`. |
| `tick_epoch` | int or null | Observation provenance: the consumed snapshot's `tick_epoch`; `null` means unknown, never fresh. |
| `provider_staleness` | map of bool | Staleness scope: every snapshot provider in `provider_order` when no provider filter is given, else exactly the selected provider — **including a selected provider with zero tracked IDs**. Each value is that gateway's carried-forward stale flag (**provider staleness, never tick age**), retained even on an empty or beyond-end page. |
| `endpoints` | list of object | The `returned page` of matched endpoint pairs, in frozen order (below). |
| `total_matches` | int | Endpoint pairs satisfying **all** active predicates **before** pagination — the separate `unpaginated` cardinality. |
| `returned_count` | int | `len(endpoints)` for this page. |
| `next_offset` | int or null | `offset + returned_count` when that is **less than** `total_matches`; otherwise `null` (including an exhausted or beyond-end page). |
| `counts` | object | PAGE-scoped cardinalities of the returned page; exact set below. |
| `caveats` | list of string | Always carries the **absence caveat**. |

`filters` object — exact fields:

| Field | Definition |
|---|---|
| `provider` | The one exact provider key to restrict to, echoed raw, or `null` when omitted. An unknown key is an argument error (a later unit). |
| `min_context_length` | The non-negative integer filter, echoed raw, or `null`; the comparison is inclusive over KNOWN safe non-negative integer contexts, and unknown contexts fail it. |
| `input_modalities` | The requested modalities, echoed raw (casing/order/duplicates preserved), or `null`; an empty `[]` is echoed as `[]` yet applies no constraint. |
| `supported_parameters` | The requested parameters, echoed raw, or `null`; an empty `[]` likewise applies no constraint. |

Endpoint object — exact fields:

| Field | Type | Definition |
|---|---|---|
| `provider` | string | Provider key of the `(provider, raw model_id)` pair. |
| `model_id` | string | Raw tracked model ID, unchanged. |
| `chat_completions_url` | string | The provider's chat-completions URL. |
| `api_type` | string | The provider's API dialect. |
| `stale` | bool | This provider's staleness — equals `provider_staleness[provider]`. |
| `name` | string | The pair's public display name. |
| `description` | string or null | The pair's public description; `null` when unknown. |
| `context_length` | int or null | The normalized context value (unknown stays `null`). |
| `input_modalities` | list of string or null | The normalized input modalities (ASCII-lowercased, first-occurrence deduplicated). |
| `output_modalities` | list of string or null | The normalized output modalities. |
| `supported_parameters` | list of string or null | The normalized supported parameters. |
| `unknown_fields` | list of string | Exactly the four capability fields whose normalized value is `null`, in the frozen order `context_length`, `input_modalities`, `output_modalities`, `supported_parameters`. |
| `metadata` | object | The complete **projected public model object** of exactly this `(provider, raw model_id)` pair, retaining valid original array casing/order/duplicates — **never the legacy raw metadata**, and never a cross-provider merge. |

`counts` object — exact fields, describing the **returned page** only (never
the global registry or the unpaginated match set; an empty page is all
zeros, even when `total_matches` is positive):

| Field | Definition |
|---|---|
| `gateways` | Distinct provider keys among the returned page rows. |
| `endpoints` | Returned page rows (`returned_count`). |
| `distinct_raw_ids` | Distinct raw `model_id` strings in the page. |
| `marker_group_labels` | Distinct delimiter-stripped labels in the page — labels, **never** a count of verified underlying models. |

Filtering and pagination semantics:

- The **AND** of the active predicates selects endpoint pairs; each pair is
  tested independently on its own `(provider, raw model_id)` metadata, so a
  context from one gateway is never combined with a capability from another.
- A `min_context_length` filter qualifies only a KNOWN finite, safe,
  non-negative integer context, compared inclusively; a `null` (unknown)
  context fails it. An empty or omitted text query applies no text constraint.
- A modality or parameter filter uses **AND** across its requested entries:
  **every** requested entry (normalized to ASCII-lowercase) must be present
  in that endpoint's validated array. A `null`/unknown array fails a nonempty
  filter; a known empty `[]` satisfies no nonempty request.
- **Sort, then slice**: order results by provider display order, then natural
  raw-ID order, and only then apply `offset`/`limit`; `total_matches` is the
  cardinality **before** that slice.
- `NaN`/`Infinity`, `bool`-as-integer and unsafe integers in PUBLIC JSON are rejected when parsing (Python and JS share these expectations); but an **unsafe UPSTREAM context** degrades to null (unknown) rather than failing the endpoint — upstream uncertainty never becomes a parsed unsafe integer.

Worked examples (complete payloads live as literal goldens in
`tests/fixtures/catalog/cases.json`, one `search_models` case per scenario):

- Over `metadata-edge-cases.json`, `query="reasoning"` matches the
  known-context, unknown-context and zero-context models (but not the
  null-description one); adding `min_context_length=65536` keeps only the
  known 65536-context model, while `min_context_length=0` keeps the known and
  the known-zero models and still excludes the unknown (null) context.
- Over `lookup.json`, the raw id `vendor/shared` is tracked by two gateways:
  `query="shared" + min_context_length=65536` yields only synthetic-a's
  endpoint; `query="shared" + input_modalities=["text","IMAGE"]` yields only
  synthetic-b's; asking for both at once yields NEITHER (no cross-provider
  merge).
- Over `search-text.json`, `query="REASONING"` returns normalized capability
  arrays while the public `metadata` object keeps its original casing;
  `query="Image"` yields nothing (the token lives only in the metadata array,
  which is never searched); and `query=" REASONING "` yields nothing because
  identifiers and queries are never trimmed.

### Success envelope — `watchdog_status` (frozen field sets)

`watchdog_status()` reports **published catalogue health**, not private
operator health. It is the **whole-catalogue** status of the consumed
snapshot: like `get_model` and `search_models` the pure payload exposes no
local filesystem path, no `state_root`/tool path, no source retrieval/cache
timestamp and no operator diagnostics — those belong to a separate,
explicitly labelled local adapter OUTSIDE this payload. `ok: true` means the
valid snapshot was queried successfully — **not** that its observations are
fresh or that every provider is healthy. The fixed test clock is a case-level
`now` epoch injected into the core; it is **never** a tool argument and never a
wall-clock lookup. Exact top-level fields:

| Field | Type | Definition |
|---|---|---|
| `ok` | bool | Exactly `true` on success — the snapshot was queried successfully, not that its observations are fresh. |
| `snapshot_id` | string | Observation provenance: the consumed snapshot's `snapshot_id`. |
| `tick_epoch` | int or null | Observation provenance: the consumed snapshot's `tick_epoch`, echoed literally (a future epoch is **never** repaired); `null` means unknown, never fresh. |
| `cadence_s` | int | The consumed snapshot's cadence, echoed: this is **snapshot data, never a hardcoded `3600`**. |
| `last_tick_age_s` | int or null | `now - tick_epoch` as a strict non-negative int for a known non-future tick; `null` when the tick is `null` or in the future — **never clamped to a healthy zero**. |
| `tick_fresh` | bool | The **inclusive one-cadence boundary**: `true` exactly when `last_tick_age_s` is a known age and `last_tick_age_s <= cadence_s` (age `0` and age `== cadence_s` are both fresh); `false` for a null or future tick. Always a strict bool. |
| `provider_staleness` | map of bool | **Every** configured provider in `provider_order`, including zero-ID providers; each value is that gateway's carried-forward stale flag, **unchanged by the clock** — this is **provider staleness, never tick age**. Provider staleness may be `true` during a fresh tick and all providers may be `false` during an old tick. |
| `provider_counts` | map of strict non-negative int | **Every** configured provider, each its tracked `(provider, raw ID)` pair count (`len(model_ids)`), **zero included**. |
| `metadata_coverage` | object | Coverage of the stated metadata fields over the global tracked-pair denominator; exact set below. |
| `counts` | object | **Whole-catalogue** cardinalities; exact set below. |
| `pending_alerts` | null | Unobservable on the published/browser sources: **strictly `null`**. |
| `site_published` | null | Unobservable on the published/browser sources: **strictly `null`**. |
| `site_publish_age_s` | null | Unobservable on the published/browser sources: **strictly `null`**. |
| `warnings` | list of string | Snapshot warnings plus derived status codes, deduplicated in the frozen status order below; stable strings, never paths or exceptions. |

`counts` object — exact fields (each a strict non-negative int; booleans are
invalid). Unlike `search_models`' PAGE-scoped counts, a status `counts` object
describes the **whole catalogue** and never a filtered page:

| Field | Definition |
|---|---|
| `gateways` | **All** configured snapshot providers in `provider_order`, **including zero-ID providers** (whole-catalogue, not a page). |
| `endpoints` | **All** tracked `(provider, raw ID)` pairs. |
| `distinct_raw_ids` | The **literal union** of raw IDs: a raw id shared by two gateways is one distinct raw id. |
| `marker_group_labels` | Distinct delimiter-stripped labels — labels, **never** verified underlying model identities. |

`metadata_coverage` object — exact fields (each a strict non-negative int at
most the `endpoints` denominator):

| Field | Definition |
|---|---|
| `endpoints` | The **global tracked-pair denominator** (equal to `counts.endpoints`). |
| `context_length_known` | Tracked pairs with a non-null **valid** context; a known `0` counts, `null` (unknown) does not. |
| `input_modalities_known` | Tracked pairs with a non-null valid input-modality array; a known empty `[]` counts, `null` does not. |
| `output_modalities_known` | As above for output modalities. |
| `supported_parameters_known` | As above for supported parameters. |
| `description_known` | Tracked pairs with a non-null description string. |

Coverage is of these stated metadata fields only — **never provider health or
price availability** — and a present metadata object is never counted as
complete metadata, because projection always supplies an object even when its
fields are unknown.

**Status warning order and the status/snapshot vocabulary split.** The
snapshot warning vocabulary stays the frozen THREE codes (`tick_epoch_unknown`,
`metadata_missing`, `metadata_invalid`) — the only codes a snapshot document
may carry. A status payload adds two **status-only** codes after them, so the
full status order is `tick_epoch_unknown`, `metadata_missing`,
`metadata_invalid`, `tick_epoch_future`, `tick_stale`:

- `tick_epoch_unknown` is added for a `null` epoch (normally already a snapshot
  warning).
- `tick_epoch_future` is added when `tick_epoch` is in the future; the raw
  epoch is echoed unchanged.
- `tick_stale` is added when the **known** age is strictly greater than
  `cadence_s`.

A fresh known epoch adds no status warning. A null or future epoch is **NOT a
stale known observation** and never carries `tick_stale`.

Compact complete example — `watchdog_status()` over a snapshot with
`synthetic-a` tracking two models (one all-known, one all-unknown) and a stale
zero-ID `synthetic-b`, at the fixed clock `now = 1759200000` (tick `1759200000`,
cadence `3600`):

```json
{
  "ok": true,
  "snapshot_id": "deb67e26339f74463348688870b091058362fcf7b88455fbea1507e6e8571b41",
  "tick_epoch": 1759200000,
  "cadence_s": 3600,
  "last_tick_age_s": 0,
  "tick_fresh": true,
  "provider_staleness": {"synthetic-a": false, "synthetic-b": true},
  "provider_counts": {"synthetic-a": 2, "synthetic-b": 0},
  "metadata_coverage": {"endpoints": 2, "context_length_known": 1,
    "input_modalities_known": 1, "output_modalities_known": 1,
    "supported_parameters_known": 1, "description_known": 1},
  "counts": {"gateways": 2, "endpoints": 2, "distinct_raw_ids": 2,
    "marker_group_labels": 2},
  "pending_alerts": null,
  "site_published": null,
  "site_publish_age_s": null,
  "warnings": ["metadata_missing"]
}
```

Worked examples (complete payloads live as literal fixed-clock goldens in
`tests/fixtures/catalog/cases.json`, one `watchdog_status` case per scenario,
each with a case-level `now` and `args: {}`): at `now == tick_epoch` the age is
`0` and the tick is fresh; age `== cadence_s` is still fresh, and one second
more is `tick_stale`; a `null` epoch yields `last_tick_age_s: null`,
`tick_fresh: false` and `tick_epoch_unknown`; a future epoch yields the same
null age and `tick_epoch_future`, never `tick_stale`; and a snapshot whose
cadence is `120` (not `3600`) proves the freshness boundary uses the snapshot's
own cadence.

### Success envelope — `list_free_models` (frozen field sets)

`list_free_models(provider=None)` is the frozen **BULK export tool**: one call
returns the whole registry, or one gateway's complete ID list. Routine agent
lookups should not use it — direct those to `search_models` with a small
explicit `limit` (e.g. 1–5), then `get_model` for one exact ID. Limits in this
surface bound returned **rows, not characters**: there is no truncation and no
browser-only semantics anywhere in it. Like the other pure payloads this
object exposes no local filesystem path, no `tool`/`state_root` field and no
source retrieval/cache data (a source layer adds a separate `source` object
OUTSIDE it). It preserves the useful root-script nesting (`providers`,
`models`, `counts`, `total_ids`, `n_gateways`) while dropping the root
script's local-only fields.

Two **different** exact top-level field sets, one per variant.

`provider` omitted or `null` — all-provider export:

| Field | Type | Definition |
|---|---|---|
| `ok` | bool | Exactly `true`. |
| `providers` | map | Provider key -> that provider's **complete** raw ID array, each array in NATURAL raw-ID order (the frozen comparator above), **including zero-ID providers** (an empty array). Provider object key order is not semantic; result array order is. |
| `models` | map | Provider key -> map of **all** tracked IDs to their complete **projected public model object** — an empty map for a zero-ID provider. Values stay attached to their own `(provider, raw model_id)` pair: a raw ID shared by two gateways keeps its two conflicting per-provider objects, never flattened or copied across. |
| `counts` | map | Provider key -> that provider's tracked-ID count as a strict non-negative int, `0` included; the same exact provider set as `providers`/`models`/`provider_staleness`. |
| `total_ids` | int | **Distinct LITERAL raw IDs** across the selected registry — never the endpoint-pair count and never marker-label count. |
| `n_gateways` | int | `len(provider_order)`, zero-ID providers included. |
| `snapshot_id` | string | Observation provenance: the consumed snapshot's `snapshot_id`, echoed literally. |
| `tick_epoch` | int or null | Observation provenance: the consumed snapshot's `tick_epoch`, echoed literally — no invented freshness or download date. |
| `provider_staleness` | map of bool | Each selected provider's strict carried-forward snapshot stale flag, **including zero-ID providers** — **provider staleness, never tick age**. |

`provider` a valid exact key — one-provider export:

| Field | Type | Definition |
|---|---|---|
| `ok` | bool | Exactly `true`. |
| `provider` | string | The exact provider key, echoed; identifiers are never trimmed or repaired. |
| `model_ids` | list of string | That provider's complete raw ID array in NATURAL raw-ID order. |
| `models` | map | Its flat raw ID -> projected public model object map — **not** the all-provider nesting. |
| `count` | int | Strict non-negative int, equal to `len(model_ids)`. |
| `snapshot_id` | string | As above. |
| `tick_epoch` | int or null | As above. |
| `provider_staleness` | map of bool | Exactly `{selected_provider: its_stale_flag}`. |

A registered **zero-ID** provider is a success: `model_ids` is `[]`, `models`
is `{}`, `count` is `0` and `provider_staleness` still carries that provider —
never a no-match or missing-provider error. No global field
(`providers`/`counts`/`total_ids`/`n_gateways`) leaks into the one-provider
shape, and published `models` values are projected public model objects,
**never the legacy raw metadata** dictionaries.

Complete worked examples live as literal goldens in
`tests/fixtures/catalog/cases.json` — `list_free_lookup_all`,
`list_free_lookup_provider_a`, `list_free_lookup_zero_provider_c` (an empty but
successful zero-ID selection) and `list_free_ordering_all` (natural raw-ID
array order, a shared raw ID whose per-provider objects conflict, and a stale
flag preserved independently of the observation epoch).

### Success envelope — `list_endpoints` (frozen field sets)

`list_endpoints(provider=None)` publishes the wiring and the projected metadata
of every selected `(provider, raw model ID)` pair. Routine lookups should not
use it — direct those to `search_models` with a small explicit `limit` (e.g.
1–5), then `get_model` for one exact ID. Limits in this surface bound returned
**rows, not characters**: there is no truncation and no browser-only semantic
difference anywhere in it. Like the other pure payloads this object exposes no
local filesystem path, no `tool`/`state_root` field and no source
retrieval/cache data (a source layer adds a separate `source` object OUTSIDE
it). It preserves the useful root-script nesting while publishing only projected
data.

One **exact** top-level field set for BOTH the all-provider and the
provider-filtered variant:

| Field | Type | Definition |
|---|---|---|
| `ok` | bool | Exactly `true`. |
| `gateways` | map | Provider key -> gateway object (below), one entry per **selected** provider, **including zero-ID ones**. Provider object key order is not semantic; result array order is. |
| `counts` | map | The exact count object below. |
| `provider` | string or null | The exact selected provider key echoed, or strictly `null` when `provider` is omitted or `null`. Identifiers are never trimmed or repaired. |
| `snapshot_id` | string | Observation provenance: the consumed snapshot's `snapshot_id`, echoed literally. |
| `tick_epoch` | int or null | Observation provenance: the consumed snapshot's `tick_epoch`, echoed literally — no invented freshness or download date. |
| `provider_staleness` | map of bool | Exactly the **selected** providers' strict carried-forward snapshot stale flags — **provider staleness, never tick age**. |

Each `gateways` value is the exact gateway entry field set:

| Field | Type | Definition |
|---|---|---|
| `chat_completions_url` | string | Wiring, from the exact snapshot provider. |
| `api_type` | string | Wiring, from the exact snapshot provider. |
| `model_ids` | list of string | That provider's complete raw ID array in NATURAL raw-ID order (the frozen comparator above) — unchanged strings, never marker labels. |
| `models` | map | Every tracked raw ID -> its complete **projected public model object**, never the legacy raw metadata; an empty map for a zero-ID provider. |

The `counts` object carries the exact count field set:

| Field | Type | Definition |
|---|---|---|
| `gateways` | int | Number of **selected** providers, zero-ID providers included. |
| `endpoints` | int | Number of selected `(provider, raw model ID)` pairs. |
| `distinct_raw_ids` | int | Number of selected literal raw IDs. |
| `marker_group_labels` | int | Number of selected marker-group labels (delimiter-stripped, frozen empty-result guard). |
| `models` | int | A **documented legacy alias** equal to `marker_group_labels`. |

Counts describe the filtered response, not the entire registry: `gateways`
counts the selected providers, zero-ID providers included, never the whole
registry. `models` is the documented legacy alias equal to
`marker_group_labels`, never a verified underlying model identity. A filtered
`list_endpoints("synthetic-a")` reports only synthetic-a's `gateways`,
`endpoints`, ids and labels, and no registry-wide total leaks in. A zero-ID
single provider yields `gateways` 1 with all other counts zero.

Provider selection is all `provider_order` keys if `provider` is omitted or
`null`, else exactly the known selected provider. A registered **zero-ID**
provider is a success: its `gateways` entry still exists with `model_ids` `[]`
and `models` `{}` — never a no-match or missing-provider error.

Complete worked examples live as literal goldens in
`tests/fixtures/catalog/cases.json` — `endpoints_lookup_all`,
`endpoints_lookup_provider_a`, `endpoints_lookup_zero_provider_c` (an empty but
successful zero-ID selection), `endpoints_ordering_all` (natural raw-ID array
order over the ordering snapshot) and `endpoints_extension_all` (a new synthetic
gateway added by DATA alone: the `provider-extension.json` snapshot adds
`synthetic-d` and the same generic checkers accept it with no provider enum or
package-code edit).

## State layout (state/, gitignored)

- `roster.json`: providers + provider_models + tick_epoch + stale_providers + transients + unconfirmed + ratelimits. Never hand-edit — use --init.
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

## Removal resolver

The hourly tick settles the hold queue itself, but a 15-min resolver (`--resolve`)
accelerates it: holds whose deadline elapses between hourly passes drain on the
next quarter-hour instead of waiting a full cadence. The resolver shares the
same `pending_removals.settle` logic as the tick (never forked); it is a
schedule accelerator, not a second source of truth.

What `--resolve` does:
- Drains `pending_removals.json`: expired removals alert via the webhook,
  recovered models silently union back into the roster.
- Takes `monitor.lock` so it never overlaps a tick (the tick is the durable
  fallback — if the resolver crashes or misses a window, the next hourly pass
  picks up the remainder).
- Drops orphan providers (held entries for a provider no longer in `providers/`)
  from the queue with a stderr note — never fetches them, never touches the
  roster.

What `--resolve` does NOT do:
- Never touches `alive.json`, the 💚 alive ping, or the ⚠️ missed-tick warning
  — the hourly tick owns those clocks; the 15-min job must not masquerade as
  a tick.
- Never rebaselines (mutually exclusive with `--init`).
- Does not persist resolver-side `dropped_alerts_total` increments — drop
  accounting is a tick-owned metric; resolver increments are lost at process
  exit (at-least-once queueing in `pending_alerts.json` is unaffected).

Orphan release notes go to stderr on both settle paths (resolver and hourly
tick): a ghost provider's held entries are printed as dropped, then consumed.
Un-flagged releases are silent on both paths — both end in the same consumed
entry.

Registration (uses the same wrapper pattern as the tick job above; the
`*/15 * * * *` cadence matches the resolver's recheck rhythm):

```bash
hermes cron create --name inference-watchdog-resolve --no-agent \
  --script inference-watchdog-resolve.sh --deliver local "*/15 * * * *"
```

## Drop-a-provider / managing providers

Providers are plain JSON config files in `providers/`. The watchdog loads every
`*.json` at startup.

- **To add a provider**: drop a new JSON config file into `providers/`.
- **To untrack a provider**: set `"enabled": false` in its JSON. The config
  file stays put — no code change, no re-registration to reverse it. It
  disappears from the dashboard, the alerts, and the MCP provider list on
  the next tick, and is noted on stderr each run.
- **To remove a provider for good**: delete its JSON file. A file the
  loader cannot use is skipped with a `config: skipping ...` warning.

A file the loader cannot use (invalid JSON, missing required fields, an auth
method it does not implement) is skipped with a
`config: skipping providers/<name>.json: <reason>` warning on stderr and costs
exactly that provider — the rest of the roster still loads.

## Architecture

The watchdog is **config-driven**: none of the gateways are hard-coded in the
monitor logic. Each provider is a `providers/*.json` file whose schema the
loader validates at startup:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `name` | string | yes | Human-readable name |
| `base_url` | string | yes | API base URL (no trailing `/`) |
| `detection` | string | yes | Which free-model detection method to apply |
| `auth` | object | yes | Authentication config |
| `auth.method` | string | yes | `env_var`, `token_file`, or `none` |
| `auth.env_key` | string | if `env_var` | Env var name |
| `auth.path_env` | string | if `token_file` | Env var holding a path to a JSON token file (takes precedence over `auth.path`) |
| `auth.path` | string | if `token_file` | Literal path to a JSON token file (legacy `token_file` alternative to `path_env`) |
| `auth.key` | string | if `token_file` | Dot-separated JSON path to the token inside the file (e.g. `providers.nous.access_token`) |
| `display` | int | yes | Column order (0 = first) |
| `roster_key` | string | no | Explicit roster-key override; defaults to the config file's stem (see `config_loader._provider_key`) |
| `ignored_slugs` | list of strings | no | Exact model ids to exclude from tracking (see below) |
| `probe` | object | no | Zero-credit-probe dialect: `max_tokens` + `paid_signals` (see below) |
| `removal_hold_seconds` | int | no | When present, confirmed removals are held this many seconds before alerting. Absent = instant. Opt-in per provider. |
| `enabled` | bool | no | `false` untracks the provider while keeping its config file on disk. Absent = enabled. Must be a real JSON boolean — a quoted `"false"` is a string and is rejected. |

### Ignored slugs (`ignored_slugs`)

Optional per-provider list of **exact-match** model ids dropped after detection
so they never enter the roster — e.g. auto-router endpoints like
`kilo-auto/free` or `openrouter/free`, which pass the free-id rule but are not
real models. Excluded ids drop silently. A value that is not a list of
non-empty, unpadded strings breaks validation: the whole config file is skipped
with the standard `config: skipping ...` stderr warning.

### Probe dialect (`probe`)

Optional block for `zero-credit-probe` gateways: it moves the prober's
gateway-specific assumptions out of code and into the provider's JSON, so a
second zero-credit gateway needs no code edit. Two keys, both optional:

- `max_tokens` — positive integer, the completion size the probe fires.
  Defaults to 3 because b.ai rejects values of 2 or less.
- `paid_signals` — non-empty list of error bodies that mean "this key has no
  credit". Each entry is `{"status": <int>, "all_of": [<substrs>],
  "any_of": [<substrs>]}`: it matches when the HTTP status equals `status`,
  every `all_of` substring appears in the lower-cased body, and either there is
  no `any_of` key or at least one `any_of` substring appears. A matching signal
  verdicts the model PAID; anything else DEFERs and self-heals next tick.
  Supplying `paid_signals` REPLACES the built-in defaults rather than adding.

`providers/bai.json` carries b.ai's dialect explicitly — the built-in defaults
are only the fallback for a config that omits the block. A block of the wrong
shape costs exactly its own file with the standard `config: skipping ...` warning.

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
  the wrapper at 3600s (live `script_timeout_seconds`; historically 300s,
  briefly raised to 1800s on 2026-09-13, then restored to 300s the same
  day). One tick carries ~45 of 47 models at 5s operator pacing (46×5s
  sleeps + 47×~1s probes ≈ 280s exceeds the 260s budget); the short tail
  self-heals next tick via `probe_state.json` persistence (the deferred
  models re-queue as tier-1 arrivals). Even the pathological overshoot
  (~295s) lands the spiral-critical persist under the 3600s kill. The
  full tick (persist + the unconditional 180s recheck nap) can exceed
  300s — the invariant is that the persist precedes any kill: a kill
  during the recheck costs that tick's roster write (roster lags one
  tick), never probe progress. A truncated pass (the normal every-tick
  tail at 5s pacing, or deeper if probes burn their 30s timeouts)
  persists its probed subset and the next tick resumes (self-healing).

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

To add a provider, drop in a JSON config (see the schema table above); to change
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