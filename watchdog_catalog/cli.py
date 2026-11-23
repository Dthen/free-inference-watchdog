"""One-shot native catalogue command over the published catalogue (slice 2).

``free-inference-watchdog`` is the INSTALLED one-shot console command and
``python -m watchdog_catalog.cli`` its equivalent module invocation; both call
:func:`main`. Importing this module defines the entrypoint and starts nothing:
no event loop, no acquisition, no cache directory, no clock read and no SDK
import. The MCP launcher (:mod:`watchdog_catalog.__main__`), its stdio transport
and the pure query core are untouched; this adapter neither imports the
transport nor reimplements a query, matching, ordering, retry or cache rule.

Exactly one operation is chosen by :mod:`argparse`, with the common options
after the chosen command::

    free-inference-watchdog search [QUERY] [OPTIONS]
    free-inference-watchdog get PROVIDER MODEL_ID [OPTIONS]
    free-inference-watchdog status [OPTIONS]

``--catalog-file``/``--catalog-url`` are mutually exclusive and the file/URL is
consumed literally; with neither flag the accepted published feed
(:data:`watchdog_catalog.source.DEFAULT_CATALOG_URL`) is selected. ``--cache-dir``
is passed through unchanged. Parsing, help and every syntax failure finish
before a source is constructed, so ``--help`` needs no feed, cache, clock or
SDK. The existing :class:`~watchdog_catalog.source.AsyncCatalogSource` owns
source validation, acquisition, cache identity, last-good fallback and
warnings: one constructor call, one awaited ``aget()`` through
:func:`asyncio.run`, exactly one pure operation, then
:func:`watchdog_catalog.source.with_source`. No alternate retry, timeout,
cache or schema policy exists here, and a failed acquisition is returned as
the accepted structured source error instead of querying a nonexistent
snapshot.

Search options map directly with no trimming, folding, filtering or sorting of
their own: ``QUERY`` -> ``query`` (omitted is no text filter, an explicitly
empty string stays empty), ``--provider`` -> ``provider``,
``--min-context-length`` -> ``min_context_length``, repeatable
``--input-modality`` -> ``input_modalities``, repeatable
``--supported-parameter`` -> ``supported_parameters``, and ``--limit`` /
``--offset`` -> the existing pagination. An omitted ``--limit``/``--offset``
stays omitted so the pure query core owns its 5 / 0 defaults; a supplied value
(including an explicit zero) is forwarded literally. A literal ``null``
is ordinary text, never a magic no-filter value.

Successful operations print a plain-text rendering of EXACTLY the same result
document: every returned row/detail field, the real source freshness and
cache-fallback warnings ONCE per result, and an explicit ``unknown`` versus
``none (known empty)`` distinction. Terminal control characters are escaped
for display only -- identifiers and the JSON data are never altered. ``--json``
prints that complete UTF-8 JSON document instead (one document plus a newline)
and also selects a JSON error document. An exact standalone ``--json`` before
``--`` selects JSON error rendering even when parsing fails, while
``--option=--json`` is a literal value. Human diagnostics go to stderr. Exit
status is 0 for every ``ok: true`` (including no matches, a stale fallback and a
status reporting stale data), 2 for a malformed invocation,
``invalid_arguments``, ``unknown_provider`` or source configuration, 1 for
``not_found``, an unavailable catalogue or an output failure, and 130 for an
interruption. CLI syntax and configuration diagnostics are fixed,
non-reflective messages: they echo no URL, path, credential or arbitrary argv
and carry no ``source`` block because no acquisition occurred. Broken pipes and
interruptions never produce a traceback or a replacement document after output
has begun.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import unicodedata

from . import catalog as _catalog
from . import query as _query
from . import source as _source
from .source import with_source

#: The console command name. Declared explicitly so usage lines never depend on
#: ``sys.argv[0]`` (which ``python -m`` sets to a filesystem path).
PROGRAM = "free-inference-watchdog"

#: The one fixed, non-reflective CLI diagnostic: no supplied token appears in
#: it, and no source/cache/credential detail is reflected.
INVALID_ARGUMENTS_MESSAGE = "Invalid command line arguments."

#: The one fixed configuration diagnostic, matching the MCP launcher's wording:
#: bounded, non-reflective and free of any supplied URL, path or credential.
INVALID_SOURCE_MESSAGE = "Invalid catalogue source configuration."

#: The fixed output-failure diagnostic (stderr only; never a JSON replacement).
OUTPUT_FAILURE_MESSAGE = "Unable to write the result."

#: The two semantic argument-error codes that are invocation defects.
_DEFECTIVE_CODES = frozenset({"invalid_arguments", "unknown_provider"})

#: The private module-level wall-clock seam for ``watchdog_status``. Tests patch
#: it for deterministic parity; it is NOT a public override. It is read only
#: after a successful acquisition.
_wall_clock = time.time

_MAX_SAFE_INT = _catalog.MAX_SAFE_INT
_STATUS_CLOCK_ERROR = (
    "the wall clock must return a finite non-negative safe integer epoch")


class _UsageError(Exception):
    """A parser-shape failure; rendered by the fixed, non-reflective boundary."""


class _HelpUnavailable(Exception):
    """``--help`` was selected but no usable stdout exists to receive it."""


class _Parser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose diagnostics never echo caller arguments.

    ``argparse`` normally prints the offending value (an invalid integer, an
    unknown choice, an unrecognized option) as part of its error text. The
    reviewed boundary keeps that detail out of every stream, so ``error`` only
    raises :class:`_UsageError` and :func:`main` emits one fixed message.
    ``--help`` still exits 0 as deliberate text output, but only to stdout:
    argparse's own fallback would silently redirect help to stderr when the
    process started without a stdout descriptor, so that case raises
    :class:`_HelpUnavailable` for the quiet exit-1 boundary instead.
    """

    def error(self, message):
        del message
        raise _UsageError()

    def _print_message(self, message, file=None):
        if not message:
            return
        if file is None:
            file = sys.stdout
        if file is None:
            raise _HelpUnavailable()
        file.write(message)


