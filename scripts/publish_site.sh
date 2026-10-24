#!/usr/bin/env bash
# publish_site.sh — LOCAL publication gate for the static dashboard.
#
# Sequence (units 9b; git staging/commit/push and Pages validation are LATER
# units and are deliberately absent here):
#
#   1. take ONE exclusive blocking advisory flock on <root>/site/.publish.lock
#      -- a lock inode DISTINCT from the builder's own site/.build.lock -- and
#      hold it continuously through the build, the read-back and the final
#      validation;
#   2. run the REAL build_site.py (the copy in THIS helper's checkout) against
#      --root with the selected interpreter, so the builder alone writes the
#      site/catalog.json + site/index.html pair;
#   3. read the EXACT final artifact bytes back, decode them as strict UTF-8
#      and hand them to the builder's own validate_artifact_pair. This second
#      gate is required independently of the builder's internal validation.
#
# A nonzero builder exit stops the gate BEFORE validation, even when a valid
# last-good pair is already on disk. A failed build or a rejected pair is never
# reported as a successful publication.
#
# This script does NOT stage, commit, push, deploy or contact the network, and
# it must never be run concurrently with a git publisher against a live
# checkout. Exit 0 means ONLY that the local artifact pair passed this gate --
# never that GitHub or the public site was updated.
#
# All operational output, including child builder progress, goes to STDERR;
# a successful run is stdout-silent. Failures exit 2 with a
# "publish_site: stage=<stage>: ..." diagnostic on stderr (git-facing wrappers
# may later map that to their own logging).
#
# Usage: scripts/publish_site.sh [--root DIR] [--python PATH]

set -u
set -o pipefail

readonly PROG="publish_site"

usage() {
    cat <<'USAGE'
usage: publish_site.sh [--root DIR] [--python PATH]

Run the LOCAL publication gate for the static dashboard: acquire the dedicated
publish lock, run the real build_site.py against --root, then validate the
published site/catalog.json and site/index.html pair by exact read-back.

This helper performs NO git staging, commit, push, deploy or network
publication. Exit 0 means only that the local artifact pair passed the gate,
never that the public site was updated.

options:
  --root DIR     project root holding state/ and receiving site/
                 (default: the repository this script lives in)
  --python PATH  interpreter for the builder and the read-back validation,
                 taken as ONE executable path (default: python3 from PATH)
  -h, --help     show this help and exit
USAGE
}

stage_err() {
    # stage_err <stage> <message>
    printf '%s: stage=%s: %s\n' "$PROG" "$1" "$2" >&2
}

