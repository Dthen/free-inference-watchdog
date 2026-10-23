"""Typed SDK registration and direct-call contract for the stdio MCP (unit 6b).

Two unparametrized groups:

1. ``test_typed_registration_discovery_contract`` builds the factory with a
   source double that fails if acquired and a clock that fails if read. It
   discovers exactly the five reviewed names/descriptions/order, normalizes the
   SDK's ACTUALLY advertised input schemas against the packaged ``tools.json``
   oracle, proves the comparator keeps a numeric-bound and an
   ``additionalProperties`` difference, and runs a fresh interpreter to show
   that the package/core import graph still excludes the transport, SDK,
   producer, provider, config, state and site modules.

2. ``test_direct_sdk_calls_and_raw_argument_boundary`` calls every tool through
   the real ``MCPServer.call_tool`` and compares the payload with the pure core
   plus exactly one acquisition's ``with_source`` attachment. A table of
   original-value rows covers bools, numeric strings, encoded-list/null
   strings, integral floats, fractions, bounds, array members, required
   ``model_id`` and extras on every tool. Schema-invalid input must be rejected
   before the source seam; application errors may reach it. A real
   ``AsyncCatalogSource`` over an owned temporary fixture proves end-to-end
   source metadata without any network.

Honest scope: these are direct SDK calls, not stdio subprocess negotiation, not
a clean install and not the console command. ``structured_content`` is asserted
equal to the text JSON whenever this SDK supplies both (a bare ``dict`` return
annotation does not publish an output schema here).
"""

import asyncio
import copy
import importlib.resources
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from watchdog_catalog import catalog, query
from watchdog_catalog.server import create_server
from watchdog_catalog.source import AsyncCatalogSource, SourceError, with_source

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "catalog" / "basic.json"
SNAPSHOT = catalog.load_snapshot(FIXTURE.read_text(encoding="utf-8"))
MAX_SAFE_INT = 9007199254740991
MODEL_ID_PATTERN = r"[^\u0009-\u000D\u0020]"
STATUS_NOW = 1759200100
SOURCE_NOW = 1759200010
PROVENANCE = {
    "mode": "file",
    "url": None,
    "cached": False,
    "fetched_at": 1759200000,
    "fetch_age_s": 0,
    "observation_age_s": 0,
    "observation_fresh": True,
    "not_modified": False,
    "warnings": [],
}
ACQUISITION = {"ok": True, "snapshot": SNAPSHOT, "source": PROVENANCE}

#: Normalizations that are documented presentation-only equivalences, never
#: semantic relaxations: generated titles, prose descriptions (tool-level prose
#: is compared directly from the resource), absent-vs-empty ``required``,
#: ``anyOf`` nullable versus a ``type`` array, and list order for unordered
#: schema arrays. An absent ``additionalProperties`` stays absent.
PRESENTATION_ONLY_KEYS = frozenset({"title", "description"})


class _AcquisitionDouble:
    """The narrow source-acquisition seam: no core, SDK or transport fake."""

    def __init__(self, result):
        self.result = copy.deepcopy(result)
        self.calls = 0

    async def aget(self):
        self.calls += 1
        return copy.deepcopy(self.result)


class _ExplodingSource:
    def __init__(self):
        self.calls = 0

    async def aget(self):  # pragma: no cover - only a factory leak reaches it
        self.calls += 1
        raise AssertionError("factory construction or discovery acquired")


class _ExplodingClock:
    def __init__(self):
        self.reads = 0

    def __call__(self):  # pragma: no cover - only a factory leak reads it
        self.reads += 1
        raise AssertionError("factory construction or discovery read the clock")