def _decimal_integer(text):
    """One explicit numeric flag: ASCII decimal digits only, no sign/float."""
    if not text or not text.isascii() or not text.isdigit():
        raise argparse.ArgumentTypeError("expected a decimal integer")
    return int(text)


def _description():
    """The reviewed one-shot command description (help is deliberate text)."""
    return (
        "One-shot read-only query over the published free-inference catalogue.\n"
        "Each invocation selects ONE catalogue snapshot, performs exactly one\n"
        "operation and prints a human-readable rendering of one complete\n"
        "result document.\n"
        "\n"
        "Text output escapes terminal control characters for display only;\n"
        "--json prints the same result as one complete UTF-8 JSON document\n"
        "and also selects a JSON error document. Human diagnostics go to\n"
        "stderr.\n"
        "\n"
        "One catalogue source is selected at a time: --catalog-file and\n"
        "--catalog-url are mutually exclusive, and with neither the published\n"
        "remote feed is used:\n"
        "  "
        + _source.DEFAULT_CATALOG_URL
        + "\n"
        "--cache-dir is optional and only chooses where the validated disk\n"
        "cache is kept. The equivalent MCP server is the separate\n"
        "free-inference-watchdog-mcp command.")


def _add_common_options(parser):
    parser.add_argument(
        "--json", action="store_true",
        help="print the complete JSON result document instead of the human "
             "rendering (also selects JSON errors)")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--catalog-url", metavar="URL", default=None,
        help="read the catalogue from this literal http(s) URL")
    selection.add_argument(
        "--catalog-file", metavar="PATH", default=None,
        help="read the catalogue from this literal local JSON file")
    parser.add_argument(
        "--cache-dir", metavar="DIR", default=None,
        help="optional directory for the validated disk cache "
             "(default: the platform user cache directory)")


