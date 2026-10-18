"""Contract-integrity tests — standalone catalogue snapshot v1 (unit 1a).

Contract-only stage: stdlib data operations only. These tests pin the
frozen v1 snapshot contract (README.md, "Published catalogue snapshot (v1)
— contract draft") against the synthetic golden fixture
tests/fixtures/catalog/basic.json. They import NO watchdog_catalog
implementation module — validator/query tests are introduced red within
their corresponding Task 2/3 implementation units and made green there.

The fixture is synthetic by contract: provider keys live in the
documented `synthetic-` namespace, so no fixture encodes the live
provider count or depends on production files.
"""

import hashlib
import json
import math
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
FIXTURE = REPO / "tests" / "fixtures" / "catalog" / "basic.json"

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


def _reject_constant(constant):
    raise ValueError(f"non-finite JSON constant: {constant}")


def _load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"),
                      parse_constant=_reject_constant)


@pytest.fixture(scope="module")
def snapshot():
    return _load_fixture()


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


# ---------- fixture parsing ----------


def test_fixture_parses_as_strict_json():
    snapshot = _load_fixture()  # raises on NaN/Infinity constants
    assert isinstance(snapshot, dict)


# ---------- top-level contract ----------


def test_top_level_field_set_is_exact(snapshot):
    assert set(snapshot) == TOP_LEVEL_FIELDS


def test_schema_version_is_v1(snapshot):
    version = snapshot["schema_version"]
    assert isinstance(version, int) and not isinstance(version, bool)
    assert version == 1


def test_snapshot_id_is_sha256_of_canonical_body(snapshot):
    snapshot_id = snapshot["snapshot_id"]
    assert isinstance(snapshot_id, str)
    assert re.fullmatch(r"[0-9a-f]{64}", snapshot_id)
    body = {k: v for k, v in snapshot.items() if k != "snapshot_id"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("utf-8")
    assert snapshot_id == hashlib.sha256(canonical).hexdigest()


def test_tick_epoch_is_fixed_non_negative_int(snapshot):
    tick = snapshot["tick_epoch"]
    assert isinstance(tick, int) and not isinstance(tick, bool)
    assert tick >= 0


def test_cadence_s_is_positive_int(snapshot):
    cadence = snapshot["cadence_s"]
    assert isinstance(cadence, int) and not isinstance(cadence, bool)
    assert cadence > 0


def test_provider_order_matches_provider_map(snapshot):
    order = snapshot["provider_order"]
    assert isinstance(order, list)
    assert order, "provider_order must list active providers"
    assert all(isinstance(key, str) and key for key in order)
    assert len(order) == len(set(order)), "provider_order has duplicates"
    assert set(order) == set(snapshot["providers"])


def test_provider_order_includes_zero_id_providers(snapshot):
    # Contract: provider_order includes providers with zero tracked IDs.
    empty = [key for key, prov in snapshot["providers"].items()
             if not prov["model_ids"]]
    assert empty, "basic fixture must include a zero-ID provider"
    for key in empty:
        assert key in snapshot["provider_order"]


# ---------- provider entries ----------


def test_provider_entries_match_contract(snapshot):
    for key, prov in snapshot["providers"].items():
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


# ---------- projected model fields ----------


def test_model_projections_match_contract(snapshot):
    for prov in snapshot["providers"].values():
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
            assert set(model["pricing"]) == PRICING_FIELDS
            for value in model["pricing"].values():
                assert _is_finite_scalar(value)


# ---------- warnings ----------


def test_warnings_use_frozen_codes(snapshot):
    warnings = snapshot["warnings"]
    assert isinstance(warnings, list)
    assert all(isinstance(w, str) for w in warnings)
    assert set(warnings) <= WARNING_CODES


# ---------- synthetic, self-contained, credential-free ----------


def test_fixture_is_synthetic_not_live(snapshot):
    keys = set(snapshot["provider_order"])
    assert keys, "fixture must contain providers"
    assert all(key.startswith(SYNTHETIC_PROVIDER_PREFIX) for key in keys)


def test_fixture_carries_no_private_or_roster_internal_keys(snapshot):
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in FORBIDDEN_KEYS, \
                    f"private/roster key leaked into snapshot: {key}"
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(snapshot)


# ---------- README contract-draft pins ----------


def test_readme_contract_section_freezes_snapshot_fields():
    text = README.read_text(encoding="utf-8")
    assert CONTRACT_SECTION in text
    for field in sorted(TOP_LEVEL_FIELDS | PROVIDER_FIELDS | MODEL_FIELDS
                        | ARCHITECTURE_FIELDS | PRICING_FIELDS):
        assert f"`{field}`" in text, f"contract draft missing field {field}"


def test_readme_contract_freezes_tool_surface():
    text = README.read_text(encoding="utf-8")
    for tool in ("list_free_models", "get_model", "list_endpoints",
                 "watchdog_status", "search_models"):
        assert f"`{tool}(" in text, f"contract draft missing tool {tool}"


def test_readme_contract_freezes_warning_vocabulary():
    text = README.read_text(encoding="utf-8")
    for code in sorted(WARNING_CODES):
        assert f"`{code}`" in text, f"contract draft missing warning {code}"


def test_readme_contract_freezes_error_rules():
    text = README.read_text(encoding="utf-8")
    for phrase in ("fail export", "NaN", "Infinity",
                   "duplicate registry keys", "last published artifact"):
        assert phrase in text, f"contract draft missing error rule: {phrase}"
