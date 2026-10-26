#!/usr/bin/env python3
"""Installed-consumer smoke for the standalone MCP package (unit 9a4).

This is a standalone verification harness, not a pytest module and not an
application entry point. It is run by an isolated Python 3.11 interpreter with
``-I`` from a neutral working directory, and it proves the consumer path from
an ACTUAL installed wheel:

* every runtime import comes from the isolated environment's site-packages,
  never from the checkout that still holds this script;
* the packaged ``tools.json`` resource supplies the five reviewed tool names;
* the reviewed pure query payloads match the literal, independently authored
  goldens in ``tests/fixtures/catalog/cases.json``;
* a copied fixture at a path with spaces and non-ASCII characters is loaded,
  persisted by ``CatalogSource`` into a cache directory with the same awkward
  characters, and reloaded from disk by a NEW source object after the file is
  made unavailable (last-good fallback with the original ``fetched_at``);
* the installer-generated console executable speaks real MCP stdio to the real
  installed SDK client, discovers the five tools, answers ``get_model`` and
  ``search_models`` with the golden query cores, persists a cache, and then
  serves the same body with truthful cached-fallback provenance from a NEW
  process after the file is gone;
* an independent console process exits 0 with empty stdout on stdin EOF, and
  ``--help`` succeeds.

No external URL and no live provider is contacted. Only owned copies under the
``--work`` directory are created, renamed or removed; the immutable fixture
directory is read only. Any failed assertion exits nonzero with diagnostics.

CLI (all paths are used literally and may contain spaces or non-ASCII):

    python -I tests/consumer_smoke.py \\
        --fixtures <repo>/tests/fixtures/catalog \\
        --console <isolated-env>/bin/free-inference-watchdog-mcp \\
        --work <fresh-owned-empty-directory>

On Windows the console path is ``<isolated-env>\\Scripts\\
free-inference-watchdog-mcp.exe``; the harness selects ``bin`` versus
``Scripts`` from the live platform, never from a simulated flag.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

#: The installed console command and reviewed server identity.
PROGRAM = "free-inference-watchdog-mcp"

#: The five reviewed tools, in the packaged contract's registration order.
TOOL_NAMES = (
    "list_free_models",
    "get_model",
    "list_endpoints",
    "watchdog_status",
    "search_models",
)

#: Dependency pins the Windows job installs and this smoke verifies.
REVIEWED_DEPENDENCIES = (
    ("mcp", "2.3.0"),
    ("pydantic", "2.14.1"),
    ("anyio", "4.15.1"),
)

#: Selected, literal golden cases from the frozen ``cases.json`` document.
#: Both cases use ``lookup.json``; the expected payloads are compared whole,
#: never regenerated from the implementation.
GET_MODEL_CASE_ID = "get_model_exact_id_alpha_with_marker_endpoints"
SEARCH_MODELS_CASE_ID = "search_provider_b_shared_stale_scope"

#: Fixed, caller-supplied retrieval epochs (lookup.json tick_epoch 1759200000
#: with cadence 3600, so both observations stay fresh and ordered).
FETCHED_AT = 1759200100
RELOADED_AT = 1759200200

#: The frozen source error, written literally so success cannot be assumed.
SOURCE_UNAVAILABLE = {
    "ok": False,
    "error": {
        "code": "source_unavailable",
        "message": "Catalogue source unavailable.",
    },
}

CACHE_VERSION = 1
SESSION_TIMEOUT_S = 90
PROCESS_TIMEOUT_S = 60
TOTAL_SESSION_TIMEOUT_S = 300


def _require(condition, message):
    """A named assertion that cannot be disabled by ``-O``."""
    if not condition:
        raise AssertionError(message)


def _scripts_dir(prefix):
    """The interpreter's script directory selected from the live platform."""
    return Path(prefix) / ("Scripts" if os.name == "nt" else "bin")


def _console_name():
    return PROGRAM + (".exe" if os.name == "nt" else "")


