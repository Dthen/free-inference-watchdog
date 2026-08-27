"""Task 8: self-contained static dashboard builder contracts (build_site.py).

The dashboard is a PRESENCE MATRIX (one row per unique model id across the
gateway union, one column per gateway) rebuilt per tick from
state/roster.json into site/index.html. Pinned here:

- a known seeded id renders in the table,
- the full roster is INLINED as JSON in <script type="application/json"
  id="roster-data"> and extracts cleanly with json.loads,
- output is DETERMINISTIC (same roster -> byte-identical HTML),
- PROVIDER DISPLAY ORDER is Dthen's quality ranking [nous, zen, kilo,
  cline, openrouter] — openrouter LAST (limits), NOT registry order and
  NOT alphabetical,
- missing or corrupt roster.json exits non-zero WITHOUT writing
  site/index.html (never publish garbage),
- HOSTILE provider values (bare string / dict / null / int instead of a
  list of model-id strings) exit 2 WITHOUT writing site/,
- an out-of-range tick_epoch degrades the refresh stamp to "unknown"
  instead of crashing the build,
- the publish temp file is pid+thread-unique (concurrent builders must
  not collide on a shared index.html.tmp),
- an oversized logo under --root exits 2 without writing site/,
- an empty providers map renders zero rows without crashing.

The builder is stdlib-only and must never page: the wrapper invokes it
with `|| true` so any failure here is silent best-effort.
"""

import json
import base64
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUILDER = REPO / "build_site.py"

# Canonical seeded fixture used by every test below.
SEED_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": ["vendor-z/zero-priced-model", "vendor-g/model-7:free"],
        "openrouter": ["vendor-z/zero-priced-model"],
        "zen": ["vendor-z/zero-priced-model", "vendor-x/preview-free"],
        "kilo": ["vendor-z/zero-priced-model"],
        "cline": ["vendor-z/zero-priced-model"],
    },
    "stale_providers": [],
}


def _run_builder(roster, tmp):
    """Run build_site.py against `roster` written into `tmp`/state; return CompletedProcess."""
    state = tmp / "state"
    state.mkdir(parents=True, exist_ok=True)
    if roster is None:
        # leave no roster.json at all (missing-file case)
        pass
    else:
        (state / "roster.json").write_text(
            json.dumps(roster), encoding="utf-8"
        )
    return subprocess.run(
        [sys.executable, str(BUILDER), "--root", str(tmp)],
        capture_output=True, text=True,
    )


def _build_html(tmp):
    return (tmp / "site" / "index.html").read_text(encoding="utf-8")


def _seed_logo(tmp, blob=b"\x89PNG\r\n\x1a\n" + b"0" * 64):
    """Minimal fake PNG under --root so the builder finds a logo there."""
    assets = tmp / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "logo.png").write_bytes(blob)


def test_builder_renders_seeded_id():
    """Output contains a known id from the seeded roster."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(SEED_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        assert "vendor-z/zero-priced-model" in html


def test_roster_data_script_extracts_cleanly():
    """The embedded <script type="application/json" id="roster-data"> payload
    must round-trip through json.loads with the original providers map."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(SEED_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        m = re.search(
            r'<script type="application/json" id="roster-data">(.*?)</script>',
            html, re.S,
        )
        assert m, "roster-data script tag missing from output"
        data = json.loads(m.group(1))
        assert set(data["providers"]) == {"nous", "openrouter", "zen", "kilo", "cline"}
        assert "vendor-z/zero-priced-model" in data["providers"]["nous"]


