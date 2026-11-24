"""The native JSON CLI adapter (slice 1).

This file owns ONLY the thin adapter between process argv and the accepted
catalogue stack: argument shape, the exact forwarding boundary into the pure
query core, source selection, JSON emission and shell exit status. It is not a
second catalogue, query, source or MCP corpus -- semantic agreement is checked
against the SAME pure core and the SAME real file fixtures the other owners
use, and every row here is a representative adapter probe, not a new matrix.

The core-call recorder tests patch the real module attributes the adapter uses
(``watchdog_catalog.query.search_models`` and friends). That recorder is
sensitive: the bounded control below loads an isolated copy of the PUBLIC
adapter source with one supplied option dropped and requires the same
comparison to fail, so a forwarding assertion cannot pass vacuously.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from watchdog_catalog import catalog, cli, query, source

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "catalog"
SEARCH_FIXTURE = FIXTURES / "search-text.json"
LOOKUP_FIXTURE = FIXTURES / "lookup.json"

#: The genuine pure core, captured before the recorder fixture replaces the
#: module attribute, so "deferred to the core" can be evaluated for real.
_REAL_SEARCH_MODELS = query.search_models


# ---------------------------------------------------------------------------
# narrow doubles: an acquisition, an acquisition recorder, a wall clock
# ---------------------------------------------------------------------------


def _provenance(**overrides):
    block = {
        "mode": "file",
        "url": None,
        "cached": False,
        "fetched_at": 1759200100,
        "fetch_age_s": 23,
        "observation_age_s": 123,
        "observation_fresh": True,
        "not_modified": False,
        "warnings": [],
    }
    block.update(overrides)
    return block


def _acquisition(snapshot=None, **overrides):
    result = {"ok": True, "snapshot": snapshot, "source": _provenance()}
    result.update(overrides)
    return result


class _RecordingSource:
    """The narrow acquisition seam: ``aget`` once and no other behaviour."""

    def __init__(self, acquisition, log=None):
        self.acquisition = copy.deepcopy(acquisition)
        self.calls = 0
        self.log = log if log is not None else []

    async def aget(self):
        self.calls += 1
        self.log.append("acquire")
        return copy.deepcopy(self.acquisition)


class _InterruptingSource:
    """An interruption BEFORE any payload exists (no document emitted)."""

    def __init__(self):
        self.calls = 0

    async def aget(self):
        self.calls += 1
        raise KeyboardInterrupt


class _CoreRecorder:
    """Replacements for the three pure operations that record their arguments."""

    def __init__(self):
        self.calls = []

    def search_models(self, snapshot, arguments):
        self.calls.append(("search_models", snapshot, arguments))
        return {"ok": True, "tool": "search_models", "rows": [],
                "page_len": 0, "total_matches": 0, "limit": 0,
                "offset": 0, "next_offset": None}

    def get_model(self, snapshot, arguments):
        self.calls.append(("get_model", snapshot, arguments))
        return {"ok": True, "tool": "get_model", "endpoint": {},
                "snapshot_id": "recorded", "tick_epoch": 0,
                "provider_staleness": {}}

    def watchdog_status(self, snapshot, arguments, *, now):
        self.calls.append(("watchdog_status", snapshot, arguments, now))
        return {"ok": True, "tool": "watchdog_status", "warnings": []}


@pytest.fixture
def recorded(monkeypatch):
    """Install the recorder on the real core module the adapter imports."""
    recorder = _CoreRecorder()
    monkeypatch.setattr(query, "search_models", recorder.search_models)
    monkeypatch.setattr(query, "get_model", recorder.get_model)
    monkeypatch.setattr(query, "watchdog_status", recorder.watchdog_status)
    return recorder


@pytest.fixture
def owner(monkeypatch):
    """Capture the constructed source selection without reading anything."""

    class _Constructed:
        def __init__(self):
            self.selections = []
            self.result = _acquisition({"recorder": True})

        def __call__(self, kind, location, cache_dir):
            self.selections.append((kind, location, cache_dir))
            return _RecordingSource(self.result)

    constructed = _Constructed()
    monkeypatch.setattr(cli, "_construct_source", constructed)
    return constructed


def _expected_search(**overrides):
    # Omitted pagination is ABSENT from the mapping: the pure core owns its
    # 5 / 0 defaults, and an explicit ``None`` would be an invalid argument.
    arguments = {
        "query": None,
        "provider": None,
        "min_context_length": None,
        "input_modalities": None,
        "supported_parameters": None,
    }
    arguments.update(overrides)
    return arguments


def _search_call(recorder):
    calls = [call for call in recorder.calls if call[0] == "search_models"]
    assert len(calls) == 1, recorder.calls
    return calls[0]


# ---------------------------------------------------------------------------
# parser surface, help and forwarded argument shape
# ---------------------------------------------------------------------------


def test_help_is_deliberate_text_and_touches_no_source(monkeypatch, capsys):
    def exploding(*args, **kwargs):  # pragma: no cover - a leak reaches it
        raise AssertionError("help constructed a catalogue source")

    monkeypatch.setattr(cli, "_construct_source", exploding)
    monkeypatch.setattr(cli, "_wall_clock", exploding)

    with pytest.raises(SystemExit) as top:
        cli.main(["--help"])
    assert top.value.code == 0
    top_text = capsys.readouterr().out
    assert top_text.startswith("usage: free-inference-watchdog")
    for command in ("search", "get", "status"):
        assert command in top_text

    with pytest.raises(SystemExit) as sub:
        cli.main(["search", "--help"])
    assert sub.value.code == 0
    search_text = capsys.readouterr().out
    assert search_text.startswith("usage: free-inference-watchdog search")
    # argparse may wrap inside the mutually exclusive group on newer Python.
    help_words = " ".join(search_text.split())
    for fragment in ("--catalog-url URL | --catalog-file PATH", "--cache-dir DIR",
                     "--json", "--input-modality VALUE",
                     "--supported-parameter VALUE", "--min-context-length N"):
        assert fragment in help_words, (fragment, search_text[:1600])


def test_search_forwards_every_supplied_option_to_the_core(recorded, owner,
                                                           capsysbinary):
    exit_code = cli.main([
        "search", "ÉCHO literal", "--json",
        "--provider", "synthetic-a",
        "--min-context-length", "0",
        "--input-modality", "TEXT",
        "--input-modality", "Image",
        "--input-modality", "TEXT",
        "--supported-parameter", "Temperature",
        "--supported-parameter", "temperature",
        "--limit", "17",
        "--offset", "3",
    ])

    assert exit_code == 0
    _, snapshot, arguments = _search_call(recorded)
    assert snapshot == {"recorder": True}
    assert arguments == _expected_search(
        query="ÉCHO literal",
        provider="synthetic-a",
        min_context_length=0,
        input_modalities=["TEXT", "Image", "TEXT"],
        supported_parameters=["Temperature", "temperature"],
        limit=17,
        offset=3,
    )
    document = json.loads(capsysbinary.readouterr().out)
    assert document["ok"] is True
    assert document["source"] == _provenance()
    assert owner.selections == [
        ("url", source.DEFAULT_CATALOG_URL, None)]


def test_omitted_search_values_use_the_existing_defaults_not_new_ones(
        recorded, owner, capsysbinary):
    assert cli.main(["search", "--json"]) == 0
    _, _, omitted = _search_call(recorded)
    assert omitted == _expected_search()
    assert "limit" not in omitted and "offset" not in omitted, (
        "an omitted pagination flag must defer to the core, not a parser literal")

    recorded.calls.clear()
    assert cli.main(["search", "", "--json"]) == 0
    _, _, empty = _search_call(recorded)
    assert empty["query"] == ""
    assert empty["query"] is not None, (
        "an explicitly empty query is not the omitted filter")
    capsysbinary.readouterr()

    # The same omitted mapping, evaluated by the REAL core, still yields the
    # existing page of 5 at offset 0: deferral changes ownership, not results.
    ordering = catalog.load_snapshot(
        (FIXTURES / "ordering.json").read_text(encoding="utf-8"))
    deferred = _REAL_SEARCH_MODELS(ordering, omitted)
    explicit = _REAL_SEARCH_MODELS(ordering, {})
    assert deferred == explicit
    assert len(deferred["endpoints"]) == 5
    assert deferred["next_offset"] == 5
    assert deferred["total_matches"] == 10


def test_zero_and_literal_capability_lists_reach_the_core_unchanged(
        recorded, owner, capsysbinary):
    assert cli.main([
        "search", "--json", "--min-context-length", "0",
        "--input-modality", "text", "--input-modality", "TEXT",
        "--limit", "1", "--offset", "0",
    ]) == 0
    _, _, arguments = _search_call(recorded)
    assert arguments["min_context_length"] == 0
    assert arguments["input_modalities"] == ["text", "TEXT"]
    assert arguments["limit"] == 1 and arguments["offset"] == 0
    capsysbinary.readouterr()


def _load_public_source_copy(mutator):
    """Execute an isolated copy of the PUBLIC adapter source with one mutation."""
    text = (REPO_ROOT / "watchdog_catalog" / "cli.py").read_text(encoding="utf-8")
    mutated = mutator(text)
    assert mutated != text, "the control mutation changed nothing"
    module = types.ModuleType("watchdog_catalog._cli_control")
    module.__file__ = str(REPO_ROOT / "watchdog_catalog" / "cli.py")
    module.__package__ = "watchdog_catalog"
    sys.modules[module.__name__] = module
    try:
        exec(compile(mutated, "<cli-control>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)
    return module


def test_forwarding_omission_control_is_caught_by_the_same_recorder(
        recorded, monkeypatch):
    """Positive control for the forwarding assertions above.

    The isolated copy drops the supplied ``supported_parameters`` member, so
    the SAME exact-arguments comparison must reject it. Without this control a
    recorder assertion could pass while a whole option silently vanished.
    """
    control = _load_public_source_copy(lambda text: text.replace(
        '        "supported_parameters": arguments.supported_parameter,\n',
        ""))

    def construct(kind, location, cache_dir):
        return _RecordingSource(_acquisition({"control": True}))

    monkeypatch.setattr(control, "_construct_source", construct)
    assert control.main([
        "search", "x", "--provider", "synthetic-a",
        "--supported-parameter", "temperature", "--json",
    ]) == 0
    _, _, arguments = _search_call(recorded)
    with pytest.raises(AssertionError):
        assert arguments == _expected_search(
            query="x", provider="synthetic-a",
            supported_parameters=["temperature"]), (
            "the dropped supplied option must fail the exact comparison")


def test_get_forwards_the_required_ids_exactly(recorded, owner, capsysbinary):
    assert cli.main(["get", "synthetic-a", "vendor/Model:free",
                     "--json"]) == 0
    calls = [call for call in recorded.calls if call[0] == "get_model"]
    assert len(calls) == 1, recorded.calls
    assert calls[0][2] == {"provider": "synthetic-a",
                           "model_id": "vendor/Model:free"}
    assert owner.selections == [("url", source.DEFAULT_CATALOG_URL, None)]
    capsysbinary.readouterr()


def test_missing_get_ids_fail_before_any_acquisition(owner, recorded,
                                                     capsysbinary):
    assert cli.main(["get", "synthetic-a", "--json"]) == 2
    assert owner.selections == []
    assert recorded.calls == []
    document = json.loads(capsysbinary.readouterr().out)
    assert document == {"ok": False,
                        "error": {"code": "invalid_arguments",
                                  "message": cli.INVALID_ARGUMENTS_MESSAGE}}
    assert "source" not in document


def test_source_selection_is_literal_and_the_default_feed_is_exact(
        monkeypatch, capsysbinary):
    seen = []

    def construct(kind, location, cache_dir):
        seen.append((kind, location, cache_dir))
        return _RecordingSource(_acquisition({"recorder": True}))

    monkeypatch.setattr(cli, "_construct_source", construct)
    monkeypatch.setattr(query, "search_models",
                        lambda snapshot, arguments: {"ok": True, "rows": []})

    assert cli.main(["search", "--json"]) == 0
    assert cli.main(["search", "--catalog-url", "https://example.test/x.json",
                     "--cache-dir", "rel-cache", "--json"]) == 0
    assert cli.main(["search", "--catalog-file", "rel/catalog.json",
                     "--json"]) == 0
    assert seen == [
        ("url", source.DEFAULT_CATALOG_URL, None),
        ("url", "https://example.test/x.json", "rel-cache"),
        ("file", "rel/catalog.json", None),
    ]
    capsysbinary.readouterr()


# ---------------------------------------------------------------------------
# structural agreement with the accepted pure core over a real snapshot file
# ---------------------------------------------------------------------------


def _document(capsysbinary):
    raw = capsysbinary.readouterr()
    assert raw.err == b""
    return json.loads(raw.out)


def test_search_json_agrees_with_the_pure_core_on_a_real_file_snapshot(
        tmp_path, capsysbinary):
    arguments = {"query": "reasoning", "provider": "synthetic-a",
                 "min_context_length": 0,
                 "input_modalities": ["TEXT", "Image"],
                 "supported_parameters": ["temperature"],
                 "limit": 5, "offset": 0}
    assert cli.main([
        "search", "reasoning", "--json",
        "--catalog-file", str(SEARCH_FIXTURE),
        "--cache-dir", str(tmp_path / "cache"),
        "--provider", "synthetic-a",
        "--min-context-length", "0",
        "--input-modality", "TEXT",
        "--input-modality", "Image",
        "--supported-parameter", "temperature",
    ]) == 0

    document = _document(capsysbinary)
    snapshot = catalog.load_snapshot(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    source_block = document.pop("source")
    assert source_block["mode"] == "file"
    assert source_block["cached"] is False
    assert document == query.search_models(snapshot, arguments)
    assert document["ok"] is True and document["total_matches"] >= 1
    assert document["endpoints"][0]["model_id"] == "vendor/literal[ab].*"


def test_get_json_agrees_with_the_pure_core_and_keeps_exact_ids(
        tmp_path, capsysbinary):
    assert cli.main([
        "get", "synthetic-a", "vendor/shared", "--json",
        "--catalog-file", str(LOOKUP_FIXTURE),
        "--cache-dir", str(tmp_path / "cache"),
    ]) == 0
    document = _document(capsysbinary)
    snapshot = catalog.load_snapshot(LOOKUP_FIXTURE.read_text(encoding="utf-8"))
    assert document.pop("source")["mode"] == "file"
    assert document == query.get_model(
        snapshot, {"provider": "synthetic-a", "model_id": "vendor/shared"})
    assert document["endpoint"]["model_id"] == "vendor/shared"


def test_status_json_agrees_with_the_pure_core_and_reads_the_clock_after_acquiring(
        tmp_path, monkeypatch, capsysbinary):
    log = []
    real = source.AsyncCatalogSource(
        "file", str(SEARCH_FIXTURE), cache_dir=str(tmp_path / "cache"))

    class _LoggedAcquisition:
        async def aget(self):
            log.append("acquire")
            return await real.aget()

    monkeypatch.setattr(cli, "_construct_source",
                        lambda kind, location, cache_dir: _LoggedAcquisition())

    def clock():
        log.append("clock")
        return 1759200123.9

    monkeypatch.setattr(cli, "_wall_clock", clock)
    real_status = query.watchdog_status
    status_calls = []

    def counted_status(snapshot, arguments, *, now):
        status_calls.append((snapshot, arguments, now))
        return real_status(snapshot, arguments, now=now)

    monkeypatch.setattr(query, "watchdog_status", counted_status)
    assert cli.main(["status", "--json",
                     "--catalog-file", str(SEARCH_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache")]) == 0

    assert log == ["acquire", "clock"], (
        "the whole-second epoch is captured once, after acquisition")
    # The source layer also consults the shared core for observation status, so
    # count only the adapter's own call by its captured reference time.
    adapter_calls = [call for call in status_calls if call[2] == 1759200123]
    assert len(adapter_calls) == 1, "exactly one pure operation ran"
    assert adapter_calls[0][1] == {}
    document = _document(capsysbinary)
    snapshot = catalog.load_snapshot(SEARCH_FIXTURE.read_text(encoding="utf-8"))
    assert document.pop("source")["mode"] == "file"
    assert document == real_status(snapshot, {}, now=1759200123)
    assert document["last_tick_age_s"] == 1759200123 - 1759200000


def test_a_failed_acquisition_is_returned_without_a_query_or_clock_read(
        tmp_path, monkeypatch, recorded, capsysbinary):
    failure = {"ok": False,
               "error": {"code": "source_unavailable",
                         "message": "Catalogue source unavailable."}}
    owner = _RecordingSource(failure)
    monkeypatch.setattr(cli, "_construct_source",
                        lambda kind, location, cache_dir: owner)

    def exploding_clock():  # pragma: no cover - a leak reaches it
        raise AssertionError("status read a clock without a snapshot")

    monkeypatch.setattr(cli, "_wall_clock", exploding_clock)
    assert cli.main(["status", "--json",
                     "--catalog-file", str(tmp_path / "absent.json"),
                     "--cache-dir", str(tmp_path / "cache")]) == 1
    assert recorded.calls == [], "a failed acquisition queried no snapshot"
    document = _document(capsysbinary)
    assert document == failure
    assert "snapshot" not in document


# ---------------------------------------------------------------------------
# exit status and non-reflective error boundaries
# ---------------------------------------------------------------------------


def test_exit_status_follows_the_structured_result(tmp_path, monkeypatch,
                                                   capsysbinary):
    # invalid_arguments from the core (an explicit limit of zero is not a
    # parser failure: the core owns the 1..200 rule).
    assert cli.main(["search", "--limit", "0", "--json",
                     "--catalog-file", str(SEARCH_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache")]) == 2
    assert json.loads(capsysbinary.readouterr().out)["error"] == {
        "code": "invalid_arguments",
        "message": "Invalid tool arguments.",
        "parameter": "limit"}

    # unknown_provider keeps its own core code and parameter.
    assert cli.main(["get", "nope", "vendor/shared", "--json",
                     "--catalog-file", str(LOOKUP_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache")]) == 2
    document = json.loads(capsysbinary.readouterr().out)
    assert document["error"] == {"code": "unknown_provider",
                                 "message": "Unknown provider.",
                                 "parameter": "provider"}
    assert "source" in document, "a successful acquisition keeps provenance"

    # not_found exits 1 and echoes no caller value.
    assert cli.main(["get", "synthetic-a", "vendor/absent", "--json",
                     "--catalog-file", str(LOOKUP_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache")]) == 1
    document = json.loads(capsysbinary.readouterr().out)
    assert document.pop("source")["mode"] == "file"
    assert document == {"ok": False,
                        "error": {"code": "not_found",
                                  "message": "Model not found."}}

    # no matches is still a success.
    assert cli.main(["search", "no-such-token-anywhere", "--json",
                     "--catalog-file", str(SEARCH_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache")]) == 0
    document = json.loads(capsysbinary.readouterr().out)
    assert document["ok"] is True and document["total_matches"] == 0

    # an unavailable source is exit 1 and never an empty catalogue.
    assert cli.main(["search", "--json",
                     "--catalog-file", str(tmp_path / "absent.json"),
                     "--cache-dir", str(tmp_path / "cache")]) == 1
    document = json.loads(capsysbinary.readouterr().out)
    assert document["error"]["code"] == "source_unavailable"


def test_cli_syntax_and_configuration_errors_are_fixed_and_non_reflective(
        owner, recorded, capsysbinary):
    secret = "/private/SECRET-catalogue?token=abc"
    hostile = [
        ["search", "--all"],
        ["search", "--limit", "not-a-number"],
        ["search", "--limit", "1_0"],
        ["search", "--offset", "-1"],
        ["search", "--catalog-file", "a.json", "--catalog-url", "https://b"],
        ["search", "--provider", "synthetic-a", "extra", "positional"],
        ["frobnicate"],
        ["get"],
    ]
    for argv in hostile:
        assert cli.main(argv + ["--json"]) == 2, argv
        raw = capsysbinary.readouterr()
        assert raw.err == b""
        document = json.loads(raw.out)
        assert document == {"ok": False,
                            "error": {"code": "invalid_arguments",
                                      "message": cli.INVALID_ARGUMENTS_MESSAGE}}
        assert "source" not in document
        assert secret not in raw.out.decode("utf-8")
    assert owner.selections == [], "a fixed CLI error acquired a source"
    assert recorded.calls == []

    # The same failure in human mode is a stderr diagnostic only.
    assert cli.main(["search", "--all"]) == 2
    raw = capsysbinary.readouterr()
    assert raw.out == b""
    assert raw.err.strip() == (
        f"free-inference-watchdog: {cli.INVALID_ARGUMENTS_MESSAGE}").encode()


def test_source_configuration_failure_is_a_fixed_invalid_arguments_error(
        capsysbinary):
    hostile = "ftp://user:SECRET@example.test/catalog.json"
    assert cli.main(["search", "--catalog-url", hostile, "--json"]) == 2
    raw = capsysbinary.readouterr()
    assert raw.err == b""
    document = json.loads(raw.out)
    assert document == {"ok": False,
                        "error": {"code": "invalid_arguments",
                                  "message": cli.INVALID_SOURCE_MESSAGE}}
    assert "SECRET" not in raw.out.decode("utf-8")


def test_json_is_a_standalone_selector_only_before_the_double_dash(
        tmp_path, capsysbinary):
    common = ["--catalog-file", str(SEARCH_FIXTURE),
              "--cache-dir", str(tmp_path / "cache")]

    # After `--`, `--json` is the literal positional query, not a selector;
    # the explicit selector before the operands keeps this a JSON case.
    assert cli.main(["search", "--json", *common, "--", "--json"]) == 0
    document = json.loads(capsysbinary.readouterr().out)
    assert document["ok"] is True and document["total_matches"] == 0

    # `--option=--json` is a literal value, so the human error boundary runs
    # (the core rejects the unknown provider; `--json` was not a selector).
    assert cli.main(["search", "--provider=--json", *common]) == 2
    raw = capsysbinary.readouterr()
    assert raw.out == b""
    assert raw.err == b"free-inference-watchdog: Unknown provider.\n"

    # A standalone `--json` before `--` selects JSON even when parsing fails.
    assert cli.main(["search", "--json", "--provider"]) == 2
    document = json.loads(capsysbinary.readouterr().out)
    assert document["error"]["code"] == "invalid_arguments"


def test_the_literal_string_null_is_not_a_magic_cli_value(
        tmp_path, capsysbinary):
    assert cli.main(["search", "--provider", "null", "--json",
                     "--catalog-file", str(SEARCH_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache")]) == 2
    document = json.loads(capsysbinary.readouterr().out)
    assert document["error"]["code"] == "unknown_provider"

    assert cli.main(["get", "synthetic-a", "null", "--json",
                     "--catalog-file", str(LOOKUP_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache")]) == 1
    document = json.loads(capsysbinary.readouterr().out)
    assert document["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# stream concerns belong to the adapter
# ---------------------------------------------------------------------------


def _fail_after_write(exception):
    written = []

    class _Stream:
        buffer = types.SimpleNamespace(write=None)

        def __init__(self):
            self.buffer.write = self._write

        def _write(self, data):
            written.append(bytes(data))
            raise exception

    return _Stream(), written


def test_output_failure_returns_one_without_a_replacement_document(
        tmp_path, monkeypatch):
    # An explicit local fixture source: this case is about the stream boundary,
    # not about (and never dependent on) the default remote feed.
    offline = ["--catalog-file", str(LOOKUP_FIXTURE),
               "--cache-dir", str(tmp_path / "cache")]
    for failure in (BrokenPipeError(), ValueError("I/O operation on closed file"),
                    OSError("stdout is gone")):
        stream, written = _fail_after_write(failure)
        monkeypatch.setattr(sys, "stdout", stream)
        assert cli.main(["status", *offline, "--json"]) == 1
        assert len(written) == 1, (
            "one attempted document, never a replacement after the failure")

    monkeypatch.setattr(sys, "stdout", None)
    assert cli.main(["status", *offline, "--json"]) == 1

    # The human rendering shares the same one-document output boundary.
    stream, written = _fail_after_write(OSError("stdout is gone"))
    monkeypatch.setattr(sys, "stdout", stream)
    assert cli.main(["status", "--catalog-file", str(LOOKUP_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache")]) == 1
    assert len(written) == 1, (
        "one attempted human rendering, never a replacement document")


def test_an_interruption_before_emission_returns_130_with_empty_stdout(
        monkeypatch, capsysbinary):
    monkeypatch.setattr(cli, "_construct_source",
                        lambda kind, location, cache_dir: _InterruptingSource())
    assert cli.main(["status", "--json"]) == 130
    raw = capsysbinary.readouterr()
    assert raw.out == b"" and raw.err == b""


def test_an_interruption_after_emission_begins_adds_no_second_document(
        tmp_path, monkeypatch):
    class _InterruptingStream:
        def __init__(self):
            self.written = b""
            self.buffer = types.SimpleNamespace(write=self._write)

        def _write(self, data):
            self.written += bytes(data)
            raise KeyboardInterrupt

    stream = _InterruptingStream()
    monkeypatch.setattr(sys, "stdout", stream)
    # Local fixture source: the interruption boundary, not the default feed.
    assert cli.main(["status", "--catalog-file", str(LOOKUP_FIXTURE),
                     "--cache-dir", str(tmp_path / "cache"), "--json"]) == 130
    assert stream.written.count(b"\n") == 1, (
        "one document was begun; no replacement was appended")
    assert b'"ok"' in stream.written, "output had begun before the interruption"


def test_diagnostics_failing_preserves_the_classified_error(
        tmp_path, monkeypatch, capsysbinary):
    class _BrokenDiagnostics:
        def write(self, data):
            raise OSError("stderr is gone")

        def flush(self):  # pragma: no cover - write fails first
            raise OSError("stderr is gone")

    monkeypatch.setattr(sys, "stderr", _BrokenDiagnostics())
    # human-mode source failure: the diagnostic cannot be delivered, but the
    # classified exit status must survive instead of becoming a traceback.
    assert cli.main(["search", "--catalog-file",
                     str(tmp_path / "absent.json"),
                     "--cache-dir", str(tmp_path / "cache")]) == 1
    raw = capsysbinary.readouterr()
    assert raw.out == b""

    # JSON-mode output failure with broken diagnostics keeps the error class.
    monkeypatch.setattr(sys, "stdout", None)
    assert cli.main(["search", "--json", "--catalog-file",
                     str(tmp_path / "absent.json"),
                     "--cache-dir", str(tmp_path / "cache")]) == 1


# ---------------------------------------------------------------------------
# human rendering: ONE rendering of the SAME result; --json is unchanged
# ---------------------------------------------------------------------------


def _fixture_snapshot(name):
    return catalog.load_snapshot((FIXTURES / name).read_text(encoding="utf-8"))


def _sourced(payload, **source_overrides):
    """Attach a normal source block through the REAL production attachment."""
    return source.with_source(
        payload,
        {"ok": True, "snapshot": {},
         "source": _provenance(**source_overrides)})


def _human(document, command):
    return cli._render_human(document, command)


def test_human_search_is_the_default_and_renders_every_row_field(
        tmp_path, capsysbinary):
    fixture = FIXTURES / "capability-unknown.json"
    assert cli.main([
        "search", "--catalog-file", str(fixture),
        "--cache-dir", str(tmp_path / "cache"), "--limit", "2",
    ]) == 0
    raw = capsysbinary.readouterr()
    assert raw.err == b""
    text = raw.out.decode("utf-8")
    assert text.startswith("search:"), text[:200]
    assert not text.lstrip().startswith("{"), "default output is not JSON"

    document = query.search_models(
        _fixture_snapshot("capability-unknown.json"), {"limit": 2})
    assert "total_matches=2" in text
    for row in document["endpoints"]:
        context = ("unknown" if row["context_length"] is None
                   else row["context_length"])
        assert f"provider={row['provider']}" in text
        assert f"model_id={row['model_id']}" in text
        assert f"name={row['name']}" in text
        assert f"context_length={context}" in text

    # Unknown and known-empty are distinct, and a known zero stays a zero.
    assert "context_length=0" in text
    assert "context_length=unknown" in text
    assert "input_modalities=none (known empty)" in text
    assert "input_modalities=unknown" in text
    assert "supported_parameters=none (known empty)" in text
    assert "next_offset=none" in text
    assert "source: mode=file" in text
    assert "source warnings: metadata_missing" in text

    # Row order is the returned order, never re-sorted for display.
    positions = [text.index(f"model_id={row['model_id']}")
                 for row in document["endpoints"]]
    assert positions == sorted(positions)


def test_human_search_promises_the_next_page_from_the_returned_offsets(
        tmp_path, capsysbinary):
    assert cli.main([
        "search", "--catalog-file", str(FIXTURES / "ordering.json"),
        "--cache-dir", str(tmp_path / "cache"), "--limit", "2",
    ]) == 0
    text = capsysbinary.readouterr().out.decode("utf-8")
    assert "total_matches=10" in text
    assert "next_offset=2" in text
    assert "continue with --offset 2" in text


def test_human_search_with_no_matches_is_explicit_and_still_shows_freshness(
        tmp_path, capsysbinary):
    assert cli.main([
        "search", "no-such-token-anywhere",
        "--catalog-file", str(SEARCH_FIXTURE),
        "--cache-dir", str(tmp_path / "cache"),
    ]) == 0
    raw = capsysbinary.readouterr()
    assert raw.err == b""
    text = raw.out.decode("utf-8")
    assert "total_matches=0" in text
    assert "no matching endpoints" in text
    assert "next_offset=none" in text
    assert "source: mode=file" in text


def test_human_get_renders_all_details_description_and_wiring(
        tmp_path, capsysbinary):
    assert cli.main([
        "get", "synthetic-a", "vendor/shared",
        "--catalog-file", str(LOOKUP_FIXTURE),
        "--cache-dir", str(tmp_path / "cache"),
    ]) == 0
    raw = capsysbinary.readouterr()
    assert raw.err == b""
    text = raw.out.decode("utf-8")
    assert text.startswith("get: provider=synthetic-a model_id=vendor/shared")
    for fragment in (
        "description=Synthetic-a scenario for the shared raw id: large 131072 "
        "context but text-only input.",
        "context_length=131072",
        "input_modalities=text",
        "output_modalities=text",
        "supported_parameters=temperature, max_tokens",
        "chat_completions_url=https://synthetic-alpha.example/v1/chat/completions",
        "api_type=openai",
        "snapshot: ",
        "tick_epoch=1759200000",
        "provider staleness: synthetic-a=no",
        "source: mode=file",
        "source warnings:",
    ):
        assert fragment in text, (fragment, text[:2000])

    # A null description is unknown, never an invented empty string.
    unknown = _human(
        _sourced(query.get_model(
            _fixture_snapshot("capability-unknown.json"),
            {"provider": "synthetic-a",
             "model_id": "vendor/unknown-capabilities"})),
        "get")
    assert "description=unknown" in unknown


def test_human_get_distinguishes_a_known_empty_description_from_unknown(
        ):
    document = _sourced(query.get_model(
        _fixture_snapshot("lookup.json"),
        {"provider": "synthetic-a", "model_id": "vendor/shared"}))
    before = copy.deepcopy(document)
    document["endpoint"]["description"] = ""
    text = _human(document, "get")
    assert "description=none (known empty)" in text
    assert "description=unknown" not in text
    assert before["endpoint"]["description"] != ""


def test_human_status_shows_nested_health_counts_coverage_and_warnings(
        tmp_path, monkeypatch, capsysbinary):
    monkeypatch.setattr(cli, "_wall_clock", lambda: 1759200100)
    assert cli.main([
        "status", "--catalog-file", str(FIXTURES / "status-short-cadence.json"),
        "--cache-dir", str(tmp_path / "cache"),
    ]) == 0
    raw = capsysbinary.readouterr()
    assert raw.err == b""
    text = raw.out.decode("utf-8")
    assert text.startswith("status: snapshot=")
    for fragment in (
        "tick_epoch=1759200000 cadence_s=120 last_tick_age_s=100 tick_fresh=yes",
        "counts: gateways=2 endpoints=2 distinct_raw_ids=2 "
        "marker_group_labels=2",
        "metadata_coverage: endpoints=2 context_length_known=1 "
        "input_modalities_known=1 output_modalities_known=1 "
        "supported_parameters_known=1 description_known=1",
        "providers:",
        "synthetic-a endpoints=2 stale=no",
        "synthetic-b endpoints=0 stale=yes",
        "warnings: metadata_missing",
        "pending_alerts=unknown site_published=unknown "
        "site_publish_age_s=unknown",
        "source: mode=file",
    ):
        assert fragment in text, (fragment, text[:2400])

    # Unknown observation time stays unknown rather than becoming zero.
    monkeypatch.setattr(cli, "_wall_clock", lambda: 1759200100)
    assert cli.main([
        "status", "--catalog-file", str(FIXTURES / "status-unknown-time.json"),
        "--cache-dir", str(tmp_path / "cache"),
    ]) == 0
    unknown = capsysbinary.readouterr().out.decode("utf-8")
    assert "last_tick_age_s=unknown" in unknown
    assert "tick_fresh=no" in unknown
    assert "warnings: tick_epoch_unknown, metadata_missing" in unknown


def test_human_freshness_and_cache_fallback_are_visible_once_not_per_row(
        ):
    document = _sourced(
        query.search_models(_fixture_snapshot("capability-unknown.json"), {}),
        cached=True, fetch_age_s=None, observation_age_s=None,
        observation_fresh=False, not_modified=True,
        warnings=["refresh_failed", "cache_fallback"])
    text = _human(document, "search")
    for fragment in (
        "cached=yes",
        "fetch_age_s=unknown",
        "observation_age_s=unknown",
        "observation_fresh=no",
        "not_modified=yes",
        "source warnings: refresh_failed, cache_fallback",
    ):
        assert fragment in text, fragment
    assert len(document["endpoints"]) == 2
    assert text.count("cached=") == 1, "provenance is shown once, not per row"
    assert text.count("source warnings:") == 1


def test_human_rendering_never_mutates_the_result_document():
    documents = [
        ("search", _sourced(query.search_models(
            _fixture_snapshot("search-text.json"), {"query": "reasoning"}))),
        ("get", _sourced(query.get_model(
            _fixture_snapshot("lookup.json"),
            {"provider": "synthetic-a", "model_id": "vendor/shared"}))),
        ("status", _sourced(query.watchdog_status(
            _fixture_snapshot("lookup.json"), {}, now=1759200100))),
    ]
    for command, document in documents:
        before = copy.deepcopy(document)
        rendered = _human(document, command)
        assert document == before, command
        assert json.loads(json.dumps(document)) == before, command
        assert rendered.strip(), command


def test_human_rendering_escapes_terminal_controls_in_values_and_labels():
    document = _sourced(query.search_models(
        _fixture_snapshot("search-text.json"), {"query": "reasoning"}))
    clean = _human(document, "search")
    hostile = copy.deepcopy(document)
    row = hostile["endpoints"][0]
    row["provider"] = "evil\x1b[2Jprovider"
    row["model_id"] = "vendor/\x9bmodel\x07"
    row["name"] = "line1\nline2\x1b]0;pwn\x07\x00FAKE"
    text = _human(hostile, "search")

    for control in ("\x1b", "\x00", "\x07", "\x9b"):
        assert control not in text, repr(control)
    assert "\\x1b[2J" in text
    assert "\\x9bmodel\\x07" in text
    assert "\\x1b]0;pwn\\x07\\x00FAKE" in text
    assert "line1\\x0aline2" in text
    assert text.count("\n") == clean.count("\n"), (
        "a hostile value injected extra display lines")
    # The data itself is only escaped for display, never rewritten.
    assert hostile["endpoints"][0]["name"].startswith("line1\nline2\x1b")


def test_json_output_after_the_default_change_is_still_the_same_document(
        tmp_path, capsysbinary):
    argv = ["search", "reasoning", "--catalog-file", str(SEARCH_FIXTURE),
            "--cache-dir", str(tmp_path / "cache"), "--limit", "1"]
    assert cli.main(argv) == 0
    human = capsysbinary.readouterr()
    assert human.out.startswith(b"search:")

    assert cli.main(argv + ["--json"]) == 0
    raw = capsysbinary.readouterr()
    assert raw.err == b""
    document = json.loads(raw.out)
    snapshot = _fixture_snapshot("search-text.json")
    assert document.pop("source")["mode"] == "file"
    assert document == query.search_models(
        snapshot, {"query": "reasoning", "limit": 1})
    assert document["endpoints"][0]["model_id"] == "vendor/literal[ab].*"


# ---------------------------------------------------------------------------
# one genuine fresh-process case: inert import, help and real UTF-8 bytes
# ---------------------------------------------------------------------------


_IMPORT_AND_HELP_PROBE = r"""
import importlib.abc
import sys

