"""Task 8: self-contained static dashboard builder contracts (build_site.py).

The dashboard is a PRESENCE MATRIX (one row per unique model id across the
gateway union, one column per gateway) rebuilt per tick from
state/roster.json into site/index.html. Pinned here:

- a known seeded id renders in the table,
- the ONE public snapshot is published as TWO artifacts (site/index.html and
  site/catalog.json); the page carries it inline as
  <script type="application/json" id="catalog-data"> plus the shared tool
  definitions as id="tool-contract", and both extract cleanly with json.loads,
- output is DETERMINISTIC (same roster -> byte-identical HTML),
- PROVIDER DISPLAY ORDER is Dthen's quality ranking [nous, tokenrouter, kilo,
  openrouter, amd, bai, nim] — openrouter BEFORE amd/bai (limits), nim last,
  NOT registry order and NOT alphabetical,
- missing or corrupt roster.json exits non-zero WITHOUT writing
  site/index.html (never publish garbage),
- HOSTILE provider values (bare string / dict / null / int instead of a
  list of model-id strings) exit 2 WITHOUT writing site/,
- an out-of-range/negative optional tick_epoch projects to null +
  tick_epoch_unknown, while a valid safe integer is retained verbatim (the
  direct local-MCP raw-value contract is unchanged and out of this boundary),
- every publish invocation stages its OWN temp files (concurrent builders
  must not collide on a shared artifact temp path),
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

# Extracts a group's display name from its name-row. The name lives INSIDE the
# last span: the builder emits a caret span, then the name span (dddb44a wrapped
# the name in a span for reliable browser tooltips). A single optional generic
# `<span>` group is greedy — it swallows the caret span and captures only the
# separating space — so the caret is consumed explicitly and the name span is
# matched as REQUIRED, which is what makes the capture the name and not a space.
_NAME_ROW_RE = (
    r'<tr class="name-row"><th[^>]*>(?:<label[^>]*>)?(?:<input[^>]*>\s*)?'
    r'(?:<span class="caret"[^>]*>[^<]*</span>\s*)?'
    r'<span[^>]*>([^<]*)</span>'
)

# Canonical seeded fixture used by every test below.
SEED_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": ["vendor-z/zero-priced-model", "vendor-g/model-7:free"],
        "openrouter": ["vendor-z/zero-priced-model"],
        "tokenrouter": ["vendor-z/zero-priced-model", "vendor-x/preview-free"],
        "kilo": ["vendor-z/zero-priced-model"],
        "amd": ["vendor-z/zero-priced-model"],
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


def test_catalog_data_script_extracts_cleanly():
    """The public catalogue island round-trips as the versioned snapshot.

    The CLI publishes ONE validated public snapshot, never raw roster state:
    the page's single ``catalog-data`` element and the external
    ``site/catalog.json`` carry the SAME snapshot, whose per-provider
    ``model_ids`` keep the raw tracked ids verbatim.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(SEED_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        islands = _script_elements(html)
        assert [attrs.get("id") for attrs, _ in islands] == list(_ISLAND_IDS)
        data = json.loads(islands[0][1])
        assert data["schema_version"] == 1 and data["snapshot_id"]
        assert set(data["providers"]) == {
            "nous", "tokenrouter", "kilo", "openrouter", "amd", "bai"}
        assert data["providers"]["nous"]["model_ids"] == [
            "vendor-g/model-7:free", "vendor-z/zero-priced-model"]
        external = json.loads(
            (tmp / "site" / "catalog.json").read_text(encoding="utf-8"))
        assert _typed(external) == _typed(data)
        assert 'id="roster-data"' not in html


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
    """DISPLAY_ORDER is Dthen's quality ranking, derived dynamically from
    config_loader.PROVIDERS (sorted by `display` field). Must produce
    [nous, tokenrouter, kilo, openrouter, amd, bai] — NOT providers.py
    registry order and NOT alphabetical. nim is absent: providers/nim.json
    is still on disk but carries enabled:false, so the loader gates it
    out of the registry. bai is last (display 5)."""
    from build_site import DISPLAY_ORDER
    assert DISPLAY_ORDER == ["nous", "tokenrouter", "kilo", "openrouter",
                             "amd", "bai"]
    # Confirm it's sourced from config_loader (dynamic), not a hardcoded list.
    src = BUILDER.read_text(encoding="utf-8")
    assert "PROVIDERS" in src and "config_loader" in src, \
        "DISPLAY_ORDER must be derived from config_loader.PROVIDERS, not hardcoded"
    assert re.search(r'DISPLAY_ORDER\s*=\s*list\(PROVIDERS\.keys\(\)\)', src), \
        "DISPLAY_ORDER must be derived via list(PROVIDERS.keys())"


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
    """One row per unique id; one column per active gateway in display order;
    footer row of per-gateway totals; sticky header present. Empty providers
    (bai in this fixture) are hidden from the matrix."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(SEED_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        # header column order pins the display order end-to-end
        # SEED_ROSTER has no bai models, so bai is hidden: 5 active gateways
        # Headers are gateway display keys (optionally wrapped in the signup
        # anchor) — extract the bare gateway name.
        head = html.split("<thead>", 1)[1].split("</thead>", 1)[0]
        cols = [c.strip() for c in re.findall(r"<th>(?:<a[^>]*>)?([^<(]*)", head)]
        # Header reads "model", not "model id" (14a442b renamed it for width).
        assert cols == ["model", "#", "nous", "tokenrouter", "kilo", "openrouter", "amd"]
        # unique ids: vendor-z/zero-priced-model + stepfun + vendor-x/preview-free = 3 rows
        # Each group is in its own <tbody>; count name-rows across all
        name_rows = re.findall(r'<tr class="name-row">', html)
        assert len(name_rows) == 3
        # footer totals: nous=2 tokenrouter=2 kilo=1 openrouter=1 amd=1 (bai hidden)
        tfoot = html.split("<tfoot>", 1)[1].split("</tfoot>", 1)[0]
        nums = re.findall(r'class="n">(\d+)<', tfoot)
        assert nums == ["2", "2", "1", "1", "1"]
        assert "position:sticky" in html


def test_no_snapshot_or_alert_wording():
    """Header copy contract: rebuild-cadence wording only —
    NO snapshot/alert/stale/outage wording anywhere in the VISIBLE page copy.
    (The two inert public JSON islands are excluded: the catalogue legitimately
    carries public keys like ``snapshot_id`` and ``warnings`` as DATA.)"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        proc = _run_builder(SEED_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        copy = _strip_public_islands(html).lower()
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


# ---------- C2: out-of-range tick_epoch projects to null + warning ----------

@pytest.mark.parametrize(("epoch", "projected"), [
    (10 ** 12, 10 ** 12),        # safe integer: retained verbatim
    (0, 0),                       # zero is a KNOWN epoch
    (-5, None),                   # negative: invalid optional value
    (2 ** 53, None),              # above the JSON-safe ceiling
])
def test_out_of_range_tick_epoch_projects_to_null_with_warning(
        epoch, projected, tmp_path):
    """An invalid optional epoch degrades to null + tick_epoch_unknown.

    The accepted public projection is authoritative on this route: a negative
    or unsafe epoch cannot be a published JSON-safe non-negative integer, so
    the snapshot carries ``tick_epoch: null`` and the frozen
    ``tick_epoch_unknown`` warning; a valid safe integer is retained verbatim.
    (Direct local-MCP raw-value behaviour is a different, unchanged contract.)
    """
    _seed_logo(tmp_path)
    roster = dict(SEED_ROSTER, tick_epoch=epoch)
    proc = _run_builder(roster, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    catalog = json.loads(_script_elements(html)[0][1])
    assert catalog["tick_epoch"] == projected
    assert ("tick_epoch_unknown" in catalog["warnings"]) is (projected is None)
    external = json.loads(
        (tmp_path / "site" / "catalog.json").read_text(encoding="utf-8"))
    assert _typed(external) == _typed(catalog)


# ---------- I1: every publish invocation stages its OWN temp paths ----------

def test_publish_staging_paths_are_unique_per_invocation(tmp_path, monkeypatch):
    """Overlapping writers never share a staging path (behavioural proof).

    Two barrier-synchronised writer invocations in one process, same output
    directory: ``tempfile.mkstemp`` must be called once per artifact per
    invocation, producing FOUR DISTINCT paths. Nothing is left behind, the
    final pair is coherent, and no shared ``.tmp`` name can be observed —
    replacing the old source-regex check with real concurrency behaviour.
    """
    import tempfile
    import threading

    import build_site
    from catalog_export import (capture_registry, serialize_snapshot,
                                validate_artifact_pair)

    tool_contract = _pair_tool_contract()
    registry = capture_registry(build_site.PROVIDERS, build_site.GATEWAY_WIRING)

    def pair(epoch):
        snapshot = build_site.build_snapshot(
            registry,
            {"tick_epoch": epoch,
             "providers": {"nous": [f"vendor-z/staged-{epoch}-free"]}},
            cadence_s=3600)
        return (
            serialize_snapshot(snapshot),
            build_site.render_catalog_page(
                snapshot, "ZmFrZWxvZ28=", tool_contract=tool_contract),
        )

    pairs = [pair(1), pair(2)]
    seen = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)
    barrier = threading.Barrier(len(pairs))
    errors = []

    def worker(catalog_text, html_text):
        try:
            barrier.wait(timeout=10)
            build_site.write_artifact_pair(tmp_path, catalog_text, html_text)
        except BaseException as exc:      # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=p) for p in pairs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    monkeypatch.undo()

    assert errors == [], errors
    assert len(seen) == 4, seen
    assert len(set(seen)) == 4, f"staging paths collided: {seen}"
    assert not any(Path(name).exists() for name in seen), "owned temp left behind"
    catalog_text = (tmp_path / "site" / "catalog.json").read_bytes().decode()
    html_text = (tmp_path / "site" / "index.html").read_bytes().decode()
    assert [(c, h) for c, h in pairs if c == catalog_text and h == html_text]
    assert validate_artifact_pair(catalog_text, html_text)["snapshot_id"]


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
#
# OPERATOR READ-BACK, NOT A DEFAULT-SUITE CHECK. These two checks read a
# DEPLOYED tick wrapper, which a portable checkout/CI run neither has nor may
# assume. There is no path default and no HOME lookup: without
# `--operator-tick-wrapper=PATH` both checks skip with an actionable opt-in
# reason. A skip here is NOT evidence that the deployed wrapper passes.


@pytest.mark.operator
def test_wrapper_build_stage_redirects_stderr_to_log(operator_tick_wrapper):
    """Task-8 fix round I4: `|| true` alone swallowed builder failures
    forever-silently; stderr must now append to state/site_build.log (log,
    never page) while the || true never-page contract stays intact."""
    line = next(
        l for l in operator_tick_wrapper.read_text(encoding="utf-8").splitlines()
        if "build_site.py" in l and not l.lstrip().startswith("#")
    )
    assert "2>> state/site_build.log" in line, f"build stage lost its log: {line}"
    assert line.rstrip().endswith("|| true"), "never-page contract broken"


@pytest.mark.operator
def test_wrapper_still_parses_bash_n(operator_tick_wrapper):
    """The selected wrapper file must stay valid shell after the I4 edit."""
    proc = subprocess.run(
        ["bash", "-n", str(operator_tick_wrapper)],
        capture_output=True,
        text=True,
        timeout=30,
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


def test_strip_free_marker_bare_free_is_not_a_marker():
    """Bare 'free' is the non-marker CONTROL, not an empty-result case.

    It carries no delimiter, so NEITHER delimiter-bounded regex matches and
    the id is returned untouched. Deliberately kept separate from the
    empty-result guard: it never reaches the `s or model_id` fallback, so it
    cannot detect a regression in that fallback.
    """
    from build_site import strip_free_marker
    assert strip_free_marker("free") == "free"


# Delimiter-only inputs: a "free" marker WITH its delimiter, but with nothing
# else in the id. Each is matched by exactly one of the two delimiter-bounded
# regexes, so stripping alone would leave "" — these are the inputs the
# helper's empty-result guard exists for.
EMPTY_RESULT_GUARD_INPUTS = [
    ":free",  # trailing colon-delimited marker, nothing before it
    "-free",  # trailing dash-delimited marker, nothing before it
    "_free",  # trailing underscore-delimited marker, nothing before it
    "free:",  # leading colon-delimited marker, nothing after it
    "free-",  # leading dash-delimited marker, nothing after it
    "free_",  # leading underscore-delimited marker, nothing after it
]


# Synthetic raw IDs for the idempotence guard — covers every marker form
# the production helper supports plus namespace-boundary, non-marker and
# delimiter-only (empty-result) inputs. These are NOT production ids; they
# exist only to pin the invariant.
SYNTHETIC_IDEMPOTENCE_IDS = [
    # Delimiter-bounded trailing markers
    "vendor-x/model-a:free",
    "vendor-x/model-b-free",
    "vendor-x/model-c_free",
    # Delimiter-bounded leading marker
    "free-experiment",
    # Non-marker identifiers (unchanged); bare "free" has no delimiter at all
    "vendor-z/zero-priced-model",
    "free",
    # Namespace boundary (marker in namespace, not at segment boundary)
    "vendor-g/free:free",
    # Empty-result inputs (delimiter-only; the guard returns the original)
    *EMPTY_RESULT_GUARD_INPUTS,
]


def test_strip_free_marker_idempotent_on_synthetic_ids():
    """Idempotent: f(f(x)) == f(x) for every synthetic raw id.

    Covers all supported marker forms, non-marker identifiers, namespace
    boundaries, and the delimiter-only empty-result inputs. This is a
    portable invariant test — it does NOT read the live production roster.
    """
    from watchdog_catalog.query import strip_free_marker as core_strip_free_marker
    from build_site import strip_free_marker
    # Compatibility re-export: the site builder exposes the SAME function object
    # as the pure query module, never a copied wrapper.
    assert strip_free_marker is core_strip_free_marker
    for mid in SYNTHETIC_IDEMPOTENCE_IDS:
        once = strip_free_marker(mid)
        twice = strip_free_marker(once)
        assert once == twice, f"not idempotent for {mid!r}: {once!r} vs {twice!r}"


@pytest.mark.parametrize("model_id", EMPTY_RESULT_GUARD_INPUTS)
def test_strip_free_marker_never_returns_empty(model_id):
    """Empty-result guard: a delimiter-only input returns its exact original.

    Idempotence alone CANNOT pin this — with the guard removed f(x) == "" and
    f("") == "" is still idempotent. Only the exact non-empty string returned
    distinguishes the guarded helper from a fallback-less one.
    """
    from build_site import strip_free_marker
    stripped = strip_free_marker(model_id)
    assert stripped == model_id, (
        f"empty-result guard returned {stripped!r}, want {model_id!r}"
    )


# ---------- Task 3: matrix rows grouped by display name ----------

# Fixture with multi-variant groups: one model ships on two gateways under
# different free-marker forms; another ships on the SAME gateway under both
# forms (must not double-count in '#'); a third is single-variant (control).
# provider_models provides the `name` field for grouping (simulating gateway
# API responses). Name matches the stripped form so groups merge as expected.
GROUP_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": [
            "vendor-x/poolside-s-2.1:free",      # variant A
            "vendor-x/standalone-1:free",        # single-variant
        ],
        "tokenrouter": [
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
    "provider_models": {
        "nous": {
            "vendor-x/poolside-s-2.1:free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
            "vendor-x/standalone-1:free": {
                "name": "vendor-x/standalone-1",
                "context_length": 131072,
                "architecture": {"input_modalities": ["text"]},
            },
        },
        "tokenrouter": {
            "vendor-x/poolside-s-2.1-free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
        },
        "kilo": {
            "vendor-x/poolside-s-2.1-free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
            "vendor-x/poolside-s-2.1:free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
            "vendor-x/standalone-2:free": {
                "name": "vendor-x/standalone-2",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text", "image"]},
            },
        },
        "openrouter": {
            "vendor-x/standalone-2-free": {
                "name": "vendor-x/standalone-2",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text", "image"]},
            },
        },
    },
    "stale_providers": [],
}


def _row_count(html):
    """Count name-rows across all tbodies (each group has its own <tbody>)."""
    return len(re.findall(r'<tr class="name-row">', html))


def _tfoot_numbers(html):
    """Return the per-gateway <tfoot> numbers in DISPLAY_ORDER."""
    tfoot = html.split("<tfoot>", 1)[1].split("</tfoot>", 1)[0]
    return [int(n) for n in re.findall(r'class="n">(\d+)<', tfoot)]


def _row_for(html, stripped_name):
    """Return the <tr> for `stripped_name` from the rendered tbody."""
    for tbody in re.findall(r"<tbody>(.*?)</tbody>", html, re.S):
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
    """Task 3: '#' column counts DISTINCT gateways reached, not total variants.
    poolside-s-2.1 has variant A on nous, variant B on tokenrouter and kilo ->
    distinct gateways = {nous, tokenrouter, kilo} -> # must be 3."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    row = _row_for(html, "vendor-x/poolside-s-2.1")
    assert row is not None, "no row for vendor-x/poolside-s-2.1"
    # '#' is the first <td class="n"> after the <th>. Task 4 wraps the
    # name in a <label> for the expand toggle, so allow anything between
    # <th> and <td class="n">. Match full <th>...</th> then the # cell.
    m = re.search(r'<th[^>]*>[\s\S]*?</th>\s*<td class="n">(\d+)</td>', row)
    assert m, f"could not find # cell in row: {row!r}"
    assert int(m.group(1)) == 3, (
        f"group's # must equal number of gateways it reaches "
        f"(kilo+openrouter+nous+tokenrouter = up to 4, but poolside-s-2.1 "
        f"is on nous+tokenrouter+kilo = 3); got {m.group(1)}"
    )


def test_grouped_row_dots_on_every_gateway_any_variant_reaches(tmp_path):
    """Task 3: presence on a gateway is true if ANY of the group's raw
    variants is on that gateway. For vendor-x/poolside-s-2.1: variant A is
    on nous, variant B is on tokenrouter and kilo -> dots on nous, tokenrouter, kilo,
    NOT on openrouter. amd and bai have no models in this fixture, so their
    columns are hidden."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    row = _row_for(html, "vendor-x/poolside-s-2.1")
    assert row is not None
    # The row has 4 active gateway cells (nous, tokenrouter, kilo, openrouter).
    # Count yes/no.
    yes = row.count('<td class="yes">')
    no = row.count('<td class="no">')
    assert yes == 3, f"expected 3 yes dots (nous+tokenrouter+kilo), got {yes}"
    assert no == 1, f"expected 1 no cell (openrouter), got {no}"


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
    m = re.search(r'<th[^>]*>[\s\S]*?</th>\s*<td class="n">(\d+)</td>', row)
    assert m, f"could not find # cell in row: {row!r}"
    # nous, tokenrouter, kilo — three distinct gateways reached
    assert int(m.group(1)) == 3, (
        f"kilo has both variants but counts as 1 gateway; # must be 3, got {m.group(1)}"
    )
    # Sanity: only ONE yes-dot in the kilo column for this row.
    assert row.count('<td class="yes">') == 3  # nous, tokenrouter, kilo each contribute one yes


def test_tfoot_totals_stay_raw_per_gateway_counts(tmp_path):
    """Task 3: <tfoot> counts must remain the honest 'ids tracked per
    gateway' number, not collapsed to groups. GROUP_ROSTER's raw per-gw
    counts: nous=2, tokenrouter=1, kilo=3, openrouter=1, amd=0, bai=0.
    Empty providers (amd, bai) are hidden from the matrix."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    nums = _tfoot_numbers(html)
    assert nums == [2, 1, 3, 1], (
        f"tfoot must stay raw per-gateway counts for active gateways only, got {nums}"
    )


def test_published_catalogue_keeps_raw_ids(tmp_path):
    """The public snapshot keeps RAW ids — variants preserved, not collapsed.

    The old raw-roster island is gone, so this pins the same guarantee on the
    versioned public contract: each provider's ``model_ids`` is its own raw
    list, and the page's embedded catalogue is byte-for-byte the same data.
    """
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    catalog = json.loads(
        (tmp_path / "site" / "catalog.json").read_text(encoding="utf-8"))
    # Raw ids are present — variants preserved, not collapsed
    assert "vendor-x/poolside-s-2.1-free" in catalog["providers"]["tokenrouter"]["model_ids"]
    assert "vendor-x/poolside-s-2.1:free" in catalog["providers"]["nous"]["model_ids"]
    assert "vendor-x/poolside-s-2.1-free" in catalog["providers"]["kilo"]["model_ids"]
    assert _typed(json.loads(_script_elements(html)[0][1])) == _typed(catalog)


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
    """Header chip N counts groups ('unique models'); the endpoints chip M
    counts distinct (gateway, raw_id) WIRINGS, not distinct raw ids. An id
    callable at two gateways is two endpoints — that is what a caller can
    actually hit — so the chip must equal the sum of the <tfoot> per-gateway
    totals. GROUP_ROSTER wirings: nous 2 + tokenrouter 1 + kilo 3 +
    openrouter 1 = 7, even though only 5 distinct raw ids exist (poolside
    '-free' spans tokenrouter+kilo and poolside ':free' spans nous+kilo).
    Live roster after this semantics: 35 unique models / 58 endpoints."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    chips = html.split('<div class="chips">', 1)[1].split("</div>", 1)[0]
    assert "<b>3</b> unique models" in chips, (
        f"unique-models chip must count groups (3), got: {chips}"
    )
    assert "<b>7</b> endpoints" in chips, (
        f"endpoints chip must count (gateway,id) wirings (7), got: {chips}"
    )
    # Consistency invariant: endpoints chip == sum of <tfoot> per-gateway
    # totals (both count wirings; the chip previously lied).
    assert "<b>7</b> endpoints" in chips and _tfoot_numbers(html) == [2, 1, 3, 1]
    assert sum(_tfoot_numbers(html)) == 7


def test_meta_copy_says_updated_hourly(tmp_path):
    """Operator copy fix: the header meta line reads ONLY 'updated hourly' —
    no 'last refreshed' stamp, no old 'rebuilt every 1h' wording."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    meta = html.split('<div class="meta">', 1)[1].split("</div>", 1)[0]
    assert meta.strip() == "updated hourly", (
        f"meta must equal 'updated hourly', got: {meta!r}"
    )
    assert "rebuilt every" not in meta
    assert "last refreshed" not in meta


def test_footer_has_github_and_kofi_links(tmp_path):
    """souls.dthen.xyz footer pattern: a centred links row linking GitHub
    then Ko-fi, opening safely in a new tab. The note text below the links
    was deleted by operator copy request — footer is links-only."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    footer = html.split('<footer class="note">', 1)[1].split("</footer>", 1)[0]
    assert 'href="https://github.com/Dthen/free-inference-watchdog"' in footer, (
        f"GitHub link missing from footer: {footer}"
    )
    assert 'href="https://ko-fi.com/dthen"' in footer, (
        f"Ko-fi link missing from footer: {footer}"
    )
    # GitHub first, then Ko-fi, both opening safely in a new tab.
    gh = footer.index("github.com")
    kf = footer.index("ko-fi.com")
    assert gh < kf, "GitHub link must come before Ko-fi"
    for link in re.findall(r"<a\b[^>]*>", footer):
        assert 'target="_blank"' in link and 'rel="noopener"' in link, (
            f"footer link lacks target=_blank rel=noopener: {link}"
        )
    # The note text below the links row is gone entirely.
    assert "ids shown verbatim" not in footer
    assert "rebuilt each tick" not in footer


def test_groups_sorted_alphabetically_by_stripped_name(tmp_path):
    """Task 3: groups sort alphabetically by stripped name. The first body
    row in DISPLAY_ORDER is the alphabetically-first group."""
    _seed_logo(tmp_path)
    proc = _run_builder(GROUP_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    # Each group is in its own <tbody>; collect name-row labels from all
    tbodies = re.findall(r"<tbody>(.*?)</tbody>", html, re.S)
    names = []
    for tbody in tbodies:
        # The name is the LAST span's text: a caret span then the name span
        # (dddb44a wrapped the name in a span for reliable browser tooltips),
        # so the name span must be matched explicitly — an optional generic
        # span group is greedy and swallows the caret, capturing only the
        # space before the name.
        names.extend(re.findall(_NAME_ROW_RE, tbody))
    assert names == sorted(names), (
        f"groups must be alphabetically sorted by stripped name; got {names}"
    )
    assert names == [
        "vendor-x/poolside-s-2.1",
        "vendor-x/standalone-1",
        "vendor-x/standalone-2",
    ]


# Synthetic roster for the rendering guard — multi-provider with shared and
# distinct raw IDs, explicit display-name metadata, and a missing-name fallback.
# This is NOT production data; it tests the builder's grouping behavior.
SYNTHETIC_RENDER_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": [
            "vendor-x/poolside-s-2.1:free",      # variant A
            "vendor-x/standalone-1:free",        # single-variant
        ],
        "tokenrouter": [
            "vendor-x/poolside-s-2.1-free",      # variant B
        ],
        "kilo": [
            "vendor-x/poolside-s-2.1-free",      # variant B (overlap)
            "vendor-x/poolside-s-2.1:free",      # variant A (overlap)
            "vendor-x/no-name-fallback:free",    # no provider_models entry
        ],
    },
    "provider_models": {
        "nous": {
            "vendor-x/poolside-s-2.1:free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
            "vendor-x/standalone-1:free": {
                "name": "vendor-x/standalone-1",
                "context_length": 131072,
                "architecture": {"input_modalities": ["text"]},
            },
        },
        "tokenrouter": {
            "vendor-x/poolside-s-2.1-free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
        },
        "kilo": {
            "vendor-x/poolside-s-2.1-free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
            "vendor-x/poolside-s-2.1:free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
            # vendor-x/no-name-fallback:free has NO provider_models entry
            # -> builder falls back to raw id as group name
        },
    },
    "stale_providers": [],
}