def build_parser():
    """The whole one-shot surface: three operations plus one source choice."""
    parser = _Parser(
        prog=PROGRAM,
        description=_description(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False)
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="{search,get,status}")

    search = commands.add_parser(
        "search", allow_abbrev=False,
        help="search the snapshot's endpoints",
        description="Return one page of matching endpoints; no extra filtering "
                    "or sorting is applied by the command.")
    _add_common_options(search)
    search.add_argument(
        "query", nargs="?", default=None, metavar="QUERY",
        help="literal text filter; omit it (or pass an empty string) for no "
             "text constraint")
    search.add_argument(
        "--provider", metavar="KEY", default=None,
        help="exact provider key")
    search.add_argument(
        "--min-context-length", metavar="N", type=_decimal_integer, default=None,
        help="inclusive minimum context length (0 is a real threshold)")
    search.add_argument(
        "--input-modality", metavar="VALUE", action="append", default=None,
        help="required input modality; repeatable and literal")
    search.add_argument(
        "--supported-parameter", metavar="VALUE", action="append", default=None,
        help="required supported parameter; repeatable and literal")
    search.add_argument(
        "--limit", metavar="N", type=_decimal_integer, default=None,
        help="page size (core default: 5; 1-200)")
    search.add_argument(
        "--offset", metavar="N", type=_decimal_integer, default=None,
        help="page offset (core default: 0)")

    lookup = commands.add_parser(
        "get", allow_abbrev=False,
        help="look up one exact provider/model-id pair",
        description="Return one exact endpoint; provider and model id are "
                    "case-sensitive and untrimmed.")
    _add_common_options(lookup)
    lookup.add_argument("provider", metavar="PROVIDER",
                        help="exact provider key")
    lookup.add_argument("model_id", metavar="MODEL_ID",
                        help="literal raw model id")

    status = commands.add_parser(
        "status", allow_abbrev=False,
        help="report whole-catalogue status",
        description="Return whole-catalogue freshness, coverage and counts.")
    _add_common_options(status)
    return parser


def _json_requested(argv):
    """Whether an exact standalone ``--json`` appears before ``--``.

    ``--option=--json`` is one token and therefore a literal value, never a
    format selector; an exact ``--json`` after ``--`` is a positional value.
    This scan runs before parsing so an otherwise malformed invocation still
    gets a JSON error document when the user asked for JSON.
    """
    for token in argv:
        if token == "--":
            return False
        if token == "--json":
            return True
    return False


def _help_requested(argv):
    """Whether an exact ``-h``/``--help`` appears before ``--``.

    Mirrors argparse's own option recognition: after ``--`` the token is a
    positional value, never a help request.
    """
    for token in argv:
        if token == "--":
            return False
        if token in ("-h", "--help"):
            return True
    return False


def _finish_help(stop):
    """Flush deliberate help text; a failed flush is a quiet exit 1.

    On success the parser's own ``SystemExit`` is re-raised unchanged. When the
    destination descriptor is dead the stream is defused so interpreter
    shutdown cannot append a second failure (exit 120) to the exit 1. An
    interruption during that flush -- or during the recovery drain itself --
    is exit 130 with the same defusal, so the buffered text cannot be replayed
    by the final flush.
    """
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
    except KeyboardInterrupt:
        return _interrupt_exit("stdout")
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        try:
            _defuse_stdout()
        except KeyboardInterrupt:
            return _interrupt_exit("stdout")
        if stop.code == 0:
            return 1
    raise stop


def select_source(arguments):
    """The ONE selected ``(kind, location)`` pair; never a source access."""
    if arguments.catalog_file is not None:
        return "file", arguments.catalog_file
    if arguments.catalog_url is not None:
        return "url", arguments.catalog_url
    return "url", _source.DEFAULT_CATALOG_URL


def _construct_source(kind, location, cache_dir):
    """The one accepted owner: its constructor owns configuration truth."""
    return _source.AsyncCatalogSource(kind, location, cache_dir=cache_dir)