repo = sys.argv[1]
blocked = {"mcp", "pydantic"}

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            raise ImportError("blocked import during CLI probe: " + fullname)
        return None

sys.meta_path.insert(0, Blocker())
sys.path.insert(0, repo)
import watchdog_catalog.cli as cli
assert "watchdog_catalog.server" not in sys.modules
assert blocked.isdisjoint({name.split(".")[0] for name in sys.modules})
try:
    cli.main(["--help"])
except SystemExit as stop:
    assert stop.code == 0
else:
    raise AssertionError("--help did not exit")
"""


def test_native_cli_process_is_sdk_free_and_emits_real_utf8_bytes(tmp_path):
    python = os.environ.get("FREE_INFERENCE_WATCHDOG_BUILD_PYTHON",
                            sys.executable)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "ascii",
    }
    probe = subprocess.run(
        [python, "-B", "-c", _IMPORT_AND_HELP_PROBE, str(REPO_ROOT)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120,
        env=environment)
    assert probe.returncode == 0, probe.stderr[-4000:]
    assert probe.stdout.startswith("usage: free-inference-watchdog")
    assert probe.stderr == ""

    run = subprocess.run(
        [python, "-B", "-m", "watchdog_catalog.cli", "search", "reasoning",
         "--catalog-file", str(SEARCH_FIXTURE),
         "--cache-dir", str(tmp_path / "cache"),
         "--limit", "1", "--json"],
        cwd=str(tmp_path), capture_output=True, timeout=120, env=environment)
    assert run.returncode == 0, run.stderr[-4000:]
    assert run.stderr == b""
    text = run.stdout.decode("utf-8")
    assert "ÉCHO TEXT".encode("utf-8") in run.stdout, (
        "the JSON document is UTF-8 regardless of the process locale")
    document = json.loads(text)
    assert document["ok"] is True
    assert document["total_matches"] == 1
    assert document["endpoints"][0]["name"] == "ÉCHO TEXT"
    assert document["endpoints"][0]["model_id"] == "vendor/literal[ab].*"

    human = subprocess.run(
        [python, "-B", "-m", "watchdog_catalog.cli", "search", "reasoning",
         "--catalog-file", str(SEARCH_FIXTURE),
         "--cache-dir", str(tmp_path / "cache"), "--limit", "1"],
        cwd=str(tmp_path), capture_output=True, timeout=120, env=environment)
    assert human.returncode == 0, human.stderr[-4000:]
    assert human.stderr == b""
    assert human.stdout.startswith(b"search:")
    assert "ÉCHO TEXT".encode("utf-8") in human.stdout, (
        "the human rendering is UTF-8 regardless of the process locale")
    assert b"model_id=vendor/literal[ab].*" in human.stdout


# ---------------------------------------------------------------------------
# native shutdown / diagnostic boundaries: real processes, real descriptors
# ---------------------------------------------------------------------------


def _native_environment(tmp_path):
    """A hermetic child environment with bytecode suppressed for the CHILD."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C",
        "LANG": "C",
    }