# Expected group labels, stated explicitly and independently of the renderer.
# The builder groups by provider_models `name` field, falling back to raw id.
# poolside-s-2.1 and standalone-1 have explicit names; no-name-fallback has
# none, so its group name is the raw id (with marker).
EXPECTED_SYNTHETIC_GROUPS = [
    "vendor-x/no-name-fallback:free",
    "vendor-x/poolside-s-2.1",
    "vendor-x/standalone-1",
]

# Every fixture raw endpoint (gateway, raw_id) must remain reachable in the
# rendered expansion rows.
EXPECTED_SYNTHETIC_ENDPOINTS = [
    ("nous", "vendor-x/poolside-s-2.1:free"),
    ("nous", "vendor-x/standalone-1:free"),
    ("tokenrouter", "vendor-x/poolside-s-2.1-free"),
    ("kilo", "vendor-x/poolside-s-2.1-free"),
    ("kilo", "vendor-x/poolside-s-2.1:free"),
    ("kilo", "vendor-x/no-name-fallback:free"),
]


def test_synthetic_roster_groups_match_expected_names(tmp_path):
    """Rendering guard: a synthetic multi-provider roster renders one row per
    distinct display name (provider_models `name` or raw-id fallback), and
    every raw endpoint remains reachable in the expansion.

    This test uses ONLY synthetic fixture data — it does NOT read the live
    production roster. It tests existing rendering behaviour, not a new
    model-identity policy.
    """
    _seed_logo(tmp_path)
    proc = _run_builder(SYNTHETIC_RENDER_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)

    # Collect rendered group names from all tbodies
    tbodies = re.findall(r"<tbody>(.*?)</tbody>", html, re.S)
    names = []
    for tbody in tbodies:
        names.extend(re.findall(_NAME_ROW_RE, tbody))

    # The rendered groups must equal the expected sorted group labels
    assert names == EXPECTED_SYNTHETIC_GROUPS, (
        f"synthetic roster: groups must equal sorted unique display names.\n"
        f"  expected ({len(EXPECTED_SYNTHETIC_GROUPS)}): {EXPECTED_SYNTHETIC_GROUPS}\n"
        f"  got      ({len(names)}): {names}"
    )

    # Every fixture raw endpoint must appear in the rendered expansion rows
    all_trs = re.findall(r"<tr(?:\s+[^>]*)?>.*?</tr>", html, re.S)
    expand_rows = [r for r in all_trs if 'class="expand"' in r]
    for gw, raw_id in EXPECTED_SYNTHETIC_ENDPOINTS:
        assert any(
            f'<span class="wire-gw">{gw}</span>' in r and raw_id in r
            for r in expand_rows
        ), f"endpoint ({gw}, {raw_id}) not reachable in rendered expansion"


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
# expansion row. provider_models provides the `name` field for grouping.
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
        "tokenrouter": [
            "vendor-x/poolside-s-2.1-free",      # variant B (different gw)
        ],
        "kilo": [
            "vendor-x/poolside-s-2.1-free",      # variant B (overlap on kilo!)
            "vendor-x/poolside-s-2.1:free",      # variant A (overlap on kilo!)
        ],
    },
    "provider_models": {
        "nous": {
            "vendor-x/poolside-s-2.1:free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
            "vendor-x/standalone-1:free": {
                "name": "vendor-x/standalone-1",
                "context_length": 131072,
                "architecture": {"input_modalities": ["text"]},
            },
        },
        "openrouter": {
            "vendor-x/standalone-2-free": {
                "name": "vendor-x/standalone-2",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text", "image"]},
            },
        },
        "tokenrouter": {
            "vendor-x/poolside-s-2.1-free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
        },
        "kilo": {
            "vendor-x/poolside-s-2.1-free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
            "vendor-x/poolside-s-2.1:free": {
                "name": "vendor-x/poolside-s-2.1",
                "context_length": 262144,
                "architecture": {"input_modalities": ["text"]},
            },
        },
    },
    "stale_providers": [],
}


def _all_trs_in_tbody(html):
    """All <tr>s across every <tbody> (each group has its own <tbody>)."""
    trs = []
    for tbody in re.findall(r"<tbody>(.*?)</tbody>", html, re.S):
        trs.extend(re.findall(r"<tr(?:\s+[^>]*)?>.*?</tr>", tbody, re.S))
    return trs


def _all_trs_for(html, stripped_name):
    """All <tr>s that belong to the expandable group for `stripped_name`
    (the name row + any hidden expansion rows)."""
    # Each group is in its own <tbody>
    for tbody in re.findall(r"<tbody>(.*?)</tbody>", html, re.S):
        rows = re.findall(r"<tr(?:\s+[^>]*)?>.*?</tr>", tbody, re.S)
        out, take = [], False
        for row in rows:
            m = re.match(_NAME_ROW_RE, row)
            if m:
                take = (m.group(1) == stripped_name)
                if take:
                    out.append(row)
            elif take and 'class="expand"' in row:
                out.append(row)
        if out:
            return out
    return []


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


def test_expand_no_script_beyond_public_islands(tmp_path):
    """Exactly the TWO inert public JSON islands; the toggle stays CSS-only.

    The old single raw roster island is gone: the page carries the catalogue
    and the shared tool contract, both ``application/json`` and inert, and no
    executable script anywhere.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        islands = _script_elements(html)
        assert [attrs.get("id") for attrs, _ in islands] == list(_ISLAND_IDS), (
            f"expected exactly the two public islands, found {islands}")
        assert [attrs.get("type") for attrs, _ in islands] == [
            "application/json", "application/json"]
        assert 'id="roster-data"' not in html


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
    chat-completions URL. The wiring mapping is imported from config_loader,
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
        from config_loader import GATEWAY_WIRING
        # We expect variant rows for: poolside:free on nous, poolside-free
        # on tokenrouter, poolside:free on kilo, poolside-free on kilo, standalone-1
        # on nous, standalone-2 on openrouter — 6 expansion rows total.
        # Each MUST mention its gateway's chat_completions_url (every
        # gateway in GATEWAY_WIRING now carries a static URL).
        expand_rows = [r for r in _all_trs_in_tbody(html) if 'class="expand"' in r]
        assert len(expand_rows) == 6, (
            f"expected 6 expansion rows, got {len(expand_rows)}"
        )
        # Each expansion row has a <td class="wire-url"> containing the URL
        # text. The cell class is part of the visible contract — readers
        # can copy the URL straight out of the page.
        for gw in ("tokenrouter", "kilo", "openrouter"):
            url = GATEWAY_WIRING[gw]["chat_completions_url"]
            assert any(url in r for r in expand_rows), (
                f"{gw} URL {url!r} missing from every expansion row"
            )


def test_expand_each_variant_row_has_api_type_and_no_auth(tmp_path):
    """Each variant <tr class='expand'> contains that gateway's api_type —
    the reader sees at a glance that it is OpenAI-shaped — and NO auth
    field: the operator dropped 'Bearer <your API key>' from the wiring
    displays because it read the same for every gateway (and its length
    overflowed the expand row)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        from config_loader import GATEWAY_WIRING
        # The auth field is gone from the wiring contract itself...
        for w in GATEWAY_WIRING.values():
            assert "auth" not in w
        # ...and never reaches the rendered page.
        assert "wire-auth" not in html, "wire-auth span back in the page"
        assert "Bearer" not in html, "Bearer auth prose back in the page"
        # Each group is in its own <tbody>; collect expand rows from all
        all_trs = re.findall(r"<tr(?:\s+[^>]*)?>.*?</tr>", html, re.S)
        expand_rows = [r for r in all_trs if 'class="expand"' in r]
        # For every gateway that appears in EXPAND_ROSTER, at least one
        # expansion row must mention that gateway's api_type.
        for gw in ("nous", "tokenrouter", "kilo", "openrouter"):
            w = GATEWAY_WIRING[gw]
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
        # Each group is now in its own <tbody>, so count total <tr>s across
        # all tbodies. 3 name rows + 6 expansion rows = 9 total.
        all_trs = re.findall(r"<tr(?:\s+[^>]*)?>.*?</tr>", html, re.S)
        name_rows = [r for r in all_trs if '<tr class="name-row">' in r]
        expand_rows = [r for r in all_trs if 'class="expand"' in r]
        assert len(name_rows) == 3, f"expected 3 name rows, got {len(name_rows)}"
        assert len(expand_rows) == 6, f"expected 6 expand rows, got {len(expand_rows)}"
        # Each name row must have a hidden <input type="checkbox"> + <label>
        # pair. The label is the user-facing click target.
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


