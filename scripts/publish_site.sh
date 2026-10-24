#!/usr/bin/env bash
# publish_site.sh — LOCAL publication gate and restricted artifact commit.
#
# Sequence (unit 9c; remote push, Pages validation and the production wrapper
# cutover are LATER units and are deliberately absent here):
#
#   1. take ONE exclusive blocking advisory flock on <root>/site/.publish.lock
#      -- a lock inode DISTINCT from the builder's own site/.build.lock -- and
#      hold it continuously through the build, the read-back, the final
#      validation AND the local artifact commit;
#   2. run the REAL build_site.py (the copy in THIS helper's checkout) against
#      --root with the selected interpreter, so the builder alone writes the
#      site/catalog.json + site/index.html pair;
#   3. read the EXACT final artifact bytes back, decode them as strict UTF-8
#      and hand them to the builder's own validate_artifact_pair. This second
#      gate is required independently of the builder's internal validation.
#      Because a selected executable may simply ignore the Python program and
#      return zero, that program emits ONE fixed private success receipt on its
#      stdout ONLY after the real validator returned normally; the shell
#      CAPTURES it (never forwards it) and requires exit 0 AND the exact
#      receipt. An empty, wrong or extra receipt is a validation failure;
#   4. commit those two paths -- and ONLY those two paths -- in the selected
#      root's own Git repository: one path-limited commit with a fixed
#      noninteractive message, whose committed entries are read back and
#      compared against the validated files before any local success is
#      claimed.
#
# A nonzero builder exit stops the gate BEFORE validation, even when a valid
# last-good pair is already on disk. A rejected pair stops it BEFORE any Git
# command runs, even when a valid last-good artifact commit and unrelated
# staged work already exist. Neither is ever reported as a successful
# publication, and no failure path stashes, resets, cleans, rebases or
# otherwise repairs the operator's working tree.
#
# The root must already BE the top level of an ordinary non-bare Git working
# tree with an existing HEAD on an attached branch and no in-progress
# merge/rebase/cherry-pick/unresolved-index state. This helper never
# initialises a repository, switches a branch, configures an identity or
# repairs a repository; it keeps the root's own user, configuration and hook
# policy. Nothing outside site/index.html and site/catalog.json is staged or
# committed, and a run whose artifacts are unchanged commits nothing at all.
#
# This script does NOT push, deploy or contact the network, and it must never
# be run concurrently with another git publisher against a live checkout. Exit
# 0 means ONLY that the local artifact pair passed this gate and that its local
# commit was handled -- never that GitHub or the public site was updated. A
# no-change run therefore succeeds without claiming anything about a remote:
# retrying a committed-but-unpushed artifact belongs to unit 9d.
#
# All operational output, including child builder progress and every Git
# command's chatter, goes to STDERR; a successful run is stdout-silent.
# Failures exit 2 with a "publish_site: stage=<stage>: ..." diagnostic on
# stderr, where <stage> is args, root, lock, builder, validate, git,
# git/add, git/commit or git/verify.
#
# Usage: scripts/publish_site.sh [--root DIR] [--python PATH]

set -u
set -o pipefail

readonly PROG="publish_site"

# The ONE fixed private success receipt of the read-back validator (see stage 3
# below). It is an internal handshake, never operational or public output, and
# it MUST stay byte-identical to the single print() in the read-back program:
# if the two ever drift apart every run fails CLOSED at stage=validate (the
# gate never accepts a missing receipt), never open.
readonly READBACK_RECEIPT='publish_site: read-back validation passed'

# The ONLY paths the local commit may touch (stage 4), and the fixed
# noninteractive message it carries. Both are literal, repo-relative and used
# with an explicit "--" so no pathspec magic or ambient glob can widen them.
readonly ARTIFACT_INDEX='site/index.html'
readonly ARTIFACT_CATALOG='site/catalog.json'
readonly COMMIT_MESSAGE='chore(site): publish dashboard artifacts'

