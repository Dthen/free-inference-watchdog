"""Runtime tests for the pure raw-JSON boundary and typed canonical bytes
(unit 2a1).

These tests exercise ONLY the public runtime API of the side-effect-free
package root and its pure raw module:

    watchdog_catalog.catalog.MAX_SAFE_INT
    watchdog_catalog.catalog.MAX_JSON_DEPTH
    watchdog_catalog.catalog.CatalogError
    watchdog_catalog.catalog.load_json
    watchdog_catalog.catalog.canonical_bytes

They read the frozen, independently-authored RULE TABLES by path
(``tests/fixtures/catalog/raw-json-cases.json`` and ``canonical-cases.json``)
and compare each literal row against the runtime functions. They never import
``tests/test_catalog.py`` or any of its private checkers, and no production
module imports them.

Structural snapshot validation, provenance/hashing policy and the
``invalid_snapshot`` / ``unsupported_schema_version`` envelopes are LATER
units: this file proves only the raw lexical boundary and the typed canonical
bytes and therefore never manufactures a structural envelope. Grouped loops
keep the added test count proportional to the coherent boundaries rather than
to the individual table rows.

The real package is imported LAZILY (inside the test bodies, via
:func:`_catalog`). pytest imports every test module during COLLECTION, so a
module-level ``import watchdog_catalog`` would place the package in
``sys.modules`` before the frozen contract-only suite's data-only guards
(``test_*_adds_*_without_package_code``) run; those guards assert the package
is absent, and this unit must not weaken or reorder them. Resolving the runtime
inside the test bodies preserves genuine import-failure RED while leaving each
guard's "no implementation module imported by that file" assertion truthfully
intact.
"""

import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"
RAW_TABLE_DOC = FIXTURE_DIR / "raw-json-cases.json"
CANONICAL_TABLE_DOC = FIXTURE_DIR / "canonical-cases.json"

INVALID_JSON = "invalid_json"
INVALID_JSON_MESSAGE = "Invalid catalogue JSON."

_CATALOG = None


def _catalog():
    """The real pure runtime module, imported on first use (see module doc).

    Deferring this import keeps ``sys.modules`` free of ``watchdog_catalog*``
    while the earlier contract-only suite runs; the genuine import-failure RED
    is preserved because every test resolves the runtime before asserting.
    """
    global _CATALOG
    if _CATALOG is None:
        import watchdog_catalog.catalog as _runtime
        _CATALOG = _runtime
    return _CATALOG


# The five strict-float snapshot-case rows and the literal path of the offending
# float token; unit 2a2 owns their ``invalid_snapshot`` classification.
STRICT_FLOAT_PATH = {
    "snap_case_schema_version_float": ("schema_version",),
    "snap_case_tick_epoch_float": ("tick_epoch",),
    "snap_case_cadence_float": ("cadence_s",),
    "snap_case_context_length_float": (
        "providers", "synthetic-a", "models", "alpha-1:free", "context_length"),
    "snap_case_schema_version_exponent": ("schema_version",),
}
STRICT_INT_PATHS = (
    ("schema_version",),
    ("tick_epoch",),
    ("cadence_s",),
    ("providers", "synthetic-a", "models", "alpha-1:free", "context_length"),
)


def _load_table(path):
    path = Path(path)
    assert path.is_file(), f"rule table fixture is missing: {path.name}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw_table():
    return _load_table(RAW_TABLE_DOC)


@pytest.fixture(scope="module")
def canonical_table():
    return _load_table(CANONICAL_TABLE_DOC)


def _row(rows, case_id):
    return next(r for r in rows if r["id"] == case_id)


def _doc_at(doc, path):
    node = doc
    for component in path:
        node = node[component]
    return node


