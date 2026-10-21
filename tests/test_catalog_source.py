"""Runtime tests for the validated local-FILE and bounded HTTP sources.

This module exercises only the small source/cache boundary:

    watchdog_catalog.source.MAX_SOURCE_BYTES
    watchdog_catalog.source.DEFAULT_CATALOG_URL
    watchdog_catalog.source.DEFAULT_TIMEOUT_S
    watchdog_catalog.source.SourceError
    watchdog_catalog.source.load_catalog_file   (unit 5a)
    watchdog_catalog.source.load_catalog_url    (unit 5b)
    watchdog_catalog.source.DiskCache           (unit 5c)
    watchdog_catalog.source.fetch_catalog_url   (unit 5d)
    watchdog_catalog.source.CatalogSource       (unit 5e)
    watchdog_catalog.source.with_source         (unit 5e)

`load_catalog_file` is one explicit, byte-bounded binary read of an
operator-selected path whose bytes are decoded as strict UTF-8 and then handed
EXACTLY ONCE to the accepted ``watchdog_catalog.catalog.load_snapshot``
validator. `load_catalog_url` is one explicit, byte-bounded, credential-free
HTTP GET of an operator-selected URL, with the same single delegation. The
accepted validator -- not these layers -- owns JSON, schema and hash truth, so
a malformed document keeps its frozen core ``CatalogError`` code/message. Only
transport-shaped faults (open/read/close failure, oversize, invalid
path/URL/byte-limit/timeout configuration) become the fixed, non-reflective
``SourceError`` with code ``source_unavailable``.

Two unparametrized local-file behavioural groups cover accepted scratch-file
loading and stable failures/import isolation. Two further unparametrized HTTP
groups cover one accepted 200 document (framing, byte bounds, defaults through
a network-free double) and every non-200 / malformed / timed-out / disconnected
/ misconfigured case. Every socket is a numeric-loopback stdlib server on an
ephemeral port; no test resolves a public host, and the production default URL
is only exercised through a request/response double.

``DiskCache`` (unit 5c) is implemented and exercised here. ``fetch_catalog_url``
(unit 5d) is ONE synchronous conditional revalidation of ONE literal URL: it
may send a single ``If-None-Match`` (preferred) or ``If-Modified-Since`` header
derived only from a detached, fully re-validated same-source candidate entry,
and it accepts a 304 only when it sent such a header. Two further
unparametrized groups cover that primitive: the accepted 200/304/changed-200
flow, validator capture and rejection, candidate detachment and DiskCache
compatibility; then every invalid candidate, unsolicited or contradictory 304,
unread 304 body, framing fault and fallback-free failure.

``CatalogSource`` and ``with_source`` (unit 5e) are implemented and exercised
here too. ``CatalogSource`` is the synchronous acquisition/state layer over
those accepted primitives: ONE selected url/file source, at most one validated
last-good entry in memory (bootstrapped ONCE from its source-scoped
``DiskCache``) and exactly ONE explicit refresh attempt per call. A successful
refresh carries ``ok``/``snapshot``/``source``; a failed one returns the
retained last-good body, validators and its ORIGINAL retrieval time with
``refresh_failed``/``cache_fallback`` warnings, or the frozen ``SourceError``
envelope when nothing valid was ever captured -- never an invented empty
success. Retrieval time (``fetched_at``/``fetch_age_s``/``not_modified``) is
kept separate from observation freshness, which is derived only from the
snapshot's own ``tick_epoch``/``cadence_s``. ``current`` is a memory-only view
and ``with_source`` attaches that provenance to a pure query payload without
querying or mutating it. Two further unparametrized groups cover the
200/304/changed-body/fallback/freshness flow with detached views, warning
propagation and source isolation; then every failure, corrupt-record, invalid
clock/configuration and zero-I/O path. Scheduling, coalesced refresh,
retries/backoff and async dispatch belong to later units, as does any CLI
wiring -- none is implemented, imported or exercised here. Nothing in this
module writes outside pytest's own scratch directory.
"""

import copy
import hashlib
import http.client
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError as _HTTPError

import pytest

from watchdog_catalog import catalog, query, source

REPO = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO / "tests" / "fixtures" / "catalog"

#: Representative committed accepted snapshots: a plain two-provider document,
#: a deliberately non-alphabetical ordering document (astral/fullwidth/Unicode
#: ids included) and the optional-metadata edge document (null/zero/typed
#: pricing rows).
ACCEPTED_FIXTURES = ("basic.json", "ordering.json", "metadata-edge-cases.json")

SOURCE_CODE = "source_unavailable"
SOURCE_MESSAGE = "Catalogue source unavailable."
PATH_TYPE_ERROR = (
    "load_catalog_file() requires a filesystem path string or "
    "PathLike returning str")
BYTE_LIMIT_ERROR = (
    "load_catalog_file() requires a positive integer byte limit")

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