# ---------- Gateway column + expand-row overflow (operator fixes, 2026-09-13) ----------
#
# Retargeted from nim to bai on 2026-09-28: nim.json carries enabled:false
# and no longer loads into the registry, so DISPLAY_ORDER has no nim column
# to render. The coverage being protected — "a gateway in the roster gets a
# column, carries its wiring URL in the expand row, and links its signup_url
# in the column head" — is about ANY gateway, not nim.

BAI_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": ["vendor-z/zero-priced-model"],
        "bai": ["meta/llama-3.3-70b-instruct"],
    },
    "stale_providers": [],
}


def test_bai_gets_a_column_and_wiring_url(tmp_path):
    """A roster carrying bai renders a bai column, and its expand row shows
    the B.AI chat-completions URL — the full catalog joins every
    user-visible surface with zero per-gateway special-casing."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(BAI_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        # Header upgrade: bai carries a signup link in its column head.
        assert ">bai</a></th>" in html, "bai column header missing"
        assert 'href="https://b.ai/"' in html, (
            "bai header not linked to its signup URL")
        assert "api.b.ai/v1/chat/completions" in html, (
            "bai chat-completions URL missing from the expand row")
        assert "Bearer" not in html


def test_expand_wire_cell_wraps_long_urls(tmp_path):
    """Operator-reported overflow: long wiring URLs (e.g. the NIM
    chat-completions endpoint) must never spill out of the expand-row cell.
    The grid gives the text columns a minmax(0, 1fr) floor (a bare 1fr track
    cannot shrink below its content) and every text span breaks long words;
    both properties are pinned."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed_logo(tmp)
        proc = _run_builder(EXPAND_ROSTER, tmp)
        assert proc.returncode == 0, proc.stderr
        html = _build_html(tmp)
        wire_css = [ln for ln in html.splitlines() if ".wire {" in ln]
        assert wire_css, "no .wire grid CSS rule"
        assert "minmax(0," in wire_css[0], (
            "wire grid columns must be minmax(0,1fr) so long URLs can shrink")
        for cls in (".wire-gw", ".wire-id", ".wire-url", ".wire-api"):
            rule = [ln for ln in html.splitlines() if ".wire" in ln and cls in ln]
            assert rule, f"missing CSS rule for {cls}"
            assert ("word-break" in rule[0]) or ("overflow-wrap" in rule[0]), (
                f"{cls} must break long words/URLs to stay inside the cell")


# ---------- Gateway header upgrade: signup links + limits tooltips -------
#
# Design (operator-locked): each gateway column head is
#   <th><a href="{signup_url}" target="_blank" rel="noopener" title="{limits_note}">{gateway}</a></th>
# signup_url/limits_note are OPTIONAL fields in providers/*.json (operator-
# maintained facts, same file as everything else about a gateway). Missing
# signup_url (or a non-https scheme) => plain <th>name</th>. Missing
# limits_note => anchor without title attr. Config load failure => empty map,
# plain headers, exit 0 (site build failure must never page).

HEADER_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": ["vendor-z/zero-priced-model", "vendor-g/model-7:free"],
        # bai, not nim: nim.json carries enabled:false, so DISPLAY_ORDER has
        # no nim column. bai stays in the registry and keeps the "gateway with
        # no header meta renders a bare <th>" coverage alive.
        "bai": ["meta/llama-3.3-70b-instruct"],
    },
    "stale_providers": [],
}

# Exact field values (VERIFIED 2026-09-13 by a dedicated URL-research pass
# with keyless HTTP + browser-render evidence). Pinning them here prevents
# silent drift of operator-maintained facts. /signup-style URLs were checked
# and REJECTED as 200-but-404 SPA traps; these are the canonical entries.
EXPECTED_PROVIDER_FIELDS = {
    "nous": {
        "signup_url": "https://portal.nousresearch.com/",
        "limits_note": "Per-token rate limits, shared across all models (as of 2026-09)",
    },
    "tokenrouter": {
        "signup_url": "https://www.tokenrouter.com/login",
        "limits_note": "RPM/RPD unpublished; concurrency by balance: $0-10 -> 5 concurrent (as of 2026-09-13)",
    },
    "kilo": {
        "signup_url": "https://app.kilo.ai/users/sign_in",
        "limits_note": "200 req/hr per IP across all free models combined (published, as of 2026-09-13)",
    },
    "openrouter": {
        "signup_url": "https://openrouter.ai/sign-in",
        "limits_note": "20 RPM / 200 requests/day on free ids (as of 2026-09)",
    },
    "amd": {
        "signup_url": "https://developer.amd.com.cn/register",
        "limits_note": "~20 RPM, ~$10/day compute points (as of 2026-09)",
    },
    "bai": {
        # Homepage is flagged low-confidence by the researcher but is the
        # real entry point — keep it.
        "signup_url": "https://b.ai/",
        "limits_note": "Undocumented; burst-kills above ~20 RPM; per-model RPM shared globally (as of 2026-09-13)",
    },
    "nim": {
        "signup_url": "https://build.nvidia.com/?modal=signin",
        "limits_note": "No published limits; ~40 RPM community baseline, per-model, not SLA (as of 2026-09-13)",
    },
}


def _render(roster, meta):
    """Render via the real build_site.render_page with an explicit
    gateway -> {signup_url, limits_note} map (the render_page 3rd arg)."""
    import build_site
    html = build_site.render_page(roster, "ZmFrZWxvZ28=", meta)
    return html.split("<thead>", 1)[1].split("</thead>", 1)[0]


def test_header_links_anchor_with_tooltip():
    """signup_url + limits_note present -> anchored th, exact href/target/rel/
    title, link text the bare gateway name."""
    head = _render(HEADER_ROSTER, {"nous": {
        "signup_url": "https://portal.nousresearch.com/",
        "limits_note": "Per-token rate limits, shared across all models (as of 2026-09)"}})
    assert head.count("<th>") == 4  # model id, #, nous, bai (bai plain: no meta entry)
    assert ('<th><a href="https://portal.nousresearch.com/" target="_blank" '
            'rel="noopener" title="Per-token rate limits, shared across all models '
            '(as of 2026-09)">nous</a></th>') in head
    assert "<th>bai</th>" in head  # absent from map -> plain th


def test_header_link_text_is_bare_name_and_title_is_escaped():
    """The bare gateway name lives in the visible link text (per-gateway
    counts moved to the tfoot — neater at the bottom) and the title is
    html-escaped through the existing escape()."""
    head = _render(HEADER_ROSTER, {"nous": {
        "signup_url": "https://x.example/signup",
        "limits_note": 'note with "quote" & <angle>'}})
    anchor = re.search(r'<th><a\b[^>]*>(.*?)</a></th>', head).group(1)
    assert anchor == "nous"
    assert 'title="note with &quot;quote&quot; &amp; &lt;angle&gt;"' in head


def test_header_missing_signup_url_plain_th():
    """No signup_url -> plain th (no anchor)."""
    head = _render(HEADER_ROSTER, {"nous": {"limits_note": "no url here"}})
    assert "<th>nous</th>" in head
    assert "<a " not in head


def test_header_missing_limits_note_anchor_without_title():
    """signup_url but no limits_note -> anchor present, no title attribute."""
    head = _render(HEADER_ROSTER, {"nous": {"signup_url": "https://x.example/signup"}})
    assert 'href="https://x.example/signup"' in head
    assert "title=" not in head
    assert ">nous</a>" in head


@pytest.mark.parametrize("bad", [
    "http://portal.example.com/",
    "javascript:alert(1)",
    "portal.example.com",
    "",
    "HTTPS:/x",
])
def test_header_scheme_guard_rejects_non_https(bad):
    """Non-https signup_url values (http, javascript:, relative, empty) are
    treated as missing — the XSS/scheme guard runs at render time."""
    head = _render(HEADER_ROSTER, {"nous": {"signup_url": bad,
                                            "limits_note": "note"}})
    assert "<th>nous</th>" in head, f"non-https {bad!r} must degrade to plain th"
    assert "<a " not in head


def test_header_scheme_guard_accepts_uppercase_https():
    """The https:// check is case-insensitive; the value renders verbatim."""
    head = _render(HEADER_ROSTER, {"nous": {"signup_url": "HTTPS://PORTAL.EXAMPLE.COM/"}})
    assert 'href="HTTPS://PORTAL.EXAMPLE.COM/"' in head
    assert ">nous</a>" in head


def test_config_load_failure_degrades_to_plain_headers(tmp_path, monkeypatch):
    """If loading provider configs raises for ANY reason the map is empty,
    headers render plain, and the builder still completes with
    exit 0 and a written site — site-build failure must never page."""
    import build_site
    _seed_logo(tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "roster.json").write_text(json.dumps(HEADER_ROSTER))

    def boom():
        raise RuntimeError("simulated config catastrophe")
    monkeypatch.setattr(build_site.config_loader, "load_configs", boom)
    assert build_site.build_provider_header_meta() == {}
    build_site.main(["--root", str(tmp_path)])  # must NOT raise
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    head = html.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert "<th>nous</th>" in head and "<th>bai</th>" in head
    assert "<a " not in head


def test_render_page_header_map_is_optional_argument():
    """Back-compat: render_page(roster, logo) without the 3rd arg must work
    and derive the map from the repo's real configs (nous carries signup_url
    in providers/nous.json)."""
    import build_site
    html = build_site.render_page(HEADER_ROSTER, "ZmFrZWxvZ28=")
    assert '>nous</a>' in html


def test_real_provider_configs_carry_verified_fields():
    """All seven providers/*.json carry BOTH fields; values pinned
    per-gateway by test_provider_config_field_values below (VERIFIED
    2026-09-13 by a dedicated URL-research pass; /signup-style URLs were
    checked and REJECTED as 200-but-404 SPA traps — these are canonical)."""
    for key, fields in EXPECTED_PROVIDER_FIELDS.items():
        cfg = json.loads((REPO / "providers" / f"{key}.json").read_text(encoding="utf-8"))
        assert {"signup_url", "limits_note"} <= set(cfg), f"{key}.json missing fields"


@pytest.mark.parametrize("key", sorted(EXPECTED_PROVIDER_FIELDS))
def test_provider_config_field_values(key):
    """Per-gateway pin of signup_url/limits_note — prevents silent drift of
    operator-maintained facts (VERIFIED 2026-09-13)."""
    cfg = json.loads((REPO / "providers" / f"{key}.json").read_text(encoding="utf-8"))
    for field, want in EXPECTED_PROVIDER_FIELDS[key].items():
        assert cfg.get(field) == want, f"providers/{key}.json {field} drifted"