def _decoded_depth(value):
    """The container depth of a freshly decoded value: a root container is
    depth 1, each nested array/object adds 1, a scalar is depth 0."""
    if isinstance(value, dict):
        inner = [_decoded_depth(v) for v in value.values()] or [0]
        return 1 + max(inner)
    if isinstance(value, list):
        inner = [_decoded_depth(v) for v in value] or [0]
        return 1 + max(inner)
    return 0


# -- literal depth builders (grouped rows; not one helper per row) --

def _nested_array(n):
    return "[" * n + "0" + "]" * n


def _nested_object(n):
    return '{"a":' * n + "0" + "}" * n


def _nested_mixed(n):
    opens, closes = [], []
    for i in range(n):
        if i % 2 == 0:
            opens.append("[")
            closes.append("]")
        else:
            opens.append('{"a":')
            closes.append("}")
    return "".join(opens) + "0" + "".join(reversed(closes))


def _assert_invalid(text, cid=None):
    catalog = _catalog()
    with pytest.raises(catalog.CatalogError) as excinfo:
        catalog.load_json(text)
    err = excinfo.value
    assert err.code == INVALID_JSON, cid or text[:40]
    assert err.message == INVALID_JSON_MESSAGE, cid or text[:40]
    return err


# ---------- raw boundary: frozen rule-table rows ----------

def test_raw_invalid_rows_reject_with_the_single_literal_envelope(raw_table):
    assert raw_table["invalid_cases"], "the raw rule table must carry rows"
    assert len(raw_table["invalid_cases"]) == 35
    for row in raw_table["invalid_cases"]:
        cid = row["id"]
        err = _assert_invalid(row["json_text"], cid)
        # str() is the fixed public message, never decoder text or raw content.
        assert str(err) == INVALID_JSON_MESSAGE, cid
        assert err.to_payload() == row["expect"], cid
        # Exactly ok+error and exactly code+message; a fresh object each call.
        payload = err.to_payload()
        assert set(payload) == {"ok", "error"}, cid
        assert payload["ok"] is False, cid
        assert set(payload["error"]) == {"code", "message"}, cid
        assert err.to_payload() is not err.to_payload(), cid


def test_raw_valid_rows_decode_to_expected_typed_values(raw_table):
    assert raw_table["valid_controls"], "the raw rule table must carry rows"
    assert len(raw_table["valid_controls"]) == 27
    catalog = _catalog()
    load_json = catalog.load_json
    for row in raw_table["valid_controls"]:
        cid = row["id"]
        decoded = load_json(row["json_text"])
        if "expect" in row:
            expected = row["expect"]
            assert decoded == expected, cid
            # Strict per-token type: an integral float token stays float.
            assert type(decoded) is type(expected), cid
        else:
            assert row["expect_depth"] == catalog.MAX_JSON_DEPTH, cid
            assert _decoded_depth(decoded) == catalog.MAX_JSON_DEPTH, cid
            if row["expect_kind"] == "object":
                assert isinstance(decoded, dict), cid
            else:
                assert isinstance(decoded, list), cid
    # Grouped strict-type pins: integral float tokens keep FLOAT type and a
    # signed float zero retains its sign.
    assert type(load_json("1.0")) is float
    assert type(load_json("1e0")) is float
    zero = load_json("-0.0")
    assert type(zero) is float
    assert zero == 0.0 and math.copysign(1.0, zero) == -1.0