def _canonical_text(body):
    """Independent stdlib canonical text (json/hashlib only), used ONLY to
    reseal a document this test constructs; no production encoder is called."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _reseal(snap):
    body = {key: value for key, value in snap.items()
            if key != "snapshot_id"}
    snap["snapshot_id"] = hashlib.sha256(_canonical_text(body)).hexdigest()
    return snap


def _fixed_source_error(call, forbidden_texts):
    """Assert one call raises ONLY the fixed SourceError envelope and return
    the error. Every retelling of the failure is non-reflective and the payload
    is fresh on every call."""
    with pytest.raises(source.SourceError) as excinfo:
        call()
    error = excinfo.value
    assert error.code == SOURCE_CODE
    assert error.message == SOURCE_MESSAGE
    assert str(error) == SOURCE_MESSAGE
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    expected = {"ok": False,
                "error": {"code": SOURCE_CODE, "message": SOURCE_MESSAGE}}
    payload = error.to_payload()
    assert payload == expected
    payload["error"]["code"] = "mutated"
    payload["ok"] = True
    assert error.to_payload() == expected
    for text in forbidden_texts:
        assert text not in str(error)
        assert text not in repr(error.to_payload())
    return error


def test_catalog_file_source_reads_one_explicit_bounded_file(
        tmp_path, monkeypatch):
    assert source.MAX_SOURCE_BYTES == 8 * 1024 * 1024

    # -- (a) committed accepted fixtures, copied BYTE-FOR-BYTE to scratch -----
    snapshots = {}
    for name in ACCEPTED_FIXTURES:
        raw = (FIXTURE_DIR / name).read_bytes()
        target = tmp_path / name
        target.write_bytes(raw)
        before_names = sorted(entry.name for entry in tmp_path.iterdir())
        snapshot = source.load_catalog_file(str(target))
        expected = json.loads(raw.decode("utf-8"))
        assert snapshot == expected
        assert snapshot["snapshot_id"] == expected["snapshot_id"]
        assert catalog.snapshot_hash(snapshot) == expected["snapshot_id"]
        # The file is untouched and no sibling was created or selected.
        assert target.read_bytes() == raw
        assert sorted(entry.name for entry in tmp_path.iterdir()) == \
            before_names
        snapshots[name] = snapshot

    ordering = snapshots["ordering.json"]
    assert ordering["provider_order"] == [
        "synthetic-b", "synthetic-a", "synthetic-c"]
    assert ordering["providers"]["synthetic-b"]["model_ids"][:3] == [
        "vendor/item02", "vendor/item1", "vendor/item10"]

    metadata = snapshots["metadata-edge-cases.json"]["providers"][
        "synthetic-a"]["models"]
    known = metadata["vendor/known-context"]
    assert known["context_length"] == 65536
    assert type(known["context_length"]) is int
    assert known["pricing"] == {"prompt": "0", "completion": 0}
    assert type(known["pricing"]["prompt"]) is str
    assert type(known["pricing"]["completion"]) is int
    unknown = metadata["vendor/unknown-context"]
    assert unknown["context_length"] is None
    assert unknown["pricing"] is None
    zero = metadata["vendor/zero-context"]
    assert zero["context_length"] == 0
    assert type(zero["context_length"]) is int
    assert zero["pricing"] == {"prompt": None, "completion": "0"}

    # -- (b) a LEGITIMATELY empty catalogue is a success, not a failure -------
    empty_doc = json.loads((FIXTURE_DIR / "basic.json").read_text("utf-8"))
    empty_doc["provider_order"] = []
    empty_doc["providers"] = {}
    _reseal(empty_doc)
    empty_path = tmp_path / "empty.json"
    empty_path.write_text(json.dumps(empty_doc, indent=2), encoding="utf-8")
    empty = source.load_catalog_file(str(empty_path))
    assert empty["provider_order"] == []
    assert empty["providers"] == {}
    assert empty["snapshot_id"] == empty_doc["snapshot_id"] == \
        catalog.snapshot_hash(empty)

    # -- (c) exactly ONE delegation of the decoded text, result forwarded -----
    calls, forwarded = [], []
    accepted_load_snapshot = catalog.load_snapshot

    def spy(text):
        calls.append(text)
        result = accepted_load_snapshot(text)
        forwarded.append(result)
        return result

    monkeypatch.setattr(catalog, "load_snapshot", spy)
    basic_path = tmp_path / "basic.json"
    snapshot = source.load_catalog_file(basic_path)  # pathlib.Path accepted
    monkeypatch.undo()
    assert len(calls) == 1
    assert calls[0] == basic_path.read_text(encoding="utf-8")
    assert snapshot is forwarded[0]

    # -- (d) str / Path / custom text PathLike, path consumed LITERALLY -------
    opened = []
    builtin_open = open

    def tracing_open(file, *args, **kwargs):
        opened.append((file, args, kwargs))
        return builtin_open(file, *args, **kwargs)

    monkeypatch.setattr(source, "open", tracing_open, raising=False)

    class LiteralPath:
        def __fspath__(self):
            return str(tmp_path) + "/./basic.json"

    literal = str(tmp_path) + "/./basic.json"
    for selected in (str(basic_path), basic_path, LiteralPath()):
        del opened[:]
        loaded = source.load_catalog_file(selected)
        assert loaded["snapshot_id"] == snapshots["basic.json"]["snapshot_id"]
        assert len(opened) == 1
        path_arg, args, kwargs = opened[0]
        expected_arg = selected if isinstance(selected, str) else \
            os.fspath(selected)
        assert path_arg == expected_arg
        assert args == ("rb",) and kwargs == {}
    # No trimming, resolution or rewriting: the literal text reached open.
    assert path_arg == literal
    assert str(tmp_path) in path_arg and "/./basic.json" in path_arg

    # -- (e) byte bounds: exact limit accepted, limit plus one rejected -------
    basic_raw = (FIXTURE_DIR / "basic.json").read_bytes()
    exact_path = tmp_path / "exact.json"
    exact_path.write_bytes(basic_raw)
    assert source.load_catalog_file(
        str(exact_path), max_bytes=len(basic_raw))["snapshot_id"] == \
        snapshots["basic.json"]["snapshot_id"]
    _fixed_source_error(
        lambda: source.load_catalog_file(
            str(exact_path), max_bytes=len(basic_raw) - 1),
        (str(tmp_path), "exact.json", basic_raw[:16].decode("utf-8", "replace")))

    # A multibyte control: the bound counts BYTES, never decoded characters.
    multibyte = json.loads(basic_raw.decode("utf-8"))
    alpha = multibyte["providers"]["synthetic-a"]
    alpha["display_name"] = "Café 東京"
    alpha["models"]["alpha-1:free"]["name"] = "Alpha Ünïcode 東京"
    _reseal(multibyte)
    multibyte_raw = json.dumps(
        multibyte, indent=2, ensure_ascii=False).encode("utf-8")
    character_count = len(multibyte_raw.decode("utf-8"))
    assert len(multibyte_raw) > character_count
    multibyte_path = tmp_path / "multibyte.json"
    multibyte_path.write_bytes(multibyte_raw)
    _fixed_source_error(
        lambda: source.load_catalog_file(
            str(multibyte_path), max_bytes=character_count),
        (str(tmp_path), "multibyte.json", "Café 東京"))
    loaded = source.load_catalog_file(
        str(multibyte_path), max_bytes=len(multibyte_raw))
    assert loaded["providers"]["synthetic-a"]["display_name"] == "Café 東京"
    assert loaded["providers"]["synthetic-a"]["models"][
        "alpha-1:free"]["name"] == "Alpha Ünïcode 東京"
    assert loaded["snapshot_id"] == multibyte["snapshot_id"]


def test_catalog_source_failures_types_and_imports_stay_stable(
        tmp_path, monkeypatch):
    raw = (FIXTURE_DIR / "basic.json").read_bytes()
    target = tmp_path / "basic.json"
    target.write_bytes(raw)
    directory = tmp_path / "catalogue-dir"
    directory.mkdir()

    # -- (a) transport-shaped faults collapse to ONE fixed SourceError --------
    class _FaultHandle:
        def __init__(self, read_error=None, close_error=None):
            self._read_error = read_error
            self._close_error = close_error

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            if self._close_error is not None:
                raise self._close_error
            return False

        def read(self, size):
            assert size == source.MAX_SOURCE_BYTES + 1
            if self._read_error is not None:
                raise self._read_error
            return raw

    def faulting_open(handle):
        def _open(file, *args, **kwargs):
            return handle
        return _open

    def permission_open(file, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(file))

    cases = (
        ("missing file", lambda: source.load_catalog_file(
            str(tmp_path / "absent.json"))),
        ("directory", lambda: source.load_catalog_file(str(directory))),
        ("invalid filesystem string", lambda: source.load_catalog_file(
            str(tmp_path / "nul") + "\x00basic.json")),
        ("permission denied", lambda: source.load_catalog_file(str(target))),
        ("read failure", lambda: source.load_catalog_file(str(target))),
        ("close failure", lambda: source.load_catalog_file(str(target))),
    )
    fault_opens = {
        "permission denied": permission_open,
        "read failure": faulting_open(_FaultHandle(read_error=OSError(5, "I/O error"))),
        "close failure": faulting_open(_FaultHandle(close_error=OSError(5, "close failed"))),
    }
    for label, call in cases:
        monkeypatch.undo()
        if label in fault_opens:
            monkeypatch.setattr(source, "open", fault_opens[label],
                                raising=False)
        _fixed_source_error(call, (str(tmp_path), "basic.json", "absent.json",
                                   "Permission denied", "I/O error",
                                   "close failed", "\x00"))
    monkeypatch.undo()

    # An oversized file is the same fixed failure, never a truncated snapshot.
    _fixed_source_error(
        lambda: source.load_catalog_file(str(target), max_bytes=len(raw) - 1),
        (str(tmp_path), "basic.json", raw[:16].decode("utf-8", "replace")))

    # -- (b) invalid path / byte-limit TYPES fail before any open -------------
    opened = []

    def forbidden_open(*args, **kwargs):
        opened.append((args, kwargs))
        raise AssertionError("invalid input must never reach the filesystem")

    monkeypatch.setattr(source, "open", forbidden_open, raising=False)

    class BytesPath:
        def __fspath__(self):
            return b"/tmp/catalogue.json"

    class IntPath:
        def __fspath__(self):
            return 7

    bad_paths = (b"/tmp/catalogue.json", bytearray(b"/tmp/x"), 3, True,
                 None, 3.5, object(), BytesPath(), IntPath())
    for bad in bad_paths:
        with pytest.raises(TypeError) as excinfo:
            source.load_catalog_file(bad)
        assert type(excinfo.value) is TypeError
        assert str(excinfo.value) == PATH_TYPE_ERROR
        assert excinfo.value.__cause__ is None
    for bad_limit in (0, -1, True, 1.0, "8388608", None):
        with pytest.raises(ValueError) as excinfo:
            source.load_catalog_file(str(target), max_bytes=bad_limit)
        assert type(excinfo.value) is ValueError
        assert str(excinfo.value) == BYTE_LIMIT_ERROR
    assert opened == []
    monkeypatch.undo()

    # -- (c) accepted core errors survive the file boundary untouched ---------
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b'{"schema_version": 1, "snapshot_id": "\xff\xfe"}')
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + raw)
    malformed = tmp_path / "malformed.json"
    malformed.write_bytes(b'{"schema_version": 1, "snapshot_id": ')
    tampered = json.loads(raw.decode("utf-8"))
    tampered["snapshot_id"] = "0" * 64
    tampered_path = tmp_path / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    future_version = json.loads(raw.decode("utf-8"))
    future_version["schema_version"] = 2
    future_path = tmp_path / "future-schema.json"
    future_path.write_text(json.dumps(future_version), encoding="utf-8")

    core_cases = (
        (invalid_utf8, "invalid_json", "Invalid catalogue JSON."),
        (bom, "invalid_json", "Invalid catalogue JSON."),
        (malformed, "invalid_json", "Invalid catalogue JSON."),
        (tampered_path, "invalid_snapshot", "Invalid catalogue snapshot."),
        (future_path, "unsupported_schema_version",
         "Unsupported catalogue schema version."),
    )
    for path, code, message in core_cases:
        with pytest.raises(catalog.CatalogError) as excinfo:
            source.load_catalog_file(str(path))
        error = excinfo.value
        assert not isinstance(error, source.SourceError)
        assert (error.code, error.message, str(error)) == (
            code, message, message)
        assert error.to_payload() == {
            "ok": False, "error": {"code": code, "message": message}}
        assert str(tmp_path) not in str(error)
        assert path.name not in str(error)
        assert "codec" not in str(error)
        assert "\xff" not in str(error)

    # -- (d) fresh-process import isolation (neutral cwd, empty HOME) ---------
    home = tmp_path / "home"
    home.mkdir()
    neutral = tmp_path / "neutral-cwd"
    neutral.mkdir()
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "PYTHONPATH": str(REPO),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    plain = subprocess.run(
        [sys.executable, "-B", "-c",
         "import watchdog_catalog, watchdog_catalog.source"],
        cwd=str(neutral), env=base_env, capture_output=True, text=True,
        timeout=120)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == "" and plain.stderr == ""

    probe_env = dict(base_env)
    probe_env["WC_FORBIDDEN"] = json.dumps(FORBIDDEN_IMPORTS)
    probe_env["WC_DATA"] = json.dumps(DATA_PATTERNS)
    probe = subprocess.run(
        [sys.executable, "-B", "-c", _IMPORT_PROBE],
        cwd=str(neutral), env=probe_env, capture_output=True, text=True,
        timeout=120)
    assert probe.returncode == 0, probe.stderr
    assert probe.stderr == "", probe.stderr
    report = json.loads(probe.stdout)
    assert report["forbidden_imports"] == []
    assert report["leaked_modules"] == []
    assert report["data_opens"] == []
    assert report["network"] == []
    # Importing the inert package does not pull the file source in.
    assert "watchdog_catalog" in report["package_only"]
    assert "watchdog_catalog.source" not in report["package_only"]


_IMPORT_PROBE = r'''
import importlib
import json
import os
import sys

forbidden = json.loads(os.environ["WC_FORBIDDEN"])
patterns = json.loads(os.environ["WC_DATA"])
report = {"forbidden_imports": [], "leaked_modules": [], "data_opens": [],
          "network": [], "package_only": []}


def _audit(event, args):
    if event == "import":
        name = args[0]
        for prefix in forbidden:
            if name == prefix or name.startswith(prefix + "."):
                report["forbidden_imports"].append(name)
    elif event == "open":
        path = args[0]
        if isinstance(path, str):
            norm = path.replace("\\", "/")
            for pattern in patterns:
                if pattern in norm:
                    report["data_opens"].append(norm)
    elif event.startswith("socket."):
        report["network"].append(event)


# The package import is deliberately OUTSIDE the audited window: only the
# explicit source import may add side effects.
importlib.import_module("watchdog_catalog")
report["package_only"] = sorted(
    name for name in sys.modules if name.startswith("watchdog_catalog"))

sys.addaudithook(_audit)
importlib.import_module("watchdog_catalog.source")
report["leaked_modules"] = [
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".")
           for prefix in forbidden)]
sys.stdout.write(json.dumps(report))
'''


# ---------------------------------------------------------------------------
# Unit 5b: one bounded, credential-free HTTP 200 catalogue document
# ---------------------------------------------------------------------------
#
# The two behavioral groups below cover ONE synchronous GET of ONE explicit
# URL. Every socket is a numeric-loopback stdlib server on an ephemeral port;
# nothing here resolves a public host, and the published default URL is only
# ever exercised through a network-free ``OpenerDirector.open`` double.

ACCEPT_HEADER = "application/json"
ENCODING_HEADER = "identity"
URL_TYPE_ERROR = "load_catalog_url() requires an http or https URL string"
URL_VALUE_ERROR = (
    "load_catalog_url() requires an absolute http or https URL with a host")
TIMEOUT_ERROR = (
    "load_catalog_url() requires a finite positive timeout in seconds")
HTTP_BYTE_LIMIT_ERROR = (
    "load_catalog_url() requires a positive integer byte limit")


def _headers_message(pairs=()):
    """A minimal read-only header mapping with ``email.message`` semantics."""
    class _Headers:
        def __init__(self, items):
            self._items = tuple(items)

        def get_all(self, name):
            found = [value for key, value in self._items
                     if key.lower() == name.lower()]
            return found or None

        def get(self, name, default=None):
            found = self.get_all(name)
            return found[0] if found else default

        def items(self):
            return list(self._items)

    return _Headers(pairs)


class _FakeResponse:
    """A scripted response double: no socket, recorded reads, close counter."""

    def __init__(self, body, status=200, headers=(), read_error=None,
                 close_error=None):
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = _headers_message(headers)
        self._read_error = read_error
        self._close_error = close_error
        self.closed = 0
        self.reads = []

    def getcode(self):
        return self.status

    def read(self, amt=None):
        self.reads.append(amt)
        if self._read_error is not None:
            raise self._read_error
        if amt is None:
            chunk = self._body[self._offset:]
        else:
            chunk = self._body[self._offset:self._offset + amt]
        self._offset += len(chunk)
        return chunk

    def close(self):
        self.closed += 1
        if self._close_error is not None:
            raise self._close_error


def _write_response(handler, status, body, headers, length, chunked):
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    if chunked:
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        for start in range(0, len(body), 1024):
            piece = body[start:start + 1024]
            handler.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
        handler.wfile.write(b"0\r\n\r\n")
        handler.close_connection = True
        return
    if length and status not in (204, 304):
        handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if status not in (204, 304) and body:
        handler.wfile.write(body)
    handler.close_connection = True


class _LoopbackServer:
    """One stdlib loopback HTTP server with scripted, per-test responses."""

    def __init__(self):
        self.requests = []
        self.hits = {}
        self.release = threading.Event()
        self._responder = None
        self._server = None
        self._thread = None

    def start(self):
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 5

            def log_message(self, *args):
                pass

            def log_error(self, *args):
                pass

            def do_GET(self):
                owner.hits[self.path] = owner.hits.get(self.path, 0) + 1
                owner.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                })
                owner._responder(self)

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            block_on_close = False

            def handle_error(self, request, client_address):
                pass

        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="catalogue-loopback", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5.0)
            assert not self._thread.is_alive()

    @property
    def port(self):
        return self._server.server_address[1]

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def serve(self, body=b"", status=200, headers=(), length=True,
              chunked=False):
        self._responder = lambda handler: _write_response(
            handler, status, body, headers, length, chunked)

    def serve_stall(self, body, declared):
        def respond(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(declared))
            handler.end_headers()
            handler.wfile.write(body)
            handler.wfile.flush()
            self.release.wait(3.0)
            handler.close_connection = True
        self._responder = respond

    def serve_disconnect(self, body, declared):
        def respond(handler):
            handler.send_response(200)
            handler.send_header("Content-Length", str(declared))
            handler.end_headers()
            handler.wfile.write(body)
            handler.wfile.flush()
            handler.close_connection = True
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            handler.connection.close()
        self._responder = respond


@pytest.fixture
def loopback():
    server = _LoopbackServer().start()
    try:
        yield server
    finally:
        server.stop()


def _sent_headers(request):
    return {name.lower(): value for name, value in request["headers"].items()}


def test_catalog_url_source_loads_one_bounded_http_document(
        loopback, tmp_path, monkeypatch):
    # -- (a) the published defaults are fixed literals ------------------------
    assert source.DEFAULT_CATALOG_URL == (
        "https://raw.githubusercontent.com/Dthen/free-inference-watchdog/"
        "master/site/catalog.json")
    assert source.DEFAULT_TIMEOUT_S == 10
    assert source.MAX_SOURCE_BYTES == 8 * 1024 * 1024

    basic_raw = (FIXTURE_DIR / "basic.json").read_bytes()
    basic_doc = json.loads(basic_raw.decode("utf-8"))

    # -- (b) ONE literal GET of a 200 document; accepted snapshot and hash ----
    loopback.serve(basic_raw)
    url = loopback.url("/catalog.json")
    snapshot = source.load_catalog_url(url)
    assert snapshot == basic_doc
    assert snapshot["snapshot_id"] == basic_doc["snapshot_id"]
    assert catalog.snapshot_hash(snapshot) == basic_doc["snapshot_id"]
    assert len(loopback.requests) == 1
    request = loopback.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/catalog.json"
    sent = _sent_headers(request)
    assert sent["accept"] == ACCEPT_HEADER
    assert sent["accept-encoding"] == ENCODING_HEADER
    for credential in ("authorization", "cookie", "proxy-authorization"):
        assert credential not in sent

    # -- (c) the selected URL reaches the request literally -------------------
    loopback.serve(basic_raw)
    literal = "/catalog.json?source=./literal&mirror=none"
    assert source.load_catalog_url(loopback.url(literal))["snapshot_id"] == \
        basic_doc["snapshot_id"]
    assert loopback.requests[-1]["path"] == literal

    # -- (d) a legitimately EMPTY catalogue is a 200 success ------------------
    empty_doc = json.loads(basic_raw.decode("utf-8"))
    empty_doc["provider_order"] = []
    empty_doc["providers"] = {}
    _reseal(empty_doc)
    loopback.serve(json.dumps(empty_doc, indent=2).encode("utf-8"))
    empty = source.load_catalog_url(loopback.url("/empty.json"))
    assert empty["provider_order"] == [] and empty["providers"] == {}
    assert empty["snapshot_id"] == empty_doc["snapshot_id"] == \
        catalog.snapshot_hash(empty)

    # -- (e) typed optional metadata survives the HTTP boundary ---------------
    loopback.serve((FIXTURE_DIR / "metadata-edge-cases.json").read_bytes())
    metadata = source.load_catalog_url(
        loopback.url("/metadata.json"))["providers"]["synthetic-a"]["models"]
    known = metadata["vendor/known-context"]
    assert known["context_length"] == 65536
    assert type(known["context_length"]) is int
    assert known["pricing"] == {"prompt": "0", "completion": 0}
    assert type(known["pricing"]["prompt"]) is str
    assert type(known["pricing"]["completion"]) is int
    assert metadata["vendor/unknown-context"]["context_length"] is None
    assert metadata["vendor/unknown-context"]["pricing"] is None
    assert metadata["vendor/zero-context"]["context_length"] == 0

    # -- (f) exactly ONE core delegation; the returned object is forwarded ----
    calls, forwarded = [], []
    accepted_load_snapshot = catalog.load_snapshot

    def spy(text):
        calls.append(text)
        result = accepted_load_snapshot(text)
        forwarded.append(result)
        return result

    loopback.serve(basic_raw)
    monkeypatch.setattr(catalog, "load_snapshot", spy)
    delegated = source.load_catalog_url(loopback.url("/delegated.json"))
    monkeypatch.undo()
    assert len(calls) == 1
    assert calls[0] == basic_raw.decode("utf-8")
    assert delegated is forwarded[0]

    # -- (g) byte bounds count RAW BYTES, exactly ----------------------------
    loopback.serve(basic_raw)
    bounded = loopback.url("/bounded.json")
    assert source.load_catalog_url(
        bounded, max_bytes=len(basic_raw))["snapshot_id"] == \
        basic_doc["snapshot_id"]
    _fixed_source_error(
        lambda: source.load_catalog_url(
            bounded, max_bytes=len(basic_raw) - 1),
        ("127.0.0.1", "bounded.json",
         basic_raw[:16].decode("utf-8", "replace")))

    multibyte = json.loads(basic_raw.decode("utf-8"))
    alpha = multibyte["providers"]["synthetic-a"]
    alpha["display_name"] = "Café 東京"
    alpha["models"]["alpha-1:free"]["name"] = "Alpha Ünïcode 東京"
    _reseal(multibyte)
    multibyte_raw = json.dumps(
        multibyte, indent=2, ensure_ascii=False).encode("utf-8")
    character_count = len(multibyte_raw.decode("utf-8"))
    assert len(multibyte_raw) > character_count
    loopback.serve(multibyte_raw)
    _fixed_source_error(
        lambda: source.load_catalog_url(
            loopback.url("/multibyte.json"), max_bytes=character_count),
        ("127.0.0.1", "multibyte.json", "Café 東京"))
    loaded = source.load_catalog_url(
        loopback.url("/multibyte.json"), max_bytes=len(multibyte_raw))
    assert loaded["providers"]["synthetic-a"]["display_name"] == "Café 東京"
    assert loaded["snapshot_id"] == multibyte["snapshot_id"]

    # -- (h) framing variants: no Content-Length, chunked, identity encoding --
    loopback.serve(basic_raw, length=False)
    assert source.load_catalog_url(
        loopback.url("/no-length"))["snapshot_id"] == \
        basic_doc["snapshot_id"]
    loopback.serve(basic_raw, chunked=True)
    assert source.load_catalog_url(
        loopback.url("/chunked"))["snapshot_id"] == basic_doc["snapshot_id"]
    loopback.serve(basic_raw, headers=[("Content-Encoding", "identity")])
    assert source.load_catalog_url(
        loopback.url("/identity"))["snapshot_id"] == basic_doc["snapshot_id"]
    # a non-JSON Content-Type is NOT a reason to reject a valid document
    loopback.serve(basic_raw,
                   headers=[("Content-Type", "text/plain; charset=utf-8")])
    assert source.load_catalog_url(
        loopback.url("/plain"))["snapshot_id"] == basic_doc["snapshot_id"]

    # -- (i) the READ cap, not a declaration, bounds a body ------------------
    loopback.serve(basic_raw + b" ", length=False)
    _fixed_source_error(
        lambda: source.load_catalog_url(
            loopback.url("/no-length-oversize"), max_bytes=len(basic_raw)),
        ("127.0.0.1", "no-length-oversize"))

    # a CHUNKED body has its OWN http.client reader, so it gets the same cap:
    # one raw byte over fails, exactly at the cap the body is accepted.
    loopback.serve(basic_raw + b" ", chunked=True)
    _fixed_source_error(
        lambda: source.load_catalog_url(
            loopback.url("/chunked-oversize"), max_bytes=len(basic_raw)),
        ("127.0.0.1", "chunked-oversize"))
    loopback.serve(basic_raw, chunked=True)
    assert source.load_catalog_url(
        loopback.url("/chunked-at-cap"),
        max_bytes=len(basic_raw))["snapshot_id"] == basic_doc["snapshot_id"]

    # oversize is rejected BEFORE any UTF-8 decode or catalogue validation.
    loopback.serve(b"\xff" * 64)
    _fixed_source_error(
        lambda: source.load_catalog_url(
            loopback.url("/oversize-binary"), max_bytes=32),
        ("127.0.0.1", "oversize-binary"))

    # a declared over-limit length fails without trusting the socket
    loopback.serve(basic_raw,
                   headers=[("Content-Length", str(len(basic_raw) + 1))],
                   length=False)
    _fixed_source_error(
        lambda: source.load_catalog_url(
            loopback.url("/declared"), max_bytes=len(basic_raw)),
        ("127.0.0.1", "declared"))

    # a COMPLETE valid snapshot followed by early EOF is still a failure
    loopback.serve(basic_raw,
                   headers=[("Content-Length", str(len(basic_raw) + 10))],
                   length=False)
    _fixed_source_error(
        lambda: source.load_catalog_url(loopback.url("/promised")),
        ("127.0.0.1", "promised", basic_raw[:16].decode("utf-8", "replace")))

    # -- (j) defaults (URL, timeout, cap) through a network-free double ------
    opened = []

    def install(response):
        def open_with(self, fullurl, data=None, timeout=None):
            opened.append((fullurl, data, timeout))
            return response
        monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_with)

    # a declared default-cap overflow is rejected before any body read
    big = b"x" * (source.MAX_SOURCE_BYTES + 1)
    oversized = _FakeResponse(
        big, headers=[("Content-Length", str(len(big)))])
    install(oversized)
    _fixed_source_error(
        lambda: source.load_catalog_url(),
        ("raw.githubusercontent.com", "master/site", "http://", "https://"))
    request, data, timeout = opened[0]
    assert request.full_url == source.DEFAULT_CATALOG_URL
    assert request.get_method() == "GET"
    assert data is None
    assert timeout == source.DEFAULT_TIMEOUT_S == 10
    assert request.get_header("Accept") == ACCEPT_HEADER
    sent_default = {name.lower(): value
                    for name, value in request.header_items()}
    assert sent_default["accept-encoding"] == ENCODING_HEADER
    assert oversized.closed == 1
    assert oversized.reads == []

    # with no declaration the READ cap itself is the default bound
    undeclared = _FakeResponse(b" " * (source.MAX_SOURCE_BYTES + 1))
    install(undeclared)
    _fixed_source_error(
        lambda: source.load_catalog_url(), ("raw.githubusercontent.com",))
    assert sum(undeclared.reads) == source.MAX_SOURCE_BYTES + 1
    assert all(amount > 0 for amount in undeclared.reads)
    assert undeclared.closed == 1

    # an exactly-at-limit body reaches core validation (not a size failure)
    at_limit = _FakeResponse(b" " * source.MAX_SOURCE_BYTES)
    install(at_limit)
    with pytest.raises(catalog.CatalogError) as excinfo:
        source.load_catalog_url()
    assert excinfo.value.code == catalog.INVALID_JSON
    assert at_limit.closed == 1

    # a successful default call forwards the default-capped snapshot
    ok_response = _FakeResponse(
        basic_raw, headers=[("Content-Length", str(len(basic_raw)))])
    install(ok_response)
    opened.clear()
    assert source.load_catalog_url()["snapshot_id"] == \
        basic_doc["snapshot_id"]
    assert len(opened) == 1
    assert opened[0][0].full_url == source.DEFAULT_CATALOG_URL
    assert ok_response.closed == 1


def test_catalog_url_source_failures_stay_stable_and_non_reflective(
        loopback, tmp_path, monkeypatch):
    basic_raw = (FIXTURE_DIR / "basic.json").read_bytes()

    # -- (a) EVERY non-200 status is one fixed failure, one GET, no redirect --
    for status in (204, 206, 301, 302, 304, 307, 308, 404, 429, 500, 503):
        redirect = 300 <= status < 400
        loopback.serve(
            b"", status=status,
            headers=[("Location", "/elsewhere")] if redirect else ())
        target = loopback.url("/status-%d" % status)
        before = len(loopback.requests)
        _fixed_source_error(
            lambda: source.load_catalog_url(target),
            ("127.0.0.1", "/status-%d" % status, str(status), "elsewhere",
             "Moved", "Not Found", "Too Many", "Internal", "Unavailable"))
        assert len(loopback.requests) == before + 1
    assert loopback.hits.get("/elsewhere", 0) == 0

    # -- (b) unsupported encodings and ambiguous/invalid lengths -------------
    framing_cases = (
        ("content-encoding", [("Content-Encoding", "gzip")]),
        ("content-encoding-list",
         [("Content-Encoding", "identity, gzip")]),
        ("length-nondigit", [("Content-Length", "12x")]),
        ("length-negative", [("Content-Length", "-5")]),
        ("length-duplicated",
         [("Content-Length", str(len(basic_raw))),
          ("Content-Length", str(len(basic_raw)))]),
        ("length-empty", [("Content-Length", "")]),
    )
    for label, headers in framing_cases:
        loopback.serve(basic_raw, headers=headers, length=False)
        _fixed_source_error(
            lambda: source.load_catalog_url(loopback.url("/" + label)),
            ("127.0.0.1", label, "gzip", "12x", "-5", "Content-Length"))

    # -- (c) accepted core errors survive the HTTP boundary untouched --------
    tampered = json.loads(basic_raw.decode("utf-8"))
    tampered["snapshot_id"] = "0" * 64
    future_version = json.loads(basic_raw.decode("utf-8"))
    future_version["schema_version"] = 2
    core_cases = (
        ("malformed", b'{"schema_version": 1, "snapshot_id": ',
         catalog.INVALID_JSON, "Invalid catalogue JSON."),
        ("invalid-utf8", b'{"schema_version": 1, "snapshot_id": "\xff\xfe"}',
         catalog.INVALID_JSON, "Invalid catalogue JSON."),
        ("tampered", json.dumps(tampered).encode("utf-8"),
         catalog.INVALID_SNAPSHOT, "Invalid catalogue snapshot."),
        ("future-schema", json.dumps(future_version).encode("utf-8"),
         catalog.UNSUPPORTED_SCHEMA_VERSION,
         "Unsupported catalogue schema version."),
    )
    for label, body, code, message in core_cases:
        loopback.serve(body)
        with pytest.raises(catalog.CatalogError) as excinfo:
            source.load_catalog_url(loopback.url("/" + label))
        error = excinfo.value
        assert not isinstance(error, source.SourceError)
        assert (error.code, error.message, str(error)) == (
            code, message, message)
        assert error.to_payload() == {
            "ok": False, "error": {"code": code, "message": message}}
        assert "127.0.0.1" not in str(error)
        assert label not in str(error)
        assert "codec" not in str(error)

    # -- (d) a stalled body is a bounded socket-operation timeout ------------
    loopback.serve_stall(b"x" * 4, declared=4096)
    started = time.monotonic()
    _fixed_source_error(
        lambda: source.load_catalog_url(
            loopback.url("/stalled"), timeout_s=0.3),
        ("127.0.0.1", "stalled", "timed out", "timeout"))
    assert time.monotonic() - started < 5.0
    loopback.release.set()

    # -- (e) a mid-body disconnect is the same fixed failure -----------------
    loopback.serve_disconnect(basic_raw[:24], declared=len(basic_raw))
    _fixed_source_error(
        lambda: source.load_catalog_url(loopback.url("/disconnect")),
        ("127.0.0.1", "disconnect", "400"))

    # -- (f) invalid URL / limit / timeout configuration never dials ---------
    opened = []

    def forbidden_open(self, *args, **kwargs):
        opened.append(args)
        raise AssertionError(
            "invalid configuration must not reach the network")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden_open)

    type_cases = (None, b"https://example.test/catalog.json", 3, True, 3.5,
                  object(), ["https://example.test/x"])
    for bad in type_cases:
        with pytest.raises(TypeError) as excinfo:
            source.load_catalog_url(bad)
        assert type(excinfo.value) is TypeError
        assert str(excinfo.value) == URL_TYPE_ERROR
        assert "example.test" not in str(excinfo.value)

    value_cases = (
        "", "https://", "https:///catalog.json",
        "ftp://example.test/catalog.json", "catalog.json",
        "//example.test/catalog.json",
        "https://user:pw@example.test/catalog.json",
        "https://example.test/catalog.json#fragment",
        "https://example.test/c a talog.json",
        "https://example.test/catalog.json\tx",
        "https://example.test/catalog.json\x00",
        "https://example.test:99999/catalog.json",
        "https://example.test:abc/catalog.json",
        "https://example.test:/catalog.json",
    )
    for bad in value_cases:
        with pytest.raises(ValueError) as excinfo:
            source.load_catalog_url(bad)
        assert type(excinfo.value) is ValueError
        assert str(excinfo.value) == URL_VALUE_ERROR
        assert "example.test" not in str(excinfo.value)

    for bad_limit in (0, -1, True, 1.0, "8388608", None):
        with pytest.raises(ValueError) as excinfo:
            source.load_catalog_url(
                "http://127.0.0.1:1/catalog.json", max_bytes=bad_limit)
        assert type(excinfo.value) is ValueError
        assert str(excinfo.value) == HTTP_BYTE_LIMIT_ERROR

    for bad_timeout in (0, -0.5, True, float("nan"), float("inf"), "10",
                        None):
        with pytest.raises(ValueError) as excinfo:
            source.load_catalog_url(
                "http://127.0.0.1:1/catalog.json", timeout_s=bad_timeout)
        assert type(excinfo.value) is ValueError
        assert str(excinfo.value) == TIMEOUT_ERROR
    assert opened == []
    monkeypatch.undo()

    # -- (g) scoped fault doubles: HTTPError and read/close cleanup ----------
    class _TrackingHTTPError(_HTTPError):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            super().close()

    http_error = _TrackingHTTPError(
        "http://127.0.0.1:1/catalogue", 500, "Internal Server Error",
        _headers_message([("X-Secret", "s3cr3t")]),
        io.BytesIO(b"secret-body"))

    def raise_http_error(self, fullurl, data=None, timeout=None):
        raise http_error

    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        raise_http_error)
    _fixed_source_error(
        lambda: source.load_catalog_url("http://127.0.0.1:1/catalogue"),
        ("127.0.0.1:1", "catalogue", "500", "Internal", "secret", "s3cr3t"))
    assert http_error.close_calls == 1

    read_failures = (
        ("oserror", OSError(104, "Connection reset by peer")),
        ("incomplete", http.client.IncompleteRead(b"s3cr3t-partial", 512)),
        ("protocol", http.client.BadStatusLine("\x00junk")),
    )
    for label, fault in read_failures:
        response = _FakeResponse(b"", read_error=fault)
        monkeypatch.setattr(
            urllib.request.OpenerDirector, "open",
            lambda self, fullurl, data=None, timeout=None, _r=response: _r)
        _fixed_source_error(
            lambda: source.load_catalog_url(
                "http://127.0.0.1:1/" + label),
            ("127.0.0.1:1", label, "s3cr3t", "junk", "reset", "peer"))
        assert response.closed == 1

    close_fault = _FakeResponse(
        basic_raw, headers=[("Content-Length", str(len(basic_raw)))],
        close_error=OSError(5, "close failed s3cr3t"))
    monkeypatch.setattr(
        urllib.request.OpenerDirector, "open",
        lambda self, fullurl, data=None, timeout=None: close_fault)
    _fixed_source_error(
        lambda: source.load_catalog_url("http://127.0.0.1:1/close"),
        ("127.0.0.1:1", "close", "s3cr3t", "failed"))
    assert close_fault.closed == 1

    both = _FakeResponse(
        b"", read_error=OSError(104, "boom"),
        close_error=OSError(5, "s3cr3t close"))
    monkeypatch.setattr(
        urllib.request.OpenerDirector, "open",
        lambda self, fullurl, data=None, timeout=None: both)
    _fixed_source_error(
        lambda: source.load_catalog_url("http://127.0.0.1:1/both"),
        ("127.0.0.1:1", "both", "s3cr3t", "boom"))
    assert both.closed == 1
    monkeypatch.undo()

    # -- (h) private opener/TLS, hostile global settings, no writes ----------
    home = tmp_path / "empty-home"
    home.mkdir()
    neutral = tmp_path / "neutral-cwd"
    neutral.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(neutral)
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                 "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)

    global_hits = []

    class _GlobalHandler(urllib.request.BaseHandler):
        def http_open(self, request):
            global_hits.append(request.full_url)
            raise AssertionError("the global opener must never be used")

    installed = urllib.request.build_opener(_GlobalHandler())
    monkeypatch.setattr(urllib.request, "_opener", installed)

    contexts = []
    real_create_default_context = ssl.create_default_context

    def create_default_context(*args, **kwargs):
        contexts.append((args, kwargs))
        return real_create_default_context(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", create_default_context)

    loopback.serve(basic_raw)
    loaded = source.load_catalog_url(loopback.url("/private"))
    assert loaded["snapshot_id"] == \
        json.loads(basic_raw.decode("utf-8"))["snapshot_id"]
    assert global_hits == []
    assert urllib.request._opener is installed
    assert contexts == [((), {})]
    assert loopback.hits["/private"] == 1
    assert list(home.iterdir()) == []
    assert list(neutral.iterdir()) == []
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "empty-home", "neutral-cwd"]
    monkeypatch.undo()


# ---------------------------------------------------------------------------
# Unit 5c: source-scoped validated disk cache
# ---------------------------------------------------------------------------
#
# The two behavioural groups below cover ONE validated cache record per source:
# real scratch-directory round trips, source-scoped digest filenames, platform
# default roots (lexical only) and every miss/corruption/persistence fault.
# Nothing here touches a real user cache directory, a provider, the network or
# a clock, and nothing is deleted or repaired by the cache itself.

CACHE_VERSION = 1
CACHE_ENTRY_FIELDS = frozenset({"cache_version", "source", "fetched_at",
                                "etag", "last_modified", "snapshot"})
CACHE_KIND_TYPE_ERROR = "DiskCache() requires a cache kind string"
CACHE_KIND_VALUE_ERROR = 'DiskCache() requires kind "url" or "file"'
CACHE_LOCATION_ERROR = "DiskCache() requires a non-empty file source path"
CACHE_DIR_ERROR = "DiskCache() requires a non-empty cache directory path"
FETCHED_AT_ERROR = (
    "DiskCache.store() requires a finite non-negative fetch time")
VALIDATOR_ERROR = (
    "DiskCache.store() requires etag and last_modified to be HTTP header "
    "values or None")


def _identity(kind, location):
    return {"kind": kind, "location": location}


def _entry_name(kind, location):
    """The expected entry filename: SHA-256 of the canonical identity."""
    return hashlib.sha256(
        catalog.canonical_bytes(_identity(kind, location))).hexdigest() + \
        ".json"


def _cache_document(snapshot, *, identity, fetched_at=1700000000, etag=None,
                    last_modified=None, version=CACHE_VERSION):
    return {
        "cache_version": version,
        "source": dict(identity),
        "fetched_at": fetched_at,
        "etag": etag,
        "last_modified": last_modified,
        "snapshot": snapshot,
    }


def _scratch_snapshot(name="basic.json"):
    return json.loads((FIXTURE_DIR / name).read_text("utf-8"))


def _temp_leftovers(directory):
    return sorted(entry.name for entry in directory.iterdir()
                  if entry.name.endswith(".tmp"))


def _empty_snapshot():
    """A legitimately EMPTY catalogue, resealed by the test's own helper."""
    document = _scratch_snapshot()
    document["provider_order"] = []
    document["providers"] = {}
    return _reseal(document)