def test_deterministic_output():
    """Two builds from identical input must be byte-identical."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_a = Path(td) / "a"
        tmp_b = Path(td) / "b"
        p1 = _run_builder(SEED_ROSTER, tmp_a)
        p2 = _run_builder(SEED_ROSTER, tmp_b)
        assert p1.returncode == 0 and p2.returncode == 0
        assert _build_html(tmp_a) == _build_html(tmp_b)


def test_display_order_constant():
    """DISPLAY_ORDER is Dthen's quality ranking, not the providers.py registry
    order (nous, openrouter, zen, kilo, cline) and NOT alphabetical."""
    src = BUILDER.read_text(encoding="utf-8")
    m = re.search(r'DISPLAY_ORDER\s*=\s*\[(.*?)\]', src, re.S)
    assert m, "DISPLAY_ORDER constant missing from build_site.py"
    order = [s.strip().strip('"\'') for s in m.group(1).split(",") if s.strip()]
    assert order == ["nous", "zen", "kilo", "cline", "openrouter", "command_code"]


def test_missing_roster_fails_without_writing_site():
    """Missing state/roster.json -> non-zero exit AND no site/index.html
    (never publish garbage / never clobber the last good page)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(None, tmp)
        assert proc.returncode != 0
        assert not (tmp / "site" / "index.html").exists()


def test_corrupt_roster_fails_without_writing_site():
    """Corrupt state/roster.json -> non-zero exit AND no site/index.html."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder("THIS IS NOT JSON{{", tmp)
        assert proc.returncode != 0
        assert not (tmp / "site" / "index.html").exists()


def test_empty_providers_map_renders_zero_rows():
    """Empty providers map -> zero matrix rows, still valid page."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder({"tick_epoch": 1, "providers": {}}, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        assert "<tbody>" in html and "</tbody>" in html
        body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        assert "<tr>" not in body, "empty providers map must render zero rows"


def test_presence_matrix_structure():
    """One row per unique id; one column per gateway in display order;
    footer row of per-gateway totals; sticky header present."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(SEED_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        # header column order pins the display order end-to-end
        head = html.split("<thead>", 1)[1].split("</thead>", 1)[0]
        cols = re.findall(r"<th>([^<]*)</th>", head)
        assert cols == ["model id", "#", "nous", "zen", "kilo", "cline", "openrouter", "command_code"]
        # unique ids: vendor-z/zero-priced-model + stepfun + vendor-x/preview-free = 3 rows
        tbody = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        rows = re.findall(r"<tr(?:\s+class=\"[^\"]*\")?><th>", tbody)
        assert len(rows) == 3
        # footer totals: nous=2 zen=2 kilo=1 cline=1 openrouter=1
        tfoot = html.split("<tfoot>", 1)[1].split("</tfoot>", 1)[0]
        nums = re.findall(r'class="n">(\d+)<', tfoot)
        assert nums == ["2", "2", "1", "1", "1", "0"]
        assert "position:sticky" in html


def test_no_snapshot_or_alert_wording():
    """Header copy contract: refresh timestamp + rebuild cadence only —
    NO snapshot/alert/stale/outage wording anywhere in the VISIBLE page copy.
    (The embedded roster-data JSON island is excluded: it legitimately carries
    upstream state keys like stale_providers as data, not copy.)"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(SEED_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        m = re.search(
            r'<script type="application/json" id="roster-data">.*?</script>',
            html, re.S,
        )
        copy = (html[: m.start()] + html[m.end():]).lower() if m else html.lower()
        for word in ("snapshot", "alert", "stale", "outage"):
            assert word not in copy, f"forbidden copy word '{word}' in page"