def _resolved(path):
    return Path(path).resolve()


def _site_roots():
    """The isolated environment's site-packages roots, resolved."""
    roots = {
        _resolved(sysconfig.get_path("purelib")),
        _resolved(sysconfig.get_path("platlib")),
    }
    return tuple(sorted(roots))


def _assert_interpreter_and_origins():
    """Prove CPython 3.11, an isolated prefix and installed package origins."""
    _require(sys.implementation.name == "cpython",
             f"not CPython: {sys.implementation.name!r}")
    _require(sys.version_info[:2] == (3, 11),
             f"not the reviewed floor: {sys.version!r}")
    _require(sys.version_info.releaselevel == "final",
             f"not a final release: {sys.version_info.releaselevel!r}")
    _require(sys.flags.isolated == 1,
             "the harness must run with -I (sys.flags.isolated != 1)")
    _require(sys.flags.no_user_site == 1,
             "user site-packages were not disabled")
    _require(sys.prefix != sys.base_prefix,
             f"not an isolated PEP 405 environment: {sys.prefix!r}")

    repo = _resolved(Path(__file__).resolve().parents[1])
    _require("" not in sys.path,
             "the current directory is on sys.path")
    path_entries = {_resolved(entry) for entry in sys.path if entry}
    _require(repo not in path_entries,
             f"the checkout is on sys.path: {repo}")
    _require(_resolved(Path(__file__).parent) not in path_entries,
             "the harness script directory leaked onto sys.path")

    roots = _site_roots()
    modules = (
        "mcp",
        "pydantic",
        "anyio",
        "watchdog_catalog",
        "watchdog_catalog.catalog",
        "watchdog_catalog.query",
        "watchdog_catalog.source",
        "watchdog_catalog.server",
        "watchdog_catalog.__main__",
    )
    origins = {}
    for name in modules:
        module = importlib.import_module(name)
        origin = _resolved(module.__file__)
        _require(any(origin.is_relative_to(root) for root in roots),
                 f"{name} did not resolve from the installed environment: "
                 f"{origin}")
        _require(not origin.is_relative_to(repo),
                 f"{name} resolved from the checkout: {origin}")
        origins[name] = str(origin)

    dependencies = {}
    for distribution, expected in REVIEWED_DEPENDENCIES:
        installed = importlib.metadata.version(distribution)
        _require(installed == expected,
                 f"{distribution} is {installed!r}, expected {expected!r}")
        dependencies[distribution] = installed

    resource = importlib.resources.files("watchdog_catalog").joinpath(
        "tools.json")
    contract = json.loads(resource.read_text(encoding="utf-8"))
    names = tuple(entry["name"] for entry in contract)
    _require(names == TOOL_NAMES,
             f"packaged tool contract names are {names!r}, expected "
             f"{TOOL_NAMES!r}")
    _require(len(set(names)) == len(TOOL_NAMES),
             "the packaged tool contract repeats a name")

    return {
        "version": sys.version,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "site_packages": [str(root) for root in roots],
        "origins": origins,
        "dependencies": dependencies,
        "tools": list(names),
    }


def _selected_cases(fixtures):
    """The two frozen golden rows, with their fixture identity checked."""
    document = json.loads(
        (fixtures / "cases.json").read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in document["cases"]}
    selected = {}
    for case_id, tool in ((GET_MODEL_CASE_ID, "get_model"),
                          (SEARCH_MODELS_CASE_ID, "search_models")):
        _require(case_id in by_id, f"missing golden case {case_id!r}")
        case = by_id[case_id]
        _require(case["tool"] == tool,
                 f"{case_id} is not a {tool} case")
        fixture = case.get("fixture", document["fixture"])
        _require(fixture == "lookup.json",
                 f"{case_id} uses {fixture!r}, expected 'lookup.json'")
        selected[case_id] = case
    return selected