def _native_run(body, tmp_path, *, close_stdout=False, close_stderr=False):
    """Run ``BODY`` in a real interpreter, optionally with a descriptor closed.

    Closing BEFORE ``exec`` proves the descriptor-absent-at-startup path; the
    bodies that call ``os.close`` themselves prove the closed-after-import
    path. A POSIX shell performs the startup close so no ``preexec_fn`` or
    platform-specific fork assumption is made; other platforms skip.
    """
    python = os.environ.get("FREE_INFERENCE_WATCHDOG_BUILD_PYTHON",
                            sys.executable)
    cwd = str(REPO_ROOT)
    environment = _native_environment(tmp_path)
    if close_stdout or close_stderr:
        if os.name != "posix":
            pytest.skip("closing a startup descriptor needs a POSIX shell")
        prefix = ""
        if close_stdout:
            prefix += "exec 1>&-; "
        if close_stderr:
            prefix += "exec 2>&-; "
        argv = ["/bin/sh", "-c", prefix + 'exec "$1" -B -c "$2"',
                "sh", python, body]
    else:
        argv = [python, "-B", "-c", body]
    return subprocess.run(argv, cwd=cwd, capture_output=True, timeout=120,
                          env=environment)


def _assert_quiet(completed, code, stdout, stderr):
    assert completed.returncode == code, (completed.returncode,
                                          completed.stdout, completed.stderr)
    assert completed.stdout == stdout
    assert completed.stderr == stderr
    assert b"Exception ignored" not in completed.stderr
    assert b"Traceback" not in completed.stderr