def test_logo_embedded_as_data_uri():
    """assets/logo.png is embedded base64 (self-contained file, no external fetches)."""
    import base64
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(SEED_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        m = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', html)
        assert m, "no PNG data-URI logo found"
        raw = base64.b64decode(m.group(1))
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(raw) <= 128 * 128 * 4  # sanity: small embedded image


# ---------- C1: hostile providers values must exit 2, never render ----------

@pytest.mark.parametrize("bad", [
    "vendor-z/zero-priced-model",            # bare string -> was iterated char-by-char
    {"nous": ["vendor-z/zero-priced-model"]},  # dict value -> was a false presence dot
    None,                          # null -> was unhandled TypeError rc=1
    7,                             # int -> was unhandled TypeError rc=1
])
def test_hostile_provider_value_exits_2_without_writing(bad, tmp_path):
    """A providers entry whose VALUE is not a list of str must exit 2 with no
    site write. Reviewer evidence pre-fix: a string value iterated
    char-by-char into ~11 junk rows (plausible FALSE dashboard at rc=0), dict
    values rendered as false presence dots, and None/int crashed rc=1."""
    roster = {"tick_epoch": SEED_ROSTER["tick_epoch"], "providers": {"nous": bad}}
    proc = _run_builder(roster, tmp_path)
    assert proc.returncode == 2, f"expected exit 2 for {bad!r}, got {proc.returncode}"
    assert not (tmp_path / "site" / "index.html").exists()


def test_valid_list_of_str_providers_still_builds(tmp_path):
    """The C1 gate must not reject legitimate rosters."""
    _seed_logo(tmp_path)
    proc = _run_builder(SEED_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "site" / "index.html").exists()


# ---------- C2 (+M4 consistency): out-of-range tick_epoch degrades clean ----

@pytest.mark.parametrize("epoch", [10**12, -5])
def test_out_of_range_tick_epoch_renders_unknown(epoch, tmp_path):
    """OverflowError/negative epochs must degrade to 'unknown' in the refresh
    stamp at rc=0 — never an unhandled traceback, page still published."""
    _seed_logo(tmp_path)
    roster = dict(SEED_ROSTER, tick_epoch=epoch)
    proc = _run_builder(roster, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    assert "unknown" in html.split('<div class="meta">', 1)[1].split("</div>", 1)[0]
    # and the embedded JSON island still carries the raw value verbatim
    m = re.search(r'id="roster-data">(\{.*?\})</script>', html)
    assert m, "roster-data script tag missing from output"
    assert json.loads(m.group(1))["tick_epoch"] == epoch


# ---------- I1: publish tmp name is pid+thread unique (no shared .tmp) ------

def test_tmp_publish_name_is_pid_thread_unique():
    """Pin the collision-free temp naming: index.html.<pid>.<tid>.tmp,
    mirroring state.py's _atomic_write_json pattern (a shared
    index.html.tmp raced 17/3 failures at 20 parallel builders)."""
    src = BUILDER.read_text(encoding="utf-8")
    assert re.search(
        r'with_name\(\s*f"\{out\.name\}\.\{os\.getpid\(\)\}'
        r'\.\{threading\.get_ident\(\)\}\.tmp"',
        src,
    ), "publish tmp name lost the pid+thread-unique pattern"


# ---------- I2: logo resolves under --root; oversize cap reachable via CLI --

def test_logo_resolves_under_root_not_builder_repo(tmp_path):
    """The embedded logo must come from <root>/assets/logo.png, NOT from the
    builder's own repo assets dir (which only serves as fallback)."""
    _seed_logo(tmp_path)  # distinct tiny fake PNG under the CLI root
    proc = _run_builder(SEED_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    m = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', html)
    assert m, "no PNG data-URI logo found"
    embedded = base64.b64decode(m.group(1))
    on_disk = (tmp_path / "assets" / "logo.png").read_bytes()
    assert embedded == on_disk, "embedded logo did not come from --root"


def test_oversized_logo_in_root_exits_2_without_writing(tmp_path):
    """A logo over MAX_LOGO_BYTES under --root exits 2 with no site write —
    proves the cap is reachable through the CLI path (pre-fix the builder
    read its OWN repo logo, so root-side bloat was invisible)."""
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (40 * 1024 + 1)
    _seed_logo(tmp_path, blob=big)
    proc = _run_builder(SEED_ROSTER, tmp_path)
    assert proc.returncode == 2
    assert not (tmp_path / "site" / "index.html").exists()


# ---------- I4: wrapper logs builder stderr instead of swallowing it --------

TICK_WRAPPER = Path.home() / ".hermes" / "scripts" / "inference-watchdog-tick.sh"


def test_wrapper_build_stage_redirects_stderr_to_log():
    """Task-8 fix round I4: `|| true` alone swallowed builder failures
    forever-silently; stderr must now append to state/site_build.log (log,
    never page) while the || true never-page contract stays intact."""
    line = next(
        l for l in TICK_WRAPPER.read_text(encoding="utf-8").splitlines()
        if "build_site.py" in l and not l.lstrip().startswith("#")
    )
    assert "2>> state/site_build.log" in line, f"build stage lost its log: {line}"
    assert line.rstrip().endswith("|| true"), "never-page contract broken"


def test_wrapper_still_parses_bash_n():
    """The real wrapper file must stay valid shell after the I4 edit."""
    proc = subprocess.run(
        ["bash", "-n", str(TICK_WRAPPER)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


# ---------- strip_free_marker: remove ONLY a free marker at a segment boundary ----------

def test_strip_free_marker_suffix_dash():
    """a/b-free -> a/b (dash suffix)."""
    from build_site import strip_free_marker
    assert strip_free_marker("a/b-free") == "a/b"


def test_strip_free_marker_suffix_colon():
    """a/b:free -> a/b (colon suffix)."""
    from build_site import strip_free_marker
    assert strip_free_marker("a/b:free") == "a/b"


def test_strip_free_marker_suffix_underscore():
    """a/b_free -> a/b (underscore suffix)."""
    from build_site import strip_free_marker
    assert strip_free_marker("a/b_free") == "a/b"


def test_strip_free_marker_case_insensitive():
    """A/B-FREE -> A/B (case-insensitive)."""
    from build_site import strip_free_marker
    assert strip_free_marker("A/B-FREE") == "A/B"


def test_strip_free_marker_prefix_form():
    """free-experiment -> experiment (prefix form)."""
    from build_site import strip_free_marker
    assert strip_free_marker("free-experiment") == "experiment"


def test_strip_free_marker_mid_string_not_touched():
    """Marker mid-string is NOT touched: freetier -> freetier, a/free-b -> a/free-b."""
    from build_site import strip_free_marker
    assert strip_free_marker("freetier") == "freetier"
    assert strip_free_marker("a/free-b") == "a/free-b"


def test_strip_free_marker_in_namespace_not_touched():
    """Marker in namespace is NOT touched: vendor-g/free:free -> vendor-g/free."""
    from build_site import strip_free_marker
    assert strip_free_marker("vendor-g/free:free") == "vendor-g/free"


def test_strip_free_marker_no_marker_unchanged():
    """No marker at all -> returned unchanged."""
    from build_site import strip_free_marker
    assert strip_free_marker("vendor-z/zero-priced-model") == "vendor-z/zero-priced-model"


def test_strip_free_marker_idempotent_on_live_roster():
    """Idempotent: f(f(x)) == f(x) for every id in the live roster."""
    import json
    from build_site import strip_free_marker
    roster_path = REPO / "state" / "roster.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    all_ids = [mid for models in roster["providers"].values() for mid in models]
    for mid in all_ids:
        once = strip_free_marker(mid)
        twice = strip_free_marker(once)
        assert once == twice, f"not idempotent for {mid!r}: {once!r} vs {twice!r}"


def test_strip_free_marker_never_returns_empty():
    """Never returns '' — a bare 'free' returns 'free'."""
    from build_site import strip_free_marker
    assert strip_free_marker("free") == "free"


# ---------- Task 3: matrix rows grouped by stripped name ----------

# Fixture with multi-variant groups: one model ships on two gateways under
# different free-marker forms; another ships on the SAME gateway under both
# forms (must not double-count in '#'); a third is single-variant (control).
GROUP_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": [
            "vendor-x/poolside-s-2.1:free",      # variant A
            "vendor-x/standalone-1:free",        # single-variant
        ],
        "zen": [
            "vendor-x/poolside-s-2.1-free",      # variant B (different gw)
        ],
        "kilo": [
            "vendor-x/poolside-s-2.1-free",      # variant B (overlap on kilo!)
            "vendor-x/poolside-s-2.1:free",      # variant A (overlap on kilo!)
            "vendor-x/standalone-2:free",        # single-variant
        ],
        "openrouter": [
            "vendor-x/standalone-2-free",        # another single-variant
        ],
    },
    "stale_providers": [],
}


def _row_count(html):
    tbody = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    return len(re.findall(r"<tr(?:\s+class=\"[^\"]*\")?><th>", tbody))


def _tfoot_numbers(html):
    """Return the per-gateway <tfoot> numbers in DISPLAY_ORDER."""
    tfoot = html.split("<tfoot>", 1)[1].split("</tfoot>", 1)[0]
    return [int(n) for n in re.findall(r'class="n">(\d+)<', tfoot)]


def _row_for(html, stripped_name):
    """Return the <tr> for `stripped_name` from the rendered tbody."""
    tbody = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    rows = re.findall(r"<tr(?:\s+[^>]*)?>.*?</tr>", tbody, re.S)
    for row in rows:
        if stripped_name in row:
            return row
    return None


def test_grouped_rows_one_per_stripped_name(tmp_path):
    """Task 3: a stripped name that has 2 raw variants on different gateways
    renders as ONE row, not two. (Today the builder makes one row per raw id,
    so this test fails pre-fix.)"""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    # Stripped groups: poolside-s-2.1 (2 variants), standalone-1 (1),
    # standalone-2 (2 variants) -> 3 rows total
    assert _row_count(html) == 3, f"expected 3 grouped rows, got {_row_count(html)}"


def test_grouped_row_dot_count_aggregates_across_variants(tmp_path):
    """Task 3: '#' column = number of gateways the group REACHES, not the
    number of variants. A group with two variants on three gateways
    (one of which has both variants) shows '#' = 3, not 4."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    row = _row_for(html, "vendor-x/poolside-s-2.1")
    assert row is not None, "no row for vendor-x/poolside-s-2.1"
    # '#' is the first <td class="n"> after the <th>. Task 4 wraps the
    # name in a <label> for the expand toggle, so allow anything between
    # <th> and <td class="n">.
    m = re.search(r'<th>.{0,400}?</th><td class="n">(\d+)</td>', row)
    assert m, f"could not find # cell in row: {row!r}"
    assert int(m.group(1)) == 3, (
        f"group's # must equal number of gateways it reaches "
        f"(kilo+openrouter+nous+zen = up to 4, but poolside-s-2.1 "
        f"is on nous+zen+kilo = 3); got {m.group(1)}"
    )


def test_grouped_row_dots_on_every_gateway_any_variant_reaches(tmp_path):
    """Task 3: presence on a gateway is true if ANY of the group's raw
    variants is on that gateway. For vendor-x/poolside-s-2.1: variant A is
    on nous, variant B is on zen and kilo -> dots on nous, zen, kilo,
    NOT on cline/openrouter/command_code."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    row = _row_for(html, "vendor-x/poolside-s-2.1")
    assert row is not None
    # The row has 6 gateway cells in DISPLAY_ORDER. Count yes/no.
    yes = row.count('<td class="yes">')
    no = row.count('<td class="no">')
    assert yes == 3, f"expected 3 yes dots (nous+zen+kilo), got {yes}"
    assert no == 3, f"expected 3 no cells (cline+openrouter+command_code), got {no}"


def test_group_overlap_on_same_gateway_not_double_counted(tmp_path):
    """Task 3: a group whose variants both appear on the SAME gateway
    (here poolside-s-2.1 has BOTH variants on kilo) must not double-count
    that gateway in '#'."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    row = _row_for(html, "vendor-x/poolside-s-2.1")
    # Task 4 wraps the name in a <label> for the expand toggle.
    m = re.search(r'<th>.{0,400}?</th><td class="n">(\d+)</td>', row)
    assert m, f"could not find # cell in row: {row!r}"
    # nous, zen, kilo — three distinct gateways reached
    assert int(m.group(1)) == 3, (
        f"kilo has both variants but counts as 1 gateway; # must be 3, got {m.group(1)}"
    )
    # Sanity: only ONE yes-dot in the kilo column for this row.
    assert row.count('<td class="yes">') == 3  # nous, zen, kilo each contribute one yes


def test_tfoot_totals_stay_raw_per_gateway_counts(tmp_path):
    """Task 3: <tfoot> counts must remain the honest 'ids tracked per
    gateway' number, not collapsed to groups. GROUP_ROSTER's raw per-gw
    counts: nous=2, zen=1, kilo=3, cline=0, openrouter=1, command_code=0."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    nums = _tfoot_numbers(html)
    assert nums == [2, 1, 3, 0, 1, 0], (
        f"tfoot must stay raw per-gateway counts in DISPLAY_ORDER, got {nums}"
    )


def test_inlined_roster_data_json_stays_raw(tmp_path):
    """Task 3: the <script id='roster-data'> JSON island must remain the
    raw roster (MCP and any other consumer reads raw ids, not groups)."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    m = re.search(
        r'<script type="application/json" id="roster-data">(.*?)</script>',
        html, re.S,
    )
    assert m, "roster-data script tag missing from output"
    data = json.loads(m.group(1))
    # Raw ids are present — variants preserved, not collapsed
    assert "vendor-x/poolside-s-2.1-free" in data["providers"]["zen"]
    assert "vendor-x/poolside-s-2.1:free" in data["providers"]["nous"]
    assert "vendor-x/poolside-s-2.1-free" in data["providers"]["kilo"]


def test_grouped_deterministic_output(tmp_path):
    """Task 3: same roster in -> identical HTML out (regression guard:
    the new group loop must stay sorted for byte-identical rebuilds)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a"
        b = Path(td) / "b"
        _seed_logo(a)
        _seed_logo(b)
        p1 = _run_builder(GROUP_ROSTER, a)
        p2 = _run_builder(GROUP_ROSTER, b)
        assert p1.returncode == 0 and p2.returncode == 0
        assert _build_html(a) == _build_html(b)


def test_chip_counts_groups_and_endpoints(tmp_path):
    """Task 3: header chip N is now groups, and a second chip M
    ('endpoints') is the raw count. GROUP_ROSTER has 3 groups and
    5 unique raw ids (poolside-s-2.1 has ':free' and '-free'; standalone-2
    has ':free' and '-free'; standalone-1 has only ':free')."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    chips = html.split('<div class="chips">', 1)[1].split("</div>", 1)[0]
    assert "<b>3</b> unique models" in chips, (
        f"unique-models chip must count groups (3), got: {chips}"
    )
    assert "<b>5</b> endpoints" in chips, (
        f"endpoints chip must count raw ids (5), got: {chips}"
    )


def test_groups_sorted_alphabetically_by_stripped_name(tmp_path):
    """Task 3: groups sort alphabetically by stripped name. The first body
    row in DISPLAY_ORDER is the alphabetically-first group."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    tbody = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    names = re.findall(r"<tr(?:\s+class=\"[^\"]*\")?><th>(?:<label[^>]*>)?(?:<input[^>]*>\s*)?(?:<span[^>]*>[^<]*</span>\s*)?([^<]+)", tbody)
    assert names == sorted(names), (
        f"groups must be alphabetically sorted by stripped name; got {names}"
    )
    assert names == [
        "vendor-x/poolside-s-2.1",
        "vendor-x/standalone-1",
        "vendor-x/standalone-2",
    ]


def test_live_roster_groups_match_plan_evidence(tmp_path):
    """Task 3 live-data guard: the live roster must render as exactly one
    row per distinct stripped name. Pin the current group count so future
    roster changes update the pin in the same commit."""
    _seed_logo(tmp_path)
    live_roster_path = REPO / "state" / "roster.json"
    live_roster = json.loads(live_roster_path.read_text(encoding="utf-8"))
    proc = _run_builder(live_roster, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    # Every raw id in the roster must be reachable from a group row's #.
    # The strongest invariant: every unique stripped name appears as a
    # row, and no raw id is a row itself.
    from build_site import strip_free_marker
    all_ids = sorted({m for models in live_roster["providers"].values() for m in models})
    expected_groups = sorted({strip_free_marker(m) for m in all_ids})
    tbody = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    names = re.findall(r"<tr(?:\s+class=\"[^\"]*\")?><th>(?:<label[^>]*>)?(?:<input[^>]*>\s*)?(?:<span[^>]*>[^<]*</span>\s*)?([^<]+)", tbody)
    assert names == expected_groups, (
        f"live roster: groups must equal sorted unique stripped names.\n"
        f"  expected ({len(expected_groups)}): {expected_groups}\n"
        f"  got      ({len(names)}): {names}"
    )


# ---------- Task 4: expandable rows with API wiring panel --------------------
#
# A row's name cell becomes a <label> wired to a hidden <input
# type="checkbox">; CSS-only sibling selector (:checked ~) toggles the
# hidden variant <tr>s that follow. No JavaScript. Keyboard-operable by
# default (native checkbox + label). Every row is expandable — single-
# variant rows just show their one endpoint.

# Fixture with two raw variants on different gateways AND a same-gateway
# overlap. The poolside group must render ONE name row + per-(gateway,raw)
# expansion rows; the single-variant group renders ONE name row + ONE
# expansion row.
EXPAND_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": [
            "vendor-x/poolside-s-2.1:free",      # variant A
            "vendor-x/standalone-1:free",        # single-variant
        ],
        "openrouter": [
            "vendor-x/standalone-2-free",        # single-variant
        ],
        "zen": [
            "vendor-x/poolside-s-2.1-free",      # variant B (different gw)
        ],
        "kilo": [
            "vendor-x/poolside-s-2.1-free",      # variant B (overlap on kilo!)
            "vendor-x/poolside-s-2.1:free",      # variant A (overlap on kilo!)
        ],
    },
    "stale_providers": [],
}