def _contract_entries():
    resource = importlib.resources.files("watchdog_catalog").joinpath("tools.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def _canonical(node):
    """Canonicalize documented presentation-only schema differences only."""
    if isinstance(node, list):
        return [_canonical(item) for item in node]
    if not isinstance(node, dict):
        return node
    result = {key: _canonical(value) for key, value in node.items()
              if key not in PRESENTATION_ONLY_KEYS}
    if "anyOf" in result:
        arms = result.pop("anyOf")
        nulls = [arm for arm in arms if arm == {"type": "null"}]
        rest = [arm for arm in arms if arm != {"type": "null"}]
        if len(nulls) == 1 and len(rest) == 1:
            merged = dict(rest[0])
            merged.update(result)
            if isinstance(merged.get("type"), str):
                merged["type"] = sorted([merged["type"], "null"])
            result = merged
        else:
            result["anyOf"] = arms
    if isinstance(result.get("type"), list):
        result["type"] = sorted(result["type"])
    if "required" in result:
        result["required"] = sorted(result["required"])
    if result.get("type") == "object" or "properties" in result:
        result.setdefault("required", [])
    return result


def _assert_schema_matches(actual, expected, label):
    left = _canonical(actual)
    right = _canonical(expected)
    assert left == right, (
        f"{label}: the SDK-advertised input schema drifted from tools.json\n"
        f"SDK:      {json.dumps(left, sort_keys=True)}\n"
        f"CONTRACT: {json.dumps(right, sort_keys=True)}")


def _has_key(node, needle):
    if isinstance(node, dict):
        return needle in node or any(_has_key(value, needle)
                                     for value in node.values())
    if isinstance(node, list):
        return any(_has_key(value, needle) for value in node)
    return False


_IMPORT_PROBE = r"""
import importlib.abc
import sys

repo, blocked_names = sys.argv[1], set(sys.argv[2].split(","))
sys.path.insert(0, repo)

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in blocked_names or fullname.split(".")[0] in blocked_names:
            raise ImportError("blocked import during package probe: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
import watchdog_catalog
from watchdog_catalog import catalog, query, source

assert "watchdog_catalog.server" not in sys.modules, "package import pulled transport"
loaded = {name.split(".")[0] for name in sys.modules}
assert loaded.isdisjoint(blocked_names), sorted(loaded & blocked_names)
assert source.CatalogSource and query.search_models and catalog.load_snapshot
"""


def test_typed_registration_discovery_contract():
    contract = _contract_entries()
    names = [entry["name"] for entry in contract]
    source = _ExplodingSource()
    clock = _ExplodingClock()

    server = create_server(source, wall_clock=clock)
    tools = asyncio.run(server.list_tools())

    assert [tool.name for tool in tools] == names, (
        "the five reviewed tools, in the reviewed order")
    by_name = {tool.name: tool for tool in tools}
    for entry in contract:
        tool = by_name[entry["name"]]
        assert tool.description == entry["description"], (
            f"{tool.name}: tool description must come from tools.json")
        _assert_schema_matches(tool.input_schema, entry["inputSchema"],
                               tool.name)
        assert set(tool.model_dump()["input_schema"]["properties"]) == set(
            entry["inputSchema"]["properties"])
        for closed in ("root", "clock", "wall_clock", "cache_dir", "source",
                       "snapshot", "path"):
            assert closed not in tool.input_schema["properties"], (
                f"{tool.name} exposes closure configuration {closed!r}")
        assert not _has_key(tool.input_schema, "enum"), (
            f"{tool.name} pins a static provider enum instead of the snapshot")
        provider = tool.input_schema["properties"].get("provider")
        if provider is not None:
            assert _canonical(provider)["type"] == ["null", "string"], (
                f"{tool.name}: provider stays a literal string, never an enum")

    assert source.calls == 0, "construction and discovery must not acquire"
    assert clock.reads == 0, "construction and discovery must not read a clock"

    # Explicit semantic spot checks, independent of the structural comparator:
    # requiredness, defaults, nullability, item types, numeric bounds and the
    # unknown-argument policy are the wire contract, not presentation.
    search = by_name["search_models"].input_schema
    assert search.get("required", []) == [], "absent required means []"
    assert search["additionalProperties"] is False
    assert _canonical(search["properties"]["limit"]) == {
        "type": "integer", "minimum": 1, "maximum": 200, "default": 50}
    assert search["properties"]["offset"]["default"] == 0
    assert search["properties"]["offset"]["minimum"] == 0
    assert search["properties"]["offset"]["maximum"] == MAX_SAFE_INT
    assert _canonical(search["properties"]["min_context_length"]) == {
        "type": ["integer", "null"], "minimum": 0, "maximum": MAX_SAFE_INT,
        "default": None}
    assert _canonical(search["properties"]["input_modalities"]) == {
        "type": ["array", "null"], "default": None,
        "items": {"type": "string", "minLength": 1}}
    lookup = by_name["get_model"].input_schema
    assert lookup["required"] == ["model_id"]
    assert lookup["properties"]["model_id"]["pattern"] == MODEL_ID_PATTERN
    for tool in tools:
        assert tool.input_schema["additionalProperties"] is False, (
            f"{tool.name} must reject unknown arguments")

    # The comparator itself must retain semantic differences, not normalise
    # them away. Positive control first: the untouched schema passes.
    candidate = by_name["search_models"].input_schema
    reviewed = next(entry["inputSchema"] for entry in contract
                    if entry["name"] == "search_models")
    _assert_schema_matches(candidate, reviewed, "positive control")
    widened = copy.deepcopy(candidate)
    widened["properties"]["limit"]["maximum"] = 201
    with pytest.raises(AssertionError):
        _assert_schema_matches(widened, reviewed, "numeric bound")
    opened = copy.deepcopy(candidate)
    opened.pop("additionalProperties")
    with pytest.raises(AssertionError):
        _assert_schema_matches(opened, reviewed, "absent additionalProperties")

    blocked = (
        "mcp", "mcp_types", "pydantic", "watchdog_catalog.server",
        "config_loader", "providers", "inference_watchdog", "catalog_export",
        "envfile", "notify", "diffing", "detection", "alive", "build_site",
        "pending_removals", "probe_select", "probe_state", "probe_zero_credit",
        "state",
    )
    probe = subprocess.run(
        [sys.executable, "-I", "-c", _IMPORT_PROBE, str(REPO),
         ",".join(blocked)],
        cwd=str(Path(__file__).resolve().parent),
        capture_output=True, text=True, timeout=120)
    assert probe.returncode == 0, probe.stderr[-4000:]
    assert probe.stdout == "" and probe.stderr == "", (
        "the package/core import graph stays silent outside the transport")


# --------------------------------------------------------------------------
# group 2: direct real SDK calls, payload equality and the raw boundary
# --------------------------------------------------------------------------
async def _payload(server, name, arguments):
    result = await server.call_tool(name, arguments)
    assert result.is_error is not True, f"{name}({arguments!r}) returned is_error"
    payload = json.loads(result.content[0].text)
    if result.structured_content is not None:
        assert result.structured_content == payload, (
            f"{name}: structured and text content disagree")
    return payload


def _expected(core, arguments, **kwargs):
    return with_source(core(SNAPSHOT, arguments, **kwargs), ACQUISITION)


VALID_ROWS = [
    ("list_free_models", {}, query.list_free_models),
    ("list_free_models", {"provider": "synthetic-a"}, query.list_free_models),
    ("list_free_models", {"provider": "synthetic-b"}, query.list_free_models),
    ("list_free_models", {"provider": ""}, query.list_free_models),
    ("list_endpoints", {}, query.list_endpoints),
    ("list_endpoints", {"provider": "synthetic-b"}, query.list_endpoints),
    ("get_model", {"model_id": "alpha-1:free"}, query.get_model),
    ("get_model", {"model_id": "alpha-1"}, query.get_model),
    ("get_model", {"model_id": "null"}, query.get_model),
    ("get_model", {"model_id": "\u00a0"}, query.get_model),
    ("search_models", {}, query.search_models),
    ("search_models", {"query": ""}, query.search_models),
    ("search_models", {"query": "  "}, query.search_models),
    ("search_models", {"query": "null"}, query.search_models),
    ("search_models", {"query": "[]"}, query.search_models),
    ("search_models", {"query": '["text"]'}, query.search_models),
    ("search_models", {"provider": "synthetic-a"}, query.search_models),
    ("search_models", {"provider": "null"}, query.search_models),
    ("search_models", {"provider": ""}, query.search_models),
    ("search_models", {"min_context_length": 0}, query.search_models),
    ("search_models", {"min_context_length": 1.0}, query.search_models),
    ("search_models", {"min_context_length": MAX_SAFE_INT},
     query.search_models),
    ("search_models", {"offset": MAX_SAFE_INT}, query.search_models),
    ("search_models", {"offset": 0.0}, query.search_models),
    ("search_models", {"limit": 1}, query.search_models),
    ("search_models", {"limit": 1.0}, query.search_models),
    ("search_models", {"limit": 200}, query.search_models),
    ("search_models", {"input_modalities": []}, query.search_models),
    ("search_models", {"input_modalities": ["text"]}, query.search_models),
    ("search_models", {"input_modalities": ["text", "text"]},
     query.search_models),
    ("search_models", {"supported_parameters": ["temperature"]},
     query.search_models),
    ("search_models",
     {"query": "alpha", "provider": "synthetic-a",
      "min_context_length": 8000, "input_modalities": ["text"],
      "supported_parameters": ["temperature"], "limit": 1, "offset": 0},
     query.search_models),
    ("search_models", {"limit": 1, "offset": 1}, query.search_models),
    ("watchdog_status", {}, query.watchdog_status),
]

INVALID_ROWS = {
    "list_free_models": [
        {"provider": 3},
        {"provider": []},
        {"provider": None, "bogus": 1},
        {"bogus": 1},
    ],
    "get_model": [
        {},
        {"model_id": None},
        {"model_id": 3},
        {"model_id": ["alpha"]},
        {"model_id": ""},
        {"model_id": " \t\n\r\v\f"},
        {"model_id": "alpha-1", "bogus": 1},
    ],
    "list_endpoints": [
        {"provider": 1.5},
        {"provider": {}},
        {"bogus": 1},
    ],
    "watchdog_status": [
        {"now": STATUS_NOW},
        {"x": 1},
    ],
    "search_models": [
        {"bogus": 1},
        {"query": 1},
        {"query": ["text"]},
        {"provider": 3},
        {"min_context_length": -1},
        {"min_context_length": 1.5},
        {"min_context_length": "5"},
        {"min_context_length": True},
        {"min_context_length": float("nan")},
        {"min_context_length": float("inf")},
        {"min_context_length": MAX_SAFE_INT + 1},
        {"offset": -1},
        {"offset": MAX_SAFE_INT + 1},
        {"offset": 2.5},
        {"offset": "0"},
        {"offset": False},
        {"limit": 0},
        {"limit": 201},
        {"limit": None},
        {"limit": True},
        {"limit": "5"},
        {"limit": 5.5},
        {"input_modalities": "[]"},
        {"input_modalities": [""]},
        {"input_modalities": ["text", 3]},
        {"input_modalities": ["text", None]},
        {"input_modalities": {"text": True}},
        {"supported_parameters": "[]"},
        {"supported_parameters": [""]},
        {"supported_parameters": [1]},
    ],
}


async def _direct_calls():
    source = _AcquisitionDouble(ACQUISITION)
    server = create_server(source, wall_clock=lambda: STATUS_NOW)

    for name, arguments, core in VALID_ROWS:
        kwargs = {"now": STATUS_NOW} if core is query.watchdog_status else {}
        before = source.calls
        payload = await _payload(server, name, arguments)
        assert source.calls == before + 1, (
            f"{name}: each accepted call acquires exactly once")
        expected = _expected(core, arguments, **kwargs)
        assert payload == expected, (
            f"{name}({arguments!r}) differs from the pure core + attachment")
        assert payload["source"] == PROVENANCE, (
            f"{name}: source attachment is exactly this acquisition's provenance")
        assert payload is not expected

    # Exactly one acquisition's provenance, detached: mutating a result cannot
    # reach the double's retained acquisition.
    before = source.calls
    mutated = await _payload(server, "get_model", {"model_id": "alpha-1:free"})
    mutated["source"]["warnings"].append("forged")
    mutated["endpoints"].clear()
    fresh = await _payload(server, "get_model", {"model_id": "alpha-1:free"})
    assert fresh == _expected(query.get_model, {"model_id": "alpha-1:free"})
    assert source.calls == before + 2

    # Schema-invalid originals never reach the source seam.
    for name, rows in INVALID_ROWS.items():
        for arguments in rows:
            before = source.calls
            with pytest.raises(ToolError) as rejected:
                await server.call_tool(name, arguments)
            assert type(rejected.value) is ToolError, (
                f"{name}({arguments!r}) must be a boundary rejection, not a crash")
            assert source.calls == before, (
                f"{name}({arguments!r}) was rejected only after acquisition")

    # A failed acquisition returns the unchanged semantic source error, fully
    # detached, for every tool, and never reads the status clock.
    failure = SourceError().to_payload()
    failing = _AcquisitionDouble(failure)
    clock = _ExplodingClock()
    broken = create_server(failing, wall_clock=clock)
    for name, arguments, _core in [(row[0], row[1], row[2])
                                   for row in VALID_ROWS
                                   if row[0] != "watchdog_status"]:
        payload = await _payload(broken, name, arguments)
        assert payload == failure, f"{name}: source error must pass through"
        payload["error"]["message"] = "forged"
        again = await _payload(broken, name, arguments)
        assert again == failure
    status = await _payload(broken, "watchdog_status", {})
    assert status == failure and clock.reads == 0, (
        "a failed acquisition short-circuits before the status clock")

    # The status clock is wrapper-owned and normalised to the core's native
    # non-negative safe-integer epoch.
    for reading, expected_now in ((STATUS_NOW, STATUS_NOW),
                                  (STATUS_NOW + 12.9, STATUS_NOW + 12)):
        clocked = create_server(_AcquisitionDouble(ACQUISITION),
                                wall_clock=lambda value=reading: value)
        payload = await _payload(clocked, "watchdog_status", {})
        assert payload == _expected(query.watchdog_status, {}, now=expected_now)

    fixed_message = (
        "create_server() requires wall_clock to return a finite non-negative "
        "safe integer epoch")
    for bad in (True, "1759200100", -1, MAX_SAFE_INT + 1,
                float("nan"), float("inf"), float("-inf")):
        clocked = create_server(_AcquisitionDouble(ACQUISITION),
                                wall_clock=lambda value=bad: value)
        with pytest.raises(UnexpectedToolError) as crashed:
            await clocked.call_tool("watchdog_status", {})
        assert isinstance(crashed.value.__cause__, ValueError)
        assert str(crashed.value.__cause__) == fixed_message


async def _real_source(workspace):
    fixture = workspace / "catalog.json"
    shutil.copyfile(FIXTURE, fixture)
    owner = AsyncCatalogSource(
        "file", str(fixture), cache_dir=str(workspace / "cache"),
        wall_clock=lambda: SOURCE_NOW, monotonic_clock=lambda: 5000.0)
    server = create_server(owner, wall_clock=lambda: STATUS_NOW)

    snapshot = catalog.load_snapshot(fixture.read_text(encoding="utf-8"))
    lookup = await _payload(server, "get_model", {"model_id": "alpha-1:free"})
    assert {key: value for key, value in lookup.items() if key != "source"} == (
        query.get_model(snapshot, {"model_id": "alpha-1:free"}))
    assert lookup["source"]["mode"] == "file"
    assert lookup["source"]["cached"] is False
    assert lookup["source"]["not_modified"] is False
    assert lookup["source"]["fetched_at"] == SOURCE_NOW
    assert lookup["source"]["fetch_age_s"] == 0
    assert lookup["source"]["warnings"] == []

    status = await _payload(server, "watchdog_status", {})
    assert {key: value for key, value in status.items() if key != "source"} == (
        query.watchdog_status(snapshot, {}, now=STATUS_NOW))
    assert status["source"]["cached"] is True, (
        "the second call in the monotonic window is a memory view")


def test_direct_sdk_calls_and_raw_argument_boundary(tmp_path):
    asyncio.run(_direct_calls())
    asyncio.run(_real_source(tmp_path))
