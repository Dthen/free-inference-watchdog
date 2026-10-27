#!/usr/bin/env python3
"""Read-only source-to-inline-artifact freshness gate for the Pages page (9f).

Usage (the supported form, from the repository root):

    python -I -S scripts/check_page_sources.py --site-dir site

The command proves that the page about to be uploaded inlines the MAINTAINED
sources byte for byte: ``web/catalog-query.js`` and ``web/webmcp.js``, each in
its own ``<script data-watchdog="...">`` element, in the builder's fixed order,
followed by the one bootstrap element. It does not re-implement, relax or extend
the builder's rendering, and it does not rebuild anything.

Why a separate gate. ``scripts/validate_site.py`` proves the catalogue pair is
ONE coherent snapshot; it deliberately reads only the two artifacts and knows
nothing about JavaScript. This gate answers the other question: is the deployed
page's executable code still the code in this checkout? When ``web/**`` changes
without a regenerated page, the page must be REBUILT by the producer, never
deployed with stale JavaScript.

Deliberately narrow and side-effect free:

* it reads ``index.html`` and the maintained sources ONCE each as raw bytes
  through a non-following ordinary-file check (a missing, unreadable,
  symlinked, directory or FIFO input is rejected without being followed or
  opened blocking), then decodes strict UTF-8 and compares the EXACT text;
* the sources come from THIS checkout's ``web/`` directory by default. The
  supplied site directory never chooses the expectation, and the Pages workflow
  passes no source directory at all;
* it imports only the standard library -- never the builder, the renderer, the
  provider/configuration/state layer, an installed package or the guarded SDK --
  and it never touches the network;
* it writes nothing: no bytecode, no lock, no cache, no artifact, no repair.

Exit 0 means only that this page inlines the current sources. It never means
published, deployed, coherent, browser-compatible or Checkpoint C. Exit 2 means
the page is stale, the artifacts/sources are unusable or the invocation was
wrong; the diagnostic on stderr is bounded and non-reflective (never a
traceback, a source body, a credential or a path).
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys

#: Nothing in this process may leave a bytecode cache beside the checkout's
#: sources: the gate is a pure reader. Set before the first import below.
sys.dont_write_bytecode = True

HTML_NAME = "index.html"

#: The maintained executable page sources, in page order, with the marker the
#: builder puts in ``data-watchdog``. This table is pinned to the builder's own
#: ``PAGE_SOURCE_FILES`` by ``tests/test_validate_site.py``; it is duplicated
#: here ONLY to keep this gate free of a builder/producer import.
PAGE_SOURCE_FILES = (
    ("catalog-query", "web/catalog-query.js"),
    ("webmcp", "web/webmcp.js"),
)

#: The builder-generated third element: exactly one bootstrap call. Its text is
#: the builder's own contract, duplicated here for the same reason as the table
#: above and pinned to ``build_site.PAGE_BOOTSTRAP_SOURCE`` by
#: ``tests/test_validate_site.py`` -- a builder-side change to the bootstrap is a
#: stale page until the producer regenerates it.
PAGE_BOOTSTRAP_MARKER = "boot"

PAGE_BOOTSTRAP_SOURCE = """(function () {
  "use strict";
  var adapter = typeof globalThis === "undefined"
    ? undefined
    : globalThis.WatchdogWebMCP;
  if (!adapter || typeof adapter.setup !== "function") {
    return;
  }
  adapter.setup().catch(function () {});
})();
"""

_TAG = re.compile(r'<script data-watchdog="([A-Za-z0-9_-]+)">')
_CLOSE = "</script>"
#: The parseable tag prefix, counted separately so a marker on an unexpected tag
#: shape is REJECTED as unverifiable instead of silently skipped. JSON islands
#: escape ``<`` and ``"``, so page DATA can never contribute a count here.
_ALL_MARKERS = '<script data-watchdog="'
_DIAGNOSTIC_LIMIT = 200
_SUCCESS_DIAGNOSTIC = (
    "index.html inlines the maintained page sources byte for byte "
    "(read-only freshness check; nothing published)")
_INTERNAL_DIAGNOSTIC = "internal freshness-check error"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_READ_CHUNK = 1 << 20


class _FreshnessError(Exception):
    """A fixed, non-reflective freshness rejection."""


def _checkout_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_web_dir():
    """This gate's OWN ``web/`` directory: never a caller-selected source set."""
    return os.path.join(_checkout_root(), "web")