def test_catalog_disk_cache_round_trips_are_source_scoped(tmp_path):
    assert source._MAX_CACHE_BYTES == 4 * source.MAX_SOURCE_BYTES

    root = tmp_path / "cache-root"
    url = "https://example.test/catalog.json"
    identity = _identity("url", url)
    cache = source.DiskCache("url", url, cache_dir=root)

    # -- (a) construction is INERT and the entry path is source-scoped -------
    assert isinstance(cache.path, Path)
    assert cache.path.is_absolute()
    assert cache.path == root / _entry_name("url", url)
    assert cache.path.name == _entry_name("url", url)
    assert len(cache.path.name) == 64 + len(".json")
    for raw in (url, "example.test", "catalog", "https"):
        assert raw not in cache.path.name
    assert not root.exists()
    assert cache.load() is None

    # -- (b) one record: exact envelope, canonical bytes, full hash ----------
    basic = _scratch_snapshot()
    before = json.loads(json.dumps(basic))
    etag = 'W/"a1b2c3"'
    modified = "Wed, 21 Oct 2026 10:00:00 GMT"
    assert cache.store(basic, fetched_at=1700000000, etag=etag,
                       last_modified=modified) is True
    assert basic == before          # the caller's snapshot is never mutated

    expected = _cache_document(basic, identity=identity, fetched_at=1700000000,
                               etag=etag, last_modified=modified)
    assert cache.path.read_bytes() == catalog.canonical_bytes(expected)

    entry = cache.load()
    assert set(entry) == CACHE_ENTRY_FIELDS
    assert entry == expected
    assert entry["cache_version"] == CACHE_VERSION
    assert type(entry["cache_version"]) is int
    assert entry["source"] == identity
    assert entry["etag"] == etag and entry["last_modified"] == modified
    assert type(entry["fetched_at"]) is int
    assert entry["snapshot"] == basic
    assert catalog.snapshot_hash(entry["snapshot"]) == basic["snapshot_id"]

    # -- (c) every load is a FRESH decode: caller mutation cannot poison -----
    pristine = json.loads(json.dumps(entry))
    again = cache.load()
    assert again == pristine and again is not entry
    assert again["snapshot"] is not entry["snapshot"]
    assert again["snapshot"]["providers"] is not entry["snapshot"]["providers"]
    again["snapshot"]["providers"].clear()
    again["snapshot"]["provider_order"].append("injected")
    again["source"]["location"] = "mutated"
    entry["fetched_at"] = -1
    assert cache.load() == pristine

    # -- (d) typed metadata and a legitimately EMPTY snapshot stay a HIT -----
    empty = _empty_snapshot()
    assert cache.store(empty, fetched_at=1700000000.5) is True
    empty_entry = cache.load()
    assert empty_entry["snapshot"]["provider_order"] == []
    assert empty_entry["snapshot"]["providers"] == {}
    assert empty_entry["snapshot"]["snapshot_id"] == empty["snapshot_id"] == \
        catalog.snapshot_hash(empty_entry["snapshot"])
    assert empty_entry["fetched_at"] == 1700000000.5
    assert type(empty_entry["fetched_at"]) is float
    assert empty_entry["etag"] is None
    assert empty_entry["last_modified"] is None
    # a validator string round trips LITERALLY, never trimmed or requoted
    literal_etag = '  W/"padded"  '
    assert cache.store(basic, fetched_at=0, etag=literal_etag) is True
    assert cache.load()["etag"] == literal_etag

    # -- (e) fetch time is distinct from the observation time, no clock ------
    ordered = _scratch_snapshot("ordering.json")
    observed = ordered["tick_epoch"]
    assert isinstance(observed, int)
    future = source.DiskCache("url", "https://example.test/future.json",
                              cache_dir=root)

    def no_clock(*args, **kwargs):
        raise AssertionError("the disk cache must never read a clock")

    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(time, "time", no_clock)
        scope.setattr(time, "time_ns", no_clock)
        assert future.store(ordered, fetched_at=catalog.MAX_SAFE_INT) is True
        future_entry = future.load()
    assert future_entry["fetched_at"] == catalog.MAX_SAFE_INT
    assert future_entry["snapshot"]["tick_epoch"] == observed
    assert future_entry["snapshot"]["tick_epoch"] != future_entry["fetched_at"]
    assert future_entry["snapshot"] == ordered

    # -- (f) URL and FILE modes never share data, even in one root ----------
    shared = tmp_path / "shared-root"
    url_cache = source.DiskCache("url", url, cache_dir=shared)
    file_cache = source.DiskCache("file", url, cache_dir=shared)
    assert url_cache.location == url                 # URL text stays literal
    assert file_cache.location == os.path.abspath(url)
    assert file_cache.location != url_cache.location
    assert file_cache.path != url_cache.path
    assert file_cache.path.name == _entry_name("file", file_cache.location)
    assert url_cache.store(basic, fetched_at=1) is True
    assert file_cache.load() is None                  # wrong mode: a miss
    assert url_cache.load()["snapshot"] == basic
    assert file_cache.store(basic, fetched_at=2) is True
    assert file_cache.load()["fetched_at"] == 2
    assert url_cache.load()["fetched_at"] == 1

    # a distinct selection of the same mode is a distinct entry
    other = source.DiskCache("url", "https://example.test/other.json",
                             cache_dir=shared)
    assert other.path != url_cache.path
    assert other.load() is None

    # a foreign-source record copied into the CORRECT filename still misses:
    # the filename scopes the lookup, the embedded identity decides the hit.
    victim = source.DiskCache("url", "https://example.test/victim.json",
                              cache_dir=tmp_path / "foreign")
    impostor = source.DiskCache("url", "https://example.test/impostor.json",
                                cache_dir=tmp_path / "foreign-src")
    assert impostor.store(basic, fetched_at=5) is True
    victim.path.parent.mkdir(parents=True, exist_ok=True)
    victim.path.write_bytes(impostor.path.read_bytes())
    assert victim.load() is None
    assert victim.path.read_bytes() == impostor.path.read_bytes()

    # -- (g) file identities freeze the LEXICAL absolute path ----------------
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    with pytest.MonkeyPatch.context() as scope:
        scope.chdir(left)
        from_left = source.DiskCache("file", "catalog.json",
                                     cache_dir=tmp_path / "identity-root")
        tilde = source.DiskCache("file", "~/catalog.json",
                                 cache_dir=tmp_path / "identity-root")
        expanded = source.DiskCache("file", "$HOME/catalog.json",
                                    cache_dir=tmp_path / "identity-root")
    with pytest.MonkeyPatch.context() as scope:
        scope.chdir(right)
        from_right = source.DiskCache("file", "catalog.json",
                                      cache_dir=tmp_path / "identity-root")
    assert from_left.location == os.path.abspath(
        os.path.join(str(left), "catalog.json"))
    assert from_right.location == os.path.abspath(
        os.path.join(str(right), "catalog.json"))
    assert from_left.path != from_right.path
    assert from_left.path.name == _entry_name("file", from_left.location)
    # no tilde/environment expansion and no cwd after the freeze
    assert tilde.location.endswith("~/catalog.json")
    assert not tilde.location.startswith(os.path.expanduser("~"))
    assert expanded.location.endswith("$HOME/catalog.json")
    # lexical only: a symlinked location is NOT resolved to its target
    link = tmp_path / "link-catalog.json"
    real = tmp_path / "real-catalog.json"
    real.write_bytes(b"{}")
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        link = None
    if link is not None:
        linked = source.DiskCache("file", link, cache_dir=tmp_path / "links")
        assert linked.location == os.path.abspath(str(link))
        assert linked.location != os.path.realpath(str(link))

    # -- (h) a relative cache_dir is anchored at construction ----------------
    with pytest.MonkeyPatch.context() as scope:
        scope.chdir(left)
        relative = source.DiskCache("url", url, cache_dir="relative-cache")
        anchored = relative.path
        assert anchored.is_absolute()
        assert anchored.parent == left / "relative-cache"
    with pytest.MonkeyPatch.context() as scope:
        scope.chdir(right)
        assert relative.path == anchored
        assert relative.store(basic, fetched_at=3) is True
    assert (left / "relative-cache" / anchored.name).is_file()
    assert not (right / "relative-cache").exists()

    # -- (i) the platform default roots, purely lexically --------------------
    defaults = (
        ("linux", {"HOME": "/home/tester"},
         "/home/tester/.cache/watchdog-catalog"),
        ("linux", {"XDG_CACHE_HOME": "/var/cache/tester",
                   "HOME": "/home/tester"},
         "/var/cache/tester/watchdog-catalog"),
        ("linux", {"XDG_CACHE_HOME": "/var/cache/tester"},
         "/var/cache/tester/watchdog-catalog"),
        ("linux", {"XDG_CACHE_HOME": "relative/cache", "HOME": "/home/tester"},
         "/home/tester/.cache/watchdog-catalog"),
        ("darwin", {"HOME": "/Users/tester"},
         "/Users/tester/Library/Caches/watchdog-catalog"),
        ("win32", {"LOCALAPPDATA": "C:/Users/tester/AppData/Local"},
         "C:/Users/tester/AppData/Local/watchdog-catalog"),
        ("win32", {"LOCALAPPDATA": "relative",
                   "USERPROFILE": "C:/Users/tester"},
         "C:/Users/tester/AppData/Local/watchdog-catalog"),
        ("win32", {"USERPROFILE": "C:/Users/tester"},
         "C:/Users/tester/AppData/Local/watchdog-catalog"),
    )
    for platform, environ, expected_root in defaults:
        found = source._default_cache_root(platform=platform,
                                          environ=environ)
        assert found == Path(expected_root), (platform, environ)

    unusable = (
        {}, {"HOME": ""}, {"HOME": "relative/home"}, {"HOME": None},
        {"XDG_CACHE_HOME": "", "HOME": ""},
        {"XDG_CACHE_HOME": "relative", "HOME": "relative/home"},
        {"LOCALAPPDATA": ""}, {"USERPROFILE": ""},
        {"LOCALAPPDATA": "relative", "USERPROFILE": "relative"},
        {"HOME": "/home/tester\x00"},
    )
    for platform in ("linux", "darwin", "win32"):
        for environ in unusable:
            assert source._default_cache_root(
                platform=platform, environ=environ) is None, (platform,
                                                              environ)
    # no cwd/root/system-temp fallback is ever invented
    with pytest.MonkeyPatch.context() as scope:
        neutral = tmp_path / "neutral"
        neutral.mkdir()
        scope.chdir(neutral)
        assert source._default_cache_root(
            platform="linux", environ={}) is None
        assert source._default_cache_root(
            platform="linux", environ={"HOME": ""}) is None
        assert list(neutral.iterdir()) == []
    # the runtime default platform is the live one
    if sys.platform == "darwin":
        assert source._default_cache_root(
            environ={"HOME": "/Users/tester"}) == \
            Path("/Users/tester/Library/Caches/watchdog-catalog")
    elif sys.platform == "win32":
        assert source._default_cache_root(
            environ={"LOCALAPPDATA": "C:/tester"}) == \
            Path("C:/tester/watchdog-catalog")
    else:
        assert source._default_cache_root(
            environ={"HOME": "/home/tester"}) == \
            Path("/home/tester/.cache/watchdog-catalog")

    # -- (j) a live default root round trips INSIDE scratch ------------------
    home = tmp_path / "home"
    home.mkdir()
    with pytest.MonkeyPatch.context() as scope:
        scope.chdir(neutral)
        scope.setenv("HOME", str(home))
        scope.delenv("XDG_CACHE_HOME", raising=False)
        default_cache = source.DiskCache("url", url + "?default")
        assert default_cache.path is not None
        assert default_cache.path.is_absolute()
        assert default_cache.path.parent == \
            home / ".cache" / "watchdog-catalog"
        assert not (home / ".cache").exists()
        assert default_cache.store(basic, fetched_at=4) is True
        assert default_cache.load()["snapshot"] == basic
        assert list(neutral.iterdir()) == []
        # an explicit root always wins over the platform default
        explicit = source.DiskCache("url", url + "?default",
                                    cache_dir=tmp_path / "explicit")
        assert explicit.path.parent == tmp_path / "explicit"
        assert explicit.path != default_cache.path
    assert sorted(entry.name for entry in (home / ".cache").iterdir()) == [
        "watchdog-catalog"]
    assert list(neutral.iterdir()) == []