def test_raw_numeric_safety_and_duplicate_policy_are_notation_independent():
    catalog = _catalog()
    load_json = catalog.load_json
    assert catalog.MAX_SAFE_INT == 9007199254740991
    # The safe range is inclusive at both ends.
    assert load_json("9007199254740991") == catalog.MAX_SAFE_INT
    assert load_json("-9007199254740991") == -catalog.MAX_SAFE_INT
    # Unsafe integers reject in EVERY notation, at every depth.
    for text in ("9007199254740992", "-9007199254740992",
                 "9007199254740992.0", "-9007199254740992.0",
                 "1e16", "-1e16", '{"a": [{"b": 9007199254740993}]}'):
        _assert_invalid(text)
    # Non-finite constants and overflowing numbers reject.
    for text in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400"):
        _assert_invalid(text)
    # Numeric-looking strings are untouched, never inspected as numbers.
    assert load_json('"9007199254740992"') == "9007199254740992"
    assert load_json('{"n": "NaN"}') == {"n": "NaN"}
    # A duplicate member name rejects at EVERY object level, including
    # decoded escape-equivalent keys.
    for text in ('{"a":1,"a":2}', '{"a":1,"\\u0061":2}',
                 '{"x":{"b":1,"b":2}}', '[{"c":1,"c":1}]',
                 '{"a":1,"b":{"c":1,"c":2}}'):
        _assert_invalid(text)
    # Different case is distinct; a name repeated in separate objects is fine.
    assert load_json('{"A": 1, "a": 2}') == {"A": 1, "a": 2}
    assert load_json('{"x":{"a":1},"y":{"a":2}}') == \
        {"x": {"a": 1}, "y": {"a": 2}}
    # Ordinary positive control.
    assert load_json('{"a": 0.5, "b": [1, 2.5]}') == {"a": 0.5, "b": [1, 2.5]}


def test_raw_container_depth_bound_is_inclusive_and_huge_depth_rejected():
    catalog = _catalog()
    load_json = catalog.load_json
    assert catalog.MAX_JSON_DEPTH == 64
    for build in (_nested_array, _nested_object, _nested_mixed):
        load_json(build(64))  # accepted at the inclusive bound
        _assert_invalid(build(65))
    # Quoted brackets never count; a root scalar is depth 0.
    quoted = '"' + "[" * 70 + '"'
    assert load_json(quoted) == "[" * 70
    assert load_json("42") == 42
    # A 5000-deep document rejects through the preflight without touching the
    # interpreter recursion limit.
    before = sys.getrecursionlimit()
    _assert_invalid("[" * 5000 + "0" + "]" * 5000)
    assert sys.getrecursionlimit() == before


def test_raw_huge_integer_lexeme_rejects_without_changing_int_limits():
    load_json = _catalog().load_json
    huge = "9" * 5000
    int_before = sys.get_int_max_str_digits()
    rec_before = sys.getrecursionlimit()
    for text in (huge, "-" + huge, '{"a": [' + huge + "]}", "1" + "0" * 5000):
        _assert_invalid(text)
    # No sys.set_int_max_str_digits / recursion-limit change was made.
    assert sys.get_int_max_str_digits() == int_before
    assert sys.getrecursionlimit() == rec_before
    # Ordinary integers still load.
    assert load_json("9007199254740991") == 9007199254740991
    assert load_json("-12345") == -12345


# ---------- typed canonical bytes: frozen rule-table rows ----------

def test_canonical_vectors_match_the_literal_text_and_sha(canonical_table):
    catalog = _catalog()
    load_json = catalog.load_json
    canonical_bytes = catalog.canonical_bytes
    assert canonical_table["canonical_vectors"], "the table must carry rows"
    assert len(canonical_table["canonical_vectors"]) == 14
    for row in canonical_table["canonical_vectors"]:
        cid = row["id"]
        decoded = load_json(row["json_text"])
        if row["scope"] == "snapshot":
            # The caller excludes ONLY the root snapshot_id.
            body = {k: v for k, v in decoded.items() if k != "snapshot_id"}
        else:
            body = decoded
        raw = canonical_bytes(body)
        assert raw == row["canonical_text"].encode("utf-8"), cid
        assert hashlib.sha256(raw).hexdigest() == row["sha256"], cid