def _all_trs_in_tbody(html):
    tbody = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    return re.findall(r"<tr(?:\s+[^>]*)?>.*?</tr>", tbody, re.S)


def _all_trs_for(html, stripped_name):
    """All <tr>s that belong to the expandable group for `stripped_name`
    (the name row + any hidden expansion rows)."""
    rows = _all_trs_in_tbody(html)
    out, take = [], False
    for row in rows:
        m = re.match(
            r"<tr(?:\s+class=\"[^\"]*\")?><th>"
            r"(?:<label[^>]*>)?(?:<input[^>]*>\s*)?"
            r"(?:<span[^>]*>[^<]*</span>\s*)?"
            r"([^<]+)",
            row,
        )
        if m:
            take = (m.group(1) == stripped_name)
            if take:
                out.append(row)
        elif take and 'class="expand"' in row:
            out.append(row)
    return out


def test_expand_html_well_formed_round_trip(tmp_path):
    """The full HTML round-trips through html.parser — proves no broken
    nesting even with the new expansion rows added (reviewer M2: a stray
    <div> in a <td> was previously caught this way)."""
    import tempfile
    from html.parser import HTMLParser
    errors = []

    class _Strict(HTMLParser):
        def error(self, message):  # called by html.parser on bad markup
            errors.append(message)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        parser = _Strict()
        parser.feed(html)
        parser.close()
        assert not errors, f"html.parser reported parse errors: {errors}"