def test_catalog_disk_cache_misses_and_write_faults_stay_contained(tmp_path):
    url = "https://example.test/catalog.json"
    identity = _identity("url", url)
    basic = _scratch_snapshot()
    empty = _empty_snapshot()
    root = tmp_path / "cache-root"
    cache = source.DiskCache("url", url, cache_dir=root)

    # -- (a) invalid configuration fails BEFORE any file/temp/replace step ---
    hits = []

    def forbidden(step):
        def _fail(*args, **kwargs):
            hits.append(step)
            raise AssertionError(
                "invalid configuration must not reach the filesystem")
        return _fail

    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source.os, "makedirs", forbidden("makedirs"))
        scope.setattr(source.tempfile, "mkstemp", forbidden("mkstemp"))
        scope.setattr(source.os, "fdopen", forbidden("fdopen"))
        scope.setattr(source.os, "replace", forbidden("replace"))
        scope.setattr(source, "open", forbidden("open"), raising=False)
        for bad_kind in (None, 3, True, 3.5, b"url", bytearray(b"file"),
                         object(), ["url"]):
            with pytest.raises(TypeError) as excinfo:
                source.DiskCache(bad_kind, url)
            assert type(excinfo.value) is TypeError
            assert str(excinfo.value) == CACHE_KIND_TYPE_ERROR
            assert excinfo.value.__cause__ is None
        for bad_kind in ("", "URL", "url ", "file ", "disk", "https"):
            with pytest.raises(ValueError) as excinfo:
                source.DiskCache(bad_kind, url)
            assert type(excinfo.value) is ValueError
            assert str(excinfo.value) == CACHE_KIND_VALUE_ERROR

        for bad_location in ("",):
            with pytest.raises(ValueError) as excinfo:
                source.DiskCache("file", bad_location)
            assert str(excinfo.value) == CACHE_LOCATION_ERROR
        for bad_location in (None, 3, True, b"/tmp/catalog.json", object()):
            with pytest.raises(TypeError) as excinfo:
                source.DiskCache("file", bad_location)
            assert str(excinfo.value) == PATH_TYPE_ERROR
        with pytest.raises(ValueError) as excinfo:
            source.DiskCache("url", "")
        assert str(excinfo.value) == URL_VALUE_ERROR
        with pytest.raises(TypeError) as excinfo:
            source.DiskCache("url", b"https://example.test/x")
        assert str(excinfo.value) == URL_TYPE_ERROR

        for bad_dir in ("",):
            with pytest.raises(ValueError) as excinfo:
                source.DiskCache("url", url, cache_dir=bad_dir)
            assert str(excinfo.value) == CACHE_DIR_ERROR
        for bad_dir in (3, True, b"/tmp/cache", object()):
            with pytest.raises(TypeError) as excinfo:
                source.DiskCache("url", url, cache_dir=bad_dir)
            assert str(excinfo.value) == PATH_TYPE_ERROR

        inert = source.DiskCache("url", "https://example.test/inert.json",
                                 cache_dir=root)
        for bad_time in (None, True, False, -1, -0.5, "1700", [1],
                         float("nan"), float("inf"), float("-inf"),
                         catalog.MAX_SAFE_INT + 1, 1e300):
            with pytest.raises(ValueError) as excinfo:
                inert.store(basic, fetched_at=bad_time)
            assert type(excinfo.value) is ValueError
            assert str(excinfo.value) == FETCHED_AT_ERROR
            assert "example.test" not in str(excinfo.value)

        for bad_validator in (3, 1.5, True, b'W/"x"', [1], "",
                              "a" * 1025, "bad\rvalue", "bad\nvalue",
                              "bad\x00value", "bad\x7fvalue",
                              "bad\x9bvalue", "bad\u0100value",
                              "bad\r\nvalue"):
            with pytest.raises(ValueError) as excinfo:
                inert.store(basic, fetched_at=1, etag=bad_validator)
            assert str(excinfo.value) == VALIDATOR_ERROR
            with pytest.raises(ValueError) as excinfo:
                inert.store(basic, fetched_at=1,
                            last_modified=bad_validator)
            assert str(excinfo.value) == VALIDATOR_ERROR

        # a bad snapshot keeps its accepted core error
        tampered = json.loads(json.dumps(basic))
        tampered["snapshot_id"] = "0" * 64
        future = json.loads(json.dumps(basic))
        future["schema_version"] = 2
        for bad_snapshot in (None, {}, [], "text", 3, tampered, future):
            with pytest.raises(catalog.CatalogError) as excinfo:
                inert.store(bad_snapshot, fetched_at=1)
            assert not isinstance(excinfo.value, source.SourceError)
            assert excinfo.value.code in (
                catalog.INVALID_SNAPSHOT, catalog.UNSUPPORTED_SCHEMA_VERSION)
        # nothing above touched mkdir/temp/wrapper-open/replace or an entry
        assert hits == []
        assert not (root / _entry_name("url",
                                       "https://example.test/inert.json")
                    ).exists()

    # the accepted validator edges: 1024 printable characters, Latin-1 text
    assert inert.store(basic, fetched_at=1, etag="a" * 1024,
                       last_modified="Caf\u00e9 \u00ff") is True
    assert inert.load()["etag"] == "a" * 1024
    assert inert.load()["last_modified"] == "Caf\u00e9 \u00ff"

    # -- (b) no safe default root: None path, misses, strict False -----------
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "_default_cache_root", lambda: None)
        rootless = source.DiskCache("url", url + "?rootless")
        assert rootless.path is None
        assert rootless.load() is None
        assert rootless.store(basic, fetched_at=1) is False
    if os.name == "posix":
        with pytest.MonkeyPatch.context() as scope:
            scope.setenv("HOME", "")
            scope.delenv("XDG_CACHE_HOME", raising=False)
            assert source._default_cache_root() is None
            empty_home = source.DiskCache("url", url + "?empty-home")
            assert empty_home.path is None
            assert empty_home.store(basic, fetched_at=1) is False
            assert empty_home.load() is None

    # an unusable root (a regular FILE where the directory must be) is a
    # strict False/None, never an exception and never a cwd write
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    blocked = source.DiskCache("url", url + "?blocked",
                               cache_dir=blocker / "below")
    assert blocked.path is not None
    assert blocked.store(basic, fetched_at=8) is False
    assert blocked.load() is None
    assert blocker.read_text(encoding="utf-8") == "file"

    # -- (c) a MISS is never a healthy empty catalogue, and never deleted ----
    assert cache.store(basic, fetched_at=1700000000) is True
    good_bytes = cache.path.read_bytes()
    base_text = good_bytes.decode("utf-8")
    truncated = good_bytes[:-24]
    duplicate = ('{"cache_version": 1, ' + base_text[1:]).encode("utf-8")
    non_finite = base_text.replace(
        '"fetched_at":1700000000', '"fetched_at":1e999').encode("utf-8")
    bad_version = catalog.canonical_bytes(_cache_document(
        basic, identity=identity, version=2))
    float_version = catalog.canonical_bytes(_cache_document(
        basic, identity=identity, version=1.0))
    bool_version = catalog.canonical_bytes(_cache_document(
        basic, identity=identity, version=True))
    text_version = catalog.canonical_bytes(_cache_document(
        basic, identity=identity, version="1"))
    missing_key = _cache_document(basic, identity=identity)
    del missing_key["etag"]
    extra_key = _cache_document(basic, identity=identity)
    extra_key["observation_epoch"] = 1
    wrong_source = _cache_document(
        basic, identity=_identity("url", "https://example.test/impostor.json"))
    wrong_mode = _cache_document(
        basic, identity=_identity("file", os.path.abspath(url)))
    sparse_source = _cache_document(basic, identity={"kind": "url"})
    extra_identity = _cache_document(
        basic, identity={"kind": "url", "location": url, "etag": "x"})
    negative_time = _cache_document(basic, identity=identity, fetched_at=-1)
    text_time = _cache_document(basic, identity=identity, fetched_at="1")
    bad_etag = _cache_document(basic, identity=identity, etag="a\r\nb")
    newer_schema = _scratch_snapshot()
    newer_schema["schema_version"] = 2
    bad_hash = json.loads(json.dumps(basic))
    bad_hash["snapshot_id"] = "0" * 64

    corrupt = {
        "empty": b"",
        "whitespace": b"   \n\t",
        "truncated": truncated,
        "duplicate-keys": duplicate,
        "non-finite": non_finite,
        "bom": b"\xef\xbb\xbf" + good_bytes,
        "invalid-utf8": b'{"cache_version": 1, "source": {"kind": "url", '
                        b'"location": "\xff\xfe"}}',
        "not-an-object": b"[]",
        "null-root": b"null",
        "cache-version-2": bad_version,
        "cache-version-float": float_version,
        "cache-version-bool": bool_version,
        "cache-version-text": text_version,
        "missing-key": catalog.canonical_bytes(missing_key),
        "extra-key": catalog.canonical_bytes(extra_key),
        "wrong-source": catalog.canonical_bytes(wrong_source),
        "wrong-mode": catalog.canonical_bytes(wrong_mode),
        "sparse-source": catalog.canonical_bytes(sparse_source),
        "extra-identity": catalog.canonical_bytes(extra_identity),
        "negative-fetch-time": catalog.canonical_bytes(negative_time),
        "text-fetch-time": catalog.canonical_bytes(text_time),
        "bad-etag": catalog.canonical_bytes(bad_etag),
        "unsupported-schema": catalog.canonical_bytes(
            _cache_document(newer_schema, identity=identity)),
        "invalid-snapshot-hash": catalog.canonical_bytes(
            _cache_document(bad_hash, identity=identity)),
        "snapshot-not-object": catalog.canonical_bytes(
            _cache_document(None, identity=identity)),
    }
    for label, payload in corrupt.items():
        assert payload != good_bytes, label
        cache.path.write_bytes(payload)
        assert cache.load() is None, label
        # never deleted, repaired or rewritten
        assert cache.path.read_bytes() == payload, label

    # -- (d) an unreadable entry is the same quiet MISS ----------------------
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "open",
                      lambda *a, **k: (_ for _ in ()).throw(
                          OSError(13, "Permission denied")), raising=False)
        assert cache.load() is None

        class _ReadFault:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self, size):
                assert size == source._MAX_CACHE_BYTES + 1
                raise OSError(5, "I/O error")
        scope.setattr(source, "open", lambda *a, **k: _ReadFault(),
                      raising=False)
        assert cache.load() is None
    cache.path.unlink()
    assert cache.load() is None          # absent entry

    # -- (e) the ENTRY byte bound is real, on the reader and the writer ------
    bound = source._MAX_CACHE_BYTES
    assert bound == 4 * source.MAX_SOURCE_BYTES
    at_bound = good_bytes + b" " * (bound - len(good_bytes))
    assert len(at_bound) == bound
    cache.path.write_bytes(at_bound)
    assert cache.load()["snapshot"] == basic      # exactly at the bound
    cache.path.write_bytes(at_bound + b" ")
    assert cache.load() is None                   # one byte past the bound
    cache.path.write_bytes(b"x" * (bound + 1))
    assert cache.load() is None
    cache.path.unlink()

    # -- (f) injected persistence faults: last-good survives, no leaks ------
    class _FaultHandle:
        """The real staged handle with ONE named step failing."""

        def __init__(self, handle, step, error, cleanup_error=None):
            self.handle = handle
            self._step = step
            self._error = error
            self._cleanup_error = cleanup_error
            self.closed = 0

        def write(self, data):
            if self._step == "write":
                raise self._error
            return self.handle.write(data)

        def flush(self):
            if self._step == "flush":
                raise self._error
            return self.handle.flush()

        def fileno(self):
            return self.handle.fileno()

        def close(self):
            self.closed += 1
            self.handle.close()
            if self._step == "close":
                raise self._error
            if self._cleanup_error is not None:
                raise self._cleanup_error

    def install(scope, step, error, cleanup=None):
        seen = {"fds": [], "handles": []}

        def raising(raised):
            def _fail(*args, **kwargs):
                raise raised
            return _fail

        close_fault = (ValueError("cleanup close fault")
                       if cleanup == "handle" else None)
        if step == "makedirs":
            scope.setattr(source.os, "makedirs", raising(error))
        elif step == "mkstemp":
            scope.setattr(source.tempfile, "mkstemp", raising(error))
        elif step == "fdopen":
            def fdopen(fd, *args, **kwargs):
                seen["fds"].append(fd)
                raise error
            scope.setattr(source.os, "fdopen", fdopen)
        elif step in ("write", "flush", "close"):
            real_fdopen = os.fdopen

            def fdopen(fd, *args, **kwargs):
                wrapper = _FaultHandle(
                    real_fdopen(fd, *args, **kwargs), step, error, close_fault)
                seen["handles"].append(wrapper)
                return wrapper
            scope.setattr(source.os, "fdopen", fdopen)
        elif step == "fsync":
            scope.setattr(source.os, "fsync", raising(error))
        else:
            scope.setattr(source.os, "replace", raising(error))
        if cleanup == "unlink":
            scope.setattr(source.os, "unlink",
                          raising(ValueError("cleanup unlink fault")))
        elif cleanup == "descriptor":
            scope.setattr(source.os, "close",
                          raising(ValueError("cleanup close fault")))
        return seen

    error = OSError(28, "No space left on device")
    for step in ("makedirs", "mkstemp", "fdopen", "write", "flush", "fsync",
                 "close", "replace"):
        fault_root = tmp_path / ("fault-" + step)
        target = source.DiskCache("url", url, cache_dir=fault_root)
        unrelated = fault_root / "unrelated.txt"

        # (i) no previous entry at all
        with pytest.MonkeyPatch.context() as scope:
            seen = install(scope, step, error)
            assert target.store(basic, fetched_at=5) is False
            if step == "fdopen":
                assert seen["fds"]
                for descriptor in seen["fds"]:
                    with pytest.raises(OSError):
                        os.fstat(descriptor)     # our own fd was closed
            for wrapper in seen["handles"]:
                assert wrapper.closed >= 1
                assert wrapper.handle.closed
        assert not target.path.exists()
        if fault_root.exists():
            assert _temp_leftovers(fault_root) == []

        # (ii) a pre-existing last-good entry and an unrelated neighbour
        fault_root.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("keep me", encoding="utf-8")
        assert target.store(basic, fetched_at=5) is True
        good = target.path.read_bytes()
        with pytest.MonkeyPatch.context() as scope:
            install(scope, step, error)
            assert target.store(empty, fetched_at=6) is False
        assert target.path.read_bytes() == good       # last-good untouched
        assert target.load()["snapshot"] == basic
        assert unrelated.read_text(encoding="utf-8") == "keep me"
        assert _temp_leftovers(fault_root) == []
        assert sorted(entry.name for entry in fault_root.iterdir()) == [
            target.path.name, "unrelated.txt"]

    # a failed store never reports success for an entry the reader rejects
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "_MAX_CACHE_BYTES", 1)
        assert target.store(empty, fetched_at=7) is False
    assert target.path.read_bytes() == good

    # -- (g) two concurrent writers: distinct temps, ONE whole record --------
    concurrent_root = tmp_path / "concurrent"
    writers = tuple(source.DiskCache("url", url, cache_dir=concurrent_root)
                    for _ in range(2))
    staged = []
    real_mkstemp = tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        staged.append(name)
        return descriptor, name

    results = {}
    barrier = threading.Barrier(2, timeout=10)

    def writer(label, snapshot, base_time):
        barrier.wait()
        results[label] = [writers[0].store(snapshot, fetched_at=base_time + i,
                                           etag="%s-%d" % (label, i))
                          for i in range(8)]

    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source.tempfile, "mkstemp", recording_mkstemp)
        threads = (
            threading.Thread(target=writer, args=("alpha", basic, 1000),
                             daemon=True),
            threading.Thread(target=writer, args=("beta", empty, 2000),
                             daemon=True),
        )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
            assert not thread.is_alive()
    assert results == {"alpha": [True] * 8, "beta": [True] * 8}
    assert len(staged) == 16
    assert len(set(staged)) == len(staged)             # unique temp names
    assert {Path(name).parent for name in staged} == {concurrent_root}
    assert not any(name.endswith(".json") for name in staged)

    final = writers[1].load()
    assert final is not None
    assert set(final) == CACHE_ENTRY_FIELDS
    if final["snapshot"] == basic:
        assert final["snapshot"]["snapshot_id"] == basic["snapshot_id"]
        assert 1000 <= final["fetched_at"] <= 1007
        assert final["etag"].startswith("alpha-")
    else:
        assert final["snapshot"]["snapshot_id"] == empty["snapshot_id"]
        assert 2000 <= final["fetched_at"] <= 2007
        assert final["etag"].startswith("beta-")
    assert catalog.snapshot_hash(final["snapshot"]) == \
        final["snapshot"]["snapshot_id"]
    assert _temp_leftovers(concurrent_root) == []
    assert sorted(entry.name for entry in concurrent_root.iterdir()) == [
        writers[0].path.name]

    # -- (h) filesystem VALUE/encoding faults stay inside the cache boundary -
    # The path layer can reject a VALUE long before the syscall -- an embedded
    # NUL, or a name the filesystem encoding cannot represent -- with ValueError
    # (UnicodeError is a ValueError subclass). A cache write must contain those
    # exactly like an OSError: strict False, last-good bytes kept, and no path
    # repair, re-encoding or escaping exception.
    nul_root = str(tmp_path / "nul-root") + "/nul\x00inside"
    nul_cache = source.DiskCache("url", url + "?nul", cache_dir=nul_root)
    assert nul_cache.path is not None
    assert "\x00" in str(nul_cache.path)          # literal, never repaired
    assert nul_cache.store(basic, fetched_at=9) is False
    assert nul_cache.load() is None               # the reader contains it too
    # a failing makedirs may have created a parent component, and the cache
    # never deletes a directory: it must simply stage NO entry and NO temp.
    nul_parent = tmp_path / "nul-root"
    if nul_parent.exists():
        assert list(nul_parent.iterdir()) == []

    value_faults = (
        ("value", ValueError("bad path value")),
        ("encoding", UnicodeEncodeError(
            "utf-8", "bad\udcff", 0, len("bad\udcff"),
            "surrogates not allowed")),
    )
    for label, fault in value_faults:
        for step in ("makedirs", "mkstemp", "replace"):
            fault_root = tmp_path / ("%s-%s" % (label, step))
            target = source.DiskCache("url", url + "?" + label + step,
                                      cache_dir=fault_root)
            unrelated = fault_root / "unrelated.txt"

            # (i) no previous entry at all
            with pytest.MonkeyPatch.context() as scope:
                install(scope, step, fault)
                assert target.store(basic, fetched_at=5) is False
            assert not target.path.exists()
            if fault_root.exists():
                assert _temp_leftovers(fault_root) == []

            # (ii) a pre-existing last-good entry and an unrelated neighbour
            fault_root.mkdir(parents=True, exist_ok=True)
            unrelated.write_text("keep me", encoding="utf-8")
            assert target.store(basic, fetched_at=5) is True
            good = target.path.read_bytes()
            with pytest.MonkeyPatch.context() as scope:
                install(scope, step, fault)
                assert target.store(empty, fetched_at=6) is False
            assert target.path.read_bytes() == good       # last-good untouched
            assert target.load()["snapshot"] == basic
            assert unrelated.read_text(encoding="utf-8") == "keep me"
            assert _temp_leftovers(fault_root) == []
            assert sorted(entry.name for entry in fault_root.iterdir()) == [
                target.path.name, "unrelated.txt"]

    # best-effort cleanup VALUE faults never mask the primary failure, expose
    # their text or widen into a delete: the store still returns strict False.
    cleanup_root = tmp_path / "cleanup-faults"
    cleanup_cache = source.DiskCache("url", url + "?cleanup",
                                     cache_dir=cleanup_root)
    cleanup_root.mkdir(parents=True, exist_ok=True)
    assert cleanup_cache.store(basic, fetched_at=5) is True
    cleanup_good = cleanup_cache.path.read_bytes()

    # (i) staged handle: write fails, and the cleanup close ALSO raises
    with pytest.MonkeyPatch.context() as scope:
        seen = install(scope, "write", OSError(28, "No space left on device"),
                       cleanup="handle")
        assert cleanup_cache.store(empty, fetched_at=6) is False
        assert seen["handles"]
        assert all(wrapper.handle.closed for wrapper in seen["handles"])
    assert cleanup_cache.path.read_bytes() == cleanup_good
    assert cleanup_cache.load()["snapshot"] == basic
    assert _temp_leftovers(cleanup_root) == []

    # (ii) wrapper open fails, and the cleanup descriptor close ALSO raises
    with pytest.MonkeyPatch.context() as scope:
        seen = install(scope, "fdopen", OSError(24, "Too many open files"),
                       cleanup="descriptor")
        assert cleanup_cache.store(empty, fetched_at=7) is False
        assert len(seen["fds"]) == 1
        leaked = seen["fds"][0]
    os.close(leaked)              # the scoped close fault ended: ours to close
    assert cleanup_cache.path.read_bytes() == cleanup_good
    assert _temp_leftovers(cleanup_root) == []

    # (iii) replace fails, and the cleanup unlink ALSO raises: the temp stays
    # behind only because the fault is artificial, so the test owns its cleanup
    with pytest.MonkeyPatch.context() as scope:
        install(scope, "replace", OSError(28, "No space left on device"),
                cleanup="unlink")
        assert cleanup_cache.store(empty, fetched_at=8) is False
    assert cleanup_cache.path.read_bytes() == cleanup_good
    assert cleanup_cache.load()["snapshot"] == basic
    leftovers = _temp_leftovers(cleanup_root)
    assert len(leftovers) == 1
    for name in leftovers:
        (cleanup_root / name).unlink()
    assert _temp_leftovers(cleanup_root) == []

    # the boundary was not widened: a plain valid write still publishes
    assert cleanup_cache.store(empty, fetched_at=9) is True
    assert cleanup_cache.load()["snapshot"] == empty
    assert _temp_leftovers(cleanup_root) == []
    assert sorted(entry.name for entry in cleanup_root.iterdir()) == [
        cleanup_cache.path.name]


