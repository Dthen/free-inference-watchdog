"""The publisher's three internal checks, in ONE stdlib module.

``scripts/publish_site.sh`` used to carry three separate Python programs in
quoted here-documents: the read-back validation, the post-commit identity check
and the unpublished-range inspection. They live here so that the artifact byte
read, the SHA-256 identity, the committed-blob read and the failure delivery
exist once, and so the shell carries ONE small bootstrap instead of three
polyglot copies.

The module is INERT on import: no I/O, no Git command, no configuration, no
renderer and no state access. Nothing runs until :func:`main` is called, and
the pair validator is imported lazily inside the stage that needs it.

Each stage keeps its OWN policy, deliberately: the read-back reads the
published pair once through ordinary ``Path.read_bytes`` (following a symlink,
exactly as before); the identity check establishes each WORKING artifact as an
ordinary regular file WITHOUT following a final-component symlink; the range
inspection reads each unpublished commit's two blobs and validates them. The
three argument vectors and the private success receipts are unchanged, because
the shell matches them byte for byte.

The pair judgement is :func:`catalog_export.validate_artifact_pair`, imported
DIRECTLY from its own module -- never through ``build_site`` -- so inspecting a
pair never loads the renderer, the configuration or any producer state. No
JSON rule, digest algorithm, limit or source policy is re-implemented here.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

#: The two published artifacts, in the fixed catalogue-then-index order every
#: receipt and every comparison below uses.
ARTIFACTS = ("site/catalog.json", "site/index.html")

#: The shell's private success markers. Each must stay byte-identical to the
#: matching ``readonly`` in scripts/publish_site.sh: a drifted marker fails the
#: gate closed instead of being accepted.
READBACK_RECEIPT = "publish_site: read-back validation passed"
IDENTITY_RECEIPT = "publish_site: artifact identities match"
RANGE_RECEIPT = "publish_site: unpublished range verified commits"


def sha256_hex(data):
    """Raw byte identity: stdlib SHA-256, never a Git-normalised blob hash."""
    return hashlib.sha256(data).hexdigest()


def _fail(message) -> NoReturn:
    """The ONE way a check reports a rejection: one stderr line, then exit 2.

    Only the delivery is shared; the wording belongs to the stage, because the
    three stages never share a diagnostic.
    """
    print(message, file=sys.stderr)
    raise SystemExit(2)


def run_git(target, *args):
    """Run one read-only ``git -C target`` command and return the process.

    stdout and stderr are captured, never forwarded: each caller owns its own
    failure diagnostic, and the identity and range policies differ on purpose.
    """
    return subprocess.run(["git", "-C", target, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


# --- stage 1: read-back validation of the EXACT published pair --------------

def check_readback(argv):
    """Validate the published pair read back once from ``argv[0]``.

    Each artifact is read ONCE, those very bytes are hashed, and the strings
    decoded FROM THOSE BYTES are validated, so the reported identities are the
    identity of exactly what the real validator accepted. Ordinary
    ``Path.read_bytes`` is kept on purpose: this stage's established read policy
    follows a symlink, and ordinary-file enforcement belongs to the identity
    stage, which runs later against the same root.

    ``argv`` is ``(<root>, <checkout>)``; the checkout entry is positional only
    -- the bootstrap already made this checkout's modules importable.
    """
    target = argv[0]
    site = Path(target) / "site"
    catalog_path = site / "catalog.json"
    html_path = site / "index.html"

    try:
        catalog_bytes = catalog_path.read_bytes()
        html_bytes = html_path.read_bytes()
    except OSError as exc:
        _fail(f"publish_site: cannot read back artifacts: {exc}")

    try:
        catalog_text = catalog_bytes.decode("utf-8", "strict")
        html_text = html_bytes.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        _fail("publish_site: read-back artifacts are not valid UTF-8 "
              f"({type(exc).__name__})")

    try:
        from catalog_export import validate_artifact_pair

        validate_artifact_pair(catalog_text, html_text)
    except Exception as exc:  # noqa: BLE001 - every rejection is a gate failure
        _fail("publish_site: artifact pair rejected: "
              f"{type(exc).__name__}: {exc}")

    # Success record: printed ONLY after the validator returned normally, and
    # the only thing this stage ever writes to stdout. The two identities are
    # the raw SHA-256 of the bytes decoded and validated above, in
    # catalogue,index order, and are frozen for the whole Git phase.
    print(f"{READBACK_RECEIPT} sha256 "
          f"{sha256_hex(catalog_bytes)} {sha256_hex(html_bytes)}")
    return 0


# --- stage 2: committed and working bytes against the frozen identities -----

def working_artifact_bytes(target, rel):
    """Exact bytes of a WORKING artifact that must be an ordinary regular file.

    ``Path.read_bytes`` (and a plain ``open``) FOLLOW a final-component
    symbolic link, so a completed ``post-commit`` hook could replace the
    validated artifact with a link to a copy holding identical bytes and a
    byte-only comparison of the working path would still pass. The file type is
    therefore established WITHOUT following that link -- ``lstat`` first, then
    an open that refuses a symlink -- and the OPENED descriptor is confirmed to
    be a regular file before any byte is read. This inspects the artifacts the
    synchronous hook already left behind; it is not a general hostile
    filesystem race-hardening layer.
    """
    path = Path(target) / rel
    try:
        info = os.lstat(path)
    except OSError as exc:
        _fail(f"publish_site: cannot read the working artifact back: "
              f"{rel}: {exc}")
    if stat.S_ISLNK(info.st_mode):
        detail = ("a symbolic link is rejected, even when its target holds "
                  "the validated bytes")
    elif stat.S_ISDIR(info.st_mode):
        detail = "a directory is not a working artifact"
    elif not stat.S_ISREG(info.st_mode):
        detail = "only an ordinary regular file is accepted"
    else:
        detail = ""
    if detail:
        _fail("publish_site: the working artifact is not an ordinary regular "
              f"file ({detail}): {rel}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.fdopen(os.open(str(path), flags), "rb")
    except OSError as exc:
        _fail(f"publish_site: cannot read the working artifact back: "
              f"{rel}: {exc}")
    with handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            _fail("publish_site: the working artifact is not an ordinary "
                  f"regular file (it was not one when opened): {rel}")
        return handle.read()


def check_identity(argv):
    """Prove the commit and the working pair still hold the frozen bytes.

    ``argv`` is ``(<root>, <commit>, <catalog sha256>, <index sha256>)``. The
    committed bytes are checked first: when a hook substituted the pair, the
    commit is what an operator has to deal with, and the diagnostic should say
    so rather than blame the working tree.
    """
    target, commit, catalog_digest, index_digest = argv
    expected = (
        (ARTIFACTS[0], catalog_digest),
        (ARTIFACTS[1], index_digest),
    )

    for rel, frozen in expected:
        try:
            proc = run_git(target, "cat-file", "blob", f"{commit}:{rel}")
        except OSError as exc:
            _fail("publish_site: cannot run git to read the committed artifact "
                  f"back: {rel}: {exc}")
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            _fail(f"publish_site: cannot read the committed artifact back: "
                  f"{rel}: {detail}")
        committed = sha256_hex(proc.stdout)
        if committed != frozen:
            _fail("publish_site: the committed artifact does not match the "
                  f"validated bytes: {rel} (validated sha256 {frozen}, "
                  f"committed sha256 {committed})")

    for rel, frozen in expected:
        actual = sha256_hex(working_artifact_bytes(target, rel))
        if actual != frozen:
            _fail("publish_site: the working artifact does not match the "
                  f"validated bytes: {rel} (validated sha256 {frozen}, "
                  f"working sha256 {actual})")

    # Success receipt: the only thing this stage ever writes to stdout, and it
    # must stay byte-identical to IDENTITY_RECEIPT in the shell.
    print(IDENTITY_RECEIPT)
    return 0


# --- stage 3: every UNPUBLISHED commit must be a valid artifact-only one ----

def verify_unpublished_range(argv):
    """Inspect the whole range from the observed destination tip to the candidate.

    ``argv`` is ``(<root>, <checkout>, <remote tip>, <candidate>)``; the
    checkout entry is positional only, because the bootstrap already made this
    checkout's modules importable. An ordinary single-parent, nonempty,
    artifact-only commit whose pair the REAL validator accepts is the only kind
    this publisher may carry, so unrelated source or private history can never
    be swept along and a previously failed verification cannot authorize a
    malformed intermediate artifact commit.
    """
    target, _checkout, remote_tip, candidate = argv

    def fail(message):
        _fail(f"publish_site: unpublished range rejected: {message}")

    def git(*args):
        proc = run_git(target, *args)
        if proc.returncode != 0:
            fail(f"git {args[0]} exited {proc.returncode}")
        return proc.stdout

    try:
        from catalog_export import validate_artifact_pair
    except Exception as exc:  # noqa: BLE001 - every import failure is a failure
        fail(f"cannot import the real validator: {type(exc).__name__}: {exc}")

    raw = git("rev-list", "--reverse", "--topo-order",
              f"{remote_tip}..{candidate}")
    commits = [line for line in raw.decode("utf-8", "strict").splitlines() if line]
    if not commits:
        fail("the unpublished range is empty although the destination tip "
             "differs from the candidate")

    for commit in commits:
        parents = git("rev-list", "--parents", "-n", "1", commit).decode().split()
        if len(parents) != 2:
            fail(f"unpublished commit {commit} is not an ordinary single-parent "
                 "commit")
        raw_paths = git("diff-tree", "--no-commit-id", "--name-only", "-r",
                        "--no-renames", commit)
        paths = [line for line in raw_paths.decode("utf-8", "strict").splitlines()
                 if line]
        if not paths:
            fail(f"unpublished commit {commit} changes no path")
        outside = sorted(set(paths) - set(ARTIFACTS))
        if outside:
            fail(f"unpublished commit {commit} changes paths outside the public "
                 "artifact pair: " + ", ".join(outside))
        blobs = {rel: git("cat-file", "blob", f"{commit}:{rel}")
                 for rel in ARTIFACTS}
        try:
            catalog_text = blobs[ARTIFACTS[0]].decode("utf-8", "strict")
            html_text = blobs[ARTIFACTS[1]].decode("utf-8", "strict")
        except UnicodeDecodeError:
            fail(f"unpublished commit {commit} holds artifacts that are not "
                 "valid UTF-8")
        try:
            validate_artifact_pair(catalog_text, html_text)
        except Exception as exc:  # noqa: BLE001 - the real validator decides
            fail(f"unpublished commit {commit} does not hold a pair the real "
                 f"validator accepts: {type(exc).__name__}: {exc}")

    print(f"{RANGE_RECEIPT} {len(commits)}")
    return 0


# --- the ONE entry point the shell's bootstrap calls ------------------------

def main(argv):
    """Dispatch one internal publication check.

    ``argv`` is the child's argument vector with the leading ``-`` removed, so
    the selector sits at position 0 and the read-back call -- which has no
    selector -- starts directly with the target root.

    A selector is recognised ONLY at the fixed 5-argument shape the shell
    builds (selector plus its four payload items), so a read-back root that is
    literally named ``identity`` or ``range`` still dispatches to
    :func:`check_readback`.
    """
    if len(argv) == 5 and argv[0] == "identity":
        return check_identity(argv[1:])
    if len(argv) == 5 and argv[0] == "range":
        return verify_unpublished_range(argv[1:])
    return check_readback(argv)
