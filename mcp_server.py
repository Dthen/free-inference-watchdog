#!/usr/bin/env python3
"""mcp_server.py — MCP query surface over Free Inference Watchdog state.

Three read-only tools, no enrichment (none exists anywhere in the repo):

  list_free_models(provider=None)   full roster, or one provider's id list
  get_model(model_id)               CROSS-GATEWAY PRESENCE LOOKUP: which of
                                    the six gateways track this id right now
  watchdog_status()                 tick freshness vs the 1h cadence, stale/
                                    failing providers, per-provider counts,
                                    pending-alert queue depth, last site publish

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
import re
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

# Gateway names in Dthen's DISPLAY_ORDER. DUPLICATED from build_site.py:63
# (cross-reference) rather than imported: importing build_site would drag the
# site builder's module surface along just for six strings, and display
# order is curation, not implementation detail — if the canonical tuple ever
# moves, grep for this comment.
PROVIDERS = ("nous", "zen", "kilo", "cline", "openrouter", "command_code")

# Gateway wiring (base URL, auth shape, api_type) and the free-marker stripper
# — imported from their single sources of truth so MCP tools and the site
# builder never drift.
from providers import GATEWAY_WIRING
from build_site import strip_free_marker

# Tick cadence, duplicated from inference_watchdog.DEFAULT_CADENCE_S
# (cross-reference) for the same decoupling reason.
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


def _natural_key(s):
    """Split digit runs so 'gemma-4-26b' sorts after 'gemma-4-9b'."""
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]


def _epoch_or_none(roster):
    """Finite numeric tick_epoch as int, else None (bool/junk dropped)."""
    tick = roster.get("tick_epoch")
    if isinstance(tick, (int, float)) and not isinstance(tick, bool) \
            and math.isfinite(tick):
        return int(tick)
    return None


def _clean_ids(models):
    """Sorted string ids from a provider value; hostile entries dropped."""
    if not isinstance(models, list):
        return []
    return sorted((mid for mid in models if isinstance(mid, str)),
                  key=_natural_key)


def _clean_names(values):
    """String entries from a roster list field (stale_providers); a
    non-list value degrades to [] — same rule as _clean_ids, so a hostile
    bare string like "zen" is dropped whole instead of char-splitting into
    ['z', 'e', 'n']."""
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


# ---------- tool: list_free_models ----------

def list_free_models(provider=None, root=None) -> dict:
    """Full roster, or one provider's id list. Structured errors, no raises."""
    r = Path(root).resolve() if root is not None else REPO
    base: dict = {"tool": "list_free_models", "state_root": str(r)}

    if provider is not None and (
            not isinstance(provider, str) or provider not in PROVIDERS):
        # Arg validation outranks state problems: the caller asked for a
        # specific thing and misspelled it — tell them the valid names.
        return _error(
            base, f"unknown provider {provider!r}; "
                  f"valid providers: {', '.join(PROVIDERS)}",
            valid_providers=list(PROVIDERS))
    try:
        roster = _load_roster_checked(r, base)
    except _StateError as exc:
        return exc.payload

    if provider is not None:
        ids = _clean_ids(roster["providers"].get(provider, []))
        return {
            **base,
            "ok": True,
            "provider": provider,
            "model_ids": ids,
            "count": len(ids),
            "tick_epoch": _epoch_or_none(roster),
        }

    # CANONICAL GATEWAY-KEY SEMANTIC (shared with watchdog_status's
    # provider_counts): report exactly the five known gateways, always.
    # Unknown junk roster keys are ignored; a gateway missing from this tick
    # degrades to an empty list. n_gateways is therefore a constant 5 — not
    # a count of raw roster keys on partial state.
    raw = roster["providers"]
    cleaned = {gw: _clean_ids(raw.get(gw, [])) for gw in PROVIDERS}
    union = {mid for ids in cleaned.values() for mid in ids}
    return {
        **base,
        "ok": True,
        "providers": cleaned,
        "counts": {gw: len(ids) for gw, ids in cleaned.items()},
        "total_ids": len(union),
        "n_gateways": len(PROVIDERS),
        "tick_epoch": _epoch_or_none(roster),
    }


