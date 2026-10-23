"""Typed SDK registration, direct-call contract and the CLI launch (units 6b/6c).

Four unparametrized groups:

1. ``test_typed_registration_discovery_contract`` builds the factory with a
   source double that fails if acquired and a clock that fails if read. It
   discovers exactly the five reviewed names/descriptions/order, normalizes the
   SDK's ACTUALLY advertised input schemas against the packaged ``tools.json``
   oracle, compares every advertised TOOL and PARAMETER description with the
   reviewed string by exact equality (separately from that structural
   normalizer, which ignores prose at every depth), proves the structural
   comparator keeps a numeric-bound and an ``additionalProperties`` difference,
   proves the description comparator keeps an altered and a missing parameter
   description, and runs a fresh interpreter to show that the package/core
   import graph still excludes the transport, SDK, producer, provider, config,
   state and site modules.

2. ``test_direct_sdk_calls_and_raw_argument_boundary`` calls every tool through
   the real ``MCPServer.call_tool`` and compares the payload with the pure core
   plus exactly one acquisition's ``with_source`` attachment. A table of
   original-value rows covers bools, numeric strings, encoded-list/null
   strings, integral floats, fractions, bounds, array members, required
   ``model_id`` and extras on every tool. Schema-invalid input must be rejected
   before the source seam; application errors may reach it. A real
   ``AsyncCatalogSource`` over an owned temporary fixture proves end-to-end
   source metadata without any network.

3. ``test_cli_source_selection_and_awaited_launch`` pins the source-selection
   CLI: help as a deliberate stdout response before any source work, ``argv=None``
   versus explicit argv, the exact published default feed, an explicit literal
   URL, literal (including relative) file paths, optional ``--cache-dir``
   pass-through, mutual exclusion in both flag orders, missing values,
   unknown/abbreviated options and installed ``--root`` rejection, plus the one
   fixed non-reflective configuration diagnostic. Construction is WRAPPED, not
   faked: the real owner validates each selection, its identity reaches the
   factory, and a real coroutine probe is awaited exactly once by the real
   ``asyncio.run`` on the main thread, with no acquisition or cache write.

4. ``test_fresh_process_cli_help_launch_and_legacy_root_preservation`` runs real
   ``python -m watchdog_catalog`` subprocesses from a neutral owned cwd with an
   empty HOME: ``--help`` under blocked SDK/legacy imports on this interpreter
   and on the Python 3.11 floor, parse/configuration failures (exit 2, empty
   stdout, no cache or cwd effects), and a real fixture-file launch whose
   immediate stdin EOF exits 0 with empty stdout and no cache directory. It then
   loads the UNCHANGED root ``mcp_server.py`` in a fresh interpreter with an
   explicitly synthetic no-auth ``config_loader`` and proves ``--root`` is still
   accepted and defaulted, forwarded once to the local ``build_server``, awaited
   once, and never replaced by the published transport or remote feed.

Honest scope: these are direct SDK calls, one CLI launch/EOF smoke and
checkout/module evidence -- not stdio client negotiation, not a clean-install
console launch, not a wheel transport check and not Git-source publication.
``structured_content`` is asserted equal to the text JSON whenever this SDK
supplies both (a bare ``dict`` return annotation does not publish an output
schema here).
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
#: semantic relaxations: generated titles, prose descriptions, absent-vs-empty
#: ``required``, ``anyOf`` nullable versus a ``type`` array, and list order for
#: unordered schema arrays. An absent ``additionalProperties`` stays absent.
#: Stripping ``description`` here keeps the STRUCTURAL comparison prose-blind;
#: it is never the prose evidence. Reviewed tool-level text is compared
#: directly from the resource and every parameter description is compared by
#: ``_assert_parameter_descriptions``.
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


def _assert_parameter_descriptions(actual_properties, reviewed_properties,
                                   label):
    """Compare reviewed per-parameter prose exactly; return the names checked.

    This is the prose path, deliberately independent of ``_canonical``: every
    reviewed property must carry a non-empty description and the SDK-advertised
    value must equal it character for character. A property-set mismatch or a
    missing reviewed description fails here rather than silently decorating the
    wrong callable.
    """
    assert set(actual_properties) == set(reviewed_properties), (
        f"{label}: parameter names drifted from tools.json\n"
        f"SDK:      {sorted(actual_properties)}\n"
        f"CONTRACT: {sorted(reviewed_properties)}")
    for name in sorted(reviewed_properties):
        expected = reviewed_properties[name].get("description")
        assert isinstance(expected, str) and expected, (
            f"{label}.{name}: the reviewed contract carries no parameter "
            "description")
        actual = actual_properties[name].get("description")
        assert actual == expected, (
            f"{label}.{name}: the SDK-advertised parameter description differs "
            "from tools.json\n"
            f"SDK:      {actual!r}\n"
            f"CONTRACT: {expected!r}")
    return set(reviewed_properties)


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
    described = set()
    for entry in contract:
        tool = by_name[entry["name"]]
        assert tool.description == entry["description"], (
            f"{tool.name}: tool description must come from tools.json")
        _assert_schema_matches(tool.input_schema, entry["inputSchema"],
                               tool.name)
        assert set(tool.model_dump()["input_schema"]["properties"]) == set(
            entry["inputSchema"]["properties"])
        described |= {
            (tool.name, name) for name in _assert_parameter_descriptions(
                tool.input_schema["properties"],
                entry["inputSchema"]["properties"], tool.name)}
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

    reviewed_parameters_set = {(entry["name"], name) for entry in contract
                               for name in entry["inputSchema"]["properties"]}
    assert described == reviewed_parameters_set, (
        "every parameter in the reviewed contract must be covered by the "
        "exact-match description comparison")

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

    # The description comparator is its own path: the untouched per-parameter
    # prose passes, while an altered and a missing description each fail that
    # same exact-match check. Prose never rides on the structural normalizer,
    # so a prose-only difference passes ``_assert_schema_matches`` yet is
    # rejected here.
    actual_properties = by_name["search_models"].input_schema["properties"]
    reviewed_properties = reviewed["properties"]
    _assert_parameter_descriptions(actual_properties, reviewed_properties,
                                   "description positive control")
    altered = copy.deepcopy(actual_properties)
    altered["limit"]["description"] = "Deliberately altered parameter prose."
    with pytest.raises(AssertionError) as altered_error:
        _assert_parameter_descriptions(altered, reviewed_properties,
                                       "altered parameter description")
    assert "parameter description differs" in str(altered_error.value)
    missing = copy.deepcopy(actual_properties)
    missing["provider"].pop("description")
    with pytest.raises(AssertionError) as missing_error:
        _assert_parameter_descriptions(missing, reviewed_properties,
                                       "missing parameter description")
    assert "parameter description differs" in str(missing_error.value)
    prose_only = copy.deepcopy(candidate)
    prose_only["properties"]["limit"]["description"] = (
        "Deliberately altered parameter prose.")
    _assert_schema_matches(prose_only, reviewed,
                           "prose outside the structural normalizer")

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


# --------------------------------------------------------------------------
# group 3: CLI parsing, one literal source selection and the awaited launch
# --------------------------------------------------------------------------
class _LaunchProbeServer:
    """The factory seam's server: a REAL coroutine that proves it was awaited.

    It registers no tools and speaks no protocol -- this is launch WIRING
    evidence, never a transport claim (real client negotiation is a later unit).
    """

    def __init__(self):
        self.body_runs = 0
        self.loop_runs = 0
        self.thread_names = []

    async def run_stdio_async(self):
        import threading

        asyncio.get_running_loop()
        self.loop_runs += 1
        self.thread_names.append(threading.current_thread().name)
        self.body_runs += 1


class _CliSeams:
    """Observe the construction/factory/runner seams around the REAL owner.

    ``_construct_source`` is WRAPPED, never faked: the real
    :class:`AsyncCatalogSource` still validates the selection, so the recorded
    arguments are exactly what the CLI selected and the recorded owner is a
    genuinely configured owner. ``_create_server`` yields the coroutine probe and
    ``_run_coroutine`` delegates to the real :func:`asyncio.run`.
    """

    def __init__(self, cli):
        self.cli = cli
        self.real_construct = cli._construct_source
        self.real_runner = cli._run_coroutine
        self.reset()

    def reset(self):
        self.probe = _LaunchProbeServer()
        self.constructed = []
        self.factory_owners = []
        self.runner_calls = 0

    def install(self, monkeypatch):
        import inspect

        seams = self

        def construct(kind, location, cache_dir):
            owner = seams.real_construct(kind, location, cache_dir)
            seams.constructed.append((kind, location, cache_dir, owner))
            return owner

        def create_server(owner):
            seams.factory_owners.append(owner)
            return seams.probe

        def run(coroutine):
            assert inspect.iscoroutine(coroutine), (
                "the CLI must give asyncio.run a coroutine object, not "
                f"{type(coroutine).__name__}")
            seams.runner_calls += 1
            return seams.real_runner(coroutine)

        monkeypatch.setattr(self.cli, "_construct_source", construct)
        monkeypatch.setattr(self.cli, "_create_server", create_server)
        monkeypatch.setattr(self.cli, "_run_coroutine", run)


SELECTION_HELP_NEEDLES = (
    "stdio",
    "stdin",
    "stderr",
    "--catalog-url",
    "--catalog-file",
    "--cache-dir",
    "mutually exclusive",
)


def test_cli_source_selection_and_awaited_launch(tmp_path, monkeypatch):
    import contextlib
    import io
    import os
    import sys as system

    import watchdog_catalog.__main__ as cli
    from watchdog_catalog import source as source_module

    published = source_module.DEFAULT_CATALOG_URL
    assert published.startswith("https://raw.githubusercontent.com/"), (
        f"the published default feed is not the reviewed HTTPS feed: {published!r}")

    # Importing the entrypoint defines the command and runs nothing.
    assert cli.PROGRAM == "free-inference-watchdog-mcp"
    assert callable(cli.main) and cli.main.__module__ == "watchdog_catalog.__main__"

    seams = _CliSeams(cli)
    seams.install(monkeypatch)

    # -- help is a deliberate stdout response, before any source work --------
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        with pytest.raises(SystemExit) as helped:
            cli.main(["--help"])
    assert helped.value.code == 0
    help_text = out.getvalue()
    assert err.getvalue() == ""
    assert help_text.startswith("usage: free-inference-watchdog-mcp"), help_text
    for needle in SELECTION_HELP_NEEDLES:
        assert needle in help_text, (needle, help_text)
    assert published in help_text, "help names the default remote feed"
    assert "--root" not in help_text, "the installed command exposes no --root"
    assert seams.constructed == [] and seams.factory_owners == []
    assert seams.runner_calls == 0

    fixture = str(FIXTURE)
    literal_url = "http://127.0.0.1:9/synthetic-catalogue.json"
    relative_file = "owned/relative-catalogue.json"
    cache = tmp_path / "selected-cache"

    # -- argv=None reads sys.argv exactly like the console entrypoint --------
    monkeypatch.setattr(system, "argv", [cli.PROGRAM])
    seams.reset()
    assert cli.main() == 0
    assert [row[:3] for row in seams.constructed] == [("url", published, None)], (
        "with no selection the published DEFAULT_CATALOG_URL is chosen verbatim")

    monkeypatch.setattr(system, "argv",
                        [cli.PROGRAM, "--catalog-file", relative_file])
    seams.reset()
    assert cli.main() == 0
    assert [row[:3] for row in seams.constructed] == [
        ("file", relative_file, None)], (
        "an explicit file stays file mode with the literal path unchanged")

    # -- every accepted selection, wired once and awaited once --------------
    launch_rows = [
        ([], ("url", published, None)),
        (["--catalog-url", literal_url], ("url", literal_url, None)),
        (["--catalog-file", relative_file], ("file", relative_file, None)),
        (["--catalog-url", literal_url, "--cache-dir", str(cache)],
         ("url", literal_url, str(cache))),
        (["--catalog-file", fixture, "--cache-dir", str(cache)],
         ("file", fixture, str(cache))),
        # flag order is not semantic
        (["--cache-dir", str(cache), "--catalog-file", relative_file],
         ("file", relative_file, str(cache))),
    ]
    for argv, expected in launch_rows:
        seams.reset()
        assert cli.main(argv) == 0, argv
        assert len(seams.constructed) == 1, (argv, seams.constructed)
        kind, location, cache_dir, owner = seams.constructed[0]
        assert (kind, location, cache_dir) == expected, (argv, expected)
        assert seams.factory_owners == [owner], (
            f"{argv}: the selected owner must be the object handed to the factory")
        assert type(owner) is source_module.AsyncCatalogSource
        # The accepted owner freezes the FILE's lexical absolute path and keeps
        # an explicit URL literal; it is the constructor, not this CLI, that
        # decides that.
        if kind == "file":
            assert owner._source.location == os.path.abspath(location), (
                f"{argv}: the real owner freezes the lexical absolute path")
        else:
            assert owner._source.location == location, (
                f"{argv}: an explicit URL stays literal")
        # Startup is lazy: the accepted owner has read nothing, retained
        # nothing and written nothing.
        assert owner._source._bootstrapped is False, argv
        assert owner._source._entry is None, argv
        assert seams.probe.body_runs == 1, (
            f"{argv}: the stdio coroutine body must really run")
        assert seams.probe.loop_runs == 1, argv
        assert seams.runner_calls == 1, argv
        assert seams.probe.thread_names == ["MainThread"], (
            "the coroutine is awaited in this thread, not a wrapper or process")
    assert not cache.exists(), "no cache directory is created at startup"

    # -- argument-shape failures exit 2 through argparse, before any wiring --
    rejected_rows = [
        (["--catalog-url", literal_url, "--catalog-file", relative_file],
         "not allowed with argument"),
        (["--catalog-file", relative_file, "--catalog-url", literal_url],
         "not allowed with argument"),
        (["--catalog-url"], "expected one argument"),
        (["--catalog-file"], "expected one argument"),
        (["--catalog"], "unrecognized arguments"),
        (["--catalog-u", literal_url], "unrecognized arguments"),
        (["--cach", str(cache)], "unrecognized arguments"),
        (["--root", str(REPO)], "unrecognized arguments"),
        (["--catalog-file", fixture, "loose-positional"], "unrecognized arguments"),
    ]
    for argv, needle in rejected_rows:
        seams.reset()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with pytest.raises(SystemExit) as rejected:
                cli.main(argv)
        assert rejected.value.code == 2, argv
        assert out.getvalue() == "", f"{argv}: parse failures never write stdout"
        assert needle in err.getvalue(), (argv, err.getvalue())
        assert seams.constructed == [] and seams.factory_owners == []
        assert seams.runner_calls == 0
    assert not cache.exists()

    # -- the accepted constructor, not the CLI, owns configuration truth ------
    invalid_rows = [
        (["--catalog-file", ""], ""),
        (["--catalog-url", ""], ""),
        (["--catalog-url", "not-a-url"], "not-a-url"),
        (["--catalog-url", "ftp://example.com/catalogue.json"],
         "ftp://example.com/catalogue.json"),
        (["--catalog-url", "https://"], "https://"),
        (["--catalog-file", fixture, "--cache-dir", ""], ""),
    ]
    fixed = "free-inference-watchdog-mcp: error: Invalid catalogue source configuration.\n"
    for argv, supplied in invalid_rows:
        seams.reset()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with pytest.raises(SystemExit) as invalid:
                cli.main(argv)
        assert invalid.value.code == 2, argv
        assert out.getvalue() == "", argv
        stderr = err.getvalue()
        assert stderr.endswith(fixed), (argv, stderr)
        assert "Traceback" not in stderr, argv
        if supplied:
            assert supplied not in stderr, (
                f"{argv}: the fixed diagnostic must not echo the selection")
        assert seams.constructed == [] and seams.factory_owners == []
        assert seams.runner_calls == 0
    assert not cache.exists()


# --------------------------------------------------------------------------
# group 4: fresh-process help/launch isolation and legacy root preservation
# --------------------------------------------------------------------------
#: Owned probe hook: refuses the named modules from a scratch ``sitecustomize``
#: so a real ``python -m watchdog_catalog`` run proves the import graph, rather
#: than a claim about a spelling of an import statement.
_ISOLATION_HOOK = '''\
"""Owned isolation probe: refuse the named modules at interpreter start."""
import importlib.abc
import sys

BLOCKED = frozenset(%r)


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in BLOCKED or fullname.split(".")[0] in BLOCKED:
            raise ImportError("blocked import during CLI isolation probe: "
                              + fullname)
        return None


sys.meta_path.insert(0, _Blocker())
'''

#: The SDK-dependent and legacy graphs the INSTALLED command must not need for
#: help, parse failures or serving.
BLOCKED_FOR_CLI = (
    "mcp", "mcp_types", "pydantic", "watchdog_catalog.server", "config_loader",
    "providers", "inference_watchdog", "catalog_export", "envfile", "notify",
    "diffing", "detection", "alive", "build_site", "pending_removals",
    "probe_select", "probe_state", "probe_zero_credit", "state",
)

#: Only the legacy producer/config/state graph: the SDK stays available so the
#: real serving path can be exercised.
BLOCKED_LEGACY_ONLY = tuple(
    name for name in BLOCKED_FOR_CLI
    if name not in ("mcp", "mcp_types", "pydantic", "watchdog_catalog.server"))

#: The floor interpreter for the stdlib-only help path, overridable for the
#: legitimate "my 3.11 lives elsewhere" case.
FLOOR_PYTHON = "/home/kimbo/.local/bin/python3.11"

_LEGACY_LAUNCH_PROBE = '''\
"""Owned probe: load the UNCHANGED root mcp_server.py with synthetic config.