# --- argument parsing (no side effects before --help is honoured) ------------
root=""
python_bin="python3"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --root)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                stage_err args "--root requires a non-empty directory value"
                usage >&2
                exit 2
            fi
            root=$2
            shift 2
            ;;
        --root=*)
            root=${1#--root=}
            if [ -z "$root" ]; then
                stage_err args "--root requires a non-empty directory value"
                usage >&2
                exit 2
            fi
            shift
            ;;
        --python)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                stage_err args "--python requires a non-empty interpreter path"
                usage >&2
                exit 2
            fi
            python_bin=$2
            shift 2
            ;;
        --python=*)
            python_bin=${1#--python=}
            if [ -z "$python_bin" ]; then
                stage_err args "--python requires a non-empty interpreter path"
                usage >&2
                exit 2
            fi
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            stage_err args "unknown argument: $1"
            usage >&2
            exit 2
            ;;
    esac
done

# --- this helper's own checkout (never the cwd, never --root) ----------------
# The builder/validator code path must come from where THIS script lives: an
# arbitrary --root is a data/artifact target and must never supply code.
self=$0
case $self in
    */*) : ;;
    *) self=$(command -v -- "$self" 2>/dev/null || printf '%s' "$self") ;;
esac
if resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ]; then
    self=$resolved
fi
script_dir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd -P) || {
    stage_err args "cannot resolve this script's directory"
    exit 2
}
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P) || {
    stage_err args "cannot resolve the repository root above $script_dir"
    exit 2
}

if [ -z "$root" ]; then
    root=$repo_root
fi

builder=$repo_root/build_site.py
if [ ! -f "$builder" ]; then
    stage_err builder "builder not found in this helper's checkout: $builder"
    exit 2
fi

# --- preflight --------------------------------------------------------------
if [ ! -d "$root" ]; then
    stage_err root "root is not an existing directory: $root"
    exit 2
fi

# Advisory locking is a POSIX capability; a host without the flock command
# fails explicitly rather than publishing unprotected.
if ! command -v flock >/dev/null 2>&1; then
    stage_err lock "flock command not found; POSIX advisory locking is required"
    exit 2
fi

site_dir=$root/site
if ! mkdir -p -- "$site_dir"; then
    stage_err lock "cannot create the site directory: $site_dir"
    exit 2
fi

# --- ONE publication lock, held for the whole gate --------------------------
# The lock path is a long-lived, shared inode: it is opened in append mode and
# NEVER unlinked, because unlinking a shared lock path would let a second
# publisher create a fresh inode and run concurrently. The lock is released by
# descriptor lifetime on every exit path (success, failure, signal).
lock_path=$site_dir/.publish.lock
if ! exec 9>>"$lock_path"; then
    stage_err lock "cannot open the publish lock: $lock_path"
    exit 2
fi
# ``flock`` treats a numeric argument as an already-open file descriptor, so
# this is the blocking exclusive lock on descriptor 9 (no timeout).
if ! flock -x 9; then
    stage_err lock "cannot acquire the publish lock: $lock_path"
    exit 2
fi

# --- build (stdout folded into stderr) --------------------------------------
"$python_bin" "$builder" --root "$root" 1>&2
build_rc=$?
if [ "$build_rc" -ne 0 ]; then
    stage_err builder "build failed (exit $build_rc): $builder --root $root"
    exit 2
fi

# --- independent read-back validation of the EXACT final artifact pair -----
# The program is fed on stdin so no shell quoting can reshape it. It imports
# from THIS helper's checkout only: the ambient path entry for the cwd and the
# target root are dropped before the import, so a synthetic root cannot shadow
# build_site or watchdog_catalog. It performs no JSON identity check of its own
# -- the existing validator decides.
validate_rc=0
"$python_bin" - "$root" "$repo_root" >&2 <<'PY' || validate_rc=$?
import sys
from pathlib import Path

target = sys.argv[1]
checkout = sys.argv[2]

# Deliberate, predictable import: this helper's checkout first, and no ambient
# cwd / target-root entry that could shadow the real modules.
sys.path[:] = [entry for entry in sys.path
               if entry not in ("", ".", target)]
sys.path.insert(0, checkout)

site = Path(target) / "site"
catalog_path = site / "catalog.json"
html_path = site / "index.html"

try:
    catalog_text = catalog_path.read_bytes().decode("utf-8", "strict")
    html_text = html_path.read_bytes().decode("utf-8", "strict")
except OSError as exc:
    print(f"publish_site: cannot read back artifacts: {exc}", file=sys.stderr)
    raise SystemExit(2)
except UnicodeDecodeError as exc:
    print("publish_site: read-back artifacts are not valid UTF-8 "
          f"({type(exc).__name__})", file=sys.stderr)
    raise SystemExit(2)

try:
    from build_site import validate_artifact_pair

    validate_artifact_pair(catalog_text, html_text)
except Exception as exc:  # noqa: BLE001 - every rejection is a gate failure
    print("publish_site: artifact pair rejected: "
          f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(2)
PY
if [ "$validate_rc" -ne 0 ]; then
    stage_err validate "read-back validation failed (exit $validate_rc): $site_dir"
    exit 2
fi

printf '%s: local gate passed: %s (artifact pair validated; nothing staged, committed, pushed or published)\n' \
    "$PROG" "$site_dir" >&2
exit 0