def test_native_shutdown_boundary_keeps_codes_and_never_flushes_a_dead_stream(
        tmp_path):
    failure_line = f"{cli.PROGRAM}: {cli.OUTPUT_FAILURE_MESSAGE}\n".encode()
    status_body = (
        "from watchdog_catalog import cli; raise SystemExit(cli.main("
        "[\"status\", \"--json\", \"--catalog-file\", "
        f"{str(LOOKUP_FIXTURE)!r}, \"--cache-dir\", "
        f"{str(tmp_path / 'cache')!r}]))")
    cases = [
        # Closed after import: the interpreter-shutdown flush must be quiet.
        ("import os; from watchdog_catalog import cli; os.close(1); "
         "raise SystemExit(cli.main([\"--help\"]))", {}, 1, b"", b""),
        ("import os; from watchdog_catalog import cli; os.close(1); "
         "raise SystemExit(cli.main([\"bad-command\", \"--json\"]))",
         {}, 2, b"", b""),
        ("import os; from watchdog_catalog import cli; os.close(2); "
         "raise SystemExit(cli.main([\"bad-command\"]))", {}, 2, b"", b""),
        # Absent at startup: no wrapper exists to flush, codes still survive.
        ("from watchdog_catalog import cli; "
         "raise SystemExit(cli.main([\"--help\"]))",
         {"close_stdout": True}, 1, b"", b""),
        ("from watchdog_catalog import cli; "
         "raise SystemExit(cli.main([\"bad-command\", \"--json\"]))",
         {"close_stdout": True}, 2, b"", b""),
        (status_body, {"close_stdout": True}, 1, b"", failure_line),
        ("from watchdog_catalog import cli; "
         "raise SystemExit(cli.main([\"bad-command\"]))",
         {"close_stderr": True}, 2, b"", b""),
    ]
    for body, options, code, stdout, stderr in cases:
        completed = _native_run(body, tmp_path, **options)
        _assert_quiet(completed, code, stdout, stderr)