# ---------------------------------------------------------------------------
# Unit 5d: conditional GET and 304/cache validity
# ---------------------------------------------------------------------------
#
# `fetch_catalog_url` is ONE synchronous conditional GET of ONE literal URL. A
# caller-supplied candidate is untrusted DATA: it is detached and fully
# re-validated (version, source identity, fetch metadata, complete schema and
# body hash) BEFORE the request, so only a same-source, complete, valid
# snapshot can authorize a 304 or contribute a conditional header. Nothing here
# persists, repairs, falls back or reads a clock; the retained observation
# keeps its own snapshot_id, tick_epoch and provider metadata.

REVALIDATION_KEYS = frozenset({"snapshot", "etag", "last_modified",
                               "not_modified"})
#: Fields that belong to later units (fallback/warning/freshness envelopes and
#: the on-disk cache document), never to this internal transport result.
DEFERRED_FIELDS = ("ok", "error", "source", "fetched_at", "age", "age_s",
                   "stale", "warnings", "warning", "cache_version")


def _script(server, *responses):
    """Serve one scripted ``(status, body, headers, length, chunked)`` row per
    request through the fixture server's existing responder hook."""
    queue = list(responses)

    def respond(handler):
        _write_response(handler, *queue.pop(0))

    server._responder = respond
    return queue


def _opener_double(scope, response=None, error=None):
    """Point the private-opener seam at one scripted double, recording calls."""
    opened = []

    def open_with(self, fullurl, data=None, timeout=None):
        opened.append((fullurl, data, timeout))
        if error is not None:
            raise error
        return response

    scope.setattr(urllib.request.OpenerDirector, "open", open_with)
    return opened


def _no_conditional(request):
    sent = _sent_headers(request)
    assert "if-none-match" not in sent
    assert "if-modified-since" not in sent


def _only_conditional(request, header, value):
    sent = _sent_headers(request)
    assert sent[header] == value
    for name in ("if-none-match", "if-modified-since"):
        if name != header:
            assert name not in sent


