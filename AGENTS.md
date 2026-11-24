# AGENTS.md — Free Inference Watchdog

## Hard rules
- Python stdlib only. Exactly ONE shipped module may import the MCP SDK/Pydantic: the standalone transport `watchdog_catalog/server.py`. The launcher `watchdog_catalog/__main__.py` imports it lazily, after argparse has finished; test modules may import `mcp`. The retired root local-state server is gone — never reintroduce a root server, forwarding shim, bridge or second registration path.
- Zero LLM tokens: the cron job is script-mode `--no-agent`. One tick per invocation: `python3 inference_watchdog.py` fetches, diffs, exits.
- Empty stdout is healthy-silent; diagnostics go to stderr. Exit codes: 0 normal, 1 partial provider failure or bootstrap refused, 2 fatal.

## Producer vs consumer
- Producer authority stays in this checkout: `inference_watchdog.py`, `config_loader.py`, `providers/*.json`, `state/`, the cron wrappers, `catalog_export.py`, `build_site.py`, credentials and the private roster.
- A consumer is the installed `free-inference-watchdog-mcp` console (or `python -m watchdog_catalog`) or the one-shot `free-inference-watchdog` (or `python -m watchdog_catalog.cli`), over the same three operations. It answers from exactly ONE explicitly selected published snapshot and needs no credentials, roster, cron or checkout.
- Imports point one way: the consumer package never imports a producer module, and no consumer path gets a local-state fallback.
- README.md is the user-facing contract (sections `Query the catalogue over MCP`, `Self-hosting the watchdog`, `Published catalogue snapshot (v2)`). Change code and README together.

## Package map
- `watchdog_catalog/__init__.py` — inert: importing it reads nothing and starts nothing.
- `watchdog_catalog/catalog.py` — raw-JSON acceptance, structural validation, the snapshot hash and the pure projection. No I/O or clock.
- `watchdog_catalog/query.py` — the pure core: marker-label stripper, ordering, exact `get_model`, paged `search_models`, `watchdog_status`. No source, clock or config access.
- `watchdog_catalog/source.py` — argument validation, acquisition, `DiskCache`, `CatalogSource` and `AsyncCatalogSource`. I/O only when called; clocks are caller-supplied seams read at operation time, never at construction.
- `watchdog_catalog/server.py` — the only SDK/Pydantic importer: typed registrations from Python signatures, reviewed descriptions copied from `watchdog_catalog/tools.json`. It reimplements no query, ordering or retry logic.
- `watchdog_catalog/__main__.py` — stdio launch and literal source selection; parsing and `--help` finish before the transport import.
- `watchdog_catalog/cli.py` — the one-shot native adapter: same source selection and operations, one acquisition and one pure call per invocation, terminal-safe rendering or `--json`. It owns no query, cache or timeout policy and imports no SDK.
- `watchdog_catalog/tools.json` — the shared contract: reviewed descriptions plus each tool's full `inputSchema`. The transport derives the advertised schema from its signatures and copies only description text, so never edit one side to move the other's constraints.
- Producer counterparts: `catalog_export.py` builds and seals ONE snapshot from resolved records plus a captured roster; `build_site.py` derives both public artifacts from it; `scripts/publish_site.sh` and `scripts/validate_site.py` gate publication; `scripts/check_page_sources.py` is the read-only inline-freshness gate.

## Public payload and identity (accepted — do not relax)
- Every public path carries the PROJECTED model only: `id`, `name`, `description`, `context_length`, `architecture`, `supported_parameters`. Unknown optional members project to `null`; private and unknown upstream fields are never serialized. `get_model` details add `description`, `chat_completions_url` and `api_type`; search rows omit them.
- There is no raw-metadata compatibility surface. Re-emitting original upstream metadata would be a NEW public contract, not a restoration.
- Provider metadata is stored raw under `roster.json` → `provider_models`; the projector reads exactly those members. Never change detection rules to match documentation.
- Endpoint identity is the exact `(provider, raw model_id)` pair: case-sensitive, untrimmed, never merged across providers. A free-marker strip produces a `watchdog_status` label count, not an identity.
- Metadata predicates are endpoint-specific; unknown metadata never becomes an invented capability, a zero or a "supported" answer.
- Observation time is the snapshot's `tick_epoch`; the injected `now` is the query's evaluation time, so `last_tick_age_s = now - tick_epoch`. Clock readers are the transport's `wall_clock`, `AsyncCatalogSource`'s `wall_clock`/`monotonic_clock` seams (at operation time only) and the producer's tick plus `state.py`. Never move a clock read into the pure core.
- Detection is selected PER PROVIDER by each config's `detection` key; there is no universal id rule. A stealth arrival is tracked only if its chosen method accepts the id the gateway assigns.
- These rules are pinned in `tests/test_contract_parity.py`, `tests/test_catalog_queries.py`, `tests/test_catalog_source.py` and `tests/test_mcp_protocol.py`; extend those rather than restating them here.

