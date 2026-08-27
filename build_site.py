#!/usr/bin/env python3
"""build_site.py — self-contained static dashboard for the Free Inference Watchdog.

Reads state/roster.json and renders site/index.html: a single dark page whose
body is a PRESENCE MATRIX — one row per UNIQUE model id across the gateway
union, one column per gateway, a green (nord14) dot where tracked, a `#`
column with the per-gateway count, rows sorted by availability-count desc
then id, and a footer row of per-gateway totals. Roster keys OUTSIDE
DISPLAY_ORDER are unknown gateways: they get NO column (their ids may still
join the union rows). The full roster is INLINED
as JSON in <script type="application/json" id="roster-data"> so the page is
fully self-contained (works from file://, no external fetches).

Design contract (Dthen-approved mockup v3 = watchdog-dashboard-MOCKUP.html):
  - PROVIDER COLUMN ORDER is Dthen's quality ranking: nous, zen, kilo,
    cline, openrouter — openrouter LAST because its limits suck. This does
    NOT match the providers.PROVIDERS registry order (nous, openrouter,
    zen, kilo, cline), and must NOT be alphabetized, so it is hardcoded
    below as DISPLAY_ORDER.
  - Header copy is minimal: title + "last refreshed {ts} · rebuilt every
    1h" + two chips ("N unique models", "6 gateways"). NO snapshot/alert/
    stale wording anywhere.
  - Colors are Nord strictly: nord0 bg #2e3440, nord1 elevated/thead/chips
    #3b4252, nord2 hover #434c5e, nord6 text #eceff4, nord4 subtle
    #d8dee9 @ opacity .72, nord8 accent (counts) #88c0d0, nord9 column
    heads #81a1c1, nord14 dots #a3be8c.
  - ts derives from roster tick_epoch as LOCAL time "%Y-%m-%d %H:%M".

Determinism: same roster.json -> byte-identical HTML. All iteration goes
through sorted()/DISPLAY_ORDER; the only time in the output derives from
tick_epoch.

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
import re
import sys
import threading
from datetime import datetime
from html import escape
from pathlib import Path

# Probe-verified gateway wiring (single source of truth): see providers.py
# GATEWAY_WIRING. We IMPORT rather than re-hardcode so a probed-URL fix
# in providers.py reaches the dashboard on the next tick with no second
# site to keep in sync.
from providers import GATEWAY_WIRING  # noqa: E402

REPO = Path(__file__).resolve().parent

# Dthen's quality ranking, best first; openrouter LAST because its limits
# suck. Differs from providers.py registry order (nous, openrouter, zen,
# kilo, cline) by design: display order is curation, not implementation
# detail, so it is hardcoded rather than derived from the registry.
DISPLAY_ORDER = ["nous", "zen", "kilo", "cline", "openrouter", "command_code"]

ROSTER_REL = Path("state/roster.json")
SITE_REL = Path("site/index.html")
LOGO_REL = Path("assets/logo.png")
MAX_LOGO_BYTES = 40 * 1024  # refuse to embed a bloated logo

# Regexes for strip_free_marker: match "free" only at a segment boundary
# (preceded by a separator for suffix, followed by one for prefix). The
# separator set is [-:_], matching how gateways tag free ids in practice.
_FREE_SUFFIX = re.compile(r"[-:_]free$", re.I)
_FREE_PREFIX = re.compile(r"^free[-:_]", re.I)


def strip_free_marker(model_id: str) -> str:
    """Remove ONLY a free marker at a segment boundary. Never fuzzy, never
    a rename map. If stripping would empty the string, return the input.

    A "free marker" is the substring "free" when it appears as a standalone
    segment at the start or end of the id, delimited by '-', ':', or '_'.
    Mid-string occurrences (``freetier``) and occurrences in the namespace
    portion after a '/' with no further separator are NOT markers.
    """
    s = _FREE_SUFFIX.sub("", model_id)
    s = _FREE_PREFIX.sub("", s)
    return s or model_id


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
    """Numeric-segment-aware ordering so 'x-2.10' sorts after 'x-2.2'."""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", s)]


def build_groups(providers):
    """Group raw ids by their free-marker-stripped name, deterministically.

    Returns (group_names, groups, raw_count) where:
      - group_names: alphabetically sorted list of stripped names, one per row
      - groups: dict[stripped_name] -> {
            "gateways": set[str],                  # every gw carrying ANY variant
            "raw_count": int,                      # # of distinct raw ids in the group
            "variants": list[(gateway, raw_id)],   # per-(gw,raw) wiring rows, in
                                                   # DISPLAY_ORDER then natural key
          }
      - raw_count: total number of distinct raw ids across all providers
        (the honest "endpoints" number; <tfoot> totals also derive from raw)

    The grouping removes ONLY a free-marker we already know is present
    (the same marker the free-roster rule tested for), so two genuinely
    different models can never collide. No fuzzy match, no rename map.
    """
    raw_ids = sorted({mid for models in providers.values() for mid in models})
    groups: dict = {}
    # Build the per-(gw, raw_id) variant map deterministically: one slot
    # per distinct raw id, then sort variants by DISPLAY_ORDER then by
    # the natural key of the raw id. We keep this separate from
    # `groups[].variants` so the byte-order in the HTML is stable.
    for mid in raw_ids:
        name = strip_free_marker(mid)
        slot = groups.setdefault(
            name,
            {"gateways": set(), "raw_count": 0, "variants": []},
        )
        slot["raw_count"] += 1
        for gw in DISPLAY_ORDER:
            if mid in providers.get(gw, []):
                slot["gateways"].add(gw)
                slot["variants"].append((gw, mid))
    for slot in groups.values():
        slot["variants"].sort(
            key=lambda pair: (DISPLAY_ORDER.index(pair[0]),
                              _natural_key(pair[1]))
        )
    group_names = sorted(groups.keys())
    return group_names, groups, len(raw_ids)


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


def render_page(roster, logo_b64):
    """Render the full HTML document as one string (byte-deterministic).

    Rows are GROUPED by stripped name: a model that ships as
    `vendor/x:free` on gateway A and `vendor/x-free` on gateway B renders
    as ONE row with dots on both A and B; the `#` column counts distinct
    gateways reached (so a gateway carrying both variants counts once).
    The <tfoot> per-gateway totals stay RAW per-gateway counts — that is
    the honest "ids tracked per gateway" number and must not change
    meaning just because the matrix collapsed. The embedded
    <script id="roster-data"> JSON island keeps the RAW roster verbatim
    (MCP and any other consumer read raw ids, not groups).
    """
    providers = roster["providers"]
    group_names, groups, raw_count = build_groups(providers)
    tick = roster.get("tick_epoch")
    if (
        isinstance(tick, (int, float))
        and not isinstance(tick, bool)
        and tick >= 0
    ):
        # Out-of-range epochs must degrade cleanly: render "unknown", never
        # crash the build. Huge future values overflow; floats NaN/inf raise
        # OverflowError/ValueError. Negative epochs would technically format
        # (pre-1970) but mark corrupt state, so they degrade to "unknown"
        # too (reviewer M4 consistency).
        try:
            ts = datetime.fromtimestamp(tick).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            ts = "unknown"
    else:
        ts = "unknown"

    def _wire_cell(gw, raw_id):
        """Return the inner-HTML for one (gateway, raw_id) wiring row.

        Imports from GATEWAY_WIRING; nous has no static URL (its base is
        read from auth.json at runtime) so the URL column carries the
        `base_url_source` prose instead so the reader still gets the
        exact runtime path.
        """
        w = GATEWAY_WIRING[gw]
        url_text = w.get("chat_completions_url") or w.get("base_url_source") or ""
        notes = w.get("notes", "")
        # All visible strings: escaped, monospaced, copy-pasteable.
        return (
            f'<span class="wire-gw">{escape(gw)}</span>'
            f'<span class="wire-id">{escape(raw_id)}</span>'
            f'<span class="wire-url">{escape(url_text)}</span>'
            f'<span class="wire-auth">{escape(w["auth"])}</span>'
            f'<span class="wire-api">{escape(w["api_type"])}</span>'
            + (f'<span class="wire-notes">{escape(notes)}</span>' if notes else "")
        )

    def row_html(name, group_index):
        group = groups[name]
        present_gws = group["gateways"]
        cells = [
            '<td class="yes">&#9679;</td>' if gw in present_gws else '<td class="no"></td>'
            for gw in DISPLAY_ORDER
        ]
        # One <input type="checkbox"> per group, named with a stable
        # group_index so two groups can never share an id. The label
        # wraps both the checkbox and the stripped name, so clicking
        # the name flips the checkbox (no onclick, no JS).
        # `tr:has(input:checked) ~ tr.expand` in the stylesheet reveals
        # the per-variant wiring rows when the user expands the name.
        cb_id = f"row-{group_index}"
        name_row = (
            f'<tr class="name-row">'
            f'<th>'
            f'<label for="{cb_id}">'
            f'<input type="checkbox" id="{cb_id}" class="row-expand" aria-label="toggle wiring for {escape(name)}">'
            f'<span class="caret" aria-hidden="true">&#9656;</span> '
            f'{escape(name)}'
            f'</label>'
            f'</th>'
            f'<td class="n">{len(present_gws)}</td>'
            + "".join(cells)
            + "</tr>"
        )
        # Per-(gateway, raw_id) expansion rows. One row per variant; a
        # single-variant group still gets its one wiring row so nothing
        # is hidden. colspan = 2 + len(DISPLAY_ORDER) so the row spans
        # the full table width on expand.
        colspan = 2 + len(DISPLAY_ORDER)
        expand_rows = "".join(
            f'<tr class="expand">'
            f'<td colspan="{colspan}">'
            f'<div class="wire">{_wire_cell(gw, mid)}</div>'
            f'</td>'
            f'</tr>'
            for (gw, mid) in group["variants"]
        )
        return name_row + expand_rows

    head_cells = "<th>model id</th><th>#</th>" + "".join(
        f"<th>{escape(gw)}</th>" for gw in DISPLAY_ORDER
    )
    # <tfoot> stays RAW per-gateway counts — that is the honest "ids
    # tracked per gateway" number and must not change meaning because
    # the matrix collapsed to one row per group above.
    foot_cells = "".join(
        '<td class="n">%d</td>' % len(providers.get(gw, [])) for gw in DISPLAY_ORDER
    )
    chips = (
        f'<span class="chip"><b>{len(group_names)}</b> unique models</span>'
        f'<span class="chip"><b>{raw_count}</b> endpoints</span>'
        f'<span class="chip"><b>{len(DISPLAY_ORDER)}</b> gateways</span>'
    )
    meta = f"last refreshed {ts} · rebuilt every 1h"
    # sort_keys keeps the embedded JSON byte-stable across builds; the
    # JSON island stays the RAW roster so MCP and other consumers that
    # read raw ids are unaffected by the matrix grouping.
    roster_json = json.dumps(roster, sort_keys=True).replace("</", "<\\/")
    roster_script = (
        f'<script type="application/json" id="roster-data">{roster_json}</script>'
    )
    body_rows = "".join(
        row_html(name, i) for i, name in enumerate(group_names)
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
  tbody tr.expand .wire {{ display:grid; grid-template-columns: 92px 1fr 1fr 1fr 92px; gap:6px 14px; font:11.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--nord4); }}
  tbody tr.expand .wire-gw {{ color:var(--nord9); font-weight:600; }}
  tbody tr.expand .wire-id {{ color:var(--nord6); }}
  tbody tr.expand .wire-url {{ color:var(--nord8); word-break:break-all; }}
  tbody tr.expand .wire-auth {{ color:var(--nord4); }}
  tbody tr.expand .wire-api {{ color:var(--nord14); }}
  tbody tr.expand .wire-notes {{ grid-column:2 / -1; color:var(--nord13); font-size:10.5px; font-style:italic; padding-top:2px; }}
</style></head>
<body><div class="wrap">
<header class="top">
  <img class="logo" alt="watchdog radar-dog logo" src="data:image/png;base64,{logo_b64}">
  <div>
    <h1>Free Inference Watchdog</h1>
    <div class="meta">{meta}</div>
    <div class="chips">{chips}</div>
    {roster_script}
  </div>
</header>
<table>
<thead><tr>{head_cells}</tr></thead>
<tbody>
{body_rows}</tbody>
<tfoot><tr><th>tracked ids per gateway</th><td></td>{foot_cells}</tr></tfoot>
</table>
<footer class="note">ids shown verbatim per gateway — the same underlying model can ship under different local ids.
Static file, rebuilt each tick.</footer>
</div></body></html>"""


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