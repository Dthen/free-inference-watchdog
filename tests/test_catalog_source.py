"""Runtime tests for the validated local-FILE and bounded HTTP sources.

This module exercises only the small read-only source boundary:

    watchdog_catalog.source.MAX_SOURCE_BYTES
    watchdog_catalog.source.DEFAULT_CATALOG_URL
    watchdog_catalog.source.DEFAULT_TIMEOUT_S
    watchdog_catalog.source.SourceError
    watchdog_catalog.source.load_catalog_file   (unit 5a)
    watchdog_catalog.source.load_catalog_url    (unit 5b)

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

Cache, conditional GET, fallback/freshness wrappers, CLI wiring and async
refresh coalescing belong to later units and are NOT implemented, imported or
exercised here. Nothing in this module writes outside pytest's own scratch
directory.
"""

import hashlib
import http.client
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError as _HTTPError

import pytest

from watchdog_catalog import catalog, source

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
