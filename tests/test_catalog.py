"""Contract-integrity tests — standalone catalogue snapshot v1 (units 1a+1b).

Contract-only stage: stdlib data operations only. These tests pin the
frozen v1 snapshot contract (README.md, "Published catalogue snapshot
(v1) — contract draft") against the synthetic golden fixtures under
tests/fixtures/catalog/. They import NO watchdog_catalog
implementation module — validator/query tests are introduced red
within their corresponding Task 2/3 implementation units and made
green there.

Unit 1a froze the top-level/provider/model snapshot contract against
basic.json (minimal valid snapshot, one zero-ID provider). Unit 1b
adds exact-ID and marker-lookup golden cases: lookup.json (shared raw
ids, marker variants, an unrelated renamed model, a second zero-ID
provider) and cases.json (independently hand-specified expected
get_model payloads). Test-local restatements of the contract
marker-strip rule and natural-order key cross-check the golden
payloads against the fixture data — no implementation code is
imported. Later units append their own fixtures/cases.

The fixtures are synthetic by contract: provider keys live in the
documented `synthetic-` namespace, so no fixture encodes the live
provider count or depends on production files.

The envelope-freeze correction closes the provisional `get_model`
response/count vocabulary: `cases.json` now carries the frozen
`provider_staleness` map and the four frozen `counts` cardinalities
(`exact_matches`, `endpoints`, `distinct_raw_ids`,
`marker_group_labels`), pinned here against the hand-authored goldens.
Provider staleness (a per-gateway carried-forward flag) stays distinct
from tick age (derived from `tick_epoch`/`cadence_s`); no source
retrieval/cache field is invented in the pure payload.
"""

import copy
import hashlib
import json
import math
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"
FIXTURE = FIXTURE_DIR / "basic.json"
LOOKUP_FIXTURE = FIXTURE_DIR / "lookup.json"
CASES_DOC = FIXTURE_DIR / "cases.json"

CONTRACT_SECTION = "Published catalogue snapshot (v1)"

TOP_LEVEL_FIELDS = frozenset({
    "schema_version", "snapshot_id", "tick_epoch", "cadence_s",
    "provider_order", "providers", "warnings",
})
PROVIDER_FIELDS = frozenset({
    "display_name", "chat_completions_url", "api_type",
    "model_ids", "models", "stale",
})
MODEL_FIELDS = frozenset({
    "id", "name", "description", "context_length",
    "architecture", "supported_parameters", "pricing",
})
ARCHITECTURE_FIELDS = frozenset({"input_modalities", "output_modalities"})
PRICING_FIELDS = frozenset({"prompt", "completion"})
GET_MODEL_RESPONSE_FIELDS = frozenset({
    "ok", "query", "snapshot_id", "tick_epoch", "exact_matches",
    "exact_gateways", "provider_staleness", "endpoints", "caveats",
    "counts",
})
ENDPOINT_FIELDS = frozenset({
    "provider", "model_id", "chat_completions_url", "api_type",
    "stale", "metadata",
})
COUNT_FIELDS = frozenset({
    "exact_matches", "endpoints", "distinct_raw_ids",
    "marker_group_labels",
})
WARNING_CODES = frozenset({
    "tick_epoch_unknown", "metadata_missing", "metadata_invalid",
})
# Keys that must never appear anywhere in the public snapshot: config
# dicts, auth/token material, roster internals, probe internals.
FORBIDDEN_KEYS = frozenset({
    "config", "_token", "auth", "env", "token_file", "access_token",
    "api_key", "pending_alerts", "ratelimits", "probe", "stale_providers",
    "transients", "unconfirmed",
})
SYNTHETIC_PROVIDER_PREFIX = "synthetic-"

# The absence caveat every get_model payload must carry (contract:
# "Preserve the absence caveat"). Pinned verbatim.
ABSENCE_CAVEAT = (
    "Cross-gateway ABSENCE is unreliable: gateways rename inconsistently, "
    "so a missing exact id does NOT prove the model is unavailable there."
)

# Marker-strip rule restated from the contract text for fixture
# cross-checks ONLY: strip a delimiter-bounded `free` segment at the
# start/end (`[-:_]` delimiters, case-insensitive), with the
# empty-result guard. NOT imported from build_site or any other
# implementation module.
_MARKER_SUFFIX = re.compile(r"[-:_]free$", re.I)
_MARKER_PREFIX = re.compile(r"^free[-:_]", re.I)