def test_canonical_lexeme_types_and_float_priced_snapshot_twins(canonical_table):
    catalog = _catalog()
    load_json = catalog.load_json
    canonical_bytes = catalog.canonical_bytes
    vec = {r["id"]: r for r in canonical_table["canonical_vectors"]}
    i1 = load_json(vec["vec_int_lexeme_one"]["json_text"])["n"]
    f1 = load_json(vec["vec_float_lexeme_one"]["json_text"])["n"]
    fe = load_json(vec["vec_float_lexeme_one_exponent"]["json_text"])["n"]
    assert type(i1) is int and type(f1) is float and type(fe) is float
    # Integer `1` and float `1.0` are DIFFERENT typed values.
    assert canonical_bytes({"n": i1}) != canonical_bytes({"n": f1})
    # Float `1.0` and float `1e0` share ONE typed value.
    assert canonical_bytes({"n": f1}) == canonical_bytes({"n": fe})
    # Escaped and literal escapes collapse to one canonical body.
    esc = load_json(vec["vec_equivalent_escapes_escaped"]["json_text"])
    lit = load_json(vec["vec_equivalent_escapes_literal"]["json_text"])
    assert canonical_bytes(esc) == canonical_bytes(lit)
    # A nested snapshot_id in a general_value vector is RETAINED content.
    nested = load_json(vec["vec_nested_snapshot_id"]["json_text"])
    assert b'"snapshot_id"' in canonical_bytes(nested)
    # Float-priced snapshot twins: raw-accepted, identical body and hash, and
    # float pricing kept as floats (never dropped or stringified).
    prod = load_json(vec["vec_snapshot_float_priced_producer"]["json_text"])
    alt = load_json(vec["vec_snapshot_float_priced_alternate"]["json_text"])
    prod_body = canonical_bytes(
        {k: v for k, v in prod.items() if k != "snapshot_id"})
    alt_body = canonical_bytes(
        {k: v for k, v in alt.items() if k != "snapshot_id"})
    assert prod_body == alt_body
    assert prod["snapshot_id"] == alt["snapshot_id"] == \
        hashlib.sha256(prod_body).hexdigest()
    price = prod["providers"]["synthetic-a"]["models"]["alpha-1:free"]["pricing"]
    assert type(price["prompt"]) is float
    assert type(price["completion"]) is float


def test_canonical_snapshot_cases_are_raw_accepted_with_hash_preconditions(
        canonical_table):
    catalog = _catalog()
    load_json = catalog.load_json
    canonical_bytes = catalog.canonical_bytes
    assert canonical_table["snapshot_cases"], "the table must carry rows"
    assert len(canonical_table["snapshot_cases"]) == 7
    for row in canonical_table["snapshot_cases"]:
        cid = row["id"]
        decoded = load_json(row["json_text"])  # raw guard accepts the numbers
        assert isinstance(decoded, dict), cid
        recomputed = hashlib.sha256(canonical_bytes(
            {k: v for k, v in decoded.items() if k != "snapshot_id"})).hexdigest()
        if cid == "snap_case_hash_tamper":
            assert decoded["snapshot_id"] != recomputed, cid
        else:
            assert decoded["snapshot_id"] == recomputed, cid
        if "expect" in row:
            # Structural classification is deferred to unit 2a2: the raw layer
            # accepts these and produces NO envelope itself.
            assert row["expect"]["error"]["code"] == "invalid_snapshot", cid
    # The strict-token inputs load with FLOAT tokens intact (never coerced to
    # int) -- the precondition unit 2a2 will classify invalid_snapshot.
    for cid, path in STRICT_FLOAT_PATH.items():
        row = _row(canonical_table["snapshot_cases"], cid)
        value = _doc_at(load_json(row["json_text"]), path)
        assert type(value) is float, cid
        assert value == int(value), cid
    # The accepted base case carries strict INT tokens.
    base = _row(canonical_table["snapshot_cases"], "snap_case_integer_token_base")
    doc = load_json(base["json_text"])
    for path in STRICT_INT_PATHS:
        assert type(_doc_at(doc, path)) is int, path