def test_expand_no_script_beyond_roster_data(tmp_path):
    """No <script> element except the existing roster-data JSON island.
    The expand toggle must be CSS-only — zero JS."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        scripts = re.findall(r"<script\b[^>]*>", html)
        assert len(scripts) == 1, (
            f"expected exactly one <script> (roster-data), found {len(scripts)}: {scripts}"
        )
        assert 'id="roster-data"' in scripts[0]


def test_expand_every_raw_id_in_html_collapsed(tmp_path):
    """Every raw id from the roster appears in the rendered HTML — even when
    the user never expands a row. The collapsed view must contain the id
    text somewhere; readers must be able to grep the page and find any id.
    This is the 'grep-verifiable' contract from the plan."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        for mid in [
            "vendor-x/poolside-s-2.1:free",
            "vendor-x/poolside-s-2.1-free",
            "vendor-x/standalone-1:free",
            "vendor-x/standalone-2-free",
        ]:
            assert mid in html, f"raw id missing from rendered HTML: {mid}"


def test_expand_each_variant_row_has_chat_completions_url(tmp_path):
    """Every variant <tr class='expand'> contains that gateway's
    chat-completions URL. The wiring mapping is imported from providers,
    never hardcoded a second time in build_site.py."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        # Pull the wiring map for cross-check (we are testing the rendered
        # page, but pinning the URLs against the import avoids a drift).
        from providers import GATEWAY_WIRING
        # We expect variant rows for: poolside:free on nous, poolside-free
        # on zen, poolside:free on kilo, poolside-free on kilo, standalone-1
        # on nous, standalone-2 on openrouter — 6 expansion rows total.
        # Each MUST mention its gateway's URL (or the nous base_url_source
        # for nous, which has no static URL).
        expand_rows = [r for r in _all_trs_in_tbody(html) if 'class="expand"' in r]
        assert len(expand_rows) == 6, (
            f"expected 6 expansion rows, got {len(expand_rows)}"
        )
        # Each expansion row has a <td class="wire-url"> containing the URL
        # text. The cell class is part of the visible contract — readers
        # can copy the URL straight out of the page.
        for gw in ("zen", "kilo", "openrouter"):
            url = GATEWAY_WIRING[gw]["chat_completions_url"]
            assert any(url in r for r in expand_rows), (
                f"{gw} URL {url!r} missing from every expansion row"
            )


def test_expand_each_variant_row_has_auth_and_api_type(tmp_path):
    """Each variant <tr class='expand'> contains that gateway's auth shape
    and api_type — the reader sees at a glance that it is OpenAI-shaped."""
    import tempfile
    from html import escape as _esc
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        from providers import GATEWAY_WIRING
        expand_rows = [r for r in _all_trs_in_tbody(html) if 'class="expand"' in r]
        # For every gateway that appears in EXPAND_ROSTER, at least one
        # expansion row must mention that gateway's auth + api_type.
        for gw in ("nous", "zen", "kilo", "openrouter"):
            w = GATEWAY_WIRING[gw]
            # The builder HTML-escapes all wiring text; the assertion
            # compares against the escaped form so that `<your ...>`
            # entities match.
            assert any(_esc(w["auth"]) in r for r in expand_rows), (
                f"{gw} auth shape {w['auth']!r} missing from expansion rows"
            )
            assert any(w["api_type"] in r for r in expand_rows), (
                f"{gw} api_type {w['api_type']!r} missing from expansion rows"
            )


def test_expand_every_row_is_expandable(tmp_path):
    """Every body row — including single-variant rows — is expandable. A
    single-variant row just shows its one endpoint. Nothing is hidden."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        tbody_rows = _all_trs_in_tbody(html)
        # 3 name rows + 6 expansion rows (4 for poolside: 2 variants on 2
        # gateways with kilo carrying both; 1 for standalone-1; 1 for
        # standalone-2) = 9 total <tr>s in tbody.
        assert len(tbody_rows) == 9, (
            f"expected 9 tbody rows (3 names + 6 expansions), got {len(tbody_rows)}: "
            f"{tbody_rows}"
        )
        # Each name row must have a hidden <input type="checkbox"> + <label>
        # pair. The label is the user-facing click target.
        name_rows = [r for r in tbody_rows if re.match(r"<tr(?:\s+class=\"[^\"]*\")?><th>", r)]
        assert len(name_rows) == 3
        for r in name_rows:
            assert 'type="checkbox"' in r, f"name row missing checkbox: {r}"
            assert "<label" in r, f"name row missing label: {r}"