def _status_epoch(clock):
    """One validated capture of the wall clock as a native epoch.

    Mirrors the MCP adapter's normalization exactly: a native ``int``/``float``
    (``bool`` excluded), finite, non-negative and inside the shared JSON-safe
    ceiling, truncated toward zero. This is adapter normalization, not a new
    clock or backend policy; ``MAX_SAFE_INT`` comes from the catalogue core.
    """
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(_STATUS_CLOCK_ERROR)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(_STATUS_CLOCK_ERROR)
    if not 0 <= value <= _MAX_SAFE_INT:
        raise ValueError(_STATUS_CLOCK_ERROR)
    return int(value)


def _search_arguments(arguments):
    """Map the parsed search surface onto the accepted core argument object.

    Every supplied member is passed literally. Omitted filters stay ``None``,
    and an omitted ``--limit``/``--offset`` is ABSENT from the mapping so the
    core owns its existing 5 / 0 pagination defaults (an explicit ``None``
    would be an invalid argument, not an omission); supplied values, including
    zero, are preserved. An explicitly empty query stays the empty string.
    """
    mapped = {
        "query": arguments.query,
        "provider": arguments.provider,
        "min_context_length": arguments.min_context_length,
        "input_modalities": arguments.input_modality,
        "supported_parameters": arguments.supported_parameter,
    }
    if arguments.limit is not None:
        mapped["limit"] = arguments.limit
    if arguments.offset is not None:
        mapped["offset"] = arguments.offset
    return mapped


def _get_arguments(arguments):
    return {"provider": arguments.provider, "model_id": arguments.model_id}


async def _answer(owner, arguments):
    """Acquire once, call exactly one pure operation, attach provenance."""
    acquisition = await owner.aget()
    if acquisition.get("ok") is not True:
        return with_source({}, acquisition)
    snapshot = acquisition["snapshot"]
    if arguments.command == "search":
        payload = _query.search_models(snapshot, _search_arguments(arguments))
    elif arguments.command == "get":
        payload = _query.get_model(snapshot, _get_arguments(arguments))
    else:
        # Whole epoch seconds, captured ONCE after successful acquisition.
        payload = _query.watchdog_status(
            snapshot, {}, now=_status_epoch(_wall_clock))
    return with_source(payload, acquisition)


def _write_stdout(data):
    """Write the complete UTF-8 document bytes, locale-independently."""
    stream = sys.stdout
    if stream is None:
        raise OSError("stdout is unavailable")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:  # a text-only replacement stream (tests, embedders)
        stream.write(data.decode("utf-8"))
        stream.flush()


def _write_stderr(text):
    """Best-effort fixed diagnostic; a broken stderr never masks a status.

    A write failure is also neutralized at the descriptor level: leaving a
    failed real ``sys.stderr`` in place makes the interpreter's final flush
    raise again and rewrite the exit status as 120. ``KeyboardInterrupt`` is
    deliberately NOT swallowed here; the caller decides the interrupt result.
    """
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        _defuse_stderr()


def _redirect_to_devnull(descriptor):
    """Point ``descriptor`` at devnull without re-closing a reused descriptor.

    ``os.open`` returns the LOWEST free number, so when the destination itself
    was closed the devnull handle *is* the destination. Closing it again would
    undo the redirection and restore the shutdown flush failure.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    if devnull == descriptor:
        return
    try:
        os.dup2(devnull, descriptor)
    except OSError:
        pass
    finally:
        try:
            os.close(devnull)
        except OSError:
            pass


def _defuse_stream(name):
    """Replace a failed standard stream's descriptor and drain its buffer."""
    stream = getattr(sys, name)
    if stream is None:
        return
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return
    _redirect_to_devnull(descriptor)
    try:
        stream.flush()
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        pass


def _defuse_stdout():
    """Avoid an interpreter-shutdown flush error after a broken stdout."""
    _defuse_stream("stdout")


def _defuse_stderr():
    """Avoid an interpreter-shutdown flush error after a broken stderr."""
    _defuse_stream("stderr")


def _interrupt_exit(stream_name):
    """Neutralize the interrupted stream's pending bytes, then report 130.

    An interruption can arrive while the stream still holds buffered bytes.
    Leaving those to interpreter shutdown rewrites 130 as 120 (or prints a
    shutdown error), and a flush of a dead descriptor fails again. The stream
    being written is defused exactly like a failed one, so nothing is appended
    after the interruption; a repeated signal during that drain is contained
    because the descriptor is already redirected to devnull.
    """
    try:
        _defuse_stream(stream_name)
    except KeyboardInterrupt:
        pass
    return 130