The real ``config_loader`` resolves provider auth at module scope, so this
probe installs an explicitly synthetic no-auth module first: no provider file,
``.env``, HOME token or production state is ever read.
"""
import contextlib
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

repo = Path(sys.argv[1])
explicit = None if sys.argv[2] == "-" else sys.argv[2]

synthetic = types.ModuleType("config_loader")
synthetic.PROVIDERS = {"synthetic-a": False, "synthetic-b": False}
synthetic.GATEWAY_WIRING = {"synthetic-a": {}, "synthetic-b": {}}
sys.modules["config_loader"] = synthetic

sys.path.insert(0, str(repo))
spec = importlib.util.spec_from_file_location("legacy_mcp_server",
                                             repo / "mcp_server.py")
module = importlib.util.module_from_spec(spec)
sys.modules["legacy_mcp_server"] = module
spec.loader.exec_module(module)

observed = {"roots": [], "body_runs": 0, "loop_runs": 0}


class ProbeServer:
    async def run_stdio_async(self):
        import asyncio
        asyncio.get_running_loop()
        observed["loop_runs"] += 1
        observed["body_runs"] += 1


probe = ProbeServer()


def build_server(root=None):
    observed["roots"].append(None if root is None else str(root))
    return probe


module.build_server = build_server

help_buffer = io.StringIO()
with contextlib.redirect_stdout(help_buffer):
    try:
        module.main(["--help"])
    except SystemExit as exit_code:
        observed["help_code"] = exit_code.code
observed["help_text"] = help_buffer.getvalue()

observed["code"] = module.main([] if explicit is None else ["--root", explicit])
observed["repo"] = str(module.REPO)
observed["cwd"] = str(Path.cwd())
observed["published"] = sorted(
    name for name in ("watchdog_catalog.__main__", "watchdog_catalog.server",
                      "watchdog_catalog.source")
    if name in sys.modules)
print(json.dumps(observed))
'''


def test_fresh_process_cli_help_launch_and_legacy_root_preservation(tmp_path):
    import json as json_module
    import os

    scratch = tmp_path / "probe"
    neutral = scratch / "cwd"
    home = scratch / "home"
    hooks_sdk = scratch / "hooks-sdk"
    hooks_legacy = scratch / "hooks-legacy"
    scratch.mkdir()
    for directory in (neutral, home, hooks_sdk, hooks_legacy):
        directory.mkdir()
    (hooks_sdk / "sitecustomize.py").write_text(
        _ISOLATION_HOOK % (tuple(sorted(BLOCKED_FOR_CLI)),), encoding="utf-8")
    (hooks_legacy / "sitecustomize.py").write_text(
        _ISOLATION_HOOK % (tuple(sorted(BLOCKED_LEGACY_ONLY)),), encoding="utf-8")

    def environment(hooks=None):
        path = [str(REPO)] if hooks is None else [str(hooks), str(REPO)]
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home),
            "PYTHONPATH": os.pathsep.join(path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }

    def module_cli(python, argv, *, hooks=None):
        return subprocess.run(
            [str(python), "-B", "-m", "watchdog_catalog", *argv],
            cwd=str(neutral), env=environment(hooks), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=180)

    def assert_no_effects():
        assert sorted(neutral.iterdir()) == [], "the CLI wrote into cwd"
        assert sorted(home.iterdir()) == [], "the CLI created cache/HOME state"

    # -- help with the SDK and the whole legacy graph blocked ---------------
    blocked_help = module_cli(sys.executable, ["--help"], hooks=hooks_sdk)
    assert blocked_help.returncode == 0, blocked_help.stderr[-4000:]
    assert "usage: free-inference-watchdog-mcp" in blocked_help.stdout
    for needle in SELECTION_HELP_NEEDLES:
        assert needle in blocked_help.stdout, needle
    assert blocked_help.stderr == ""
    assert_no_effects()

    # -- the same stdlib-only help on the floor interpreter ----------------
    floor = Path(os.environ.get("FREE_INFERENCE_WATCHDOG_FLOOR_PYTHON",
                                FLOOR_PYTHON))
    assert floor.exists(), (
        f"the Python 3.11 floor interpreter is missing: {floor}. Install it "
        "or set FREE_INFERENCE_WATCHDOG_FLOOR_PYTHON to a 3.11 interpreter.")

    # The floor interpreter has no mcp/pytest, which is exactly the point: help
    # must not need them.
    floor_help = module_cli(floor, ["--help"], hooks=hooks_sdk)
    assert floor_help.returncode == 0, floor_help.stderr[-4000:]
    assert "usage: free-inference-watchdog-mcp" in floor_help.stdout
    for needle in SELECTION_HELP_NEEDLES:
        assert needle in floor_help.stdout, needle
    assert floor_help.stderr == ""
    assert_no_effects()

    # -- subprocess parse/configuration failures: exit 2, empty stdout -----
    subprocess_failures = [
        (["--catalog-url", "http://127.0.0.1:9/catalogue.json",
          "--catalog-file", str(FIXTURE)], "not allowed with argument"),
        (["--catalog-file", str(FIXTURE), "--catalog-url",
          "http://127.0.0.1:9/catalogue.json"], "not allowed with argument"),
        (["--root", str(REPO)], "unrecognized arguments"),
        (["--catalog-file"], "expected one argument"),
        (["--catalog-url", "not-a-url"],
         "Invalid catalogue source configuration."),
    ]
    for argv, needle in subprocess_failures:
        failed = module_cli(sys.executable, argv, hooks=hooks_sdk)
        assert failed.returncode == 2, (argv, failed.stderr[-2000:])
        assert failed.stdout == "", f"{argv}: parse failures must not use stdout"
        assert needle in failed.stderr, (argv, failed.stderr[-2000:])
        assert "blocked import" not in failed.stderr, argv
    assert_no_effects()

    # -- the REAL package CLI: fixture file + immediate stdin EOF ----------
    # Legacy modules stay blocked: the installed command must serve the
    # projected public catalogue without the producer/config/state graph. This
    # is a launch/EOF smoke, NOT an initialize/tools/call protocol claim.
    cache = scratch / "launch-cache"
    launch = module_cli(
        sys.executable,
        ["--catalog-file", str(FIXTURE), "--cache-dir", str(cache)],
        hooks=hooks_legacy)
    assert launch.returncode == 0, launch.stderr[-4000:]
    assert launch.stdout == "", (
        "protocol stdout carried incidental messages: " + launch.stdout[:2000])
    assert "Traceback" not in launch.stderr, launch.stderr[-4000:]
    assert "was never awaited" not in launch.stderr, launch.stderr[-4000:]
    assert not cache.exists(), "an EOF-only run acquires and caches nothing"
    assert_no_effects()

    # -- the unchanged root script keeps its LOCAL compatibility route ------
    script = scratch / "legacy_launch_probe.py"
    script.write_text(_LEGACY_LAUNCH_PROBE, encoding="utf-8")
    explicit_root = scratch / "owned-legacy-root"
    explicit_root.mkdir()

    def legacy(*args):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(script), str(REPO), *args],
            cwd=str(neutral),
            env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                 "HOME": str(home)},
            capture_output=True, text=True, timeout=180)

    for argument, expected_root in (("-", str(REPO)),
                                    (str(explicit_root), str(explicit_root))):
        result = legacy(argument)
        assert result.returncode == 0, result.stderr[-4000:]
        assert result.stderr == "", result.stderr[-4000:]
        observed = json_module.loads(result.stdout)
        assert observed["code"] == 0
        assert observed["roots"] == [expected_root], (
            "the root script forwards exactly one root to its LOCAL build_server")
        assert observed["body_runs"] == 1 and observed["loop_runs"] == 1, (
            "the root script still awaits its own stdio coroutine exactly once")
        assert observed["repo"] == str(REPO)
        assert observed["published"] == [], (
            "the local route must not import the published transport: "
            f"{observed['published']}")
        assert observed["help_code"] == 0
        assert "--root" in observed["help_text"]
        assert "--catalog-url" not in observed["help_text"] and \
            "--cache-dir" not in observed["help_text"], (
            "the root script's CLI surface stays --root only")
    assert_no_effects()