def _copy_fixture(fixtures, directory, filename):
    """Copy the immutable fixture into an owned path and return the copy."""
    directory.mkdir(parents=True)
    copied = directory / filename
    shutil.copyfile(fixtures / "lookup.json", copied)
    return copied


def _check_pure_queries(fixtures, copy, fixture_body, cases):
    """Load the copied fixture through installed APIs and match the goldens."""
    from watchdog_catalog import catalog, query, source

    fixture_text = (fixtures / "lookup.json").read_text(encoding="utf-8")
    expected_body = catalog.load_snapshot(fixture_text)
    _require(expected_body == fixture_body,
             "the immutable fixture changed after it was first loaded")
    copied_body = source.load_catalog_file(copy)
    _require(isinstance(copied_body, dict) and copied_body,
             "load_catalog_file returned no snapshot")
    _require(copied_body == expected_body,
             "the copied fixture did not load to the fixture body")

    get_case = cases[GET_MODEL_CASE_ID]
    search_case = cases[SEARCH_MODELS_CASE_ID]
    got_get = query.get_model(copied_body, get_case["args"])
    _require(got_get == get_case["expect"],
             f"{GET_MODEL_CASE_ID}: pure get_model payload differs from the "
             f"literal golden\nGOT: {json.dumps(got_get, sort_keys=True)}")
    _require(got_get["counts"]["endpoints"] > 0,
             f"{GET_MODEL_CASE_ID}: the successful lookup is empty")
    got_search = query.search_models(copied_body, search_case["args"])
    _require(got_search == search_case["expect"],
             f"{SEARCH_MODELS_CASE_ID}: pure search_models payload differs "
             f"from the literal golden\nGOT: "
             f"{json.dumps(got_search, sort_keys=True)}")
    _require(got_search["returned_count"] > 0
             and len(got_search["endpoints"]) > 0,
             f"{SEARCH_MODELS_CASE_ID}: the successful search is empty")
    return {
        "get_model": GET_MODEL_CASE_ID,
        "search_models": SEARCH_MODELS_CASE_ID,
        "snapshot_id": copied_body["snapshot_id"],
        "endpoints": {
            "get_model": got_get["counts"]["endpoints"],
            "search_models": got_search["returned_count"],
        },
    }


def _cache_entry(source_api, location, cache_dir):
    """The one source-scoped cache entry path for this copied file."""
    return source_api.DiskCache("file", str(location),
                                cache_dir=str(cache_dir)).path


