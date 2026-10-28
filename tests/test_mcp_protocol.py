"""Typed SDK registration, direct-call contract, CLI launch and real stdio.

Six unparametrized groups:

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
   state and site modules. ``test_repository_carries_one_python_mcp_server``
   widens that to the whole checkout: the retired root server is gone and exactly
   one shipped module imports the SDK.

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

4. ``test_fresh_process_cli_help_launch_isolation`` runs real
   ``python -m watchdog_catalog`` subprocesses from a neutral owned cwd with an
   empty HOME: ``--help`` under blocked SDK/legacy imports on this interpreter
   and on the Python 3.11 floor, parse/configuration failures (exit 2, empty
   stdout, no cache or cwd effects, including the retired ``--root`` flag being
   unrecognized), and a real fixture-file launch whose immediate stdin EOF exits
   0 with empty stdout and no cache directory. The floor itself is DISCOVERED
   portably and probed for CPython identity/version (an explicit
   ``FREE_INFERENCE_WATCHDOG_FLOOR_PYTHON`` is authoritative, then an ordinary
   ``python3.11``, then the current interpreter when it really is 3.11); a
   controlled double proves that a path which exists is not enough.

5. ``test_file_source_stdio_discovery_calls_and_invalid_requests`` runs the real
   installed ``ClientSession`` against an owned ``python -B -m watchdog_catalog``
   child over its actual stdin/stdout. An owned probe hook blocks the legacy
   producer/config/state graph before the package imports and reports the
   child's real import origins, with a positive control showing the blocker
   raises and the package resolves to this tree in that same environment.
   Initialize/initialized and tools/list are observable on the wire; all five
   tools, their reviewed descriptions and their SDK-advertised schemas are
   discovered, every tool is called and each decoded payload (text JSON, plus
   ``structured_content`` when present) is compared with the accepted pure core
   plus source provenance. Invalid originals -- extras, absent/null/blank
   lookups, wrong types, numeric strings/bools/fractions, page bounds, unsafe
   integers and encoded-list strings -- are transmitted verbatim, rejected over
   the wire, and followed by a still-usable session. The exact raw stdout bytes
   of the whole session are retained and audited as protocol-only JSON-RPC, and
   stdin is closed so the child exits naturally with status 0. Every dynamic age
   is checked against the CALLER's own wall-clock bracket around that request,
   and the status wrapper's ``last_tick_age_s`` is an independently validated
   capture: it is never assumed equal to the source's own earlier observation
   epoch. A fixed-time oracle regression inside this group pins that decision --
   equal, adjacent and descheduled captures are accepted, while an
   out-of-bracket capture, bool/fractional/negative ages, a wrong source age, a
   reset retrieval age and unchanged-clock payload drift are rejected.

6. ``test_loopback_url_stdio_last_good_and_unavailable_source`` serves a
   synthetic validated catalogue from a bounded 127.0.0.1 listener with a
   request counter, then acquires it over real stdio: no feed GET at startup or
   discovery, exactly one on the first query, and none for later calls in the
   same owner window; the source-scoped cache entry on disk is inspected. The
   listener is then closed and a NEW process with the same literal URL and cache
   directory must fail refresh but serve the validated last-good snapshot with
   preserved ``snapshot_id``/``tick_epoch``/``fetched_at`` and explicit
   ``refresh_failed``/``cache_fallback`` warnings, leaving the persisted bytes
   untouched. The preserved ``fetched_at`` gives the retrieval age a real lower
   bound on every restart, checked against that request's own bracket and kept
   independent of the status wrapper's age. Absent and deliberately corrupt
   caches for that same unavailable URL must return the exact semantic
   ``SourceError().to_payload()``, never an empty success or a leaked exception;
   a legitimate empty query on a valid snapshot stays distinct from that
   failure. Every restart negotiates over the real transport, closes stdin and
   exits 0.

Honest scope: groups 1-4 are direct SDK calls plus CLI launch/checkout evidence;
groups 5-6 add a real SDK client over a real child subprocess and its exact raw
wire transcript. They are checkout/module evidence only: no wheel build, no
clean-install console launch, no published Git-source ``uvx`` run and no PyPI
publication is claimed here. ``structured_content`` is asserted equal to the text
JSON whenever this SDK supplies both (a bare ``dict`` return annotation does not
publish an output schema here).
"""

import asyncio
import copy
import importlib.resources
import json
import os
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


#: The ONE shipped Python module allowed to import the MCP SDK/Pydantic. Tests
#: may import the SDK to speak the protocol; no other shipped module may.
SDK_TRANSPORT_MODULE = "watchdog_catalog/server.py"

#: Roots that hold no shipped application Python: the suite itself, local-only
#: caches and the untracked agent/plan notes.
NON_SHIPPED_TREES = frozenset({".git", ".hermes", ".pytest_cache", "__pycache__",
                               "site", "state", "tests"})

#: Names the retirement replaced. A reintroduced root server, forwarding shim or
#: deprecated variant under one of these names is a second launch path.
RETIRED_ROOT_SERVERS = ("mcp_server.py",)


def _sdk_transport_audit(root):
    """Every shipped SDK importer and retired-root-server path under ``root``.

    The scan is syntax, not a claim about a spelling: every ``import``/``from``
    in the file is walked, including function-scoped guarded imports, so a
    lazily imported second transport is found too.
    """
    import ast

    root = Path(root)
    importers = set()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if NON_SHIPPED_TREES.intersection(relative.parts[:-1]):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] in ("mcp", "pydantic")
                   for name in modules):
                importers.add(relative.as_posix())
                break
    retired = sorted(name for name in RETIRED_ROOT_SERVERS
                     if (root / name).exists())
    return sorted(importers), retired


def test_repository_carries_one_python_mcp_server(tmp_path):
    """The legacy local server is REPLACED, not kept beside the canonical one.

    Retirement means one shipped registration path: the root ``mcp_server.py``,
    its forwarding shims and any deprecated second server are gone, so exactly
    one shipped module imports the MCP SDK. The predicate is sensitivity-proved
    on a planted tree, so a scan that quietly stopped finding SDK imports could
    not pass by finding nothing.
    """
    importers, retired = _sdk_transport_audit(REPO)
    assert retired == [], (
        f"a retired root server is back under {retired}; the canonical server "
        f"is {SDK_TRANSPORT_MODULE} only")
    assert importers == [SDK_TRANSPORT_MODULE], (
        "shipped modules importing mcp/pydantic must be exactly the one "
        f"canonical transport, found {importers}")

    planted = tmp_path / "root"
    (planted / "watchdog_catalog").mkdir(parents=True)
    (planted / "tests").mkdir()
    (planted / "watchdog_catalog" / "server.py").write_text(
        "from mcp.server.mcpserver import MCPServer\n", encoding="utf-8")
    (planted / "legacy_shim.py").write_text(
        "def main():\n    import mcp\n    return mcp\n", encoding="utf-8")
    (planted / "mcp_server.py").write_text(
        "from pydantic import BaseModel\n", encoding="utf-8")
    (planted / "tests" / "test_protocol.py").write_text(
        "import mcp\n", encoding="utf-8")
    planted_importers, planted_retired = _sdk_transport_audit(planted)
    assert planted_importers == ["legacy_shim.py", "mcp_server.py",
                                "watchdog_catalog/server.py"], (
        "the audit must find a second transport and a guarded import, count a "
        f"reintroduced retired server as an importer too, and ignore the suite, "
        f"found {planted_importers}")
    assert planted_retired == ["mcp_server.py"], (
        "the audit must detect a reintroduced retired root server, found "
        f"{planted_retired}")


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
# group 4: fresh-process help and launch isolation
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

#: The stdlib-only help path must run on the exact CPython 3.11 floor stated by
#: the package metadata. Discovery is portable: an explicit override wins, then
#: an ordinary ``python3.11`` executable, then a CURRENT interpreter that really
#: is CPython 3.11 (its executable may be named ``python``). A path that exists
#: is not enough -- the interpreter's own identity and version are probed.
FLOOR_ENVIRONMENT = "FREE_INFERENCE_WATCHDOG_FLOOR_PYTHON"
FLOOR_MINOR = (3, 11)