# ---------- tool: get_model ----------

ABSENCE_CAVEAT = (
    "Cross-gateway ABSENCE is unreliable: gateways rename inconsistently, "
    "so a missing exact id does NOT prove the model is unavailable there."
)


def get_model(model_id, root=None) -> dict:
    """Which gateways track this exact id right now — exact only, caveated.

    Additive change: also returns ``endpoints`` — one entry per (gateway,
    raw id) in the roster whose free-marker-stripped name equals the
    stripped query. This makes ``get_model("foo")`` and
    ``get_model("foo:free")`` return the same wiring list (marker-insensitive
    lookup). ``exact_matches`` stays STRICTLY exact — the honest raw answer,
    never grouped.
    """
    r = Path(root).resolve() if root is not None else REPO
    base: dict = {"tool": "get_model", "state_root": str(r)}
    if not isinstance(model_id, str) or not model_id.strip():
        return _error(base, "model_id must be a non-empty string")
    try:
        roster = _load_roster_checked(r, base)
    except _StateError as exc:
        return exc.payload

    providers_map = roster["providers"]
    exact = {gw: model_id in providers_map.get(gw, []) for gw in PROVIDERS}

    # Marker-insensitive wiring lookup: strip the query, then collect every
    # (gateway, raw id) in the roster whose stripped name matches. Order:
    # PROVIDERS display order, then raw id by _natural_key.
    needle = strip_free_marker(model_id)
    endpoints = []
    for gw in PROVIDERS:
        for raw_id in _clean_ids(providers_map.get(gw, [])):
            if strip_free_marker(raw_id) == needle:
                wiring = GATEWAY_WIRING.get(gw, {})
                endpoints.append({
                    "gateway": gw,
                    "model_id": raw_id,
                    "chat_completions_url": wiring.get("chat_completions_url"),
                    "auth": wiring.get("auth"),
                    "api_type": wiring.get("api_type"),
                })
    # Deduplicate while preserving order (a gateway carries a raw id once),
    # then sort by (PROVIDERS index, _natural_key(model_id)).
    seen = set()
    unique = []
    for ep in endpoints:
        key = (ep["gateway"], ep["model_id"])
        if key not in seen:
            seen.add(key)
            unique.append(ep)
    gw_index = {gw: i for i, gw in enumerate(PROVIDERS)}
    unique.sort(key=lambda ep: (gw_index[ep["gateway"]], _natural_key(ep["model_id"])))

    return {
        **base,
        "ok": True,
        "query": model_id,
        "exact_matches": exact,
        "exact_gateways": [gw for gw in PROVIDERS if exact[gw]],
        "endpoints": unique,
        "caveats": [ABSENCE_CAVEAT],
    }


# ---------- tool: list_endpoints ----------

def list_endpoints(provider=None, root=None) -> dict:
    """All gateway wiring entries across the roster, or one gateway's.

    Returns every (gateway, raw id) pair in the roster with the gateway's
    wiring (base URL, auth, api_type). Optional provider=<name> filters to a
    single gateway. Structured errors, no raises into the MCP layer.
    """
    r = Path(root).resolve() if root is not None else REPO
    base: dict = {"tool": "list_endpoints", "state_root": str(r)}

    if provider is not None and (
            not isinstance(provider, str) or provider not in PROVIDERS):
        return _error(
            base, f"unknown provider {provider!r}; "
                  f"valid providers: {', '.join(PROVIDERS)}",
            valid_providers=list(PROVIDERS))
    try:
        roster = _load_roster_checked(r, base)
    except _StateError as exc:
        return exc.payload

    raw = roster["providers"]
    gateways = {}
    total_endpoints = 0
    all_stripped = set()
    for gw in PROVIDERS:
        if provider is not None and gw != provider:
            continue
        ids = _clean_ids(raw.get(gw, []))
        wiring = GATEWAY_WIRING.get(gw, {})
        gateways[gw] = {
            "chat_completions_url": wiring.get("chat_completions_url"),
            "auth": wiring.get("auth"),
            "api_type": wiring.get("api_type"),
            "model_ids": ids,
        }
        # Include optional notes field if present (cline, command_code)
        if "notes" in wiring:
            gateways[gw]["notes"] = wiring["notes"]
        total_endpoints += len(ids)
        all_stripped.update(strip_free_marker(mid) for mid in ids)

    return {
        **base,
        "ok": True,
        "gateways": gateways,
        "counts": {
            "endpoints": total_endpoints,
            "models": len(all_stripped),
            "gateways": len(PROVIDERS),
        },
        "provider": provider,
        "tick_epoch": _epoch_or_none(roster),
    }


