#!/usr/bin/env python3
"""Read-only validation of one standalone Pages site directory (unit 9e).

Usage (the supported form, from the repository root):

    python -I -S scripts/validate_site.py --site-dir site

The command proves that ``DIR/catalog.json`` and ``DIR/index.html`` describe ONE
public catalogue snapshot, by delegating the whole judgement to the accepted
:func:`catalog_export.validate_artifact_pair` -- the same function
``build_site`` re-exports. It does not re-implement, relax or extend the
schema, hash or island rules, and it does not rebuild anything.

Deliberately narrow and side-effect free:

* it reads exactly those two artifacts, each ONCE as raw bytes through a
  non-following ordinary-file check (a missing, unreadable, symlinked,
  directory or FIFO artifact is rejected without being followed or opened
  blocking), decodes strict UTF-8 and hands those same observations to the
  validator;
* it bootstraps ONLY its own checkout's pure validator modules
  (``catalog_export`` and the pure ``watchdog_catalog.catalog`` core) -- never a
  module from the supplied site directory, the current working directory, an
  installed package, the provider/configuration/state layer, the renderer, the
  builder or the guarded MCP SDK -- and it never touches the network;
* it writes nothing: no bytecode, no lock, no cache, no artifact, no repair.

Exit 0 means only that this read-only pair passed. It never means published,
deployed, fresh, browser-compatible, Windows-verified or Checkpoint B. Exit 2
means the pair was rejected or the invocation was wrong; the diagnostic on
stderr is bounded and non-reflective (never a traceback, an artifact body, a
credential or a path).
"""

from __future__ import annotations

import argparse
import os
import stat
import sys

#: Nothing in this process may leave a bytecode cache beside the checkout's
#: sources: the validator is a pure reader. Set before the first import below.
sys.dont_write_bytecode = True

_CATALOG_NAME = "catalog.json"
_HTML_NAME = "index.html"
_DIAGNOSTIC_LIMIT = 200
_SUCCESS_DIAGNOSTIC = (
    "catalog.json and index.html describe one catalogue "
    "snapshot (read-only pair check; nothing published)")
_INTERNAL_DIAGNOSTIC = "internal validator error"

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_READ_CHUNK = 1 << 20


class _ArtifactError(Exception):
    """A fixed, non-reflective artifact rejection."""


def _checkout_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bootstrap_pure_validator():
    """Put ONLY this checkout's own pure modules first on ``sys.path``.

    ``-I``/``-S`` already keep the cwd and site-packages out, but the ordering
    and the cwd exclusion are enforced here too so the contract also holds for
    a plain invocation. The supplied site directory is never a path entry.
    """
    blocked = {os.path.realpath(os.getcwd())}
    root = os.path.realpath(_checkout_root())
    kept = []
    for entry in sys.path:
        if not entry:
            continue
        resolved = os.path.realpath(entry)
        if resolved in blocked or resolved == root:
            continue
        kept.append(entry)
    sys.path[:] = [_checkout_root()] + kept


def _read_artifact(site_dir, name):
    """One ordinary-file read of ``site_dir/name`` as raw bytes, or a rejection."""
    path = os.path.join(site_dir, name)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise _ArtifactError("%s is missing" % name)
    except OSError:
        raise _ArtifactError("%s is unreadable" % name)
    if not stat.S_ISREG(before.st_mode):
        raise _ArtifactError("%s is not a regular file" % name)
    flags = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _BINARY | _NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _ArtifactError("%s cannot be opened" % name)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
                (opened.st_dev, opened.st_ino)
                != (before.st_dev, before.st_ino)):
            raise _ArtifactError("%s is not a stable regular file" % name)
        chunks = []
        while True:
            chunk = os.read(descriptor, _READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    except OSError:
        raise _ArtifactError("%s cannot be read" % name)
    finally:
        os.close(descriptor)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise _ArtifactError("%s is not valid UTF-8" % name)


def _report(message):
    line = "validate_site: %s" % message
    sys.stderr.write(line[:_DIAGNOSTIC_LIMIT] + "\n")
    return 2


def _parser():
    parser = argparse.ArgumentParser(
        description=(
            "Read-only check that one site directory's catalog.json and "
            "index.html describe the same public catalogue snapshot."),
        allow_abbrev=False)
    parser.add_argument(
        "--site-dir", required=True, metavar="DIR",
        help="site directory containing catalog.json and index.html")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        catalog_text = _read_artifact(args.site_dir, _CATALOG_NAME)
        html_text = _read_artifact(args.site_dir, _HTML_NAME)
        validate_artifact_pair(catalog_text, html_text)
    except _ArtifactError as exc:
        return _report(str(exc))
    except CatalogError as exc:
        return _report("catalogue rejected (%s)" % exc.code)
    except ValueError:
        return _report("catalogue artifacts do not match")
    sys.stderr.write("validate_site: %s\n" % _SUCCESS_DIAGNOSTIC)
    return 0


def _run(argv=None):
    try:
        return main(argv)
    except Exception:  # no traceback, no artifact/credential/path echo
        return _report(_INTERNAL_DIAGNOSTIC)


_bootstrap_pure_validator()

from catalog_export import validate_artifact_pair  # noqa: E402
from watchdog_catalog.catalog import CatalogError  # noqa: E402

if __name__ == "__main__":
    sys.exit(_run())