#: A controlled discovery double. It reports the REAL CPython implementation
#: identity (``sys.implementation.name`` is lowercase ``cpython``) with the
#: version 3.14, so ONLY the probe's version gate can reject it: a wrong
#: identity name would prove nothing about the version rule. It is never used
#: as real floor evidence.
_NON_FLOOR_DOUBLE = """#!/bin/sh
# controlled discovery double: never a real interpreter
printf '{"implementation": "cpython", "version": [3, 14]}\\n'
"""


def _floor_python():
    """Resolve an interpreter that really IS the CPython 3.11 floor.

    An explicit ``FREE_INFERENCE_WATCHDOG_FLOOR_PYTHON`` is AUTHORITATIVE: a
    wrong interpreter named there is an actionable failure, never a silent
    fallback to some other installation. Otherwise an ordinary ``python3.11``
    executable is discovered on PATH and finally the CURRENT interpreter is
    accepted when it really is CPython 3.11. Existence is not evidence: every
    candidate is probed for implementation and version.
    """
    override = os.environ.get(FLOOR_ENVIRONMENT)
    candidates = ([override] if override
                  else [shutil.which("python3.11"), sys.executable])
    tried = []
    for candidate in candidates:
        if not candidate:
            continue
        if not Path(candidate).is_file():
            tried.append(f"{candidate}: no such executable file")
            continue
        try:
            probe = subprocess.run(
                [candidate, "-I", "-c",
                 "import json, sys; print(json.dumps({'implementation': "
                 "sys.implementation.name, 'version': "
                 "list(sys.version_info[:2])}))"],
                capture_output=True, text=True, timeout=60,
                env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"})
        except OSError as error:
            tried.append(f"{candidate}: cannot run ({error})")
            continue
        if probe.returncode != 0:
            tried.append(f"{candidate}: cannot run ({probe.stderr.strip()[:200]})")
            continue
        try:
            facts = json.loads(probe.stdout)
            identity = (facts["implementation"], tuple(facts["version"]))
        except (ValueError, KeyError, TypeError):
            tried.append(f"{candidate}: unreadable interpreter probe "
                         f"{probe.stdout.strip()[:120]!r}")
            continue
        if identity != ("cpython", FLOOR_MINOR):
            tried.append(f"{candidate}: {identity[0]} "
                         f"{'.'.join(str(part) for part in identity[1])} is not "
                         f"exactly CPython "
                         f"{'.'.join(str(part) for part in FLOOR_MINOR)}")
            continue
        return str(candidate)
    raise AssertionError(
        "the Python 3.11 floor interpreter is missing or wrong; tried: "
        + "; ".join(tried)
        + f". Install CPython 3.11 once outside pytest or set "
        f"{FLOOR_ENVIRONMENT}=/path/to/python3.11.")


def test_fresh_process_cli_help_launch_isolation(
        tmp_path, monkeypatch):
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

    # -- the floor interpreter must really BE CPython 3.11 -----------------
    # Discovery is a prerequisite, not an account assumption. A controlled
    # double (never used as floor evidence) reports the REAL ``cpython``
    # identity with the version 3.14, so only the probe's VERSION gate can
    # reject it -- whether it is named explicitly or found as an ordinary
    # ``python3.11`` executable. A named path that does not exist is an
    # actionable failure, never a silent fallback or a skip.
    double_directory = scratch / "floor-double"
    double_directory.mkdir()
    double = double_directory / "python3.11"
    double.write_text(_NON_FLOOR_DOUBLE, encoding="utf-8")
    double.chmod(0o755)
    absent = double_directory / "python3.11-absent"
    # The caller's OWN configuration is the integration condition: a supplied
    # FREE_INFERENCE_WATCHDOG_FLOOR_PYTHON override is authoritative even when
    # no floor is discoverable on PATH. Every discovery NEGATIVE therefore
    # lives in one contained monkeypatch context, so the exact caller
    # environment (presence, absence and values) is back BEFORE the genuine
    # floor work and no test default can replace it.
    caller_state = {name: os.environ.get(name)
                    for name in (FLOOR_ENVIRONMENT, "PATH")}
    caller_floor = caller_state[FLOOR_ENVIRONMENT]

    with monkeypatch.context() as probe_patch:
        probe_patch.setenv(FLOOR_ENVIRONMENT, str(double))
        with pytest.raises(AssertionError) as wrong_version:
            _floor_python()
        assert str(double) in str(wrong_version.value), wrong_version.value
        # The double IS CPython by identity: pin the VERSION rejection itself,
        # never an identity mismatch that could pass for another reason.
        assert ("cpython 3.14 is not exactly CPython 3.11"
                in str(wrong_version.value)), wrong_version.value

        probe_patch.setenv("PATH", str(double_directory))
        probe_patch.delenv(FLOOR_ENVIRONMENT, raising=False)
        if (sys.implementation.name == "cpython"
                and sys.version_info[:2] == FLOOR_MINOR):
            # A CURRENT interpreter that really is CPython 3.11 is legitimate
            # even when its executable is not named ``python3.11``.
            assert Path(_floor_python()).resolve() == Path(sys.executable).resolve()
        else:
            with pytest.raises(AssertionError) as discovered_wrong:
                _floor_python()
            assert str(double) in str(discovered_wrong.value), discovered_wrong.value
            assert ("cpython 3.14 is not exactly CPython 3.11"
                    in str(discovered_wrong.value)), discovered_wrong.value

        probe_patch.setenv(FLOOR_ENVIRONMENT, str(absent))
        with pytest.raises(AssertionError) as missing_floor:
            _floor_python()
        assert FLOOR_ENVIRONMENT in str(missing_floor.value), missing_floor.value

    # Nothing above leaked: the caller's state is restored EXACTLY -- presence,
    # absence and values -- before any real interpreter, help, build, install
    # or session work.
    for name, value in caller_state.items():
        assert os.environ.get(name) == value, (
            f"{name} was not restored to the caller's value {value!r}")
    floor = Path(_floor_python())
    if caller_floor:
        # Genuine integration consumes a caller's valid explicit override even
        # when no floor is discoverable on PATH.
        assert floor == Path(caller_floor), (
            f"the caller's floor override was not used: {floor}")
    assert floor.is_file(), f"the discovered floor is not a file: {floor}"
    floor_probe = json_module.loads(subprocess.run(
        [str(floor), "-I", "-c",
         "import json, sys; print(json.dumps({'implementation': "
         "sys.implementation.name, 'version': list(sys.version_info[:2])}))"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}).stdout)
    assert (floor_probe["implementation"], tuple(floor_probe["version"])) == (
        "cpython", FLOOR_MINOR), (
        f"the floor interpreter is {floor_probe}, not CPython 3.11")

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



# --------------------------------------------------------------------------
# groups 5-6: a real SDK client over the child's real stdin/stdout
#
# Nothing below launches, patches or replaces an SDK process factory, and no
# result is synthesized: the harness owns the child's pipes, records every byte
# it reads and writes, and feeds the unchanged bytes to the installed SDK
# ``ClientSession`` through a transparent anyio memory-stream pair. The SDK
# still performs the handshake, every ``tools/list`` and ``tools/call``
# request, response correlation and dispatch. Invalid requests travel over that
# same wire: the recorded outbound frames are the evidence, not a local verdict.
# --------------------------------------------------------------------------
import os
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.session import HANDSHAKE_PROTOCOL_VERSIONS
from mcp.shared.message import SessionMessage
from watchdog_catalog.source import CatalogSource, DiskCache

#: The installed SDK's own accepted handshake tuple. Membership is the support
#: claim, so no universal protocol-version literal is invented here.
SUPPORTED_HANDSHAKE_VERSIONS = tuple(HANDSHAKE_PROTOCOL_VERSIONS)

STDIO_SESSION_TIMEOUT_S = 240
STDIO_CALL_TIMEOUT_S = 60
STDIO_EXIT_TIMEOUT_S = 20
STDIO_DRAIN_TIMEOUT_S = 15

#: The reviewed server identity advertised in ``initialize``.
SERVER_IDENTITY = "free-inference-watchdog-mcp"

#: Owned probe hook, installed through ``PYTHONPATH`` so it runs at interpreter
#: start BEFORE ``python -m watchdog_catalog`` imports anything. It refuses the
#: legacy producer/config/state graph and, at exit, reports this child's real
#: import origins -- so neither claim rests on reading the launch command.
_STDIO_PROBE_HOOK = '''\
"""Owned probe hook: refuse the legacy graph and report this child's imports."""
import atexit
import importlib.abc
import json
import os
import sys

BLOCKED = frozenset(%r)
REPORT = os.environ.get("WD_PROBE_REPORT")


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in BLOCKED or fullname.split(".")[0] in BLOCKED:
            raise ImportError("blocked import during stdio integration probe: "
                              + fullname)
        return None


sys.meta_path.insert(0, _Blocker())


def _origin(name):
    return getattr(sys.modules.get(name), "__file__", None)


def _report():
    if not REPORT:
        return
    loaded = {name.split(".")[0] for name in sys.modules}
    payload = {
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "watchdog_catalog": _origin("watchdog_catalog"),
        "watchdog_catalog.server": _origin("watchdog_catalog.server"),
        "legacy_loaded": sorted(loaded & BLOCKED),
        "sys_path": list(sys.path),
    }
    with open(REPORT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


atexit.register(_report)
'''


class _StdioChild:
    """One owned ``python -B -m watchdog_catalog`` child and its transcript.

    The child is launched exactly as an operator would launch the module, from a
    neutral cwd, with an explicit credential-free environment, a scratch HOME, an
    explicit import path and an owned cache directory. stdout/stderr/stdin are
    owned pipes: every byte is recorded verbatim and forwarded unchanged, and the
    shutdown protocol -- close stdin, await the NATURAL exit, drain both pipes --
    is owned here rather than delegated to SDK teardown.
    """

    def __init__(self, label, argv, environment, cwd, artifacts):
        self.label = label
        self.argv = [str(part) for part in argv]
        self.environment = dict(environment)
        self.cwd = Path(cwd)
        self.artifacts = Path(artifacts)
        self.proc = None
        self.pid = None
        self.raw_stdout = bytearray()
        self.raw_stdin = bytearray()
        self.stderr = bytearray()
        self.stdout_lines = []
        self.stdin_lines = []
        self.protocol_violations = []
        self.codec_errors = []
        self.stdin_closed = False
        self.natural_exit = False
        self.forced_cleanup = False
        self.tool_calls = 0
        self._stdout_eof = anyio.Event()
        self._stdin_eof = anyio.Event()
        self._stderr_eof = anyio.Event()

    @asynccontextmanager
    async def session(self):
        """Run the real SDK client against this child, then prove graceful EOF."""
        send_out, recv_out = anyio.create_memory_object_stream(0)
        send_in, recv_in = anyio.create_memory_object_stream(0)
        self.proc = await anyio.open_process(
            self.argv, env=self.environment, cwd=str(self.cwd),
            stdin=-1, stdout=-1, stderr=-1)
        self.pid = self.proc.pid
        try:
            with anyio.fail_after(STDIO_SESSION_TIMEOUT_S):
                async with anyio.create_task_group() as group:
                    group.start_soon(self._pump_stdout, send_out)
                    group.start_soon(self._pump_stdin, recv_in)
                    group.start_soon(self._pump_stderr)
                    try:
                        async with ClientSession(recv_out, send_in) as session:
                            try:
                                yield session
                            finally:
                                await self._close_stdin_and_reap()
                    finally:
                        # The session is gone: release both remaining ends so
                        # the reader/writer pumps can reach EOF instead of
                        # being cancelled mid-drain.
                        await _quietly_close(send_in)
                        await _quietly_close(recv_out)
                        with anyio.move_on_after(STDIO_DRAIN_TIMEOUT_S) as drain:
                            await self._stdout_eof.wait()
                            await self._stderr_eof.wait()
                            await self._stdin_eof.wait()
                        if drain.cancelled_caught:
                            self.forced_cleanup = True
                            group.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await self._ensure_reaped()
            self._dump_artifacts()

    async def _close_stdin_and_reap(self):
        """Close the real child stdin, then wait out its NATURAL exit, bounded."""
        if self.proc is None or self.stdin_closed:
            return
        await self.proc.stdin.aclose()
        self.stdin_closed = True
        try:
            with anyio.fail_after(STDIO_EXIT_TIMEOUT_S):
                await self.proc.wait()
            self.natural_exit = True
        except TimeoutError:
            # Forced cleanup is recorded, never reported as success.
            self.forced_cleanup = True
            self.proc.kill()
            with anyio.fail_after(STDIO_EXIT_TIMEOUT_S):
                await self.proc.wait()

    async def _ensure_reaped(self):
        """Never leave an owned child behind, even when the session failed."""
        if self.proc is None or self.proc.returncode is not None:
            return
        self.forced_cleanup = True
        try:
            await self.proc.stdin.aclose()
        except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
            pass
        with anyio.move_on_after(STDIO_EXIT_TIMEOUT_S):
            await self.proc.wait()
        if self.proc.returncode is None:
            self.proc.kill()
            with anyio.move_on_after(STDIO_EXIT_TIMEOUT_S):
                await self.proc.wait()

    async def _pump_stdout(self, send):
        """Record raw stdout bytes verbatim, then hand the SDK its own codec."""
        buffer = b""
        try:
            async with send:
                while True:
                    try:
                        chunk = await self.proc.stdout.receive()
                    except anyio.EndOfStream:
                        break
                    self.raw_stdout += chunk
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        await self._accept_line(line, send)
                if buffer:
                    self.protocol_violations.append(
                        "stdout ended mid-frame: %r" % (bytes(buffer[:200]),))
                    await self._accept_line(buffer, send)
        finally:
            self._stdout_eof.set()

    async def _accept_line(self, raw, send):
        self.stdout_lines.append(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            self.protocol_violations.append("stdout is not UTF-8: %r" % (error,))
            await send.send(error)
            return
        try:
            message = mcp_types.jsonrpc_message_adapter.validate_json(
                text, by_name=False)
        except ValueError as error:
            # The SDK's own transport reports a parse failure as a stream value;
            # the violation is recorded so the session audit fails, never lost.
            self.codec_errors.append(error)
            self.protocol_violations.append(
                "stdout line is not a JSON-RPC frame: %r" % (raw[:400],))
            await send.send(error)
            return
        await send.send(SessionMessage(message))

    async def _pump_stdin(self, receive):
        """Record each exact outbound frame, then write those same bytes."""
        try:
            async with receive:
                async for session_message in receive:
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_unset=True)
                    data = (payload + "\n").encode("utf-8")
                    self.raw_stdin += data
                    self.stdin_lines.append(data[:-1])
                    await self.proc.stdin.send(data)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass
        finally:
            self._stdin_eof.set()

    async def _pump_stderr(self):
        try:
            while True:
                try:
                    chunk = await self.proc.stderr.receive()
                except anyio.EndOfStream:
                    break
                self.stderr += chunk
        finally:
            self._stderr_eof.set()

    def _dump_artifacts(self):
        """Keep this session's raw transport evidence, whatever the verdict."""
        try:
            self.artifacts.mkdir(parents=True, exist_ok=True)
            stem = self.artifacts / self.label
            Path(str(stem) + ".stdout.raw").write_bytes(bytes(self.raw_stdout))
            Path(str(stem) + ".stdin.raw").write_bytes(bytes(self.raw_stdin))
            Path(str(stem) + ".stderr.raw").write_bytes(bytes(self.stderr))
            summary = {
                "argv": self.argv,
                "cwd": str(self.cwd),
                "pid": self.pid,
                "returncode": None if self.proc is None else self.proc.returncode,
                "stdin_closed": self.stdin_closed,
                "natural_exit": self.natural_exit,
                "forced_cleanup": self.forced_cleanup,
                "stdout_frames": [line.decode("utf-8", "replace")
                                  for line in self.stdout_lines],
                "outbound_frames": [line.decode("utf-8", "replace")
                                    for line in self.stdin_lines],
                "protocol_violations": list(self.protocol_violations),
            }
            Path(str(stem) + ".session.json").write_text(
                json.dumps(summary, indent=1), encoding="utf-8")
        except OSError:  # pragma: no cover - evidence capture never masks a verdict
            pass


async def _quietly_close(stream):
    try:
        await stream.aclose()
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        pass


def _stdio_workspace(tmp_path, label):
    """One owned workspace per group: neutral cwd, scratch HOME, probe hook."""
    root = tmp_path / label
    neutral = root / "cwd"
    home = root / "home"
    hooks = root / "hooks"
    artifacts = root / "artifacts"
    for directory in (neutral, home, hooks, artifacts):
        directory.mkdir(parents=True)
    (hooks / "sitecustomize.py").write_text(
        _STDIO_PROBE_HOOK % (tuple(sorted(BLOCKED_LEGACY_ONLY)),),
        encoding="utf-8")
    return root, neutral, home, hooks, artifacts


def _child_environment(home, hooks, report):
    """The minimal, credential-free child environment (never the shell's)."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHONPATH": os.pathsep.join([str(hooks), str(REPO)]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "WD_PROBE_REPORT": str(report),
    }


def _probe_process(argv, environment, cwd):
    return subprocess.run(
        [str(part) for part in argv], cwd=str(cwd), env=environment,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)


async def _wire_call(child, session, tool, arguments):
    """One ``tools/call`` over the real wire, with its exact outbound frame."""
    before = len(child.stdin_lines)
    with anyio.fail_after(STDIO_CALL_TIMEOUT_S):
        result = await session.call_tool(tool, arguments)
    assert len(child.stdin_lines) == before + 1, (
        f"{tool}({arguments!r}) did not produce exactly one outbound request")
    frame = json.loads(child.stdin_lines[before].decode("utf-8"))
    assert frame.get("method") == "tools/call", frame
    assert frame["params"]["name"] == tool, frame
    sent = frame["params"].get("arguments", {})
    assert sent == arguments, (
        f"{tool}: the wire request did not preserve the original arguments\n"
        f"SENT: {sent!r}\nWANT: {arguments!r}")
    child.tool_calls += 1
    return result


async def _call_with_bracket(child, session, tool, arguments):
    """One wire call plus the CALLER's own wall-clock bracket around it.

    The child reads its clocks inside this window, so every age reported by
    the request must fall inside it; an age outside the bracket is wrong data,
    not a scheduling accident.
    """
    low = int(time.time())
    result = await _wire_call(child, session, tool, arguments)
    high = int(time.time())
    assert low <= high, (low, high)
    return result, low, high


def _decoded_payload(result, label):
    """The decoded tool payload, rejecting transport-level error results."""
    assert result.is_error is not True, f"{label}: the tool reported an error"
    assert result.content, f"{label}: a successful result carries no content"
    assert len(result.content) == 1, f"{label}: unexpected content blocks"
    block = result.content[0]
    assert getattr(block, "type", None) == "text", (label, block)
    payload = json.loads(block.text)
    assert isinstance(payload, dict), (label, type(payload))
    if result.structured_content is not None:
        assert result.structured_content == payload, (
            f"{label}: structured and text content disagree")
    return payload


def _assert_rejection(result, label):
    """Invalid input must be a rejection, never a successful query."""
    assert result.is_error is True, f"{label}: invalid arguments were accepted"
    assert result.content, f"{label}: a rejection carries no diagnostic content"
    text = result.content[0].text
    assert isinstance(text, str) and text.strip(), label
    try:
        payload = json.loads(text)
    except ValueError:
        return
    assert not (isinstance(payload, dict) and payload.get("ok") is True), (
        f"{label}: a rejected request produced a successful query payload")


def _expected_core(core, arguments, *, status_epoch=None):
    """The accepted pure-core payload for one request.

    A non-status core is clock-free. ``watchdog_status`` needs the status
    wrapper's OWN capture, validated independently by
    :func:`_assert_status_capture`; the source's observation epoch describes a
    DIFFERENT capture and is never substituted for it.
    """
    if core is query.watchdog_status:
        assert status_epoch is not None, (
            "the status core needs its independently validated capture")
        return query.watchdog_status(SNAPSHOT, arguments, now=status_epoch)
    assert status_epoch is None, "a non-status core takes no clock"
    return core(SNAPSHOT, arguments)


def _assert_provenance(source, *, mode, url, cached, fetched_at, low, high,
                       warnings):
    """Compare one acquisition's OWN provenance against its capture bracket.

    ``low``/``high`` bracket the acquisition call that produced ``source``, so
    the retrieval age and the observation age are proven to be real elapsed
    values of THAT capture without masking, patching or rewriting any field.
    Both bounds are meaningful where the fixed old fixture tick and a preserved
    ``fetched_at`` make them known.

    The returned epoch is the SOURCE's own capture
    (``tick_epoch + observation_age_s``). The status wrapper captures wall time
    SEPARATELY, possibly in another second, so this value is provenance
    evidence only -- never the status clock.
    """
    assert isinstance(source, dict), source
    assert set(source) == set(PROVENANCE), sorted(source)
    assert source["mode"] == mode, ("mode", source["mode"], mode)
    assert source["url"] == url, ("url", source["url"], url)
    assert source["cached"] is cached, ("cached", source["cached"], cached)
    assert source["not_modified"] is False, source["not_modified"]
    assert source["fetched_at"] == fetched_at, (
        "fetched_at", source["fetched_at"], fetched_at)
    fetch_age = source["fetch_age_s"]
    assert isinstance(fetch_age, int) and not isinstance(fetch_age, bool), (
        "fetch_age_s", fetch_age)
    assert max(0, low - fetched_at) <= fetch_age <= max(0, high - fetched_at), (
        "fetch_age_s", fetch_age, fetched_at, low, high)
    tick_epoch = SNAPSHOT["tick_epoch"]
    observation_age = source["observation_age_s"]
    assert isinstance(observation_age, int) and not isinstance(
        observation_age, bool), ("observation_age_s", observation_age)
    assert max(0, low - tick_epoch) <= observation_age <= max(
        0, high - tick_epoch), (
            "observation_age_s", observation_age, low, high, tick_epoch)
    assert source["observation_fresh"] is False, (
        "the fixture tick is deliberately old")
    assert source["warnings"] == warnings, source["warnings"]
    return tick_epoch + observation_age


def _assert_status_capture(payload, *, low, high, label):
    """Validate the status wrapper's INDEPENDENT clock capture.

    ``low``/``high`` are the CALLER's own wall-clock bracket around the request
    that produced ``payload``. The epoch is trusted only AFTER the age that
    implies it is a native, non-bool, non-negative integer inside that bracket:
    an epoch merely derived from an unchecked field is never taken as the
    clock. The age/cadence boundary is re-derived here too, so a flipped fresh
    flag cannot ride along with an unchanged age.
    """
    tick_epoch = payload["tick_epoch"]
    assert tick_epoch == SNAPSHOT["tick_epoch"], (label, tick_epoch)
    cadence_s = payload["cadence_s"]
    assert cadence_s == SNAPSHOT["cadence_s"], (label, cadence_s)
    age = payload["last_tick_age_s"]
    assert isinstance(age, int) and not isinstance(age, bool), (
        f"{label}: last_tick_age_s must be a non-negative native integer, "
        f"got {age!r}")
    assert age >= 0, f"{label}: last_tick_age_s must not be negative, got {age!r}"
    assert low - tick_epoch <= age <= high - tick_epoch, (
        f"{label}: last_tick_age_s {age} is outside the caller's bracket "
        f"[{low - tick_epoch}, {high - tick_epoch}]")
    epoch = tick_epoch + age
    assert low <= epoch <= high, (label, epoch, low, high)
    assert payload["tick_fresh"] is (age <= cadence_s), (
        f"{label}: tick_fresh does not match the validated age/cadence "
        f"boundary ({age} vs {cadence_s})")
    return epoch


def _assert_core_payload(payload, core, arguments, *, low, high, source_epoch,
                         label):
    """Compare one decoded payload with the accepted core, clock-aware.

    Every non-status tool stays an exact, clock-free full-payload comparison.
    ``watchdog_status`` is compared at the STATUS wrapper's independently
    validated capture; ``source_epoch`` is the same request's earlier source
    capture and is deliberately NOT used as the status clock.
    """
    if core is query.watchdog_status:
        status_epoch = _assert_status_capture(
            payload, low=low, high=high, label=label)
        assert payload == _expected_core(
            core, arguments, status_epoch=status_epoch), (
                f"{label}: the wire payload differs from the pure core at the "
                "independently validated status epoch")
        return
    assert payload == _expected_core(core, arguments), (
        f"{label}: the wire payload differs from the pure core")


def _fixed_clock_status(root, label, *, source_now, status_now):
    """A REAL file acquisition and the REAL SDK server on fixed clocks.

    Only the two documented programming seams are used: the core's
    ``CatalogSource.refresh(now=...)`` and the public
    ``create_server(..., wall_clock=...)``. No production clock is patched, no
    package code is replaced and no WIRE result is replaced -- this block pins
    the ORACLE that decides what a real wire result must equal.

    ``source_now`` is the acquisition's own capture and ``status_now`` is the
    status wrapper's INDEPENDENT capture; they are deliberately allowed to
    differ by more than the same second.
    """
    owner = CatalogSource(
        "file", FIXTURE, cache_dir=str(root / (label.replace(" ", "-") + "-cache")))
    acquisition = owner.refresh(now=source_now)
    assert acquisition["ok"] is True, acquisition
    assert acquisition["source"]["fetched_at"] == source_now
    server = create_server(_AcquisitionDouble(acquisition),
                           wall_clock=lambda: status_now)
    return asyncio.run(_payload(server, "watchdog_status", {}))


def _outbound_frames(child):
    frames = []
    for line in child.stdin_lines:
        frame = json.loads(line.decode("utf-8"))
        assert isinstance(frame, dict) and frame.get("jsonrpc") == "2.0", line[:400]
        frames.append(frame)
    assert bytes(child.raw_stdin) == b"".join(
        line + b"\n" for line in child.stdin_lines)
    return frames


def _audit_clean_stdio(child):
    """Audit the retained raw transcript: protocol-only, natural exit, no noise."""
    assert child.forced_cleanup is False, (
        f"{child.label}: the child needed forced cleanup, so graceful EOF was "
        "not demonstrated")
    assert child.stdin_closed is True, f"{child.label}: stdin was never closed"
    assert child.natural_exit is True, f"{child.label}: no natural exit observed"
    assert child.proc is not None
    assert child.proc.returncode == 0, (child.label, child.proc.returncode)
    assert child.codec_errors == [], (child.label, child.codec_errors)
    assert child.protocol_violations == [], (child.label,
                                             child.protocol_violations)
    raw = bytes(child.raw_stdout)
    text = raw.decode("utf-8")
    assert text, f"{child.label}: the session produced no protocol stdout"
    assert text.endswith("\n"), "stdout ended without a complete frame"
    lines = text.split("\n")[:-1]
    assert len(lines) == len(child.stdout_lines), (
        child.label, len(lines), len(child.stdout_lines))
    frames = []
    for line in lines:
        assert line, f"{child.label}: an empty stdout line is not a frame"
        frame = json.loads(line)
        assert isinstance(frame, dict), line[:300]
        assert frame.get("jsonrpc") == "2.0", line[:300]
        assert (("result" in frame) or ("error" in frame)
                or isinstance(frame.get("method"), str)), line[:300]
        assert ("result" in frame) != ("error" in frame), line[:300]
        frames.append(frame)
    stderr_text = bytes(child.stderr).decode("utf-8", "replace")
    for marker in ("Traceback (most recent call last)", "was never awaited",
                   "Task exception was never retrieved"):
        assert marker not in stderr_text, (
            f"{child.label}: {marker} in stderr\n{stderr_text[-2000:]}")
    outbound = _outbound_frames(child)
    assert sum(1 for frame in outbound
               if frame.get("method") == "tools/call") == child.tool_calls, (
        "outbound tools/call frames do not match the calls made")
    return frames, outbound


def _assert_child_origin(child, report_path):
    """Prove this exact child imported THIS tree and no legacy module."""
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert report["pid"] == child.pid, (
        "the import report did not come from this child process")
    assert report["cwd"] == str(child.cwd), report["cwd"]
    assert report["watchdog_catalog"] == str(
        REPO / "watchdog_catalog" / "__init__.py"), report["watchdog_catalog"]
    assert report["watchdog_catalog.server"] == str(
        REPO / "watchdog_catalog" / "server.py"), report["watchdog_catalog.server"]
    assert report["legacy_loaded"] == [], report["legacy_loaded"]
    assert str(REPO) in report["sys_path"], report["sys_path"]
    return report


#: Request rows exercised over the wire; every one is compared with the accepted
#: pure core over the same validated fixture snapshot.
WIRE_VALID_ROWS = (
    ("list_free_models", {}, query.list_free_models),
    ("list_free_models", {"provider": "synthetic-a"}, query.list_free_models),
    ("list_free_models", {"provider": "synthetic-b"}, query.list_free_models),
    ("list_endpoints", {}, query.list_endpoints),
    ("list_endpoints", {"provider": "synthetic-b"}, query.list_endpoints),
    ("get_model", {"model_id": "alpha-1:free"}, query.get_model),
    ("get_model", {"model_id": "alpha-1"}, query.get_model),
    ("get_model", {"model_id": "null"}, query.get_model),
    ("search_models", {}, query.search_models),
    # valid strings that LOOK like encoded JSON must stay literal strings
    ("search_models", {"query": "null"}, query.search_models),
    ("search_models", {"query": "[]"}, query.search_models),
    ("search_models", {"query": '["text"]'}, query.search_models),
    ("search_models", {"provider": "synthetic-a"}, query.search_models),
    ("search_models", {"provider": "null"}, query.search_models),
    ("search_models", {"input_modalities": []}, query.search_models),
    ("search_models", {"supported_parameters": ["temperature"]},
     query.search_models),
    ("search_models", {"query": "alpha", "provider": "synthetic-a",
                       "min_context_length": 8000,
                       "input_modalities": ["text"],
                       "supported_parameters": ["temperature"],
                       "limit": 1, "offset": 0}, query.search_models),
    ("search_models", {"limit": 1, "offset": 1}, query.search_models),
    ("search_models", {"limit": 200}, query.search_models),
    ("search_models", {"provider": "unknown-gateway"}, query.search_models),
    ("search_models", {"query": "definitely-absent"}, query.search_models),
    ("watchdog_status", {}, query.watchdog_status),
)

#: Invalid originals, all finite JSON values. Every row must be rejected over the
#: wire AND appear verbatim (type included) in the recorded outbound frame.
WIRE_INVALID_ROWS = (
    # unknown arguments, including on the zero-argument status tool
    ("watchdog_status", {"now": STATUS_NOW}),
    ("watchdog_status", {"x": 1}),
    ("list_free_models", {"bogus": 1}),
    ("list_endpoints", {"provider": None, "x": 0}),
    ("get_model", {"model_id": "alpha-1", "bogus": 1}),
    ("search_models", {"bogus": 1}),
    # required lookup absent, null, empty and ASCII blank
    ("get_model", {}),
    ("get_model", {"model_id": None}),
    ("get_model", {"model_id": ""}),
    ("get_model", {"model_id": " \t\n\r\v\f"}),
    # wrong scalar/container types for string and array parameters
    ("list_free_models", {"provider": 3}),
    ("list_free_models", {"provider": []}),
    ("list_endpoints", {"provider": 1.5}),
    ("list_endpoints", {"provider": {}}),
    ("get_model", {"model_id": 3}),
    ("get_model", {"model_id": ["alpha"]}),
    ("search_models", {"query": 1}),
    ("search_models", {"query": ["text"]}),
    ("search_models", {"provider": 3}),
    # numeric strings, bools and fractions are not integers
    ("search_models", {"limit": "5"}),
    ("search_models", {"limit": True}),
    ("search_models", {"limit": 5.5}),
    ("search_models", {"min_context_length": "5"}),
    ("search_models", {"min_context_length": True}),
    ("search_models", {"min_context_length": 1.5}),
    ("search_models", {"offset": "0"}),
    ("search_models", {"offset": False}),
    ("search_models", {"offset": 2.5}),
    # page size: zero, above the cap and explicit null
    ("search_models", {"limit": 0}),
    ("search_models", {"limit": 201}),
    ("search_models", {"limit": None}),
    # unsafe per-page integers
    ("search_models", {"min_context_length": -1}),
    ("search_models", {"min_context_length": MAX_SAFE_INT + 1}),
    ("search_models", {"offset": -1}),
    ("search_models", {"offset": MAX_SAFE_INT + 1}),
    # an encoded-list STRING is not a genuine list
    ("search_models", {"input_modalities": "[]"}),
    ("search_models", {"input_modalities": '["text"]'}),
    ("search_models", {"supported_parameters": "[]"}),
    ("search_models", {"input_modalities": [""]}),
    ("search_models", {"input_modalities": ["text", 3]}),
    ("search_models", {"supported_parameters": [""]}),
    ("search_models", {"supported_parameters": [1]}),
)


async def _file_stdio_session(child):
    """Negotiate, discover, call every tool and transmit invalid requests."""
    contract = _contract_entries()
    names = [entry["name"] for entry in contract]
    low = int(time.time()) - 1
    fetched_at = None
    async with child.session() as session:
        init = await session.initialize()
        assert init.server_info is not None, "the server sent no identity"
        assert init.server_info.name == SERVER_IDENTITY, init.server_info
        assert isinstance(init.server_info.version, str)
        assert init.protocol_version, "no protocol version was negotiated"
        assert init.protocol_version in SUPPORTED_HANDSHAKE_VERSIONS, (
            init.protocol_version)
        assert session.protocol_version == init.protocol_version
        assert init.capabilities is not None
        assert init.capabilities.tools is not None, (
            "the server advertised no tools capability")

        listed = await session.list_tools()
        assert [tool.name for tool in listed.tools] == names
        for entry, tool in zip(contract, listed.tools):
            assert tool.description == entry["description"], tool.name
            _assert_schema_matches(tool.input_schema, entry["inputSchema"],
                                   tool.name)
            _assert_parameter_descriptions(
                tool.input_schema["properties"],
                entry["inputSchema"]["properties"], tool.name)
            for closed in ("root", "clock", "wall_clock", "cache_dir", "source",
                           "snapshot", "path", "now"):
                assert closed not in tool.input_schema["properties"], (
                    f"{tool.name} exposes closure configuration {closed!r}")
            assert tool.input_schema["additionalProperties"] is False, tool.name

        # Independent spot checks on the WIRE schema, so requiredness, defaults,
        # nullability, item types, numeric bounds and unknown-argument policy are
        # not taken only from the tools.json oracle.
        by_name = {tool.name: tool.input_schema for tool in listed.tools}
        assert set(by_name) == set(names) and len(listed.tools) == 5
        search_schema = by_name["search_models"]
        assert search_schema.get("required", []) == []
        assert search_schema["properties"]["limit"]["default"] == 50
        assert search_schema["properties"]["limit"]["minimum"] == 1
        assert search_schema["properties"]["limit"]["maximum"] == 200
        assert search_schema["properties"]["offset"]["default"] == 0
        assert search_schema["properties"]["offset"]["minimum"] == 0
        assert search_schema["properties"]["offset"]["maximum"] == MAX_SAFE_INT
        assert _canonical(
            search_schema["properties"]["min_context_length"]) == {
                "type": ["integer", "null"], "minimum": 0,
                "maximum": MAX_SAFE_INT, "default": None}
        assert _canonical(search_schema["properties"]["input_modalities"]) == {
            "type": ["array", "null"], "default": None,
            "items": {"type": "string", "minLength": 1}}
        lookup_schema = by_name["get_model"]
        assert lookup_schema["required"] == ["model_id"]
        assert lookup_schema["properties"]["model_id"]["pattern"] == (
            MODEL_ID_PATTERN)
        assert by_name["watchdog_status"]["properties"] == {}

        for tool_name, arguments, core in WIRE_VALID_ROWS:
            label = f"{tool_name}({arguments!r})"
            result, call_low, call_high = await _call_with_bracket(
                child, session, tool_name, arguments)
            payload = _decoded_payload(result, label)
            source = payload.pop("source")
            if fetched_at is None:
                assert source["cached"] is False, "the first call is a fetch"
                fetched_at = source["fetched_at"]
                assert low <= fetched_at <= int(time.time()) + 1, (
                    fetched_at, low)
                assert fetched_at > SNAPSHOT["tick_epoch"], (
                    "the retrieval time is separate from the snapshot's "
                    "observation tick")
                cached = False
            else:
                cached = True
            source_epoch = _assert_provenance(
                source, mode="file", url=None, cached=cached,
                fetched_at=fetched_at, low=call_low, high=call_high,
                warnings=["tick_stale"])
            _assert_core_payload(
                payload, core, arguments, low=call_low, high=call_high,
                source_epoch=source_epoch, label=label)

        # Integral floats are preserved as ORIGINALS: they travel as JSON
        # numbers with a fraction and the core's literal filter echo keeps the
        # float, while the page integers normalise to ints.
        label = "search_models integral floats"
        result, float_low, float_high = await _call_with_bracket(
            child, session, "search_models",
            {"min_context_length": 1.0, "offset": 0.0, "limit": 1.0})
        payload = _decoded_payload(result, label)
        sent = json.loads(child.stdin_lines[-1].decode("utf-8"))["params"][
            "arguments"]
        assert type(sent["min_context_length"]) is float, sent
        assert type(sent["offset"]) is float, sent
        source = payload.pop("source")
        source_epoch = _assert_provenance(
            source, mode="file", url=None, cached=True, fetched_at=fetched_at,
            low=float_low, high=float_high, warnings=["tick_stale"])
        assert type(payload["filters"]["min_context_length"]) is float, (
            "the raw filter echo was coerced to an integer")
        assert payload["filters"]["min_context_length"] == 1.0
        _assert_core_payload(
            payload, query.search_models,
            {"min_context_length": 1.0, "offset": 0.0, "limit": 1.0},
            low=float_low, high=float_high, source_epoch=source_epoch,
            label=label)

        # Every invalid original goes OVER the wire and is rejected there; the
        # session then still serves a valid request.
        for tool_name, arguments in WIRE_INVALID_ROWS:
            label = f"{tool_name}({arguments!r})"
            _assert_rejection(
                await _wire_call(child, session, tool_name, arguments), label)
            followup = _decoded_payload(
                await _wire_call(child, session, "watchdog_status", {}),
                f"watchdog_status after {label}")
            assert followup["ok"] is True, label

        # An unknown tool name is a rejection, not a crash or a dropped session.
        _assert_rejection(
            await _wire_call(child, session, "watchdog_status_typo", {}),
            "unknown tool name")
        result, final_low, final_high = await _call_with_bracket(
            child, session, "get_model", {"model_id": "alpha-1:free"})
        final = _decoded_payload(result, "final valid call")
        source = final.pop("source")
        source_epoch = _assert_provenance(
            source, mode="file", url=None, cached=True, fetched_at=fetched_at,
            low=final_low, high=final_high, warnings=["tick_stale"])
        _assert_core_payload(
            final, query.get_model, {"model_id": "alpha-1:free"},
            low=final_low, high=final_high, source_epoch=source_epoch,
            label="final valid call")

    frames, outbound = _audit_clean_stdio(child)
    assert outbound[0]["method"] == "initialize", outbound[0]
    assert outbound[1]["method"] == "notifications/initialized", outbound[1]
    assert outbound[1].get("params") in (None, {}), outbound[1]
    assert [frame["method"] for frame in outbound].count("initialize") == 1
    assert [frame["method"] for frame in outbound].count("tools/list") == 1
    assert outbound[0]["params"]["protocolVersion"] in SUPPORTED_HANDSHAKE_VERSIONS
    request_ids = [frame["id"] for frame in outbound if "id" in frame]
    response_ids = [frame["id"] for frame in frames if "id" in frame]
    assert response_ids == request_ids, (response_ids, request_ids)
    assert len(frames) == len(request_ids), "unexpected unsolicited stdout frames"


def test_file_source_stdio_discovery_calls_and_invalid_requests(tmp_path):
    root, neutral, home, hooks, artifacts = _stdio_workspace(
        tmp_path, "file-stdio")
    fixture = root / "catalog.json"
    shutil.copyfile(FIXTURE, fixture)
    cache = root / "cache"
    report = root / "child-report.json"
    environment = _child_environment(home, hooks, report)

    # Positive control: the SAME environment and cwd both block the legacy graph
    # and resolve the package to this selected tree.
    blocked_report = root / "blocked-report.json"
    blocked = _probe_process(
        [sys.executable, "-B", "-c", "import config_loader"],
        dict(environment, WD_PROBE_REPORT=str(blocked_report)), neutral)
    assert blocked.returncode != 0, "the import blocker did not raise"
    assert "blocked import during stdio integration probe: config_loader" in (
        blocked.stderr), blocked.stderr[-2000:]
    assert blocked.stdout == "", "a blocked import wrote to stdout"
    blocked_observed = json.loads(blocked_report.read_text(encoding="utf-8"))
    assert blocked_observed["watchdog_catalog"] is None
    assert blocked_observed["legacy_loaded"] == []

    positive_report = root / "positive-report.json"
    resolved = _probe_process(
        [sys.executable, "-B", "-c",
         "import watchdog_catalog, watchdog_catalog.server as server; "
         "print(server.__file__)"],
        dict(environment, WD_PROBE_REPORT=str(positive_report)), neutral)
    assert resolved.returncode == 0, resolved.stderr[-2000:]
    assert resolved.stdout.strip() == str(
        REPO / "watchdog_catalog" / "server.py"), resolved.stdout

    child = _StdioChild(
        "file-source",
        [sys.executable, "-B", "-m", "watchdog_catalog",
         "--catalog-file", str(fixture), "--cache-dir", str(cache)],
        environment, neutral, artifacts)
    anyio.run(_file_stdio_session, child)

    _assert_child_origin(child, report)
    assert sorted(neutral.iterdir()) == [], "the child wrote into its cwd"
    assert sorted(home.iterdir()) == [], "the child created HOME state"
    assert cache.is_dir() and list(cache.glob("*.json")), (
        "the acquiring file-mode session persisted no cache entry")

    # ------------------------------------------------------------------
    # Clock-oracle regression: the source acquisition and the status wrapper
    # capture wall time INDEPENDENTLY. This block is fixed-time oracle
    # evidence through the two documented seams; it is clearly separate from
    # every real wire result above and never replaces one.
    # ------------------------------------------------------------------
    oracle_root = root / "clock-oracle"
    oracle_root.mkdir()
    source_now = 1760200000
    tick_epoch = SNAPSHOT["tick_epoch"]

    def seam_payload(label, status_now):
        payload = _fixed_clock_status(
            oracle_root, label, source_now=source_now, status_now=status_now)
        source = payload.pop("source")
        # The acquisition's OWN bracket: this fixed-time refresh captured
        # exactly ``source_now``, so its ages are exact, not merely bounded.
        source_epoch = _assert_provenance(
            source, mode="file", url=None, cached=False, fetched_at=source_now,
            low=source_now, high=source_now, warnings=["tick_stale"])
        return payload, source_epoch

    def accepted(label, status_now, *, low, high):
        payload, source_epoch = seam_payload(label, status_now)
        _assert_core_payload(
            payload, query.watchdog_status, {}, low=low, high=high,
            source_epoch=source_epoch, label=label)
        return payload

    # Positive rows: independently valid captures are accepted, including a
    # separation beyond one second (descheduling) and a genuine multi-second
    # caller window rather than one hard-coded instant.
    for label, status_now, low, high in (
            ("equal captures", source_now, source_now, source_now),
            ("adjacent seconds", source_now + 1, source_now + 1, source_now + 1),
            ("separated captures", source_now + 3, source_now + 3, source_now + 3),
            ("descheduled capture", source_now + 4, source_now + 2,
             source_now + 5)):
        payload = accepted(label, status_now, low=low, high=high)
        assert payload["ok"] is True, label
        assert payload["snapshot_id"] == SNAPSHOT["snapshot_id"], label
        assert payload["last_tick_age_s"] == status_now - tick_epoch, (
            label, payload["last_tick_age_s"])
        assert payload["tick_fresh"] is False, label

    def rejected(label, *, status_now, low, high, match, mutate=None):
        payload, source_epoch = seam_payload(label, status_now)
        if mutate is not None:
            mutate(payload)
        with pytest.raises(AssertionError, match=match):
            _assert_core_payload(
                payload, query.watchdog_status, {}, low=low, high=high,
                source_epoch=source_epoch, label=label)

    def mutate_age(value):
        return lambda payload: payload.update(last_tick_age_s=value)

    # A status capture outside the caller's bracket is wrong data, not a
    # scheduling accident; the type and sign checks are separate assertions.
    rejected("status outside bracket", status_now=source_now + 60,
             low=source_now, high=source_now + 5, match="last_tick_age_s")
    for label, value in (("bool age", True), ("fractional age", 1.5),
                         ("negative age", -1)):
        rejected(label, status_now=source_now, low=source_now,
                 high=source_now, match="last_tick_age_s",
                 mutate=mutate_age(value))
    # Payload drift at an UNCHANGED clock still fails the full-core parity.
    rejected("wrong count", status_now=source_now, low=source_now,
             high=source_now, match="differs from the pure core",
             mutate=lambda payload: payload["counts"].update(
                 gateways=payload["counts"]["gateways"] + 1))
    rejected("wrong stale flag", status_now=source_now, low=source_now,
             high=source_now, match="tick_fresh",
             mutate=lambda payload: payload.update(
                 tick_fresh=not payload["tick_fresh"]))
    rejected("warning drift", status_now=source_now, low=source_now,
             high=source_now, match="differs from the pure core",
             mutate=lambda payload: payload["warnings"].remove("tick_stale"))

    # Provenance has its own capture: a wrong observation age or a reset
    # retrieval age violates a known lower bound and is rejected there.
    _, provenance_epoch = seam_payload("provenance lower bounds", source_now)
    assert provenance_epoch == source_now, provenance_epoch
    for label, mutation, match in (
            ("wrong observation age",
             lambda source: source.update(observation_age_s=0),
             "observation_age_s"),
            ("future observation age",
             lambda source: source.update(observation_age_s=2000000),
             "observation_age_s")):
        source = _fixed_clock_status(
            oracle_root, label, source_now=source_now,
            status_now=source_now)["source"]
        mutation(source)
        with pytest.raises(AssertionError, match=match):
            _assert_provenance(
                source, mode="file", url=None, cached=False,
                fetched_at=source_now, low=source_now, high=source_now,
                warnings=["tick_stale"])
    reset = _fixed_clock_status(
        oracle_root, "reset retrieval age", source_now=source_now,
        status_now=source_now)["source"]
    reset.update(fetched_at=source_now - 600, fetch_age_s=0)
    with pytest.raises(AssertionError, match="fetch_age_s"):
        _assert_provenance(
            reset, mode="file", url=None, cached=False,
            fetched_at=source_now - 600, low=source_now, high=source_now,
            warnings=["tick_stale"])


# --------------------------------------------------------------------------
# group 6: loopback URL acquisition, offline last-good restart and source errors
# --------------------------------------------------------------------------
class _LoopbackFeed:
    """A bounded 127.0.0.1 feed that counts ONLY real feed traffic."""

    def __init__(self, body):
        self.body = bytes(body)
        self.requests = []
        self.log = []
        feed = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - http.server's documented API
                feed.requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(feed.body)))
                self.end_headers()
                self.wfile.write(feed.body)

            def log_message(self, format, *args):  # noqa: A002
                feed.log.append(format % args)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = "http://127.0.0.1:%d/catalog.json" % (
            self.server.server_address[1],)
        self._thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(10)


#: The five-tool semantic sweep against a URL source.
URL_VALID_ROWS = (
    ("get_model", {"model_id": "alpha-1:free"}, query.get_model),
    ("list_free_models", {}, query.list_free_models),
    ("list_endpoints", {"provider": "synthetic-b"}, query.list_endpoints),
    ("search_models", {"query": "alpha", "limit": 1, "offset": 0},
     query.search_models),
    ("watchdog_status", {}, query.watchdog_status),
)


async def _first_url_stdio_session(child, feed):
    """Discover without a fetch, then acquire once and serve inside the window."""
    low = int(time.time()) - 1
    async with child.session() as session:
        init = await session.initialize()
        assert init.server_info.name == SERVER_IDENTITY
        assert init.protocol_version in SUPPORTED_HANDSHAKE_VERSIONS
        assert init.capabilities.tools is not None
        listed = await session.list_tools()
        assert [tool.name for tool in listed.tools] == [
            entry["name"] for entry in _contract_entries()]
        assert feed.requests == [], (
            "startup or tool discovery fetched the feed: %r" % (feed.requests,))

        result, first_low, first_high = await _call_with_bracket(
            child, session, "get_model", {"model_id": "alpha-1:free"})
        first = _decoded_payload(result, "first acquisition")
        assert feed.requests == ["/catalog.json"], feed.requests
        source = first.pop("source")
        assert source["cached"] is False, "the first query must be a real fetch"
        fetched_at = source["fetched_at"]
        assert low <= fetched_at <= int(time.time()) + 1
        assert source["fetch_age_s"] == 0
        source_epoch = _assert_provenance(
            source, mode="url", url=feed.url, cached=False,
            fetched_at=fetched_at, low=first_low, high=first_high,
            warnings=["tick_stale"])
        _assert_core_payload(
            first, query.get_model, {"model_id": "alpha-1:free"},
            low=first_low, high=first_high, source_epoch=source_epoch,
            label="first acquisition")

        for tool_name, arguments, core in URL_VALID_ROWS:
            label = f"{tool_name}({arguments!r})"
            result, call_low, call_high = await _call_with_bracket(
                child, session, tool_name, arguments)
            payload = _decoded_payload(result, label)
            source = payload.pop("source")
            source_epoch = _assert_provenance(
                source, mode="url", url=feed.url, cached=True,
                fetched_at=fetched_at, low=call_low, high=call_high,
                warnings=["tick_stale"])
            _assert_core_payload(
                payload, core, arguments, low=call_low, high=call_high,
                source_epoch=source_epoch, label=label)
        assert feed.requests == ["/catalog.json"], (
            "the same owner/cache window issued another feed request: %r"
            % (feed.requests,))

        # Application-level and empty-result semantics stay distinct from a
        # source failure while the source IS available.
        unknown = _decoded_payload(await _wire_call(
            child, session, "search_models", {"provider": "unknown-gateway"}),
            "unknown provider")
        assert unknown["ok"] is False
        assert unknown["error"]["code"] == "unknown_provider"
        empty = _decoded_payload(await _wire_call(
            child, session, "search_models", {"query": "definitely-absent"}),
            "empty query")
        assert empty["ok"] is True and empty["endpoints"] == []
        assert feed.requests == ["/catalog.json"]

        entry_path = DiskCache(
            "url", feed.url, cache_dir=str(child.cache_dir)).path
        assert entry_path is not None, "the URL source resolved no cache path"
        entry_file = Path(entry_path)
        assert entry_file.exists(), "the first acquisition persisted no cache"
        entry = json.loads(entry_file.read_text(encoding="utf-8"))
        assert entry["source"] == {"kind": "url", "location": feed.url}
        assert entry["fetched_at"] == fetched_at
        assert entry["snapshot"]["snapshot_id"] == SNAPSHOT["snapshot_id"]
        assert entry["snapshot"]["tick_epoch"] == SNAPSHOT["tick_epoch"]

    _audit_clean_stdio(child)
    return fetched_at, entry_path


async def _offline_url_stdio_session(child, fetched_at, warnings):
    """A fresh process must fail its refresh yet serve the validated last-good."""
    observed_source = None
    async with child.session() as session:
        init = await session.initialize()
        assert init.server_info.name == SERVER_IDENTITY
        assert init.protocol_version in SUPPORTED_HANDSHAKE_VERSIONS
        await session.list_tools()
        for tool_name, arguments, core in URL_VALID_ROWS:
            label = f"offline {tool_name}({arguments!r})"
            result, call_low, call_high = await _call_with_bracket(
                child, session, tool_name, arguments)
            payload = _decoded_payload(result, label)
            source = payload.pop("source")
            observed_source = source
            source_epoch = _assert_provenance(
                source, mode="url", url=child.url, cached=True,
                fetched_at=fetched_at, low=call_low, high=call_high,
                warnings=warnings)
            assert payload["snapshot_id"] == SNAPSHOT["snapshot_id"], label
            assert payload["tick_epoch"] == SNAPSHOT["tick_epoch"], label
            _assert_core_payload(
                payload, core, arguments, low=call_low, high=call_high,
                source_epoch=source_epoch, label=label)

        # Retrieval and observation stay separate: a failed refresh preserves the
        # original fetch time, and the old fixture tick is never made fresh.
        assert observed_source["fetch_age_s"] >= 0
        assert observed_source["observation_age_s"] > 300, (
            "a fresh download must not imply a fresh observation tick")

        # A legitimate empty query on a VALID last-good snapshot is not a source
        # error.
        empty = _decoded_payload(await _wire_call(
            child, session, "search_models", {"query": "definitely-absent"}),
            "offline empty query")
        assert empty["ok"] is True and empty["endpoints"] == []
        assert "error" not in empty
        assert empty["source"]["warnings"] == warnings
    _audit_clean_stdio(child)


async def _unavailable_stdio_session(child):
    """Every tool returns the exact semantic source error, not an empty success."""
    expected = SourceError().to_payload()
    async with child.session() as session:
        init = await session.initialize()
        assert init.server_info.name == SERVER_IDENTITY
        await session.list_tools()
        for tool_name, arguments in (
                ("list_free_models", {}),
                ("get_model", {"model_id": "alpha-1:free"}),
                ("list_endpoints", {}),
                ("watchdog_status", {}),
                ("search_models", {})):
            label = f"unavailable {tool_name}"
            payload = _decoded_payload(
                await _wire_call(child, session, tool_name, arguments), label)
            assert payload == expected, (label, payload)
            assert "source" not in payload, label
    _audit_clean_stdio(child)


def _url_child(label, url, cache_dir, environment, neutral, artifacts):
    child = _StdioChild(
        label,
        [sys.executable, "-B", "-m", "watchdog_catalog",
         "--catalog-url", url, "--cache-dir", str(cache_dir)],
        environment, neutral, artifacts)
    child.url = url
    child.cache_dir = cache_dir
    return child


def test_loopback_url_stdio_last_good_and_unavailable_source(tmp_path):
    root, neutral, home, hooks, artifacts = _stdio_workspace(tmp_path, "url-stdio")
    cache = root / "cache"
    feed = _LoopbackFeed(FIXTURE.read_bytes()).start()
    try:
        assert feed.url.startswith("http://127.0.0.1:"), feed.url

        first_child = _url_child(
            "url-first", feed.url, cache,
            _child_environment(home, hooks, root / "first-report.json"),
            neutral, artifacts)
        fetched_at, entry_path = anyio.run(
            _first_url_stdio_session, first_child, feed)
        assert feed.requests == ["/catalog.json"], feed.requests
    finally:
        feed.stop()

    _assert_child_origin(first_child, root / "first-report.json")
    entry_file = Path(entry_path)
    cache_bytes = entry_file.read_bytes()
    entry_names = sorted(path.name for path in cache.iterdir())
    assert entry_names == [entry_file.name]

    offline_warnings = ["refresh_failed", "cache_fallback", "tick_stale"]
    offline_child = _url_child(
        "url-offline", feed.url, cache,
        _child_environment(home, hooks, root / "offline-report.json"),
        neutral, artifacts)
    anyio.run(_offline_url_stdio_session, offline_child, fetched_at,
              offline_warnings)
    _assert_child_origin(offline_child, root / "offline-report.json")
    assert entry_file.read_bytes() == cache_bytes, (
        "a failed refresh rewrote the persisted last-good cache")
    assert sorted(path.name for path in cache.iterdir()) == entry_names

    empty_cache = root / "absent-cache"
    absent_child = _url_child(
        "url-absent", feed.url, empty_cache,
        _child_environment(home, hooks, root / "absent-report.json"),
        neutral, artifacts)
    anyio.run(_unavailable_stdio_session, absent_child)
    _assert_child_origin(absent_child, root / "absent-report.json")
    assert not empty_cache.exists(), (
        "a failed refresh with no cache created cache state")

    corrupt_cache = root / "corrupt-cache"
    corrupt_cache.mkdir()
    target = DiskCache("url", feed.url, cache_dir=str(corrupt_cache)).path
    assert target is not None
    Path(target).write_bytes(b'{"cache_version": 1, "broken": ')
    corrupt_bytes = Path(target).read_bytes()
    corrupt_child = _url_child(
        "url-corrupt", feed.url, corrupt_cache,
        _child_environment(home, hooks, root / "corrupt-report.json"),
        neutral, artifacts)
    anyio.run(_unavailable_stdio_session, corrupt_child)
    _assert_child_origin(corrupt_child, root / "corrupt-report.json")
    assert Path(target).read_bytes() == corrupt_bytes, (
        "a corrupt cache entry was repaired, rewritten or deleted")
    assert sorted(path.name for path in corrupt_cache.iterdir()) == [
        Path(target).name]

    assert sorted(neutral.iterdir()) == [], "a child wrote into its cwd"
    assert sorted(home.iterdir()) == [], "a child created HOME state"