def test_cli_headers_link_real_provider_fields(tmp_path):
    """End-to-end through the real CLI + real providers/*.json: both fields
    produce the fully-formed anchor."""
    _seed_logo(tmp_path)
    proc = _run_builder(HEADER_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    assert ('<th><a href="https://portal.nousresearch.com/" target="_blank" '
            'rel="noopener" title="Per-token rate limits, shared across all models '
            '(as of 2026-09)">nous</a></th>') in html
    assert '<th><a href="https://b.ai/"' in html
    assert ">bai</a></th>" in html


def test_footer_links_use_class_not_inline_style(tmp_path):
    """Reviewer minor folded in: footer links row is class="footer-links"
    styled from the <style> block — the file convention is zero inline
    styles (static HTML stays; only the styling location moves)."""
    _seed_logo(tmp_path)
    proc = _run_builder(HEADER_ROSTER, tmp_path)
    assert proc.returncode == 0, proc.stderr
    html = _build_html(tmp_path)
    assert '<div class="footer-links">' in html
    assert 'style="display:flex' not in html
    rule = [ln for ln in html.splitlines() if ".footer-links" in ln]
    assert rule and "display:flex" in rule[0]


def test_dead_raw_count_key_removed(tmp_path):
    """Reviewer minor folded in: build_groups' never-read raw_count key is
    gone from both code and docstring."""
    src = BUILDER.read_text(encoding="utf-8")
    assert "raw_count" not in src


# ---------- Unit 4c: snapshot-authoritative public matrix rendering ----------
#
# The public route renders an already-VALIDATED snapshot through the SAME
# template the legacy adapter uses. Two synthetic-only groups pin it:
#   1. snapshot authority: provider order, per-provider wiring and per-pair
#      projected metadata come from the snapshot, never module globals; the
#      public data-script slot carries ONLY its two inert JSON islands (no
#      roster island) and the visible matrix is the page minus those islands.
#   2. one shared template: public HTML equals the legacy render_page output
#      minus ONLY its roster-data island, and a capture -> build -> render
#      round trip leaks none of the excluded operational fields.
# No production roster, provider config, site artifact or network is touched.

PUBLIC_RENDER_LOGO = "ZmFrZWxvZ28="

# Authoritative display order DIFFERS from the poisoned global order below and
# from the reversed insertion order used for the snapshot's providers map.
PUBLIC_PROVIDER_ORDER = ["synthetic-beta", "synthetic-alpha", "synthetic-empty"]
PUBLIC_LONG_RUN = "acme/token-" + "0" * 59 + "1:free"
PUBLIC_TOKEN_IDS = [
    "acme/token-2:free",
    "acme/token-02:free",
    "acme/token-10-free",
    PUBLIC_LONG_RUN,
    "acme/token-\u0662:free",  # Arabic-Indic digit two: TEXT token, not a digit run
]


def _public_snapshot(providers, provider_models):
    """Build a REAL validated snapshot through the accepted capture/build path."""
    from catalog_export import build_snapshot, capture_registry

    providers = {key: list(ids) for key, ids in providers.items()}
    wiring = {
        key: {
            "chat_completions_url": (
                f"https://{key}.synthetic.example/v1/chat/completions"),
            "api_type": "openai-synthetic",
        }
        for key in providers
    }
    configs = {key: {"name": key} for key in providers}
    roster = {
        "tick_epoch": 1787721434,
        "providers": providers,
        "provider_models": provider_models,
        "stale_providers": [],
    }
    return build_snapshot(capture_registry(configs, wiring), roster,
                          cadence_s=3600)


def _resnapshot_with_map_order(snapshot, map_order):
    """Reseal a deep copy whose providers MAP insertion order differs from
    provider_order -- valid, because only set equality is constrained."""
    import copy

    from watchdog_catalog import catalog

    body = copy.deepcopy(snapshot)
    body.pop("snapshot_id")
    body["providers"] = {key: body["providers"][key] for key in map_order}
    body["snapshot_id"] = catalog.snapshot_hash(body)
    return catalog.validate_snapshot(body)


def _public_wires(html):
    """Every (gateway, raw id, url, api type) wiring row, in document order."""
    wires = []
    for tr in re.findall(r'<tr class="expand">.*?</tr>', html, re.S):
        wires.append((
            re.search(r'<span class="wire-gw">([^<]*)</span>', tr).group(1),
            re.search(r'<span class="wire-id">([^<]*)</span>', tr).group(1),
            re.search(r'<span class="wire-url">([^<]*)</span>', tr).group(1),
            re.search(r'<span class="wire-api">([^<]*)</span>', tr).group(1),
        ))
    return wires


def _thead_columns(html):
    head = html.split("<thead>", 1)[1].split("</thead>", 1)[0]
    return [c.strip() for c in re.findall(r"<th>(?:<a[^>]*>)?([^<(]*)", head)]


# ---------- Unit 4d: shared public JSON islands (catalogue + tools) --------
#
# The public route carries exactly TWO inline inert JSON elements, in order:
# `catalog-data` (the complete public snapshot) then `tool-contract` (the
# supplied shared tool array). These helpers read them back the way a browser
# does — one real HTML script-data element each, not a regex that stops
# silently at an injected end tag — and compare values with their TYPES
# intact, because ordinary Python equality conflates 1/1.0/True.

TOOLS_RESOURCE = REPO / "watchdog_catalog" / "tools.json"

_TOOL_NAMES = (
    "list_free_models", "get_model", "list_endpoints", "watchdog_status",
    "search_models",
)

_ISLAND_IDS = ("catalog-data", "tool-contract")


def _load_tool_contract():
    """Decode the ACTUAL unchanged shared tool resource (a bare array)."""
    return json.loads(TOOLS_RESOURCE.read_text(encoding="utf-8"))


def _script_elements(html):
    """All <script> elements as (attrs dict, EXACT inner text), in order."""
    from html.parser import HTMLParser

    class _Collector(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.scripts = []
            self.open = None

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                assert self.open is None, "nested/unterminated <script> element"
                self.open = [dict(attrs), []]

        def handle_data(self, data):
            if self.open is not None:
                self.open[1].append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self.open is not None:
                self.scripts.append((self.open[0], "".join(self.open[1])))
                self.open = None

    collector = _Collector()
    collector.feed(html)
    collector.close()
    assert collector.open is None, "unterminated <script> element"
    return collector.scripts


_ISLANDS_RE = (
    r'<script type="application/json" id="catalog-data">.*?</script>'
    r'<script type="application/json" id="tool-contract">.*?</script>'
)


def _strip_public_islands(html):
    """Drop EXACTLY the two expected inert islands, asserting both exist once."""
    for island_id in _ISLAND_IDS:
        assert html.count(f'<script type="application/json" id="{island_id}">') == 1
    stripped, count = re.subn(_ISLANDS_RE, "", html, count=1, flags=re.S)
    assert count == 1, "expected the two islands adjacent, in order"
    return stripped


def _typed(value):
    """A type-carrying view of one JSON value (1 / 1.0 / True stay distinct)."""
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if value is None:
        return ("null",)
    if isinstance(value, list):
        return ("list", tuple(_typed(item) for item in value))
    if isinstance(value, dict):
        return ("map", tuple(sorted((key, _typed(item))
                                    for key, item in value.items())))
    raise AssertionError("non-JSON value in island: %r" % type(value).__name__)


def test_public_snapshot_renderer_is_snapshot_authoritative():
    """Public matrix derives order/wiring/metadata from the snapshot only.

    Builds two actual validated synthetic snapshots (populated + true empty),
    poisons the module globals and the legacy adapter with matching keys but
    WRONG values, and pins literal output: header order, every raw endpoint
    identity + wiring, portable natural order (ASCII/leading-zero/long-run/
    non-ASCII digit controls), per-gateway footer counts, endpoint totals and
    the `#` distinct-gateway count. Also pins the PUBLIC ISLANDS: exactly the
    two expected inert JSON elements (ids, type, order, complete payloads), a
    whole-value catalogue equal to the SAME snapshot's export text and the
    supplied tool array, and no roster island.
    """
    import copy

    import build_site
    from catalog_export import serialize_snapshot

    tool_contract = _load_tool_contract()

    beta_models = {
        "acme/token-2:free": {
            "name": "acme/token", "context_length": 262144,
            "architecture": {"input_modalities": ["text"]}},
        "acme/token-02:free": {
            "name": "acme/token", "context_length": 262144,
            "architecture": {"input_modalities": ["text"]}},
        "acme/token-10-free": {
            "name": "acme/token", "context_length": 262144,
            "architecture": {"input_modalities": ["text"]}},
        PUBLIC_LONG_RUN: {
            # Known context 0 must still render a tooltip, never "unknown".
            "name": "acme/token", "context_length": 0,
            "architecture": {"input_modalities": ["text"]}},
        "acme/token-\u0662:free": {
            # Known EMPTY modalities must not crash the tooltip builder.
            "name": "acme/token", "context_length": 262144,
            "architecture": {"input_modalities": []}},
        # No metadata entry at all -> projected name falls back to the raw id.
    }
    alpha_models = {
        "acme/token-2:free": {
            "name": "acme/token", "context_length": 262144,
            "architecture": {"input_modalities": ["text"]}},
        "acme/alpha-only-free": {
            "name": "acme/alpha-only", "context_length": 131072,
            "architecture": {"input_modalities": ["text", "image"]}},
    }
    snapshot = _public_snapshot(
        {
            "synthetic-beta": PUBLIC_TOKEN_IDS + ["acme/only-beta-free"],
            "synthetic-alpha": ["acme/token-2:free", "acme/alpha-only-free"],
            "synthetic-empty": [],
        },
        {"synthetic-beta": beta_models, "synthetic-alpha": alpha_models},
    )
    assert snapshot["provider_order"] == PUBLIC_PROVIDER_ORDER
    # Same snapshot, providers MAP inserted in a different order: rendering
    # must follow provider_order, not dict insertion order.
    snapshot = _resnapshot_with_map_order(
        snapshot, ["synthetic-empty", "synthetic-alpha", "synthetic-beta"])

    header_meta = {
        "synthetic-beta": {
            "signup_url": "https://beta.synthetic.example/signup",
            "limits_note": 'beta "note" & <angle>',
        },
        # Non-https must degrade to a plain <th> (scheme guard).
        "synthetic-alpha": {
            "signup_url": "javascript:alert(1)", "limits_note": "ignored"},
    }

    saved_order = build_site.DISPLAY_ORDER
    saved_wiring = build_site.GATEWAY_WIRING
    saved_meta = build_site.build_provider_header_meta
    saved_render_page = build_site.render_page
    saved_load_configs = build_site.config_loader.load_configs

    def _forbidden(*args, **kwargs):
        raise AssertionError("public renderer consulted legacy configuration")

    def _thead(html):
        return html.split("<thead>", 1)[1].split("</thead>", 1)[0]

    try:
        # Matching keys, WRONG order / URLs / api types: a wrong source must
        # produce a meaningful literal mismatch, never a KeyError.
        build_site.DISPLAY_ORDER = [
            "synthetic-alpha", "synthetic-empty", "synthetic-beta"]
        build_site.GATEWAY_WIRING = {
            key: {
                "chat_completions_url":
                    f"https://poisoned.example/WRONG-{key}",
                "api_type": "WRONG-api-type",
            }
            for key in PUBLIC_PROVIDER_ORDER
        }
        build_site.build_provider_header_meta = _forbidden
        build_site.render_page = _forbidden
        build_site.config_loader.load_configs = _forbidden

        snapshot_before = copy.deepcopy(snapshot)
        header_before = copy.deepcopy(header_meta)

        html = build_site.render_catalog_page(
            snapshot, PUBLIC_RENDER_LOGO, header_meta,
            tool_contract=tool_contract)
        assert html == build_site.render_catalog_page(
            snapshot, PUBLIC_RENDER_LOGO, header_meta,
            tool_contract=tool_contract), "output must be deterministic"
        assert snapshot == snapshot_before, "snapshot was mutated by rendering"
        assert header_meta == header_before, "header map was mutated by rendering"

        # The matrix/visible-document bytes are the FULL page minus ONLY the
        # two inert islands, so every matrix assertion below is unaffected by
        # their payload text.
        matrix_html = _strip_public_islands(html)

        # --- snapshot-authoritative column order / headers -----------------
        assert _thead_columns(matrix_html) == [
            "model", "#", "synthetic-beta", "synthetic-alpha"], _thead(matrix_html)
        head = _thead(matrix_html)
        assert ('<th><a href="https://beta.synthetic.example/signup" '
                'target="_blank" rel="noopener" title="beta &quot;note&quot; '
                '&amp; &lt;angle&gt;">synthetic-beta</a></th>') in head
        assert "<th>synthetic-alpha</th>" in head
        # Scheme guard: a javascript: signup_url is never linked or emitted.
        assert "javascript:" not in matrix_html
        # Zero-ID provider is hidden: no column, no chip inflation.
        assert "synthetic-empty" not in matrix_html

        # --- chips, footer totals, distinct-gateway '#' --------------------
        chips = matrix_html.split('<div class="chips">', 1)[1].split("</div>", 1)[0]
        assert "<b>3</b> unique models" in chips, chips   # alpha-only + only-beta + token
        assert "<b>8</b> endpoints" in chips, chips       # 6 beta + 2 alpha wirings
        assert "<b>2</b> gateways" in chips, chips        # empty provider hidden
        assert _tfoot_numbers(matrix_html) == [6, 2]

        assert _row_count(matrix_html) == 3
        names = []
        for tbody in re.findall(r"<tbody>(.*?)</tbody>", matrix_html, re.S):
            names.extend(re.findall(_NAME_ROW_RE, tbody))
        assert names == ["acme/alpha-only", "acme/only-beta-free", "acme/token"]

        expected_hashes = {
            "acme/alpha-only": "1",
            "acme/only-beta-free": "1",
            "acme/token": "2",
        }
        for name, want in expected_hashes.items():
            row = _row_for(matrix_html, name)
            m = re.search(r'<th[^>]*>[\s\S]*?</th>\s*<td class="n">(\d+)</td>',
                          row)
            assert m and m.group(1) == want, (name, row)

        # --- every exact raw endpoint identity + per-provider wiring --------
        expected_wires = [
            ("synthetic-beta", raw_id,
             "https://synthetic-beta.synthetic.example/v1/chat/completions",
             "openai-synthetic")
            for raw_id in PUBLIC_TOKEN_IDS + ["acme/only-beta-free"]
        ] + [
            ("synthetic-alpha", "acme/token-2:free",
             "https://synthetic-alpha.synthetic.example/v1/chat/completions",
             "openai-synthetic"),
            ("synthetic-alpha", "acme/alpha-only-free",
             "https://synthetic-alpha.synthetic.example/v1/chat/completions",
             "openai-synthetic"),
        ]
        wires = _public_wires(matrix_html)
        assert len(wires) == 8, wires
        assert sorted(wires) == sorted(expected_wires)
        assert "poisoned.example" not in matrix_html
        assert "WRONG-api-type" not in matrix_html

        # --- portable natural order inside the shared group ----------------
        token_body = next(
            body for body in re.findall(r"<tbody>(.*?)</tbody>", matrix_html, re.S)
            if re.search(_NAME_ROW_RE, body).group(1) == "acme/token")
        assert re.findall(r'<span class="wire-id">([^<]*)</span>',
                          token_body) == PUBLIC_TOKEN_IDS + [
                              "acme/token-2:free"]  # alpha variant last

        # --- tooltip metadata: known 0 rendered, unknown stays absent ------
        assert "Context length: 0 (synthetic-beta)" in matrix_html, "known 0 dropped"
        assert "Input modalities: text" in matrix_html
        only_beta_row = _row_for(matrix_html, "acme/only-beta-free")
        assert "Context length" not in only_beta_row
        assert "Input modalities" not in only_beta_row

        # --- preserved presentation: CSS-only expansion, colspan, logo -----
        assert "type:checkbox" not in matrix_html  # no stray inline JS/type confusion
        assert 'type="checkbox"' in matrix_html
        assert '<label for="row-0">' in matrix_html
        assert 'colspan="4"' in matrix_html              # 2 + 2 active gateways
        assert "tbody tr:has(input.row-expand:checked) ~ tr.expand" in matrix_html
        assert "--nord0:#2e3440" in matrix_html
        assert ('src="data:image/png;base64,' + PUBLIC_RENDER_LOGO + '"') in matrix_html
        assert 'href="https://github.com/Dthen/free-inference-watchdog"' in matrix_html
        assert 'href="https://ko-fi.com/dthen"' in matrix_html

        # --- the two inert islands: ids, type, order, complete payloads ----
        islands = _script_elements(html)
        assert [attrs.get("id") for attrs, _ in islands] == list(_ISLAND_IDS)
        assert [attrs.get("type") for attrs, _ in islands] == [
            "application/json", "application/json"]
        assert all(attrs.get("src") is None for attrs, _ in islands)
        assert 'id="roster-data"' not in html
        assert "<script" not in matrix_html
        assert _typed(json.loads(islands[0][1])) == _typed(
            json.loads(serialize_snapshot(snapshot)))
        assert _typed(json.loads(islands[1][1])) == _typed(tool_contract)

        # --- default headers are plain and I/O-free ------------------------
        default_html = build_site.render_catalog_page(
            snapshot, PUBLIC_RENDER_LOGO, tool_contract=tool_contract)
        assert "<a " not in _thead(default_html)
        assert _thead_columns(default_html) == [
            "model", "#", "synthetic-beta", "synthetic-alpha"]
        assert [attrs.get("id") for attrs, _ in _script_elements(default_html)] \
            == list(_ISLAND_IDS)

        # --- true empty registry stays a valid, distinguishable page -------
        empty_html = build_site.render_catalog_page(
            _public_snapshot({}, {}), PUBLIC_RENDER_LOGO,
            tool_contract=tool_contract)
        assert _thead_columns(empty_html) == ["model", "#"]
        assert 'class="name-row"' not in empty_html
        empty_chips = empty_html.split('<div class="chips">', 1)[1].split(
            "</div>", 1)[0]
        assert "<b>0</b> unique models" in empty_chips
        assert "<b>0</b> endpoints" in empty_chips
        assert "<b>0</b> gateways" in empty_chips
        empty_matrix = _strip_public_islands(empty_html)
        assert "<script" not in empty_matrix
        empty_islands = _script_elements(empty_html)
        assert [attrs.get("id") for attrs, _ in empty_islands] == list(_ISLAND_IDS)
        assert json.loads(empty_islands[1][1]) == tool_contract
    finally:
        build_site.DISPLAY_ORDER = saved_order
        build_site.GATEWAY_WIRING = saved_wiring
        build_site.build_provider_header_meta = saved_meta
        build_site.render_page = saved_render_page
        build_site.config_loader.load_configs = saved_load_configs


def test_shared_template_parity_and_public_html_is_secret_safe():
    """One template: public HTML == legacy HTML minus ONLY the roster island.

    Also drives real capture -> build_snapshot -> render_catalog_page with
    genuine token/auth/local-path/pending/upstream-unknown sentinels in
    EXCLUDED fields, asserting none reach the HTML (islands included) or the
    external export JSON, while retained public name/context/modality/wiring
    strings do reach the decoded island data, hostile visible/attribute text
    stays escaped, and no input is mutated.
    """
    import copy
    from html.parser import HTMLParser

    import build_site
    from catalog_export import build_snapshot, capture_registry, serialize_snapshot
    from config_loader import GATEWAY_WIRING

    tool_contract = _load_tool_contract()

    # ---- (a) legacy parity through the shared template -------------------
    order = ["nous", "tokenrouter", "kilo", "openrouter"]
    legacy_roster = copy.deepcopy(EXPAND_ROSTER)
    parity_header_meta = {"nous": {
        "signup_url": "https://nous.synthetic.example/signup",
        "limits_note": 'nous "note" & <synthetic>'}}
    parity_records = capture_registry(
        {key: {"name": key} for key in order},
        {key: {"chat_completions_url":
               GATEWAY_WIRING[key]["chat_completions_url"],
               "api_type": GATEWAY_WIRING[key]["api_type"]}
         for key in order},
    )
    parity_snapshot = build_snapshot(
        parity_records,
        {
            "tick_epoch": 1787721434,
            "providers": {key: list(legacy_roster["providers"][key])
                          for key in order},
            "provider_models": copy.deepcopy(legacy_roster["provider_models"]),
            "stale_providers": [],
        },
        cadence_s=3600,
    )
    legacy_html = build_site.render_page(
        legacy_roster, PUBLIC_RENDER_LOGO, parity_header_meta)
    public_html = build_site.render_catalog_page(
        parity_snapshot, PUBLIC_RENDER_LOGO, parity_header_meta,
        tool_contract=tool_contract)
    assert re.findall(r'<script\b[^>]*>', legacy_html) == [
        '<script type="application/json" id="roster-data">']
    assert [attrs.get("id") for attrs, _ in _script_elements(public_html)] == [
        "catalog-data", "tool-contract"]
    stripped_legacy = re.sub(
        r'<script type="application/json" id="roster-data">.*?</script>', "",
        legacy_html, count=1, flags=re.S)
    assert stripped_legacy == _strip_public_islands(public_html), (
        "public route must reuse the ONE legacy template byte-for-byte")

    # ---- (b) secret-safe public HTML from real capture/build -------------
    token_sentinel = "sk-live-SENTINEL-TOKEN-9f3a"
    auth_sentinel = "Bearer SENTINEL-AUTH-77c1"
    path_sentinel = "/home/operator/.secrets/SENTINEL-PATH"
    pending_sentinel = "SENTINEL-PENDING-ALERT"
    unknown_sentinel = "SENTINEL-UPSTREAM-UNKNOWN"

    configs = {"send-gw": {
        "name": 'Send <Gateway> "quoted"',
        "_token": token_sentinel,
        "auth": auth_sentinel,
        "base_url": path_sentinel,
        "pending_alerts": [pending_sentinel],
        "upstream_unknown": unknown_sentinel,
    }}
    wiring = {"send-gw": {
        "chat_completions_url":
            "https://send.synthetic.example/v1/chat/completions",
        "api_type": "openai-synthetic",
    }}
    roster = {
        "tick_epoch": 1787721434,
        "providers": {"send-gw": ["vendor-a/secret-safe-free"]},
        "provider_models": {"send-gw": {"vendor-a/secret-safe-free": {
            "name": 'Vendor <A> "Safe"',
            "context_length": 65536,
            "architecture": {"input_modalities": ["text", "image"]},
            "auth": auth_sentinel,
            "upstream_unknown": unknown_sentinel,
        }}},
        "stale_providers": [],
        "_token": token_sentinel,
        "local_path": path_sentinel,
        "pending_alerts": [pending_sentinel],
        "upstream_unknown": unknown_sentinel,
        "auth": auth_sentinel,
    }
    header_meta = {"send-gw": {
        "signup_url":
            'https://send.synthetic.example/signup?x="><script>alert(2)</script>',
        "limits_note": 'note "quoted" & <angle>',
    }}
    snapshot_before = copy.deepcopy(
        build_snapshot(capture_registry(configs, wiring),
                       copy.deepcopy(roster), cadence_s=1800))
    configs_before = copy.deepcopy(configs)
    wiring_before = copy.deepcopy(wiring)
    roster_before = copy.deepcopy(roster)
    header_before = copy.deepcopy(header_meta)

    snapshot = build_snapshot(capture_registry(configs, wiring), roster,
                              cadence_s=1800)
    assert snapshot == snapshot_before
    safe_html = build_site.render_catalog_page(
        snapshot, PUBLIC_RENDER_LOGO, header_meta, tool_contract=tool_contract)

    assert snapshot == snapshot_before, "snapshot mutated by rendering"
    assert configs == configs_before and wiring == wiring_before
    assert roster == roster_before, "operational roster input mutated"
    assert header_meta == header_before

    # Both inert islands are present and decode to the intended public data.
    safe_islands = _script_elements(safe_html)
    assert [attrs.get("id") for attrs, _ in safe_islands] == list(_ISLAND_IDS)
    embedded_catalogue = json.loads(safe_islands[0][1])
    embedded_tools = json.loads(safe_islands[1][1])
    external_json = serialize_snapshot(snapshot)

    for sentinel in (token_sentinel, auth_sentinel, path_sentinel,
                     pending_sentinel, unknown_sentinel):
        assert sentinel not in safe_html, f"excluded field leaked: {sentinel}"
        assert sentinel not in external_json, f"excluded field in export: {sentinel}"
    assert "SENTINEL" not in safe_html
    assert "SENTINEL" not in external_json

    # The whole embedded catalogue agrees completely with the export text,
    # typing included — so the island is the SAME public contract, not a
    # second, lossier projection.
    assert _typed(embedded_catalogue) == _typed(json.loads(external_json))
    assert embedded_catalogue["snapshot_id"] == snapshot["snapshot_id"]
    assert _typed(embedded_tools) == _typed(tool_contract)
    # Retained public values DO survive in the decoded data.
    retained = embedded_catalogue["providers"]["send-gw"]["models"][
        "vendor-a/secret-safe-free"]
    assert retained["name"] == 'Vendor <A> "Safe"'
    assert retained["context_length"] == 65536
    assert retained["architecture"]["input_modalities"] == ["text", "image"]
    assert embedded_catalogue["providers"]["send-gw"][
        "chat_completions_url"] == (
            "https://send.synthetic.example/v1/chat/completions")
    assert embedded_catalogue["providers"]["send-gw"]["api_type"] == "openai-synthetic"

    # Intended public values DO survive, escaped where they are hostile text.
    assert "Vendor &lt;A&gt; &quot;Safe&quot;" in safe_html
    assert "vendor-a/secret-safe-free" in safe_html
    assert "Context length: 65536" in safe_html
    assert "Input modalities: text, image" in safe_html
    assert "https://send.synthetic.example/v1/chat/completions" in safe_html
    assert "openai-synthetic" in safe_html
    # Visible copy stays escaped; the hostile header metadata is escaped too.
    assert "<script>alert(2)</script>" not in safe_html
    assert 'href="https://send.synthetic.example/signup?x=&quot;&gt;&lt;script&gt;alert(2)&lt;/script&gt;"' in safe_html
    assert 'title="note &quot;quoted&quot; &amp; &lt;angle&gt;"' in safe_html

    # ---- (c) stdlib structural sanity check (not a browser layout proof) --
    class _TagAudit(HTMLParser):
        _VOID = frozenset({"meta", "img", "input", "link", "br", "hr"})

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.bad = []

        def handle_starttag(self, tag, attrs):
            if tag not in self._VOID:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in self.stack:
                while self.stack and self.stack[-1] != tag:
                    self.bad.append(("unclosed", self.stack.pop()))
                if self.stack:
                    self.stack.pop()
            else:
                self.bad.append(("stray", tag))

    audit = _TagAudit()
    audit.feed(safe_html)
    audit.close()
    assert audit.bad == [], f"unbalanced markup: {audit.bad}"
    assert audit.stack == [], f"unclosed elements: {audit.stack}"
# ---------- Unit 4d: lossless public islands and script-context escaping ----
#
# Two further synthetic-only groups pin the embedding boundary that unit 4c
# deliberately staged out:
#   1. the two islands carry the WHOLE public snapshot and the REAL shared tool
#      array, losslessly and deterministically, for populated, zero-ID and
#      true-empty registries;
#   2. hostile retained text (closing-script in every case, comment/open-script
#      sequences, quotes/backslashes/ampersands/angles, real U+2028/U+2029,
#      non-ASCII/emoji, control characters) stays inert and round-trips
#      exactly, in BOTH islands, with no injected executable markup anywhere.
# Neither group writes a site artifact, touches the production roster or uses
# the network; HTMLParser proves structure/extraction only, never browser
# execution, layout or native WebMCP support.

#: Hostile retained strings for the script-context group. Every entry is legal
#: in a public v1 snapshot value, so the fixture stays a VALID snapshot.
_SCRIPT_HOSTILE_TEXTS = (
    "</script>",
    "</SCRIPT>",
    "</ScRiPt\t>",
    "<!--",
    "<SCRIPT >",
    "<script>",
    "<script src=//evil.example/x.js>",
    "<!-- <script> </script> -->",
    'quoted "double" & \'single\' \\backslash <angle> & amp',
    "line\u2028sep\u2029par",
    "\u65e5\u672c\u8a9e \U0001f415 \u00fcn\u00efcode",
    "control:\x00\x01\x1f\x7f:end",
    "]]><!--<script>alert(1)</script>",
)


def test_embedded_islands_are_whole_value_lossless_and_deterministic():
    """Both islands carry the COMPLETE inputs, typed, for every registry shape.

    Loops the accepted capture/build path over a populated, a zero-ID and a
    true-empty registry, decodes each island with stdlib HTMLParser, and pins:
    exactly two inert JSON elements in order; the catalogue validates and
    equals the SAME snapshot's export text with types intact (root
    ``snapshot_id``, warnings and provider metadata the matrix never shows);
    the tool contract equals the real unchanged five-definition resource in
    resource order; ASCII-only, less-than-free script bodies; deterministic
    output and unmutated inputs.
    """
    import copy

    import build_site
    from catalog_export import serialize_snapshot
    from watchdog_catalog import catalog

    tool_contract = _load_tool_contract()
    assert isinstance(tool_contract, list)
    assert [definition["name"] for definition in tool_contract] == list(_TOOL_NAMES)

    fixtures = {
        "populated": _public_snapshot(
            {
                "synthetic-beta": ["acme/token-2:free", "acme/only-beta-free"],
                "synthetic-alpha": ["acme/token-2:free"],
                "synthetic-empty": [],
            },
            {
                "synthetic-beta": {
                    "acme/token-2:free": {
                        "name": "acme/token", "context_length": 262144,
                        "pricing": {"prompt": 1, "completion": 1.0}},
                    "acme/only-beta-free": {
                        "name": "acme/only-beta", "context_length": 8192},
                },
                "synthetic-alpha": {
                    "acme/token-2:free": {
                        "name": "acme/token", "context_length": 262144,
                        "pricing": {"prompt": "0.0", "completion": None}},
                },
            },
        ),
        "zero-id": _public_snapshot({"synthetic-empty": []}, {}),
        "true-empty": _public_snapshot({}, {}),
    }

    for label, snapshot in fixtures.items():
        snapshot_before = copy.deepcopy(snapshot)
        tools_before = copy.deepcopy(tool_contract)

        html = build_site.render_catalog_page(
            snapshot, PUBLIC_RENDER_LOGO, tool_contract=tool_contract)
        assert html == build_site.render_catalog_page(
            snapshot, PUBLIC_RENDER_LOGO, tool_contract=tool_contract), label
        assert snapshot == snapshot_before, f"{label}: snapshot mutated"
        assert tool_contract == tools_before, f"{label}: tool array mutated"

        islands = _script_elements(html)
        assert [attrs.get("id") for attrs, _ in islands] == list(_ISLAND_IDS), label
        assert [attrs.get("type") for attrs, _ in islands] == [
            "application/json", "application/json"], label
        assert all(attrs.get("src") is None for attrs, _ in islands), label
        catalogue_body, tools_body = islands[0][1], islands[1][1]

        # Script-context-safe, complete text bodies: pure ASCII JSON with no
        # literal angle bracket, ampersand or raw Unicode line separator.
        for body in (catalogue_body, tools_body):
            assert body, label
            assert body.isascii(), label
            for forbidden in ("<", ">", "&", "\u2028", "\u2029", "<!--"):
                assert forbidden not in body, (label, forbidden)

        # Whole-value public catalogue: accepted validator, full agreement with
        # the SAME snapshot's export text (root id included), types intact.
        embedded = json.loads(catalogue_body)
        assert _typed(catalog.load_snapshot(catalogue_body)) == _typed(snapshot)
        assert _typed(embedded) == _typed(json.loads(serialize_snapshot(snapshot)))
        assert embedded["snapshot_id"] == snapshot["snapshot_id"], label
        assert embedded["schema_version"] == 1
        assert embedded["cadence_s"] == 3600

        # The REAL shared tool array, complete and in resource order.
        decoded_tools = json.loads(tools_body)
        assert _typed(decoded_tools) == _typed(tool_contract), label
        assert [definition["name"] for definition in decoded_tools] == list(
            _TOOL_NAMES), label
        assert all(
            definition["description"]
            and definition["inputSchema"]["type"] == "object"
            for definition in decoded_tools), label

        if label == "populated":
            assert embedded["provider_order"] == [
                "synthetic-beta", "synthetic-alpha", "synthetic-empty"]
            # A zero-ID provider ships in the DATA even though it gets no
            # column: empty legitimate data stays distinguishable from loss.
            assert embedded["providers"]["synthetic-empty"]["model_ids"] == []
            # int / float / numeric-string / null must not conflate.
            beta = embedded["providers"]["synthetic-beta"]["models"][
                "acme/token-2:free"]
            assert beta["pricing"] == {"prompt": 1, "completion": 1.0}
            assert type(beta["pricing"]["prompt"]) is int
            assert type(beta["pricing"]["completion"]) is float
            alpha = embedded["providers"]["synthetic-alpha"]["models"][
                "acme/token-2:free"]
            assert alpha["pricing"] == {"prompt": "0.0", "completion": None}
            assert type(alpha["pricing"]["prompt"]) is str
            assert embedded["providers"]["synthetic-beta"]["stale"] is False
            assert type(embedded["tick_epoch"]) is int
        if label == "zero-id":
            assert embedded["providers"]["synthetic-empty"]["model_ids"] == []
            assert embedded["providers"]["synthetic-empty"]["models"] == {}
            assert "name-row" not in _strip_public_islands(html)
        if label == "true-empty":
            assert embedded["provider_order"] == []
            assert embedded["providers"] == {}


def test_script_context_hostile_text_is_inert_and_lossless():
    """Hostile public text cannot escape, close or corrupt either island.

    Builds one REAL validated snapshot whose retained values carry every
    closing-script casing, comment/open-script sequences, quotes, backslashes,
    ampersands, angles, real U+2028/U+2029, non-ASCII/emoji and control
    characters, plus a valid-shape tool-description variant with hostile text.
    Parses the WHOLE page: exactly the two intended inert elements, complete
    round-tripping payloads, and no injected executable script/image/event
    attribute or prematurely closed island. Matrix visible/attribute escaping
    still holds and no input is mutated.
    """
    import copy
    from html import escape
    from html.parser import HTMLParser

    import build_site
    from catalog_export import build_snapshot, capture_registry

    hostile_name = "</script><SCRIPT ><!-- & \"quoted\" \u2028 \u65e5\u672c \U0001f415"
    hostile_id = "vendor/</script>id</SCRIPT>-free"

    model_ids = [f"vendor/hostile-{index:02d}:free"
                 for index in range(len(_SCRIPT_HOSTILE_TEXTS))]
    model_meta = {
        f"vendor/hostile-{index:02d}:free": {
            "description": text,
            "supported_parameters": [text],
        }
        for index, text in enumerate(_SCRIPT_HOSTILE_TEXTS)
    }
    model_ids.append(hostile_id)
    model_meta[hostile_id] = {
        "name": hostile_name,
        "description": "<!-- <script> </script> -->",
        "supported_parameters": [hostile_name],
    }

    configs = {"hostile-gw": {
        "name": "</ScRiPt><script>alert('display')</script><!--",
        "_token": "SENTINEL-HOSTILE-TOKEN",
    }}
    wiring = {"hostile-gw": {
        "chat_completions_url": "https://hostile.synthetic.example/v1/chat/completions",
        "api_type": "<!--api-type-->",
    }}
    roster = {
        "tick_epoch": 1787721434,
        "providers": {"hostile-gw": model_ids},
        "provider_models": {"hostile-gw": model_meta},
        "stale_providers": [],
    }

    tool_contract = copy.deepcopy(_load_tool_contract())
    tool_contract[0]["description"] = "\n".join(_SCRIPT_HOSTILE_TEXTS)

    snapshot_before = copy.deepcopy(
        build_snapshot(capture_registry(configs, wiring),
                       copy.deepcopy(roster), cadence_s=3600))
    configs_before = copy.deepcopy(configs)
    wiring_before = copy.deepcopy(wiring)
    roster_before = copy.deepcopy(roster)
    tools_before = copy.deepcopy(tool_contract)

    snapshot = build_snapshot(capture_registry(configs, wiring), roster,
                              cadence_s=3600)
    html = build_site.render_catalog_page(
        snapshot, PUBLIC_RENDER_LOGO, tool_contract=tool_contract)

    assert html == build_site.render_catalog_page(
        snapshot, PUBLIC_RENDER_LOGO, tool_contract=tool_contract)
    assert snapshot == snapshot_before, "snapshot mutated by rendering"
    assert configs == configs_before and wiring == wiring_before
    assert roster == roster_before, "roster input mutated"
    assert tool_contract == tools_before, "tool array mutated"

    # ---- whole-page structure: two inert islands and nothing executable ---
    class _PageAudit(HTMLParser):
        _BANNED = frozenset({"iframe", "object", "embed", "base", "form"})

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.tags = []
            self.scripts = []
            self.open = None
            self.bad = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            self.tags.append((tag, attrs))
            if tag == "script":
                if self.open is not None:
                    self.bad.append(("nested-script", tag))
                self.open = [attrs, []]
            if tag in self._BANNED:
                self.bad.append(("banned-element", tag))
            for name, value in attrs.items():
                if name.lower().startswith("on"):
                    self.bad.append(("event-handler", name))
                if name == "src" and not value.startswith("data:image/png;base64,"):
                    self.bad.append(("external-src", value))

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)

        def handle_data(self, data):
            if self.open is not None:
                self.open[1].append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self.open is not None:
                self.scripts.append((self.open[0], "".join(self.open[1])))
                self.open = None

    audit = _PageAudit()
    audit.feed(html)
    audit.close()
    assert audit.open is None, "an island was left unterminated"
    assert audit.bad == [], f"injected markup/attribute: {audit.bad}"
    assert [attrs.get("id") for attrs, _ in audit.scripts] == list(_ISLAND_IDS)
    assert [attrs.get("type") for attrs, _ in audit.scripts] == [
        "application/json", "application/json"]
    assert all(attrs.get("src") is None for attrs, _ in audit.scripts)
    assert len([tag for tag, _ in audit.tags if tag == "img"]) == 1
    assert len([tag for tag, _ in audit.tags if tag == "script"]) == 2

    catalogue_body, tools_body = audit.scripts[0][1], audit.scripts[1][1]
    for body in (catalogue_body, tools_body):
        assert body.isascii()
        for forbidden in ("<", ">", "&", "\u2028", "\u2029", "<!--", "</"):
            assert forbidden not in body, forbidden

    # ---- complete round-trip of every hostile value, both islands --------
    embedded = json.loads(catalogue_body)
    models = embedded["providers"]["hostile-gw"]["models"]
    for index, text in enumerate(_SCRIPT_HOSTILE_TEXTS):
        model = models[f"vendor/hostile-{index:02d}:free"]
        assert model["description"] == text, (index, model["description"])
        assert model["supported_parameters"] == [text], index
    assert models[hostile_id]["name"] == hostile_name
    assert models[hostile_id]["supported_parameters"] == [hostile_name]
    assert embedded["providers"]["hostile-gw"]["display_name"] == (
        "</ScRiPt><script>alert('display')</script><!--")
    assert embedded["providers"]["hostile-gw"]["api_type"] == "<!--api-type-->"
    assert "SENTINEL-HOSTILE-TOKEN" not in html

    embedded_tools = json.loads(tools_body)
    assert _typed(embedded_tools) == _typed(tool_contract)
    assert embedded_tools[0]["description"] == tools_before[0]["description"]
    assert "</script>" in embedded_tools[0]["description"]  # inert, retained

    # ---- the visible matrix still escapes its text -----------------------
    matrix_html = _strip_public_islands(html)
    assert "<script" not in matrix_html
    assert "<!--" not in matrix_html
    assert escape(hostile_name) in matrix_html
    assert hostile_name not in matrix_html
    assert "evil.example" not in matrix_html
    assert "src=//evil.example" not in matrix_html
    assert "SENTINEL-HOSTILE-TOKEN" not in matrix_html


# ---------- Unit 4e: validated public artifact-pair publication -------------
#
# The CLI is now the producer's ONE publication route: one roster read, one
# registry capture, ONE validated public snapshot, and BOTH site/index.html and
# site/catalog.json written from that same object. Three synthetic-only groups
# pin it (no production roster/state, no network, no cron/publish wrapper):
#   1. coherent acquisition -> validated pair, deterministic, secret-safe, with
#      the real shared tool resource, default headers/logo and no roster
#      island; one snapshot object feeds both artifacts;
#   2. failure retention for absent AND pre-existing last-good targets across
#      source/registry/logo/resource/render/serialization/staging/replacement
#      failures, including a mixed pair after a late injected failure;
#   3. concurrent thread/process writers and the generated-file ignore rules.

PAIR_TOOLS = REPO / "watchdog_catalog" / "tools.json"

# Every excluded-field sentinel below is a SYNTHETIC value that must reach
# neither public artifact; the retained public facts must survive.
PAIR_ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": ["vendor-z/shared-free", "vendor-a/only-nous:free"],
        "kilo": ["vendor-z/shared-free"],
        "openrouter": [],
        # Unknown to the registry: validated, then excluded -- never published.
        "mystery-gateway": ["vendor-m/ghost-free"],
    },
    "provider_models": {
        "nous": {
            "vendor-z/shared-free": {
                "name": "vendor-z/shared",
                "context_length": 4096,
                "architecture": {"input_modalities": ["text"]},
                "_token": "SENTINEL-4E-MODEL-TOKEN",
            },
        },
    },
    "stale_providers": ["kilo"],
    "_token": "SENTINEL-4E-TOKEN",
    "auth": {"token": "SENTINEL-4E-AUTH"},
    "local_path": "/home/operator/SENTINEL-4E-PATH",
    "pending_alerts": ["SENTINEL-4E-PENDING"],
    "upstream_unknown": "SENTINEL-4E-UPSTREAM",
}

