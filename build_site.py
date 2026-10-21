#!/usr/bin/env python3
"""build_site.py — self-contained static dashboard for the Free Inference Watchdog.

Reads state/roster.json and renders site/index.html: a single dark page whose
body is a PRESENCE MATRIX — one row per UNIQUE model id across the gateway
union, one column per gateway, a green (nord14) dot where tracked, a `#`
column counting the gateways each model reaches, rows sorted alphabetically
by stripped name, and a footer row of per-gateway totals. Roster keys
OUTSIDE DISPLAY_ORDER are unknown gateways: they get NO column (their ids
may still join the union rows). The full roster is INLINED
as JSON in <script type="application/json" id="roster-data"> so the page is
fully self-contained (works from file://, no external fetches).

Design contract (Dthen-approved mockup v3 = watchdog-dashboard-MOCKUP.html):
  - Display order is derived from config_loader.PROVIDERS (sorted by the
    `display` field in each provider's JSON config). This does NOT match
    the providers.PROVIDERS registry order, and must NOT be alphabetized.
  - Header copy is minimal: title + "updated hourly" + three chips
    ("N unique models", "M endpoints", "G gateways"). NO
    snapshot/alert/stale wording anywhere.
  - Colors are Nord strictly: nord0 bg #2e3440, nord1 elevated/thead/chips
    #3b4252, nord2 hover #434c5e, nord6 text #eceff4, nord4 subtle
    #d8dee9 @ opacity .72, nord8 accent (counts) #88c0d0, nord9 column
    heads #81a1c1, nord14 dots #a3be8c.

Determinism: same roster.json -> byte-identical HTML. All iteration goes
through sorted()/DISPLAY_ORDER.

Logo handling (stdlib-only at build time): assets/logo.png was downscaled
to 128x128 with PIL during task prep (~31KB, within the <=128px / ~31KB
budget), so the builder embeds it as-is as a base64 data-URI. The >40KB
fallback (stdlib PNG chunk parse + zlib + resample) was not needed. The
builder itself only reads bytes and refuses logos over MAX_LOGO_BYTES.

Usage: python3 build_site.py [--root DIR]   (default root = repo dir)
Exit codes: 0 ok; 2 missing/corrupt roster, a provider value that is not a
list of model-id strings, or a missing/oversized/unreadable logo — and
site/index.html is NEVER written on failure (never publish garbage).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
from html import escape
from pathlib import Path

# Probe-verified gateway wiring (single source of truth): see config_loader.py
# GATEWAY_WIRING. We IMPORT rather than re-hardcode so a probed-URL fix
# in config_loader.py reaches the dashboard on the next tick with no second
# site to keep in sync. config_loader is also held as a MODULE reference:
# build_provider_header_meta() re-reads providers/*.json through it per
# render, and degrade tests monkeypatch config_loader.load_configs.
import config_loader  # noqa: E402
from config_loader import GATEWAY_WIRING, PROVIDERS  # noqa: E402

# Dthen's quality ranking, derived from config_loader.PROVIDERS (sorted by
# `display` field in providers/*.json). Single source of truth; a display
# reorder in config reaches the dashboard on the next tick with no second
# site to keep in sync.
DISPLAY_ORDER = list(PROVIDERS.keys())

REPO = Path(__file__).resolve().parent

ROSTER_REL = Path("state/roster.json")
SITE_REL = Path("site/index.html")
LOGO_REL = Path("assets/logo.png")
MAX_LOGO_BYTES = 40 * 1024  # refuse to embed a bloated logo

# The free-marker stripper and the portable natural raw-ID key now live in the
# pure, side-effect-free watchdog_catalog.query module (stdlib-only, no
# config/state/SDK import). These names re-export those exact function objects
# during the compatibility transition, so existing importers, the legacy
# adapter and the public renderer never drift from a second copy.
from watchdog_catalog.catalog import canonical_bytes  # noqa: E402
from watchdog_catalog.query import natural_key, strip_free_marker  # noqa: E402


def load_roster(root):
    """Return parsed roster dict; exit 2 (no site write) if missing/corrupt."""
    path = root / ROSTER_REL
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"build_site: no roster at {path}", file=sys.stderr)
        raise SystemExit(2)
    except OSError as exc:
        print(f"build_site: cannot read {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    try:
        roster = json.loads(raw)
    except ValueError as exc:
        print(f"build_site: corrupt roster {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(roster, dict) or not isinstance(roster.get("providers"), dict):
        print(f"build_site: roster lacks a providers map: {path}", file=sys.stderr)
        raise SystemExit(2)
    # Hostile-value gate (C1): every provider value must be a list of str.
    # Without this, a bare string iterated char-by-char into junk matrix rows
    # (plausible FALSE dashboard at rc=0), dict/None/int values crashed or
    # rendered false presence dots.
    for gw, models in sorted(roster["providers"].items()):
        if not (
            isinstance(models, list)
            and all(isinstance(mid, str) for mid in models)
        ):
            print(
                f"build_site: provider {gw!r} value must be a list of "
                f"model-id strings, got {type(models).__name__}: {path}",
                file=sys.stderr,
            )
            raise SystemExit(2)
    return roster


def load_logo_b64(root):
    """Base64 of the logo PNG; exit 2 if missing or over MAX_LOGO_BYTES.

    Resolution precedence: <root>/assets/logo.png FIRST — --root owns the
    content being published; the builder's own repo assets dir is only a
    FALLBACK for when the target root has no logo yet (e.g. building into a
    bare deploy dir).
    """
    path = root / LOGO_REL
    if not path.exists():
        fallback = REPO / LOGO_REL
        if fallback.exists():
            path = fallback
    try:
        blob = path.read_bytes()
    except OSError:
        print(f"build_site: logo missing at {path}", file=sys.stderr)
        raise SystemExit(2)
    if len(blob) > MAX_LOGO_BYTES:
        print(
            f"build_site: logo {path} is {len(blob)}B > {MAX_LOGO_BYTES}B cap "
            "(downscale before embedding)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return base64.b64encode(blob).decode("ascii")


def _natural_key(s):
    """Thin private delegate to the accepted portable natural raw-ID key.

    Kept as a name (rather than inlining the import at every call site) so the
    module's private call surface is unchanged; the ordering RULE itself lives
    only in ``watchdog_catalog.query.natural_key`` -- ASCII digit runs by run
    length then digits, every other piece case-sensitively by code point, so a
    very long digit run, mixed case, leading zeroes and non-ASCII digit
    lookalikes can never raise the old heterogeneous int/str comparison error.
    """
    return natural_key(s)


def _script_json(value):
    """Encode one JSON value as the body of an inert HTML script element.

    Encoding goes through the SAME accepted
    :func:`watchdog_catalog.catalog.canonical_bytes` used for the whole public
    snapshot and its export text (UTF-8, code-point sorted keys, compact
    separators, ``ensure_ascii=True``, finite values only) -- never a second
    set of JSON encoding or hash rules. That encoder already escapes U+2028 /
    U+2029, control characters and every non-ASCII character, so the only
    HTML-relevant literal left is ``<``; the small pure pass below turns each
    of those into its JSON Unicode escape (and, defensively, ``>`` and ``&``),
    which is still JSON and still decodes to the identical value.

    Consequence: the body contains no ``<!--``, no ``<script`` and no
    ``</script`` in any letter case, so it cannot open a comment, open a
    script element or prematurely close the element it sits in -- the three
    sequences WHATWG's script-content restrictions key on. Decoding preserves
    every value, array order and the integer/float/string/null/bool
    distinction. HTML entity encoding and JavaScript-only ``\\x`` escapes are
    deliberately NOT used: neither is JSON, and neither survives script-data
    parsing unchanged.
    """
    text = canonical_bytes(value).decode("utf-8")
    return (text
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _islands_script(snapshot, tool_contract):
    """The data-script slot: exactly two inline inert JSON islands, in order."""
    return (
        '<script type="application/json" id="catalog-data">'
        + _script_json(snapshot)
        + '</script>'
        '<script type="application/json" id="tool-contract">'
        + _script_json(tool_contract)
        + '</script>')


def build_provider_header_meta():
    """gateway key -> {'signup_url': str, 'limits_note': str} (fields may be
    absent — both are OPTIONAL operator-maintained facts in providers/*.json,
    same file as everything else about a gateway).

    Called ONCE per render. DEGRADE-SAFE by contract: if config loading
    raises for ANY reason, return an empty map so headers render plain and
    the site build still exits 0 (a site build failure must never page —
    established wrapper rule). The scheme/XSS guard itself lives at render
    time (only https:// signup_urls are ever linked).
    """
    try:
        meta = {}
        for cfg in config_loader.load_configs():
            key = config_loader._provider_key(cfg)
            entry = {}
            for field in ("signup_url", "limits_note"):
                value = cfg.get(field)
                if isinstance(value, str) and value:
                    entry[field] = value
            if entry:
                meta[key] = entry
        return meta
    except Exception as exc:
        # Degrade, never die: broken/absent configs cost the dashboard its
        # links, nothing more. stderr goes to state/site_build.log (log,
        # never page).
        print(f"build_site: provider header meta degraded: {exc}", file=sys.stderr)
        return {}


def build_groups(providers, provider_models, *, provider_order=None):
    """Group raw ids by their display `name` field, deterministically.

    Returns (group_names, groups, endpoints) where:
      - group_names: alphabetically sorted list of group names (the `name`
        field from provider_models, or raw id as fallback), one per row
      - groups: dict[group_name] -> {
            "gateways": set[str],                  # every gw carrying ANY variant
            "variants": list[(gateway, raw_id)],   # per-(gw,raw) wiring rows, in
                                                   # provider order then natural key
          }
      - endpoints: number of distinct (gateway, raw_id) wiring pairs, i.e.
        sum(len(slot["variants"]) for slot in groups.values()). An id
        callable at two gateways is TWO endpoints — that is what a caller
        can actually hit — and this equals the sum of the <tfoot>
        per-gateway totals. The count of distinct raw ids across all
        providers is a third quantity nobody renders.

    Grouping key: the `name` field from each gateway's provider_models entry.
    Fallback: if a model id has no entry in provider_models (e.g. gateway
    fetch failed), use the raw id as the group name (don't crash).

    provider_order: optional keyword-only provider precedence. None (the legacy
    two-positional-argument shape) means the module DISPLAY_ORDER; an explicit
    list -- including an EMPTY one -- is used verbatim, never truthiness-fallen
    back to the global. Every internal provider loop and the variant sort use
    this order, so a caller can render from its own explicit provider view.
    """
    order = DISPLAY_ORDER if provider_order is None else list(provider_order)
    raw_ids = sorted({mid for models in providers.values() for mid in models})
    groups: dict = {}
    # Build the per-(gw, raw_id) variant map deterministically: one slot
    # per distinct raw id, then sort variants by DISPLAY_ORDER then by
    # the natural key of the raw id. We keep this separate from
    # `groups[].variants` so the byte-order in the HTML is stable.
    for mid in raw_ids:
        # Find the group name by looking up the `name` field in provider_models
        group_name = mid  # fallback: raw id
        for gw in order:
            if mid in providers.get(gw, []):
                model_info = provider_models.get(gw, {}).get(mid, {})
                name = model_info.get("name")
                if name:
                    group_name = name
                    break  # first gateway with a name wins (stable)
        slot = groups.setdefault(group_name, {"gateways": set(), "variants": []})
        for gw in order:
            if mid in providers.get(gw, []):
                slot["gateways"].add(gw)
                slot["variants"].append((gw, mid))
    for slot in groups.values():
        slot["variants"].sort(
            key=lambda pair: (order.index(pair[0]),
                              _natural_key(pair[1]))
        )
    group_names = sorted(groups.keys())
    endpoints = sum(len(slot["variants"]) for slot in groups.values())
    return group_names, groups, endpoints


def build_counts(providers):
    """Kept for backward-compat with the test suite; returns per-raw-id
    availability across DISPLAY_ORDER. The dashboard now renders by
    stripped group, not by raw id — see build_groups / render_page."""
    ids = sorted({mid for models in providers.values() for mid in models})
    counts = {}
    for mid in ids:
        n = 0
        for gw in DISPLAY_ORDER:
            if mid in providers.get(gw, []):
                n += 1
        counts[mid] = n
    return ids, counts


def _render_matrix(*, gateways, models_by_gateway, provider_order, wiring,
                   logo_b64, header_meta, data_script):
    """Render the ONE shared matrix HTML document from explicit resolved views.

    This private engine owns the single copy of the template, the alphabetical
    display-name grouping, the per-(gateway, raw id) expansion rows, the counts
    and the Nord styling. It performs NO roster/registry read, NO logo or header
    config load and NO I/O: every input is handed in by a thin adapter.

    gateways: explicit provider-ID view -- gateway key -> tracked raw ids. Only
        keys present here can get a column; an EMPTY list is already filtered
        out by the adapter (zero-model columns stay hidden).
    models_by_gateway: per-provider model view -- gateway key -> raw id ->
        mapping read for `name`, `context_length` and
        `architecture.input_modalities`. A missing pair stays unknown.
    provider_order: authoritative provider precedence for columns, group-name
        selection and variant ordering. An explicit EMPTY list is honoured.
    wiring: gateway key -> {'chat_completions_url', 'api_type'}.
    header_meta: explicit trusted gateway -> {'signup_url', 'limits_note'} map;
        never derived here.
    data_script: the complete data-script slot markup. The legacy adapter
        passes its roster-data island; the staged public route passes "".

    Rows are GROUPED by stripped name: a model that ships as
    `vendor/x:free` on gateway A and `vendor/x-free` on gateway B renders
    as ONE row with dots on both A and B; the `#` column counts distinct
    gateways reached (so a gateway carrying both variants counts once).
    The <tfoot> per-gateway totals stay RAW per-gateway counts — that is
    the honest "ids tracked per gateway" number and must not change
    meaning just because the matrix collapsed.
    """
    # Active gateways in the caller's provider order: only providers with models.
    active_gateways = [gw for gw in provider_order if gw in gateways]
    group_names, groups, endpoints = build_groups(
        gateways, models_by_gateway, provider_order=provider_order)

    def _wire_cell(gw, raw_id):
        """Return the inner-HTML for one (gateway, raw_id) wiring row.

        Reads the caller's wiring view: the URL and api type come from THAT
        provider entry, so the reader gets the exact endpoint.
        """
        w = wiring[gw]
        # Both wiring sources (the legacy config_loader map and a validated
        # public provider entry) always carry chat_completions_url and
        # api_type for an ACTIVE gateway -- the subscripts are safe.
        url_text = w["chat_completions_url"]
        # All visible strings: escaped, monospaced, copy-pasteable.
        return (
            f'<span class="wire-gw">{escape(gw)}</span>'
            f'<span class="wire-id">{escape(raw_id)}</span>'
            f'<span class="wire-url">{escape(url_text)}</span>'
            f'<span class="wire-api">{escape(w["api_type"])}</span>'
        )

    def row_html(name, group_index):
        group = groups[name]
        present_gws = group["gateways"]
        cells = [
            '<td class="yes">&#9679;</td>' if gw in present_gws else '<td class="no"></td>'
            for gw in active_gateways
        ]
        # Build hover title from ALL variants' metadata.
        # A group may span multiple gateways; collect context_length and
        # input_modalities from each provider, then show per-provider values
        # when they differ.
        ctx_by_val = {}  # {value: set of providers}
        mod_by_val = {}  # {tuple_of_modalities: set of providers}
        for gw, mid in group["variants"]:
            model_info = models_by_gateway.get(gw, {}).get(mid, {})
            cl = model_info.get("context_length")
            arch = model_info.get("architecture", {})
            im = arch.get("input_modalities")
            if cl is not None:
                ctx_by_val.setdefault(cl, set()).add(gw)
            if im:
                mod_by_val.setdefault(tuple(im), set()).add(gw)
        title_parts = []
        if ctx_by_val:
            if len(ctx_by_val) == 1:
                title_parts.append(f"Context length: {next(iter(ctx_by_val))}")
            else:
                items = sorted(
                    ((val, sorted(gws)) for val, gws in ctx_by_val.items()),
                    key=lambda x: x[0],
                )
                prov_parts = [f"{val} ({', '.join(gws)})" for val, gws in items]
                title_parts.append(f"Context length: {', '.join(prov_parts)}")
        if mod_by_val:
            if len(mod_by_val) == 1:
                title_parts.append(f"Input modalities: {', '.join(next(iter(mod_by_val)))}")
            else:
                items = sorted(
                    ((mods, sorted(gws)) for mods, gws in mod_by_val.items()),
                    key=lambda x: x[0],
                )
                prov_parts = [f"{', '.join(mods)} ({', '.join(gws)})" for mods, gws in items]
                title_parts.append(f"Input modalities: {', '.join(prov_parts)}")
        title_attr = f' title="{"&#10;".join(escape(part) for part in title_parts)}"' if title_parts else ""
        # One <input type="checkbox"> per group, named with a stable
        # group_index so two groups can never share an id. The label
        # wraps both the checkbox and the stripped name, so clicking
        # the name flips the checkbox (no onclick, no JS).
        # `tr:has(input:checked) ~ tr.expand` in the stylesheet reveals
        # the per-variant wiring rows when the user expands the name.
        # Title goes on a span wrapping just the name text — the visible
        # hover target. Browser tooltips on <label> can be inconsistent;
        # a <span title="…"> wrapping the text is reliable across browsers.
        cb_id = f"row-{group_index}"
        name_row = (
            f'<tr class="name-row">'
            f'<th>'
            f'<label for="{cb_id}">'
            f'<input type="checkbox" id="{cb_id}" class="row-expand" aria-label="toggle wiring for {escape(name)}">'
            f'<span class="caret" aria-hidden="true">&#9656;</span> '
            f'<span{title_attr}>{escape(name)}</span>'
            f'</label>'
            f'</th>'
            f'<td class="n">{len(present_gws)}</td>'
            + "".join(cells)
            + "</tr>"
        )
        # Per-(gateway, raw_id) expansion rows. One row per variant; a
        # single-variant group still gets its one wiring row so nothing
        # is hidden. colspan = 2 + len(active_gateways) so the row spans
        # the full table width on expand.
        colspan = 2 + len(active_gateways)
        expand_rows = "".join(
            f'<tr class="expand">'
            f'<td colspan="{colspan}">'
            f'<div class="wire">{_wire_cell(gw, mid)}</div>'
            f'</td>'
            f'</tr>'
            for (gw, mid) in group["variants"]
        )
        return name_row + expand_rows

    def _head_cell(gw):
        """Gateway column head: '<gateway>' always visible; wrapped in a
        signup anchor when providers/*.json carries an https:// signup_url,
        with the dated limits_note as the native title tooltip. Missing /
        non-https signup_url => plain <th> (scheme guard — XSS via a config
        'javascript:' value must never become an href). Missing limits_note
        => anchor without title attr. Per-gateway counts live in the tfoot
        only (operator: neater at the bottom). Static HTML only: no JS
        tooltips."""
        text = escape(gw)
        entry = header_meta.get(gw) or {}
        url = entry.get("signup_url", "")
        if not url.lower().startswith("https://"):
            return f"<th>{text}</th>"
        note = entry.get("limits_note")
        title = f' title="{escape(note)}"' if note else ""
        return (f'<th><a href="{escape(url)}" target="_blank"'
                f' rel="noopener"{title}>{text}</a></th>')

    head_cells = "<th>model</th><th>#</th>" + "".join(
        _head_cell(gw) for gw in active_gateways
    )
    # <tfoot> stays RAW per-gateway counts — that is the honest "ids
    # tracked per gateway" number and must not change meaning because
    # the matrix collapsed to one row per group above.
    foot_cells = "".join(
        '<td class="n">%d</td>' % len(gateways.get(gw, [])) for gw in active_gateways
    )
    chips = (
        f'<span class="chip"><b>{len(group_names)}</b> unique models</span>'
        f'<span class="chip"><b>{endpoints}</b> endpoints</span>'
        f'<span class="chip"><b>{len(active_gateways)}</b> gateways</span>'
    )
    meta = "updated hourly"
    # The data-script slot is supplied fully formed by the adapter: the legacy
    # one carries its roster-data island, the staged public route carries "".
    body_rows = "".join(
        f"<tbody>{row_html(name, i)}</tbody>"
        for i, name in enumerate(group_names)
    )

    css = """  :root { color-scheme: dark;
    --nord0:#2e3440; --nord1:#3b4252; --nord2:#434c5e; --nord3:#4c566a;
    --nord4:#d8dee9; --nord5:#e5e9f0; --nord6:#eceff4;
    --nord7:#8fbcbb; --nord8:#88c0d0; --nord9:#81a1c1; --nord10:#5e81ac;
    --nord11:#bf616a; --nord12:#d08770; --nord13:#ebcb8b; --nord14:#a3be8c; --nord15:#b48ead; }"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Free Inference Watchdog</title>
<style>
{css}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--nord0); color:var(--nord6); font:15px/1.45 system-ui,-apple-system,sans-serif; padding:28px; }}
  .wrap {{ max-width:1150px; margin:0 auto; }}
  header.top {{ display:flex; align-items:center; gap:16px; margin-bottom:18px; }}
  .logo {{ width:60px; height:60px; border-radius:14px; flex:none; }}
  h1 {{ font-size:23px; letter-spacing:-.02em; color:var(--nord6); }}
  .meta {{ color:var(--nord4); opacity:.72; font-size:12.5px; margin-top:3px; }}
  .chips {{ margin-top:9px; display:flex; gap:8px; flex-wrap:wrap; }}
  .chip {{ background:var(--nord1); border:1px solid var(--nord2); border-radius:999px; padding:2px 11px; font-size:12px; color:var(--nord4); }}
  .chip b {{ color:var(--nord8); font-weight:600; }}
  table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
  thead th {{ position:sticky; top:0; background:var(--nord1); color:var(--nord9); font-weight:600; text-transform:lowercase;
              padding:7px 10px; border-bottom:1px solid var(--nord2); text-align:center; }}
  thead th:first-child {{ text-align:left; width:auto; }}
  tbody th {{ font:12px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--nord6); text-align:left;
              padding:3.5px 10px; font-weight:400; white-space:nowrap; }}
  tbody tr:nth-child(odd) {{ background:rgba(67,76,94,.22); }}      /* nord2 wash */
  tbody tr:hover {{ background:var(--nord2); }}
  td {{ text-align:center; padding:3.5px 10px; }}
  td.yes {{ color:var(--nord14); font-size:11px; }}                 /* aurora green presence dots */
  td.n {{ color:var(--nord8); font-size:11px; }}
  tfoot td, tfoot th {{ padding:6px 10px; border-top:1px solid var(--nord2); color:var(--nord4);
                        font-weight:600; text-transform:lowercase; text-align:center; font-size:12px; }}
  tfoot th {{ text-align:left; }}
  footer.note {{ margin-top:14px; color:var(--nord4); opacity:.55; font-size:11.5px; }}
  footer.note a {{ color:var(--nord8); text-decoration:none; }}
  .footer-links {{ display:flex; gap:20px; justify-content:center; margin-bottom:12px; }}
  /* ---- expand rows: pure CSS, no JS ---- */
  /* Hide the native checkbox; the <label> is the visible click target. */
  tbody td > .row-expand, tbody th .row-expand {{ position:absolute; opacity:0; pointer-events:none; width:0; height:0; }}
  /* The label wraps the caret + name and is the focus/keyboard target. */
  tbody th label {{ cursor:pointer; display:inline-flex; align-items:center; gap:4px; }}
  /* Caret rotates on expand via the :checked state. */
  tbody th .caret {{ display:inline-block; transition:transform .12s linear; color:var(--nord4); font-size:9px; width:9px; }}
  /* Expansion rows hidden by default; tr:has(input:checked) ~ tr.expand
     reveals subsequent .expand rows in the same parent (the body) when
     the row-expand checkbox is checked. */
  tbody tr.expand {{ display:none; }}
  tbody tr:has(input.row-expand:checked) ~ tr.expand {{ display:table-row; }}
  /* Caret visual feedback on expand. */
  tbody tr:has(input.row-expand:checked) .caret {{ transform:rotate(90deg); color:var(--nord8); }}
  /* Wiring panel: monospace, copy-pasteable, dark-elevated. */
  tbody tr.expand > td {{ background:var(--nord1); border-top:1px solid var(--nord2); padding:8px 14px; text-align:left; }}
  /* Text columns use minmax(0,1fr): a bare 1fr track can't shrink below its
     content, so long chat-completions URLs overflowed the cell (operator
     report 2026-09-13). Every span also breaks long words. */
  tbody tr.expand .wire {{ display:grid; grid-template-columns: 92px minmax(0,1fr) minmax(0,1fr) 128px; gap:6px 14px; font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--nord4); }}
  tbody tr.expand .wire-gw {{ color:var(--nord9); font-weight:600; overflow-wrap:anywhere; }}
  tbody tr.expand .wire-id {{ color:var(--nord6); overflow-wrap:anywhere; }}
  tbody tr.expand .wire-url {{ color:var(--nord8); word-break:break-all; }}
  tbody tr.expand .wire-api {{ color:var(--nord14); overflow-wrap:anywhere; }}
