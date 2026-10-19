"""Runtime tests for the pure raw-JSON boundary, typed canonical bytes (unit
2a1) and the strict public snapshot validator / hash provenance (unit 2a2).

These tests exercise ONLY the public runtime API of the side-effect-free
package root and its pure raw + structural + query modules:

    watchdog_catalog.catalog.MAX_SAFE_INT
    watchdog_catalog.catalog.MAX_JSON_DEPTH
    watchdog_catalog.catalog.CatalogError
    watchdog_catalog.catalog.load_json
    watchdog_catalog.catalog.canonical_bytes
    watchdog_catalog.catalog.INVALID_SNAPSHOT
    watchdog_catalog.catalog.UNSUPPORTED_SCHEMA_VERSION
    watchdog_catalog.catalog.validate_snapshot
    watchdog_catalog.catalog.load_snapshot
    watchdog_catalog.catalog.snapshot_hash
    watchdog_catalog.query.strip_free_marker

They read the frozen, independently-authored RULE TABLES by path
(``tests/fixtures/catalog/raw-json-cases.json``, ``canonical-cases.json`` and
``structural-cases.json``) plus the committed snapshot fixtures, and compare
each literal row against the runtime functions. They never import
``tests/test_catalog.py`` or any of its private checkers, and no production
module imports them.

The structural layer is exercised through a SMALL TEST-LOCAL path setter and
reseal helper built on independent ``json.dumps``/``hashlib`` calls: expected
envelopes come from the literal table rows, never from runtime-derived hashes,
and every fixture file stays byte-identical. Grouped loops keep the added test
count proportional to the coherent boundaries rather than to the individual
table rows.

The real package is imported normally at module level. The frozen
contract-only suite proves its own isolation in a fresh interpreter (with the
implementation package blocked) rather than by inspecting this process's
``sys.modules``, so this module needs no lazy import and must not use one to
keep the process-global module table clean.
"""

import collections
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from watchdog_catalog import catalog as _catalog

REPO = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"
RAW_TABLE_DOC = FIXTURE_DIR / "raw-json-cases.json"
CANONICAL_TABLE_DOC = FIXTURE_DIR / "canonical-cases.json"
STRUCTURAL_TABLE_DOC = FIXTURE_DIR / "structural-cases.json"

INVALID_JSON = "invalid_json"
INVALID_JSON_MESSAGE = "Invalid catalogue JSON."
INVALID_SNAPSHOT = "invalid_snapshot"
UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
STRUCTURAL_MESSAGES = {
    INVALID_SNAPSHOT: "Invalid catalogue snapshot.",
    UNSUPPORTED_SCHEMA_VERSION: "Unsupported catalogue schema version.",
}

# The nine committed public snapshot fixtures: every one of them is a valid
# publication and must be accepted, unchanged, by the structural layer.
SNAPSHOT_FIXTURES = (
    "basic.json",
    "capability-unknown.json",
    "lookup.json",
    "metadata-edge-cases.json",
    "ordering.json",
    "provider-extension.json",
    "search-text.json",
    "status-short-cadence.json",
    "status-unknown-time.json",
)

# The five strict-float snapshot-case rows and the literal path of the offending
# float token; this unit's ``load_snapshot`` classifies their
# ``invalid_snapshot`` envelope.
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
    catalog = _catalog
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
    catalog = _catalog
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
    catalog = _catalog
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
    catalog = _catalog
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
    load_json = _catalog.load_json
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
    catalog = _catalog
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
    catalog = _catalog
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
    catalog = _catalog
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
            # Structural classification stays a SEPARATE layer: the raw layer
            # accepts these and produces NO envelope itself, and this unit's
            # load_snapshot is what composes raw acceptance with the structural
            # envelope asserted in the structural group below.
            assert row["expect"]["error"]["code"] == "invalid_snapshot", cid
    # The strict-token inputs load with FLOAT tokens intact (never coerced to
    # int) -- the precondition the structural layer classifies invalid_snapshot.
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
    catalog = _catalog
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


# ---------- structural validation: literal table rows and fixtures ----------

# Small TEST-LOCAL change applier + reseal helper. Independent stdlib only
# (json/hashlib); no ``tests/test_catalog.py`` private helper is imported and no
# production module is used to build the expected value.