## Artifacts and publication
- There are exactly two generated site payloads under version control: `site/index.html` and `site/catalog.json`. Every other file under `site/` — locks, temp files, extra payloads — stays local-only; do not widen the allowlist. `web/catalog-query.js`, `web/webmcp.js` and `watchdog_catalog/tools.json` are maintained source inlined by the builder, not extra payloads.
- `build_site.py` builds one validated snapshot and derives both artifacts from it under one build lock; an interrupted or mixed pair fails validation and is never repaired. `scripts/validate_site.py` judges the pair read-only, and `scripts/check_page_sources.py` compares inlined script bodies with `web/` byte for byte. Neither rebuilds, and neither may read private roster state.
- `scripts/publish_site.sh` holds its own publish lock, rebuilds, re-validates and commits only those two paths when they changed. It pushes only with `--push`, never forces, rebases or rolls back, fails closed on unrelated or divergent history, and treats a zero exit as unpublished without an exact destination ref read-back.
- The tracked page and the deployed dashboard show the last PUBLISHED build; regenerating is a separate, separately authorized step — a local build is never publication, and never run the publisher merely to check documentation.
- The default consumer feed is `https://raw.githubusercontent.com/Dthen/free-inference-watchdog/master/site/catalog.json` (the exact `watchdog_catalog.source.DEFAULT_CATALOG_URL`); `https://models.dthen.xyz/catalog.json` is its Pages mirror, never an implicit fallback.

## Browser surface (WebMCP)
- The generated page registers the same three tools: `build_site.py` inlines `web/catalog-query.js`, `web/webmcp.js` and one bootstrap calling `WatchdogWebMCP.setup()`. The adapter feature-detects `document.modelContext.registerTool`, builds each descriptor from the embedded `tool-contract` island (a defensive copy, never a second schema table), and annotates every tool `readOnlyHint: true`, `untrustedContentHint: true`, `consequentialHint: false`.
- Results are complete canonical JSON text plus `source: {mode: "browser"}` — no browser-only fields, truncation or fake pagination. Availability is conditional: a secure context plus `chrome://flags/#enable-webmcp-testing` or an enrolled origin trial (Chrome 149+), with cross-origin iframes needing the `tools` Permissions Policy. Trial enrolment is optional, separately authorized and never performed here.
- This is an in-page API, not a remotely connectable MCP HTTP URL: no polyfill, no backend, no hosted transport. Keep native-API churn inside the detection step and allow one registration attempt per document.

## Config traps
- Optional fields are validated PER-FILE and strictly: a bad value skips that one config with a `config: skipping ...` warning, never the roster.
- Bools must be real JSON booleans: `bool("false")` is `True`. If you add one, reject anything that is not `isinstance(x, bool)`.
- `display` defaults to 0 and orders every user-visible list; `signup_url`/`limits_note` feed the dashboard only; `removal_hold_seconds` is opt-in per provider.

## Alert hygiene
- A failed fetch is sticky carry-forward of last-known ids: a failed provider retains its last-known models; it does not create mass removals.
- Every diff is re-fetched after `recheck_delay` (180s) before it is believed.
- Both removal settle paths share `pending_removals.settle`; never fork it, and resolve passes never touch `alive.json`.

## Tests and docs
- Fast default: `python3 -m pytest tests/ -q`. Integration only: `-m integration`. Explicit full run: `python3 -m pytest -o addopts='' tests/ -q`. Run focused files while editing.
- The one `integration` marker is registered in `pyproject.toml` and assigned in `tests/conftest.py`; do not add a second marker or infer ownership from source shape.
- The fast suite needs pytest plus the SDK pins; integration cases need `FREE_INFERENCE_WATCHDOG_WHEELHOUSE`, `FREE_INFERENCE_WATCHDOG_UV`, the interpreter overrides and `CONTRACT_NODE`, and fail rather than skip. Operator read-backs skip unless `--operator-tick-wrapper=PATH` is passed.
- `tests/test_readme.py` EXECUTES the documentation: fenced JSON, shell blocks, the stdio vector, the worked results, both wrapper bodies and every internal anchor. Editing README.md is a code change — re-run it.
- One README, not two: the repo carries `README.md`, `AGENTS.md` and `LICENSE`.