</style></head>
<body><div class="wrap">
<header class="top">
  <img class="logo" alt="watchdog radar-dog logo" src="data:image/png;base64,{logo_b64}">
  <div>
    <h1>Free Inference Watchdog</h1>
    <div class="meta">{meta}</div>
    <div class="chips">{chips}</div>
    {data_script}
  </div>
</header>
<table>
<thead><tr>{head_cells}</tr></thead>
<tbody>
{body_rows}</tbody>
<tfoot><tr><th>tracked ids per gateway</th><td></td>{foot_cells}</tr></tfoot>
</table>
<footer class="note"><div class="footer-links"><a href="https://github.com/Dthen/free-inference-watchdog" target="_blank" rel="noopener">GitHub</a><a href="https://ko-fi.com/dthen" target="_blank" rel="noopener">Ko-fi</a></div></footer>
</div></body></html>"""


def render_page(roster, logo_b64, header_meta=None):
    """Render the full HTML document as one string (byte-deterministic).

    Legacy adapter: keeps its existing signature, roster-data island bytes and
    default header derivation EXACTLY. Providers with zero free models are
    hidden from the matrix — they clutter the view with an empty column and
    inflate the gateway chip.

    header_meta: gateway key -> {'signup_url', 'limits_note'} (see
    build_provider_header_meta). None (default) = derive from the repo's
    providers/*.json once per render; config load failure degrades to plain
    headers, never a crash. Wiring and display order are this module's
    legacy GATEWAY_WIRING / DISPLAY_ORDER views.
    """
    providers = roster["providers"]
    # Hide providers with zero free models — they clutter the matrix with
    # an empty column and inflate the "gateways" chip for no reason.
    providers = {gw: ids for gw, ids in providers.items() if ids}
    if header_meta is None:
        header_meta = build_provider_header_meta()
    # sort_keys keeps the embedded JSON byte-stable across builds; the
    # JSON island stays the RAW roster so MCP and other consumers that
    # read raw ids are unaffected by the matrix grouping.
    roster_json = json.dumps(roster, sort_keys=True).replace("</", "<\\/")
    return _render_matrix(
        gateways=providers,
        models_by_gateway=roster.get("provider_models", {}),
        provider_order=DISPLAY_ORDER,
        wiring=GATEWAY_WIRING,
        logo_b64=logo_b64,
        header_meta=header_meta,
        data_script=(
            f'<script type="application/json" id="roster-data">'
            f"{roster_json}</script>"),
    )


def render_catalog_page(snapshot, logo_b64, header_meta=None, *, tool_contract):
    """Render the matrix HTML from an already-VALIDATED public snapshot.

    ``snapshot`` is a CALLER PRECONDITION: validation (and the eventual
    build-before-render/write sequencing) belongs to the publishing unit, so
    this function performs none itself. In-memory only — no roster/registry
    read or capture, no logo/header/config load, no clock and no I/O. The
    module's import-time producer configuration stays outside this function's
    purity claim.

    ``tool_contract`` is a REQUIRED keyword-only argument: the already-decoded
    bare shared ``watchdog_catalog/tools.json`` array, loaded by the caller.
    This explicit input keeps call-time rendering I/O-free instead of
    introducing an implicit file read or an import-time mutable tool cache. A
    valid snapshot and a reviewed JSON-native tool contract are caller
    preconditions, as the valid snapshot was in 4c.

    Every rendered fact is derived from the snapshot, never from a module
    global or another provider's data:

    * column order comes from ``snapshot["provider_order"]`` (an explicit empty
      list is honoured, never replaced by DISPLAY_ORDER);
    * tracked ids/models come from each public provider entry's ``model_ids`` /
      ``models``;
    * the wiring URL and api type come from THAT provider entry;
    * tooltip context/modalities come from the selected pair's projected model
      (unknown/null stays silent; a known 0 or known [] renders safely).

    ``header_meta`` is an explicit trusted gateway -> {'signup_url',
    'limits_note'} map. None means an EMPTY map on this route — deliberately
    NOT a config read, so default headers are plain. The HTTPS-only scheme
    guard and attribute escaping are shared template behaviour.

    The data-script slot carries exactly two inline inert JSON islands, in
    order: ``catalog-data`` (the COMPLETE public snapshot) then
    ``tool-contract`` (the supplied tool array), each encoded through the
    accepted canonical encoder plus the script-context escape — so the page
    stays self-contained with no executable script, no network fetch and no
    ``src`` attribute, and no raw private state.
    """
    provider_order = list(snapshot["provider_order"])
    providers = snapshot["providers"]
    # Explicit provider-ID / per-provider-model / wiring views. Zero-ID
    # providers are dropped here so they get no hidden-but-present column.
    gateways = {}
    models_by_gateway = {}
    wiring = {}
    for key in provider_order:
        entry = providers[key]
        model_ids = entry["model_ids"]
        if not model_ids:
            continue
        gateways[key] = list(model_ids)
        models_by_gateway[key] = entry["models"]
        wiring[key] = {
            "chat_completions_url": entry["chat_completions_url"],
            "api_type": entry["api_type"],
        }
    return _render_matrix(
        gateways=gateways,
        models_by_gateway=models_by_gateway,
        provider_order=provider_order,
        wiring=wiring,
        logo_b64=logo_b64,
        header_meta={} if header_meta is None else header_meta,
        data_script=_islands_script(snapshot, tool_contract),
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Render site/index.html from state/roster.json (stdlib only)."
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=REPO,
        help="project root containing state/ and receiving site/ (default: repo dir)",
    )
    args = ap.parse_args(argv)
    roster = load_roster(args.root)
    logo_b64 = load_logo_b64(args.root)
    html = render_page(roster, logo_b64)
    out = args.root / SITE_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    # Unique per process + thread, mirroring state.py's _atomic_write_json:
    # a shared 'index.html.tmp' let two concurrent builders race — one
    # builder's replace moved the other's temp away, so the loser died on
    # FileNotFoundError (reviewer measured 17/3 failures at 20 parallel).
    tmp = out.with_name(
        f"{out.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out)  # atomic publish: never a torn or partial write
    # Progress goes to STDERR: the tick wrapper delivers non-empty STDOUT to
    # Discord home, so a successful rebuild must stay stdout-silent.
    n_ids = len({m for models in roster["providers"].values() for m in models})
    print(f"build_site: wrote {out} ({n_ids} unique ids)", file=sys.stderr)


if __name__ == "__main__":
    main()