def test_canonical_bytes_encoder_flags_and_no_key_exclusion():
    catalog = _catalog()
    canonical_bytes = catalog.canonical_bytes
    # The documented public names hang off the submodule exactly as imported.
    assert catalog.load_json is catalog.load_json
    assert catalog.canonical_bytes is canonical_bytes
    # Compact separators and code-point key sorting.
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    # ensure_ascii=True escapes non-ASCII.
    assert canonical_bytes({"s": "caf\u00e9"}) == b'{"s":"caf\\u00e9"}'
    # The generic encoder excludes NO keys, including a root snapshot_id.
    assert b'"snapshot_id"' in canonical_bytes({"snapshot_id": "deadbeef", "x": 1})
    # Native non-finite Python inputs are rejected, never emitted as bare NaN.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_bytes({"x": bad})


# ---------- import isolation: silent, side-effect-free package root ----------

_PROBE = r'''
import json, os, sys

forbidden_prefixes = json.loads(os.environ["WC_FORBIDDEN"])
data_patterns = json.loads(os.environ["WC_DATA_PATTERNS"])

forbidden_imports = []
application_opens = []
network_events = []


def _audit(name, args):
    if name == "import":
        module = args[0]
        for prefix in forbidden_prefixes:
            if module == prefix or module.startswith(prefix + "."):
                forbidden_imports.append(module)
                return
    elif name == "open":
        path = args[0]
        if isinstance(path, str):
            norm = path.replace("\\", "/")
            for pattern in data_patterns:
                if pattern in norm:
                    application_opens.append(norm)
                    return
    elif name.startswith("socket."):
        network_events.append(name)


sys.addaudithook(_audit)

import watchdog_catalog
import watchdog_catalog.catalog

assert "watchdog_catalog" in sys.modules
assert "watchdog_catalog.catalog" in sys.modules

# Belt-and-suspenders: no forbidden module is resident, whether freshly
# imported or already pre-loaded before the audit hook was installed.
leaked_modules = [
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".")
           for prefix in forbidden_prefixes)
]

sys.stdout.write(json.dumps({
    "forbidden_imports": forbidden_imports,
    "leaked_modules": leaked_modules,
    "application_opens": application_opens,
    "network_events": network_events,
}))
'''


def test_package_import_is_silent_and_side_effect_free(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "neutral-cwd"
    cwd.mkdir()
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # (a) A plain import from a neutral cwd and clean HOME is completely silent.
    plain = subprocess.run(
        [sys.executable, "-B", "-c",
         "import watchdog_catalog, watchdog_catalog.catalog"],
        cwd=str(cwd), env=base_env, capture_output=True, text=True, timeout=120)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == "", plain.stdout
    assert plain.stderr == "", plain.stderr

    # (b) An audit-hooked import touches no operator module, application data
    #     file or network facility. Mandatory module source/bytecode reads are
    #     allowed; only application data and operator/SDK imports are trapped.
    forbidden = [
        "mcp", "mcp_server", "build_site", "state", "config_loader",
        "providers", "inference_watchdog", "notify", "envfile", "diffing",
        "detection", "alive", "pending_removals", "probe_select",
        "probe_state", "probe_zero_credit", "anthropic", "httpx", "requests",
        "watchdog_catalog.query", "watchdog_catalog.server",
        "watchdog_catalog.adapter",
    ]
    data_patterns = ["/.env", "roster", "tools.json", "catalog.json",
                     "/state/", "/providers/"]
    env = dict(base_env)
    env["WC_FORBIDDEN"] = json.dumps(forbidden)
    env["WC_DATA_PATTERNS"] = json.dumps(data_patterns)
    probe = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr
    report = json.loads(probe.stdout)
    assert report["forbidden_imports"] == [], report["forbidden_imports"]
    assert report["leaked_modules"] == [], report["leaked_modules"]
    assert report["application_opens"] == [], report["application_opens"]
    assert report["network_events"] == [], report["network_events"]