#: One real SIGINT at the exact write/flush call under test. ``raise_signal``
#: is the deterministic equivalent of a terminal interrupt arriving inside the
#: call: the exception is raised at that line, with the real bytes still in the
#: real stream's buffer, so interpreter shutdown exercises the same final flush
#: as an asynchronous signal would.
_INTERRUPT_SEAMS = r"""
import signal
import sys

class _At:
    def __init__(self, real):
        self._real = real
        self.fired = False
    def __getattr__(self, name):
        return getattr(self._real, name)
    def _fire(self):
        if not self.fired:
            self.fired = True
            signal.raise_signal(signal.SIGINT)

class _FlushInterrupt(_At):
    def flush(self):
        self._fire()
        return self._real.flush()

class _WriteInterrupt(_At):
    def write(self, text):
        if not self.fired:
            self._real.write(text[:16])
        self._fire()
        return self._real.write(text)

class _Buffer:
    def __init__(self, real):
        self._real = real
        self.fired = False
    def __getattr__(self, name):
        return getattr(self._real, name)
    def flush(self):
        if not self.fired:
            self.fired = True
            signal.raise_signal(signal.SIGINT)
        return self._real.flush()

class _StdoutBufferInterrupt(_At):
    def __init__(self, real):
        super().__init__(real)
        self.buffer = _Buffer(real.buffer)

class _BufferedInterrupt(_At):
    def __init__(self, real):
        super().__init__(real)
        self._pending = []
    def write(self, text):
        self._pending.append(text)
        return len(text)
    def flush(self):
        self._fire()
        for text in self._pending:
            self._real.write(text)
        self._pending.clear()
        return self._real.flush()
"""