# -- human rendering: ONE result document, plain terminal-safe text -----------
#
# A rendering only: it reads the payload produced by the pure core plus the
# source block attached by ``with_source``, never queries, re-validates,
# re-sorts, truncates or mutates it. Values are escaped for display so hostile
# control characters cannot drive the terminal; the underlying data is
# untouched, so ``--json`` remains the complete machine document.

_UNKNOWN = "unknown"
_KNOWN_EMPTY = "none (known empty)"
_NO_NEXT_OFFSET = "none"


def _escape_display(text):
    """Escape control/format characters while preserving printable text."""
    escaped = []
    for character in text:
        category = unicodedata.category(character)
        if category in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            code = ord(character)
            if code <= 0xFF:
                escaped.append("\\x%02x" % code)
            elif code <= 0xFFFF:
                escaped.append("\\u%04x" % code)
            else:
                escaped.append("\\U%08x" % code)
        else:
            escaped.append(character)
    return "".join(escaped)


def _human_text(value):
    """One scalar for display: unknown and known-empty stay distinct."""
    if value is None:
        return _UNKNOWN
    if value is True:
        return "yes"
    if value is False:
        return "no"
    text = _escape_display(str(value))
    return text if text else _KNOWN_EMPTY


def _human_list(values):
    if values is None:
        return _UNKNOWN
    if not values:
        return _KNOWN_EMPTY
    return ", ".join(_human_text(item) for item in values)


def _human_map(mapping):
    if not isinstance(mapping, dict):
        return _UNKNOWN
    if not mapping:
        return _KNOWN_EMPTY
    return ", ".join("%s=%s" % (_human_text(key), _human_text(value))
                     for key, value in mapping.items())


def _human_source_lines(document):
    """The acquisition's real freshness/fallback block, shown exactly once."""
    source = document.get("source")
    if not isinstance(source, dict):
        return ["source: unavailable"]
    lines = [
        "source: mode=%s cached=%s fetched_at=%s fetch_age_s=%s "
        "observation_age_s=%s observation_fresh=%s not_modified=%s" % (
            _human_text(source.get("mode")),
            _human_text(source.get("cached")),
            _human_text(source.get("fetched_at")),
            _human_text(source.get("fetch_age_s")),
            _human_text(source.get("observation_age_s")),
            _human_text(source.get("observation_fresh")),
            _human_text(source.get("not_modified")),
        )
    ]
    warnings = source.get("warnings")
    if warnings is None:
        lines.append("source warnings: " + _UNKNOWN)
    elif warnings:
        lines.append("source warnings: "
                     + ", ".join(_human_text(code) for code in warnings))
    else:
        lines.append("source warnings: none")
    return lines


def _render_search_human(document):
    next_offset = document.get("next_offset")
    lines = [
        "search: total_matches=%s next_offset=%s" % (
            _human_text(document.get("total_matches")),
            _NO_NEXT_OFFSET if next_offset is None
            else _human_text(next_offset)),
        "snapshot: %s tick_epoch=%s" % (
            _human_text(document.get("snapshot_id")),
            _human_text(document.get("tick_epoch"))),
        "provider staleness: " + _human_map(document.get("provider_staleness")),
    ]
    if next_offset is not None:
        lines.append("next page: continue with --offset %s"
                     % _human_text(next_offset))
    lines.extend(_human_source_lines(document))
    lines.append("endpoints:")
    endpoints = document.get("endpoints") or []
    if not endpoints:
        lines.append("  no matching endpoints")
    for index, row in enumerate(endpoints, 1):
        lines.append("[%d] provider=%s model_id=%s name=%s" % (
            index, _human_text(row.get("provider")),
            _human_text(row.get("model_id")),
            _human_text(row.get("name"))))
        lines.append("    context_length=%s"
                     % _human_text(row.get("context_length")))
        lines.append("    input_modalities=%s"
                     % _human_list(row.get("input_modalities")))
        lines.append("    output_modalities=%s"
                     % _human_list(row.get("output_modalities")))
        lines.append("    supported_parameters=%s"
                     % _human_list(row.get("supported_parameters")))
    return lines