usage() {
    cat <<'USAGE'
usage: publish_site.sh [--root DIR] [--python PATH]

Run the LOCAL publication gate for the static dashboard: acquire the dedicated
publish lock, run the real build_site.py against --root, validate the published
site/catalog.json and site/index.html pair by exact read-back, then commit that
pair -- and only that pair -- in --root's own Git repository.

The commit is path-limited: site/index.html and site/catalog.json are staged
and committed with a fixed message, while every other tracked, staged or
untracked file is left exactly as it was. --root must already be the top level
of an ordinary non-bare Git working tree with an existing commit on an attached
branch; this helper never initialises, repairs, rebases or reconfigures a
repository. A run whose artifacts are unchanged commits nothing.

This helper performs NO remote push, deploy or network publication. Exit 0
means only that the local artifact pair passed the gate and its local commit was
handled, never that the public site was updated.

options:
  --root DIR     project root holding state/ and receiving site/ (default: the
                 repository this script lives in)
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
#
# Its stdout is CAPTURED, never forwarded: the only thing it ever writes there
# is the private success receipt, emitted after the validator returned
# normally. Exit status alone cannot prove the program ran (a selected
# executable may ignore its arguments and stdin, do nothing and return zero),
# so the gate requires BOTH a zero exit AND that exact receipt. Diagnostics go
# to stderr and pass straight through.
#
# Bash command substitution deletes every trailing newline, so the compared
# value deliberately has none: the receipt is one line and the expected string
# is stored without its terminator. Any other difference -- no output, wrong
# text, a second line, a repeated receipt -- fails this gate.
validate_rc=0
validate_out=$("$python_bin" - "$root" "$repo_root" <<'PY'
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

# Success receipt: printed ONLY after the validator returned normally above,
# and the only thing this program ever writes to stdout. The text must stay
# byte-identical to READBACK_RECEIPT in the shell; a drift fails closed.
print("publish_site: read-back validation passed")
PY
) || validate_rc=$?
if [ "$validate_rc" -ne 0 ]; then
    stage_err validate "read-back validation failed (exit $validate_rc): $site_dir"
    exit 2
fi
if [ "$validate_out" != "$READBACK_RECEIPT" ]; then
    stage_err validate "read-back validator did not confirm execution (${#validate_out} chars of stdout, expected the private success receipt): $site_dir"
    exit 2
fi

# --- local Git phase: restricted staging and a path-limited commit ----------
# Entered ONLY after the real validator returned normally and its receipt was
# accepted above, so a failed build or a rejected pair never inspects, stages
# or commits anything -- not even in a repository that already carries a valid
# last-good artifact commit and unrelated staged work. Every Git command below
# addresses the SELECTED root explicitly: never this helper's own checkout and
# never the ambient cwd.
if ! command -v git >/dev/null 2>&1; then
    stage_err git "git command not found; the local artifact commit requires git"
    exit 2
fi

# Ambient location overrides would silently retarget every command below at a
# different repository or index, so they are dropped rather than trusted. The
# root's own configuration, hooks and identity are deliberately left alone.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
      GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CEILING_DIRECTORIES \
      GIT_DISCOVERY_ACROSS_FILESYSTEM GIT_NAMESPACE

root_phys=$(CDPATH= cd -- "$root" && pwd -P) || {
    stage_err git "cannot resolve the root path for the Git phase: $root"
    exit 2
}

git_root() {
    git -C "$root_phys" "$@"
}

# --- the root must BE the top of an ordinary, attached, idle work tree ------
if ! git_root rev-parse --git-dir >/dev/null 2>&1; then
    stage_err git "root is not a git repository (no repository at or above it): $root_phys"
    exit 2
fi
if [ "$(git_root rev-parse --is-bare-repository 2>/dev/null)" = true ]; then
    stage_err git "root is a bare repository; an ordinary non-bare working tree is required: $root_phys"
    exit 2
fi
if [ "$(git_root rev-parse --is-inside-work-tree 2>/dev/null)" != true ]; then
    stage_err git "root is not inside a git working tree: $root_phys"
    exit 2
fi
# A subdirectory that merely DISCOVERS an ancestor repository is refused: the
# artifacts must belong to the repository that owns this root, not to some
# enclosing checkout the operator never selected.
if ! toplevel=$(git_root rev-parse --show-toplevel 2>/dev/null); then
    stage_err git "cannot resolve this working tree's top level: $root_phys"
    exit 2
fi
if [ "$toplevel" != "$root_phys" ]; then
    stage_err git "root is not the top level of its git working tree (top level is $toplevel): $root_phys"
    exit 2
fi
if ! git_root rev-parse --verify --quiet 'HEAD^{commit}' >/dev/null 2>&1; then
    stage_err git "the working tree has no commit on HEAD (unborn branch); an existing HEAD is required: $root_phys"
    exit 2
fi
if ! current_branch=$(git_root symbolic-ref --quiet --short HEAD 2>/dev/null) \
        || [ -z "$current_branch" ]; then
    stage_err git "HEAD is not attached to a branch (detached HEAD); an attached branch is required: $root_phys"
    exit 2
fi

# An in-progress operation makes "the artifact commit" ambiguous: these markers
# exist only while one is unfinished, and an unmerged index would smuggle
# stage entries into the commit.
if ! git_dir=$(git_root rev-parse --absolute-git-dir 2>/dev/null); then
    stage_err git "cannot resolve the repository's git directory: $root_phys"
    exit 2
fi
for in_progress in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG \
                   sequencer rebase-merge rebase-apply; do
    if [ -e "$git_dir/$in_progress" ]; then
        stage_err git "repository is in an in-progress git operation ($in_progress present); refusing to commit artifacts: $root_phys"
        exit 2
    fi
done
if [ -n "$(git_root ls-files --unmerged 2>/dev/null)" ]; then
    stage_err git "the index has unresolved merge entries; refusing to commit artifacts: $root_phys"
    exit 2
fi

# Both artifacts must be real ordinary files: a symlink or a directory cannot
# be committed as the validated pair, and following a symlink could commit
# content from outside the root.
for artifact_rel in "$ARTIFACT_INDEX" "$ARTIFACT_CATALOG"; do
    artifact_path=$root_phys/$artifact_rel
    if [ -L "$artifact_path" ] || [ ! -f "$artifact_path" ]; then
        stage_err git "artifact is not a regular file (symlinks and directories are rejected): $artifact_path"
        exit 2
    fi
done

# --- does the validated pair actually differ from HEAD? ---------------------
# Compared through the allowlist only, so unrelated work -- however dirty -- can
# never be mistaken for an artifact change. A new artifact has no HEAD entry,
# and ``git diff`` alone would report such an untracked path as unchanged.
artifact_changed=0
for artifact_rel in "$ARTIFACT_INDEX" "$ARTIFACT_CATALOG"; do
    if ! git_root cat-file -e "HEAD:$artifact_rel" 2>/dev/null; then
        artifact_changed=1
        continue
    fi
    compare_rc=0
    git_root diff --quiet --no-ext-diff HEAD -- "$artifact_rel" || compare_rc=$?
    case $compare_rc in
        0) : ;;
        1) artifact_changed=1 ;;
        *)
            stage_err git "cannot compare the working artifact against HEAD (git diff exit $compare_rc): $artifact_rel"
            exit 2
            ;;
    esac