def _interrupt_rows(cache, absent):
    """The interrupted-output cases: one SIGINT seam per existing return."""
    return [
        ("help flush",
         "import sys\nfrom watchdog_catalog import cli\n"
         "sys.stdout = _FlushInterrupt(sys.stdout)\n"
         "raise SystemExit(cli.main([\"--help\"]))"),
        ("help write",
         "import sys\nfrom watchdog_catalog import cli\n"
         "sys.stdout = _WriteInterrupt(sys.stdout)\n"
         "raise SystemExit(cli.main([\"--help\"]))"),
        ("fixed JSON error emission",
         "import sys\nfrom watchdog_catalog import cli\n"
         "sys.stdout = _StdoutBufferInterrupt(sys.stdout)\n"
         "raise SystemExit(cli.main([\"bad-command\", \"--json\"]))"),
        ("document emission on a dead stdout",
         "import os\nimport sys\nfrom watchdog_catalog import cli\n"
         "os.close(1)\n"
         "sys.stdout = _StdoutBufferInterrupt(sys.stdout)\n"
         "raise SystemExit(cli.main([\"status\", \"--json\", "
         f"\"--catalog-file\", {str(LOOKUP_FIXTURE)!r}, "
         f"\"--cache-dir\", {cache!r}]))"),
        ("human diagnostic on a dead stderr",
         "import os\nimport sys\nfrom watchdog_catalog import cli\n"
         "os.close(2)\n"
         "sys.stderr = _BufferedInterrupt(sys.stderr)\n"
         "raise SystemExit(cli.main([\"bad-command\"]))"),
        ("source failure diagnostic",
         "import sys\nfrom watchdog_catalog import cli\n"
         "sys.stderr = _BufferedInterrupt(sys.stderr)\n"
         "raise SystemExit(cli.main([\"search\", \"--catalog-file\", "
         f"{absent!r}, \"--cache-dir\", {cache!r}]))"),
        ("output-failure diagnostic",
         "import os\nimport sys\nfrom watchdog_catalog import cli\n"
         "os.close(1)\n"
         "sys.stderr = _BufferedInterrupt(sys.stderr)\n"
         "raise SystemExit(cli.main([\"status\", \"--json\", "
         f"\"--catalog-file\", {str(LOOKUP_FIXTURE)!r}, "
         f"\"--cache-dir\", {cache!r}]))"),
        ("diagnostic with nothing pending",
         "import signal\nfrom watchdog_catalog import cli\n"
         "cli._write_stderr = lambda text: signal.raise_signal(signal.SIGINT)\n"
         "raise SystemExit(cli.main([\"bad-command\"]))"),
        # The three carried cleanup-time seams: a SIGINT arriving inside the
        # non-interrupt recovery drain itself must still end at clean 130.
        ("help flush recovery drain",
         "import sys\nfrom watchdog_catalog import cli\n"
         "real_stdout = sys.stdout\n"
         "real_drain = cli._defuse_stream\n"
         "class S:\n"
         "    failed = False\n"
         "    def __getattr__(self, name):\n"
         "        return getattr(real_stdout, name)\n"
         "    def write(self, text):\n"
         "        return real_stdout.write(text)\n"
         "    def flush(self):\n"
         "        if not self.failed:\n"
         "            self.failed = True\n"
         "            raise OSError('dead stdout')\n"
         "        return real_stdout.flush()\n"
         "sys.stdout = S()\n"
         "def drain(name):\n"
         "    drain.once = getattr(drain, 'once', False)\n"
         "    if not drain.once:\n"
         "        drain.once = True\n"
         "        signal_raise()\n"
         "    return real_drain(name)\n"
         "def signal_raise():\n"
         "    import signal as _signal\n"
         "    _signal.raise_signal(_signal.SIGINT)\n"
         "cli._defuse_stream = drain\n"
         "raise SystemExit(cli.main([\"--help\"]))"),
        ("parser-help recovery drain",
         "import sys\nfrom watchdog_catalog import cli\n"
         "real_stdout = sys.stdout\n"
         "real_drain = cli._defuse_stream\n"
         "class S:\n"
         "    def __getattr__(self, name):\n"
         "        if name == 'write':\n"
         "            raise AttributeError(name)\n"
         "        return getattr(real_stdout, name)\n"
         "sys.stdout = S()\n"
         "def drain(name):\n"
         "    if not getattr(drain, 'once', False):\n"
         "        drain.once = True\n"
         "        import signal as _signal\n"
         "        _signal.raise_signal(_signal.SIGINT)\n"
         "    return real_drain(name)\n"
         "cli._defuse_stream = drain\n"
         "raise SystemExit(cli.main([\"--help\"]))"),
        ("fixed JSON error recovery drain",
         "from watchdog_catalog import cli\n"
         "real_drain = cli._defuse_stream\n"
         "def boom(document):\n"
         "    raise OSError('dead stdout')\n"
         "cli._emit = boom\n"
         "def drain(name):\n"
         "    if not getattr(drain, 'once', False):\n"
         "        drain.once = True\n"
         "        import signal as _signal\n"
         "        _signal.raise_signal(_signal.SIGINT)\n"
         "    return real_drain(name)\n"
         "cli._defuse_stream = drain\n"
         "raise SystemExit(cli.main([\"bad-command\", \"--json\"]))"),
    ]


def test_native_output_interrupts_are_clean_130_without_tracebacks(tmp_path):
    """Every interrupted output return defuses its stream before exit 130.

    A pending buffer at shutdown is the difference between 130 and the
    interpreter's own 120, and a flush interrupt must never escape as a
    traceback or leak a replacement document/diagnostic.
    """
    rows = _interrupt_rows(str(tmp_path / "cache"),
                           str(tmp_path / "absent.json"))
    for label, tail in rows:
        completed = _native_run(_INTERRUPT_SEAMS + "\n" + tail, tmp_path)
        assert completed.returncode == 130, (
            label, completed.returncode, completed.stdout, completed.stderr)
        assert completed.stdout == b"", (label, completed.stdout)
        assert completed.stderr == b"", (label, completed.stderr)
        assert b"Exception ignored" not in completed.stderr, label
        assert b"Traceback" not in completed.stderr, label