def _read_cache_entry(path):
    with open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _check_cache_persistence(source_api, copy, fixture_body, cache_dir):
    """Persist one fixed-time observation, then prove a NEW object reloads."""
    persisted_source = source_api.CatalogSource(
        "file", str(copy), cache_dir=str(cache_dir))
    fresh = persisted_source.refresh(now=FETCHED_AT)
    _require(fresh.get("ok") is True, f"fresh file refresh failed: {fresh!r}")
    _require(fresh["snapshot"] == fixture_body,
             "the fresh refresh body differs from the immutable fixture")
    _require(fresh["snapshot"]["snapshot_id"]
             == fixture_body["snapshot_id"],
             "the fresh refresh snapshot_id changed")
    metadata = fresh["source"]
    _require(metadata["mode"] == "file" and metadata["url"] is None,
             f"unexpected file-source provenance: {metadata!r}")
    _require(metadata["cached"] is False,
             "a fresh file read reported cached=True")
    _require(metadata["fetched_at"] == FETCHED_AT,
             f"fetched_at is {metadata['fetched_at']!r}, expected "
             f"{FETCHED_AT}")
    _require("cache_write_failed" not in metadata["warnings"],
             f"the reviewed cache write failed: {metadata['warnings']!r}")
    _require(metadata["warnings"] == [],
             f"a fresh file read carried warnings: {metadata['warnings']!r}")

    entry_path = _cache_entry(source_api, copy, cache_dir)
    _require(entry_path is not None, "the cache root was unavailable")
    _require(entry_path.is_file(),
             f"no persisted cache entry at {entry_path}")
    entry = _read_cache_entry(entry_path)
    _require(set(entry) == {"cache_version", "source", "fetched_at", "etag",
                            "last_modified", "snapshot"},
             f"unexpected cache envelope: {sorted(entry)!r}")
    _require(entry["cache_version"] == CACHE_VERSION,
             f"cache_version is {entry['cache_version']!r}")
    _require(entry["source"] == {"kind": "file",
                                 "location": os.path.abspath(str(copy))},
             f"cache source identity is {entry['source']!r}")
    _require(entry["fetched_at"] == FETCHED_AT,
             f"cache fetched_at is {entry['fetched_at']!r}")
    _require(entry["snapshot"] == fixture_body,
             "the persisted snapshot differs from the fixture body")

    # Make ONLY the owned copy unavailable, with every handle already closed.
    hidden = copy.with_name(copy.name + ".unavailable")
    os.replace(copy, hidden)
    _require(not copy.exists(), "the selected file was not made unavailable")

    reloaded_source = source_api.CatalogSource(
        "file", str(copy), cache_dir=str(cache_dir))
    fallback = reloaded_source.refresh(now=RELOADED_AT)
    _require(fallback.get("ok") is True,
             f"last-good disk fallback failed: {fallback!r}")
    _require(fallback["snapshot"] == fixture_body,
             "the fallback body differs from the persisted fixture body")
    _require(fallback["snapshot"]["snapshot_id"]
             == fixture_body["snapshot_id"],
             "the fallback snapshot_id changed")
    fallback_metadata = fallback["source"]
    _require(fallback_metadata["cached"] is True,
             "the disk fallback did not report cached=True")
    _require(fallback_metadata["fetched_at"] == FETCHED_AT,
             f"the fallback lost the original fetched_at: "
             f"{fallback_metadata['fetched_at']!r}")
    _require(fallback_metadata["fetch_age_s"]
             == RELOADED_AT - FETCHED_AT,
             f"the fallback retrieval age is "
             f"{fallback_metadata['fetch_age_s']!r}")
    _require(fallback_metadata["warnings"]
             == ["refresh_failed", "cache_fallback"],
             f"the fallback warnings are "
             f"{fallback_metadata['warnings']!r}")

    empty_cache = cache_dir.parent / (cache_dir.name + " empty")
    unavailable_source = source_api.CatalogSource(
        "file", str(copy), cache_dir=str(empty_cache))
    unavailable = unavailable_source.refresh(now=RELOADED_AT)
    _require(unavailable == SOURCE_UNAVAILABLE,
             f"an unavailable file with an empty cache returned "
             f"{unavailable!r}, expected the frozen source error")

    return {
        "fetched_at": FETCHED_AT,
        "reloaded_at": RELOADED_AT,
        "cache_entry": str(entry_path),
        "fallback_warnings": list(fallback_metadata["warnings"]),
        "empty_cache_error": unavailable["error"]["code"],
    }


def _child_environment(work):
    """A minimal credential-free child environment owned by this job."""
    home = work / "home"
    temporary = work / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    temporary.mkdir(parents=True, exist_ok=True)
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR"):
            value = os.environ.get(name)
            if value:
                environment[name] = value
        local = home / "AppData" / "Local"
        local.mkdir(parents=True, exist_ok=True)
        environment["USERPROFILE"] = str(home)
        environment["LOCALAPPDATA"] = str(local)
    _require("PYTHONPATH" not in environment,
             "the child environment must not carry PYTHONPATH")
    return environment