def test_catalog_revalidation_conditional_get_and_304_reuse(loopback,
                                                            tmp_path):
    basic_raw = (FIXTURE_DIR / "basic.json").read_bytes()
    basic = json.loads(basic_raw.decode("utf-8"))
    url = loopback.url("/catalog.json")
    identity = _identity("url", url)
    stamp = "Wed, 21 Oct 2026 10:00:00 GMT"
    newer = "Thu, 22 Oct 2026 10:00:00 GMT"
    entry = _cache_document(basic, identity=identity, fetched_at=1700000000,
                            etag='W/"v1"', last_modified=stamp)

    # -- (a) no candidate: ONE unconditional GET, this response's validators --
    _script(loopback, (200, basic_raw,
                       [("ETag", 'W/"v1"'), ("Last-Modified", stamp)],
                       True, False))
    first = source.fetch_catalog_url(url)
    assert set(first) == REVALIDATION_KEYS
    assert first["not_modified"] is False
    assert first["snapshot"] == basic
    assert first["snapshot"]["snapshot_id"] == basic["snapshot_id"] == \
        catalog.snapshot_hash(first["snapshot"])
    assert (first["etag"], first["last_modified"]) == ('W/"v1"', stamp)
    assert len(loopback.requests) == 1
    assert loopback.requests[0]["method"] == "GET"
    assert loopback.requests[0]["path"] == "/catalog.json"
    sent = _sent_headers(loopback.requests[0])
    assert sent["accept"] == ACCEPT_HEADER
    assert sent["accept-encoding"] == ENCODING_HEADER
    for name in ("if-none-match", "if-modified-since", "authorization",
                 "cookie", "proxy-authorization"):
        assert name not in sent

    # -- (b) a valid same-source candidate sends EXACTLY its ETag; the 304
    # reuses the detached snapshot with every observation field untouched ----
    loopback.requests.clear()
    _script(loopback, (304, b"", [("ETag", 'W/"v1"')], False, False))
    before = json.loads(json.dumps(entry["snapshot"]))
    reused = source.fetch_catalog_url(url, cached_entry=entry)
    assert set(reused) == REVALIDATION_KEYS
    assert reused["not_modified"] is True
    assert (reused["etag"], reused["last_modified"]) == ('W/"v1"', stamp)
    assert reused["snapshot"] == before
    assert reused["snapshot"] is not entry["snapshot"]
    assert reused["snapshot"]["snapshot_id"] == basic["snapshot_id"]
    assert reused["snapshot"]["tick_epoch"] == basic["tick_epoch"]
    assert reused["snapshot"]["provider_order"] == basic["provider_order"]
    assert reused["snapshot"]["warnings"] == basic["warnings"]
    assert list(reused["snapshot"]["providers"]) == list(basic["providers"])
    assert entry["snapshot"] == before      # the caller's entry is never edited
    assert len(loopback.requests) == 1
    _only_conditional(loopback.requests[0], "if-none-match", 'W/"v1"')

    # -- (c) one validator is selected: ETag preferred, else Last-Modified ----
    validator_rows = (
        ("etag-preferred", 'W/"v1"', stamp, "if-none-match", 'W/"v1"'),
        ("date-fallback", None, stamp, "if-modified-since", stamp),
        ("unusable-etag-falls-back", "*", stamp, "if-modified-since", stamp),
        ("quoted-empty-etag", '""', None, "if-none-match", '""'),
    )
    for label, etag, modified, header, value in validator_rows:
        loopback.requests.clear()
        _script(loopback, (304, b"", [], False, False))
        candidate = _cache_document(basic, identity=identity, fetched_at=5,
                                    etag=etag, last_modified=modified)
        kept = source.fetch_catalog_url(url, cached_entry=candidate)
        assert kept["not_modified"] is True, label
        assert kept["snapshot"] == basic, label
        assert (kept["etag"], kept["last_modified"]) == (etag, modified), label
        assert len(loopback.requests) == 1, label
        _only_conditional(loopback.requests[0], header, value)

    # a weak response tag and the candidate's strong spelling are the SAME
    # opaque tag for GET, and the candidate's metadata is preserved on 304.
    loopback.requests.clear()
    _script(loopback, (304, b"", [("ETag", '"v1"')], False, False))
    strong = source.fetch_catalog_url(url, cached_entry=entry)
    assert strong["not_modified"] is True
    assert strong["etag"] == 'W/"v1"'
    assert strong["last_modified"] == stamp
    assert strong["snapshot"] == basic

    # -- (d) a legitimately EMPTY cached snapshot stays usable; a future
    # fetch time is candidate DATA (never repaired, never clock-compared) -----
    empty = _empty_snapshot()
    loopback.requests.clear()
    _script(loopback, (304, b"", [], False, False))
    empty_entry = _cache_document(empty, identity=identity,
                                  fetched_at=catalog.MAX_SAFE_INT,
                                  etag='"e1"')
    empty_reuse = source.fetch_catalog_url(url, cached_entry=empty_entry)
    assert empty_reuse["not_modified"] is True
    assert empty_reuse["snapshot"]["provider_order"] == []
    assert empty_reuse["snapshot"]["providers"] == {}
    assert empty_reuse["snapshot"]["snapshot_id"] == empty["snapshot_id"]
    assert set(empty_reuse) == REVALIDATION_KEYS
    for absent in DEFERRED_FIELDS:
        assert absent not in empty_reuse

    # -- (e) a changed 200 is the NEW body: its missing validators clear the
    # old body's metadata rather than inheriting it ---------------------------
    changed = json.loads(basic_raw.decode("utf-8"))
    changed["tick_epoch"] += 60
    _reseal(changed)
    changed_raw = json.dumps(changed, indent=2).encode("utf-8")
    loopback.requests.clear()
    _script(loopback, (200, changed_raw, [], True, False))
    refreshed = source.fetch_catalog_url(url, cached_entry=entry)
    assert refreshed["not_modified"] is False
    assert (refreshed["etag"], refreshed["last_modified"]) == (None, None)
    assert refreshed["snapshot"]["snapshot_id"] == changed["snapshot_id"]
    assert refreshed["snapshot"]["snapshot_id"] != basic["snapshot_id"]
    assert refreshed["snapshot"]["tick_epoch"] == changed["tick_epoch"]
    assert len(loopback.requests) == 1
    _only_conditional(loopback.requests[0], "if-none-match", 'W/"v1"')

    # -- (f) optional response validators are captured literally or dropped
    # WITHOUT discarding an accepted body -------------------------------------
    header_rows = (
        ("etag-strong", [("ETag", '"abc"')], ('"abc"', None)),
        ("etag-weak", [("ETag", 'W/"abc"')], ('W/"abc"', None)),
        ("etag-quoted-empty", [("ETag", '""')], ('""', None)),
        ("etag-header-empty", [("ETag", "")], (None, None)),
        ("etag-wildcard", [("ETag", "*")], (None, None)),
        ("etag-list", [("ETag", '"a", "b"')], (None, None)),
        ("etag-unquoted", [("ETag", "abc")], (None, None)),
        ("etag-unbalanced", [("ETag", '"abc')], (None, None)),
        ("etag-interior-quote", [("ETag", '"a"b"')], (None, None)),
        ("etag-interior-space", [("ETag", '"a b"')], (None, None)),
        ("etag-control", [("ETag", '"a\tb"')], (None, None)),
        ("etag-over-bound", [("ETag", '"' + "a" * 1024 + '"')], (None, None)),
        ("etag-duplicate", [("ETag", '"a"'), ("ETag", '"a"')], (None, None)),
        ("etag-obs-text", [("ETag", '"caf\xe9"')], ('"caf\xe9"', None)),
        ("date-valid", [("Last-Modified", stamp)], (None, stamp)),
        ("date-rfc850",
         [("Last-Modified", "Wednesday, 21-Oct-26 10:00:00 GMT")],
         (None, "Wednesday, 21-Oct-26 10:00:00 GMT")),
        ("date-asctime", [("Last-Modified", "Wed Oct 21 10:00:00 2026")],
         (None, "Wed Oct 21 10:00:00 2026")),
        ("date-list", [("Last-Modified", stamp + ", " + newer)], (None, None)),
        ("date-no-zone",
         [("Last-Modified", "Wed, 21 Oct 2026 10:00:00")], (None, None)),
        ("date-non-utc",
         [("Last-Modified", "Wed, 21 Oct 2026 10:00:00 PST")], (None, None)),
        ("date-trailing", [("Last-Modified", stamp + " extra")], (None, None)),
        ("date-impossible",
         [("Last-Modified", "Wed, 32 Oct 2026 10:00:00 GMT")], (None, None)),
        ("date-garbage", [("Last-Modified", "yesterday")], (None, None)),
        ("date-empty", [("Last-Modified", "")], (None, None)),
        ("date-over-bound", [("Last-Modified", stamp + " GMT" * 300)],
         (None, None)),
        ("date-duplicate",
         [("Last-Modified", stamp), ("Last-Modified", stamp)], (None, None)),
        ("both", [("ETag", '"abc"'), ("Last-Modified", stamp)],
         ('"abc"', stamp)),
    )
    with pytest.MonkeyPatch.context() as scope:
        for label, headers, expected in header_rows:
            response = _FakeResponse(basic_raw, status=200, headers=headers)
            _opener_double(scope, response=response)
            captured = source.fetch_catalog_url(url)
            assert (captured["etag"], captured["last_modified"]) == expected, \
                label
            assert captured["not_modified"] is False, label
            assert captured["snapshot"]["snapshot_id"] == \
                basic["snapshot_id"], label
            assert response.closed == 1, label
            assert response.reads, label

    # -- (g) neither candidate validator is usable: unconditional GET, and a
    # 304 is then a failure rather than an empty success ----------------------
    loopback.requests.clear()
    _script(loopback, (200, basic_raw, [("ETag", '"fresh"')], True, False))
    bare = _cache_document(basic, identity=identity, etag="*",
                           last_modified="yesterday")
    unconditional = source.fetch_catalog_url(url, cached_entry=bare)
    assert unconditional["not_modified"] is False
    assert unconditional["etag"] == '"fresh"'
    assert unconditional["last_modified"] is None
    _no_conditional(loopback.requests[0])

    # -- (h) mutating the caller's candidate inside the opener cannot poison
    # the retained 304 body; mutating a result cannot poison a later call -----
    victim = _cache_document(json.loads(json.dumps(basic)), identity=identity,
                             fetched_at=7, etag='W/"v1"',
                             last_modified=stamp)
    expected_body = json.loads(json.dumps(victim["snapshot"]))

    with pytest.MonkeyPatch.context() as scope:
        def hostile(self, fullurl, data=None, timeout=None):
            target = victim["snapshot"]
            target["provider_order"] = []
            target["providers"] = {}
            target["snapshot_id"] = "0" * 64
            victim["etag"] = '"mutated"'
            return _FakeResponse(b"", status=304,
                                 headers=[("ETag", 'W/"v1"')])

        scope.setattr(urllib.request.OpenerDirector, "open", hostile)
        owned = source.fetch_catalog_url(url, cached_entry=victim)
    assert owned["not_modified"] is True
    assert owned["snapshot"] == expected_body
    assert owned["snapshot"]["snapshot_id"] == basic["snapshot_id"]
    assert owned["snapshot"] is not victim["snapshot"]
    assert (owned["etag"], owned["last_modified"]) == ('W/"v1"', stamp)

    owned["snapshot"]["providers"] = {}
    owned["snapshot"]["snapshot_id"] = "f" * 64
    owned["etag"] = "mutated"
    loopback.requests.clear()
    _script(loopback, (304, b"", [], False, False))
    replay = source.fetch_catalog_url(
        url, cached_entry=json.loads(json.dumps(entry)))
    assert replay["snapshot"] == basic
    assert replay["etag"] == 'W/"v1"'
    _only_conditional(loopback.requests[0], "if-none-match", 'W/"v1"')

    # -- (i) the returned snapshot and the DiskCache record are compatible:
    # one stored result loads back as an accepted candidate, and the fetch
    # itself never reads or writes the cache ---------------------------------
    root = tmp_path / "cache-root"
    cache = source.DiskCache("url", url, cache_dir=root)
    assert list(tmp_path.iterdir()) == []
    assert cache.store(first["snapshot"], fetched_at=1700000000,
                       etag=first["etag"],
                       last_modified=first["last_modified"]) is True
    stored_bytes = cache.path.read_bytes()
    loaded = cache.load()
    assert loaded == _cache_document(first["snapshot"], identity=identity,
                                     fetched_at=1700000000,
                                     etag=first["etag"],
                                     last_modified=first["last_modified"])
    loopback.requests.clear()
    _script(loopback, (304, b"", [], False, False))
    from_disk = source.fetch_catalog_url(url, cached_entry=loaded)
    assert from_disk["not_modified"] is True
    assert from_disk["snapshot"] == basic
    assert (from_disk["etag"], from_disk["last_modified"]) == ('W/"v1"', stamp)
    _only_conditional(loopback.requests[0], "if-none-match", 'W/"v1"')
    assert cache.path.read_bytes() == stored_bytes
    assert _temp_leftovers(root) == []
    assert sorted(item.name for item in tmp_path.iterdir()) == ["cache-root"]