def _decode_doc(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _canonical_text(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _reseal(snap):
    """Reseal a mutated in-memory copy: `snapshot_id` becomes the canonical
    body hash, so hash equality is TRUE before the semantic defect is tested."""
    body = {k: v for k, v in snap.items() if k != "snapshot_id"}
    snap["snapshot_id"] = hashlib.sha256(_canonical_text(body)).hexdigest()
    return snap


def _apply_changes(snap, changes):
    for change in changes:
        op = change["op"]
        if op == "replace_root":
            snap = change["value"]
            continue
        path = change["path"]
        parent = snap
        for component in path[:-1]:
            parent = parent[component]
        if op == "delete":
            del parent[path[-1]]
        else:
            assert op == "set", op
            parent[path[-1]] = change["value"]
    return snap


def _structural_snapshot(row):
    snap = _apply_changes(_decode_doc(FIXTURE_DIR / row["base_fixture"]),
                          row["changes"])
    return _reseal(snap) if row["reseal"] else snap


def _mutate(base, path, value):
    """A fresh copy of `base` with one literal slot replaced."""
    snap = json.loads(json.dumps(base))
    parent = snap
    for component in path[:-1]:
        parent = parent[component]
    parent[path[-1]] = value
    return snap


def _assert_rejected(snapshot, code, cid):
    catalog = _catalog
    with pytest.raises(catalog.CatalogError) as excinfo:
        catalog.validate_snapshot(snapshot)
    err = excinfo.value
    expect = {"ok": False,
              "error": {"code": code, "message": STRUCTURAL_MESSAGES[code]}}
    assert err.code == code, cid
    assert err.message == STRUCTURAL_MESSAGES[code], cid
    assert str(err) == STRUCTURAL_MESSAGES[code], cid
    assert err.to_payload() == expect, cid
    payload = err.to_payload()
    assert set(payload) == {"ok", "error"}, cid
    assert payload["ok"] is False, cid
    assert set(payload["error"]) == {"code", "message"}, cid
    assert err.code != INVALID_JSON, cid
    return err


@pytest.fixture(scope="module")
def structural_table():
    return _decode_doc(STRUCTURAL_TABLE_DOC)


def test_structural_error_literals_and_envelopes_are_frozen():
    catalog = _catalog
    assert catalog.INVALID_SNAPSHOT == INVALID_SNAPSHOT
    assert catalog.UNSUPPORTED_SCHEMA_VERSION == UNSUPPORTED_SCHEMA_VERSION
    for code, message in STRUCTURAL_MESSAGES.items():
        err = catalog.CatalogError(code)
        assert err.code == code
        assert err.message == message
        assert str(err) == message
        payload = err.to_payload()
        assert payload == {"ok": False,
                           "error": {"code": code, "message": message}}
        assert set(payload) == {"ok", "error"}
        # Never an argument-style `parameter`, never a raw value or path.
        assert set(payload["error"]) == {"code", "message"}
        assert payload["ok"] is False
        assert err.to_payload() is not err.to_payload()
    # The DEFAULT code stays the raw one, so the 2a1 surface is unchanged.
    assert catalog.CatalogError().code == INVALID_JSON


def test_structural_invalid_rows_reject_with_the_frozen_envelope(
        structural_table):
    rows = structural_table["invalid_cases"]
    assert len(rows) == 57
    for row in rows:
        snap = _structural_snapshot(row)
        if row["reseal"]:
            body = {k: v for k, v in snap.items() if k != "snapshot_id"}
            assert snap["snapshot_id"] == \
                hashlib.sha256(_canonical_text(body)).hexdigest(), row["id"]
        err = _assert_rejected(snap, row["expect"]["error"]["code"], row["id"])
        # The literal, independently-authored envelope is reproduced exactly.
        assert err.to_payload() == row["expect"], row["id"]


def test_structural_valid_controls_are_accepted_identically_and_unmutated(
        structural_table):
    rows = structural_table["valid_controls"]
    assert len(rows) == 10
    catalog = _catalog
    for row in rows:
        snap = _structural_snapshot(row)
        before = json.loads(json.dumps(snap))
        assert catalog.validate_snapshot(snap) is snap, row["id"]
        assert snap == before, row["id"]
        assert catalog.load_snapshot(json.dumps(snap)) == snap, row["id"]


def test_committed_snapshot_fixtures_validate_identically():
    catalog = _catalog
    for name in SNAPSHOT_FIXTURES:
        text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
        doc = catalog.load_json(text)
        before = json.loads(json.dumps(doc))
        assert catalog.validate_snapshot(doc) is doc, name
        assert doc == before, name
        assert catalog.load_snapshot(text) == doc, name


def test_canonical_snapshot_cases_classify_via_load_snapshot(canonical_table):
    rows = canonical_table["snapshot_cases"]
    assert len(rows) == 7
    catalog = _catalog
    base = _row(rows, "snap_case_integer_token_base")
    accepted = catalog.load_snapshot(base["json_text"])
    assert isinstance(accepted, dict) and base.get("expect") is None
    strict_float = 0
    for row in rows:
        if row["id"] == "snap_case_integer_token_base":
            continue
        if row["id"] in STRICT_FLOAT_PATH:
            strict_float += 1
        with pytest.raises(catalog.CatalogError) as excinfo:
            catalog.load_snapshot(row["json_text"])
        err = excinfo.value
        assert err.code == INVALID_SNAPSHOT, row["id"]
        assert err.to_payload() == row["expect"], row["id"]
    assert strict_float == 5
    # The tampered row is a genuine body/hash mismatch, not a shape accident.
    tamper = _row(rows, "snap_case_hash_tamper")
    doc = catalog.load_json(tamper["json_text"])
    body = {k: v for k, v in doc.items() if k != "snapshot_id"}
    assert doc["snapshot_id"] != catalog.snapshot_hash(body)


def test_raw_faults_precede_structural_validation(raw_table):
    catalog = _catalog
    for row in raw_table["invalid_cases"]:
        with pytest.raises(catalog.CatalogError) as excinfo:
            catalog.load_snapshot(row["json_text"])
        err = excinfo.value
        assert err.code == INVALID_JSON, row["id"]
        assert err.to_payload() == row["expect"], row["id"]


def test_raw_valid_documents_reach_the_structural_layer(raw_table):
    catalog = _catalog
    non_objects = 0
    for row in raw_table["valid_controls"]:
        decoded = catalog.load_json(row["json_text"])  # lexically accepted
        if not isinstance(decoded, dict):
            non_objects += 1
        with pytest.raises(catalog.CatalogError) as excinfo:
            catalog.load_snapshot(row["json_text"])
        assert excinfo.value.code == INVALID_SNAPSHOT, row["id"]
    assert non_objects, "the raw table must carry non-object roots"
    for text in ("null", "[]", "true", "42", "1.5", '"x"',
                 "-9007199254740991", "{}", '{"A":1,"a":2}'):
        _assert_rejected(catalog.load_json(text), INVALID_SNAPSHOT, text)


def test_structural_precedence_matrix_on_native_values():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")

    # (a) Non-object roots, including tuple/bytes/UserDict, never escape.
    for root in (None, [], "x", 1, 1.5, True, (), b"{}",
                 collections.UserDict(base)):
        _assert_rejected(root, INVALID_SNAPSHOT, f"root {root!r}")

    # (b) Wrong required-field set both ways.
    for mutate in (lambda s: s.pop("cadence_s"),
                   lambda s: s.__setitem__("surprise", 1),
                   lambda s: s.pop("snapshot_id")):
        snap = json.loads(json.dumps(base))
        mutate(snap)
        _assert_rejected(snap, INVALID_SNAPSHOT, "field set")

    # (c) A non-strict-integer version TYPE is invalid_snapshot.
    for value in (True, False, "1", None, 1.0, 1e0):
        _assert_rejected(_mutate(base, ("schema_version",), value),
                         INVALID_SNAPSHOT, f"version type {value!r}")

    # (d) A well-typed integer version != 1 is unsupported_schema_version EVEN
    #     with a malformed body and a wrong hash: the value check precedes
    #     every v1 body check and the hash.
    for value in (2, 0, -1, catalog.MAX_SAFE_INT):
        snap = _mutate(base, ("schema_version",), value)
        snap["providers"] = []
        snap["snapshot_id"] = "0" * 64
        _assert_rejected(snap, UNSUPPORTED_SCHEMA_VERSION, f"version {value}")

    # (e) Version 1: a malformed body, a wrong 64-hex hash and a malformed hash
    #     are each invalid_snapshot.
    _assert_rejected(_mutate(base, ("providers",), []),
                     INVALID_SNAPSHOT, "malformed body")
    _assert_rejected(_mutate(base, ("snapshot_id",), "0" * 64),
                     INVALID_SNAPSHOT, "wrong 64-hex hash")
    _assert_rejected(_mutate(base, ("snapshot_id",), "not-a-hash"),
                     INVALID_SNAPSHOT, "malformed hash")
    _assert_rejected(_mutate(base, ("provider_order",), ["synthetic-a"]),
                     INVALID_SNAPSHOT, "order/map mismatch")

    # (f) Accepting control: the untouched snapshot is returned BY IDENTITY.
    assert catalog.validate_snapshot(base) is base


def test_structural_native_body_guards_reject_without_incidental_errors():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")
    model = ("providers", "synthetic-a", "models", "vendor/shared")
    cases = (
        (("providers",), []),
        (("providers", "synthetic-a"), "oops"),
        ((model[0], model[1], "models"), []),
        (model + ("architecture",), []),
        (model + ("pricing",), []),
        (("warnings",), {}),
        (("providers",), {1: {}}),
        (("providers", "synthetic-a", "models"), {7: {}}),
        (("tick_epoch",), float("nan")),
        (("tick_epoch",), float("inf")),
        (("tick_epoch",), catalog.MAX_SAFE_INT + 1),
        (("cadence_s",), float("nan")),
        (("cadence_s",), 0),
        (("cadence_s",), catalog.MAX_SAFE_INT + 1),
        (("cadence_s",), 2 ** 200),
        (("cadence_s",), 3600.0),
        (model + ("context_length",), float("nan")),
        (model + ("context_length",), catalog.MAX_SAFE_INT + 1),
        (model + ("context_length",), True),
        (model + ("pricing", "prompt"), float("nan")),
        (model + ("pricing", "prompt"), float("inf")),
        (model + ("pricing", "prompt"), catalog.MAX_SAFE_INT + 1),
    )
    for path, value in cases:
        snap = _mutate(base, path, value)
        # Reseal so the hash is consistent and ONLY the body guard can reject:
        # a stale snapshot_id would let a dropped numeric bound hide behind it.
        _reseal(snap)
        _assert_rejected(snap, INVALID_SNAPSHOT, f"{path} = {value!r}")

    # A non-JSON native in an otherwise-shaped slot rejects as invalid_snapshot
    # and is left EXACTLY as supplied -- never repaired, coerced or replaced.
    for field in ("providers", "warnings", "tick_epoch", "cadence_s",
                  "provider_order", "snapshot_id"):
        marker = object()
        snap = json.loads(json.dumps(base))
        snap[field] = marker
        _assert_rejected(snap, INVALID_SNAPSHOT, f"{field} = object()")
        assert snap[field] is marker, field


def test_structural_generic_registry_shapes_and_identifiers_are_accepted():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")

    # (a) Identifier namespaces: a literal `auth` provider key and literal
    #     `__proto__`/`constructor`/`auth` raw tracked ids are LEGAL.
    snap = json.loads(json.dumps(base))
    entry = snap["providers"].pop("synthetic-b")
    for index, raw_id in enumerate(list(entry["model_ids"])):
        replacement = ("__proto__", "constructor")[index]
        model = entry["models"].pop(raw_id)
        model["id"] = replacement
        entry["models"][replacement] = model
    entry["model_ids"] = sorted(entry["models"])
    snap["providers"]["auth"] = entry
    snap["provider_order"] = ["synthetic-a", "auth", "synthetic-c"]
    _reseal(snap)
    assert catalog.validate_snapshot(snap) is snap
    assert snap["providers"]["auth"]["model_ids"] == ["__proto__", "constructor"]

    # (b) A legitimately empty registry.
    snap = json.loads(json.dumps(base))
    snap["provider_order"] = []
    snap["providers"] = {}
    _reseal(snap)
    assert catalog.validate_snapshot(snap) is snap

    # (c) A registry in which every provider tracks at least one id.
    snap = json.loads(json.dumps(base))
    del snap["providers"]["synthetic-c"]
    snap["provider_order"] = ["synthetic-a", "synthetic-b"]
    _reseal(snap)
    assert catalog.validate_snapshot(snap) is snap


def test_structural_wiring_is_syntactic_and_preserved_verbatim():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")
    path = ("providers", "synthetic-a", "chat_completions_url")
    accepted = (
        "https://host.example/v1/chat/completions?x=1",
        "https://host.example/v1/chat/completions?",
        "http://[::1]:8000/v1/chat/completions",
        "http://[::1]/v1/chat/completions",
        "HTTP://EXAMPLE.COM/v1/chat/completions",
        "https://host.example/v1/chat/completions#",
    )
    for url in accepted:
        snap = _mutate(base, path, url)
        _reseal(snap)
        assert catalog.validate_snapshot(snap) is snap, url
        # The ORIGINAL string is preserved verbatim -- never normalized.
        assert snap["providers"]["synthetic-a"]["chat_completions_url"] == url
    rejected = (
        "", "   https://host.example/v1", "https://host.example/v1 ",
        "https://host.example/v1\n", "https://ho\tst.example/v1",
        "https://host.example/v1\x7f", "/v1/chat/completions",
        "ftp://host.example/v1", "https://user@host.example/v1",
        "https://user:pass@host.example/v1", "https://host.example/v1#frag",
        "https://host.example/v1#%20", "http://::1/v1", 5, True, None,
    )
    for url in rejected:
        snap = _mutate(base, path, url)
        _reseal(snap)
        _assert_rejected(snap, INVALID_SNAPSHOT, f"wiring {url!r}")


def test_structural_pricing_literals_are_validated_but_never_normalized():
    catalog = _catalog
    base = _decode_doc(FIXTURE_DIR / "lookup.json")
    path = ("providers", "synthetic-a", "models", "vendor/shared", "pricing")
    accepted = (
        None,
        {"prompt": 0.5, "completion": None},
        {"prompt": "0.001", "completion": "1e-5"},
        {"prompt": " 1.5 ", "completion": "1_000"},
        {"prompt": "-0", "completion": "+1.5"},
        {"prompt": "١٢٣", "completion": "１２３"},
        {"prompt": 9007199254740991, "completion": "9007199254740992"},
        {"prompt": 1.0, "completion": -0.0},
    )
    for pricing in accepted:
        snap = _mutate(base, path, pricing)
        _reseal(snap)
        assert catalog.validate_snapshot(snap) is snap, pricing
        stored = snap["providers"]["synthetic-a"]["models"]["vendor/shared"][
            "pricing"]
        assert stored == pricing, pricing
        if pricing is not None:
            for key in ("prompt", "completion"):
                assert type(stored[key]) is type(pricing[key]), pricing
                assert repr(stored[key]) == repr(pricing[key]), pricing
    rejected = (
        {"prompt": True, "completion": "0"},
        {"prompt": "abc", "completion": "0"},
        {"prompt": "nan", "completion": "0"},
        {"prompt": "inf", "completion": "0"},
        {"prompt": "1e400", "completion": "0"},
        {"prompt": float("nan"), "completion": "0"},
        {"prompt": float("inf"), "completion": "0"},
        {"prompt": [], "completion": "0"},
        {"prompt": {"a": 1}, "completion": "0"},
        {"prompt": "0"},
        {"prompt": "0", "completion": "0", "extra": "x"},
        "free",
        0,
    )
    for pricing in rejected:
        snap = _mutate(base, path, pricing)
        _reseal(snap)
        _assert_rejected(snap, INVALID_SNAPSHOT, f"pricing {pricing!r}")


def test_snapshot_hash_matches_literal_vectors_and_retains_nested_ids(
        canonical_table):
    catalog = _catalog
    vectors = {row["id"]: row for row in canonical_table["canonical_vectors"]}
    snapshot_scoped = [row for row in canonical_table["canonical_vectors"]
                       if row["scope"] == "snapshot"]
    assert len(snapshot_scoped) == 2
    for row in snapshot_scoped:
        doc = catalog.load_json(row["json_text"])
        body = {k: v for k, v in doc.items() if k != "snapshot_id"}
        assert '"snapshot_id"' not in row["canonical_text"], row["id"]
        assert catalog.snapshot_hash(doc) == row["sha256"], row["id"]
        assert catalog.snapshot_hash(body) == row["sha256"], row["id"]
        assert catalog.snapshot_hash(doc) == hashlib.sha256(
            row["canonical_text"].encode("utf-8")).hexdigest(), row["id"]
        # Sealing then setting the root id is not circular.
        sealed = dict(body)
        sealed["snapshot_id"] = catalog.snapshot_hash(body)
        assert catalog.snapshot_hash(sealed) == catalog.snapshot_hash(body)

    # ONLY the root snapshot_id is excluded: a NESTED one is content and stays
    # bound into the digest.
    nested = catalog.load_json(vectors["vec_nested_snapshot_id"]["json_text"])
    body = {k: v for k, v in nested.items() if k != "snapshot_id"}
    assert body == {"inner": {"snapshot_id": "cafe"}}
    assert catalog.snapshot_hash(nested) == hashlib.sha256(
        b'{"inner":{"snapshot_id":"cafe"}}').hexdigest()
    without_nested = {"inner": {}}
    assert catalog.snapshot_hash(nested) != catalog.snapshot_hash(
        without_nested)

    # A non-dict argument is a programming error, never a public code.
    for bad in ([], "x", None, 1, b"{}"):
        with pytest.raises(TypeError):
            catalog.snapshot_hash(bad)


# ---------- optional capability projection + separate normalization ----------
#
# `project_capabilities` (raw upstream fragment -> public capability shape +
# warnings) and `normalize_capabilities` (valid public capability projection ->
# query shape) are exercised against the SAME independently hand-authored
# `metadata-normalization.json` rule table that the contract suite's fixture
# checker pins. The literal row data is the authoritative expected value; the
# table is loaded with a plain ``json.loads`` (NOT the strict raw loader)
# because it deliberately carries an unsafe ``2**53`` context token, which the
# projection must classify unknown without ever coercing it.

NORMALIZATION_TABLE_DOC = FIXTURE_DIR / "metadata-normalization.json"

# The four normalized capability fields and their frozen order (unknown_fields
# names exactly these, in exactly this order).
CAPABILITY_FIELD_ORDER = (
    "context_length", "input_modalities", "output_modalities",
    "supported_parameters",
)
# The capability-only warning order (tick_epoch_unknown is snapshot-level and
# never arises here).
CAPABILITY_WARNING_ORDER = ("metadata_missing", "metadata_invalid")
PROJECTED_CAPABILITY_FIELDS = frozenset(
    {"context_length", "architecture", "supported_parameters"})
PROJECTED_ARCHITECTURE_FIELDS = frozenset(
    {"input_modalities", "output_modalities"})
NORMALIZED_CAPABILITY_FIELDS = frozenset(CAPABILITY_FIELD_ORDER) | {
    "unknown_fields"}


@pytest.fixture(scope="module")
def normalization_rows():
    doc = _load_table(NORMALIZATION_TABLE_DOC)
    return {row["id"]: row for row in doc["rows"]}


def test_project_capabilities_matches_every_literal_table_row(
        normalization_rows):
    catalog = _catalog
    assert len(normalization_rows) == 22, "the rule table must carry 22 rows"
    for cid, row in normalization_rows.items():
        raw = copy.deepcopy(row["raw"])
        before = copy.deepcopy(raw)
        projected, warnings = catalog.project_capabilities(raw)
        assert set(projected) == PROJECTED_CAPABILITY_FIELDS, cid
        assert set(projected["architecture"]) == \
            PROJECTED_ARCHITECTURE_FIELDS, cid
        assert projected == row["projected"], cid
        assert warnings == row["warnings"], cid
        assert raw == before, cid  # the raw fragment is never mutated


def test_normalize_capabilities_matches_every_literal_table_row(
        normalization_rows):
    catalog = _catalog
    assert len(normalization_rows) == 22, "the rule table must carry 22 rows"
    for cid, row in normalization_rows.items():
        projected = copy.deepcopy(row["projected"])
        before = copy.deepcopy(projected)
        normalized = catalog.normalize_capabilities(projected)
        assert set(normalized) == NORMALIZED_CAPABILITY_FIELDS, cid
        assert normalized == row["normalized"], cid
        assert normalized["unknown_fields"] == [
            field for field in CAPABILITY_FIELD_ORDER
            if normalized[field] is None], cid
        assert projected == before, cid  # never normalized in place
    # A valid full public MODEL (extra identity/display/pricing members) is a
    # valid projection input and those extra members are ignored.
    model = {
        "id": "vendor/example",
        "name": "Example",
        "description": "ignored",
        "context_length": 4096,
        "architecture": {"input_modalities": ["TEXT"],
                         "output_modalities": None},
        "supported_parameters": ["Temperature"],
        "pricing": {"prompt": "0", "completion": "0"},
    }
    before = copy.deepcopy(model)
    assert catalog.normalize_capabilities(model) == {
        "context_length": 4096,
        "input_modalities": ["text"],
        "output_modalities": None,
        "supported_parameters": ["temperature"],
        "unknown_fields": ["output_modalities"],
    }
    assert model == before


def test_capability_projection_edges_distinguish_unknown_from_malformed():
    catalog = _catalog
    project = catalog.project_capabilities
    # An absent OR explicit-null architecture PARENT is unknown
    # (metadata_missing); a malformed non-null parent is metadata_invalid.
    # Either way BOTH children project to null.
    for raw in (
            {"context_length": 4096, "supported_parameters": ["temperature"]},
            {"context_length": 4096, "architecture": None,
             "supported_parameters": ["temperature"]}):
        projected, warnings = project(raw)
        assert projected["architecture"] == {
            "input_modalities": None, "output_modalities": None}
        assert warnings == ["metadata_missing"]
    projected, warnings = project(
        {"context_length": 4096, "architecture": ["text"],
         "supported_parameters": ["temperature"]})
    assert projected["architecture"] == {
        "input_modalities": None, "output_modalities": None}
    assert warnings == ["metadata_invalid"]
    # A float context (1.0) is malformed, never coerced.
    projected, warnings = project(
        {"context_length": 1.0,
         "architecture": {"input_modalities": [], "output_modalities": ["text"]},
         "supported_parameters": []})
    assert projected["context_length"] is None
    assert projected["architecture"]["input_modalities"] == []
    assert projected["architecture"]["output_modalities"] == ["text"]
    assert projected["supported_parameters"] == []
    assert warnings == ["metadata_invalid"]
    # Unknown (null) and known-empty ([]) stay distinct: only the null field is
    # flagged unknown, and a known zero context is KNOWN.
    normalized = catalog.normalize_capabilities(
        {"context_length": 0,
         "architecture": {"input_modalities": [],
                          "output_modalities": None},
         "supported_parameters": []})
    assert normalized["context_length"] == 0
    assert normalized["input_modalities"] == []
    assert normalized["unknown_fields"] == ["output_modalities"]
    # A malformed array is rejected WHOLE, never partially salvaged.
    projected, _ = project(
        {"context_length": 4096,
         "architecture": {"input_modalities": ["text", 5],
                          "output_modalities": ["text"]},
         "supported_parameters": ["temperature"]})
    assert projected["architecture"]["input_modalities"] is None
    assert projected["architecture"]["output_modalities"] == ["text"]
    # Whitespace tokens are valid and untrimmed; non-ASCII case pairs stay
    # literal (never Unicode-folded) in the public projection.
    projected, warnings = project(
        {"context_length": 4096,
         "architecture": {"input_modalities": ["\u00c9", "\u00e9"],
                          "output_modalities": [" Text "]},
         "supported_parameters": [" TEXT ", " text "]})
    assert warnings == []
    assert projected["architecture"]["input_modalities"] == ["\u00c9", "\u00e9"]
    assert projected["architecture"]["output_modalities"] == [" Text "]
    # Extra upstream members (including a private-looking one) are ignored and
    # never serialized into the projection.
    projected, warnings = project(
        {"context_length": 4096, "api_key": "secret",
         "architecture": {"input_modalities": ["text"],
                          "output_modalities": ["text"], "extra": 1},
         "supported_parameters": ["temperature"], "note": "ignored"})
    assert set(projected) == PROJECTED_CAPABILITY_FIELDS
    assert set(projected["architecture"]) == PROJECTED_ARCHITECTURE_FIELDS
    assert warnings == []
    # Cross-field warning order and dedup: metadata_missing precedes
    # metadata_invalid and each code appears once.
    _, warnings = project(
        {"context_length": None,
         "architecture": {"input_modalities": "text",
                          "output_modalities": None},
         "supported_parameters": "temperature"})
    assert warnings == ["metadata_missing", "metadata_invalid"]
    # A non-mapping raw is a programming-error TypeError, never a public code.
    for bad in ([], "x", None, 1, b"{}", ("t",)):
        with pytest.raises(TypeError):
            project(bad)


def test_capability_functions_never_alias_or_mutate_their_inputs():
    catalog = _catalog
    shared = ["temperature"]
    raw = {
        "context_length": 4096,
        "architecture": {"input_modalities": shared,
                         "output_modalities": shared},
        "supported_parameters": shared,
    }
    projected, warnings = catalog.project_capabilities(raw)
    # Every retained array is a NEW list, and the two architecture children are
    # not the same object even when upstream shared one.
    assert projected["architecture"]["input_modalities"] is not shared
    assert projected["supported_parameters"] is not shared
    assert projected["architecture"]["input_modalities"] is not \
        projected["architecture"]["output_modalities"]
    projected["architecture"]["input_modalities"].append("vision")
    projected["supported_parameters"].append("seed")
    warnings.append("metadata_invalid")
    assert raw == {
        "context_length": 4096,
        "architecture": {"input_modalities": ["temperature"],
                         "output_modalities": ["temperature"]},
        "supported_parameters": ["temperature"],
    }
    # A later call is unaffected by mutating an earlier result.
    projected2, warnings2 = catalog.project_capabilities(raw)
    assert projected2["architecture"]["input_modalities"] == ["temperature"]
    assert warnings2 == []
    # Normalization returns new lists and a fresh unknown_fields each call.
    normalized = catalog.normalize_capabilities(projected2)
    normalized["input_modalities"].append("audio")
    normalized["unknown_fields"].append("context_length")
    again = catalog.normalize_capabilities(projected2)
    assert again["input_modalities"] == ["temperature"]
    assert again["unknown_fields"] == []
    assert projected2["architecture"]["input_modalities"] == ["temperature"]
    assert projected2["supported_parameters"] == ["temperature"]


# ---------- import isolation: silent, side-effect-free package root ----------

# Application/operator modules a fresh import of the package must never pull in,
# and the application data files it must never open. Shared by both fresh-process
# audits below so the two import ORDERS are audited against one literal list.
FORBIDDEN_IMPORTS = [
    "mcp", "mcp_server", "build_site", "state", "config_loader",
    "providers", "inference_watchdog", "notify", "envfile", "diffing",
    "detection", "alive", "pending_removals", "probe_select",
    "probe_state", "probe_zero_credit", "anthropic", "httpx", "requests",
    "watchdog_catalog.query", "watchdog_catalog.server",
    "watchdog_catalog.adapter",
]
DATA_PATTERNS = ["/.env", "roster", "tools.json", "catalog.json",
                 "/state/", "/providers/"]
# Package-first and catalog-first, each in its OWN fresh process: the audit must
# not depend on which entry point a consumer reaches the package through.
IMPORT_ORDERS = (
    ["watchdog_catalog", "watchdog_catalog.catalog"],
    ["watchdog_catalog.catalog", "watchdog_catalog"],
)
# The pure query module is a second, EXPLICIT entry point: importing it directly
# is allowed, so for its two orders ONLY "watchdog_catalog.query" is dropped from
# the forbidden set (the requested module itself). Every other prefix, data-file
# and socket rule stays identical, and the original package/catalog orders above
# keep the full list — including the query entry.
QUERY_IMPORT_ORDERS = (
    ["watchdog_catalog", "watchdog_catalog.query"],
    ["watchdog_catalog.query", "watchdog_catalog"],
)

_PROBE = r'''
import importlib, json, os, sys

forbidden_prefixes = json.loads(os.environ["WC_FORBIDDEN"])
data_patterns = json.loads(os.environ["WC_DATA_PATTERNS"])
import_order = json.loads(os.environ.get(
    "WC_IMPORT_ORDER", '["watchdog_catalog", "watchdog_catalog.catalog"]'))

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

for module_name in import_order:
    importlib.import_module(module_name)

# Root plus every explicitly requested entry point is resident; the query-only
# orders do not require an irrelevant eager catalog import, while the original
# package/catalog orders still assert catalog presence.
assert "watchdog_catalog" in sys.modules
for module_name in import_order:
    assert module_name in sys.modules, module_name

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
    env = dict(base_env)
    env["WC_FORBIDDEN"] = json.dumps(FORBIDDEN_IMPORTS)
    env["WC_DATA_PATTERNS"] = json.dumps(DATA_PATTERNS)
    probe = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr
    report = json.loads(probe.stdout)
    assert report["forbidden_imports"] == [], report["forbidden_imports"]
    assert report["leaked_modules"] == [], report["leaked_modules"]
    assert report["application_opens"] == [], report["application_opens"]
    assert report["network_events"] == [], report["network_events"]


def test_package_import_audit_is_import_order_independent(tmp_path):
    """The audit holds for BOTH fresh-process package/catalog entry points —
    importing the submodule directly (catalog-first) and importing the package
    first — and for the separate, EXPLICIT query entry point. A lazy/ordered
    import must never become the way a forbidden module slips in."""
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
    for order in IMPORT_ORDERS:
        env = dict(base_env)
        env["WC_FORBIDDEN"] = json.dumps(FORBIDDEN_IMPORTS)
        env["WC_DATA_PATTERNS"] = json.dumps(DATA_PATTERNS)
        env["WC_IMPORT_ORDER"] = json.dumps(order)
        probe = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
        assert probe.returncode == 0, (order, probe.stderr)
        assert probe.stderr == "", (order, probe.stderr)
        report = json.loads(probe.stdout)
        assert report["forbidden_imports"] == [], (order, report)
        assert report["leaked_modules"] == [], (order, report)
        assert report["application_opens"] == [], (order, report)
        assert report["network_events"] == [], (order, report)

    # The query module is the requested module in its own two orders, so ONLY its
    # own name is dropped from the forbidden set; the original package/catalog
    # orders above still forbid it, and every other rule stays identical.
    assert "watchdog_catalog.query" in FORBIDDEN_IMPORTS
    query_forbidden = [p for p in FORBIDDEN_IMPORTS
                       if p != "watchdog_catalog.query"]
    for order in QUERY_IMPORT_ORDERS:
        env = dict(base_env)
        env["WC_FORBIDDEN"] = json.dumps(query_forbidden)
        env["WC_DATA_PATTERNS"] = json.dumps(DATA_PATTERNS)
        env["WC_IMPORT_ORDER"] = json.dumps(order)
        probe = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
        assert probe.returncode == 0, (order, probe.stderr)
        assert probe.stderr == "", (order, probe.stderr)
        report = json.loads(probe.stdout)
        assert report["forbidden_imports"] == [], (order, report)
        assert report["leaked_modules"] == [], (order, report)
        assert report["application_opens"] == [], (order, report)
        assert report["network_events"] == [], (order, report)

    # A plain explicit query import from a neutral cwd + clean HOME is silent.
    plain = subprocess.run(
        [sys.executable, "-B", "-c", "import watchdog_catalog.query"],
        cwd=str(cwd), env=base_env, capture_output=True, text=True, timeout=120)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == "", plain.stdout
    assert plain.stderr == "", plain.stderr


# ---------- full public model projection ----------
#
# `project_model` composes the accepted capability projection with the three
# identity/display/pricing members into ONE complete public model object. Every
# expected value below is an INDEPENDENT literal restatement of the frozen
# contract (the README projected-model field table), never derived from a
# runtime helper.

PUBLIC_MODEL_FIELDS = frozenset(
    {"id", "name", "description", "context_length", "architecture",
     "supported_parameters", "pricing"})
PUBLIC_ARCHITECTURE_FIELDS = frozenset(
    {"input_modalities", "output_modalities"})
PUBLIC_PRICING_FIELDS = frozenset({"prompt", "completion"})

# A complete, all-known upstream fragment used as the positive control.
_KNOWN_FRAGMENT = {
    "name": "Vendor Known",
    "description": "A known model.",
    "context_length": 4096,
    "architecture": {"input_modalities": ["text"],
                     "output_modalities": ["text"]},
    "supported_parameters": ["temperature"],
    "pricing": {"prompt": "0", "completion": 0},
}


def _known_fragment():
    return json.loads(json.dumps(_KNOWN_FRAGMENT))


def _fragment(**overrides):
    raw = _known_fragment()
    raw.update(overrides)
    return raw


def _fragment_without(*keys):
    raw = _known_fragment()
    for key in keys:
        raw.pop(key, None)
    return raw


def test_project_model_identity_name_description_and_raw_degradation():
    catalog = _catalog
    project = catalog.project_model

    # (a) Tracked-key authority: a mismatched, non-string or absent upstream
    #     `id` is ignored and contributes NO warning.
    for upstream_id in ("upstream/other", 5, None, ["x"]):
        model, warnings = project("vendor/key", _fragment(id=upstream_id))
        assert model["id"] == "vendor/key", upstream_id
        assert warnings == [], upstream_id
    model, warnings = project("vendor/key", _fragment_without("id"))
    assert model["id"] == "vendor/key"
    assert warnings == []

    # (b) Always exactly the seven public fields.
    model, _ = project("vendor/key", _known_fragment())
    assert set(model) == PUBLIC_MODEL_FIELDS
    assert set(model["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS
    assert set(model["pricing"]) == PUBLIC_PRICING_FIELDS

    # (c) `name`: the upstream value iff a NONEMPTY string (verbatim, including
    #     whitespace and non-ASCII); otherwise the tracked key.
    assert project("k", _fragment(name="  spaced  "))[0]["name"] == "  spaced  "
    assert project("k", _fragment(name="\u03a9"))[0]["name"] == "\u03a9"
    for bad in (None, "", True, False, 0, 5, 1.5, [], {}, ["x"]):
        model, warnings = project("tracked/fallback", _fragment(name=bad))
        assert model["name"] == "tracked/fallback", bad
        assert warnings == [], bad
    assert project("tracked/missing", _fragment_without("name"))[0]["name"] == \
        "tracked/missing"

    # (d) `description`: any string (including "") is retained verbatim; every
    #     non-string (or an absent member) degrades to null -- never `""`, and
    #     never a warning.
    assert project("k", _fragment(description=""))[0]["description"] == ""
    assert project("k", _fragment(description="text"))[0]["description"] == "text"
    for bad in (None, True, False, 0, 5, 1.5, [], {}, ["x"]):
        model, warnings = project("k", _fragment(description=bad))
        assert model["description"] is None, bad
        assert warnings == [], bad
    assert project("k", _fragment_without("description"))[0]["description"] \
        is None

    # (e) An invalid tracked id is a PROGRAMMING error (TypeError), never a
    #     public envelope and never a partially-projected model.
    for bad_id in ("", None, 5, 1.5, True, ["x"], ("x",), b"x", {}):
        with pytest.raises(TypeError):
            project(bad_id, _known_fragment())

    # (f) An absent argument, an explicit null and an ACTUAL empty dict are the
    #     same all-unknown model, and the container is metadata_missing.
    all_unknown = {
        "id": "vendor/absent",
        "name": "vendor/absent",
        "description": None,
        "context_length": None,
        "architecture": {"input_modalities": None, "output_modalities": None},
        "supported_parameters": None,
        "pricing": None,
    }
    for raw in (None, {}):
        model, warnings = project("vendor/absent", raw)
        assert model == all_unknown, raw
        assert warnings == ["metadata_missing"], raw
    assert project("vendor/absent") == project("vendor/absent", None)

    # (g) A non-null NON-DICT container is MALFORMED, not missing: the same
    #     all-unknown model, but ONLY metadata_invalid -- never manufactured
    #     missing-field warnings. Classification is `is not None`, NOT
    #     truthiness, so false/0/[]/"" are all non-null and therefore invalid.
    for raw in ([], ["text"], False, 0, 0.0, "", "text", ("x",), b"x"):
        model, warnings = project("vendor/bad-container", raw)
        assert model == {
            "id": "vendor/bad-container",
            "name": "vendor/bad-container",
            "description": None,
            "context_length": None,
            "architecture": {"input_modalities": None,
                             "output_modalities": None},
            "supported_parameters": None,
            "pricing": None,
        }, raw
        assert warnings == ["metadata_invalid"], raw


def test_project_model_pricing_projection_and_scalar_type_preservation():
    catalog = _catalog
    project = catalog.project_model

    def pricing_of(value):
        return project("k", _fragment(pricing=value))[0]["pricing"]

    # (a) Whole-object null when pricing is unknown overall: absent, explicit
    #     null or any non-object value.
    assert pricing_of(None) is None
    assert project("k", _fragment_without("pricing"))[0]["pricing"] is None
    for wrong in ("free", 0, 0.0, True, False, [], ["0"], ("x",)):
        assert pricing_of(wrong) is None, wrong

    # (b) A present pricing OBJECT always yields EXACTLY prompt+completion, even
    #     empty or both-null; extra upstream keys are dropped, never leaked.
    for value, expect in (
            ({}, {"prompt": None, "completion": None}),
            ({"prompt": None, "completion": None},
             {"prompt": None, "completion": None}),
            ({"prompt": "0", "completion": "0"},
             {"prompt": "0", "completion": "0"}),
            ({"prompt": "0", "completion": True},
             {"prompt": "0", "completion": None}),
            ({"prompt": "0.5", "completion": "abc"},
             {"prompt": "0.5", "completion": None}),
            ({"prompt": "0", "completion": "0", "image": "0", "request": 1},
             {"prompt": "0", "completion": "0"})):
        got = pricing_of(value)
        assert got == expect, value
        assert set(got) == PUBLIC_PRICING_FIELDS, value

    # (c) An invalid individual scalar degrades to null; a valid SIBLING
    #     survives unchanged.
    invalid = (True, False, float("nan"), float("inf"), float("-inf"),
               9007199254740992, -9007199254740992, 9007199254740992.0,
               "abc", "nan", "inf", "1e400", [], {}, ["0"])
    for bad in invalid:
        got = pricing_of({"prompt": bad, "completion": "0.5"})
        assert got["prompt"] is None, bad
        assert got["completion"] == "0.5", bad
        assert type(got["completion"]) is str, bad

    # (d) Valid scalars keep their ORIGINAL TYPE and spelling: int 1, float 1.0
    #     and string "1" are three DISTINCT retained values (equality alone
    #     cannot pin this -- assert type identity). No coercion, no zero default.
    got = pricing_of({"prompt": 1, "completion": 1.0})
    assert got["prompt"] == 1 and type(got["prompt"]) is int
    assert got["completion"] == 1.0 and type(got["completion"]) is float
    got = pricing_of({"prompt": "1", "completion": "0"})
    assert got["prompt"] == "1" and type(got["prompt"]) is str
    assert type(got["completion"]) is str
    # None vs 0 vs 0.0 vs "0" stay four DISTINCT values.
    got = pricing_of({"prompt": None, "completion": 0})
    assert got["prompt"] is None
    assert got["completion"] == 0 and type(got["completion"]) is int
    got = pricing_of({"prompt": 0.0, "completion": "0"})
    assert got["prompt"] == 0.0 and type(got["prompt"]) is float
    assert got["completion"] == "0" and type(got["completion"]) is str
    # Finite numeric-string spellings are kept VERBATIM (no trim, no rewrite):
    # the permissive float-probe edge is preserved, never tightened.
    for spelling in (" 1.5 ", "1_000", "\u0661\u0662\u0663", "-0", "+1.5",
                     "1e-5", "0.001"):
        got = pricing_of({"prompt": spelling, "completion": spelling})
        assert got["prompt"] == spelling, spelling
        assert type(got["prompt"]) is str, spelling
    # Negative finite prices are accepted (the existing predicate, unchanged).
    got = pricing_of({"prompt": -1.5, "completion": "-0.5"})
    assert got["prompt"] == -1.5 and type(got["prompt"]) is float
    assert got["completion"] == "-0.5"
    # No free-price inference: two unknown scalars stay null, never 0.
    assert pricing_of({"prompt": "abc", "completion": "abc"}) == {
        "prompt": None, "completion": None}


def test_project_model_composes_capabilities_and_isolates_warnings(
        normalization_rows):
    catalog = _catalog
    project = catalog.project_model
    assert len(normalization_rows) == 22

    # (a) Every literal capability row, projected through project_model with an
    #     explicit synthetic tracked id, reproduces its capability fragment, its
    #     warnings and its normalized fields EXACTLY. Each row's `raw` carries
    #     only capability members, so its warnings are the model's warnings.
    for cid, row in normalization_rows.items():
        model, warnings = project("synthetic/tracked-id", row["raw"])
        assert model["context_length"] == row["projected"]["context_length"], cid
        assert model["architecture"] == row["projected"]["architecture"], cid
        assert model["supported_parameters"] == \
            row["projected"]["supported_parameters"], cid
        assert warnings == row["warnings"], cid
        assert catalog.normalize_capabilities(model) == row["normalized"], cid

    # (b) Warning isolation: with all FOUR capabilities known, a missing,
    #     malformed or null name/description/pricing NEVER contributes a code.
    complete = {
        "context_length": 4096,
        "architecture": {"input_modalities": ["text"],
                         "output_modalities": ["text"]},
        "supported_parameters": ["temperature"],
    }
    for extra in ({}, {"name": None}, {"name": 5}, {"description": 5},
                  {"description": None}, {"pricing": "free"},
                  {"pricing": None}, {"pricing": {"prompt": True}},
                  {"name": [], "description": {}, "pricing": []}):
        model, warnings = project("k", dict(complete, **extra))
        assert warnings == [], extra
        assert set(model) == PUBLIC_MODEL_FIELDS, extra

    # (c) Mixed missing/invalid capability data warns in the frozen order, each
    #     code exactly once.
    _, warnings = project("k", {
        "context_length": None,
        "architecture": {"input_modalities": "text",
                         "output_modalities": None},
        "supported_parameters": "temperature",
    })
    assert warnings == ["metadata_missing", "metadata_invalid"]


def test_project_model_is_pure_unaliased_and_validates_in_a_strict_snapshot():
    catalog = _catalog
    project = catalog.project_model

    # (a) No aliasing: every retained container/list is a NEW object, and
    #     mutating a returned collection never touches the input or a later call.
    shared = ["temperature"]
    pricing = {"prompt": "0", "completion": "0"}
    raw = {
        "name": "N",
        "description": "D",
        "context_length": 4096,
        "architecture": {"input_modalities": shared,
                         "output_modalities": shared},
        "supported_parameters": shared,
        "pricing": pricing,
        "api_key": "secret",          # ignored extras must never be serialized
        "note": "ignored",
    }
    before = copy.deepcopy(raw)
    model, warnings = project("vendor/shared", raw)
    assert raw == before
    assert model["architecture"]["input_modalities"] is not shared
    assert model["supported_parameters"] is not shared
    assert model["architecture"]["input_modalities"] is not \
        model["architecture"]["output_modalities"]
    assert model["pricing"] is not pricing
    # Ignored extras (including nested extras) never leak into the projection.
    assert set(model) == PUBLIC_MODEL_FIELDS
    assert set(model["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS
    assert set(model["pricing"]) == PUBLIC_PRICING_FIELDS
    # Mutate the returned collections: input and a fresh call are unaffected.
    model["architecture"]["input_modalities"].append("vision")
    model["supported_parameters"].append("seed")
    model["pricing"]["prompt"] = "999"
    warnings.append("metadata_invalid")
    assert raw == before
    again, again_warnings = project("vendor/shared", raw)
    assert again["architecture"]["input_modalities"] == ["temperature"]
    assert again["supported_parameters"] == ["temperature"]
    assert again["pricing"] == {"prompt": "0", "completion": "0"}
    assert again_warnings == []
    # Nested extras on architecture/pricing are dropped, never interpreted.
    nested = project("k", _fragment(
        architecture={"input_modalities": ["text"],
                      "output_modalities": ["text"], "extra": 1},
        pricing={"prompt": "0", "completion": "0", "image": "0"}))[0]
    assert set(nested["architecture"]) == PUBLIC_ARCHITECTURE_FIELDS
    assert set(nested["pricing"]) == PUBLIC_PRICING_FIELDS

    # (b) Every projected model independently satisfies the unchanged public
    #     validator, with the exact literal field set asserted separately.
    known, known_warnings = project("vendor/shared", _known_fragment())
    assert set(known) == PUBLIC_MODEL_FIELDS
    assert known_warnings == []
    assert catalog._is_model("vendor/shared", known)
    degraded, degraded_warnings = project("vendor/shared", None)
    assert set(degraded) == PUBLIC_MODEL_FIELDS
    assert degraded_warnings == ["metadata_missing"]
    assert catalog._is_model("vendor/shared", degraded)

    # (c) Strict-snapshot integration: a synthetic COPY of an existing fixture
    #     with projected models embedded and independently resealed passes the
    #     unchanged public validator and the load_snapshot roundtrip. The
    #     json/hashlib reseal helper is the hash oracle, never a production one.
    snap = _decode_doc(FIXTURE_DIR / "lookup.json")
    snap["providers"]["synthetic-a"]["models"]["vendor/shared"] = known
    snap["providers"]["synthetic-b"]["models"]["vendor/shared"] = degraded
    _reseal(snap)
    body = {k: v for k, v in snap.items() if k != "snapshot_id"}
    assert snap["snapshot_id"] == hashlib.sha256(
        _canonical_text(body)).hexdigest()
    assert catalog.validate_snapshot(snap) is snap
    assert catalog.load_snapshot(json.dumps(snap)) == snap


# ---------- pure free-marker helper: literal corpus + compatibility edges ----------
#
# `watchdog_catalog.query.strip_free_marker` is the pure, stdlib-only marker
# helper extracted from build_site. The 17 expected values below are the
# INDEPENDENTLY authored rows of cases.json's `marker_strip_rule` table (never a
# runtime helper or another test module); the fallback controls and compatibility
# edges are literal characterizations of the frozen README contract.

MARKER_CASES_DOC = FIXTURE_DIR / "cases.json"

# Delimiter-only ids (a "free" segment with its delimiter but nothing else) plus
# the bare non-marker control. Each expected value is a literal restatement of
# the empty-result guard: a strip that would empty the id returns it unchanged,
# and bare "free" carries no delimiter so neither delimiter-bounded regex matches.
MARKER_FALLBACK_CONTROLS = {
    ":free": ":free",
    "-free": "-free",
    "_free": "_free",
    "free:": "free:",
    "free-": "free-",
    "free_": "free_",
    "free": "free",
}

# Controller characterization edges pinning the regex semantics the public
# algorithm must preserve: '$' matches at end OR before a single final newline
# (so one trailing '\n' survives stripping, two do not); stripping is ONE suffix
# pass then ONE prefix pass (never recursive or global); a mid-token 'free', an
# unmatched leading/trailing space and a 'free' inside the namespace are never
# markers.
MARKER_COMPAT_EDGES = {
    "free-free-X-free-free": "free-X-free",
    "vendor/x:free\n": "vendor/x\n",
    "vendor/x:free\n\n": "vendor/x:free\n\n",
    "free:": "free:",
    " free-x ": " free-x ",
    "vendor/freebie": "vendor/freebie",
    "FREE-X:free": "X",
}


def test_strip_free_marker_matches_literal_corpus_and_compat_edges():
    """The pure helper reproduces all 17 frozen marker rows unchanged and holds
    the delimiter-only empty-result guard plus the characterized regex edges."""
    from watchdog_catalog.query import strip_free_marker

    rows = _load_table(MARKER_CASES_DOC)["marker_strip_rule"]
    # Count and uniqueness are asserted so a silently dropped or duplicated row
    # cannot shrink the loop and still pass.
    assert len(rows) == 17, len(rows)
    assert len({row["raw_id"] for row in rows}) == 17
    for row in rows:
        raw_id, stripped = row["raw_id"], row["stripped"]
        assert strip_free_marker(raw_id) == stripped, (
            f"marker rule mismatch for {raw_id!r}: want {stripped!r}")
    for raw_id, want in MARKER_FALLBACK_CONTROLS.items():
        assert strip_free_marker(raw_id) == want, (
            f"fallback control mismatch for {raw_id!r}: want {want!r}")
    for raw_id, want in MARKER_COMPAT_EDGES.items():
        assert strip_free_marker(raw_id) == want, (
            f"compat edge mismatch for {raw_id!r}: want {want!r}")


# ---------- pure ordering primitives: natural raw-ID + provider precedence ----------
#
# `natural_key` / `endpoint_key` are the two pure ordering primitives of the
# frozen README rule (README.md, "Total ordering rule" and "Natural comparator"):
# provider precedence is the index in `provider_order`, and within one provider
# raw IDs compare by maximal ASCII digit runs (ORIGINAL digit count, then lexical
# ASCII digits), case-sensitive Unicode code-point text, digit runs before text,
# empty boundary text tokens preserved and a strict prefix first.
#
# Every expected order below is INDEPENDENT LITERAL data: the seven rows of
# `ordering-cases.json` (hand-authored before any implementation) plus the literal
# `provider_order`/golden arrays of `cases.json`. No expectation is generated by a
# reference implementation, and `tests/test_catalog.py`'s private
# `_natural_key_contract` is never imported.

ORDERING_VECTOR_DOC = FIXTURE_DIR / "ordering-cases.json"

# Literal provider precedence: the fixture's `provider_order` array. It is
# asserted equal to the fixture field BEFORE any golden map is consulted, and the
# golden provider arrays are then flattened in THIS order -- never by iterating
# the golden object's own (meaningless) key order.
PROVIDER_PRECEDENCE = ("synthetic-b", "synthetic-a", "synthetic-c")
# The fixture's PHYSICAL provider-map insertion order: deliberately NOT the query
# order, and used as the input order the helper has to sort.
PROVIDER_MAP_INSERTION = ("synthetic-a", "synthetic-b", "synthetic-c")

# Compact literal controls for the frozen comparator edges the seven vectors do
# not reach: the empty id, a strict prefix, several digit runs in one id, the
# never-trimmed surrounding space and the snapshot-versus-query divergence row.
NATURAL_KEY_CONTROLS = (
    (["", "vendor/x"], ["", "vendor/x"]),
    (["vendor/x0", "vendor/x"], ["vendor/x", "vendor/x0"]),
    (["a1b10", "a1b02", "a1b2"], ["a1b2", "a1b02", "a1b10"]),
    (["vendor/model", " vendor/model "], [" vendor/model ", "vendor/model"]),
    (["vendor/item02", "vendor/item1"], ["vendor/item1", "vendor/item02"]),
)


def test_natural_key_matches_every_literal_ordering_vector_and_control():
    """The natural comparator reproduces all seven frozen vector rows from their
    literal input/expected arrays -- forward and reverse-supplied -- plus the
    literal empty/strict-prefix/multi-run/no-trim controls, without mutating the
    inputs and without ever raising a heterogeneous comparison TypeError."""
    from watchdog_catalog.query import natural_key

    rows = _load_table(ORDERING_VECTOR_DOC)["cases"]
    assert len(rows) == 7, len(rows)
    assert len({row["id"] for row in rows}) == 7
    corpus = set()
    for row in rows:
        cid = row["id"]
        supplied, expected = row["input"], row["expected"]
        assert len(supplied) == len(expected), cid
        # Guard: every vector is a genuine SORT, never an identity order.
        assert supplied != expected, cid
        before = copy.deepcopy(supplied)
        assert sorted(supplied, key=natural_key) == expected, cid
        assert supplied == before, cid  # the input list is never reordered
        # Reverse-supplied control: the same literal expectation still holds.
        assert sorted(list(reversed(supplied)), key=natural_key) == expected, cid
        corpus.update(supplied)
        corpus.update(expected)

    for supplied, expected in NATURAL_KEY_CONTROLS:
        assert sorted(supplied, key=natural_key) == expected, supplied
        corpus.update(supplied)

    # The frozen snapshot order is the OPPOSITE literal code-point order for the
    # divergence row: item02 precedes item1 there and follows it naturally.
    assert sorted(["vendor/item02", "vendor/item1"]) == \
        ["vendor/item02", "vendor/item1"]

    # Cross-vector comparability sweep: every key in the corpus compares with
    # every other (no heterogeneous TypeError), equal strings give equal keys and
    # the key order is one consistent total order over the whole corpus.
    assert len(corpus) >= 20, len(corpus)
    ordered = sorted(corpus, key=natural_key)
    assert [natural_key(raw_id) for raw_id in ordered] == \
        sorted(natural_key(raw_id) for raw_id in corpus)
    for left, right in zip(ordered, ordered[1:]):
        assert natural_key(left) <= natural_key(right), (left, right)
    assert natural_key("") == natural_key("")
    assert natural_key("vendor/item1") == natural_key("vendor/item1")


def test_endpoint_key_orders_by_provider_precedence_then_natural_raw_id():
    """`endpoint_key` ranks by the caller's `provider_order` index and then by the
    natural raw-ID order: all ten pairs of the ordering fixture reproduce the
    literal golden arrays from physical (wrong-order) input, forward and
    reversed; a shared raw ID stays two pairs; the zero-id provider is tolerated;
    and the snapshot data is never reordered."""
    from watchdog_catalog.query import endpoint_key

    ordering = _decode_doc(FIXTURE_DIR / "ordering.json")
    cases = {row["id"]: row for row in _load_table(MARKER_CASES_DOC)["cases"]}

    def pair_key(pair):
        return endpoint_key(ordering["provider_order"], pair[0], pair[1])

    # (a) Declared precedence is the literal array, the physical provider-map
    #     insertion order is a DIFFERENT order, and precedence is the supplied
    #     index -- reversing that list reverses provider precedence.
    assert ordering["provider_order"] == list(PROVIDER_PRECEDENCE)
    assert list(ordering["providers"]) == list(PROVIDER_MAP_INSERTION)
    assert list(ordering["providers"]) != list(PROVIDER_PRECEDENCE)
    flipped = ["synthetic-c", "synthetic-a", "synthetic-b"]
    assert endpoint_key(flipped, "synthetic-a", "vendor/item2") < \
        endpoint_key(flipped, "synthetic-b", "vendor/item2")

    golden = cases["list_free_ordering_all"]["expect"]["providers"]
    assert set(golden) == set(PROVIDER_PRECEDENCE)
    expected_pairs = [(provider, raw_id)
                      for provider in PROVIDER_PRECEDENCE
                      for raw_id in golden[provider]]
    assert len(expected_pairs) == 10, len(expected_pairs)
    assert len(set(expected_pairs)) == 10

    # (b) Snapshot model_ids stay in literal code-point order while the query
    #     order genuinely differs.
    for provider in PROVIDER_PRECEDENCE:
        ids = ordering["providers"][provider]["model_ids"]
        assert ids == sorted(ids), provider
    snapshot_b = ordering["providers"]["synthetic-b"]["model_ids"]
    natural_b = golden["synthetic-b"]
    assert snapshot_b.index("vendor/item02") < snapshot_b.index("vendor/item1")
    assert natural_b.index("vendor/item02") > natural_b.index("vendor/item1")

    # (c) Input pairs come from the ACTUAL physical snapshot order (provider-map
    #     insertion order, stored lexical model_ids), which is NOT the expected
    #     order; sorting with the helper -- forward and reversed -- reproduces the
    #     literal golden arrays, and nothing in the snapshot is touched.
    physical_pairs = [(provider, raw_id)
                      for provider in ordering["providers"]
                      for raw_id in ordering["providers"][provider]["model_ids"]]
    assert len(physical_pairs) == 10
    assert physical_pairs == [(provider, raw_id)
                              for provider in PROVIDER_MAP_INSERTION
                              for raw_id in ordering["providers"][provider]["model_ids"]]
    assert physical_pairs != expected_pairs  # guard: the input is unsorted
    providers_before = copy.deepcopy(ordering["providers"])
    order_before = copy.deepcopy(ordering["provider_order"])
    assert sorted(physical_pairs, key=pair_key) == expected_pairs
    assert sorted(list(reversed(physical_pairs)), key=pair_key) == expected_pairs
    assert ordering["providers"] == providers_before
    assert ordering["provider_order"] == order_before
    # Provider precedence is NOT alphabetic key order: the sorted provider
    # sequence is b...b, a, where the alphabetical one would be a, b, c.
    assert [pair[0] for pair in sorted(physical_pairs, key=pair_key)] != \
        sorted(pair[0] for pair in physical_pairs)
    # Within-provider natural order for the whole nine-id provider.
    assert sorted([("synthetic-b", raw_id) for raw_id in snapshot_b],
                  key=pair_key) == [("synthetic-b", raw_id)
                                    for raw_id in natural_b]

    # (d) A raw id shared by two providers stays TWO sortable pairs (never
    #     deduped) and orders by provider precedence; the get_model golden's
    #     literal endpoint order is the control.
    shared = cases[
        "get_model_ordering_identical_raw_id_provider_precedence"]["expect"]
    assert shared["exact_gateways"] == ["synthetic-b", "synthetic-a"]
    assert [endpoint["provider"] for endpoint in shared["endpoints"]] == \
        ["synthetic-b", "synthetic-a"]
    sorted_shared = sorted(
        [("synthetic-a", "vendor/item2"), ("synthetic-b", "vendor/item2")],
        key=pair_key)
    assert sorted_shared == [("synthetic-b", "vendor/item2"),
                             ("synthetic-a", "vendor/item2")]
    assert [pair[0] for pair in sorted_shared] == \
        [endpoint["provider"] for endpoint in shared["endpoints"]]
    assert len(sorted_shared) == 2 and len(set(sorted_shared)) == 2
    assert pair_key(sorted_shared[0]) != pair_key(sorted_shared[1])

    # (e) The existing search page is the provider's natural order seen through a
    #     TEST-ONLY offset/limit slice: query filtering and pagination runtime is
    #     deferred, so no query algorithm is implemented here.
    page = cases["search_ordering_provider_b_natural_page"]["expect"]
    assert (page["offset"], page["limit"]) == (1, 2)
    assert [(endpoint["provider"], endpoint["model_id"])
            for endpoint in page["endpoints"]] == \
        [("synthetic-b", "vendor/item2"), ("synthetic-b", "vendor/item02")]
    b_pairs = sorted([("synthetic-b", raw_id) for raw_id in snapshot_b],
                     key=pair_key)
    assert b_pairs[page["offset"]:page["offset"] + page["limit"]] == \
        [(endpoint["provider"], endpoint["model_id"])
         for endpoint in page["endpoints"]]

    # (f) The zero-id provider contributes no pair, its rank is still tolerated
    #     and sorts last; identical pairs give equal keys and a stable sort keeps
    #     the original references.
    assert golden["synthetic-c"] == []
    assert ordering["providers"]["synthetic-c"]["model_ids"] == []
    assert pair_key(("synthetic-b", "vendor/item1")) < \
        pair_key(("synthetic-c", "vendor/absent"))
    assert pair_key(("synthetic-a", "vendor/item2")) < \
        pair_key(("synthetic-c", "vendor/absent"))
    duplicates = [{"provider": "synthetic-b", "model_id": "vendor/item1"},
                  {"provider": "synthetic-b", "model_id": "vendor/item1"}]

    def record_key(record):
        return endpoint_key(ordering["provider_order"], record["provider"],
                            record["model_id"])

    assert record_key(duplicates[0]) == record_key(duplicates[1])
    stable = sorted(duplicates, key=record_key)
    assert stable[0] is duplicates[0] and stable[1] is duplicates[1]
