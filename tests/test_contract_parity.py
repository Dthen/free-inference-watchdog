"""Permanent Python/Node contract parity harness for the shared catalogue core.

This module is the frozen Task-7 parity lane. It compares the ACCEPTED Python
reference (`watchdog_catalog.catalog` + `watchdog_catalog.query`) with the
dependency-free shared implementation in `web/catalog-query.js`, executed by the
real Node CLI at `tests/js/run-contract.cjs`, over EVERY frozen fixture family.

Independence of the oracle. The frozen fixture tables carry hand-authored
literal expectations; every row is therefore asserted three ways:

* Python result == the literal `expect` of the row,
* Node result   == the same literal `expect`,
* Python result == Node result over the WHOLE payload.

Agreement between the two runtimes is never accepted as proof of correctness:
a row only passes when both runtimes also match the independent literal. The
additional synthetic snapshots below (bulk pagination, adversarial registries)
carry explicit Python-side literal assertions as well as the differential
comparison.

Comparator. Typed canonical text on BOTH sides -- Python
`catalog.canonical_bytes` and Node `canonicalText` -- so object keys are
canonicalized while ARRAY order and every scalar TYPE survive: `true` differs
from `1`, `null` differs from `0` and `1` differs from `1.0`. A permissive
Python `==`, a `json.dumps` default encoder, a `JSON.stringify`/`parse` round
trip, sorted arrays or removed semantic fields would each hide a real drift.

Node discovery. `CONTRACT_NODE` is authoritative when set (a broken value
FAILS); otherwise `node` is discovered on PATH and an absent runtime gives an
actionable skip. The verification run for this unit executes with Node present,
so no parity test is skipped there.

The Node child always runs with an explicit argv/cwd, a timeout, captured
stdout/stderr and a minimal allowlisted environment (no inherited PYTHONPATH /
PYTHONHOME / tool overrides).

Transport bounds are enforced WHILE the bytes move. `subprocess.run(...)`/
`communicate()` buffer a whole stream in memory and only then compare its
length (Python docs: "The data read is buffered in memory, so do not use this
method if the data size is large or unlimited"), so the parity lane drives
every child through `_bounded_child`: one thread per pipe direction, a finite
cap per stream checked on each bounded chunk read, a bounded stdin writer and an
explicit timeout/kill/wait lifecycle that reaps the child. The runner's own
stdin bound is enforced the same way, on incremental fixed-size reads, with the
entry point made explicit so that `require()`/`import()` of the runner is inert.
"""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import NamedTuple

import pytest

from watchdog_catalog import catalog
from watchdog_catalog import query as catalog_query

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "catalog"
RUNNER = REPO_ROOT / "tests" / "js" / "run-contract.cjs"
SHARED_MODULE = REPO_ROOT / "web" / "catalog-query.js"

CHILD_TIMEOUT_S = 120
MAX_CHILD_OUTPUT_BYTES = 64 * 1024 * 1024
#: The runner's documented raw-stdin bound (32 MiB) and the fixed read size it
#: uses to enforce it incrementally; the detection allowance is one read.
RUNNER_STDIN_BOUND_BYTES = 32 * 1024 * 1024
RUNNER_READ_CHUNK_BYTES = 64 * 1024
#: One bounded chunk for every child pipe. A capture can therefore add at most
#: one chunk beyond its cap, never a whole stream.
PIPE_CHUNK_BYTES = 64 * 1024
#: How long a drained pipe thread may take to finish after the child is gone.
PIPE_THREAD_JOIN_S = 30
#: Small caps for the transport controls, so no test has to hold a 64 MiB blob.
CONTROL_CAP_BYTES = 256 * 1024
#: One frozen clock for every case that needs `now`; never the real clock.
FIXED_NOW = 1759200000

# The declared fixture inventory: standalone snapshots and the rule/expectation
# tables that reference them. Every declared name must exist on disk and every
# reference must resolve to a declared snapshot; the integrity owner proves it.
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
TABLE_FIXTURES = (
    "argument-cases.json",
    "canonical-cases.json",
    "cases.json",
    "metadata-normalization.json",
    "ordering-cases.json",
    "raw-json-cases.json",
    "structural-cases.json",
)
FIXTURE_FILES = frozenset(SNAPSHOT_FIXTURES) | frozenset(TABLE_FIXTURES)

#: Every tool the shared contract exposes. The case table and the argument
#: valid controls must each cover exactly these, so a dropped tool is visible.
TOOL_NAMES = frozenset({
    "get_model",
    "watchdog_status",
    "search_models",
})
#: Search goldens the case table must keep carrying: pagination bounds, a
#: cross-provider page, an exhausted and a beyond-end page, an empty query and
#: provider-B natural ordering.
SEARCH_ANCHOR_IDS = frozenset({
    "search_pagination_limit2_offset0",
    "search_pagination_limit2_offset2_crosses_provider",
    "search_pagination_limit2_offset4_exhausted",
    "search_pagination_limit2_offset99_beyond_end",
    "search_provider_a_limit200_empty_query",
    "search_shared_query_limit2_offset0",
    "search_ordering_provider_b_natural_page",
    "search_default_limit_five_rows",
})
SEARCH_ROW_FIELDS = frozenset({
    "provider", "model_id", "name", "context_length", "input_modalities",
    "output_modalities", "supported_parameters",
})
SEARCH_PAYLOAD_FIELDS = frozenset({
    "ok", "snapshot_id", "tick_epoch", "provider_staleness", "total_matches",
    "next_offset", "endpoints",
})
EXPECTED_EXPORTS = sorted([
    "CatalogError", "JsonFloat", "MAX_JSON_DEPTH", "MAX_SAFE_INT",
    "SCHEMA_VERSION", "canonicalBytes", "canonicalText",
    "compareNaturalIds", "decodeTypedJson", "getModel",
    "isFloatToken", "loadJson",
    "loadSnapshot", "naturalKey", "normalizeCapabilities",
    "projectCapabilities", "projectModel", "searchInputModalityPairs",
    "searchMinContextPairs", "searchModels", "searchSupportedParameterPairs",
    "searchTextProviderPairs", "snapshotHash", "stripFreeMarker",
    "validateSnapshot", "watchdogStatus",
])


# ---------------------------------------------------------------------------
# environment, child process and origin guards
# ---------------------------------------------------------------------------


def _node_binary() -> str:
    """The Node executable: an explicit override is authoritative, else PATH."""
    override = os.environ.get("CONTRACT_NODE")
    if override is not None:
        if not override:
            pytest.fail("CONTRACT_NODE is set but empty; set it to a Node binary "
                        "or unset it to use PATH discovery")
        resolved = shutil.which(override)
        if resolved is None and os.path.isfile(override):
            resolved = os.path.abspath(override)
        if resolved is None:
            pytest.fail(f"CONTRACT_NODE={override!r} is not an executable Node "
                        "binary; the explicit override is authoritative")
        return resolved
    resolved = shutil.which("node")
    if resolved is None:
        pytest.skip("Node is not on PATH: install Node or set CONTRACT_NODE to "
                    "run the permanent Python/Node contract parity lane")
    return resolved