def test_catalog_revalidation_invalid_304_and_stale_candidates_fail_closed(
        loopback, tmp_path):
    basic_raw = (FIXTURE_DIR / "basic.json").read_bytes()
    basic = json.loads(basic_raw.decode("utf-8"))
    url = loopback.url("/catalog.json")
    identity = _identity("url", url)
    stamp = "Wed, 21 Oct 2026 10:00:00 GMT"
    valid = _cache_document(basic, identity=identity, etag='W/"v1"',
                            last_modified=stamp)
    pristine = json.loads(json.dumps(valid))
    tampered = json.loads(json.dumps(basic))
    tampered["snapshot_id"] = "0" * 64
    future_schema = json.loads(json.dumps(basic))
    future_schema["schema_version"] = 2

    # -- (a) an unsolicited 304 with no candidate is one fixed failure --------
    loopback.requests.clear()
    _script(loopback, (304, b"", [], False, False))
    _fixed_source_error(lambda: source.fetch_catalog_url(url),
                        ("127.0.0.1", "catalog.json", "304", "Not Modified"))
    assert len(loopback.requests) == 1
    _no_conditional(loopback.requests[0])

    # -- (b) every invalid candidate is a MISS: NO conditional header, a 200
    # still recovers, and a 304 stays one fixed failure in ONE GET ------------
    def candidate(snapshot=basic, identity_value=None, **overrides):
        document = _cache_document(
            snapshot,
            identity=identity if identity_value is None else identity_value,
            etag='W/"v1"', last_modified=stamp)
        document.update(overrides)
        return document

    def without_pairs(pairs, field):
        document = dict(pairs)
        del document[field]
        return document

    bad_entries = (
        ("no-candidate", None),
        ("json-list", [basic]),
        ("scalar", 17),
        ("empty-dict", {}),
        ("foreign-source", candidate(identity_value=_identity(
            "url", url + "?mirror=none"))),
        ("file-mode", candidate(identity_value=_identity(
            "file", "/tmp/some/catalog.json"))),
        ("extra-identity", candidate(identity_value={
            "kind": "url", "location": url, "extra": 1})),
        ("wrong-version", candidate(cache_version=2)),
        ("string-version", candidate(cache_version="1")),
        ("bool-version", candidate(cache_version=True)),
        ("missing-field", without_pairs(candidate(), "last_modified")),
        ("extra-field", candidate(extra="field")),
        ("negative-fetch-time", candidate(fetched_at=-1)),
        ("bool-fetch-time", candidate(fetched_at=True)),
        ("nan-fetch-time", candidate(fetched_at=float("nan"))),
        ("int-etag", candidate(etag=12345)),
        ("control-etag", candidate(etag='"a\tb"')),
        ("bad-hash", candidate(snapshot=tampered)),
        ("future-schema", candidate(snapshot=future_schema)),
    )
    for label, bad in bad_entries:
        loopback.requests.clear()
        _script(loopback, (200, basic_raw, [], True, False))
        recovered = source.fetch_catalog_url(url, cached_entry=bad)
        assert recovered["not_modified"] is False, label
        assert recovered["snapshot"] == basic, label
        assert len(loopback.requests) == 1, label
        _no_conditional(loopback.requests[0])

        loopback.requests.clear()
        _script(loopback, (304, b"", [], False, False))
        _fixed_source_error(
            lambda value=bad: source.fetch_catalog_url(
                url, cached_entry=value), ())
        assert len(loopback.requests) == 1, label
        _no_conditional(loopback.requests[0])

    # A fully VALID candidate whose optional validators are merely UNUSABLE is
    # still a hit for the body, but contributes no conditional header -- so its
    # 304 can authorize nothing either.
    unusable_values = _cache_document(
        basic, identity=identity, etag="*",
        last_modified="Wed, 21 Oct 2026 10:00:00 PST")
    loopback.requests.clear()
    _script(loopback, (200, basic_raw, [], True, False))
    accepted = source.fetch_catalog_url(url, cached_entry=unusable_values)
    assert accepted["not_modified"] is False
    assert accepted["snapshot"] == basic
    _no_conditional(loopback.requests[0])
    loopback.requests.clear()
    _script(loopback, (304, b"", [], False, False))
    _fixed_source_error(
        lambda: source.fetch_catalog_url(url, cached_entry=unusable_values), ())
    _no_conditional(loopback.requests[0])

    # -- (c) an oversized candidate is a miss; exactly at the accepted bound
    # it is still usable (the bound is narrowed for the probe, never widened) -
    sized = candidate()
    size = len(catalog.canonical_bytes(sized))
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "_MAX_CACHE_BYTES", size - 1)
        loopback.requests.clear()
        _script(loopback, (200, basic_raw, [], True, False))
        assert source.fetch_catalog_url(
            url, cached_entry=sized)["not_modified"] is False
        _no_conditional(loopback.requests[0])
        loopback.requests.clear()
        _script(loopback, (304, b"", [], False, False))
        _fixed_source_error(
            lambda: source.fetch_catalog_url(url, cached_entry=sized), ())
        _no_conditional(loopback.requests[0])
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "_MAX_CACHE_BYTES", size)
        loopback.requests.clear()
        _script(loopback, (304, b"", [], False, False))
        assert source.fetch_catalog_url(
            url, cached_entry=sized)["not_modified"] is True
        _only_conditional(loopback.requests[0], "if-none-match", 'W/"v1"')

    # -- (d) a 304 body is NEVER read, decoded or parsed, and its hypothetical
    # Content-Length never reaches the 200 framing gate ----------------------
    with pytest.MonkeyPatch.context() as scope:
        poison = _FakeResponse(
            b"", status=304,
            headers=[("ETag", 'W/"v1"'),
                     ("Content-Length", str(source.MAX_SOURCE_BYTES + 4096))],
            read_error=AssertionError("a 304 body must never be read"))
        _opener_double(scope, response=poison)
        reused = source.fetch_catalog_url(url, cached_entry=valid,
                                          max_bytes=len(basic_raw))
        assert reused["not_modified"] is True
        assert reused["snapshot"] == basic
        assert (reused["etag"], reused["last_modified"]) == ('W/"v1"', stamp)
        assert poison.reads == []
        assert poison.closed == 1

    class _PoisonBody(io.BytesIO):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.read_calls = 0

        def _poison(self, *args, **kwargs):
            self.read_calls += 1
            raise AssertionError("a 304 body must never be read")

        read = _poison
        readline = _poison

    class _TrackingHTTPError(_HTTPError):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            super().close()

    with pytest.MonkeyPatch.context() as scope:
        body = _PoisonBody(b'{"schema_version": 1}')
        not_modified = _TrackingHTTPError(
            url, 304, "Not Modified",
            _headers_message([("ETag", 'W/"v1"'),
                              ("Content-Length",
                               str(source.MAX_SOURCE_BYTES))]),
            body)
        _opener_double(scope, error=not_modified)
        reused = source.fetch_catalog_url(url, cached_entry=valid,
                                          max_bytes=8)
        assert reused["not_modified"] is True
        assert reused["snapshot"] == basic
        assert body.read_calls == 0
        assert not_modified.close_calls == 1

    # -- (e) a contradictory response tag fails closed; an unusable one is
    # ignored; a failed 304 close is a source failure -------------------------
    with pytest.MonkeyPatch.context() as scope:
        conflict = _FakeResponse(b"", status=304,
                                 headers=[("ETag", '"other"')])
        _opener_double(scope, response=conflict)
        _fixed_source_error(
            lambda: source.fetch_catalog_url(url, cached_entry=valid),
            ('"other"', "v1"))
        assert conflict.reads == []
        assert conflict.closed == 1

    with pytest.MonkeyPatch.context() as scope:
        unusable = _FakeResponse(b"", status=304, headers=[("ETag", "*")])
        _opener_double(scope, response=unusable)
        assert source.fetch_catalog_url(
            url, cached_entry=valid)["not_modified"] is True
        assert unusable.closed == 1

    with pytest.MonkeyPatch.context() as scope:
        broken = _FakeResponse(b"", status=304,
                               close_error=OSError(5, "close failed s3cr3t"))
        _opener_double(scope, response=broken)
        _fixed_source_error(
            lambda: source.fetch_catalog_url(url, cached_entry=valid),
            ("s3cr3t", "close"))
        assert broken.reads == []
        assert broken.closed == 1

    # -- (f) an invalid 200 body keeps its accepted core error even with a
    # valid last-good entry: this function never falls back ------------------
    invalid_bodies = (
        ("malformed", b'{"schema_version": 1, "snapshot_id": ',
         catalog.INVALID_JSON, "Invalid catalogue JSON."),
        ("tampered", json.dumps(tampered).encode("utf-8"),
         catalog.INVALID_SNAPSHOT, "Invalid catalogue snapshot."),
        ("future-schema", json.dumps(future_schema).encode("utf-8"),
         catalog.UNSUPPORTED_SCHEMA_VERSION,
         "Unsupported catalogue schema version."),
    )
    for label, body, code, message in invalid_bodies:
        loopback.requests.clear()
        _script(loopback, (200, body, [], True, False))
        with pytest.raises(catalog.CatalogError) as excinfo:
            source.fetch_catalog_url(url, cached_entry=valid)
        error = excinfo.value
        assert not isinstance(error, source.SourceError)
        assert (error.code, error.message, str(error)) == (
            code, message, message), label
        assert "127.0.0.1" not in str(error)
        assert len(loopback.requests) == 1, label
        _only_conditional(loopback.requests[0], "if-none-match", 'W/"v1"')
        assert valid == pristine

    # -- (g) transport and framing failures stay fixed and non-reflective ----
    loopback.requests.clear()
    _script(loopback, (500, b"", [], False, False))
    _fixed_source_error(
        lambda: source.fetch_catalog_url(url, cached_entry=valid),
        ("127.0.0.1", "500", "Internal", "Unavailable"))
    assert len(loopback.requests) == 1
    assert valid == pristine

    loopback.requests.clear()
    _script(loopback, (200, basic_raw + b" ", [], False, False))
    _fixed_source_error(
        lambda: source.fetch_catalog_url(url, cached_entry=valid,
                                         max_bytes=len(basic_raw)),
        ("127.0.0.1",))
    assert len(loopback.requests) == 1
    assert valid == pristine

    with pytest.MonkeyPatch.context() as scope:
        broken = _FakeResponse(b"", read_error=OSError(104,
                                                       "Connection reset"))
        _opener_double(scope, response=broken)
        _fixed_source_error(
            lambda: source.fetch_catalog_url(url, cached_entry=valid),
            ("reset", "peer"))
        assert broken.closed == 1
        assert valid == pristine

    # -- (h) configuration reuse: fixed exceptions, before any network -------
    with pytest.MonkeyPatch.context() as scope:
        opened = _opener_double(scope, response=_FakeResponse(b""))
        with pytest.raises(TypeError) as excinfo:
            source.fetch_catalog_url(None, cached_entry=valid)
        assert type(excinfo.value) is TypeError
        assert str(excinfo.value) == URL_TYPE_ERROR
        for bad in ("", "//example.test/x", "ftp://example.test/x"):
            with pytest.raises(ValueError) as excinfo:
                source.fetch_catalog_url(bad, cached_entry=valid)
            assert str(excinfo.value) == URL_VALUE_ERROR
        for bad_timeout in (0, -1, True, float("nan"), "10"):
            with pytest.raises(ValueError) as excinfo:
                source.fetch_catalog_url(url, cached_entry=valid,
                                         timeout_s=bad_timeout)
            assert str(excinfo.value) == TIMEOUT_ERROR
        for bad_limit in (0, -1, True, 1.0, None):
            with pytest.raises(ValueError) as excinfo:
                source.fetch_catalog_url(url, cached_entry=valid,
                                         max_bytes=bad_limit)
            assert str(excinfo.value) == HTTP_BYTE_LIMIT_ERROR
        assert opened == []

    # nothing above persisted anything, and the candidate is still pristine
    assert valid == pristine
    assert sorted(item.name for item in tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Unit 5e: last-good fallback, provenance and observation freshness
# ---------------------------------------------------------------------------
#
# ``CatalogSource`` is the synchronous acquisition/state layer over the
# accepted loaders, revalidator and ``DiskCache``. It owns ONE selected source,
# keeps at most one validated last-good snapshot in memory (bootstrapped ONCE
# from its source-scoped disk entry) and reports that snapshot's REAL
# observation age separately from its retrieval time. A failed refresh returns
# the last-good body with ``refresh_failed``/``cache_fallback`` warnings, or the
# frozen ``SourceError`` envelope when nothing valid was ever captured -- never
# an invented empty success. ``with_source`` attaches that provenance to any
# pure query payload without touching it.

SOURCE_METADATA_KEYS = frozenset({
    "mode", "url", "cached", "fetched_at", "fetch_age_s", "observation_age_s",
    "observation_fresh", "not_modified", "warnings"})
NOW_ERROR = (
    "CatalogSource() requires now as a non-negative integer epoch in seconds")
WITH_SOURCE_ERROR = (
    "with_source() requires a payload object and an acquisition object")


def _clock_snapshot(*, tick_epoch, cadence_s=60):
    """A committed accepted snapshot with an explicit observation clock."""
    document = _scratch_snapshot()
    document["tick_epoch"] = tick_epoch
    document["cadence_s"] = cadence_s
    if tick_epoch is None:
        # The accepted schema requires a null tick to say it is unknown.
        document["warnings"] = ["tick_epoch_unknown"]
    return _reseal(document)


def _snapshot_bytes(snapshot):
    return json.dumps(snapshot, indent=2).encode("utf-8")


def _error_envelope():
    return {"ok": False,
            "error": {"code": SOURCE_CODE, "message": SOURCE_MESSAGE}}


def _acquisition_metadata(acquisition, *, snapshot=None):
    """Assert the exact successful-acquisition envelope and return its source."""
    assert set(acquisition) == {"ok", "snapshot", "source"}
    assert acquisition["ok"] is True
    metadata = acquisition["source"]
    assert set(metadata) == SOURCE_METADATA_KEYS
    assert type(metadata["cached"]) is bool
    assert type(metadata["not_modified"]) is bool
    assert type(metadata["warnings"]) is list
    if metadata["fetch_age_s"] is not None:
        assert metadata["fetch_age_s"] >= 0
    if snapshot is not None:
        assert acquisition["snapshot"] == snapshot
        assert catalog.snapshot_hash(acquisition["snapshot"]) == \
            snapshot["snapshot_id"]
    return metadata


def test_catalog_source_refresh_fallback_and_freshness(loopback, tmp_path,
                                                      monkeypatch):
    fresh = _clock_snapshot(tick_epoch=0)
    fresh_raw = _snapshot_bytes(fresh)
    newer = _clock_snapshot(tick_epoch=120)
    newer["providers"]["synthetic-b"]["stale"] = True
    newer = _reseal(newer)
    newer_raw = _snapshot_bytes(newer)
    url = loopback.url("/catalog.json")
    cache_root = tmp_path / "cache-root"
    stamp = "Wed, 21 Oct 2026 10:00:00 GMT"

    origin = source.CatalogSource("url", url, cache_dir=cache_root)
    assert not cache_root.exists()          # construction performs no I/O

    # -- (a) no capture yet: a STRUCTURED source error, never an empty ok -----
    assert origin.current(now=60) == _error_envelope()
    assert not cache_root.exists()

    # -- (b) 200: fetched_at is NOW; observation age comes from tick_epoch ----
    loopback.requests.clear()
    _script(loopback, (200, fresh_raw,
                       [("ETag", 'W/"one"'), ("Last-Modified", stamp)],
                       True, False))
    first = origin.refresh(now=60)
    metadata = _acquisition_metadata(first, snapshot=fresh)
    assert metadata["mode"] == "url" and metadata["url"] == url
    assert metadata["cached"] is False and metadata["not_modified"] is False
    assert metadata["fetched_at"] == 60 and metadata["fetch_age_s"] == 0
    # In the inclusive one-cadence boundary at 60 seconds: still fresh.
    assert metadata["observation_age_s"] == 60
    assert metadata["observation_fresh"] is True
    assert metadata["warnings"] == []
    assert len(loopback.requests) == 1
    assert cache_root.is_dir()

    # -- (c) 304: retrieval time moves, the OBSERVATION time does not ---------
    loopback.requests.clear()
    _script(loopback, (304, b"", [("ETag", 'W/"one"')], False, False))
    revalidated = origin.refresh(now=61)
    metadata = _acquisition_metadata(revalidated, snapshot=fresh)
    assert metadata["cached"] is True and metadata["not_modified"] is True
    assert metadata["fetched_at"] == 61 and metadata["fetch_age_s"] == 0
    # One second past the cadence: a brand-new download is NOT a new
    # observation.
    assert metadata["observation_age_s"] == 61
    assert metadata["observation_fresh"] is False
    assert metadata["warnings"] == ["tick_stale"]
    _only_conditional(loopback.requests[0], "if-none-match", 'W/"one"')

    # -- (d) a changed 200 replaces body AND validators -----------------------
    loopback.requests.clear()
    _script(loopback, (200, newer_raw, [], True, False))
    replaced = origin.refresh(now=130)
    metadata = _acquisition_metadata(replaced, snapshot=newer)
    assert metadata["cached"] is False and metadata["not_modified"] is False
    assert metadata["fetched_at"] == 130
    assert metadata["observation_age_s"] == 10
    assert metadata["observation_fresh"] is True
    assert metadata["warnings"] == []
    _only_conditional(loopback.requests[0], "if-none-match", 'W/"one"')
    loopback.requests.clear()
    _script(loopback, (200, newer_raw, [], True, False))
    origin.refresh(now=131)
    _no_conditional(loopback.requests[0])   # the new body has no validators

    # -- (e) failed refresh: last-good body, real age, explicit warnings ------
    loopback.requests.clear()
    _script(loopback, (404, b"", [], False, False))
    fallback = origin.refresh(now=200)
    metadata = _acquisition_metadata(fallback, snapshot=newer)
    assert metadata["cached"] is True and metadata["not_modified"] is False
    assert metadata["fetched_at"] == 131   # original retrieval time preserved
    assert metadata["fetch_age_s"] == 69
    assert metadata["observation_age_s"] == 80
    assert metadata["observation_fresh"] is False
    assert metadata["warnings"] == [
        "refresh_failed", "cache_fallback", "tick_stale"]
    assert fallback["snapshot"]["snapshot_id"] == newer["snapshot_id"]
    assert fallback["snapshot"]["tick_epoch"] == 120
    for provider in newer["provider_order"]:
        assert fallback["snapshot"]["providers"][provider]["stale"] is \
            newer["providers"][provider]["stale"]
        assert fallback["snapshot"]["providers"][provider]["model_ids"] == \
            newer["providers"][provider]["model_ids"]
    assert fallback["snapshot"]["providers"]["synthetic-b"]["stale"] is True

    # -- (f) cold start: the source-scoped DISK entry is the last-good body ---
    cold = source.CatalogSource("url", url, cache_dir=cache_root)
    loopback.requests.clear()
    _script(loopback, (500, b"", [], True, False))
    recovered = cold.refresh(now=500)
    metadata = _acquisition_metadata(recovered, snapshot=newer)
    assert metadata["cached"] is True and metadata["not_modified"] is False
    assert metadata["fetched_at"] == 131
    assert metadata["fetch_age_s"] == 369
    assert metadata["observation_age_s"] == 380
    assert metadata["warnings"] == [
        "refresh_failed", "cache_fallback", "tick_stale"]
    loopback.requests.clear()
    view = cold.current(now=500)
    metadata = _acquisition_metadata(view, snapshot=newer)
    assert metadata["cached"] is True and metadata["not_modified"] is False
    assert metadata["warnings"] == [
        "refresh_failed", "cache_fallback", "tick_stale"]
    assert loopback.requests == []

    # -- (g) explicit FILE refresh: frozen absolute path, real re-read --------
    file_dir = tmp_path / "selected"
    file_dir.mkdir()
    selected = file_dir / "catalog.json"
    selected.write_bytes(fresh_raw)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(file_dir)
    local = source.CatalogSource("file", "catalog.json",
                                 cache_dir=tmp_path / "file-cache")
    monkeypatch.chdir(elsewhere)
    (elsewhere / "catalog.json").write_bytes(newer_raw)
    local_first = local.refresh(now=5)
    metadata = _acquisition_metadata(local_first, snapshot=fresh)
    assert metadata["mode"] == "file" and metadata["url"] is None
    assert metadata["cached"] is False and metadata["not_modified"] is False
    assert metadata["fetched_at"] == 5 and metadata["fetch_age_s"] == 0
    assert metadata["observation_age_s"] == 5
    assert metadata["observation_fresh"] is True
    assert metadata["warnings"] == []
    # The SAME frozen file is re-read explicitly -- never a cwd reselection.
    selected.write_bytes(newer_raw)
    local_second = local.refresh(now=200)
    metadata = _acquisition_metadata(local_second, snapshot=newer)
    assert metadata["cached"] is False and metadata["not_modified"] is False
    assert metadata["fetched_at"] == 200
    # A recent successful download of OLD observations remains stale.
    assert metadata["observation_age_s"] == 80
    assert metadata["observation_fresh"] is False
    assert metadata["warnings"] == ["tick_stale"]
    selected.unlink()
    local_fallback = local.refresh(now=201)
    metadata = _acquisition_metadata(local_fallback, snapshot=newer)
    assert metadata["cached"] is True
    assert metadata["fetched_at"] == 200
    assert metadata["observation_age_s"] == 81
    assert metadata["warnings"] == [
        "refresh_failed", "cache_fallback", "tick_stale"]

    # -- (h) future and unknown observation clocks ----------------------------
    future_path = tmp_path / "future.json"
    future_path.write_bytes(_snapshot_bytes(_clock_snapshot(tick_epoch=70)))
    future_source = source.CatalogSource(
        "file", future_path, cache_dir=tmp_path / "future-cache")
    captured = future_source.refresh(now=80)
    assert _acquisition_metadata(captured)["fetched_at"] == 80
    backwards = future_source.current(now=61)
    metadata = _acquisition_metadata(backwards)
    assert metadata["fetched_at"] == 80
    assert metadata["fetch_age_s"] is None       # never clamped to zero
    assert metadata["observation_age_s"] is None
    assert metadata["observation_fresh"] is False
    assert metadata["warnings"] == ["fetch_time_future", "tick_epoch_future"]

    unknown_path = tmp_path / "unknown.json"
    unknown_path.write_bytes(_snapshot_bytes(_clock_snapshot(tick_epoch=None)))
    unknown_source = source.CatalogSource(
        "file", unknown_path, cache_dir=tmp_path / "unknown-cache")
    metadata = _acquisition_metadata(unknown_source.refresh(now=10))
    assert metadata["observation_age_s"] is None
    assert metadata["observation_fresh"] is False
    assert metadata["warnings"] == ["tick_epoch_unknown"]

    # -- (i) optional persistence failure is a WARNING, not a data failure ----
    blocked = tmp_path / "blocked-root"
    blocked.write_bytes(b"not a cache directory")
    stalled = source.CatalogSource("url", url, cache_dir=blocked)
    loopback.requests.clear()
    _script(loopback, (200, fresh_raw, [], True, False))
    stored = stalled.refresh(now=60)
    metadata = _acquisition_metadata(stored, snapshot=fresh)
    assert metadata["warnings"] == ["cache_write_failed"]
    assert stalled.current(now=60)["source"]["warnings"] == [
        "cache_write_failed"]
    # A LATER successful store clears the persistence warning.
    healing = source.CatalogSource("url", url,
                                   cache_dir=tmp_path / "healing-cache")
    loopback.requests.clear()
    _script(loopback, (200, fresh_raw, [], True, False))
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source.DiskCache, "store", lambda self, *a, **k: False)
        broken = healing.refresh(now=60)
    assert _acquisition_metadata(broken)["warnings"] == ["cache_write_failed"]
    loopback.requests.clear()
    _script(loopback, (200, fresh_raw, [], True, False))
    healed = healing.refresh(now=61)
    metadata = _acquisition_metadata(healed, snapshot=fresh)
    assert metadata["fetched_at"] == 61
    # The persistence warning is gone; the observation age is still real.
    assert metadata["warnings"] == ["tick_stale"]
    assert healing.current(now=61)["source"]["warnings"] == ["tick_stale"]

    # -- (j) all five pure tools + with_source expose the SAME warnings -------
    tools = (
        ("list_free_models",
         lambda snapshot: query.list_free_models(snapshot, {})),
        ("get_model",
         lambda snapshot: query.get_model(
             snapshot, {"model_id": "alpha-1:free"})),
        ("list_endpoints",
         lambda snapshot: query.list_endpoints(snapshot, {})),
        ("watchdog_status",
         lambda snapshot: query.watchdog_status(snapshot, {}, now=200)),
        ("search_models",
         lambda snapshot: query.search_models(snapshot, {})),
    )
    exposed = cold.current(now=500)
    for round_index in (1, 2):
        for label, call in tools:
            base = call(newer)
            attached = source.with_source(base, exposed)
            assert attached == {**copy.deepcopy(base),
                                "source": copy.deepcopy(
                                    exposed["source"])}, (round_index, label)
            assert attached["ok"] is base["ok"], label
            assert attached["source"]["warnings"] == [
                "refresh_failed", "cache_fallback", "tick_stale"], label
            assert call(newer) == base, label  # the core payload is untouched
    # An argument-error result keeps its shape and still gains provenance.
    argument_error = query.get_model(newer, {"model_id": 3})
    assert argument_error["ok"] is False
    attached_error = source.with_source(argument_error, exposed)
    assert attached_error["ok"] is False
    assert attached_error["error"] == argument_error["error"]
    assert attached_error["source"]["warnings"] == [
        "refresh_failed", "cache_fallback", "tick_stale"]
    assert query.get_model(newer, {"model_id": 3}) == argument_error
    # A FAILED acquisition attaches as its own structured error, never an ok.
    missing = source.CatalogSource("url", loopback.url("/absent.json"),
                                   cache_dir=tmp_path / "absent-cache")
    loopback.requests.clear()
    _script(loopback, (404, b"", [], False, False))
    failure = missing.refresh(now=10)
    assert failure == _error_envelope()
    attached_failure = source.with_source(
        query.list_free_models(newer, {}), failure)
    assert attached_failure == _error_envelope()
    assert attached_failure is not failure
    for bad in (None, [], "payload", 3, True):
        with pytest.raises(TypeError) as excinfo:
            source.with_source(bad, exposed)
        assert type(excinfo.value) is TypeError
        assert str(excinfo.value) == WITH_SOURCE_ERROR
        with pytest.raises(TypeError) as excinfo:
            source.with_source({"ok": False}, bad)
        assert str(excinfo.value) == WITH_SOURCE_ERROR

    # -- (k) mutation cannot poison retained memory or another instance -------
    pristine = copy.deepcopy(newer)
    hostile = cold.current(now=500)
    hostile["snapshot"]["providers"] = {}
    hostile["snapshot"]["provider_order"].append("injected")
    hostile["snapshot"]["snapshot_id"] = "0" * 64
    hostile["source"]["warnings"].append("mutated")
    hostile["source"]["fetched_at"] = -1
    first["snapshot"]["providers"].clear()
    first["source"]["warnings"].append("mutated")
    assert cold.current(now=500)["snapshot"] == pristine
    again = cold.current(now=500)["source"]
    assert again["fetched_at"] == 131
    assert again["warnings"] == [
        "refresh_failed", "cache_fallback", "tick_stale"]
    assert origin.current(now=131)["snapshot"] == pristine

    # -- (l) distinct sources stay distinct through success and fallback ------
    other_url = loopback.url("/other.json")
    shared = tmp_path / "shared-cache"
    alpha = source.CatalogSource("url", url, cache_dir=shared)
    beta = source.CatalogSource("url", other_url, cache_dir=shared)
    loopback.requests.clear()
    _script(loopback, (200, fresh_raw, [], True, False),
            (200, newer_raw, [], True, False))
    alpha_snapshot = alpha.refresh(now=10)
    beta_snapshot = beta.refresh(now=10)
    assert _acquisition_metadata(alpha_snapshot)["fetched_at"] == 10
    assert alpha_snapshot["snapshot"]["tick_epoch"] == 0
    assert beta_snapshot["snapshot"]["tick_epoch"] == 120
    assert alpha._cache.path != beta._cache.path
    assert sorted(item.name for item in shared.iterdir()) == sorted(
        [alpha._cache.path.name, beta._cache.path.name])
    loopback.requests.clear()
    _script(loopback, (404, b"", [], False, False))
    alpha_fallback = alpha.refresh(now=20)
    assert _acquisition_metadata(alpha_fallback)["cached"] is True
    assert alpha_fallback["snapshot"]["tick_epoch"] == 0
    assert beta.current(now=20)["snapshot"]["tick_epoch"] == 120


