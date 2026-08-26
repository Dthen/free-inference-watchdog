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
        rows = re.findall(r"<tr><th>", tbody)
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