done

if [ "$artifact_changed" -eq 0 ]; then
    # No empty commit, and no touching of unrelated staged work. This says
    # NOTHING about any remote: unit 9d starts its unpushed-commit retry from
    # exactly this state.
    printf '%s: local gate passed: %s (artifact pair validated; no artifact change, nothing committed, nothing pushed or published)\n' \
        "$PROG" "$site_dir" >&2
    exit 0
fi

# --- stage ONLY the two literal allowlisted paths ---------------------------
# This add makes new artifacts known to git (a path-limited commit refuses a
# path it has never seen); it never widens to -A, -u or a directory.
if ! git_root add -- "$ARTIFACT_INDEX" "$ARTIFACT_CATALOG" 1>&2; then
    stage_err git/add "cannot stage the artifact pair for the local commit (index locked or paths ignored): $root_phys"
    exit 2
fi

# --- commit ONLY those paths, with documented path-limited semantics --------
# ``--only`` takes the updated working-tree contents of exactly these paths and
# DISREGARDS whatever is staged for other paths, so unrelated staged work
# cannot ride along. The root's hooks run: a rejecting hook must fail the
# commit, so hooks are never bypassed here.
head_before=$(git_root rev-parse --verify --quiet HEAD 2>/dev/null) || head_before=''
if [ -z "$head_before" ]; then
    stage_err git/commit "cannot resolve HEAD before committing: $root_phys"
    exit 2
fi
if ! git_root commit --quiet --only -m "$COMMIT_MESSAGE" \
        -- "$ARTIFACT_INDEX" "$ARTIFACT_CATALOG" 1>&2; then
    stage_err git/commit "artifact commit failed; no local success is claimed and nothing was pushed: $root_phys"
    exit 2
fi

# --- read the EXACT target back instead of trusting the return status ------
# A commit is only a success once HEAD really advanced onto the previous HEAD
# and the committed blobs are the validated files.
head_after=$(git_root rev-parse --verify --quiet HEAD 2>/dev/null) || head_after=''
if [ -z "$head_after" ] || [ "$head_after" = "$head_before" ]; then
    stage_err git/verify "the commit command reported success but HEAD did not advance: $root_phys"
    exit 2
fi
actual_parent=$(git_root rev-parse --verify --quiet 'HEAD^' 2>/dev/null) || actual_parent=''
if [ "$actual_parent" != "$head_before" ]; then
    stage_err git/verify "the new commit is not a child of the previously validated HEAD: $root_phys"
    exit 2
fi

committed_paths=$(git_root diff-tree --no-commit-id --name-only -r --no-renames HEAD 2>/dev/null) \
    || committed_paths=''
expected_paths="$ARTIFACT_CATALOG
$ARTIFACT_INDEX"
if [ "$committed_paths" != "$expected_paths" ]; then
    stage_err git/verify "the artifact commit does not contain exactly the allowlist ($ARTIFACT_INDEX, $ARTIFACT_CATALOG); found [${committed_paths//$'\n'/, }]: $root_phys"
    exit 2
fi

for artifact_rel in "$ARTIFACT_INDEX" "$ARTIFACT_CATALOG"; do
    committed_blob=$(git_root rev-parse --verify --quiet "HEAD:$artifact_rel" 2>/dev/null) \
        || committed_blob=''
    working_blob=$(git_root hash-object -- "$artifact_rel" 2>/dev/null) || working_blob=''
    if [ -z "$committed_blob" ] || [ "$committed_blob" != "$working_blob" ]; then
        stage_err git/verify "the committed artifact blob does not match the validated file: $artifact_rel"
        exit 2
    fi
done

printf '%s: local gate passed: %s (artifact pair validated and committed locally as %s; nothing pushed, deployed or published)\n' \
    "$PROG" "$site_dir" "$head_after" >&2
exit 0