def _decode_result(result, label):
    """One successful SDK tool result decoded from its real content blocks."""
    _require(result.is_error is not True,
             f"{label}: the server returned a protocol error result")
    _require(len(result.content) == 1,
             f"{label}: expected exactly one content block, got "
             f"{len(result.content)}")
    block = result.content[0]
    _require(getattr(block, "type", None) == "text",
             f"{label}: unexpected content block {block!r}")
    payload = json.loads(block.text)
    _require(isinstance(payload, dict),
             f"{label}: the tool payload is not an object")
    if result.structured_content is not None:
        _require(result.structured_content == payload,
                 f"{label}: structured and text content disagree")
    return payload


def _assert_golden_core(payload, golden, label):
    """Compare the complete query core, excluding separate provenance."""
    core = {key: value for key, value in payload.items() if key != "source"}
    _require("source" in payload, f"{label}: no source provenance")
    _require(core == golden,
             f"{label}: query core differs from the literal golden\n"
             f"GOT: {json.dumps(core, sort_keys=True)}")


async def _initialize(session, label):
    initialization = await asyncio.wait_for(
        session.initialize(), SESSION_TIMEOUT_S)
    _require(initialization.server_info is not None,
             f"{label}: no server identity")
    _require(initialization.server_info.name == PROGRAM,
             f"{label}: server identity is "
             f"{initialization.server_info.name!r}")
    listed = await asyncio.wait_for(session.list_tools(), SESSION_TIMEOUT_S)
    names = tuple(tool.name for tool in listed.tools)
    _require(names == TOOL_NAMES,
             f"{label}: discovered {names!r}, expected {TOOL_NAMES!r}")
    return initialization


async def _call(session, tool, arguments, label):
    result = await asyncio.wait_for(
        session.call_tool(tool, arguments), SESSION_TIMEOUT_S)
    return _decode_result(result, label)


async def _first_transport_session(console, arguments, cwd, environment,
                                   cases, fixture_body):
    """Real SDK client over the installer-generated console's stdio."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=str(console), args=arguments, env=environment, cwd=str(cwd))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await _initialize(session, "first stdio session")

            model = await _call(
                session, "get_model", cases[GET_MODEL_CASE_ID]["args"],
                "stdio get_model")
            _assert_golden_core(model, cases[GET_MODEL_CASE_ID]["expect"],
                                "stdio get_model")
            model_source = model["source"]
            _require(model_source["mode"] == "file"
                     and model_source["url"] is None,
                     f"stdio get_model provenance: {model_source!r}")
            _require(model_source["cached"] is False,
                     "the first stdio acquisition was not a fresh file read")
            _require("refresh_failed" not in model_source["warnings"],
                     f"the first stdio acquisition failed: "
                     f"{model_source['warnings']!r}")
            _require(model["snapshot_id"] == fixture_body["snapshot_id"],
                     "stdio get_model snapshot identity changed")

            search = await _call(
                session, "search_models",
                cases[SEARCH_MODELS_CASE_ID]["args"], "stdio search_models")
            _assert_golden_core(
                search, cases[SEARCH_MODELS_CASE_ID]["expect"],
                "stdio search_models")
            search_source = search["source"]
            _require(search_source["mode"] == "file"
                     and search_source["url"] is None,
                     f"stdio search provenance: {search_source!r}")
            _require(isinstance(search_source["cached"], bool),
                     f"stdio search cached flag: {search_source!r}")
            _require("refresh_failed" not in search_source["warnings"],
                     f"the stdio search acquisition failed: "
                     f"{search_source['warnings']!r}")
            _require(search["snapshot_id"] == fixture_body["snapshot_id"],
                     "stdio search snapshot identity changed")
            return model, search


async def _fallback_transport_session(console, arguments, cwd, environment,
                                      golden, cases, fetched_at):
    """A NEW console process must serve the persisted body from disk."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=str(console), args=arguments, env=environment, cwd=str(cwd))
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await _initialize(session, "fallback stdio session")
            payload = await _call(
                session, "get_model", cases[GET_MODEL_CASE_ID]["args"],
                "stdio cached get_model")
            _assert_golden_core(payload, golden, "stdio cached get_model")
            metadata = payload["source"]
            _require(metadata["mode"] == "file" and metadata["url"] is None,
                     f"cached stdio provenance: {metadata!r}")
            _require(metadata["cached"] is True,
                     "the new process did not serve cached=True")
            _require(metadata["fetched_at"] == fetched_at,
                     f"cached stdio fetched_at is "
                     f"{metadata['fetched_at']!r}, expected {fetched_at!r}")
            warnings = metadata["warnings"]
            _require(warnings[:2] == ["refresh_failed", "cache_fallback"],
                     f"cached stdio warnings: {warnings!r}")
            return payload