def _reject_constant(constant):
    raise ValueError(f"non-finite JSON constant: {constant}")


def _load_strict(path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      parse_constant=_reject_constant)


def _load_fixture():
    return _load_strict(FIXTURE)


@pytest.fixture(scope="module")
def snapshot():
    return _load_fixture()


@pytest.fixture(scope="module")
def lookup_snapshot():
    return _load_strict(LOOKUP_FIXTURE)


@pytest.fixture(scope="module")
def cases_document():
    return _load_strict(CASES_DOC)


def _is_finite_scalar(value):
    """Finite scalar preserved as returned: number or numeric string."""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            return math.isfinite(float(value))
        except ValueError:
            return False
    return False


def _counts_are_strict_int(value):
    """Counts are non-negative ints; bool is NOT an integer here."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def _assert_pricing_contract(pricing):
    """Projected `pricing` contract (resolved nullability): the whole
    object is `null` when pricing is unknown overall; otherwise exactly
    `prompt` + `completion`, each a finite scalar preserved as returned
    (number or numeric string) or `null` when individually unknown.
    Booleans and non-finite numbers are invalid."""
    if pricing is None:
        return
    assert isinstance(pricing, dict), \
        "pricing must be the two-scalar object or null"
    assert set(pricing) == PRICING_FIELDS, \
        f"pricing must carry exactly {sorted(PRICING_FIELDS)}"
    for value in pricing.values():
        assert _is_finite_scalar(value), \
            "pricing scalars must be finite numbers/strings or null"


def _strip_marker_contract(raw_id):
    """Contract restatement of the free-marker stripper (above)."""
    stripped = _MARKER_SUFFIX.sub("", raw_id)
    stripped = _MARKER_PREFIX.sub("", stripped)
    return stripped or raw_id


def _natural_key_contract(raw_id):
    """Contract restatement of result ordering: provider display order,
    then natural raw-ID order — ASCII digit runs compared by digit
    length and lexical order, never locale-dependent sorting or
    floating-point conversion of long digit sequences."""
    return [(len(part), part) if part.isdigit() else part
            for part in re.split(r"(\d+)", raw_id)]


# ---------- shared snapshot-contract integrity ----------


def _assert_snapshot_contract(snap):
    """Every integrity assertion of the frozen v1 snapshot contract.
    Applied to any fixture snapshot: unit 1a pins basic.json, unit 1b
    pins lookup.json, later units pin their own fixtures."""
    assert set(snap) == TOP_LEVEL_FIELDS

    version = snap["schema_version"]
    assert isinstance(version, int) and not isinstance(version, bool)
    assert version == 1

    snapshot_id = snap["snapshot_id"]
    assert isinstance(snapshot_id, str)
    assert re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
    body = {k: v for k, v in snap.items() if k != "snapshot_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    assert snapshot_id == hashlib.sha256(canonical).hexdigest()

    tick = snap["tick_epoch"]
    assert isinstance(tick, int) and not isinstance(tick, bool)
    assert tick >= 0

    cadence = snap["cadence_s"]
    assert isinstance(cadence, int) and not isinstance(cadence, bool)
    assert cadence > 0

    order = snap["provider_order"]
    assert isinstance(order, list)
    assert order, "provider_order must list active providers"
    assert all(isinstance(key, str) and key for key in order)
    assert len(order) == len(set(order)), "provider_order has duplicates"
    assert set(order) == set(snap["providers"])

    # Contract: provider_order includes providers with zero tracked IDs.
    empty = [key for key, prov in snap["providers"].items()
             if not prov["model_ids"]]
    assert empty, "fixture must include a zero-ID provider"
    for key in empty:
        assert key in order

    for key, prov in snap["providers"].items():
        assert key.startswith(SYNTHETIC_PROVIDER_PREFIX)
        assert set(prov) == PROVIDER_FIELDS
        assert isinstance(prov["display_name"], str) and prov["display_name"]
        assert isinstance(prov["chat_completions_url"], str)
        assert isinstance(prov["api_type"], str) and prov["api_type"]
        ids = prov["model_ids"]
        assert isinstance(ids, list)
        assert all(isinstance(i, str) and i for i in ids)
        assert ids == sorted(set(ids)), "model_ids must be sorted+deduplicated"
        assert set(prov["models"]) == set(ids), \
            "models must be keyed by exactly the tracked raw IDs"
        assert isinstance(prov["stale"], bool)

    for prov in snap["providers"].values():
        for raw_id, model in prov["models"].items():
            assert set(model) == MODEL_FIELDS
            assert model["id"] == raw_id
            assert isinstance(model["name"], str) and model["name"]
            assert model["description"] is None or \
                isinstance(model["description"], str)
            ctx = model["context_length"]
            assert ctx is None or (isinstance(ctx, int)
                                   and not isinstance(ctx, bool) and ctx >= 0)
            assert set(model["architecture"]) == ARCHITECTURE_FIELDS
            for modality in ("input_modalities", "output_modalities"):
                values = model["architecture"][modality]
                assert isinstance(values, list)
                assert all(isinstance(v, str) and v for v in values)
            params = model["supported_parameters"]
            assert isinstance(params, list)
            assert all(isinstance(p, str) and p for p in params)
            _assert_pricing_contract(model["pricing"])

    warnings = snap["warnings"]
    assert isinstance(warnings, list)
    assert all(isinstance(w, str) for w in warnings)
    assert set(warnings) <= WARNING_CODES

    keys = set(order)
    assert keys, "fixture must contain providers"
    assert all(key.startswith(SYNTHETIC_PROVIDER_PREFIX) for key in keys)

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in FORBIDDEN_KEYS, \
                    f"private/roster key leaked into snapshot: {key}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(snap)


# ---------- fixture parsing ----------


def test_fixture_parses_as_strict_json():
    snap = _load_fixture()  # raises on NaN/Infinity constants
    assert isinstance(snap, dict)


# ---------- top-level contract (unit 1a: basic.json) ----------


def test_top_level_field_set_is_exact(snapshot):
    _assert_snapshot_contract(snapshot)


def test_schema_version_is_v1(snapshot):
    _assert_snapshot_contract(snapshot)


def test_snapshot_id_is_sha256_of_canonical_body(snapshot):
    _assert_snapshot_contract(snapshot)


def test_tick_epoch_is_fixed_non_negative_int(snapshot):
    _assert_snapshot_contract(snapshot)


def test_cadence_s_is_positive_int(snapshot):
    _assert_snapshot_contract(snapshot)


def test_provider_order_matches_provider_map(snapshot):
    _assert_snapshot_contract(snapshot)


def test_provider_order_includes_zero_id_providers(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- provider entries ----------


def test_provider_entries_match_contract(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- projected model fields ----------


def test_model_projections_match_contract(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- warnings ----------


def test_warnings_use_frozen_codes(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- synthetic, self-contained, credential-free ----------


def test_fixture_is_synthetic_not_live(snapshot):
    _assert_snapshot_contract(snapshot)


def test_fixture_carries_no_private_or_roster_internal_keys(snapshot):
    _assert_snapshot_contract(snapshot)


# ---------- unit 1b: lookup.json fixture integrity ----------


def test_lookup_fixture_matches_snapshot_contract(lookup_snapshot):
    _assert_snapshot_contract(lookup_snapshot)


# ---------- unit 1b: cases.json integrity ----------


def _iter_get_model_cases(cases_doc):
    return [case for case in cases_doc["cases"]
            if case["tool"] == "get_model"]


def _case_fixture(case, cases_doc):
    name = case.get("fixture", cases_doc["fixture"])
    return _load_strict(FIXTURE_DIR / name)


def test_cases_document_parses_as_strict_json():
    # _load_strict would have raised on NaN/Infinity constants.
    doc = _load_strict(CASES_DOC)
    assert isinstance(doc["cases"], list) and doc["cases"]
    assert isinstance(doc["marker_strip_rule"], list) \
        and doc["marker_strip_rule"]


def test_cases_reference_snapshots_matching_contract(cases_document):
    default = cases_document["fixture"]
    assert isinstance(default, str) and default
    for case in cases_document["cases"]:
        name = case.get("fixture", default)
        _assert_snapshot_contract(_load_strict(FIXTURE_DIR / name))


def test_get_model_golden_payloads_are_wellformed(cases_document):
    for case in _iter_get_model_cases(cases_document):
        expect = case["expect"]
        snap = _case_fixture(case, cases_document)
        assert expect["ok"] is True
        assert expect["query"] == case["args"]["model_id"]
        assert expect["snapshot_id"] == snap["snapshot_id"]
        assert expect["tick_epoch"] == snap["tick_epoch"]
        assert set(expect["exact_matches"]) == set(snap["provider_order"])
        assert all(isinstance(v, bool)
                   for v in expect["exact_matches"].values())
        assert expect["exact_gateways"] == [
            p for p in snap["provider_order"] if expect["exact_matches"][p]]
        # Contract: "Preserve the absence caveat" on every payload.
        assert ABSENCE_CAVEAT in expect["caveats"]
        counts = expect["counts"]
        assert counts["endpoints"] == len(expect["endpoints"])
        assert counts["exact_matches"] == len(expect["exact_gateways"])


def test_get_model_endpoints_are_tracked_pairs_with_attached_metadata(
        cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        for endpoint in case["expect"]["endpoints"]:
            provider = endpoint["provider"]
            assert provider in snap["provider_order"]
            entry = snap["providers"][provider]
            raw_id = endpoint["model_id"]
            assert raw_id in entry["model_ids"]
            assert endpoint["chat_completions_url"] == \
                entry["chat_completions_url"]
            assert endpoint["api_type"] == entry["api_type"]
            assert endpoint["stale"] == entry["stale"]
            # Metadata stays attached to its own (provider, raw id) pair.
            assert endpoint["metadata"] == entry["models"][raw_id]


def test_get_model_endpoints_match_marker_rule_and_order(cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        query = case["args"]["model_id"]
        needle = _strip_marker_contract(query)
        expected = []
        for provider in snap["provider_order"]:
            for raw_id in snap["providers"][provider]["model_ids"]:
                if _strip_marker_contract(raw_id) == needle:
                    expected.append((provider, raw_id))
        expected.sort(key=lambda pair: (
            snap["provider_order"].index(pair[0]),
            _natural_key_contract(pair[1])))
        golden = [(e["provider"], e["model_id"])
                  for e in case["expect"]["endpoints"]]
        assert golden == expected, \
            f"{case['id']}: endpoints violate marker rule or ordering"


def test_no_inferred_identity_renamed_models_never_marker_match(
        cases_document):
    base = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == "get_model_marker_base_no_exact")
    endpoint_ids = [e["model_id"] for e in base["expect"]["endpoints"]]
    assert "vendor/model-renamed" not in endpoint_ids
    # The renamed model differs from the oracle base by more than a
    # delimiter-bounded free marker, so no marker rule may group them.
    assert _strip_marker_contract("vendor/model-renamed") == \
        "vendor/model-renamed"
    assert _strip_marker_contract("vendor/model-renamed") != \
        _strip_marker_contract("vendor/model")


def test_marker_strip_rule_matches_contract_restatement(cases_document):
    for row in cases_document["marker_strip_rule"]:
        raw_id, stripped = row["raw_id"], row["stripped"]
        assert isinstance(raw_id, str) and raw_id
        assert isinstance(stripped, str) and stripped
        assert _strip_marker_contract(raw_id) == stripped, (
            f"marker rule mismatch for {raw_id!r}: "
            f"contract restates {stripped!r}")


def test_unknown_id_absence_is_success_not_error(cases_document):
    unknown = next(c for c in _iter_get_model_cases(cases_document)
                   if c["id"] == "get_model_unknown_id_empty_success")
    expect = unknown["expect"]
    assert expect["ok"] is True
    assert expect["endpoints"] == []
    assert expect["exact_gateways"] == []
    # The id is genuinely absent from the fixture: a no-match lookup
    # degrades to an empty successful result, never an error payload.
    tracked = {
        raw_id
        for prov in _case_fixture(unknown, cases_document)["providers"].values()
        for raw_id in prov["model_ids"]}
    assert unknown["args"]["model_id"] not in tracked


def test_padded_query_golden_pins_no_trimming(cases_document):
    padded = next(c for c in _iter_get_model_cases(cases_document)
                  if c["id"] == "get_model_padded_query_not_trimmed")
    expect = padded["expect"]
    assert expect["ok"] is True
    assert expect["endpoints"] == []
    # The pin bites: the unpadded base DOES have marker matches.
    snap = _case_fixture(padded, cases_document)
    needle = _strip_marker_contract(padded["args"]["model_id"].strip())
    assert any(
        _strip_marker_contract(raw_id) == needle
        for prov in snap["providers"].values()
        for raw_id in prov["model_ids"])


# ---------- frozen get_model success envelope (envelope-freeze correction) ----------


def _assert_get_model_payload(case, snap):
    """Every integrity assertion of the frozen successful PURE get_model
    payload (README "Success envelope — `get_model`"). Validates the
    hand-authored golden against its own snapshot fixture with stdlib
    data operations only — no query implementation is imported, and the
    expected result is never generated by a future implementation."""
    expect = case["expect"]

    # Exact top-level field set: a stray or legacy key is corruption.
    assert set(expect) == GET_MODEL_RESPONSE_FIELDS, (
        f"{case['id']}: unexpected response fields "
        f"{sorted(set(expect) ^ GET_MODEL_RESPONSE_FIELDS)}")

    # Success is explicit — never an error envelope.
    assert expect["ok"] is True
    assert isinstance(expect["query"], str)
    assert expect["query"] == case["args"]["model_id"], \
        "query must be the raw argument, never trimmed or repaired"

    # Observation provenance — the payload mirrors the consumed snapshot.
    assert isinstance(expect["snapshot_id"], str)
    assert re.fullmatch(r"[0-9a-f]{64}", expect["snapshot_id"])
    assert expect["snapshot_id"] == snap["snapshot_id"]
    assert expect["tick_epoch"] == snap["tick_epoch"]
    tick = expect["tick_epoch"]
    assert tick is None or (isinstance(tick, int)
                            and not isinstance(tick, bool))

    # Literal raw-ID membership covers ALL snapshot providers.
    assert set(expect["exact_matches"]) == set(snap["provider_order"])
    assert all(isinstance(v, bool)
               for v in expect["exact_matches"].values())
    assert expect["exact_gateways"] == [
        p for p in snap["provider_order"] if expect["exact_matches"][p]]

    # Provider staleness covers all snapshot providers and mirrors the
    # roster's carried-forward stale flags (a distinct signal from tick
    # age, which is derived from tick_epoch/cadence_s).
    assert set(expect["provider_staleness"]) == set(snap["provider_order"])
    assert all(isinstance(v, bool)
               for v in expect["provider_staleness"].values())
    for provider in snap["provider_order"]:
        assert expect["provider_staleness"][provider] == \
            snap["providers"][provider]["stale"], \
            f"{case['id']}: provider staleness mismatch for {provider}"

    # Endpoints: tracked pairs, per-pair projected metadata, order.
    assert isinstance(expect["endpoints"], list)
    for endpoint in expect["endpoints"]:
        assert set(endpoint) == ENDPOINT_FIELDS, (
            f"{case['id']}: unexpected endpoint fields "
            f"{sorted(set(endpoint) ^ ENDPOINT_FIELDS)}")
        provider = endpoint["provider"]
        raw_id = endpoint["model_id"]
        assert provider in snap["providers"]
        entry = snap["providers"][provider]
        assert isinstance(raw_id, str) and raw_id in entry["model_ids"], \
            f"{case['id']}: endpoint pair ({provider!r}, {raw_id!r}) " \
            "is not a tracked pair"
        assert endpoint["chat_completions_url"] == \
            entry["chat_completions_url"]
        assert endpoint["api_type"] == entry["api_type"]
        assert isinstance(endpoint["stale"], bool)
        assert endpoint["stale"] == entry["stale"] == \
            expect["provider_staleness"][provider], \
            f"{case['id']}: endpoint staleness does not match provider " \
            "staleness"
        # `metadata` is the projected public model object of THIS exact
        # (provider, raw model_id) pair — never the legacy raw object.
        metadata = endpoint["metadata"]
        assert isinstance(metadata, dict)
        assert set(metadata) == MODEL_FIELDS
        assert metadata == entry["models"][raw_id]
        assert metadata["id"] == raw_id
        _assert_pricing_contract(metadata["pricing"])

    # The absence caveat is preserved on every payload.
    assert isinstance(expect["caveats"], list) and expect["caveats"]
    assert ABSENCE_CAVEAT in expect["caveats"]

    # Counts: exact set, strict non-negative ints (booleans invalid),
    # and the frozen cardinality definitions over the returned result.
    counts = expect["counts"]
    assert set(counts) == COUNT_FIELDS, (
        f"{case['id']}: unexpected count fields "
        f"{sorted(set(counts) ^ COUNT_FIELDS)}")
    for name, value in counts.items():
        assert _counts_are_strict_int(value), \
            f"{case['id']}: counts.{name} must be a non-negative int"
    assert counts["exact_matches"] == len(expect["exact_gateways"])
    assert counts["endpoints"] == len(expect["endpoints"])
    assert counts["distinct_raw_ids"] == \
        len({e["model_id"] for e in expect["endpoints"]})
    assert counts["marker_group_labels"] == \
        len({_strip_marker_contract(e["model_id"])
             for e in expect["endpoints"]})

    # Array order (provider display order, then natural raw-ID order)
    # and marker-rule membership, recomputed independently from the
    # snapshot — never read back from the golden.
    needle = _strip_marker_contract(expect["query"])
    expected_pairs = []
    for provider in snap["provider_order"]:
        for raw_id in snap["providers"][provider]["model_ids"]:
            if _strip_marker_contract(raw_id) == needle:
                expected_pairs.append((provider, raw_id))
    expected_pairs.sort(key=lambda pair: (
        snap["provider_order"].index(pair[0]),
        _natural_key_contract(pair[1])))
    golden_pairs = [(e["provider"], e["model_id"])
                    for e in expect["endpoints"]]
    assert golden_pairs == expected_pairs, \
        f"{case['id']}: endpoints violate marker rule or ordering"


def _copy_case(cases_document, case_id):
    """Deep-copied golden case + its snapshot, for in-memory mutation
    positive controls. No committed fixture is ever corrupted."""
    case = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == case_id)
    return copy.deepcopy(case), _case_fixture(case, cases_document)


def test_get_model_response_envelope_field_sets_are_frozen(cases_document):
    assert set(GET_MODEL_RESPONSE_FIELDS) == {
        "ok", "query", "snapshot_id", "tick_epoch", "exact_matches",
        "exact_gateways", "provider_staleness", "endpoints", "caveats",
        "counts"}
    assert set(ENDPOINT_FIELDS) == {
        "provider", "model_id", "chat_completions_url", "api_type",
        "stale", "metadata"}
    assert set(COUNT_FIELDS) == {
        "exact_matches", "endpoints", "distinct_raw_ids",
        "marker_group_labels"}
    for case in _iter_get_model_cases(cases_document):
        _assert_get_model_payload(case, _case_fixture(case, cases_document))


def test_get_model_counts_use_strict_int_types(cases_document):
    for case in _iter_get_model_cases(cases_document):
        counts = case["expect"]["counts"]
        assert set(counts) == COUNT_FIELDS
        for name, value in counts.items():
            assert _counts_are_strict_int(value), \
                f"{case['id']}: counts.{name}={value!r} is not a strict int"
    # bool subclasses int, so the guard must be tested explicitly.
    assert not _counts_are_strict_int(True)
    assert not _counts_are_strict_int(False)


def test_get_model_provider_staleness_covers_all_providers(cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        staleness = case["expect"]["provider_staleness"]
        assert set(staleness) == set(snap["provider_order"])
        for provider in snap["provider_order"]:
            assert staleness[provider] is \
                snap["providers"][provider]["stale"]


def test_get_model_endpoint_staleness_matches_provider_staleness(
        cases_document):
    for case in _iter_get_model_cases(cases_document):
        expect = case["expect"]
        for endpoint in expect["endpoints"]:
            assert endpoint["stale"] is \
                expect["provider_staleness"][endpoint["provider"]]


def test_get_model_metadata_is_projected_pair_object(cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        for endpoint in case["expect"]["endpoints"]:
            metadata = endpoint["metadata"]
            # Non-null projected public object; the legacy raw metadata
            # object is never exposed (exact field set only).
            assert isinstance(metadata, dict)
            assert set(metadata) == MODEL_FIELDS
            assert metadata["id"] == endpoint["model_id"]
            assert metadata == snap["providers"][endpoint["provider"]][
                "models"][endpoint["model_id"]]


def test_get_model_observation_provenance_matches_snapshot(cases_document):
    for case in _iter_get_model_cases(cases_document):
        snap = _case_fixture(case, cases_document)
        expect = case["expect"]
        assert expect["snapshot_id"] == snap["snapshot_id"]
        assert expect["tick_epoch"] == snap["tick_epoch"]
        assert re.fullmatch(r"[0-9a-f]{64}", expect["snapshot_id"])


def test_get_model_absence_is_zero_count_success(cases_document):
    unknown = next(c for c in _iter_get_model_cases(cases_document)
                   if c["id"] == "get_model_unknown_id_empty_success")
    expect = unknown["expect"]
    snap = _case_fixture(unknown, cases_document)
    assert expect["ok"] is True
    assert expect["exact_gateways"] == [] and expect["endpoints"] == []
    assert expect["counts"] == {
        "exact_matches": 0, "endpoints": 0,
        "distinct_raw_ids": 0, "marker_group_labels": 0}
    # Zero results are still a success, and provider_staleness still
    # covers every snapshot provider.
    assert set(expect["provider_staleness"]) == set(snap["provider_order"])


def test_get_model_shared_raw_id_is_two_endpoints_one_distinct_id(
        cases_document):
    shared = next(c for c in _iter_get_model_cases(cases_document)
                  if c["id"] == "get_model_shared_raw_id_two_endpoints")
    counts = shared["expect"]["counts"]
    assert counts["endpoints"] == 2
    assert counts["distinct_raw_ids"] == 1
    assert counts["exact_matches"] == 2
    raw_ids = {e["model_id"] for e in shared["expect"]["endpoints"]}
    assert raw_ids == {"vendor/shared"}


def test_get_model_marker_variant_separates_raw_ids_from_marker_label(
        cases_document):
    base = next(c for c in _iter_get_model_cases(cases_document)
                if c["id"] == "get_model_marker_base_no_exact")
    counts = base["expect"]["counts"]
    # Two marker variants, two distinct raw ids, one marker-group label,
    # and no literal raw-ID membership (the query id itself is absent).
    assert counts["endpoints"] == 2
    assert counts["distinct_raw_ids"] == 2
    assert counts["marker_group_labels"] == 1
    assert counts["exact_matches"] == 0
    assert counts["distinct_raw_ids"] != counts["marker_group_labels"]


def test_pricing_contract_accepts_documented_null_forms():
    # Whole object unknown.
    _assert_pricing_contract(None)
    # Individually unknown scalars.
    _assert_pricing_contract({"prompt": None, "completion": None})
    _assert_pricing_contract({"prompt": "0", "completion": None})
    _assert_pricing_contract({"prompt": None, "completion": 0})
    # Provider-returned valid scalar types are preserved.
    _assert_pricing_contract({"prompt": "0", "completion": 0})
    _assert_pricing_contract({"prompt": 0.5, "completion": "1"})


def test_pricing_contract_rejects_bools_and_non_finite():
    for bad in ({"prompt": True, "completion": "0"},
                {"prompt": "0", "completion": False},
                {"prompt": float("inf"), "completion": "0"},
                {"prompt": float("nan"), "completion": "0"},
                {"prompt": "free", "completion": "0"}):
        with pytest.raises(AssertionError):
            _assert_pricing_contract(bad)
    with pytest.raises(AssertionError):
        _assert_pricing_contract({"prompt": "0"})  # missing scalar


def test_get_model_integrity_rejects_unexpected_response_key(cases_document):
    case, snap = _copy_case(cases_document, "get_model_marker_base_no_exact")
    case["expect"]["surprise"] = 1
    with pytest.raises(AssertionError, match="unexpected response fields"):
        _assert_get_model_payload(case, snap)


def test_get_model_integrity_rejects_bool_count(cases_document):
    case, snap = _copy_case(cases_document, "get_model_marker_base_no_exact")
    case["expect"]["counts"]["endpoints"] = True
    with pytest.raises(AssertionError, match="non-negative int"):
        _assert_get_model_payload(case, snap)


def test_get_model_integrity_rejects_endpoint_raw_id_conflation(
        cases_document):
    case, snap = _copy_case(cases_document, "get_model_marker_base_no_exact")
    # Claim synthetic-a's raw id on the synthetic-b endpoint pair.
    assert case["expect"]["endpoints"][1]["provider"] == "synthetic-b"
    case["expect"]["endpoints"][1]["model_id"] = "vendor/model:free"
    with pytest.raises(AssertionError, match="not a tracked pair"):
        _assert_get_model_payload(case, snap)


def test_get_model_integrity_rejects_staleness_mismatch(cases_document):
    case, snap = _copy_case(cases_document,
                            "get_model_shared_raw_id_two_endpoints")
    # Baseline is all-false; flip ONLY the payload's provider flag.
    assert snap["providers"]["synthetic-a"]["stale"] is False
    case["expect"]["provider_staleness"]["synthetic-a"] = True
    with pytest.raises(AssertionError, match="provider staleness mismatch"):
        _assert_get_model_payload(case, snap)


def test_get_model_integrity_positive_control_recognises_true_staleness(
        cases_document):
    # The all-false baseline cannot prove TRUE-value handling, so mutate
    # the snapshot copy AND the payload to a consistent genuine stale=true
    # pair and require the assertion to PASS...
    case, snap = _copy_case(cases_document,
                            "get_model_shared_raw_id_two_endpoints")
    snap["providers"]["synthetic-a"]["stale"] = True
    case["expect"]["provider_staleness"]["synthetic-a"] = True
    case["expect"]["endpoints"][0]["stale"] = True
    _assert_get_model_payload(case, snap)
    # ...then break only the payload flag: the assertion must catch it.
    case["expect"]["endpoints"][0]["stale"] = False
    with pytest.raises(AssertionError, match="endpoint staleness"):
        _assert_get_model_payload(case, snap)


# ---------- README contract-draft pins ----------


def _contract_section_text():
    """The README body of the v1 contract section ONLY (from the
    `## Published catalogue snapshot (v1)` heading to the next `## `
    heading). Scoping the pins here means unrelated prose elsewhere in
    README.md can never accidentally satisfy a contract assertion."""
    text = README.read_text(encoding="utf-8")
    heading = f"## {CONTRACT_SECTION}"
    start = text.index(heading)
    body = text[start:]
    end = body.find("\n## ", len(heading))
    return body if end == -1 else body[:end]


def test_readme_contract_section_freezes_snapshot_fields():
    text = _contract_section_text()
    assert CONTRACT_SECTION in text
    for field in sorted(TOP_LEVEL_FIELDS | PROVIDER_FIELDS | MODEL_FIELDS
                        | ARCHITECTURE_FIELDS | PRICING_FIELDS):
        assert f"`{field}`" in text, f"contract draft missing field {field}"


def test_readme_contract_freezes_tool_surface():
    text = _contract_section_text()
    for tool in ("list_free_models", "get_model", "list_endpoints",
                 "watchdog_status", "search_models"):
        assert f"`{tool}(" in text, f"contract draft missing tool {tool}"


def test_readme_contract_freezes_warning_vocabulary():
    text = _contract_section_text()
    for code in sorted(WARNING_CODES):
        assert f"`{code}`" in text, f"contract draft missing warning {code}"


def test_readme_contract_freezes_error_rules():
    text = _contract_section_text()
    for phrase in ("fail export", "NaN", "Infinity",
                   "duplicate registry keys", "last published artifact"):
        assert phrase in text, f"contract draft missing error rule: {phrase}"


def test_readme_contract_freezes_identity_rules():
    text = _contract_section_text()
    for phrase in ("literal raw-ID membership", "[-:_]",
                   "empty-result guard",
                   "not proof of cross-gateway model identity",
                   "(provider, raw model_id)", "never silently trimmed"):
        assert phrase in text, \
            f"contract draft missing identity rule: {phrase}"


def test_readme_contract_freezes_get_model_envelope_field_sets():
    text = _contract_section_text()
    for field in sorted(GET_MODEL_RESPONSE_FIELDS | ENDPOINT_FIELDS
                        | COUNT_FIELDS):
        assert f"`{field}`" in text, \
            f"contract section missing envelope field {field}"


def test_readme_contract_freezes_count_definitions():
    text = _contract_section_text()
    for phrase in ("Matching gateways", "distinct raw", "marker group",
                   "never the global registry", "all zeros",
                   "provider display order"):
        assert phrase in text, \
            f"contract section missing count rule: {phrase}"


def test_readme_contract_distinguishes_provider_staleness_from_tick_age():
    text = _contract_section_text()
    for phrase in ("provider staleness, never tick age",
                   "carried-forward stale flag", "provider_staleness"):
        assert phrase in text, \
            f"contract section missing staleness rule: {phrase}"


def test_readme_contract_freezes_metadata_projection_boundary():
    text = _contract_section_text()
    for phrase in ("projected public model object",
                   "never the legacy raw metadata",
                   "PRESERVES raw tracked metadata"):
        assert phrase in text, \
            f"contract section missing metadata boundary: {phrase}"


def test_readme_contract_resolves_pricing_nullability():
    text = _contract_section_text()
    for phrase in ("whole object", "individually unknown", "Booleans"):
        assert phrase in text, \
            f"contract section missing pricing rule: {phrase}"