def _child_env() -> dict:
    """A minimal allowlisted environment for the Node CLI.

    No inherited PYTHONPATH, PYTHONHOME, NODE_OPTIONS or tool/storage override
    reaches the child, so the parity lane cannot be steered by the caller's
    shell state.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NODE_NO_WARNINGS": "1",
    }
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    return env


@pytest.fixture(scope="session")
def node() -> str:
    return _node_binary()


def _contained(path, root: Path) -> bool:
    """True when `path` resolves inside `root` (symlink and boundary safe)."""
    resolved_root = Path(os.path.realpath(root))
    resolved = Path(os.path.realpath(path))
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return False
    return resolved.is_file()


def test_python_and_shared_origins_are_this_worktree(node):
    """Both Python modules and the Node module resolve inside the candidate."""
    for module in (catalog, catalog_query):
        origin = Path(module.__file__)
        assert _contained(origin, REPO_ROOT), (
            f"{module.__name__} resolved outside this worktree: {origin}")
        assert Path(os.path.realpath(origin)).is_relative_to(
            Path(os.path.realpath(REPO_ROOT)))
    assert _contained(SHARED_MODULE, REPO_ROOT)
    assert _contained(RUNNER, REPO_ROOT)
    response = _run(node, [{"id": "hello", "op": "hello"}])[0]
    assert response["status"] == "ok"
    assert Path(os.path.realpath(response["module"])) == \
        Path(os.path.realpath(SHARED_MODULE))
    assert _contained(response["module"], REPO_ROOT)
    digest = hashlib.sha256(SHARED_MODULE.read_bytes()).hexdigest()
    assert response["module_sha256"] == digest
    assert response["exports"] == EXPECTED_EXPORTS
    assert response["max_safe_int"] == catalog.MAX_SAFE_INT == 9007199254740991
    assert response["node"] == f"v{_node_version(node)}"


class ChildRun(NamedTuple):
    """One bounded child execution and its explicit failure classification.

    `failure` is `None` for a child that terminated on its own within the
    timeout without crossing a stream cap; otherwise it names the transport
    bound that ended the run ("output-limit" or "timeout"). A non-zero exit of a
    child that stayed inside its caps is NOT a transport failure: the caller
    distinguishes child-nonzero and malformed-response outcomes itself.
    """

    returncode: int | None
    stdout: bytes
    stderr: bytes
    failure: str | None
    elapsed_s: float


def _kill_and_reap(process: subprocess.Popen) -> int | None:
    """Terminate a live child and reap it, so no transport path leaks a zombie."""
    if process.poll() is None:
        try:
            process.kill()
        except OSError:                    # already reaped by someone else
            pass
    try:
        return process.wait(timeout=PIPE_THREAD_JOIN_S)
    except subprocess.TimeoutExpired:      # pragma: no cover - defensive
        return process.poll()


def _close_pipes(process: subprocess.Popen) -> None:
    """Close every pipe we opened, swallowing the errors a dead child implies."""
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _bounded_child(argv, *, payload=None, stdout_cap: int = MAX_CHILD_OUTPUT_BYTES,
                   stderr_cap: int = MAX_CHILD_OUTPUT_BYTES,
                   timeout: float = CHILD_TIMEOUT_S, cwd=REPO_ROOT,
                   env=None) -> ChildRun:
    """Run one real child, bounding both output streams WHILE they arrive.

    Why not `subprocess.run(..., capture_output=True)`: it funnels everything
    through `communicate()`, which buffers each stream in memory and hands the
    bytes over only at EOF (Python docs: "The data read is buffered in memory,
    so do not use this method if the data size is large or unlimited"). Checking
    `len(stdout)` afterwards therefore detects an oversized stream only after it
    has already been allocated -- it does not bound the consumption.

    This helper instead gives each pipe direction its own thread:

    * stdout and stderr are read in chunks of at most `PIPE_CHUNK_BYTES` and
      each stream has its OWN finite cap. The chunk that would cross a cap is
      truncated at the cap, the run is marked "output-limit" and the child is
      killed, so a capture never exceeds `cap` bytes (no disk spool, no
      unbounded growth).
    * stdin is written from a third thread in bounded chunks, so a child that
      emits on its pipes BEFORE it consumes its input cannot deadlock the
      parent on a full pipe (the documented hazard of interleaved
      `.stdin.write()`/`.stdout.read()`).
    * the wait loop enforces the timeout, kills and reaps the child, then joins
      and closes every helper thread and pipe. Normal runs keep their exact
      bytes, native return code and empty-stderr expectation.

    Everything here is portable stdlib. POSIX-only process/resource calls are
    deliberately absent: native RSS/`resource` measurement lives in the
    out-of-tree evidence probes, never in the committed transport path.
    """
    if not isinstance(argv, list) or not argv:
        raise ValueError("argv must be a nonempty argument list")
    feeds_stdin = payload is not None
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if feeds_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(cwd), env=_child_env() if env is None else env)
    started = time.monotonic()
    stop = threading.Event()
    lock = threading.Lock()
    capture = {"stdout": [], "stderr": []}
    seen = {"stdout": 0, "stderr": 0}
    overflow = {"stdout": False, "stderr": False}

    def drain(stream, name: str, cap: int) -> None:
        sink = capture[name]
        reader = getattr(stream, "read1", stream.read)
        try:
            while not stop.is_set():
                chunk = reader(PIPE_CHUNK_BYTES)
                if not chunk:
                    return
                with lock:
                    room = cap - seen[name]
                    if len(chunk) > room:
                        if room > 0:
                            sink.append(chunk[:room])
                            seen[name] += room
                        overflow[name] = True
                        stop.set()
                        return
                    sink.append(chunk)
                    seen[name] += len(chunk)
        except (OSError, ValueError):      # the child is gone / the pipe was closed
            return

    def feed() -> None:
        assert process.stdin is not None       # PIPE was requested above
        try:
            view = memoryview(payload)
            for start in range(0, len(view), PIPE_CHUNK_BYTES):
                if stop.is_set():
                    return
                process.stdin.write(view[start:start + PIPE_CHUNK_BYTES])
                process.stdin.flush()
        except (OSError, ValueError):      # BrokenPipe: the child stopped reading
            return
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    threads = [
        threading.Thread(target=drain, args=(process.stdout, "stdout", stdout_cap),
                         name="contract-stdout", daemon=True),
        threading.Thread(target=drain, args=(process.stderr, "stderr", stderr_cap),
                         name="contract-stderr", daemon=True),
    ]
    if feeds_stdin:
        threads.append(threading.Thread(target=feed, name="contract-stdin",
                                        daemon=True))
    for thread in threads:
        thread.start()

    deadline = started + timeout
    timed_out = False
    returncode = process.poll()
    while returncode is None:
        if stop.is_set():
            returncode = _kill_and_reap(process)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            returncode = _kill_and_reap(process)
            break
        time.sleep(0.002)
        returncode = process.poll()

    for thread in threads:
        thread.join(timeout=PIPE_THREAD_JOIN_S)
    stuck = any(thread.is_alive() for thread in threads)
    _close_pipes(process)
    elapsed = time.monotonic() - started

    if timed_out:
        failure: str | None = "timeout"
    elif overflow["stdout"] or overflow["stderr"]:
        failure = "output-limit"
    elif stuck:                            # pragma: no cover - defensive
        failure = "pipe-thread-stuck"
    else:
        failure = None
    return ChildRun(returncode=returncode,
                    stdout=b"".join(capture["stdout"]),
                    stderr=b"".join(capture["stderr"]),
                    failure=failure, elapsed_s=elapsed)


def _node_version(node: str) -> str:
    run = _bounded_child([node, "--version"], stdout_cap=CONTROL_CAP_BYTES,
                         stderr_cap=CONTROL_CAP_BYTES)
    assert run.failure is None, f"node --version: {run.failure}"
    assert run.returncode == 0, run.stderr
    return run.stdout.decode("utf-8").strip().lstrip("v")


def test_broken_override_fails_instead_of_skipping(monkeypatch):
    """An explicit broken runtime override is a FAILURE, never a silent skip."""
    monkeypatch.setenv("CONTRACT_NODE", "/nonexistent/node-does-not-exist")
    with pytest.raises(pytest.fail.Exception):
        _node_binary()
    monkeypatch.setenv("CONTRACT_NODE", "")
    with pytest.raises(pytest.fail.Exception):
        _node_binary()


def test_runner_protocol_fault_is_a_visible_nonzero_exit(node):
    """A malformed request document must never look like a passing child."""
    run = _bounded_child([node, str(RUNNER)],
                         payload=b'{"protocol":2,"requests":[]}',
                         stdout_cap=CONTROL_CAP_BYTES,
                         stderr_cap=CONTROL_CAP_BYTES)
    assert run.failure is None, f"the fault probe hit a transport bound: {run.failure}"
    assert run.returncode != 0
    assert run.stdout == b""
    assert "run-contract.cjs" in run.stderr.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# runner transport and typed comparator
# ---------------------------------------------------------------------------


def _run(node: str, requests, *, timeout: float = CHILD_TIMEOUT_S):
    """Execute one real Node child and validate its framing exactly."""
    payload = json.dumps({"protocol": 1, "requests": requests},
                         ensure_ascii=True).encode("utf-8")
    run = _bounded_child([node, str(RUNNER)], payload=payload, timeout=timeout)
    assert run.failure is None, (
        "the parity child crossed a transport bound ("
        f"{run.failure}) after {run.elapsed_s:.2f}s: stdout "
        f"{len(run.stdout)} B, stderr {len(run.stderr)} B")
    assert run.returncode == 0, (
        "run-contract.cjs failed with exit "
        f"{run.returncode}: {run.stderr.decode('utf-8', 'replace')}")
    # `_bounded_child` truncates at the cap, so this is a structural guarantee
    # as well as an assertion about this run.
    assert len(run.stdout) <= MAX_CHILD_OUTPUT_BYTES
    assert len(run.stderr) == 0, run.stderr.decode("utf-8", "replace")
    document = json.loads(run.stdout.decode("utf-8"))
    assert set(document) == {"protocol", "responses"}
    assert document["protocol"] == 1
    responses = document["responses"]
    assert [row["id"] for row in responses] == [row["id"] for row in requests]
    assert len(responses) == len(requests)
    for request, response in zip(requests, responses):
        assert response["op"] == request["op"]
    return responses


def canonical(value) -> str:
    """The typed canonical text of one JSON-shaped value (accepted encoder)."""
    return catalog.canonical_bytes(value).decode("ascii")


def _fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _fixture_doc(name: str):
    return json.loads(_fixture_text(name))


def _document_text(document) -> str:
    """The typed JSON text of a document (float tokens stay float tokens)."""
    return json.dumps(document, ensure_ascii=True, separators=(",", ":"))


def _reseal(document):
    """The frozen fixture recipe: `snapshot_id` = canonical body digest."""
    body = {key: value for key, value in document.items()
            if key != "snapshot_id"}
    document["snapshot_id"] = hashlib.sha256(
        catalog.canonical_bytes(body)).hexdigest()
    return document


def _apply_changes(document, changes):
    value = document
    for change in changes:
        op = change["op"]
        if op == "replace_root":
            value = change["value"]
            continue
        parent = value
        for component in change["path"][:-1]:
            parent = parent[component]
        if op == "delete":
            del parent[change["path"][-1]]
        else:
            assert op == "set", op
            parent[change["path"][-1]] = change["value"]
    return value


# ---------------------------------------------------------------------------
# shared models for synthetic snapshots
# ---------------------------------------------------------------------------


def _model(raw_id, *, name=None, description=None, context_length=None,
           input_modalities=None, output_modalities=None,
           supported_parameters=None):
    return {
        "id": raw_id,
        "name": name if name is not None else raw_id,
        "description": description,
        "context_length": context_length,
        "architecture": {"input_modalities": input_modalities,
                         "output_modalities": output_modalities},
        "supported_parameters": supported_parameters,
    }


def _provider(models, display_name, url, *, api_type="openai", stale=False):
    # `model_ids` is the frozen natural-order array (the validator rejects an
    # unsorted array); the models map keeps the caller's insertion order so a
    # reversed map can be exercised without an invalid document.
    return {
        "display_name": display_name,
        "chat_completions_url": url,
        "api_type": api_type,
        "model_ids": sorted(models, key=catalog_query.natural_key),
        "models": models,
        "stale": stale,
    }


def _snapshot(order, providers, *, tick_epoch=1759200000, cadence_s=3600,
              warnings=()):
    document = {
        "schema_version": 2,
        "tick_epoch": tick_epoch,
        "cadence_s": cadence_s,
        "provider_order": list(order),
        "providers": providers,
        "warnings": list(warnings),
    }
    return _reseal(document)


def _tool_request(request_id, tool, args, *, fixture=None, snapshot_text=None,
                  now=None):
    request = {"id": request_id, "op": "tool", "tool": tool,
               "args_text": _document_text(args)}
    if fixture is not None:
        request["fixture"] = fixture
    if snapshot_text is not None:
        request["snapshot_text"] = snapshot_text
    if now is not None:
        request["now"] = now
    return request


def _python_tool(tool, snapshot, args, *, now=FIXED_NOW):
    if tool == "search_models":
        return catalog_query.search_models(snapshot, args)
    if tool == "get_model":
        return catalog_query.get_model(snapshot, args)
    if tool == "watchdog_status":
        return catalog_query.watchdog_status(snapshot, args, now=now)
    raise AssertionError(tool)


def _assert_search_shape(result, where):
    """Exact payload/row field sets and strict scalar types on a search page."""
    assert isinstance(result, dict), where
    assert set(result) == SEARCH_PAYLOAD_FIELDS, where
    assert result["ok"] is True and isinstance(result["ok"], bool), where
    assert type(result["total_matches"]) is int and \
        result["total_matches"] >= 0, where
    if result["next_offset"] is not None:
        assert type(result["next_offset"]) is int, where
    for name, value in result["provider_staleness"].items():
        assert type(name) is str and type(value) is bool, (where, name)
    for row in result["endpoints"]:
        assert set(row) == SEARCH_ROW_FIELDS, where
        assert type(row["provider"]) is str and type(row["model_id"]) is str
        assert row["context_length"] is None or \
            type(row["context_length"]) is int, where
        for name in ("input_modalities", "output_modalities",
                     "supported_parameters"):
            assert row[name] is None or type(row[name]) is list, (where, name)


# ---------------------------------------------------------------------------
# fixture integrity and standalone snapshots
# ---------------------------------------------------------------------------


def test_fixture_tables_have_unique_ids_and_resolvable_references():
    """Declared fixtures exist, references resolve and row ids never collide.

    Row COUNTS are deliberately not pinned: each parity owner below executes
    every row its table carries, so a new row is covered without editing this
    test. What must hold is that every family is nonempty, that ids are unique
    wherever two families share ONE request/result map, and that every fixture
    reference names a declared standalone snapshot.
    """
    for name in FIXTURE_FILES:
        assert (FIXTURE_DIR / name).is_file(), name

    cases = _fixture_doc("cases.json")
    arguments = _fixture_doc("argument-cases.json")
    raw = _fixture_doc("raw-json-cases.json")
    structural = _fixture_doc("structural-cases.json")
    vectors = _fixture_doc("canonical-cases.json")
    ordering = _fixture_doc("ordering-cases.json")
    metadata = _fixture_doc("metadata-normalization.json")
    assert metadata.get("not_a_snapshot") is True

    snapshots = set(SNAPSHOT_FIXTURES)
    assert cases["fixture"] in snapshots, cases["fixture"]
    for row in cases["cases"]:
        assert row.get("fixture", cases["fixture"]) in snapshots, row["id"]
    for row in arguments["invalid_cases"] + arguments["valid_controls"]:
        assert row["fixture"] in snapshots, row["id"]
    for row in structural["invalid_cases"] + structural["valid_controls"]:
        assert row["base_fixture"] in snapshots, row["id"]

    case_ids = [row["id"] for row in cases["cases"]]
    marker_ids = [row["raw_id"] for row in cases["marker_strip_rule"]]
    argument_invalid = [row["id"] for row in arguments["invalid_cases"]]
    argument_valid = [row["id"] for row in arguments["valid_controls"]]
    raw_invalid = [row["id"] for row in raw["invalid_cases"]]
    raw_valid = [row["id"] for row in raw["valid_controls"]]
    structural_invalid = [row["id"] for row in structural["invalid_cases"]]
    structural_valid = [row["id"] for row in structural["valid_controls"]]
    vector_ids = [row["id"] for row in vectors["canonical_vectors"]]
    snapshot_case_ids = [row["id"] for row in vectors["snapshot_cases"]]
    ordering_ids = [row["id"] for row in ordering["cases"]]
    metadata_ids = [row["id"] for row in metadata["rows"]]

    # Each entry lists the row batches the owners below merge into ONE
    # request/result map. Every batch must be nonempty on its own, so an
    # occupied sibling can never hide an emptied subfamily, and ids must be
    # unique inside a family AND across each merged group. The argument and
    # case/marker owners run separate children, so those families stand
    # alone. An empty `raw_id` is a legitimate marker stimulus and is not
    # rejected for being empty.
    id_groups = {
        "cases.json cases": [case_ids],
        "cases.json marker_strip_rule": [marker_ids],
        "argument-cases.json invalid_cases": [argument_invalid],
        "argument-cases.json valid_controls": [argument_valid],
        "raw-json invalid_cases + valid_controls": [raw_invalid, raw_valid],
        "structural invalid_cases + valid_controls": [
            structural_invalid, structural_valid],
        "canonical vectors + snapshot_cases": [vector_ids, snapshot_case_ids],
        "ordering cases + prefixed marker ids": [
            ordering_ids, ["marker:" + raw_id for raw_id in marker_ids]],
        "metadata-normalization rows": [metadata_ids],
    }
    for label, batches in id_groups.items():
        for ids in batches:
            assert ids, f"{label} is empty"
        merged = [value for ids in batches for value in ids]
        duplicates = sorted({value for value in merged
                             if merged.count(value) > 1})
        assert not duplicates, f"{label}: duplicate ids {duplicates}"

    assert {row["tool"] for row in cases["cases"]} == TOOL_NAMES
    assert {row["tool"] for row in arguments["valid_controls"]} == TOOL_NAMES
    search_ids = {row["id"] for row in cases["cases"]
                  if row["tool"] == "search_models"}
    assert SEARCH_ANCHOR_IDS <= search_ids, sorted(
        SEARCH_ANCHOR_IDS - search_ids)


def test_every_standalone_snapshot_loads_and_verifies_its_seal_in_both_engines(
        node):
    requests = []
    for name in SNAPSHOT_FIXTURES:
        text = _fixture_text(name)
        snapshot = catalog.load_snapshot(text)
        assert catalog.snapshot_hash(snapshot) == snapshot["snapshot_id"], name
        requests.append({"id": name, "op": "load_snapshot", "text": text})
    responses = _run(node, requests)
    for name, response in zip(SNAPSHOT_FIXTURES, responses):
        assert response["status"] == "ok", (name, response)
        snapshot = catalog.load_snapshot(_fixture_text(name))
        assert response["hash"] == snapshot["snapshot_id"], name
        assert response["snapshot_id"] == snapshot["snapshot_id"], name
        assert response["canonical"] == canonical(snapshot), name


# ---------------------------------------------------------------------------
# cases.json: complete literal tool expectations for every tool
# ---------------------------------------------------------------------------


def test_cases_table_matches_both_engines_and_the_independent_literals(node):
    document = _fixture_doc("cases.json")
    requests = []
    python_results = {}
    for row in document["cases"]:
        fixture = row.get("fixture", document["fixture"])
        snapshot = catalog.load_snapshot(_fixture_text(fixture))
        args = copy.deepcopy(row["args"])
        now = row.get("now", FIXED_NOW)
        result = _python_tool(row["tool"], snapshot, args, now=now)
        python_results[row["id"]] = (result, row["expect"], row["tool"])
        requests.append(_tool_request(
            row["id"], row["tool"], row["args"], fixture=fixture,
            now=now if row["tool"] == "watchdog_status" else None))
    responses = _run(node, requests)

    for response in responses:
        result, expect, tool = python_results[response["id"]]
        where = response["id"]
        assert canonical(result) == canonical(expect), where
        assert response["canonical"] == canonical(expect), where
        assert response["canonical"] == canonical(result), where
        if tool == "search_models":
            _assert_search_shape(result, where)
            _assert_search_shape(expect, where)
        assert result is not expect


# ---------------------------------------------------------------------------
# argument table: exact envelopes and valid controls over three tools
# ---------------------------------------------------------------------------


def test_argument_table_invalid_envelopes_are_exact_in_both_engines(node):
    document = _fixture_doc("argument-cases.json")
    requests = []
    expected = {}
    for row in document["invalid_cases"]:
        snapshot = catalog.load_snapshot(_fixture_text(row["fixture"]))
        result = _python_tool(row["tool"], snapshot, copy.deepcopy(row["args"]))
        expected[row["id"]] = result
        # Exact literal envelope, including the `parameter` name.
        assert result == row["expect"], row["id"]
        assert set(result) == {"ok", "error"}, row["id"]
        assert set(result["error"]) == {"code", "message", "parameter"}, row["id"]
        assert result["ok"] is False, row["id"]
        requests.append(_tool_request(
            row["id"], row["tool"], row["args"], fixture=row["fixture"],
            now=FIXED_NOW if row["tool"] == "watchdog_status" else None))
    responses = _run(node, requests)
    for response in responses:
        literal = expected[response["id"]]
        assert response["canonical"] == canonical(literal), response["id"]
        assert response["canonical"] == canonical(
            next(row for row in document["invalid_cases"]
                 if row["id"] == response["id"])["expect"]), response["id"]


def test_argument_table_valid_controls_succeed_and_agree(node):
    document = _fixture_doc("argument-cases.json")
    requests = []
    for row in document["valid_controls"]:
        requests.append(_tool_request(
            row["id"], row["tool"], row["args"], fixture=row["fixture"],
            now=FIXED_NOW if row["tool"] == "watchdog_status" else None))
    responses = _run(node, requests)
    for row, response in zip(document["valid_controls"], responses):
        snapshot = catalog.load_snapshot(_fixture_text(row["fixture"]))
        result = _python_tool(row["tool"], snapshot, copy.deepcopy(row["args"]))
        where = row["id"]
        assert result["ok"] is True, where
        assert response["status"] == "ok", where
        assert response["canonical"] == canonical(result), where
        if row["tool"] == "search_models":
            _assert_search_shape(result, where)


# ---------------------------------------------------------------------------
# raw JSON table: rejections and accepted typed values
# ---------------------------------------------------------------------------


def test_raw_json_table_classifies_identically_in_both_loaders(node):
    document = _fixture_doc("raw-json-cases.json")
    requests = []
    for row in document["invalid_cases"]:
        requests.append({"id": row["id"], "op": "load_json",
                         "text": row["json_text"]})
    for row in document["valid_controls"]:
        requests.append({"id": row["id"], "op": "load_json",
                         "text": row["json_text"]})
    responses = {response["id"]: response for response in _run(node, requests)}

    for row in document["invalid_cases"]:
        where = row["id"]
        with pytest.raises(catalog.CatalogError) as excinfo:
            catalog.load_json(row["json_text"])
        assert excinfo.value.to_payload() == row["expect"], where
        response = responses[where]
        assert response["status"] == "rejected", where
        assert response["code"] == "invalid_json", where
        assert response["canonical"] == canonical(row["expect"]), where

    for row in document["valid_controls"]:
        where = row["id"]
        value = catalog.load_json(row["json_text"])
        # Scalar TYPE and container depth stay visible, never coerced.
        if "expect" in row:
            assert canonical(value) == canonical(row["expect"]), where
            assert type(value) is type(row["expect"]), (where, type(value))
        if "expect_kind" in row:
            assert _value_kind(value) == row["expect_kind"], where
        if "expect_depth" in row:
            assert _value_depth(value) == row["expect_depth"], where
        response = responses[where]
        assert response["status"] == "ok", where
        assert response["canonical"] == canonical(value), where

    # Signed zero and float/integer twins survive the cross-runtime comparison.
    twins = {row["id"]: responses[row["id"]]["canonical"]
             for row in document["valid_controls"]
             if row["id"] in ("raw_valid_integral_float_token",
                             "raw_valid_fractional_float",
                             "raw_valid_safe_int_max")}
    assert twins["raw_valid_integral_float_token"] == "1.0"
    assert twins["raw_valid_fractional_float"] == "0.5"
    assert twins["raw_valid_safe_int_max"] == "9007199254740991"


def _value_kind(value) -> str:
    """The fixture's container vocabulary: array, object or mixed nesting."""
    if isinstance(value, list):
        outer = "array"
        children = list(value)
    elif isinstance(value, dict):
        outer = "object"
        children = list(value.values())
    else:
        raise AssertionError(f"not a container: {type(value)}")
    seen = set()
    stack = children
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            seen.add("array")
            stack.extend(item)
        elif isinstance(item, dict):
            seen.add("object")
            stack.extend(item.values())
    return "mixed" if len(seen) > 1 else outer


def _value_depth(value) -> int:
    if isinstance(value, list):
        return 1 + max((_value_depth(item) for item in value), default=0)
    if isinstance(value, dict):
        return 1 + max((_value_depth(item) for item in value.values()),
                       default=0)
    return 0


# ---------------------------------------------------------------------------
# structural table: frozen rejections and accepted controls
# ---------------------------------------------------------------------------


def test_structural_table_classifies_identically_in_both_loaders(node):
    document = _fixture_doc("structural-cases.json")
    requests = []
    cases = {}
    for row in document["invalid_cases"]:
        text = _structural_text(row)
        cases[row["id"]] = (row, text, "invalid")
        requests.append({"id": row["id"], "op": "validate_value",
                         "value_text": text})
    for row in document["valid_controls"]:
        text = _structural_text(row)
        cases[row["id"]] = (row, text, "valid")
        requests.append({"id": row["id"], "op": "validate_value",
                         "value_text": text})
    responses = {response["id"]: response for response in _run(node, requests)}

    for cid, (row, text, kind) in cases.items():
        response = responses[cid]
        # The SAME raw document reaches both structural layers.
        native = json.loads(text)
        if kind == "invalid":
            expected_code = row["expect"]["error"]["code"]
            with pytest.raises(catalog.CatalogError) as excinfo:
                catalog.validate_snapshot(native)
            assert excinfo.value.code == expected_code, cid
            assert excinfo.value.to_payload() == row["expect"], cid
            assert response["status"] == "rejected", cid
            assert response["code"] == expected_code, cid
            assert response["canonical"] == canonical(row["expect"]), cid
        else:
            snapshot = catalog.validate_snapshot(native)
            assert catalog.snapshot_hash(snapshot) == snapshot["snapshot_id"], cid
            assert response["status"] == "ok", cid
            assert response["canonical"] == canonical(snapshot), cid
            assert response["hash"] == snapshot["snapshot_id"], cid

    # When the same text is ALSO raw-acceptable, the full public loader agrees
    # in both engines; the unsafe-numeric rows (raw-rejected by design) are
    # covered at the structural layer above rather than silently dropped.
    public = []
    for cid, (row, text, kind) in cases.items():
        try:
            catalog.load_json(text)
        except catalog.CatalogError:
            continue
        public.append((cid, text, kind, row))
    assert public, "no structural case is also raw-acceptable"
    loaded = {response["id"]: response for response in _run(
        node, [{"id": cid, "op": "load_snapshot", "text": text}
               for cid, text, _, _ in public])}
    for cid, text, kind, row in public:
        response = loaded[cid]
        if kind == "invalid":
            with pytest.raises(catalog.CatalogError) as excinfo:
                catalog.load_snapshot(text)
            assert excinfo.value.to_payload() == row["expect"], cid
            assert response["status"] == "rejected", cid
            assert response["canonical"] == canonical(row["expect"]), cid
        else:
            snapshot = catalog.load_snapshot(text)
            assert response["status"] == "ok", cid
            assert response["canonical"] == canonical(snapshot), cid
            assert response["hash"] == snapshot["snapshot_id"], cid


def _structural_text(row) -> str:
    document = _apply_changes(
        _fixture_doc(row["base_fixture"]), copy.deepcopy(row["changes"]))
    if row["reseal"]:
        _reseal(document)
    return _document_text(document)


# ---------------------------------------------------------------------------
# canonical table: typed vectors and snapshot cases
# ---------------------------------------------------------------------------


def test_canonical_vectors_and_typed_snapshot_cases_match_both_engines(node):
    document = _fixture_doc("canonical-cases.json")
    requests = []
    for row in document["canonical_vectors"]:
        requests.append({"id": row["id"], "op": "canonical_vector",
                         "text": row["json_text"], "scope": row["scope"]})
    for row in document["snapshot_cases"]:
        requests.append({"id": row["id"], "op": "load_snapshot",
                         "text": row["json_text"]})
    responses = {response["id"]: response for response in _run(node, requests)}

    for row in document["canonical_vectors"]:
        where = row["id"]
        value = catalog.load_json(row["json_text"])
        # A `snapshot`-scoped vector is the canonical BODY (root snapshot_id
        # excluded), the exact recipe the public hash uses.
        if row["scope"] == "snapshot":
            assert isinstance(value, dict), where
            body = {key: item for key, item in value.items()
                    if key != "snapshot_id"}
            text = canonical(body)
        else:
            text = canonical(value)
        assert text == row["canonical_text"], where
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == \
            row["sha256"], where
        response = responses[where]
        assert response["status"] == "ok", where
        assert response["sha256"] == row["sha256"], where
        if row["scope"] == "snapshot":
            assert response["body_canonical"] == text, where
        else:
            assert response["canonical"] == text, where

    for row in document["snapshot_cases"]:
        where = row["id"]
        response = responses[where]
        if row.get("accepted") is True:
            snapshot = catalog.load_snapshot(row["json_text"])
            assert catalog.snapshot_hash(snapshot) == snapshot["snapshot_id"], where
            assert response["status"] == "ok", where
            assert response["canonical"] == canonical(snapshot), where
        else:
            with pytest.raises(catalog.CatalogError) as excinfo:
                catalog.load_snapshot(row["json_text"])
            assert excinfo.value.to_payload() == row["expect"], where
            assert response["status"] == "rejected", where
            assert response["canonical"] == canonical(row["expect"]), where

    # The integer/float twin vectors are distinguishable in BOTH runtimes.
    int_vector = next(row for row in document["canonical_vectors"]
                      if row["id"] == "vec_int_lexeme_one")
    float_vector = next(row for row in document["canonical_vectors"]
                        if row["id"] == "vec_float_lexeme_one")
    assert responses[int_vector["id"]]["canonical"] == '{"n":1}'
    assert responses[float_vector["id"]]["canonical"] == '{"n":1.0}'


# ---------------------------------------------------------------------------
# ordering and marker strip tables through the real helpers
# ---------------------------------------------------------------------------


def test_ordering_and_marker_tables_match_both_implementations(node):
    ordering = _fixture_doc("ordering-cases.json")["cases"]
    marker = _fixture_doc("cases.json")["marker_strip_rule"]
    requests = [{"id": row["id"], "op": "ordering", "ids": row["input"]}
                for row in ordering]
    requests += [{"id": "marker:" + row["raw_id"], "op": "marker_strip",
                  "ids": [row["raw_id"]]} for row in marker]
    responses = {response["id"]: response for response in _run(node, requests)}

    for row in ordering:
        where = row["id"]
        ordered = sorted(row["input"], key=catalog_query.natural_key)
        assert ordered == row["expected"], where
        keys = [[list(token) for token in catalog_query.natural_key(raw_id)]
                for raw_id in row["input"]]
        response = responses[where]
        assert response["status"] == "ok", where
        assert response["canonical"] == canonical(
            {"order": row["expected"], "keys": keys}), where
        # Array order is significant, so a sorted-friendly comparator cannot
        # pass by accident.
        if len(row["expected"]) > 1 and row["expected"] != sorted(
                row["expected"]):
            assert response["canonical"] != canonical(
                {"order": sorted(row["expected"]), "keys": keys}), where

    for row in marker:
        where = "marker:" + row["raw_id"]
        stripped = catalog_query.strip_free_marker(row["raw_id"])
        assert stripped == row["stripped"], where
        assert responses[where]["canonical"] == canonical([stripped]), where


# ---------------------------------------------------------------------------
# metadata normalization table: projections and warnings
# ---------------------------------------------------------------------------


def test_metadata_normalization_table_matches_both_engines(node):
    rows = _fixture_doc("metadata-normalization.json")["rows"]
    requests = [{"id": row["id"], "op": "normalization",
                 "raw_text": _document_text(row["raw"])} for row in rows]
    responses = {response["id"]: response for response in _run(node, requests)}

    for row in rows:
        where = row["id"]
        raw = copy.deepcopy(row["raw"])
        before = copy.deepcopy(raw)
        projected, warnings = catalog.project_capabilities(raw)
        assert projected == row["projected"], where
        assert warnings == row["warnings"], where
        assert raw == before, where
        normalized = catalog.normalize_capabilities(copy.deepcopy(projected))
        assert normalized == row["normalized"], where
        assert responses[where]["status"] == "ok", where
        assert responses[where]["canonical"] == canonical(
            {"projected": row["projected"], "warnings": row["warnings"],
             "normalized": row["normalized"]}), where
        assert responses[where]["canonical"] == canonical(
            {"projected": projected, "warnings": warnings,
             "normalized": normalized}), where


# ---------------------------------------------------------------------------
# typed comparator self-controls
# ---------------------------------------------------------------------------


def test_typed_comparator_self_controls(node):
    base = {"b": [1, True, None, 1.0], "a": {"z": 0}}
    # Object KEY order is canonicalized away.
    assert canonical({"a": {"z": 0}, "b": [1, True, None, 1.0]}) == canonical(
        base)
    # Array order is semantic.
    assert canonical({"b": [True, 1, None, 1.0], "a": {"z": 0}}) != canonical(
        base)
    # Scalar types are semantic.
    assert canonical({"x": True}) != canonical({"x": 1})
    assert canonical({"x": None}) != canonical({"x": 0})
    assert canonical({"x": 1}) != canonical({"x": 1.0})
    assert canonical({"x": -0.0}) != canonical({"x": 0})
    # A changed nested field is semantic.
    assert canonical({"a": {"z": 1}, "b": [1, True, None, 1.0]}) != canonical(
        base)

    # The Node encoder makes the same distinctions on the same document text.
    requests = [
        {"id": "int", "op": "canonical_vector", "text": '{"x":1}'},
        {"id": "float", "op": "canonical_vector", "text": '{"x":1.0}'},
        {"id": "bool", "op": "canonical_vector", "text": '{"x":true}'},
        {"id": "null", "op": "canonical_vector", "text": '{"x":null}'},
        {"id": "zero", "op": "canonical_vector", "text": '{"x":0}'},
        {"id": "reordered", "op": "canonical_vector", "text": '{"b":1,"a":2}'},
        {"id": "key-order", "op": "canonical_vector", "text": '{"a":2,"b":1}'},
    ]
    responses = {response["id"]: response for response in _run(node, requests)}
    assert responses["int"]["canonical"] == '{"x":1}'
    assert responses["float"]["canonical"] == '{"x":1.0}'
    assert responses["bool"]["canonical"] == '{"x":true}'
    assert responses["null"]["canonical"] == '{"x":null}'
    assert responses["zero"]["canonical"] == '{"x":0}'
    for left in ("int", "float", "bool", "null", "zero"):
        for right in ("int", "float", "bool", "null", "zero"):
            if left < right:
                assert responses[left]["canonical"] != \
                    responses[right]["canonical"], (left, right)
    assert responses["reordered"]["canonical"] == \
        responses["key-order"]["canonical"] == '{"a":2,"b":1}'


# ---------------------------------------------------------------------------
# bulk pagination differential: 215 pairs, one global sort-then-slice
# ---------------------------------------------------------------------------


def _bulk_snapshot():
    omega = {}
    for index in range(120):
        raw_id = "om-%03d" % index
        omega[raw_id] = _model(
            raw_id, name="Omega row %03d" % index,
            description="Bulk population row for the omega gateway.",
            context_length=65536 if index >= 115 else 8192,
            input_modalities=["text"], output_modalities=["text"],
            supported_parameters=["temperature"])
    alpha = {}
    for index in range(95):
        raw_id = "al-%03d" % index
        alpha[raw_id] = _model(
            raw_id, name="Alpha row %03d" % index,
            description="Bulk population row for the alpha gateway.",
            context_length=8192, input_modalities=["text"],
            output_modalities=["text"], supported_parameters=["temperature"])
    providers = {
        "zero": _provider({}, "Zero", "https://zero.example/v1/chat/completions"),
        "alpha": _provider(alpha, "Alpha",
                           "https://alpha.example/v1/chat/completions"),
        "omega": _provider(omega, "Omega",
                           "https://omega.example/v1/chat/completions"),
    }
    return _snapshot(["omega", "alpha", "zero"], providers)


def test_search_pagination_goldens_and_differential_parity(node):
    snapshot = _bulk_snapshot()
    text = _document_text(snapshot)
    assert sum(len(provider["model_ids"])
               for provider in snapshot["providers"].values()) == 215
    assert snapshot["provider_order"] != list(snapshot["providers"])
    assert snapshot["provider_order"] != sorted(snapshot["provider_order"])

    cases = [
        ("default", {}),
        ("max", {"limit": 200}),
        ("tail", {"limit": 200, "offset": 200}),
        ("cross", {"limit": 2, "offset": 119}),
        ("exact-end", {"limit": 2, "offset": 213}),
        ("continuing", {"limit": 2, "offset": 211}),
        ("at-end", {"limit": 2, "offset": 215}),
        ("beyond", {"limit": 2, "offset": 999999}),
        ("max-safe", {"limit": 2, "offset": catalog.MAX_SAFE_INT}),
        ("filtered", {"min_context_length": 65536, "limit": 3}),
        ("zero-provider", {"provider": "zero"}),
        ("alpha-tail", {"provider": "alpha", "limit": 200, "offset": 94}),
        ("empty-query-slice", {"query": "", "limit": 2, "offset": 118}),
    ]
    requests = [_tool_request(name, "search_models", args,
                              snapshot_text=text) for name, args in cases]
    responses = {response["id"]: response for response in _run(node, requests)}
    assert len(responses) == len(cases)

    results = {}
    for name, args in cases:
        result = catalog_query.search_models(snapshot, copy.deepcopy(args))
        results[name] = result
        _assert_search_shape(result, name)
        assert responses[name]["canonical"] == canonical(result), name

    default = results["default"]
    assert default["total_matches"] == 215
    assert (len(default["endpoints"]), default["next_offset"]) == (5, 5)
    assert [(row["provider"], row["model_id"]) for row in default["endpoints"]] \
        == [("omega", "om-%03d" % index) for index in range(5)]

    max_page = results["max"]
    assert (len(max_page["endpoints"]), max_page["next_offset"]) == (200, 200)
    assert max_page["endpoints"][119]["model_id"] == "om-119"
    assert max_page["endpoints"][120]["provider"] == "alpha"
    assert max_page["endpoints"][120]["model_id"] == "al-000"

    tail = results["tail"]
    assert len(tail["endpoints"]) == 15 and tail["next_offset"] is None
    assert [row["model_id"] for row in tail["endpoints"]] == \
        ["al-%03d" % index for index in range(80, 95)]

    cross = results["cross"]
    assert [(row["provider"], row["model_id"]) for row in cross["endpoints"]] \
        == [("omega", "om-119"), ("alpha", "al-000")]

    assert len(results["exact-end"]["endpoints"]) == 2
    assert results["exact-end"]["next_offset"] is None
    assert results["continuing"]["next_offset"] == 213
    for name in ("at-end", "beyond", "max-safe"):
        assert results[name]["endpoints"] == [], name
        assert results[name]["next_offset"] is None, name
        assert results[name]["total_matches"] == 215, name
        assert results[name]["provider_staleness"] == {
            "omega": False, "alpha": False, "zero": False}, name

    filtered = results["filtered"]
    assert [row["model_id"] for row in filtered["endpoints"]] == \
        ["om-115", "om-116", "om-117"]
    assert (filtered["total_matches"], filtered["next_offset"]) == (5, 3)

    zero = results["zero-provider"]
    assert zero["endpoints"] == [] and zero["total_matches"] == 0
    assert zero["provider_staleness"] == {"zero": False}

    alpha_tail = results["alpha-tail"]
    assert len(alpha_tail["endpoints"]) == 1
    assert alpha_tail["endpoints"][0]["model_id"] == "al-094"
    assert alpha_tail["next_offset"] is None

    empty_quote = results["empty-query-slice"]
    assert [(row["provider"], row["model_id"]) for row in empty_quote["endpoints"]] \
        == [("omega", "om-118"), ("omega", "om-119")]


# ---------------------------------------------------------------------------
# adversarial parity cases
# ---------------------------------------------------------------------------


def _adversarial_snapshots():
    """Sealed registries covering the Task-7 adversarial parity list."""
    prototype_order = ["__proto__", "constructor", "toString", "empty"]
    prototype_models = {
        "__proto__": _model("__proto__", name="Literal prototype key",
                            description="not a prototype",
                            context_length=0, input_modalities=["text"],
                            output_modalities=["text"],
                            supported_parameters=["temperature"]),
        "constructor": _model("constructor", name="Constructor member",
                              context_length=8192,
                              input_modalities=["text"],
                              output_modalities=["text"],
                              supported_parameters=["temperature"]),
        "hasOwnProperty": _model("hasOwnProperty", name="Own property name",
                                 context_length=8192,
                                 input_modalities=["text"],
                                 output_modalities=["text"],
                                 supported_parameters=["temperature"]),
    }
    prototype = _snapshot(prototype_order, {
        "__proto__": _provider(
            prototype_models, "Literal proto",
            "https://proto.example/v1/chat/completions", stale=True),
        "constructor": _provider(
            {"vendor/plain": _model("vendor/plain", name="Plain",
                                    context_length=4096,
                                    input_modalities=["IMAGE"],
                                    output_modalities=["text"],
                                    supported_parameters=["temperature"])},
            "Constructor", "https://constructor.example/v1/chat/completions"),
        "toString": _provider(
            {"vendor/tostring": _model("vendor/tostring",
                                       name="Stringly",
                                       context_length=4096,
                                       input_modalities=["text"],
                                       output_modalities=["text"],
                                       supported_parameters=["top_p"])},
            "ToString", "https://tostring.example/v1/chat/completions"),
        "empty": _provider(
            {}, "Empty", "https://empty.example/v1/chat/completions"),
    })

    unicode_ids = ["vendor/é", "vendor/É", "vendor/e\u0301", "vendor/\U0001f600",
                   "vendor/\U0001f601", "vendor/\uffff", "vendor/\u0000"]
    huge = "vendor/long" + "9" * 64
    huge_next = "vendor/long" + "8" * 64
    unicode_models = {}
    for raw_id in reversed(sorted(unicode_ids + [huge, huge_next],
                                  key=catalog_query.natural_key)):
        unicode_models[raw_id] = _model(
            raw_id, name="Row " + raw_id, context_length=1000,
            input_modalities=["text"], output_modalities=["text"],
            supported_parameters=["temperature"])
    unicode_snapshot = _snapshot(["unicode"], {"unicode": _provider(
        unicode_models, "Unicode",
        "https://unicode.example/v1/chat/completions")})

    boundary_models = {
        "zero-context": _model("zero-context", context_length=0,
                               input_modalities=[], output_modalities=["text"],
                               supported_parameters=[]),
        "null-context": _model("null-context"),
        "false-flags": _model("false-flags", context_length=1,
                             input_modalities=["text"],
                             output_modalities=None,
                             supported_parameters=None),
    }
    boundary = _snapshot(["bounds"], {"bounds": _provider(
        boundary_models, "Bounds",
        "https://bounds.example/v1/chat/completions")},
        tick_epoch=4102444800, warnings=["tick_epoch_unknown"])

    surrogate_name = "Lone \ud800 surrogate"
    surrogate = _snapshot(["surrogates"], {"surrogates": _provider(
        {"vendor/surrogate": _model(
            "vendor/surrogate", name=surrogate_name,
            description="Field with an unpaired \udfff unit",
            context_length=4096, input_modalities=["text"],
            output_modalities=["text"],
            supported_parameters=["temperature"])},
        "Surrogates", "https://surrogate.example/v1/chat/completions")})

    duplicate_ids = {
        "schema_version": 2, "snapshot_id": "0" * 64,
        "tick_epoch": 1759200000, "cadence_s": 3600,
        "provider_order": ["dup"],
        "providers": {"dup": {
            "display_name": "Dup",
            "chat_completions_url": "https://dup.example/v1/chat/completions",
            "api_type": "openai",
            "model_ids": ["vendor/x", "vendor/x"],
            "models": {"vendor/x": _model("vendor/x")},
            "stale": False}},
        "warnings": [],
    }
    invalid_schema = {
        "schema_version": 2.0, "snapshot_id": "0" * 64,
        "tick_epoch": 1759200000, "cadence_s": 3600,
        "provider_order": [], "providers": {}, "warnings": [],
    }
    return {
        "prototype": prototype,
        "unicode": unicode_snapshot,
        "boundary": boundary,
        "surrogate": surrogate,
        "duplicate_ids": duplicate_ids,
        "invalid_schema": invalid_schema,
    }


def test_adversarial_registries_and_documents_match_both_engines(node):
    snapshots = _adversarial_snapshots()
    requests = []
    cases = []

    def add(name, tool, args, document):
        requests.append(_tool_request(
            name, tool, args, snapshot_text=_document_text(document),
            now=FIXED_NOW if tool == "watchdog_status" else None))
        cases.append((name, tool, args, document))

    def add_unsealed(name, tool, args, document):
        """A rejected document: both loaders see the SAME raw text."""
        text = _document_text(document)
        requests.append(_tool_request(name, tool, args, snapshot_text=text))
        cases.append((name, tool, args, text))

    prototype = snapshots["prototype"]
    add("prototype-all", "search_models", {"limit": 200}, prototype)
    add("prototype-query", "search_models", {"query": "__proto__"}, prototype)
    add("prototype-provider", "search_models", {"provider": "constructor"},
        prototype)
    add("prototype-zero-id", "search_models", {"provider": "empty"}, prototype)
    add("prototype-tostring-provider", "search_models",
        {"provider": "toString"}, prototype)

    unicode_snapshot = snapshots["unicode"]
    add("unicode-order", "search_models", {"limit": 200}, unicode_snapshot)
    add("unicode-upper", "search_models", {"query": "É"}, unicode_snapshot)
    add("unicode-lower", "search_models", {"query": "é"}, unicode_snapshot)
    add("unicode-nfd", "search_models", {"query": "e\u0301"},
        unicode_snapshot)
    add("unicode-astral", "search_models", {"query": "\U0001f600"},
        unicode_snapshot)
    add("unicode-huge-run", "search_models", {"query": "8" * 64},
        unicode_snapshot)

    boundary = snapshots["boundary"]
    add("boundary-all", "search_models", {"limit": 200}, boundary)
    add("boundary-context-zero", "search_models",
        {"min_context_length": 0}, boundary)
    add("boundary-context-one", "search_models",
        {"min_context_length": 1}, boundary)
    add("boundary-empty-modalities", "search_models",
        {"input_modalities": ["text"]}, boundary)
    add("boundary-parameters", "search_models",
        {"supported_parameters": ["temperature"]}, boundary)
    add("boundary-status", "watchdog_status", {}, boundary)

    surrogate = snapshots["surrogate"]
    add("surrogate-snapshot", "search_models", {"limit": 200}, surrogate)
    add("surrogate-query", "search_models", {"query": "\ud800"}, surrogate)
    add("surrogate-query-other", "search_models", {"query": "\udfff"},
        surrogate)
    add("surrogate-query-pair", "search_models", {"query": "\ud800\udfff"},
        surrogate)
    add("surrogate-near-miss", "search_models", {"query": "\ud801"}, surrogate)

    add_unsealed("duplicate-model-ids", "search_models", {},
                 snapshots["duplicate_ids"])
    add_unsealed("invalid-schema-version", "search_models", {},
                 snapshots["invalid_schema"])

    responses = {response["id"]: response for response in _run(node, requests)}
    assert len(responses) == len(cases)

    for name, tool, args, document in cases:
        response = responses[name]
        if name in ("duplicate-model-ids", "invalid-schema-version"):
            # A string here is the exact raw text both loaders rejected.
            with pytest.raises(catalog.CatalogError) as excinfo:
                catalog.load_snapshot(document)
            assert excinfo.value.code == "invalid_snapshot", name
            assert response["status"] == "rejected", name
            assert response["code"] == "invalid_snapshot", name
            assert response["canonical"] == canonical(
                excinfo.value.to_payload()), name
            continue
        snapshot = catalog.load_snapshot(_document_text(document))
        result = _python_tool(tool, snapshot, copy.deepcopy(args))
        assert result["ok"] is True, name
        assert response["canonical"] == canonical(result), name

    # The prototype-looking registry keeps every member LITERAL data.
    prototype_result = catalog_query.search_models(
        catalog.load_snapshot(_document_text(prototype)), {"limit": 200})
    assert prototype_result["total_matches"] == 5
    assert [(row["provider"], row["model_id"])
            for row in prototype_result["endpoints"]] == [
        ("__proto__", "__proto__"),
        ("__proto__", "constructor"),
        ("__proto__", "hasOwnProperty"),
        ("constructor", "vendor/plain"),
        ("toString", "vendor/tostring"),
    ]
    assert prototype_result["provider_staleness"] == {
        "__proto__": True, "constructor": False, "toString": False,
        "empty": False}
    proto_query = catalog_query.search_models(
        catalog.load_snapshot(_document_text(prototype)),
        {"query": "__proto__"})
    assert [row["model_id"] for row in proto_query["endpoints"]] == [
        "__proto__"]

    # Unicode order is natural code-point order, not UTF-16 code-unit order.
    unicode_result = catalog_query.search_models(
        catalog.load_snapshot(_document_text(unicode_snapshot)),
        {"limit": 200})
    ordered_ids = [row["model_id"] for row in unicode_result["endpoints"]]
    expected_order = sorted(
        unicode_snapshot["providers"]["unicode"]["model_ids"],
        key=catalog_query.natural_key)
    assert ordered_ids == expected_order
    assert unicode_snapshot["providers"]["unicode"]["models"] is not None
    # The models map insertion order is the REVERSE of the natural order, so a
    # map-order or UTF-16 order implementation cannot pass.
    assert list(unicode_snapshot["providers"]["unicode"]["models"]) == \
        list(reversed(expected_order))
    upper = catalog_query.search_models(
        catalog.load_snapshot(_document_text(unicode_snapshot)),
        {"query": "É"})
    lower = catalog_query.search_models(
        catalog.load_snapshot(_document_text(unicode_snapshot)),
        {"query": "é"})
    assert [row["model_id"] for row in upper["endpoints"]] == ["vendor/É"]
    # ASCII-only folding: `É` never folds to `é`, and the NFD `e`+U+0301 form
    # is a different code-point sequence, so each query has exactly one match.
    assert [row["model_id"] for row in lower["endpoints"]] == ["vendor/é"]

    # Lone surrogate fields: matching the SAME unpaired unit succeeds in both
    # runtimes; a different unit and the astral pair remain distinct values.
    surrogate_accepted = catalog.load_snapshot(_document_text(surrogate))
    surrogate_result = catalog_query.search_models(surrogate_accepted,
                                                   {"query": "\ud800"})
    assert surrogate_result["total_matches"] == 1
    assert surrogate_result["endpoints"][0]["model_id"] == "vendor/surrogate"
    assert responses["surrogate-query"]["canonical"] == canonical(
        surrogate_result)
    other = catalog_query.search_models(surrogate_accepted,
                                        {"query": "\udfff"})
    assert other["total_matches"] == 1  # the description holds \udfff
    assert responses["surrogate-query-other"]["canonical"] == canonical(other)
    pair = catalog_query.search_models(surrogate_accepted,
                                       {"query": "\ud800\udfff"})
    assert pair["total_matches"] == 0
    assert responses["surrogate-query-pair"]["canonical"] == canonical(pair)
    near = catalog_query.search_models(surrogate_accepted,
                                       {"query": "\ud801"})
    assert near["total_matches"] == 0
    assert responses["surrogate-near-miss"]["canonical"] == canonical(near)


def test_duplicate_members_raw_rejection_is_identical(node):
    duplicate_members = (
        '{"schema_version":2,"schema_version":2,"snapshot_id":"' + "0" * 64 +
        '","tick_epoch":1759200000,"cadence_s":3600,"provider_order":[],'
        '"providers":{},"warnings":[]}')
    with pytest.raises(catalog.CatalogError) as excinfo:
        catalog.load_snapshot(duplicate_members)
    assert excinfo.value.code == "invalid_json"
    response = _run(node, [{"id": "dup", "op": "load_snapshot",
                            "text": duplicate_members}])[0]
    assert response["status"] == "rejected"
    assert response["code"] == "invalid_json"
    assert response["canonical"] == canonical(excinfo.value.to_payload())


# ---------------------------------------------------------------------------
# typed numeric inputs
# ---------------------------------------------------------------------------


def test_integral_float_pagination_inputs_are_accepted_in_both_engines(node):
    snapshot = _snapshot(["floats"], {"floats": _provider(
        {"vendor/priced": _model(
            "vendor/priced", name="Priced", context_length=0,
            input_modalities=["text"], output_modalities=["text"],
            supported_parameters=["temperature"])},
        "Floats", "https://floats.example/v1/chat/completions")})
    text = _document_text(snapshot)
    accepted = catalog.load_snapshot(text)
    args = {"min_context_length": 0.0, "limit": 1.0, "offset": 0.0}
    result = catalog_query.search_models(accepted, copy.deepcopy(args))
    _assert_search_shape(result, "typed-float")
    assert result["total_matches"] == 1 and len(result["endpoints"]) == 1
    response = _run(node, [_tool_request("typed", "search_models", args,
                                         snapshot_text=text)])[0]
    assert response["status"] == "ok"
    assert response["canonical"] == canonical(result)


# ---------------------------------------------------------------------------
# isolation, concurrency and batching
# ---------------------------------------------------------------------------


def test_search_models_python_result_is_fresh_and_inputs_are_untouched(node):
    snapshot = _snapshot(["iso"], {"iso": _provider(
        {"vendor/cased": _model(
            "vendor/cased", name="Cased", context_length=100,
            input_modalities=["TEXT", "Image", "text"],
            output_modalities=[], supported_parameters=["Temperature",
                                                        "TEMPERATURE"])},
        "Iso", "https://iso.example/v1/chat/completions")})
    before = copy.deepcopy(snapshot)
    args = {"input_modalities": ["TEXT", "text"],
            "supported_parameters": ["temperature"]}
    args_before = copy.deepcopy(args)
    first = catalog_query.search_models(snapshot, args)
    saved = copy.deepcopy(first)
    first["endpoints"][0]["input_modalities"].append("injected")
    first["endpoints"][0]["supported_parameters"].append("injected")
    first["provider_staleness"]["iso"] = True
    first["endpoints"].clear()
    assert catalog_query.search_models(snapshot, args) == saved
    assert args == args_before
    assert snapshot == before
    assert saved["endpoints"][0]["input_modalities"] == ["text", "image"]
    assert saved["endpoints"][0]["supported_parameters"] == ["temperature"]


def test_concurrent_children_and_one_batched_request_set(node):
    snapshot = _bulk_snapshot()
    text = _document_text(snapshot)
    cases = [("c-default", {}), ("c-cross", {"limit": 2, "offset": 119}),
             ("c-filtered", {"min_context_length": 65536, "limit": 3}),
             ("c-tail", {"limit": 200, "offset": 200})]
    batch = [_tool_request(name, "search_models", args, snapshot_text=text)
             for name, args in cases]
    # The requests are distinct, so a truncated or zipped list cannot pass.
    assert len({request["id"] for request in batch}) == len(batch)
    first = _run(node, batch)
    expected = [canonical(catalog_query.search_models(snapshot, args))
                for _, args in cases]
    assert [response["canonical"] for response in first] == expected

    # Four REAL concurrent children, each carrying the same batch.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        runs = list(pool.map(lambda _: _run(node, batch), range(4)))
    assert len(runs) == 4
    for responses in runs:
        assert [response["id"] for response in responses] == \
            [request["id"] for request in batch]
        assert [response["canonical"] for response in responses] == expected

    # A larger single batch keeps response ids, order and cardinality exact.
    many = []
    for index in range(40):
        many.append(_tool_request(
            f"bulk-{index:02d}", "search_models",
            {"limit": 1 + index % 5, "offset": index},
            snapshot_text=text))
    responses = _run(node, many)
    assert [response["id"] for response in responses] == \
        [request["id"] for request in many]
    for index, response in enumerate(responses):
        result = catalog_query.search_models(
            snapshot, {"limit": 1 + index % 5, "offset": index})
        assert response["canonical"] == canonical(result), index


# ---------------------------------------------------------------------------
# G1 -- requiring/importing the runner is inert; only the CLI owns stdio
# ---------------------------------------------------------------------------


REQUIRE_DRIVER = "require(process.argv[1]);"
DYNAMIC_IMPORT_DRIVER = (
    'import { pathToFileURL } from "node:url";'
    " await import(pathToFileURL(process.argv[1]).href);")


def _import_run(node: str, driver: str, payload: bytes) -> ChildRun:
    """Load the real runner from another Node entry point and watch its stdio."""
    if driver is REQUIRE_DRIVER:
        argv = [node, "-e", driver, str(RUNNER)]
    else:
        argv = [node, "--input-type=module", "-e", driver, str(RUNNER)]
    return _bounded_child(argv, payload=payload, stdout_cap=CONTROL_CAP_BYTES,
                         stderr_cap=CONTROL_CAP_BYTES, timeout=60)


@pytest.mark.parametrize("driver", [REQUIRE_DRIVER, DYNAMIC_IMPORT_DRIVER])
def test_runner_import_is_inert_for_empty_and_valid_stdin(node, driver):
    """Loading the runner must not read stdin, write protocol stdout or fail.

    A require/import is inert: only command execution owns stdin/stdout. Both a
    real `require()` and a real dynamic `import()` of the same CommonJS file are
    exercised, each with an EMPTY stdin (the historical SyntaxError path) and
    with a perfectly valid protocol document that the module must not consume.
    """
    empty = _import_run(node, driver, b"")
    assert empty.failure is None, empty
    assert empty.returncode == 0, (
        "importing run-contract.cjs failed with exit "
        f"{empty.returncode}: {empty.stderr.decode('utf-8', 'replace')}")
    assert empty.stdout == b"", (
        "importing run-contract.cjs wrote to stdout: "
        f"{empty.stdout[:200]!r}")
    assert empty.stderr == b"", (
        "importing run-contract.cjs wrote a diagnostic: "
        f"{empty.stderr[:400]!r}")

    document = json.dumps(
        {"protocol": 1, "requests": [{"id": "inert", "op": "hello"}]}
    ).encode("utf-8")
    supplied = _import_run(node, driver, document)
    assert supplied.failure is None, supplied
    assert supplied.returncode == 0, supplied.stderr.decode("utf-8", "replace")
    assert supplied.stdout == b"", (
        "an import ran the protocol against the supplied stdin: "
        f"{supplied.stdout[:200]!r}")
    assert supplied.stderr == b"", supplied.stderr[:400]


def test_runner_import_returns_while_stdin_stays_open(node):
    """An import must not wait for stdin EOF: the pipe is never closed here."""
    process = subprocess.Popen(
        [node, "-e", REQUIRE_DRIVER, str(RUNNER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT), env=_child_env())
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    returncode: int | None = None
    try:
        try:
            # stdin is deliberately left open and empty for the whole wait.
            returncode = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "requiring run-contract.cjs blocked for 20s while stdin stayed "
                "open: the module reads stdin at load time instead of only when "
                "it is the command entry point")
        stdout = process.stdout.read()
        stderr = process.stderr.read()
    finally:
        _kill_and_reap(process)
        _close_pipes(process)
    assert returncode == 0, stderr.decode("utf-8", "replace")
    assert stdout == b"", stdout[:200]
    assert stderr == b"", stderr[:400]


def test_runner_command_line_entry_point_still_owns_stdio(node):
    """POSITIVE CONTROL: the actual CLI path must still run the protocol.

    A guard that suppressed the entry point everywhere would satisfy the
    inertness tests, so this control pins the visible behaviour of real command
    execution: one protocol document, requests echoed in order, and a nonzero
    exit with a stderr diagnostic for a malformed document.
    """
    requests = [{"id": "cli-1", "op": "hello"},
                {"id": "cli-2", "op": "canonical_vector",
                 "text": '{"b":1.0,"a":true}'}]
    payload = json.dumps({"protocol": 1, "requests": requests}).encode("utf-8")
    good = _bounded_child([node, str(RUNNER)], payload=payload,
                          stdout_cap=CONTROL_CAP_BYTES,
                          stderr_cap=CONTROL_CAP_BYTES, timeout=60)
    assert good.failure is None, good
    assert good.returncode == 0, good.stderr.decode("utf-8", "replace")
    assert good.stderr == b"", good.stderr[:400]
    document = json.loads(good.stdout.decode("utf-8"))
    assert document["protocol"] == 1
    assert [row["id"] for row in document["responses"]] == ["cli-1", "cli-2"]
    assert [row["op"] for row in document["responses"]] == ["hello",
                                                            "canonical_vector"]
    # An independent literal: keys canonicalized, the typed float token kept.
    assert document["responses"][1]["canonical"] == '{"a":true,"b":1.0}'
    assert document["responses"][0]["status"] == "ok"
    assert document["responses"][0]["protocol"] == 1

    malformed = _bounded_child([node, str(RUNNER)],
                               payload=b'{"protocol":1,"requests":[{"id":1}]}',
                               stdout_cap=CONTROL_CAP_BYTES,
                               stderr_cap=CONTROL_CAP_BYTES, timeout=60)
    assert malformed.failure is None, malformed
    assert malformed.returncode != 0
    assert malformed.stdout == b"", malformed.stdout[:200]
    assert b"run-contract.cjs" in malformed.stderr


# ---------------------------------------------------------------------------
# G2 -- the runner's stdin bound is enforced DURING the read
# ---------------------------------------------------------------------------


def _request_document(request_id: str = "pad") -> bytes:
    """The smallest valid protocol document for one `hello` request."""
    return json.dumps(
        {"protocol": 1, "requests": [{"id": request_id, "op": "hello"}]}
    ).encode("utf-8")


def _padded_to(total: int, *, request_id: str = "pad") -> bytes:
    """A valid protocol document padded with harmless spaces to EXACTLY `total`."""
    document = _request_document(request_id)
    assert total >= len(document), (total, len(document))
    return document + b" " * (total - len(document))


def test_runner_stdin_overflow_is_rejected_before_eof(node):
    """The 32 MiB bound must be enforced while reading, not after EOF.

    stdin is never closed: the child has to reject the first byte past the bound
    on its own. A read-everything-then-measure implementation instead blocks
    until EOF and consumes the whole oversized stream, which is what the
    `written < total` and timeout assertions catch.
    """
    prefix = _request_document("overflow")
    total = RUNNER_STDIN_BOUND_BYTES + (4 * 1024 * 1024)
    filler = b" " * (1024 * 1024)
    process = subprocess.Popen(
        [node, str(RUNNER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(REPO_ROOT), env=_child_env())
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    started = time.monotonic()
    written = 0
    returncode: int | None = None
    try:
        try:
            process.stdin.write(prefix)
            process.stdin.flush()
            while written < total:
                process.stdin.write(filler)
                process.stdin.flush()
                written += len(filler)
        except (BrokenPipeError, OSError):
            pass                      # the child closed stdin: exactly the point
        try:
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "run-contract.cjs still waited for stdin EOF after "
                f"{RUNNER_STDIN_BOUND_BYTES} bytes: the bound is checked on the "
                "accumulated document instead of during the read")
        elapsed = time.monotonic() - started
        stdout = process.stdout.read()
        stderr = process.stderr.read()
    finally:
        _kill_and_reap(process)
        _close_pipes(process)

    assert returncode != 0
    assert stdout == b"", stdout[:200]
    assert b"stdin exceeds" in stderr, stderr[:400]
    assert written < total, (
        "the runner consumed the entire oversized stream, so the bound was "
        "applied after EOF rather than to the first over-limit byte")
    assert elapsed < 30
    match = re.search(
        rb"rejected at (\d+) bytes after (\d+) bounded reads of at most (\d+)",
        stderr)
    assert match, stderr[:400]
    rejected_at, reads, read_size = (int(group) for group in match.groups())
    assert rejected_at > RUNNER_STDIN_BOUND_BYTES
    # One bounded detection read, not an arbitrarily oversized buffer.
    assert rejected_at <= RUNNER_STDIN_BOUND_BYTES + (1024 * 1024)
    assert read_size <= 1024 * 1024
    assert reads >= RUNNER_STDIN_BOUND_BYTES // read_size


def test_runner_accepts_exact_limit_payload_and_split_utf8(node):
    """Positive controls: the exact bound is accepted and UTF-8 survives chunking."""
    exact = _padded_to(RUNNER_STDIN_BOUND_BYTES)
    assert len(exact) == RUNNER_STDIN_BOUND_BYTES
    run = _bounded_child([node, str(RUNNER)], payload=exact, timeout=120)
    assert run.failure is None, run
    assert run.returncode == 0, run.stderr.decode("utf-8", "replace")
    assert run.stderr == b"", run.stderr[:400]
    assert [row["id"] for row in json.loads(run.stdout)["responses"]] == ["pad"]

    # A three-byte character placed so that it straddles the runner's fixed
    # 64 KiB read boundary: a decoder applied per chunk would corrupt it.
    inner = '{"k":"\u20ac\u20ac"}'
    body = json.dumps({"protocol": 1, "requests": [
        {"id": "utf8", "op": "canonical_vector", "text": inner}]},
        ensure_ascii=False).encode("utf-8")
    marker = "\u20ac".encode("utf-8")
    start = body.index(marker)
    padding = (RUNNER_READ_CHUNK_BYTES - 1) - start
    assert padding > 0
    assert start + padding == RUNNER_READ_CHUNK_BYTES - 1
    split = _bounded_child([node, str(RUNNER)], payload=b" " * padding + body,
                           timeout=60)
    assert split.failure is None, split
    assert split.returncode == 0, split.stderr.decode("utf-8", "replace")
    row = json.loads(split.stdout)["responses"][0]
    expected = canonical(json.loads(inner))
    assert row["canonical"] == expected
    assert row["sha256"] == hashlib.sha256(expected.encode("ascii")).hexdigest()

    # The same claim over a payload whose multibyte text spans many chunks.
    wide_inner = json.dumps({"k": "\u00e9\u20ac\u4e2d" * 60000},
                            ensure_ascii=False)
    wide = json.dumps({"protocol": 1, "requests": [
        {"id": "wide", "op": "canonical_vector", "text": wide_inner}]},
        ensure_ascii=False).encode("utf-8")
    assert len(wide) > 4 * RUNNER_READ_CHUNK_BYTES
    wide_run = _bounded_child([node, str(RUNNER)], payload=wide, timeout=120)
    assert wide_run.failure is None, wide_run
    assert wide_run.returncode == 0, wide_run.stderr.decode("utf-8", "replace")
    wide_row = json.loads(wide_run.stdout)["responses"][0]
    wide_expected = canonical(json.loads(wide_inner))
    assert wide_row["canonical"] == wide_expected


def test_runner_rejects_one_byte_over_bound_on_closed_pipe(node):
    """CONTROL (passes before and after the fix): the bound itself is unchanged."""
    over = _padded_to(RUNNER_STDIN_BOUND_BYTES + 1)
    assert len(over) == RUNNER_STDIN_BOUND_BYTES + 1
    run = _bounded_child([node, str(RUNNER)], payload=over, timeout=120)
    assert run.failure is None, run
    assert run.returncode != 0
    assert run.stdout == b"", run.stdout[:200]
    assert b"stdin exceeds" in run.stderr


# ---------------------------------------------------------------------------
# G2 -- the wrapped child transport: independent caps and lifecycle
# ---------------------------------------------------------------------------


def test_bounded_child_keeps_exact_bytes_and_native_status(node):
    """CONTROL: a child inside its caps keeps exact bytes and its own exit code."""
    code = ('process.stdout.write("out-bytes");'
            'process.stderr.write("err-bytes");'
            'process.exit(3);')
    run = _bounded_child([node, "-e", code], stdout_cap=CONTROL_CAP_BYTES,
                         stderr_cap=CONTROL_CAP_BYTES, timeout=60)
    assert run.failure is None, run
    assert run.returncode == 3
    assert run.stdout == b"out-bytes"
    assert run.stderr == b"err-bytes"


def test_bounded_child_stdout_flood_stops_before_producer_eof(node):
    """stdout is capped during the read; the still-running producer is killed."""
    cap = CONTROL_CAP_BYTES
    code = ("const cap = Number(process.argv[1]);"
            "const chunk = Buffer.alloc(65536, 0x61);"
            "for (let written = 0; written <= cap + 262144; ) {"
            "  process.stdout.write(chunk); written += chunk.length; }"
            'setTimeout(() => { process.stdout.write("TAIL-SENTINEL");'
            " process.exit(0); }, 30000);")
    run = _bounded_child([node, "-e", code, str(cap)], stdout_cap=cap,
                         stderr_cap=cap, timeout=60)
    assert run.failure == "output-limit"
    assert len(run.stdout) <= cap, "the capture grew past its cap"
    assert b"TAIL-SENTINEL" not in run.stdout
    assert run.elapsed_s < 20, "the overflow waited for the producer to finish"
    assert run.returncode is not None
    assert run.returncode < 0, (
        "the producer exited on its own instead of being terminated: "
        f"returncode {run.returncode}")


def test_bounded_child_stderr_flood_has_its_own_cap(node):
    """stderr is bounded independently, so a quiet stdout stays intact."""
    cap = CONTROL_CAP_BYTES
    code = ("const cap = Number(process.argv[1]);"
            'process.stdout.write("ok");'
            "const chunk = Buffer.alloc(65536, 0x62);"
            "for (let written = 0; written <= cap + 262144; ) {"
            "  process.stderr.write(chunk); written += chunk.length; }"
            "setTimeout(() => { process.exit(0); }, 30000);")
    run = _bounded_child([node, "-e", code, str(cap)], stdout_cap=cap,
                         stderr_cap=cap, timeout=60)
    assert run.failure == "output-limit"
    assert run.stdout == b"ok"
    assert len(run.stderr) <= cap, "the capture grew past its cap"
    assert run.returncode is not None and run.returncode < 0


def test_bounded_child_timeout_kills_and_reaps(node):
    """A child that never exits is killed, reaped and reported as a timeout."""
    code = "setInterval(() => {}, 1000);"
    run = _bounded_child([node, "-e", code], stdout_cap=CONTROL_CAP_BYTES,
                         stderr_cap=CONTROL_CAP_BYTES, timeout=1.0)
    assert run.failure == "timeout"
    assert run.elapsed_s < 20
    assert run.returncode is not None, "the timed-out child was not reaped"
    assert run.returncode < 0


def test_bounded_child_simultaneous_io_does_not_deadlock(node):
    """Output emitted before stdin is consumed must not deadlock the transport."""
    size = 512 * 1024
    code = ("const size = Number(process.argv[1]);"
            "const out = Buffer.alloc(size, 0x61);"
            "const err = Buffer.alloc(size, 0x62);"
            "process.stdout.write(out, () => {"
            "  process.stderr.write(err, () => { process.exit(0); });"
            "});")
    payload = b"z" * size
    run = _bounded_child([node, "-e", code, str(size)], payload=payload,
                         stdout_cap=8 * 1024 * 1024,
                         stderr_cap=8 * 1024 * 1024, timeout=60)
    assert run.failure is None, run
    assert run.returncode == 0
    assert len(run.stdout) == size
    assert len(run.stderr) == size
    assert run.stdout == b"a" * size
    assert run.stderr == b"b" * size