async def _transport_checks(console, console_arguments, cwd, environment,
                            cache_dir, cases, fixture_body):
    """Run both stdio sessions and prove the cache existed between them."""
    model, search = await _first_transport_session(
        console, console_arguments, cwd, environment, cases, fixture_body)

    entries = sorted(cache_dir.glob("*.json"))
    _require(len(entries) == 1,
             f"expected one persisted stdio cache entry, found "
             f"{[entry.name for entry in entries]!r}")
    entry = _read_cache_entry(entries[0])
    _require(entry["snapshot"] == fixture_body,
             "the stdio cache entry body differs from the fixture")
    fetched_at = entry["fetched_at"]
    _require(isinstance(fetched_at, int) and not isinstance(fetched_at, bool),
             f"the stdio cache fetched_at is not an integer: {fetched_at!r}")

    return model, search, fetched_at, entries[0]


def _run_console(console, arguments, cwd, environment, label):
    """One independent bounded console process with stdin at EOF."""
    try:
        completed = subprocess.run(
            [str(console), *arguments], cwd=str(cwd), env=environment,
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=PROCESS_TIMEOUT_S)
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"{label}: console exceeded {PROCESS_TIMEOUT_S}s") from error
    _require(completed.returncode == 0,
             f"{label}: console exited {completed.returncode}\n"
             f"stdout: {completed.stdout!r}\n"
             f"stderr: {completed.stderr!r}")
    return completed


def _check_console_eof_and_help(console, eof_file, eof_cache, cwd,
                                environment):
    """The console exits 0 with empty stdout on EOF, and ``--help`` works."""
    eof = _run_console(
        console, ["--catalog-file", str(eof_file),
                  "--cache-dir", str(eof_cache)],
        cwd, environment, "stdin EOF launch")
    _require(eof.stdout == b"",
             f"stdin EOF launch wrote to stdout: {eof.stdout!r}")

    help_run = _run_console(console, ["--help"], cwd, environment,
                            "console --help")
    text = help_run.stdout.decode("utf-8", "replace")
    _require(text.strip() != "", "--help printed nothing")
    _require("usage:" in text.lower(), f"--help printed no usage: {text!r}")
    _require(PROGRAM in text, f"--help omitted {PROGRAM!r}: {text!r}")


def _assert_console_identity(console):
    """The console is the installer-generated script of THIS environment."""
    scripts = _scripts_dir(sys.prefix)
    selected = _resolved(console)
    _require(selected.name == _console_name(),
             f"console {selected} is not named {_console_name()!r}")
    _require(selected.parent == scripts.resolve(),
             f"console {selected} is not in the installed {scripts}")
    _require(selected.is_file(),
             f"the installed console does not exist: {selected}")
    _require(Path(sys.executable).parent.resolve() == scripts.resolve(),
             f"interpreter {sys.executable} is not the environment's "
             f"{scripts} interpreter")


def _parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="consumer_smoke",
        description="Installed-wheel consumer smoke for the standalone MCP.")
    parser.add_argument("--fixtures", required=True, metavar="DIR",
                        help="the immutable tests/fixtures/catalog directory")
    parser.add_argument("--console", required=True, metavar="PATH",
                        help="the installed free-inference-watchdog-mcp "
                             "console executable")
    parser.add_argument("--work", required=True, metavar="DIR",
                        help="a fresh, empty, owned working directory")
    return parser.parse_args(argv)