def _render_get_human(document):
    lines = [
        "get: provider=%s model_id=%s" % (
            _human_text(document.get("endpoint", {}).get("provider")),
            _human_text(document.get("endpoint", {}).get("model_id"))),
        "snapshot: %s tick_epoch=%s" % (
            _human_text(document.get("snapshot_id")),
            _human_text(document.get("tick_epoch"))),
        "provider staleness: " + _human_map(document.get("provider_staleness")),
    ]
    lines.extend(_human_source_lines(document))
    lines.append("endpoint:")
    endpoint = document.get("endpoint") or {}
    scalar_fields = (
        ("provider", "provider"),
        ("model_id", "model_id"),
        ("name", "name"),
        ("description", "description"),
        ("context_length", "context_length"),
    )
    for label, key in scalar_fields:
        lines.append("  %s=%s" % (label, _human_text(endpoint.get(key))))
    for label, key in (
        ("input_modalities", "input_modalities"),
        ("output_modalities", "output_modalities"),
        ("supported_parameters", "supported_parameters"),
    ):
        lines.append("  %s=%s" % (label, _human_list(endpoint.get(key))))
    for label, key in (
        ("chat_completions_url", "chat_completions_url"),
        ("api_type", "api_type"),
    ):
        lines.append("  %s=%s" % (label, _human_text(endpoint.get(key))))
    return lines


def _render_status_human(document):
    coverage = document.get("metadata_coverage") or {}
    counts = document.get("counts") or {}
    lines = [
        "status: snapshot=%s" % _human_text(document.get("snapshot_id")),
        "tick_epoch=%s cadence_s=%s last_tick_age_s=%s tick_fresh=%s" % (
            _human_text(document.get("tick_epoch")),
            _human_text(document.get("cadence_s")),
            _human_text(document.get("last_tick_age_s")),
            _human_text(document.get("tick_fresh"))),
        "counts: gateways=%s endpoints=%s distinct_raw_ids=%s "
        "marker_group_labels=%s" % (
            _human_text(counts.get("gateways")),
            _human_text(counts.get("endpoints")),
            _human_text(counts.get("distinct_raw_ids")),
            _human_text(counts.get("marker_group_labels"))),
        "metadata_coverage: endpoints=%s context_length_known=%s "
        "input_modalities_known=%s output_modalities_known=%s "
        "supported_parameters_known=%s description_known=%s" % (
            _human_text(coverage.get("endpoints")),
            _human_text(coverage.get("context_length_known")),
            _human_text(coverage.get("input_modalities_known")),
            _human_text(coverage.get("output_modalities_known")),
            _human_text(coverage.get("supported_parameters_known")),
            _human_text(coverage.get("description_known"))),
        "providers:",
    ]
    staleness = document.get("provider_staleness") or {}
    provider_counts = document.get("provider_counts") or {}
    for provider in staleness:
        lines.append("  %s endpoints=%s stale=%s" % (
            _human_text(provider),
            _human_text(provider_counts.get(provider)),
            _human_text(staleness[provider])))
    if not staleness:
        lines.append("  " + _KNOWN_EMPTY)
    lines.extend(_human_source_lines(document))
    warnings = document.get("warnings")
    if warnings is None:
        lines.append("warnings: " + _UNKNOWN)
    elif warnings:
        lines.append("warnings: "
                     + ", ".join(_human_text(code) for code in warnings))
    else:
        lines.append("warnings: none")
    lines.append(
        "pending_alerts=%s site_published=%s site_publish_age_s=%s" % (
            _human_text(document.get("pending_alerts")),
            _human_text(document.get("site_published")),
            _human_text(document.get("site_publish_age_s"))))
    return lines


