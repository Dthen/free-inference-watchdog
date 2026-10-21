"""Runtime tests for the validated local catalogue-FILE source (unit 5a).

This module exercises ONLY the small read-only file boundary:

    watchdog_catalog.source.MAX_SOURCE_BYTES
    watchdog_catalog.source.SourceError
    watchdog_catalog.source.load_catalog_file

`load_catalog_file` is one explicit, byte-bounded binary read of an
operator-selected path whose bytes are decoded as strict UTF-8 and then handed
EXACTLY ONCE to the accepted ``watchdog_catalog.catalog.load_snapshot``
validator. The accepted validator -- not this layer -- owns JSON, schema and
hash truth, so a malformed document keeps its frozen core ``CatalogError``
code/message. Only transport-shaped faults (open/read/close failure, oversize,
invalid path or byte-limit configuration) become the fixed, non-reflective
``SourceError`` with code ``source_unavailable``.

The two behavioral groups below are deliberately unparametrized and share
small literal tables: group 1 covers accepted scratch-file loading (fixture
bytes copied verbatim, typed values, literal path preservation, delegation
identity, byte bounds); group 2 covers stable failures, pre-open type
rejection, preserved core errors and fresh-process import isolation.

Remote HTTP, cache/fallback/freshness wrappers and CLI wiring belong to later
units and are NOT implemented, imported or exercised here. Nothing in this
module writes outside pytest's own scratch directory, and no test touches the
network.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

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