# ---------- tool: watchdog_status ----------

def watchdog_status(now=None, root=None) -> dict:
    """Tick age vs cadence, stale providers, counts, alert depth, site pub."""
    now_v = time.time() if now is None else now
    r = Path(root).resolve() if root is not None else REPO
    base: dict = {"tool": "watchdog_status", "state_root": str(r)}

    try:
        roster = _load_roster_checked(r, base)
    except _StateError as exc:
        # State unreadable: surface the failure payload plus UNKNOWN for the
        # optional fields — ok:False must never masquerade as healthy zeros
        # (a fabricated 0 alert depth would read as "queue drained").
        payload = exc.payload
        payload.setdefault("cadence_s", CADENCE_S)
        payload["pending_alerts"] = None
        payload["site_published"] = False
        return payload

    tick = _epoch_or_none(roster)
    last_tick_age_s = None
    tick_fresh = None
    if tick is not None:
        last_tick_age_s = int(now_v - tick)
        tick_fresh = 0 <= last_tick_age_s <= CADENCE_S

    provs = roster["providers"]
    pending_path = r / "state" / "pending_alerts.json"
    site = r / "site" / "index.html"
    try:
        mtime = site.stat().st_mtime
    except OSError:
        site_published = False
        site_publish_age_s = None
    else:
        site_published = True
        site_publish_age_s = max(0, int(now_v - mtime))

    return {
        **base,
        "ok": True,
        "cadence_s": CADENCE_S,
        "last_tick_age_s": last_tick_age_s,
        "tick_fresh": tick_fresh,
        "stale_providers": _clean_names(roster.get("stale_providers", [])),
        "provider_counts": {
            gw: len(_clean_ids(provs.get(gw, []))) for gw in PROVIDERS
        },
        "pending_alerts": len(state.load_pending(pending_path)),
        "site_published": site_published,
        "site_publish_age_s": site_publish_age_s,
    }


# ---------- MCP transport wiring ----------

TOOL_DESCRIPTIONS = {
    "list_free_models":
        "List free-tier model ids across the watched gateways "
        "(nous, zen, kilo, cline, openrouter, command_code). Pass provider=<name> for one "
        "gateway's id list; omit it for the full roster with per-gateway "
        "counts. Read-only snapshot of the latest watchdog tick.",
    "get_model":
        "Cross-gateway PRESENCE lookup: which of the six gateways track "
        "this exact model id right now. Returns exact_matches per gateway "
        "(exact ids only — gateways rename inconsistently, so cross-gateway "
        "absence is unreliable; see the caveats attached to every result). "
        "Also returns 'endpoints' — one wiring entry (base URL, auth, "
        "api_type) per (gateway, raw id) in the roster whose stripped name "
        "matches the query, so get_model('foo') and get_model('foo:free') "
        "return the same wiring list.",
    "watchdog_status":
        "Watchdog health: last tick age vs the 1h cadence, stale/failing "
        "providers, per-gateway model counts, pending-alert queue depth, "
        "and the age of the last static-site publish.",
    "list_endpoints":
        "All gateway wiring entries across the roster: for each gateway, "
        "its base URL, auth shape, api_type, and the raw model ids tracked. "
        "Pass provider=<name> for one gateway; omit for all. Read-only "
        "snapshot of the latest watchdog tick.",
}


def build_server(root=None):
    """MCPServer with the three tools closed over `root`.

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
            "auth, api_type) for actually calling a model."),
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