def test_catalog_source_failures_and_io_discipline(loopback, tmp_path,
                                                   monkeypatch):
    fresh = _clock_snapshot(tick_epoch=0)
    fresh_raw = _snapshot_bytes(fresh)
    url = loopback.url("/catalog.json")
    fresh_path = tmp_path / "fresh.json"
    fresh_path.write_bytes(fresh_raw)

    # -- (a) failure without a valid capture is a STRUCTURED source error -----
    tampered = _scratch_snapshot()
    tampered["snapshot_id"] = "0" * 64
    failure_rows = (
        ("404", (404, b"", [], False, False)),
        ("429", (429, b"", [], False, False)),
        ("500", (500, b"", [], True, False)),
        ("unsolicited-304", (304, b"", [], False, False)),
        ("invalid-json", (200, b'{"schema_version": 1, ', [], True, False)),
        ("tampered-body", (200, json.dumps(tampered).encode("utf-8"),
                           [], True, False)),
    )
    for label, response in failure_rows:
        client = source.CatalogSource(
            "url", url, cache_dir=tmp_path / ("failure-" + label))
        loopback.requests.clear()
        _script(loopback, response)
        assert client.refresh(now=1) == _error_envelope(), label
        assert len(loopback.requests) == 1, label
        assert client.current(now=1) == _error_envelope(), label
        assert not (tmp_path / ("failure-" + label)).exists(), label

    # -- (b) a corrupt/foreign/old DISK record is a MISS, never an empty ok ---
    old_schema = _scratch_snapshot()
    old_schema["schema_version"] = 2
    record_rows = (
        ("corrupt", b"{not json"),
        ("foreign", catalog.canonical_bytes(_cache_document(
            _scratch_snapshot(),
            identity=_identity("url", "https://other.test/catalog.json")))),
        ("old-version", catalog.canonical_bytes(_cache_document(
            _scratch_snapshot(), identity=_identity("url", url), version=0))),
        ("old-schema", catalog.canonical_bytes(_cache_document(
            old_schema, identity=_identity("url", url)))),
        ("tampered-record", catalog.canonical_bytes(_cache_document(
            tampered, identity=_identity("url", url)))),
    )
    for label, payload in record_rows:
        root = tmp_path / ("record-" + label)
        entry = source.DiskCache("url", url, cache_dir=root)
        entry.path.parent.mkdir(parents=True)
        entry.path.write_bytes(payload)
        client = source.CatalogSource("url", url, cache_dir=root)
        loopback.requests.clear()
        _script(loopback, (500, b"", [], True, False))
        assert client.refresh(now=1) == _error_envelope(), label
        assert entry.path.read_bytes() == payload, label   # never repaired
        assert _temp_leftovers(root) == [], label

    # -- (c) failed FILE selection stays a structured error -------------------
    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_bytes(b"not a catalogue")
    as_directory = tmp_path / "as-directory"
    as_directory.mkdir()
    file_rows = (
        ("missing", tmp_path / "missing.json"),
        ("corrupt", corrupt_file),
        ("directory", as_directory),
    )
    for label, selected in file_rows:
        client = source.CatalogSource(
            "file", selected, cache_dir=tmp_path / ("file-" + label))
        assert client.refresh(now=1) == _error_envelope(), label
        assert client.current(now=1) == _error_envelope(), label

    # -- (d) a legitimately EMPTY snapshot still succeeds and can fall back ---
    empty = _empty_snapshot()
    empty_path = tmp_path / "empty.json"
    empty_path.write_bytes(_snapshot_bytes(empty))
    empty_client = source.CatalogSource(
        "file", empty_path, cache_dir=tmp_path / "empty-cache")
    served = empty_client.refresh(now=1759200010)
    assert served["ok"] is True
    assert served["snapshot"]["provider_order"] == []
    assert served["snapshot"]["providers"] == {}
    assert served["source"]["warnings"] == []
    empty_path.unlink()
    empty_fallback = empty_client.refresh(now=1759200020)
    metadata = _acquisition_metadata(empty_fallback)
    assert empty_fallback["snapshot"]["provider_order"] == []
    assert empty_fallback["snapshot"]["snapshot_id"] == empty["snapshot_id"]
    assert metadata["warnings"] == ["refresh_failed", "cache_fallback"]

    # -- (e) no safe default root / unavailable explicit root: warning only ---
    with pytest.MonkeyPatch.context() as scope:
        scope.delenv("XDG_CACHE_HOME", raising=False)
        scope.delenv("HOME", raising=False)
        rootless = source.CatalogSource("file", fresh_path)
        assert rootless._cache.path is None
        rootless_result = rootless.refresh(now=5)
    metadata = _acquisition_metadata(rootless_result, snapshot=fresh)
    assert metadata["warnings"] == ["cache_write_failed"]
    unavailable = tmp_path / "unavailable-root"
    unavailable.write_bytes(b"file, not a directory")
    explicit = source.CatalogSource("file", fresh_path, cache_dir=unavailable)
    explicit_result = explicit.refresh(now=5)
    metadata = _acquisition_metadata(explicit_result, snapshot=fresh)
    assert metadata["warnings"] == ["cache_write_failed"]

    # -- (f) invalid `now` and configuration fail BEFORE any I/O --------------
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid input must never reach operational I/O")

    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "fetch_catalog_url", forbidden)
        scope.setattr(source, "load_catalog_file", forbidden)
        scope.setattr(source.DiskCache, "load", forbidden)
        scope.setattr(source.DiskCache, "store", forbidden)
        guarded = source.CatalogSource("url", url, cache_dir=tmp_path / "guard")
        for bad in (True, False, 1.0, "1", None, -1,
                    catalog.MAX_SAFE_INT + 1, -(10 ** 30)):
            for label, invoke in (("refresh", guarded.refresh),
                                  ("current", guarded.current)):
                with pytest.raises(ValueError) as excinfo:
                    invoke(now=bad)
                assert type(excinfo.value) is ValueError, (label, bad)
                assert str(excinfo.value) == NOW_ERROR, (label, bad)
        with pytest.raises(TypeError) as excinfo:
            source.CatalogSource(None, url)
        assert type(excinfo.value) is TypeError
        with pytest.raises(TypeError) as excinfo:
            source.CatalogSource("file", b"/tmp/catalogue.json")
        assert type(excinfo.value) is TypeError
        with pytest.raises(ValueError) as excinfo:
            source.CatalogSource("http", url)
        assert type(excinfo.value) is ValueError
        with pytest.raises(ValueError) as excinfo:
            source.CatalogSource("file", "")
        assert type(excinfo.value) is ValueError
        for bad in (0, -1, True, float("nan"), float("inf"), "10"):
            with pytest.raises(ValueError) as excinfo:
                source.CatalogSource("url", url, timeout_s=bad)
            assert str(excinfo.value) == TIMEOUT_ERROR
        for bad in (0, -1, True, 1.0, None):
            with pytest.raises(ValueError) as excinfo:
                source.CatalogSource("url", url, max_bytes=bad)
            assert str(excinfo.value) == HTTP_BYTE_LIMIT_ERROR
            with pytest.raises(ValueError) as excinfo:
                source.CatalogSource("file", fresh_path, max_bytes=bad)
            assert str(excinfo.value) == BYTE_LIMIT_ERROR
        assert calls == []
        assert not (tmp_path / "guard").exists()

    # -- (g) bootstrap happens ONCE per instance, and current() never bootstraps
    counted = {"load": 0}
    accepted_load = source.DiskCache.load

    def counting_load(self):
        counted["load"] += 1
        return accepted_load(self)

    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source.DiskCache, "load", counting_load)
        counted_client = source.CatalogSource(
            "file", fresh_path, cache_dir=tmp_path / "counted-cache")
        assert counted["load"] == 0
        assert counted_client.current(now=1) == _error_envelope()
        assert counted["load"] == 0
        counted_client.refresh(now=1)
        assert counted["load"] == 1
        counted_client.refresh(now=2)
        counted_client.current(now=2)
        assert counted["load"] == 1

    # -- (h) current()/with_source() perform ZERO operational I/O -------------
    quiet = source.CatalogSource("url", url, cache_dir=tmp_path / "quiet-cache")
    loopback.requests.clear()
    _script(loopback, (200, fresh_raw, [], True, False))
    acquired = quiet.refresh(now=60)
    requests_before = len(loopback.requests)
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "fetch_catalog_url", forbidden)
        scope.setattr(source, "load_catalog_file", forbidden)
        scope.setattr(source.DiskCache, "load", forbidden)
        scope.setattr(source.DiskCache, "store", forbidden)
        scope.setattr(urllib.request.OpenerDirector, "open", forbidden)
        view = quiet.current(now=60)
        attached = source.with_source(query.list_free_models(fresh, {}),
                                      acquired)
        repeated = quiet.current(now=60)
    assert _acquisition_metadata(view)["fetched_at"] == 60
    assert attached["source"] == acquired["source"]
    assert repeated["snapshot"] == fresh
    assert len(loopback.requests) == requests_before
    assert calls == []

    # -- (i) a failed refresh preserves last-good DISK bytes and metadata -----
    cache_file = quiet._cache.path
    stored_bytes = cache_file.read_bytes()
    loopback.requests.clear()
    _script(loopback, (429, b"", [], True, False))
    fallback = quiet.refresh(now=61)
    metadata = _acquisition_metadata(fallback, snapshot=fresh)
    assert metadata["fetched_at"] == 60
    assert metadata["fetch_age_s"] == 1
    assert metadata["warnings"] == [
        "refresh_failed", "cache_fallback", "tick_stale"]
    assert cache_file.read_bytes() == stored_bytes
    assert _temp_leftovers(cache_file.parent) == []

    # -- (j) disk retrieval metadata is preserved literally: future and float -
    for label, fetched_at in (("future", 1000), ("float", 5.5)):
        root = tmp_path / ("seed-" + label)
        seed = source.DiskCache("url", url, cache_dir=root)
        assert seed.store(fresh, fetched_at=fetched_at) is True
        client = source.CatalogSource("url", url, cache_dir=root)
        loopback.requests.clear()
        _script(loopback, (500, b"", [], True, False))
        recovered = client.refresh(now=10)
        metadata = _acquisition_metadata(recovered, snapshot=fresh)
        assert metadata["fetched_at"] == fetched_at, label
        assert type(metadata["fetched_at"]) is type(fetched_at), label
        assert metadata["observation_age_s"] == 10, label
        assert metadata["observation_fresh"] is True, label
        if label == "future":
            assert metadata["fetch_age_s"] is None
            assert metadata["warnings"] == [
                "refresh_failed", "cache_fallback", "fetch_time_future"]
        else:
            assert metadata["fetch_age_s"] == 4.5
            assert type(metadata["fetch_age_s"]) is float
            assert metadata["warnings"] == [
                "refresh_failed", "cache_fallback"]

    # -- (k) no implicit source switch, no private path or error-text leak ----
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "load_catalog_file", forbidden)
        url_client = source.CatalogSource(
            "url", url, cache_dir=tmp_path / "no-switch")
        loopback.requests.clear()
        _script(loopback, (404, b"", [], False, False))
        assert url_client.refresh(now=1) == _error_envelope()
    with pytest.MonkeyPatch.context() as scope:
        scope.setattr(source, "fetch_catalog_url", forbidden)
        file_client = source.CatalogSource(
            "file", fresh_path, cache_dir=tmp_path / "no-switch-file")
        assert file_client.refresh(now=5)["ok"] is True

    leak_root = tmp_path / "leak-cache"
    leaked = source.CatalogSource("file", fresh_path, cache_dir=leak_root)
    leaked_acquisition = leaked.refresh(now=5)
    rendered = json.dumps(leaked_acquisition, sort_keys=True)
    for secret in (str(tmp_path), str(fresh_path), fresh_path.name,
                   str(leak_root), leaked._cache.path.name):
        assert secret not in rendered
    assert leaked_acquisition["source"]["url"] is None
    missing_path = tmp_path / "hidden-secret-catalogue.json"
    hidden = source.CatalogSource("file", missing_path,
                                  cache_dir=tmp_path / "hidden-cache")
    hidden_error = hidden.refresh(now=1)
    assert hidden_error == _error_envelope()
    assert "hidden-secret" not in repr(hidden_error)
    assert str(tmp_path) not in repr(hidden_error)

    # -- (l) views are detached deep copies -----------------------------------
    detached = leaked.current(now=5)
    detached["snapshot"]["provider_order"].clear()
    detached["snapshot"]["providers"] = {}
    detached["source"]["warnings"].append("mutated")
    detached_source = detached["source"]
    detached_source["fetched_at"] = -1
    again = leaked.current(now=5)
    assert again["snapshot"] == fresh
    assert again["source"]["fetched_at"] == 5
    assert again["source"]["warnings"] == []
    assert leaked_acquisition["snapshot"] == fresh