def _prepare(fixtures, work, console):
    _require(fixtures.is_dir(), f"missing fixture directory: {fixtures}")
    for name in ("lookup.json", "cases.json"):
        _require((fixtures / name).is_file(),
                 f"missing immutable fixture {fixtures / name}")
    _require(work.is_dir(), f"missing work directory: {work}")
    _require(not any(work.iterdir()),
             f"the work directory is not fresh: {work}")
    _assert_console_identity(console)


def main(argv=None):
    arguments = _parse_arguments(argv)
    fixtures = _resolved(arguments.fixtures)
    work = _resolved(arguments.work)
    console = Path(arguments.console)

    from watchdog_catalog import catalog, source

    _prepare(fixtures, work, console)
    immutable = {
        name: (fixtures / name).read_bytes()
        for name in ("lookup.json", "cases.json")
    }
    identity = _assert_interpreter_and_origins()
    cases = _selected_cases(fixtures)

    fixture_text = (fixtures / "lookup.json").read_text(encoding="utf-8")
    fixture_body = catalog.load_snapshot(fixture_text)

    pure_copy = _copy_fixture(
        fixtures, work / "catalogue space \u00e9", "lookup copy.json")
    pure = _check_pure_queries(fixtures, pure_copy, fixture_body, cases)

    cache_dir = work / "cache space \u00e9"
    cache = _check_cache_persistence(
        source, pure_copy, fixture_body, cache_dir)

    transport_file = _copy_fixture(
        fixtures, work / "transport space \u00e9", "lookup transport.json")
    transport_cache = work / "transport cache \u00e9"
    transport_cache.mkdir()
    neutral = work / "neutral"
    neutral.mkdir()
    environment = _child_environment(work)
    console_arguments = ["--catalog-file", str(transport_file),
                         "--cache-dir", str(transport_cache)]

    model, search, fetched_at, entry_path = asyncio.run(asyncio.wait_for(
        _transport_checks(console, console_arguments, neutral, environment,
                          transport_cache, cases, fixture_body),
        TOTAL_SESSION_TIMEOUT_S))

    os.replace(transport_file, transport_file.with_name(
        transport_file.name + ".unavailable"))
    _require(not transport_file.exists(),
             "the transport file was not made unavailable")
    fallback = asyncio.run(asyncio.wait_for(
        _fallback_transport_session(
            console, console_arguments, neutral, environment,
            cases[GET_MODEL_CASE_ID]["expect"], cases, fetched_at),
        TOTAL_SESSION_TIMEOUT_S))

    eof_file = _copy_fixture(
        fixtures, work / "eof space \u00e9", "lookup eof.json")
    _check_console_eof_and_help(
        console, eof_file, work / "eof cache \u00e9", neutral, environment)

    for name, original in immutable.items():
        _require((fixtures / name).read_bytes() == original,
                 f"the immutable fixture was modified: {fixtures / name}")

    report = {
        "ok": True,
        "interpreter": identity,
        "cases": pure,
        "cache": cache,
        "stdio": {
            "tools": list(TOOL_NAMES),
            "get_model_endpoints": model["counts"]["endpoints"],
            "search_models_returned": search["returned_count"],
            "cache_entry": str(entry_path),
            "cached_fallback_fetched_at": fallback["source"]["fetched_at"],
            "cached_fallback_warnings":
                list(fallback["source"]["warnings"]),
            "stdin_eof_exit": 0,
            "stdin_eof_stdout_empty": True,
            "help_exit": 0,
        },
        "checks": [
            "installed_imports",
            "packaged_tool_contract",
            "pure_golden_queries",
            "cache_persist_and_new_object_reload",
            "cache_source_unavailable",
            "stdio_discovery_and_calls",
            "stdio_new_process_cached_fallback",
            "console_stdin_eof",
            "console_help",
        ],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
