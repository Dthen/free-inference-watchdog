#!/usr/bin/env python3
"""mcp_server.py — MCP query surface over Free Inference Watchdog state.

Four read-only tools, no enrichment (none exists anywhere in the repo):

  list_free_models(provider=None)   full roster, or one provider's id list
                                    plus raw per-model metadata (provider_models)
  get_model(model_id)               CROSS-GATEWAY PRESENCE LOOKUP: which of the
                                    watched gateways track this id right now;
                                    each endpoint carries the raw metadata dict
  watchdog_status()                 tick freshness vs the 1h cadence, stale/
                                    failing providers, per-provider counts,
                                    metadata coverage, pending-alert queue
                                    depth, last site publish
  list_endpoints(provider=None)     per-gateway wiring entries
                                    (chat-completions URL,
                                    api_type), raw tracked ids
                                    and their raw metadata

IMPORT CHOICE (corrected fact, probe-verified 2026-08-26 on this box): the
installed MCP SDK no longer ships `mcp.server.fastmcp.FastMCP`. The current
surface is `from mcp.server.mcpserver import MCPServer` with `.add_tool()`,
`.call_tool()`, and `run_stdio_async()` for the stdio loop — all probed under
the Hermes venv interpreter before this module was written. The SDK import is
guarded (the ONLY non-stdlib import allowed here): the tool FUNCTIONS stay
importable and directly testable under any Python, while build_server()/main()
degrade with a clear error where the SDK is absent. Registration into
~/.hermes/config.yaml is deliberately OUT of scope (orchestrator step).

HONESTY CONTRACT (Dthen, 2026-08-25): gateways rename inconsistently, so
get_model reports EXACT id matches per provider plus an explicit caveat that
cross-gateway ABSENCE is unreliable precisely because of such renames. No
alias map, no allowlist, no normalized-name matching — exact ids only.

Never raises into the MCP protocol layer: every tool returns structured
error payloads on missing/corrupt/hostile state. (_StateError below is an
internal control-flow sentinel only — it is always caught inside the tool
function that triggered it.) State files are read exclusively through
state.py loaders, inheriting their hostile-file degradation (RecursionError
gate, junk-type drops) for free.

Usage: python3 mcp_server.py [--root DIR]   (default root = repo dir)
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent

try:
    # Corrected-fact import — see module docstring. Guarded so the pure
    # functions below remain importable (and unit-testable) on interpreters
    # without the SDK; only build_server()/main() need it.
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover - exercised only off the venv
    MCPServer = None

import state

# Gateway names in Dthen's DISPLAY order, imported from config_loader (the
# providers/*.json `display` fields are the single source — no duplicate
# list lives in module code; test pins are expectations, not sources).
# Dict-key order is display order.
from config_loader import PROVIDERS as PROVIDER_KEYS
PROVIDERS = PROVIDER_KEYS

# Gateway wiring (base URL, api_type) and the free-marker stripper
# — imported from their single sources of truth so MCP tools and the site
# builder never drift. The stripper lives in the pure, side-effect-free
# watchdog_catalog.query module; the site builder re-exports the same object.
from config_loader import GATEWAY_WIRING
from watchdog_catalog.query import strip_free_marker
from watchdog_catalog import query as _query

# Local-to-public producer adapter plus the pure structural module it composes.
# Both are pure: importing them pulls no SDK, provider config, state, page
# renderer or environment access (the producer carries its own fresh-process
# import audit). The U1a capture/sanitize/snapshot helpers below are their only
# users; list_free_models delegates through them to the pure query.
from watchdog_catalog import catalog as _catalog
import catalog_export as _catalog_export

# Tick cadence, duplicated from inference_watchdog.DEFAULT_CADENCE_S
# (cross-reference): importing the watchdog module for one integer would
# drag its whole runtime into the MCP server, so this stays a literal.
CADENCE_S = 1 * 3600


class _StateError(Exception):
    """Internal-only: carries a ready-to-return structured error payload.

    Never escapes a tool function — each tool catches it at its own boundary
    and converts it to the payload, so the MCP protocol layer only ever sees
    returned dicts.
    """

    def __init__(self, payload):
        super().__init__(payload.get("error", "state error"))
        self.payload = payload


def _model_metadata(roster, provider, ids):
    """Raw objects for tracked ids only; legacy/junk metadata degrades to {}."""
    providers = roster.get("provider_models")
    if not isinstance(providers, dict):
        return {}
    models = providers.get(provider)
    if not isinstance(models, dict):
        return {}
    return {mid: models[mid] for mid in ids if isinstance(models.get(mid), dict)}


def _clean_names(values):
    """String entries from a roster list field (stale_providers); a
    non-list value degrades to [] — a hostile bare string like "zen" is
    dropped whole instead of char-splitting into ['z', 'e', 'n']."""
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, str)]


def _load_roster_checked(root: Path, base: dict) -> dict:
    """Roster via state.load_roster (hostile-file safe), or _StateError.

    state.load_roster collapses missing/corrupt/non-dict into None, so the
    existence check here only sharpens the MESSAGE (missing vs corrupt);
    the degradation class itself stays inherited.
    """
    path = root / "state" / "roster.json"
    if not path.exists():
        raise _StateError(
            _error(base, f"no roster found at {path}"))
    roster = state.load_roster(path)
    if not isinstance(roster, dict):
        raise _StateError(
            _error(base, f"corrupt or unreadable roster at {path}"))
    if not isinstance(roster.get("providers"), dict):
        raise _StateError(_error(
            base, f"corrupt roster at {path}: 'providers' is not a map"))
    return roster


def _error(base: dict, message: str, **extra) -> dict:
    """Structured error payload — NEVER an exception into the MCP layer."""
    out = dict(base)
    out["ok"] = False
    out["error"] = message
    out.update(extra)
    return out


def _capture_registry(base: dict) -> list:
    """Capture the resolved local registry ONCE as four-field public records.

    Reads the existing ``PROVIDERS`` and ``GATEWAY_WIRING`` module roots (both
    already resolved by the legacy config loader) and returns a NEW list of NEW
    dicts carrying exactly ``key``/``name``/``chat_completions_url``/``api_type``
    in PROVIDERS insertion order. Only the two ROOTS are guarded: a non-dict
    root raises the existing ``_StateError`` with the fixed, non-reflective
    structural message. A missing or malformed per-key config/wiring does NOT
    raise during capture -- its required wiring slots are forced to ``None`` so
    the accepted builder rejects it later, after the caller's argument and state
    validation have run. ``name`` is read only from a dict config (an
    empty/non-string name is a legitimate accepted fallback and never discards
    good wiring). Every other config/wiring member (tokens, auth, base_url,
    private objects, ...) is dropped and never traversed, copied or serialized;
    no credential, key or URL is re-derived and no I/O happens.
    """
    configs = PROVIDERS
    wiring = GATEWAY_WIRING
    if not isinstance(configs, dict) or not isinstance(wiring, dict):
        raise _StateError(_error(base, "Invalid catalogue snapshot."))
    records = []
    for key in configs:
        config = configs.get(key)
        wiring_entry = wiring.get(key)
        name = config.get("name") if isinstance(config, dict) else None
        if isinstance(config, dict) and isinstance(wiring_entry, dict):
            url = wiring_entry.get("chat_completions_url")
            api_type = wiring_entry.get("api_type")
        else:
            # A non-dict config OR wiring forces the required slots to None, so
            # a malformed config can never hide behind optional-name fallback or
            # otherwise-valid wiring. Left for the accepted builder to reject.
            url = None
            api_type = None
        records.append({
            "key": key,
            "name": name,
            "chat_completions_url": url,
            "api_type": api_type,
        })
    return records


def _presanitized_roster(roster, registry) -> dict:
    """Build the LOCAL-only shallow sanitized roster for the accepted adapter.

    ``roster`` is one captured roster observation; ``registry`` is the captured
    four-field record list (a precondition -- this helper never walks the module
    globals again). A non-dict ``roster`` or a missing/non-dict ``providers``
    map raises the accepted ``catalog.CatalogError`` (``invalid_snapshot``)
    rather than degrading corruption to a false empty success; the public caller
    still runs its own ``_load_roster_checked`` first.

    The result is a NEW shallow root dict plus a NEW ``providers`` map keyed by
    EXACTLY the captured registry keys in captured order. A missing/non-list
    value becomes ``[]``; a list keeps only NONEMPTY ``str`` IDs, literal and
    untrimmed, preserving duplicates and input order HERE (the accepted adapter
    owns canonical sorted/set; the pure query owns natural order). Unknown
    providers are excluded even when malformed. Every other root member is
    passed through by reference, so the accepted projector still sees the ACTUAL
    absent/None/malformed metadata containers and the caller keeps the original
    raw objects for later restoration. The input roster is never mutated.
    """
    if not isinstance(roster, dict):
        raise _catalog.CatalogError(_catalog.INVALID_SNAPSHOT)
    raw_providers = roster.get("providers")
    if not isinstance(raw_providers, dict):
        raise _catalog.CatalogError(_catalog.INVALID_SNAPSHOT)
    clean = dict(roster)  # NEW shallow root; untouched members alias by reference
    providers = {}
    for record in registry:
        key = record["key"]
        source = raw_providers.get(key)
        if isinstance(source, list):
            providers[key] = [mid for mid in source
                              if isinstance(mid, str) and mid]
        else:
            providers[key] = []
    clean["providers"] = providers
    return clean


def _local_snapshot(roster, registry, base: dict) -> dict:
    """Assemble the validated public snapshot from captured in-memory inputs.

    Uses the SUPPLIED captured ``registry`` (it never recaptures), builds the
    local-only sanitized roster ONCE through :func:`_presanitized_roster`, then
    calls the accepted producer's ``catalog_export.build_snapshot`` ONCE with
    the frozen ``CADENCE_S``. Returns the EXACT validated object the accepted
    builder returns -- no clone, reseal, revalidation or second query algorithm.

    Only the accepted ``catalog.CatalogError`` is caught, from either the
    sanitizer or the builder, and mapped to the existing fixed local
    ``_StateError`` payload (base + ``ok`` False + the string error); a bad
    cadence, record, key, wiring or root therefore stays truthful instead of
    becoming an empty success. No state, clock, pending-queue or filesystem
    access happens here, and no input is mutated.
    """
    try:
        clean = _presanitized_roster(roster, registry)
        return _catalog_export.build_snapshot(
            registry, clean, cadence_s=CADENCE_S)
    except _catalog.CatalogError:
        raise _StateError(_error(base, "Invalid catalogue snapshot."))


# ---------- tool: list_free_models ----------

def list_free_models(provider=None, root=None) -> dict:
    """Full roster, or one provider's id list. Structured errors, no raises.

    Local compatibility wrapper: the captured registry and the roster read ONCE
    through state.py are adapted to the validated public snapshot, and the
    selection, ordering and counting are delegated to the pure
    ``watchdog_catalog.query`` core. Only the OUTPUT is adapted back -- the
    already-selected ``(provider, raw id)`` pairs get their original raw
    metadata dictionaries restored, and the snapshot provenance
    (``snapshot_id``, ``provider_staleness``, ``warnings``) is carried through.

    Additive: also returns ``models`` — the raw API metadata objects for
    exactly the tracked ids, per gateway ({} when the roster predates
    metadata capture or the model carries none)."""
    r = Path(root).resolve() if root is not None else REPO
    base: dict = {"tool": "list_free_models", "state_root": str(r)}

    # Capture the resolved registry ONCE. A bad registry root outranks every
    # argument and state error, so a broken local wiring is never masked by a
    # misspelled provider name.
    try:
        registry = _capture_registry(base)
    except _StateError as exc:
        return exc.payload
    # Valid provider names are ONLY the captured records with a nonempty string
    # key. A malformed captured key stays in the registry for the builder's
    # later structural rejection but never reaches join() for error formatting.
    valid_providers = [rec["key"] for rec in registry
                       if isinstance(rec["key"], str) and rec["key"]]

    if provider is not None and (
            not isinstance(provider, str) or provider not in valid_providers):
        # Arg validation outranks state problems: the caller asked for a
        # specific thing and misspelled it — tell them the valid names.
        return _error(
            base, f"unknown provider {provider!r}; "
                  f"valid providers: {', '.join(valid_providers)}",
            valid_providers=list(valid_providers))
    try:
        roster = _load_roster_checked(r, base)
        snapshot = _local_snapshot(roster, registry, base)
    except _StateError as exc:
        return exc.payload

    # Delegate selection/order/count to the pure core ONCE (including the
    # all-provider case). The whole decoded argument object is passed; the pure
    # argument validation stays the backstop, never the primary boundary.
    result = _query.list_free_models(snapshot, {"provider": provider})
    if not result.get("ok"):
        # Backstop only: with the real capture/validation above the core cannot
        # reject the provider here. Map either declared provider argument code
        # to the SAME existing local provider error, using the OUTER caller's
        # value (the core envelope carries no value of its own).
        return _error(
            base, f"unknown provider {provider!r}; "
                  f"valid providers: {', '.join(valid_providers)}",
            valid_providers=list(valid_providers))

    # Fresh outer mapping: the spy-observed core result is never mutated.
    out = {**base, **result}
    out["warnings"] = list(snapshot["warnings"])
    if provider is not None:
        out["models"] = _model_metadata(
            roster, result["provider"], result["model_ids"])
    else:
        out["models"] = {gw: _model_metadata(roster, gw, ids)
                         for gw, ids in result["providers"].items()}
    return out


# ---------- tool: get_model ----------

ABSENCE_CAVEAT = (
    "Cross-gateway ABSENCE is unreliable: gateways rename inconsistently, "
    "so a missing exact id does NOT prove the model is unavailable there."
)


def get_model(model_id, root=None) -> dict:
    """Which gateways track this exact id right now — exact only, caveated.

    Local compatibility wrapper: the captured registry and the roster read ONCE
    through state.py are adapted to the validated public snapshot, and the
    exact membership, the separate marker-insensitive endpoint lookup, their
    order and the counts are delegated to the pure ``watchdog_catalog.query``
    core. Only the OUTPUT is adapted back -- each selected endpoint carries the
    legacy ``gateway`` key and its ORIGINAL raw metadata dictionary restored
    from the same in-memory roster, while the core provenance
    (``snapshot_id``, ``tick_epoch``, ``provider_staleness``, ``counts``,
    ``warnings``) passes through.

    ``model_id`` must be a string containing at least one character outside
    the frozen six ASCII blanks (space, tab, LF, CR, VT, FF); NBSP and other
    non-ASCII whitespace stay LITERAL query characters. Empty or
    ASCII-blank-only queries are rejected before any state read, and the
    identifier is never trimmed.

    ``exact_matches`` stays STRICTLY exact — the honest raw answer, never
    grouped — while ``endpoints`` is the separate marker-insensitive wiring
    lookup. Each endpoint keeps the core ``stale`` flag and the gateway's raw
    metadata object ({} when the roster carries none or the hierarchy is
    malformed).
    """
    r = Path(root).resolve() if root is not None else REPO
    base: dict = {"tool": "get_model", "state_root": str(r)}

    # Capture the resolved registry ONCE. A bad registry root outranks every
    # argument and state error, so broken local wiring is never masked by a
    # bad query.
    try:
        registry = _capture_registry(base)
    except _StateError as exc:
        return exc.payload

    # The frozen six ASCII blanks: never Python's default Unicode-whitespace
    # strip, which would wrongly reject a literal NBSP (or EM SPACE) query.
    if not isinstance(model_id, str) or not model_id.strip(" \t\n\r\v\f"):
        return _error(base, "model_id must be a non-empty string")

    try:
        roster = _load_roster_checked(r, base)
        snapshot = _local_snapshot(roster, registry, base)
    except _StateError as exc:
        return exc.payload

    # Delegate membership, the separate marker lookup, order and counts to the
    # pure core ONCE. The core's argument validation stays a defensive
    # backstop: the local checks above already rejected every value it can
    # reject, so this only fires on a wrapper-level inconsistency.
    result = _query.get_model(snapshot, {"model_id": model_id})
    if not result.get("ok"):
        return _error(base, "model_id must be a non-empty string")

    # Fresh outer mapping: neither the validated snapshot nor the returned core
    # result is mutated by the local adaptation below.
    out = {**base, **result}
    out["warnings"] = list(snapshot["warnings"])
    # Adapt ONLY each endpoint the core selected, in its returned order: rename
    # ``provider`` to the legacy ``gateway``, preserve every other core field
    # (including ``stale``), and restore the ORIGINAL raw metadata object for
    # that exact (gateway, raw id) pair from the one retained roster.
    endpoints = []
    for endpoint in result["endpoints"]:
        provider = endpoint["provider"]
        raw_id = endpoint["model_id"]
        local = {("gateway" if key == "provider" else key): value
                 for key, value in endpoint.items()}
        metadata = _model_metadata(roster, provider, [raw_id])
        local["metadata"] = metadata.get(raw_id, {})
        endpoints.append(local)
    out["endpoints"] = endpoints
    return out


# ---------- tool: list_endpoints ----------

def list_endpoints(provider=None, root=None) -> dict:
    """All gateway wiring entries across the roster, or one gateway's.

    Local compatibility wrapper: the captured registry and the roster read ONCE
    through state.py are adapted to the validated public snapshot, and provider
    selection, raw-ID ordering and every count are delegated to the pure
    ``watchdog_catalog.query`` core. Only the OUTPUT is adapted back -- each
    selected gateway keeps exactly its four fields while its ``models`` map is
    rebuilt from the ORIGINAL raw metadata dictionaries of the same retained
    roster, and the snapshot provenance (``snapshot_id``, ``tick_epoch``,
    ``provider_staleness``, ``warnings``) is carried through.

    ``counts.gateways`` is the number of SELECTED providers (zero-ID providers
    included), never the full registry; ``counts.endpoints`` counts selected
    ``(provider, raw id)`` pairs, ``counts.distinct_raw_ids`` the selected
    literal raw strings, ``counts.marker_group_labels`` the selected
    delimiter-stripped labels, and the legacy ``counts.models`` is the
    documented alias of that label count -- never a verified model identity.
    ``gateways[*].models`` restores raw tracked metadata ({} when the roster
    predates metadata capture or the hierarchy/entry is malformed).
    """
    r = Path(root).resolve() if root is not None else REPO
    base: dict = {"tool": "list_endpoints", "state_root": str(r)}

    # Capture the resolved registry ONCE. A bad registry root outranks every
    # argument and state error, so a broken local wiring is never masked by a
    # misspelled provider name.
    try:
        registry = _capture_registry(base)
    except _StateError as exc:
        return exc.payload
    # Valid provider names are ONLY the captured records with a nonempty string
    # key. A malformed captured key stays in the registry for the builder's
    # later structural rejection but never reaches join() for error formatting.
    valid_providers = [rec["key"] for rec in registry
                       if isinstance(rec["key"], str) and rec["key"]]

    if provider is not None and (
            not isinstance(provider, str) or provider not in valid_providers):
        # Arg validation outranks state problems: the caller asked for a
        # specific gateway and misspelled it — tell them the valid names.
        return _error(
            base, f"unknown provider {provider!r}; "
                  f"valid providers: {', '.join(valid_providers)}",
            valid_providers=list(valid_providers))
    try:
        roster = _load_roster_checked(r, base)
        snapshot = _local_snapshot(roster, registry, base)
    except _StateError as exc:
        return exc.payload

    # Delegate selection/order/count to the pure core ONCE (including the
    # all-provider case). The whole decoded argument object is passed; the pure
    # argument validation stays the backstop, never the primary boundary.
    result = _query.list_endpoints(snapshot, {"provider": provider})
    if not result.get("ok"):
        # Backstop only: with the real capture/validation above the core cannot
        # reject the provider here. Map either declared provider argument code
        # to the SAME existing local provider error, using the OUTER caller's
        # value (the core envelope carries no value of its own).
        return _error(
            base, f"unknown provider {provider!r}; "
                  f"valid providers: {', '.join(valid_providers)}",
            valid_providers=list(valid_providers))

    # Fresh outer mapping: the spy-observed core result is never mutated.
    out = {**base, **result}
    out["warnings"] = list(snapshot["warnings"])
    # Adapt ONLY the gateways the core selected, in its returned order: keep the
    # exact four fields and replace only ``models`` with the ORIGINAL raw
    # metadata objects for the returned raw IDs of the one retained roster.
    gateways = {}
    for key, entry in result["gateways"].items():
        local = dict(entry)
        local["models"] = _model_metadata(roster, key, entry["model_ids"])
        gateways[key] = local
    out["gateways"] = gateways
    return out


# ---------- tool: watchdog_status ----------

def watchdog_status(now=None, root=None) -> dict:
    """Catalogue health from the pure core, plus LOCAL operator diagnostics.

    Local compatibility wrapper: the captured registry and the roster read ONCE
    through state.py are adapted to the validated public snapshot, and catalogue
    health — tick age/freshness, provider staleness, every per-provider count,
    the whole-catalogue cardinalities and the projected metadata coverage — is
    delegated to the pure ``watchdog_catalog.query`` core with a fixed clock.
    ``snapshot_id``, strict ``tick_epoch``, ``cadence_s``, ``counts``,
    ``metadata_coverage``, ``provider_staleness`` and the core ``warnings`` UNION
    (snapshot warnings plus the derived ``tick_epoch_unknown`` /
    ``tick_epoch_future`` / ``tick_stale`` codes) are carried through unchanged.
    ``snapshot_id`` binds that LOCAL sanitized public projection, NOT the raw
    local metadata dictionaries, the pending queue or the site mtime, so it can
    differ from a strictly published feed's id for the same tick.

    Freshness is strict and inclusive: a null or future ``tick_epoch`` gives
    ``last_tick_age_s`` None and ``tick_fresh`` False (never a healthy clamp or
    a negative age), and an age exactly equal to ``cadence_s`` is still fresh.

    ``now`` is the legacy LOCAL clock parameter (never a public tool argument).
    ``None`` reads ``time.time()`` exactly ONCE; an explicit native int or
    finite native float is used as given — ``0`` is literal and never triggers
    a fallback clock read. The value must be non-negative and within
    ``2**53 - 1``; a bool, non-number, NaN/Infinity, negative value (including a
    negative fraction, which ``int()`` would truncate to a fake healthy ``0``)
    or unsafe number is rejected with the flat local string
    ``now must be a finite non-negative safe number`` BEFORE any roster, site,
    pending-queue or core work, without echoing the value. The validated number
    is converted with ``int()`` exactly ONCE for the pure core's fixed clock,
    while the RAW validated numeric clock drives the legacy filesystem
    diagnostic so fractional file mtimes keep their sub-second contribution.

    The core payload is catalogue health only: its own ``pending_alerts``,
    ``site_published`` and ``site_publish_age_s`` placeholders stay ``None``.
    This LOCAL wrapper overwrites them with genuinely observed operator state
    (pending queue depth; site presence plus ``max(0, int(raw_now - mtime))``),
    and keeps the legacy carried ``stale_providers`` cleaned string list in its
    original order/duplicates (inactive names included) — core
    ``provider_staleness`` covers active providers only and is independent of
    the tick age. ``metadata_summary`` stays a LOCAL raw-dictionary PRESENCE
    summary over the core-selected ``(provider, raw id)`` scope: an empty raw
    dict counts, a missing/non-dict value does not. It measures something else
    than the projected-field ``metadata_coverage`` the core reports, and the two
    are not aliases.

    State unreadable (missing/corrupt/non-object roster, bad ``providers`` map)
    still returns the existing legacy failure payload with ``cadence_s`` and
    ``pending_alerts`` None / ``site_published`` False — never fabricated
    healthy zeros. A structurally invalid local snapshot/wiring is the fixed
    ``Invalid catalogue snapshot.`` error, and the core's frozen argument
    envelope (it accepts only ``{}``) maps to a flat local string error.
    Programming exceptions are not swallowed.
    """
    r = Path(root).resolve() if root is not None else REPO
    base: dict = {"tool": "watchdog_status", "state_root": str(r)}

    # Capture the resolved registry ONCE. A bad registry/wiring root outranks
    # every clock and state error, so broken local wiring is never masked.
    try:
        registry = _capture_registry(base)
    except _StateError as exc:
        return exc.payload

    # ONE clock observation, validated BEFORE any state/filesystem I/O. None is
    # read exactly once; an explicit value (including 0) is used literally.
    raw_now = time.time() if now is None else now
    if isinstance(raw_now, bool) or not isinstance(raw_now, (int, float)):
        return _error(base, "now must be a finite non-negative safe number")
    if isinstance(raw_now, int):
        # Integer bounds, never math.isfinite(int): a huge int would raise
        # OverflowError on the float conversion instead of failing flat.
        clock_ok = 0 <= raw_now <= 9007199254740991
    else:
        # Reject negatives BEFORE int(): int(-0.5) truncates to a fake 0.
        clock_ok = (math.isfinite(raw_now)
                    and 0 <= raw_now <= 9007199254740991)
    if not clock_ok:
        return _error(base, "now must be a finite non-negative safe number")
    now_int = int(raw_now)  # exactly once, for the pure core precondition

    try:
        roster = _load_roster_checked(r, base)
    except _StateError as exc:
        # State unreadable: surface the failure payload plus UNKNOWN for the
        # optional fields — ok:False must never masquerade as healthy zeros (a
        # fabricated 0 alert depth would read as "queue drained").
        payload = exc.payload
        payload.setdefault("cadence_s", CADENCE_S)
        payload["pending_alerts"] = None
        payload["site_published"] = False
        return payload

    # Structural local wiring/snapshot fault: the accepted adapter boundary maps
    # it to the existing fixed, non-reflective catalogue message — never a
    # partially successful health payload.
    try:
        snapshot = _local_snapshot(roster, registry, base)
    except _StateError as exc:
        return exc.payload

    # Delegate catalogue health, freshness and every count/cardinality/coverage
    # projection to the pure core ONCE. No public arguments exist: pass the
    # empty object explicitly and the normalized fixed clock by keyword.
    result = _query.watchdog_status(snapshot, {}, now=now_int)
    if not result.get("ok"):
        # Backstop only: the local checks above always pass ``{}``. Adapt the
        # core's frozen argument envelope to the flat local string form.
        return _error(base, "Invalid tool arguments.")

    # Fresh outer mapping: neither the validated snapshot nor the returned core
    # object is mutated by the local diagnostics below.
    out = {**base, **result}

    # LOCAL carried observation (original order/duplicates, inactive included).
    out["stale_providers"] = _clean_names(roster.get("stale_providers", []))

    # LOCAL raw-dictionary PRESENCE summary over the snapshot's captured
    # provider scope and each provider's SELECTED ids — never another globals
    # walk, another roster read or a projected-field count. An empty raw dict is
    # present; a missing or non-dict value is not.
    order = snapshot["provider_order"]
    providers = snapshot["providers"]
    raw_counts = {}
    for gw in order:
        raw_counts[gw] = len(_model_metadata(
            roster, gw, providers[gw]["model_ids"]))
    out["metadata_summary"] = {
        # Pair count (gateway, model) over unique selected ids — not a union of
        # model identities, which gateways rename.
        "models_with_metadata": sum(raw_counts.values()),
        "provider_counts": raw_counts,
    }

    # LOCAL operator observations (kept out of the core object and the hash).
    pending_path = r / "state" / "pending_alerts.json"
    site = r / "site" / "index.html"
    out["pending_alerts"] = len(state.load_pending(pending_path))
    try:
        mtime = site.stat().st_mtime
    except OSError:
        out["site_published"] = False
        out["site_publish_age_s"] = None
    else:
        out["site_published"] = True
        # RAW validated clock, not the normalized int: a fractional `now` and a
        # fractional file mtime must not lose their sub-second contribution.
        out["site_publish_age_s"] = max(0, int(raw_now - mtime))
    return out


# ---------- MCP transport wiring ----------

_WATCHED_GATEWAYS = ", ".join(PROVIDERS)

TOOL_DESCRIPTIONS = {
    "list_free_models":
        "List free-tier model ids across the watched gateways "
        f"({_WATCHED_GATEWAYS}). Pass provider=<name> for one "
        "gateway's id list; omit it for the full roster with per-gateway "
        "counts. Read-only snapshot of the latest watchdog tick.",
    "get_model":
        "Cross-gateway PRESENCE lookup: which of the watched gateways track "
        "this exact model id right now. Returns exact_matches per gateway "
        "(exact ids only — gateways rename inconsistently, so cross-gateway "
        "absence is unreliable; see the caveats attached to every result). "
        "Also returns 'endpoints' — one wiring entry (base URL, "
        "api_type) per (gateway, raw id) in the roster whose stripped name "
        "matches the query, so get_model('foo') and get_model('foo:free') "
        "return the same wiring list.",
    "watchdog_status":
        "Watchdog health: last tick age vs the 1h cadence, stale/failing "
        "providers, per-gateway model counts, pending-alert queue depth, "
        "and the age of the last static-site publish.",
    "list_endpoints":
        "All gateway wiring entries across the roster: for each gateway, "
        "its base URL, api_type, and the raw model ids tracked. "
        "Pass provider=<name> for one gateway; omit for all. Read-only "
        "snapshot of the latest watchdog tick.",
}


def build_server(root=None):
    """MCPServer with the four tools closed over `root`.

    Lambdas (not direct function refs) keep the exposed schemas clean:
    the transport-facing signatures carry only real tool arguments, while
    the test-facing `root=` kwarg stays an implementation detail.
    """
    if MCPServer is None:
        raise RuntimeError(
            "the 'mcp' SDK is not installed in this interpreter; "
            "run the server under the Hermes venv python")

    srv = MCPServer(
        name="free-inference-watchdog",
        title="Free Inference Watchdog",
        instructions=(
            "Read-only queries over the free-inference-watchdog roster "
            "state (refreshed hourly). Presence answers are exact-id "
            "based; never read cross-gateway absence as proof of "
            "unavailability. Use list_endpoints to get wiring (base URL, "
            "api_type) for actually calling a model."),
    )
    srv.add_tool(lambda provider=None: list_free_models(provider, root=root),
                 name="list_free_models",
                 description=TOOL_DESCRIPTIONS["list_free_models"])
    srv.add_tool(lambda model_id: get_model(model_id, root=root),
                 name="get_model",
                 description=TOOL_DESCRIPTIONS["get_model"])
    srv.add_tool(lambda: watchdog_status(root=root),
                 name="watchdog_status",
                 description=TOOL_DESCRIPTIONS["watchdog_status"])
    srv.add_tool(lambda provider=None: list_endpoints(provider, root=root),
                 name="list_endpoints",
                 description=TOOL_DESCRIPTIONS["list_endpoints"])
    return srv


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Free Inference Watchdog MCP server (stdio transport).")
    ap.add_argument("--root", type=Path, default=REPO,
                    help="project root containing state/ and site/ "
                         "(default: repo dir)")
    args = ap.parse_args(argv)
    srv = build_server(args.root)
    # run_stdio_async is a COROUTINE function (caught by the stdio smoke
    # test: a bare call silently created an unawaited coroutine and exited).
    # asyncio.run drives the stdio read/write loop to completion.
    # SHUTDOWN SEMANTICS (documented limitation): shutdown relies on the
    # MCP client closing stdin; a bare SIGINT alone does not terminate
    # mid-session — well-behaved clients always close stdin, so this only
    # matters for ad-hoc manual runs (kill the process there instead).
    asyncio.run(srv.run_stdio_async())
    return 0


if __name__ == "__main__":
    sys.exit(main())