PAIR_SENTINELS = (
    "SENTINEL-4E-TOKEN", "SENTINEL-4E-AUTH", "SENTINEL-4E-PATH",
    "SENTINEL-4E-PENDING", "SENTINEL-4E-UPSTREAM", "SENTINEL-4E-MODEL-TOKEN",
)

LAST_GOOD = {
    "catalog.json": b'{"last_good":true}\n',
    "index.html": b"<!doctype html><p>last-good-page</p>",
}


def _write_roster(root, roster):
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "roster.json").write_text(json.dumps(roster), encoding="utf-8")


def _seed_last_good(root):
    site = root / "site"
    site.mkdir(parents=True, exist_ok=True)
    for name, blob in LAST_GOOD.items():
        (site / name).write_bytes(blob)


def _targets(root):
    site = root / "site"
    return site / "catalog.json", site / "index.html"


def _run_cli(root):
    """Run the real CLI in-process; return its exit code (0 when it returns)."""
    import build_site
    try:
        build_site.main(["--root", str(root)])
    except SystemExit as exc:
        return exc.code
    return 0


def _site_leftovers(root):
    """Owned temp/lock leftovers under site/, sorted (bounded cleanup proof)."""
    site = root / "site"
    if not site.is_dir():
        return []
    return sorted(p.name for p in site.iterdir()
                  if p.name not in {"index.html", "catalog.json", ".build.lock"})