def _read_text(path, label):
    """One ordinary-file read of ``path`` as text, or a labelled rejection.

    ``label`` is always a FIXED relative name (``index.html`` or
    ``web/catalog-query.js``): a diagnostic may say which artifact is unusable
    but never reflects a supplied absolute path.
    """
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise _FreshnessError("%s is missing" % label)
    except OSError:
        raise _FreshnessError("%s is unreadable" % label)
    if not stat.S_ISREG(before.st_mode):
        raise _FreshnessError("%s is not a regular file" % label)
    flags = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _BINARY | _NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _FreshnessError("%s cannot be opened" % label)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
                (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)):
            raise _FreshnessError("%s is not a stable regular file" % label)
        chunks = []
        while True:
            chunk = os.read(descriptor, _READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    except OSError:
        raise _FreshnessError("%s cannot be read" % label)
    finally:
        os.close(descriptor)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _FreshnessError("%s is not valid UTF-8" % label)


def _inline_scripts(html):
    """Every ``<script data-watchdog="MARKER">BODY</script>`` pair, in order."""
    found = []
    position = 0
    while True:
        match = _TAG.search(html, position)
        if match is None:
            return found
        start = match.end()
        end = html.find(_CLOSE, start)
        if end < 0:
            raise _FreshnessError(
                "%s has an unterminated inline script; the page cannot be "
                "verified" % HTML_NAME)
        found.append((match.group(1), html[start:end]))
        position = end + len(_CLOSE)


def check_page(html, sources):
    """Reject anything but the builder's exact elements and current bodies."""
    scripts = _inline_scripts(html)
    # Every marker must belong to a tag this gate can parse; an unexpected
    # attribute shape is a page this gate cannot verify, never an ignored hint.
    if html.count(_ALL_MARKERS) != len(scripts):
        raise _FreshnessError(
            "%s carries inline scripts that cannot be verified; rebuild the "
            "page" % HTML_NAME)
    expected = [marker for marker, _ in PAGE_SOURCE_FILES] + [
        PAGE_BOOTSTRAP_MARKER]
    actual = [marker for marker, _ in scripts]
    if actual != expected:
        missing = [marker for marker in expected if marker not in actual]
        if missing:
            raise _FreshnessError(
                "%s does not embed the maintained page scripts (%s missing); "
                "rebuild the page" % (HTML_NAME, ", ".join(missing)))
        raise _FreshnessError(
            "%s embeds unexpected, duplicated or reordered page scripts; "
            "rebuild the page" % HTML_NAME)
    bodies = dict(scripts)
    for marker, relative in PAGE_SOURCE_FILES:
        if bodies[marker] != sources[marker]:
            raise _FreshnessError(
                "%s inlines a stale %s body; rebuild the page from %s"
                % (HTML_NAME, marker, relative))
    if bodies[PAGE_BOOTSTRAP_MARKER] != PAGE_BOOTSTRAP_SOURCE:
        raise _FreshnessError(
            "%s inlines a stale %s body; rebuild the page" % (
                HTML_NAME, PAGE_BOOTSTRAP_MARKER))


def _report(message):
    line = "check_page_sources: %s" % message
    sys.stderr.write(line[:_DIAGNOSTIC_LIMIT] + "\n")
    return 2


def _parser():
    parser = argparse.ArgumentParser(
        description=(
            "Read-only check that one site directory's index.html inlines this "
            "checkout's maintained web/ page sources byte for byte."),
        allow_abbrev=False)
    parser.add_argument(
        "--site-dir", required=True, metavar="DIR",
        help="site directory containing index.html")
    parser.add_argument(
        "--web-dir", metavar="DIR", default=None,
        help=("directory holding the maintained page sources (default: this "
              "gate's own checkout web/ directory)"))
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    web_dir = args.web_dir if args.web_dir is not None else _default_web_dir()
    try:
        html = _read_text(os.path.join(args.site_dir, HTML_NAME), HTML_NAME)
        sources = {}
        for marker, relative in PAGE_SOURCE_FILES:
            # ``relative`` is the builder's ``web/<file>`` label; the source is
            # resolved inside the selected web directory, never by the label.
            name = relative.split("/", 1)[1]
            sources[marker] = _read_text(os.path.join(web_dir, name), relative)
        check_page(html, sources)
    except _FreshnessError as exc:
        return _report(str(exc))
    sys.stderr.write("check_page_sources: %s\n" % _SUCCESS_DIAGNOSTIC)
    return 0


def _run(argv=None):
    try:
        return main(argv)
    except Exception:  # no traceback, no source-body/credential/path echo
        return _report(_INTERNAL_DIAGNOSTIC)


if __name__ == "__main__":
    sys.exit(_run())