_RENDERERS = {
    "search": _render_search_human,
    "get": _render_get_human,
    "status": _render_status_human,
}


def _render_human(document, command):
    """The complete plain-text rendering of one successful result document."""
    return "\n".join(_RENDERERS[command](document)) + "\n"


def _emit(document):
    data = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
    _write_stdout(data)


def _emit_human(document, command):
    _write_stdout(_render_human(document, command).encode("utf-8"))


def _fixed_error(message):
    return {"ok": False,
            "error": {"code": "invalid_arguments", "message": message}}


def _exit_code(document):
    if document.get("ok") is True:
        return 0
    error = document.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return 2 if code in _DEFECTIVE_CODES else 1


def _report_fixed(json_mode, message):
    """One fixed CLI/configuration diagnostic, exit 2, with no source block."""
    if json_mode:
        try:
            _emit(_fixed_error(message))
        except KeyboardInterrupt:
            return _interrupt_exit("stdout")
        except (BrokenPipeError, OSError, ValueError, AttributeError):
            try:
                _defuse_stdout()
            except KeyboardInterrupt:
                return _interrupt_exit("stdout")
        return 2
    try:
        _write_stderr(f"{PROGRAM}: {message}\n")
    except KeyboardInterrupt:
        return _interrupt_exit("stderr")
    return 2


def _emit_document(document, exit_code, human_command=None):
    """Emit one result document; never a replacement after output begins."""
    try:
        if human_command is None:
            _emit(document)
        else:
            _emit_human(document, human_command)
    except KeyboardInterrupt:
        return _interrupt_exit("stdout")
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        try:
            _defuse_stdout()
        except KeyboardInterrupt:
            return _interrupt_exit("stdout")
        try:
            _write_stderr(f"{PROGRAM}: {OUTPUT_FAILURE_MESSAGE}\n")
        except KeyboardInterrupt:
            return _interrupt_exit("stderr")
        return exit_code if exit_code else 1
    return exit_code


def main(argv=None):
    """Parse argv, run one operation and return the shell exit status.

    ``argparse`` exits 0 for ``--help`` when its text reaches stdout; a help
    request with no writable stdout descriptor is the quiet output-failure
    exit 1 instead. Every parser-shape or source configuration failure is one
    fixed non-reflective ``invalid_arguments`` diagnostic (exit 2) with no
    acquisition and no source block.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    json_mode = _json_requested(tokens)
    try:
        arguments = build_parser().parse_args(tokens)
    except _UsageError:
        return _report_fixed(json_mode, INVALID_ARGUMENTS_MESSAGE)
    except _HelpUnavailable:
        return 1
    except SystemExit as stop:
        return _finish_help(stop)
    except (BrokenPipeError, OSError, ValueError, AttributeError):
        if not _help_requested(tokens):
            raise
        try:
            _defuse_stdout()
        except KeyboardInterrupt:
            return _interrupt_exit("stdout")
        return 1
    except KeyboardInterrupt:  # pragma: no cover - native process case owns it
        # A help write can be interrupted mid-text; no other parse path emits
        # before returning, so only that request has bytes to neutralize.
        if _help_requested(tokens):
            return _interrupt_exit("stdout")
        return 130

    try:
        kind, location = select_source(arguments)
        owner = _construct_source(kind, location, arguments.cache_dir)
    except (TypeError, ValueError):
        return _report_fixed(json_mode, INVALID_SOURCE_MESSAGE)

    try:
        document = asyncio.run(_answer(owner, arguments))
    except KeyboardInterrupt:
        return 130

    exit_code = _exit_code(document)
    if document.get("ok") is not True and not json_mode:
        error = document.get("error")
        message = (error.get("message") if isinstance(error, dict)
                   else OUTPUT_FAILURE_MESSAGE)
        try:
            _write_stderr(f"{PROGRAM}: {message}\n")
        except KeyboardInterrupt:
            return _interrupt_exit("stderr")
        return exit_code
    return _emit_document(document, exit_code,
                          None if json_mode else arguments.command)


if __name__ == "__main__":
    raise SystemExit(main())