def _pair_tool_contract():
    return json.loads(PAIR_TOOLS.read_text(encoding="utf-8"))


class _FailingTextHandle:
    """Proxy over a REAL staged text handle that fails exactly one operation.

    ``close`` releases the underlying descriptor FIRST and only then raises, so
    the injected close fault never leaks a descriptor of its own (the leak under
    test is the writer's, not the double's). ``seen['hit']`` is set when the
    fault actually fires, proving the row reached the intended path.
    """

    def __init__(self, handle, operation, seen):
        self._handle = handle
        self._operation = operation
        self._seen = seen

    def __getattr__(self, name):
        return getattr(self._handle, name)

    # Context-manager support keeps this double faithful to BOTH staging
    # shapes (a ``with`` block or explicit close), so a regression row fails on
    # the leftover/retention symptom instead of a double-compatibility TypeError.
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    def _fire(self, message):
        self._seen["hit"] = True
        raise OSError(message)

    def write(self, text):
        if self._operation == "write":
            self._fire("injected write failure")
        return self._handle.write(text)

    def flush(self):
        if self._operation == "flush":
            self._fire("injected flush failure")
        return self._handle.flush()

    def close(self):
        self._handle.close()
        if self._operation == "close":
            self._fire("injected close failure")


class _FailingLockFile:
    """Proxy over a REAL lock file whose close releases then raises."""

    def __init__(self, handle, seen):
        self._handle = handle
        self._seen = seen

    def fileno(self):
        return self._handle.fileno()

    def close(self):
        self._handle.close()
        self._seen["hit"] = True
        raise OSError("injected lock close failure")


def _arm_stage_failure(patch, which, operation, seen):
    """Inject ONE post-create staging fault on artifact #``which`` (1-based).

    ``seen['hit']`` is set only when the injected fault actually fires, so a
    row can prove it reached the intended ownership path; ``seen['fd']`` records
    the descriptor handed to ``os.fdopen`` for that artifact so descriptor
    closure is checkable deterministically.
    """
    import build_site
    import os

    real_fdopen = os.fdopen
    real_fsync = os.fsync
    real_mkstemp = build_site.tempfile.mkstemp
    calls = {"fdopen": 0, "fsync": 0, "mkstemp": 0}

    if operation == "mkstemp":
        def mkstemp(*args, **kwargs):
            calls["mkstemp"] += 1
            if calls["mkstemp"] == which:
                seen["hit"] = True
                raise OSError("injected mkstemp failure")
            return real_mkstemp(*args, **kwargs)

        patch.setattr(build_site.tempfile, "mkstemp", mkstemp)
        return

    def fdopen(fd, *args, **kwargs):
        calls["fdopen"] += 1
        current = calls["fdopen"]
        if current == which and operation == "fdopen":
            seen["hit"] = True
            seen["fd"] = fd
            raise OSError("injected fdopen failure")
        handle = real_fdopen(fd, *args, **kwargs)
        if current == which:
            seen["fd"] = fd
            if operation in ("write", "flush", "close"):
                return _FailingTextHandle(handle, operation, seen)
        return handle

    patch.setattr(build_site.os, "fdopen", fdopen)

    if operation == "fsync":
        def fsync(fd):
            calls["fsync"] += 1
            if calls["fsync"] == which:
                seen["hit"] = True
                raise OSError("injected fsync failure")
            return real_fsync(fd)

        patch.setattr(build_site.os, "fsync", fsync)


def _expected_pair(root):
    """Independent oracle: the accepted capture -> build -> serialize -> render."""
    import build_site
    from catalog_export import capture_registry, serialize_snapshot

    snapshot = build_site.build_snapshot(
        capture_registry(build_site.PROVIDERS, build_site.GATEWAY_WIRING),
        json.loads(json.dumps(PAIR_ROSTER)), cadence_s=3600)
    return (
        snapshot,
        serialize_snapshot(snapshot),
        build_site.render_catalog_page(
            snapshot, build_site.load_logo_b64(root),
            build_site.build_provider_header_meta(),
            tool_contract=_pair_tool_contract()),
    )