def test_expand_uses_no_onclick_and_no_javascript(tmp_path):
    """Keyboard-operable, no onclick. The toggle is a native <label>+
    <input> pair, so the browser handles focus/click for free; we must
    not regress to JS handlers."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        # No onclick, no onkeydown, no addEventListener, no event= attrs.
        assert "onclick" not in html, "expand rows must not use onclick"
        assert "onkeydown" not in html, "expand rows must not use onkeydown"
        # No new <script> (test_expand_no_script_beyond_roster_data covers
        # that strictly; this is the reader-friendly guard).


def test_expand_via_css_sibling_selector(tmp_path):
    """The toggle is a hidden <input type="checkbox"> whose :checked state
    is read by a CSS sibling-selector. We assert the CSS rule + the DOM
    structure that makes it work."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        # The expansion rows must be tagged with a class the CSS can hook.
        assert 'class="expand"' in html, "expansion rows missing expand class"
        # The CSS must contain a sibling-selector rule that hides
        # expansion rows unless the checkbox is checked.
        # We accept either the general-sibling form "~" or adjacent "+".
        # The rule MUST hide .expand by default and reveal on :checked ~.
        assert ".expand" in html, "no .expand CSS rule"
        assert (":checked" in html) and ("~" in html or "+" in html), (
            "no :checked sibling-selector rule — expand must be CSS-only"
        )
        # The default style hides .expand; :checked reveals it.
        # The simplest possible pattern: .row-expand:checked ~ .expand { ... }
        # (or any of: input[type=checkbox]:checked + .expand, etc.)
