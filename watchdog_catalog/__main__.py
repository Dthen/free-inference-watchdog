"""Source-selection CLI and stdio launcher for the standalone MCP (unit 6c).

``free-inference-watchdog-mcp`` is the INSTALLED console command and
``python -m watchdog_catalog`` the equivalent module invocation; both call
:func:`main`. Importing this module defines the entrypoint and starts nothing:
no event loop, no server, no acquisition, no cache directory and no SDK import.

Exactly one catalogue source is selected, by :mod:`argparse` alone:

* ``--catalog-url URL`` -- one literal ``http(s)`` URL;
* ``--catalog-file PATH`` -- one literal local JSON file;
* neither -- :data:`watchdog_catalog.source.DEFAULT_CATALOG_URL`, the published
  remote feed (the only default);
* the file/URL pair is MUTUALLY EXCLUSIVE in either flag order, and
  ``--cache-dir DIR`` optionally names the cache root, passed through unchanged.

Parsing and help happen BEFORE the source owner is built and before the
SDK-dependent factory is imported, so ``--help`` (and every parse failure) works
with no feed, no SDK, no credentials, no state and no cache. The selection is
validated by the existing
:class:`watchdog_catalog.source.AsyncCatalogSource` constructor -- this module
duplicates no URL/path validation -- and only its configuration ``TypeError``/
``ValueError`` becomes the one fixed, non-reflective argparse diagnostic
``Invalid catalogue source configuration.`` (exit 2, empty stdout, no echo of
the supplied URL/path/credential, no traceback). Acquisition failures are NOT
CLI failures: they stay lazy structured source results at tool-call time, so
startup never probes a provider and never fabricates an empty catalogue.

The one selected owner is constructed with the accepted default timeout,
byte-limit, revalidation and backoff policy untouched, and the SAME object is
handed to the accepted :func:`watchdog_catalog.server.create_server` factory.
The SDK's own ``MCPServer.run_stdio_async()`` is then awaited to completion by
the real stdlib event-loop runner :func:`asyncio.run`. Nothing about stdio,
negotiation, registration, source scheduling or query semantics is
reimplemented here, and neither ``aget`` nor a cache read/write happens at
startup. Stable stdout is protocol-only: there is no banner or configuration
dump, help is a deliberate explicit stdout response, and every diagnostic goes
to stderr. Normal shutdown follows the client's stdin EOF and a completed run
returns 0.

Scope boundary: this is the ONLY Python MCP server. The former root
``python3 mcp_server.py [--root ...]`` local-state route is RETIRED rather than
kept as a compatibility mode, so no ``--root`` flag, local-state fallback,
raw-metadata restoration or second registration path exists here or elsewhere
in the checkout. This module is the installed projected public-catalogue route
and never imports, patches, replaces or defaults to a producer graph, and no
per-tool path/URL/cadence/provider/auth configuration system is introduced for
the installed command. ``mcp``/Pydantic stay consumer
dependencies of the command: importing ``watchdog_catalog`` itself stays
stdlib-only.
"""

from __future__ import annotations

import argparse
import asyncio

from . import source as _source

#: The console command name. Declared explicitly so usage lines never depend on
#: ``sys.argv[0]`` (which ``python -m`` sets to a filesystem path).
PROGRAM = "free-inference-watchdog-mcp"

#: The one fixed configuration diagnostic: bounded, non-reflective and free of
#: any supplied URL, path, credential or traceback.
INVALID_SOURCE_MESSAGE = "Invalid catalogue source configuration."


def _description():
    """The reviewed ``--help`` description: accurate about stdio and selection."""
    return (
        "Read-only MCP server over the published free-inference catalogue, "
        "speaking the\n"
        "MCP stdio transport on stdin/stdout.\n"
        "\n"
        "stdout carries protocol messages only; diagnostics go to stderr. "
        "Startup prints\n"
        "no banner or configuration dump, fetches no catalogue and probes no "
        "provider.\n"
        "The process exits when the client closes stdin.\n"
        "\n"
        "One catalogue source is selected at a time: --catalog-file and "
        "--catalog-url are\n"
        "mutually exclusive, and with neither the published remote feed is "
        "used:\n"
        "  "
        + _source.DEFAULT_CATALOG_URL
        + "\n"
        "--cache-dir is optional and only chooses where the validated disk "
        "cache is kept.")


def build_parser():
    """The installed command's whole surface: two selections plus one cache root."""
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=_description(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False)
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
    return parser


def select_source(arguments):
    """The ONE selected ``(kind, location)`` pair; never a source access.

    An explicit file stays ``file`` mode with its literal path, an explicit URL
    stays ``url`` mode with its literal URL, and with neither flag the published
    :data:`watchdog_catalog.source.DEFAULT_CATALOG_URL` is chosen exactly.
    """
    if arguments.catalog_file is not None:
        return "file", arguments.catalog_file
    if arguments.catalog_url is not None:
        return "url", arguments.catalog_url
    return "url", _source.DEFAULT_CATALOG_URL


def _construct_source(kind, location, cache_dir):
    """The one accepted owner: its constructor owns configuration truth."""
    return _source.AsyncCatalogSource(kind, location, cache_dir=cache_dir)


def _create_server(owner):
    """Resolve the SDK-dependent factory LAZILY, after parse and validation."""
    from .server import create_server

    return create_server(owner)


#: The real stdlib event-loop runner seam: exactly :func:`asyncio.run`, which
#: creates, drives and closes the loop around the awaited coroutine.
_run_coroutine = asyncio.run


def serve(owner):
    """Build the typed server around ``owner`` and await its stdio loop once."""
    server = _create_server(owner)
    _run_coroutine(server.run_stdio_async())


def main(argv=None):
    """Parse argv, select one source, then serve stdio until stdin EOF.

    Returns 0 for a normally completed stdio run. Argument-shape failures and an
    invalid catalogue configuration exit 2 through :mod:`argparse` before the
    factory is imported; acquisition failures are not CLI errors and surface
    later as structured tool results.
    """
    parser = build_parser()
    arguments = parser.parse_args(argv)
    kind, location = select_source(arguments)
    try:
        owner = _construct_source(kind, location, arguments.cache_dir)
    except (TypeError, ValueError):
        parser.error(INVALID_SOURCE_MESSAGE)
    serve(owner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