def test_cli_publishes_one_validated_public_pair(tmp_path, monkeypatch, capsys):
    """One roster read -> one capture -> ONE snapshot -> BOTH artifacts.

    Pins the coherent public route end to end: spied single acquisition, the
    SAME snapshot object serialized and rendered, deterministic repeated
    builds, exact both-target read-back, the real shared tool array, default
    provider headers/logo, no legacy roster island, zero-ID/true-empty data
    staying distinguishable from corruption, excluded sentinels absent, and a
    stdlib-only run with no SDK/Node/PATH dependency.
    """
    import copy
    import os

    import build_site
    from catalog_export import validate_artifact_pair

    _seed_logo(tmp_path)
    _write_roster(tmp_path, PAIR_ROSTER)

    calls = {"load": 0, "capture": 0, "build": 0, "serialize": [], "render": []}
    seen = {}
    real_load = build_site.load_roster
    real_capture = build_site.capture_registry
    real_build = build_site.build_snapshot
    real_serialize = build_site.serialize_snapshot
    real_render = build_site.render_catalog_page

    def spy_load(root):
        calls["load"] += 1
        return real_load(root)

    def spy_capture(providers, wiring):
        calls["capture"] += 1
        seen["providers"] = copy.deepcopy(providers)
        seen["wiring"] = copy.deepcopy(wiring)
        return real_capture(providers, wiring)

    def spy_build(records, roster, *, cadence_s):
        calls["build"] += 1
        calls["cadence"] = cadence_s
        seen["roster"] = copy.deepcopy(roster)
        snapshot = real_build(records, roster, cadence_s=cadence_s)
        calls["snapshot"] = snapshot
        return snapshot

    def spy_serialize(snapshot):
        calls["serialize"].append(snapshot)
        return real_serialize(snapshot)

    def spy_render(snapshot, logo_b64, header_meta=None, *, tool_contract):
        calls["render"].append((snapshot, tool_contract))
        html = real_render(snapshot, logo_b64, header_meta,
                           tool_contract=tool_contract)
        calls["html"] = html
        return html

    for name, spy in (("load_roster", spy_load),
                      ("capture_registry", spy_capture),
                      ("build_snapshot", spy_build),
                      ("serialize_snapshot", spy_serialize),
                      ("render_catalog_page", spy_render)):
        monkeypatch.setattr(build_site, name, spy)

    providers_before = copy.deepcopy(build_site.PROVIDERS)
    wiring_before = copy.deepcopy(build_site.GATEWAY_WIRING)
    roster_before = copy.deepcopy(PAIR_ROSTER)

    assert _run_cli(tmp_path) == 0
    first = capsys.readouterr()
    assert first.out == "", "a successful CLI must stay stdout-silent"
    assert first.err.strip(), "diagnostics belong on stderr"

    # --- one observation, one capture, one validated snapshot --------------
    assert calls["load"] == 1 and calls["capture"] == 1 and calls["build"] == 1
    assert calls["cadence"] == 3600 == build_site.BUILD_CADENCE_S
    snapshot = calls["snapshot"]
    assert len(calls["serialize"]) == 1 and len(calls["render"]) == 1
    assert calls["serialize"][0] is snapshot, "JSON must come from THE snapshot"
    assert calls["render"][0][0] is snapshot, "HTML must come from THE snapshot"
    assert calls["render"][0][1] == _pair_tool_contract()
    assert build_site.PROVIDERS == providers_before
    assert build_site.GATEWAY_WIRING == wiring_before
    assert seen["roster"] == roster_before, "operational roster must be unmutated"
    assert seen["providers"] == providers_before
    assert seen["wiring"] == wiring_before

    catalog_path, html_path = _targets(tmp_path)
    catalog_text = catalog_path.read_bytes().decode("utf-8")
    html_text = html_path.read_bytes().decode("utf-8")
    # Exact both-target read-back: the files ARE the prepared payloads.
    assert catalog_text == real_serialize(snapshot)
    assert html_text == calls["html"]
    assert catalog_text.endswith("\n") and not catalog_text.endswith("\n\n")

    # --- the published pair is coherent and carries the public contract ----
    published = validate_artifact_pair(catalog_text, html_text)
    assert published["snapshot_id"] == snapshot["snapshot_id"]
    assert published == snapshot
    assert published["schema_version"] == 1
    assert published["cadence_s"] == 3600
    assert published["tick_epoch"] == 1787721434
    assert published["provider_order"] == list(build_site.PROVIDERS)
    assert published["providers"]["openrouter"]["model_ids"] == []
    assert published["providers"]["kilo"]["stale"] is True
    assert published["providers"]["nous"]["models"]["vendor-z/shared-free"][
        "architecture"]["input_modalities"] == ["text"]
    # An unknown registry key is validated then EXCLUDED, never published.
    assert "mystery-gateway" not in published["providers"]
    assert "vendor-m/ghost-free" not in html_text

    # --- no raw private state, no legacy island, two inert islands only ----
    for sentinel in PAIR_SENTINELS:
        assert sentinel not in catalog_text, sentinel
        assert sentinel not in html_text, sentinel
    islands = _script_elements(html_text)
    assert [attrs.get("id") for attrs, _ in islands] == list(_ISLAND_IDS)
    assert 'id="roster-data"' not in html_text
    assert len(re.findall(r"<script\b", html_text)) == 2
    matrix_html = _strip_public_islands(html_text)
    assert "vendor-z/shared" in matrix_html
    assert "Context length: 4096" in matrix_html

    # --- default headers + logo come from the real project surface ---------
    assert 'href="https://portal.nousresearch.com/"' in html_text
    logo_b64 = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"',
                         html_text).group(1)
    assert base64.b64decode(logo_b64) == (tmp_path / "assets" / "logo.png").read_bytes()

    # --- deterministic repeated builds (same root and a fresh root) --------
    assert _run_cli(tmp_path) == 0
    second = capsys.readouterr()
    assert second.out == ""
    assert catalog_path.read_bytes().decode("utf-8") == catalog_text
    assert html_path.read_bytes().decode("utf-8") == html_text
    twin = tmp_path / "twin"
    _seed_logo(twin)
    _write_roster(twin, PAIR_ROSTER)
    assert _run_cli(twin) == 0
    capsys.readouterr()
    assert _targets(twin)[0].read_bytes().decode("utf-8") == catalog_text
    assert _targets(twin)[1].read_bytes().decode("utf-8") == html_text
    assert not [p for p in (tmp_path / "site").iterdir() if p.suffix == ".tmp"]

    # --- legitimate empty data stays distinguishable from corruption -------
    empty = tmp_path / "empty"
    _seed_logo(empty)
    _write_roster(empty, {"tick_epoch": 42, "providers": {}})
    assert _run_cli(empty) == 0
    capsys.readouterr()
    empty_catalog, empty_html = (
        p.read_bytes().decode("utf-8") for p in _targets(empty))
    empty_published = validate_artifact_pair(empty_catalog, empty_html)
    assert empty_published["provider_order"] == list(build_site.PROVIDERS)
    assert all(entry["model_ids"] == [] and entry["models"] == {}
               for entry in empty_published["providers"].values())
    assert empty_published["snapshot_id"] and empty_published["tick_epoch"] == 42
    assert "name-row" not in _strip_public_islands(empty_html)
    assert empty_catalog != catalog_text

    # --- stdlib only: no SDK import and no PATH/Node binary needed ---------
    probe_root = tmp_path / "probe"
    _seed_logo(probe_root)
    _write_roster(probe_root, PAIR_ROSTER)
    probe = (
        "import sys, build_site;"
        "build_site.main(['--root', sys.argv[1]]);"
        "print(sorted(m for m in ('mcp', 'node', 'requests', 'httpx')"
        " if m in sys.modules))")
    proc = subprocess.run(
        [sys.executable, "-S", "-c", probe, str(probe_root)],
        cwd=str(REPO), capture_output=True, text=True,
        env={"PATH": "", "HOME": str(tmp_path), "PYTHONPATH": str(REPO)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout
    assert validate_artifact_pair(
        *[p.read_bytes().decode("utf-8") for p in _targets(probe_root)])


def test_cli_failure_retention_and_injected_write_failures(
        tmp_path, monkeypatch, capsys):
    """Pre-replacement failures retain last-good bytes; an injected mixed pair fails.

    One row loop covers source, registry, logo, resource, serialization,
    render, validation and staging faults against BOTH absent and pre-existing
    final files. Replacement failures are then injected first and second
    separately; a mixed surviving pair is rejected by the pure validator and
    an interloper-spliced target is caught by the exact-target read-back.
    """
    import catalog_export
    from catalog_export import validate_artifact_pair
    from watchdog_catalog import catalog

    import build_site
    import os

    def _raise_catalog(*args, **kwargs):
        raise catalog.CatalogError(catalog.INVALID_SNAPSHOT)

    def _raise_runtime(*args, **kwargs):
        raise RuntimeError("forced failure")

    def _raise_pair(*args, **kwargs):
        raise ValueError("Catalogue artifacts do not match.")

    def _raise_os(*args, **kwargs):
        raise OSError("forced write failure")

    bad_tools = tmp_path / "bad-tools.json"
    bad_tools.write_text("{not json", encoding="utf-8")
    wrong_tools = tmp_path / "wrong-tools.json"
    wrong_tools.write_text('{"name": "not-an-array"}', encoding="utf-8")

    def malformed_roster(root):
        _write_roster(root, {"tick_epoch": 1,
                             "providers": {"nous": "vendor-z/shared-free"}})

    def bad_id_roster(root):
        _write_roster(root, {"tick_epoch": 1,
                             "providers": {"mystery-gateway": [None]}})

    def corrupt_roster(root):
        (root / "state").mkdir(parents=True, exist_ok=True)
        (root / "state" / "roster.json").write_text("NOT JSON{{", encoding="utf-8")

    def wrong_root_roster(root):
        (root / "state").mkdir(parents=True, exist_ok=True)
        (root / "state" / "roster.json").write_text("[]", encoding="utf-8")

    oversized = b"\x89PNG\r\n\x1a\n" + b"0" * (40 * 1024 + 1)

    def big_logo(root):
        _seed_logo(root, blob=oversized)

    cases = [
        ("missing-roster", lambda root, patch: (
            root / "state" / "roster.json").unlink(missing_ok=True)),
        ("corrupt-roster", lambda root, patch: corrupt_roster(root)),
        ("wrong-root-roster", lambda root, patch: wrong_root_roster(root)),
        ("malformed-provider", lambda root, patch: malformed_roster(root)),
        ("malformed-unknown-provider-id",
         lambda root, patch: bad_id_roster(root)),
        ("bad-logo", lambda root, patch: big_logo(root)),
        ("missing-tool-resource",
         lambda root, patch: patch.setattr(
             "build_site.TOOLS_REL", tmp_path / "absent-tools.json")),
        ("corrupt-tool-resource",
         lambda root, patch: patch.setattr("build_site.TOOLS_REL", bad_tools)),
        ("wrong-type-tool-resource",
         lambda root, patch: patch.setattr("build_site.TOOLS_REL", wrong_tools)),
        ("invalid-registry-wiring",
         lambda root, patch: patch.setattr("build_site.GATEWAY_WIRING", {})),
        ("invalid-registry-config",
         lambda root, patch: patch.setattr(
             "build_site.PROVIDERS", {"nous": "not-a-config"})),
        ("invalid-snapshot",
         lambda root, patch: patch.setattr(
             "build_site.build_snapshot", _raise_catalog)),
        ("serialization-failure",
         lambda root, patch: patch.setattr(
             "build_site.serialize_snapshot", _raise_runtime)),
        ("render-failure",
         lambda root, patch: patch.setattr(
             "build_site.render_catalog_page", _raise_runtime)),
        ("pair-validation-failure",
         lambda root, patch: patch.setattr(
             "build_site.validate_artifact_pair", _raise_pair)),
        ("staging-failure",
         lambda root, patch: patch.setattr(
             build_site.tempfile, "mkstemp", _raise_os)),
        ("replacement-failure",
         lambda root, patch: patch.setattr(
             build_site.os, "replace", _raise_os)),
    ]

    for mode in ("absent", "pre-existing"):
        for index, (label, setup) in enumerate(cases):
            root = tmp_path / f"{mode}-{index}"
            root.mkdir()
            _seed_logo(root)
            _write_roster(root, PAIR_ROSTER)
            if mode == "pre-existing":
                _seed_last_good(root)
            capsys.readouterr()
            with monkeypatch.context() as patch:
                setup(root, patch)
                assert _run_cli(root) == 2, (mode, label)
            captured = capsys.readouterr()
            assert captured.out == "", (mode, label, captured.out)
            assert captured.err.strip(), (mode, label)
            for name in LAST_GOOD:
                target = root / "site" / name
                if mode == "absent":
                    assert not target.exists(), (mode, label, name)
                else:
                    assert target.read_bytes() == LAST_GOOD[name], (
                        mode, label, name)
            assert _site_leftovers(root) == [], (mode, label)

    # Independent oracle for the NEW pair, reused by the resource-ownership
    # rows below and by the replacement/splice regressions after them.
    oracle_root = tmp_path / "oracle"
    _seed_logo(oracle_root)
    snapshot, new_catalog, new_html = _expected_pair(oracle_root)
    assert validate_artifact_pair(new_catalog, new_html)["snapshot_id"] == \
        snapshot["snapshot_id"]

    # ---- owned staging resources: every post-create failure, both artifacts -
    #
    # _stage owns the descriptor AND the temp path from the instant the
    # exclusive create returns. Every one of these injections fires AFTER a
    # successful mkstemp (proved by seen["hit"]), so each row is a real
    # resource-ownership failure path, not a missing-symbol check. `seen["fd"]`
    # records the descriptor handed to fdopen so a leaked descriptor is
    # detectable deterministically; the close double releases the real
    # descriptor first so the double never leaks on its own.
    def _assert_fd_released(fd, label):
        try:
            os.fstat(fd)
        except OSError:
            return
        raise AssertionError(f"staging descriptor {fd} left open: {label}")

    def _assert_diag(captured, label, needle=None):
        assert captured.out == "", (label, captured.out)
        assert captured.err.strip(), label
        assert "Traceback" not in captured.err, label
        if needle is not None:
            assert needle in captured.err, (label, captured.err)

    def _assert_finals(root, mode, label):
        for name in LAST_GOOD:
            target = root / "site" / name
            if mode == "absent":
                assert not target.exists(), (mode, label, name)
            else:
                assert target.read_bytes() == LAST_GOOD[name], (
                    mode, label, name)

    def staged_root(label, mode):
        root = tmp_path / label
        root.mkdir()
        _seed_logo(root)
        _write_roster(root, PAIR_ROSTER)
        if mode == "pre-existing":
            _seed_last_good(root)
        return root

    for mode in ("absent", "pre-existing"):
        for which in (1, 2):
            for operation in ("fdopen", "write", "flush", "fsync", "close"):
                label = f"stage-{mode}-{which}-{operation}"
                root = staged_root(label, mode)
                capsys.readouterr()
                seen = {}
                with monkeypatch.context() as patch:
                    _arm_stage_failure(patch, which, operation, seen)
                    code = _run_cli(root)
                assert seen.get("hit") is True, label
                assert code == 2, label
                _assert_fd_released(seen["fd"], label)
                _assert_diag(capsys.readouterr(), label,
                             f"injected {operation} failure")
                _assert_finals(root, mode, label)
                assert _site_leftovers(root) == [], label

        # Partial staging: the SECOND exclusive create itself fails, so the
        # first already-staged temp is inside the writer's own cleanup region.
        label = f"partial-stage-{mode}"
        root = staged_root(label, mode)
        capsys.readouterr()
        seen = {}
        with monkeypatch.context() as patch:
            _arm_stage_failure(patch, 2, "mkstemp", seen)
            code = _run_cli(root)
        assert seen.get("hit") is True, label
        assert code == 2, label
        _assert_diag(capsys.readouterr(), label, "injected mkstemp failure")
        _assert_finals(root, mode, label)
        assert _site_leftovers(root) == [], label

    # ---- REAL staging encoding fault: an unpaired-surrogate raw ID ---------
    #
    # A raw ID carrying a lone surrogate is legal input to the JSON roster and
    # is accepted by the snapshot/schema surfaces, but it cannot be encoded as
    # UTF-8 for the HTML payload. Nothing is mocked here: the fault is a real
    # UnicodeEncodeError raised by the real staging write, so it must be
    # normalized at the writer's staging boundary into exit 2 + a bounded
    # diagnostic instead of escaping the CLI as a traceback. Both payloads
    # reach staging (two exclusive creates) and neither final path is touched.
    surrogate_id = "vendor-z/surrogate-\ud800-free"
    surrogate_roster = json.loads(json.dumps(PAIR_ROSTER))
    surrogate_roster["providers"]["nous"] = [
        surrogate_id] + surrogate_roster["providers"]["nous"]

    for mode in ("absent", "pre-existing"):
        label = f"staging-encoding-{mode}"
        root = tmp_path / label
        root.mkdir()
        _seed_logo(root)
        _write_roster(root, surrogate_roster)
        if mode == "pre-existing":
            _seed_last_good(root)
        capsys.readouterr()
        stages = {"mkstemp": 0}
        real_mkstemp_enc = build_site.tempfile.mkstemp

        def counting_mkstemp_enc(*args, **kwargs):
            stages["mkstemp"] += 1
            return real_mkstemp_enc(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(build_site.tempfile, "mkstemp", counting_mkstemp_enc)
            code = _run_cli(root)
        assert code == 2, label
        _assert_diag(capsys.readouterr(), label, "not encodable as UTF-8")
        # The JSON payload staged successfully and the HTML temp was created
        # before its write hit the encoding fault: the fault is at the staging
        # boundary, not earlier in preparation.
        assert stages["mkstemp"] == 2, label
        _assert_finals(root, mode, label)
        assert _site_leftovers(root) == [], label

    # Control: an ordinary (encodable) non-ASCII id still publishes and
    # round-trips through the existing public contract.
    control_id = "vendor-z/\u00fcn\u00efcode-\u514d\u8d39-free"
    control_roster = json.loads(json.dumps(PAIR_ROSTER))
    control_roster["providers"]["nous"] = [
        control_id] + control_roster["providers"]["nous"]
    label = "staging-encoding-control"
    root = tmp_path / label
    root.mkdir()
    _seed_logo(root)
    _write_roster(root, control_roster)
    capsys.readouterr()
    assert _run_cli(root) == 0, label
    captured = capsys.readouterr()
    assert captured.out == "", (label, captured.out)
    control_catalog, control_html = _targets(root)
    control_catalog_text = control_catalog.read_bytes().decode("utf-8")
    control_html_text = control_html.read_bytes().decode("utf-8")
    assert control_id in control_html_text, label
    assert control_id in validate_artifact_pair(
        control_catalog_text, control_html_text)[
            "providers"]["nous"]["model_ids"], label
    assert _site_leftovers(root) == [], label

    # ---- lock lifetime: open / acquire / release / close -------------------
    #
    # Both temps exist before each of these faults. A pre-existing lock sentinel
    # plus an unrelated site file prove cleanup never unlinks the lock inode or
    # arbitrary site contents, and never rewrites either.
    LOCK_SENTINEL = b"pre-existing-build-lock"
    real_flock = build_site.fcntl.flock
    real_pair_replace = os.replace

    def lock_root(label, mode):
        root = tmp_path / f"lock-{mode}-{label}"
        root.mkdir()
        _seed_logo(root)
        _write_roster(root, PAIR_ROSTER)
        if mode == "pre-existing":
            _seed_last_good(root)
        site = root / "site"
        site.mkdir(parents=True, exist_ok=True)
        (site / ".build.lock").write_bytes(LOCK_SENTINEL)
        (site / "unrelated.txt").write_bytes(b"keep me")
        return root

    def site_entries(root):
        return sorted(p.name for p in (root / "site").iterdir())

    def lock_identity(root):
        lock = root / "site" / ".build.lock"
        return lock.stat().st_ino, lock.read_bytes()

    pre_entries = {
        "absent": [".build.lock", "unrelated.txt"],
        "pre-existing": [".build.lock", "catalog.json", "index.html",
                         "unrelated.txt"],
    }
    whole_entries = [".build.lock", "catalog.json", "index.html",
                     "unrelated.txt"]

    def run_cli_inside_ambient_exception(root):
        """Run the real CLI from inside a caller's UNRELATED, handled except.

        Returns ``(exit_code, ambient_preserved)``. The ambient error belongs to
        the CALLER, so after the writer returns ``sys.exc_info()`` must still be
        that same handled exception: its own release/close fault may never be
        suppressed by it, and it may never be replaced by it.
        """
        observed = {}

        def caller():
            try:
                raise RuntimeError("ambient caller error")
            except RuntimeError as ambient:
                observed["code"] = _run_cli(root)
                observed["active"] = sys.exc_info()[0] is RuntimeError
                observed["same"] = sys.exc_info()[1] is ambient
            return observed["code"], observed["active"] and observed["same"]

        return caller()

    for mode in ("absent", "pre-existing"):
        # -- lock OPEN fails after both temps exist --------------------------
        label = f"lock-open-{mode}"
        root = lock_root("open", mode)
        identity = lock_identity(root)
        seen, calls = {}, {"replace": 0}
        capsys.readouterr()

        def counting_replace(src, dst):
            calls["replace"] += 1
            real_pair_replace(src, dst)

        real_open = open

        def failing_open(file, *args, **kwargs):
            if str(file).endswith(".build.lock"):
                seen["hit"] = True
                raise OSError("injected lock open failure")
            return real_open(file, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr("builtins.open", failing_open)
            patch.setattr(build_site.os, "replace", counting_replace)
            assert _run_cli(root) == 2, label
        assert seen.get("hit") is True, label
        assert calls["replace"] == 0, label
        _assert_diag(capsys.readouterr(), label, "injected lock open failure")
        _assert_finals(root, mode, label)
        assert site_entries(root) == pre_entries[mode], label
        assert lock_identity(root) == identity, label

        # -- lock ACQUISITION fails: no replacement, no unlock of an
        #    unacquired lock --------------------------------------------------
        label = f"lock-acquire-{mode}"
        root = lock_root("acquire", mode)
        identity = lock_identity(root)
        seen, calls, ops = {}, {"replace": 0}, []
        capsys.readouterr()

        def failing_acquire(fd, operation):
            ops.append(operation)
            if operation == build_site.fcntl.LOCK_EX:
                seen["hit"] = True
                raise OSError("injected lock acquisition failure")
            return real_flock(fd, operation)

        with monkeypatch.context() as patch:
            patch.setattr(build_site.fcntl, "flock", failing_acquire)
            patch.setattr(build_site.os, "replace", counting_replace)
            assert _run_cli(root) == 2, label
        assert seen.get("hit") is True, label
        assert calls["replace"] == 0, label
        assert build_site.fcntl.LOCK_UN not in ops, (label, ops)
        _assert_diag(capsys.readouterr(), label,
                     "injected lock acquisition failure")
        _assert_finals(root, mode, label)
        assert site_entries(root) == pre_entries[mode], label
        assert lock_identity(root) == identity, label

        # -- lock RELEASE fails after a valid publication: exit 2, but the
        #    surviving pair is the COMPLETE new pair (no success is reported) -
        label = f"lock-release-{mode}"
        root = lock_root("release", mode)
        identity = lock_identity(root)
        seen = {}
        capsys.readouterr()

        def failing_release(fd, operation):
            if operation == build_site.fcntl.LOCK_UN:
                seen["hit"] = True
                raise OSError("injected lock release failure")
            return real_flock(fd, operation)

        with monkeypatch.context() as patch:
            patch.setattr(build_site.fcntl, "flock", failing_release)
            assert _run_cli(root) == 2, label
        assert seen.get("hit") is True, label
        _assert_diag(capsys.readouterr(), label, "injected lock release failure")
        assert (root / "site" / "catalog.json").read_bytes().decode("utf-8") == \
            new_catalog, label
        assert (root / "site" / "index.html").read_bytes().decode("utf-8") == \
            new_html, label
        assert site_entries(root) == whole_entries, label
        assert lock_identity(root) == identity, label

        # -- lock CLOSE fails after a valid publication ----------------------
        label = f"lock-close-{mode}"
        root = lock_root("close", mode)
        identity = lock_identity(root)
        seen = {}
        capsys.readouterr()

        def closing_open(file, *args, **kwargs):
            handle = real_open(file, *args, **kwargs)
            if str(file).endswith(".build.lock"):
                seen["hit"] = True
                return _FailingLockFile(handle, seen)
            return handle

        with monkeypatch.context() as patch:
            patch.setattr("builtins.open", closing_open)
            assert _run_cli(root) == 2, label
        assert seen.get("hit") is True, label
        _assert_diag(capsys.readouterr(), label, "injected lock close failure")
        assert (root / "site" / "catalog.json").read_bytes().decode("utf-8") == \
            new_catalog, label
        assert (root / "site" / "index.html").read_bytes().decode("utf-8") == \
            new_html, label
        assert site_entries(root) == whole_entries, label
        assert lock_identity(root) == identity, label

        # -- the SAME release/close faults, except the CLI now runs inside a
        #    CALLER's unrelated handled except block: this writer's own
        #    completion state must decide the outcome, so the caller's ambient
        #    exception can neither suppress the fault into false success nor
        #    become the reported error --------------------------------------
        for kind in ("release", "close"):
            label = f"lock-{kind}-ambient-{mode}"
            root = lock_root(f"{kind}-ambient", mode)
            identity = lock_identity(root)
            seen = {}
            capsys.readouterr()
            if kind == "release":
                def ambient_release(fd, operation):
                    if operation == build_site.fcntl.LOCK_UN:
                        seen["hit"] = True
                        raise OSError("injected lock release failure")
                    return real_flock(fd, operation)

                with monkeypatch.context() as patch:
                    patch.setattr(build_site.fcntl, "flock", ambient_release)
                    code, ambient_preserved = run_cli_inside_ambient_exception(
                        root)
            else:
                def ambient_close(file, *args, **kwargs):
                    handle = real_open(file, *args, **kwargs)
                    if str(file).endswith(".build.lock"):
                        seen["hit"] = True
                        return _FailingLockFile(handle, seen)
                    return handle

                with monkeypatch.context() as patch:
                    patch.setattr("builtins.open", ambient_close)
                    code, ambient_preserved = run_cli_inside_ambient_exception(
                        root)
            assert seen.get("hit") is True, label
            assert ambient_preserved is True, label
            assert code == 2, (label, "ambient exception caused false success")
            _assert_diag(capsys.readouterr(), label,
                         f"injected lock {kind} failure")
            assert (root / "site" / "catalog.json").read_bytes().decode(
                "utf-8") == new_catalog, label
            assert (root / "site" / "index.html").read_bytes().decode(
                "utf-8") == new_html, label
            assert site_entries(root) == whole_entries, label
            assert lock_identity(root) == identity, label

        # -- publication AND release fail: the PRIMARY publication error is
        #    preserved (the release fault never masks it) ---------------------
        label = f"publish-and-release-{mode}"
        root = lock_root("both", mode)
        identity = lock_identity(root)
        seen, calls = {}, {"replace": 0}
        capsys.readouterr()

        def failing_first_replace(src, dst):
            calls["replace"] += 1
            if calls["replace"] == 1:
                seen["replace_hit"] = True
                raise OSError("injected first replacement failure")
            return real_pair_replace(src, dst)

        def unlock_also_fails(fd, operation):
            if operation == build_site.fcntl.LOCK_UN:
                seen["unlock_hit"] = True
                raise OSError("injected lock release failure")
            return real_flock(fd, operation)

        with monkeypatch.context() as patch:
            patch.setattr(build_site.os, "replace", failing_first_replace)
            patch.setattr(build_site.fcntl, "flock", unlock_also_fails)
            assert _run_cli(root) == 2, label
        assert seen.get("replace_hit") is True, label
        assert seen.get("unlock_hit") is True, label
        captured = capsys.readouterr()
        _assert_diag(captured, label, "injected first replacement failure")
        assert "cannot release build lock" not in captured.err, label
        _assert_finals(root, mode, label)
        assert site_entries(root) == pre_entries[mode], label
        assert lock_identity(root) == identity, label

        # -- the SAME dual failure, this time with an unrelated caller
        #    exception active: the writer's OWN publication error must remain
        #    the reported one, never the caller's nor the secondary cleanup ---
        label = f"publish-and-release-ambient-{mode}"
        root = lock_root("both-ambient", mode)
        identity = lock_identity(root)
        seen = {}
        capsys.readouterr()

        def ambient_first_replace(src, dst):
            seen["replace_hit"] = True
            raise OSError("injected first replacement failure")

        def ambient_unlock_also_fails(fd, operation):
            if operation == build_site.fcntl.LOCK_UN:
                seen["unlock_hit"] = True
                raise OSError("injected lock release failure")
            return real_flock(fd, operation)

        with monkeypatch.context() as patch:
            patch.setattr(build_site.os, "replace", ambient_first_replace)
            patch.setattr(build_site.fcntl, "flock", ambient_unlock_also_fails)
            code, ambient_preserved = run_cli_inside_ambient_exception(root)
        assert seen.get("replace_hit") is True, label
        assert seen.get("unlock_hit") is True, label
        assert ambient_preserved is True, label
        assert code == 2, label
        captured = capsys.readouterr()
        _assert_diag(captured, label, "injected first replacement failure")
        assert "cannot release build lock" not in captured.err, label
        assert "ambient caller error" not in captured.err, label
        _assert_finals(root, mode, label)
        assert site_entries(root) == pre_entries[mode], label
        assert lock_identity(root) == identity, label

    # ---- injected replacement failures (first, then second) ---------------
    def injected_root(label):
        root = tmp_path / label
        root.mkdir()
        _seed_logo(root)
        _write_roster(root, PAIR_ROSTER)
        _seed_last_good(root)
        return root

    # First replacement fails: NEITHER target may change.
    root = injected_root("first-replace")
    real_replace = os.replace
    calls = {"n": 0}

    def fail_first(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("injected first replacement failure")
        real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(build_site.os, "replace", fail_first)
        assert _run_cli(root) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip()
    assert calls["n"] == 1
    for name in LAST_GOOD:
        assert (root / "site" / name).read_bytes() == LAST_GOOD[name], name
    assert _site_leftovers(root) == []

    # Second replacement fails: the surviving pair is MIXED, the CLI reports
    # failure (never success) and the pure validator rejects the pair.
    root = injected_root("second-replace")
    calls = {"n": 0}

    def fail_second(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("injected second replacement failure")
        real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(build_site.os, "replace", fail_second)
        assert _run_cli(root) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip()
    assert calls["n"] == 2
    mixed_catalog = (root / "site" / "catalog.json").read_bytes().decode("utf-8")
    assert mixed_catalog == new_catalog
    assert (root / "site" / "index.html").read_bytes() == LAST_GOOD["index.html"]
    with pytest.raises(ValueError):
        validate_artifact_pair(mixed_catalog, LAST_GOOD["index.html"].decode("utf-8"))
    assert _site_leftovers(root) == []

    # Both replacements "succeed" but an interloper spliced a DIFFERENT valid
    # HTML into the target: only the exact-target read-back can catch it.
    root = injected_root("spliced-target")
    other_catalog, other_html = _rendered_other_pair(tmp_path)
    other_tmp = root / "site" / ".other.html.tmp"
    other_tmp.write_bytes(other_html.encode("utf-8"))
    real_replace = os.replace

    def splice(src, dst):
        if str(dst).endswith("index.html"):
            real_replace(other_tmp, dst)
        else:
            real_replace(src, dst)

    with monkeypatch.context() as patch:
        patch.setattr(build_site.os, "replace", splice)
        assert _run_cli(root) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.strip()
    spliced_catalog = (root / "site" / "catalog.json").read_bytes().decode("utf-8")
    spliced_html = (root / "site" / "index.html").read_bytes().decode("utf-8")
    assert spliced_catalog == new_catalog and spliced_html == other_html
    with pytest.raises(ValueError):
        validate_artifact_pair(spliced_catalog, spliced_html)
    assert other_catalog != spliced_catalog
    assert _site_leftovers(root) == []

    # ---- direct writer: a known-bad PREPARED pair never reaches staging -----
    # Both payloads are independently VALID (real builders), but they belong to
    # different snapshots. write_artifact_pair must reject the pair BEFORE it
    # creates a single temp or touches a final path.
    assert validate_artifact_pair(new_catalog, new_html)
    assert validate_artifact_pair(other_catalog, other_html)
    for mode in ("absent", "pre-existing"):
        label = f"known-bad-pair-{mode}"
        root = tmp_path / label
        root.mkdir()
        if mode == "pre-existing":
            _seed_last_good(root)
        staged = {"n": 0}
        real_mkstemp_pair = build_site.tempfile.mkstemp

        def counting_mkstemp(*args, **kwargs):
            staged["n"] += 1
            return real_mkstemp_pair(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(build_site.tempfile, "mkstemp", counting_mkstemp)
            with pytest.raises(build_site._PairWriteError):
                build_site.write_artifact_pair(root, new_catalog, other_html)
        assert staged["n"] == 0, label
        assert _site_leftovers(root) == [], label
        for name in LAST_GOOD:
            target = root / "site" / name
            if mode == "absent":
                assert not target.exists(), (label, name)
            else:
                assert target.read_bytes() == LAST_GOOD[name], (label, name)


def _rendered_other_pair(root):
    """A second, independently VALID pair (different snapshot id)."""
    import build_site
    from catalog_export import capture_registry, serialize_snapshot

    root.mkdir(parents=True, exist_ok=True)
    _seed_logo(root)
    snapshot = build_site.build_snapshot(
        capture_registry(build_site.PROVIDERS, build_site.GATEWAY_WIRING),
        {"tick_epoch": 99, "providers": {"kilo": ["vendor-z/other-free"]}},
        cadence_s=3600)
    return (
        serialize_snapshot(snapshot),
        build_site.render_catalog_page(
            snapshot, "ZmFrZWxvZ28=", tool_contract=_pair_tool_contract()),
    )


def test_concurrent_publication_and_generated_file_ignore_rules(
        tmp_path, monkeypatch):
    """Threads and processes publish whole pairs; only public artifacts track.

    Distinct legitimate snapshots (distinct ids) race for one output directory
    behind a barrier with an injected post-replace delay; the surviving pair
    must be ONE writer's complete pair, with no owned temp leftovers and no
    traceback. The .gitignore exceptions are then read (git check-ignore) with
    read-only commands against the real repo.
    """
    import os
    import threading
    import time

    import build_site
    import catalog_export

    tool_contract = _pair_tool_contract()
    registry = build_site.capture_registry(
        build_site.PROVIDERS, build_site.GATEWAY_WIRING)

    def candidate(epoch):
        snapshot = build_site.build_snapshot(
            registry,
            {"tick_epoch": epoch,
             "providers": {"nous": [f"vendor-z/concurrent-{epoch}-free"]}},
            cadence_s=3600)
        return (
            snapshot["snapshot_id"],
            catalog_export.serialize_snapshot(snapshot),
            build_site.render_catalog_page(
                snapshot, "ZmFrZWxvZ28=", tool_contract=tool_contract),
        )

    candidates = [candidate(epoch) for epoch in (11, 22, 33, 44)]
    assert len({snapshot_id for snapshot_id, _, _ in candidates}) == 4

    def assert_one_complete_pair(root):
        catalog_text = (root / "site" / "catalog.json").read_bytes().decode("utf-8")
        html_text = (root / "site" / "index.html").read_bytes().decode("utf-8")
        matches = [snapshot_id for snapshot_id, catalog, html in candidates
                   if catalog == catalog_text and html == html_text]
        assert len(matches) == 1, "final pair must be one writer's whole pair"
        published = catalog_export.validate_artifact_pair(catalog_text, html_text)
        assert published["snapshot_id"] == matches[0]
        assert _site_leftovers(root) == []
        return matches[0]

    # ---- threads: same process, one output directory ----------------------
    thread_root = tmp_path / "threads"
    (thread_root / "site").mkdir(parents=True)
    barrier = threading.Barrier(len(candidates))
    errors = []
    real_replace = os.replace

    def slow_replace(src, dst):
        real_replace(src, dst)
        time.sleep(0.02)   # widen the window a non-serialized writer would lose

    def worker(catalog_text, html_text):
        try:
            barrier.wait(timeout=10)
            build_site.write_artifact_pair(thread_root, catalog_text, html_text)
        except BaseException as exc:      # noqa: BLE001 - surfaced as a failure
            errors.append(exc)

    with monkeypatch.context() as patch:
        patch.setattr(build_site.os, "replace", slow_replace)
        threads = [threading.Thread(target=worker, args=(catalog, html))
                   for _, catalog, html in candidates]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
    assert errors == [], errors
    assert_one_complete_pair(thread_root)

    # ---- independent processes: one output directory ----------------------
    process_root = tmp_path / "processes"
    (process_root / "site").mkdir(parents=True)
    payloads = tmp_path / "payloads"
    payloads.mkdir()
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from pathlib import Path; import build_site;"
        "build_site.write_artifact_pair(Path(sys.argv[2]),"
        " Path(sys.argv[3]).read_bytes().decode('utf-8'),"
        " Path(sys.argv[4]).read_bytes().decode('utf-8'))")
    procs = []
    for index, (_, catalog_text, html_text) in enumerate(candidates):
        catalog_file = payloads / f"c{index}.json"
        catalog_file.write_text(catalog_text, encoding="utf-8")
        html_file = payloads / f"h{index}.html"
        html_file.write_text(html_text, encoding="utf-8")
        procs.append(subprocess.Popen(
            [sys.executable, "-c", script, str(REPO), str(process_root),
             str(catalog_file), str(html_file)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    for proc in procs:
        out, err = proc.communicate(timeout=90)
        assert proc.returncode == 0, err
        assert out == "" and "Traceback" not in err
    assert_one_complete_pair(process_root)

    # ---- generated-file ignore rules (read-only) --------------------------
    ignore_lines = [line.strip() for line in
                    (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert [line for line in ignore_lines if line.startswith("!")] == [
        "!site/index.html", "!site/catalog.json"]

    def ignored(relative):
        proc = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "--no-index", "-q", relative],
            capture_output=True, text=True)
        assert proc.returncode in (0, 1), proc.stderr
        return proc.returncode == 0

    for public in ("site/index.html", "site/catalog.json"):
        assert not ignored(public), public
    for generated in ("site/.build.lock", "site/index.html.4242.99.tmp",
                      "site/catalog.json.4242.99.tmp", "site/other-public.json",
                      "state/roster.json"):
        assert ignored(generated), generated
