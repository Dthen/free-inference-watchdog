#!/usr/bin/env bash
# publish_site.sh — publication gate: locked build, validated pair, restricted
# artifact commit and -- ONLY with the explicit --push opt-in -- one scoped Git
# push with an exact remote read-back.
#
# Sequence (units 9c-9d; Pages validation, hosted CI, the production wrapper
# cutover and any public read-back are LATER units and are deliberately absent
# here):
#
#   1. take ONE exclusive blocking advisory flock on <root>/site/.publish.lock
#      -- a lock inode DISTINCT from the builder's own site/.build.lock -- and
#      hold it continuously through the build, the read-back, the final
#      validation, the local artifact commit AND (in --push mode) the push and
#      the read-back of the destination ref that follows it;
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
#      receipt, whose fixed marker is followed by the TWO raw SHA-256
#      identities of the very byte strings that were decoded and validated.
#      The record is accepted in ONE exact format only -- fixed marker, literal
#      " sha256 ", lowercase 64-hex catalogue digest, exactly ONE ASCII space,
#      lowercase 64-hex index digest, and no other non-newline output -- and it
#      is matched as a whole, never word-split, so an extra space, tab or any
#      other byte cannot be normalised away. The single deliberate
#      normalisation is that trailing NEWLINE(s) of the child's stdout are
#      dropped by command substitution.
#      Those identities are FROZEN there: a later re-read of the files can
#      never redefine what was approved, and a Git-normalised hash is not a
#      substitute for exact-byte identity. An empty, wrong, extra, digest-less,
#      re-spaced or otherwise damaged receipt is a validation failure;
#   4. commit the artifact paths that changed -- site/index.html and
#      site/catalog.json, EITHER ONE ALONE OR BOTH and NEVER a third path -- in
#      the selected root's own Git repository: one path-limited commit with a
#      fixed noninteractive message, after which the committed blobs AND the
#      ordinary working artifacts are compared against those frozen identities.
#      A one-artifact commit is legitimate (a build may restore only the
#      artifact an outside change touched), so the commit's COMPLETE
#      changed-path set must be a NONEMPTY SUBSET of the two literals, read from
#      the whole commit rather than from a path-filtered diff. A hook
#      may legitimately run (hooks are never bypassed) but it cannot substitute
#      the approved bytes: a replaced or re-staged pair fails even when the new
#      pair is itself coherent and passes the validator. Each current working
#      artifact must also still BE an ordinary regular file, established
#      without following a final-component symlink BEFORE its bytes are read,
#      so a post-commit hook that swaps the file for a same-bytes symlink, a
#      directory or any other non-regular type fails too. Before any local
#      success is claimed the exact target commit, its parent, its changed-path
#      set (a nonempty subset of the allowlist) and both of those byte
#      identities are read back.
#
# A nonzero builder exit stops the gate BEFORE validation, even when a valid
# last-good pair is already on disk. A rejected pair stops it BEFORE any Git
# command runs, even when a valid last-good artifact commit and unrelated
# staged work already exist. Neither is ever reported as a successful
# publication, and no failure path stashes, resets, cleans, rebases or
# otherwise repairs the operator's working tree. Verification runs AFTER the
# commit, so a hook that substituted the bytes may already have left a commit
# behind: that is reported honestly with an identifiable Git stage, the commit
# is left for operator handling, and nothing is reset, repaired or pushed.
#
# The root must already BE the top level of an ordinary non-bare Git working
# tree with an existing HEAD on an attached branch and no in-progress
# merge/rebase/cherry-pick/unresolved-index state. This helper never
# initialises a repository, switches a branch, configures an identity or
# repairs a repository; it keeps the root's own user, configuration and hook
# policy. Nothing outside site/index.html and site/catalog.json is staged or
# committed, and a run whose artifacts are unchanged commits nothing at all.
#
# TWO MODES, exactly one of them per run:
#
#   * LOCAL-ONLY (the default): no remote is contacted and no remote command
#     runs. Exit 0 means ONLY that the local artifact pair passed this gate and
#     that its local commit was handled -- never that GitHub or the public site
#     was updated. A no-change run succeeds without saying anything about a
#     remote.
#   * PUBLICATION (--push, opt-in): after the same lock-covered build, the SAME
#     verified candidate is pushed to the selected root's ONE configured
#     upstream branch of its ONE configured upstream remote -- an ordinary
#     explicitly scoped NAMED-remote push, so the remote's own pre-push hooks
#     still receive that configured NAME and its remote-specific transport
#     policy stays in force -- and the exact destination ref at the remote's ONE
#     resolved PUSH URL is read back before any success is claimed. The candidate
#     is the new local artifact commit when this run made one, and the existing
#     HEAD when the artifacts did not change: a previously committed or
#     previously rejected artifact commit is therefore retried on a later run
#     instead of being forgotten, and an already-current destination is reported
#     without a redundant push or an empty commit. The candidate's committed
#     blobs AND both ordinary working artifacts must match this run's frozen
#     identities before any push or remote-success claim, and every unpublished
#     commit in the range from the observed destination tip to the candidate
#     must be an ordinary single-parent, nonempty, artifact-only commit whose
#     pair the real validator accepts -- so unrelated history is never carried
#     along. A divergent/unknown destination tip, a missing destination branch,
#     an ambiguous upstream, a mirror remote or an unreachable endpoint fails
#     closed. Nothing is ever forced, rebased, merged, reset, stashed, fetched,
#     bootstrapped, unshallowed or configured, a zero-exit push is never
#     believed without the exact read-back, and a remote write is never rolled
#     back. Ref equality is a Git-ref publication ONLY: no Pages deployment,
#     hosted CI run or public read-back follows from it.
#
# This helper must never be run concurrently with another git publisher against
# a live checkout.
#
# All operational output, including child builder progress and this script's
# own Git diagnostics, goes to STDERR; a successful run is stdout-silent.
# Remote commands are noninteractive and hard-bounded, and their raw output --
# which can carry the endpoint URL -- is never echoed: diagnostics name the
# stage and the destination ref, never the configured URL value.
# Failures exit 2 with a "publish_site: stage=<stage>: ..." diagnostic on
# stderr, where <stage> is args, root, lock, builder, validate, git,
# git/add, git/commit, git/verify, push/config, push/inspect, push/history,
# push/send or push/verify.
#
# Usage: scripts/publish_site.sh [--root DIR] [--python PATH] [--push]

set -u
set -o pipefail

readonly PROG="publish_site"

# The ONE fixed private success receipt of the read-back validator (see stage 3
# below). It is an internal handshake, never operational or public output, and
# it MUST stay byte-identical to the single print() in the read-back program:
# if the two ever drift apart every run fails CLOSED at stage=validate (the
# gate never accepts a missing receipt), never open.
readonly READBACK_RECEIPT='publish_site: read-back validation passed'

# The receipt is extended into a strictly checked internal RECORD: the fixed
# marker, this field name, and the two raw SHA-256 identities of the exact
# byte strings the validator was given (catalogue first, index second). It is
# never written to disk, added to site/ or exposed in a public payload field.
readonly READBACK_IDENTITY_FIELD='sha256'

# The ONE fixed private success receipt of the post-commit identity check: the
# committed blobs AND the working artifacts match the frozen identities.
readonly IDENTITY_RECEIPT='publish_site: artifact identities match'

# The ONE fixed private success marker of the unpublished-range inspection that
# runs before an opted-in push. Like the read-back receipt it is an internal
# handshake and its stdout is never forwarded; it is emitted only after every
# unpublished commit really passed the checks below, followed by the count.
readonly RANGE_RECEIPT='publish_site: unpublished range verified commits'

# The ONLY paths the local commit may touch (stage 4), and the fixed
# noninteractive message it carries. Both are literal, repo-relative and used
# with an explicit "--" so no pathspec magic or ambient glob can widen them.
readonly ARTIFACT_INDEX='site/index.html'
readonly ARTIFACT_CATALOG='site/catalog.json'
readonly COMMIT_MESSAGE='chore(site): publish dashboard artifacts'

usage() {
    cat <<'USAGE'
usage: publish_site.sh [--root DIR] [--python PATH] [--push]

Run the publication gate for the static dashboard: acquire the dedicated publish
lock, run the real build_site.py against --root, validate the published
site/catalog.json and site/index.html pair by exact read-back, then commit the
artifact paths that changed -- site/index.html and site/catalog.json, either one
alone or both -- in --root's own Git repository.

The commit is path-limited to those two literal paths: they are staged and
committed with a fixed message, while every other tracked, staged or untracked
file is left exactly as it was, and a commit that changes any third path is
rejected. --root must already be the top level of an ordinary non-bare Git
working tree with an existing commit on an attached branch; this helper never
initialises, repairs, rebases or reconfigures a repository. A run whose
artifacts are unchanged commits nothing.

The identity of the pair is frozen when the real validator accepts it -- raw
SHA-256 of those exact bytes -- and, after the commit, both the committed blobs
and the ordinary working files must still match it. Repository hooks run as
usual and are never bypassed, but a hook that substitutes the artifacts (even
with a different, individually valid pair) or that leaves changed working files
behind makes the run fail at the Git verification stage instead of claiming
success. Both working artifacts must still be ordinary regular files there: a
symbolic link, a directory or any other non-regular file is rejected even when
following it would yield the validated bytes.

Without --push this is a LOCAL-ONLY gate: no remote command runs and no network
activity happens. Exit 0 means only that the local artifact pair passed the
gate, its local commit was handled and the committed and working bytes still
matched the validated identities -- never that the public site was updated.

With --push the SAME lock-covered run then publishes that verified candidate --
the new local artifact commit, or the existing HEAD when the artifacts did not
change, so an earlier committed-but-unpushed artifact is retried -- to the
attached branch's single configured upstream branch ref on its single configured
upstream remote, and reads that exact ref back (at that remote's actual push URL)
before claiming success. The send is an ordinary, explicitly scoped NAMED-remote
push, so the remote's own pre-push hooks still receive the configured remote name
and its remote-specific transport settings stay in force. It never guesses a
remote, destination or push.default, never creates tracking configuration, never
bootstraps or fetches a missing branch, never force-pushes, deletes, mirrors or
publishes tags, and fails closed on an absent/ambiguous upstream, a configured
upstream value that is not the name of a configured remote, a mirror remote, a
divergent or unknown destination tip, an unpublished commit range that is not
artifact-only with a valid pair, and a read-back that does not equal the
candidate. Values that could carry credentials (a URL-shaped upstream remote or
destination ref) are never echoed into diagnostics. A push failure leaves the
local commit for a later retry; a remote that may already have changed is
reported honestly and never rolled back. Ref equality is a Git ref publication
ONLY: no Pages deployment, hosted CI run or public read-back is claimed.

options:
  --root DIR     project root holding state/ and receiving site/ (default: the
                 repository this script lives in)
  --python PATH  interpreter for the builder and the read-back validation,
                 taken as ONE executable path (default: python3 from PATH)
  --push         publish the verified candidate to the configured upstream
                 branch ref (opt-in; without it nothing contacts a remote)
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
push_enabled=0

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
        --push)
            push_enabled=1
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
# It reads each artifact ONCE, hashes those very bytes with stdlib SHA-256 and
# validates the strings decoded FROM THOSE BYTES: the identities it reports are
# therefore the identity of exactly what the real validator accepted, not of a
# second open of a file a hook may since have changed.
#
# Its stdout is CAPTURED, never forwarded: the only thing it ever writes there
# is the private success record, emitted after the validator returned
# normally. Exit status alone cannot prove the program ran (a selected
# executable may ignore its arguments and stdin, do nothing and return zero),
# so the gate requires BOTH a zero exit AND that exact record.
#
# Bash command substitution deletes every trailing newline, so the compared
# value deliberately has none: that is the ONLY normalisation of the record, and
# it is why a record followed by blank line(s) still counts as exact. Everything
# else is compared byte for byte against ONE anchored expression below -- never
# through a word-splitting ``read``, which would collapse runs of space and tab
# and ignore leading/trailing whitespace, silently turning a corrupted record
# back into the exact one.
validate_rc=0
validate_out=$("$python_bin" - "$root" "$repo_root" <<'PY'
import hashlib
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
    catalog_bytes = catalog_path.read_bytes()
    html_bytes = html_path.read_bytes()
except OSError as exc:
    print(f"publish_site: cannot read back artifacts: {exc}", file=sys.stderr)
    raise SystemExit(2)

try:
    catalog_text = catalog_bytes.decode("utf-8", "strict")
    html_text = html_bytes.decode("utf-8", "strict")
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

# Success record: printed ONLY after the validator returned normally above, and
# the only thing this program ever writes to stdout. The marker must stay
# byte-identical to READBACK_RECEIPT in the shell; the two identities are the
# raw SHA-256 of the bytes decoded and validated above, in catalogue,index
# order, and are frozen for the whole Git phase.
print("publish_site: read-back validation passed "
      f"sha256 {hashlib.sha256(catalog_bytes).hexdigest()} "
      f"{hashlib.sha256(html_bytes).hexdigest()}")
PY
) || validate_rc=$?
if [ "$validate_rc" -ne 0 ]; then
    stage_err validate "read-back validation failed (exit $validate_rc): $site_dir"
    exit 2
fi

# --- strict acceptance of the extended receipt RECORD ----------------------
# Exactly: the fixed marker, the literal field name, ONE ASCII space, the
# lowercase 64-hex catalogue identity, ONE ASCII space, the lowercase 64-hex
# index identity -- and no other non-newline output. The WHOLE captured value
# is matched against ONE anchored POSIX extended regular expression, so every
# byte distinction survives the check; a missing, corrupted, repeated,
# multi-line, digest-less or extra-byte record is a validation failure, and an
# extended format can only ever fail CLOSED.
#
# Word splitting is deliberately NOT used here. ``read`` with the default IFS
# collapses runs of space, tab and newline and ignores leading and trailing
# whitespace (see the shell's Word Splitting and ``read`` documentation), so an
# appended space, an appended tab, a doubled space after the field name or a
# tab between the two identities would all have been silently NORMALISED into
# the exact record and accepted.
#
# The single deliberate normalisation has already happened: command
# substitution removes every trailing NEWLINE of the child's stdout, so an
# otherwise exact record followed by trailing newline(s) is still accepted. Any
# other non-newline byte, and a newline anywhere but the end, cannot match --
# the expression is anchored at both ends and neither hex field can span one.
catalog_digest=''
index_digest=''
readback_pattern="^${READBACK_RECEIPT} ${READBACK_IDENTITY_FIELD} ([0-9a-f]{64}) ([0-9a-f]{64})\$"
if [[ $validate_out =~ $readback_pattern ]]; then
    catalog_digest=${BASH_REMATCH[1]}
    index_digest=${BASH_REMATCH[2]}
fi
if [ -z "$catalog_digest" ] || [ -z "$index_digest" ]; then
    stage_err validate "read-back validator did not confirm execution (${#validate_out} chars of stdout, expected the private success receipt with two exact artifact identities): $site_dir"
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

# --- local artifact commit: only when the validated pair changed ------------
# Kept as one function so the commit path and the no-change path can rejoin
# ONE publication path further down. It stages exactly the two literals,
# commits with --only, and reads the target commit, its parent, its complete
# changed-path set and its stored path list back before returning; on success
# it sets ``candidate`` to the commit that publication must carry.
commit_artifact_pair() {
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
    # commit, so hooks are never bypassed here, and a hook that instead MUTATES or
    # re-stages the artifacts is caught by the frozen-identity check below.
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
    # A commit is only a success once HEAD really advanced onto the previous HEAD,
    # changed a nonempty subset of the allowlist, and both committed blobs AND both
    # working artifacts still hash to the identities frozen at validation.
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

    # --- read the WHOLE commit's changed-path set back --------------------------
    # The allowed set is a NONEMPTY SUBSET of the two literals, not an exact
    # two-path list: a build that restores only the one artifact an outside change
    # touched is a legitimate artifact commit, and so is one that restores both.
    # The inspection is deliberately UNFILTERED -- no trailing pathspec -- because
    # a path-filtered diff would report just the allowlisted paths and hide a third
    # path that a hook or a widened commit smuggled into the same commit.
    paths_rc=0
    committed_paths=$(git_root diff-tree --no-commit-id --name-only -r --no-renames HEAD 2>/dev/null) \
        || paths_rc=$?
    if [ "$paths_rc" -ne 0 ]; then
        stage_err git/verify "cannot read the artifact commit's changed paths back (git diff-tree exit $paths_rc); a commit may already exist at $head_after and is left for operator handling (nothing was reset, repaired, retried or pushed): $root_phys"
        exit 2
    fi
    if [ -z "$committed_paths" ]; then
        stage_err git/verify "the commit advanced HEAD but reports no changed path; no artifact commit is claimed, a commit may already exist at $head_after and is left for operator handling: $root_phys"
        exit 2
    fi
    outside_paths=''
    while IFS= read -r committed_rel; do
        case $committed_rel in
            "$ARTIFACT_INDEX"|"$ARTIFACT_CATALOG") : ;;
            *)
                if [ -n "$outside_paths" ]; then
                    outside_paths="$outside_paths, $committed_rel"
                else
                    outside_paths=$committed_rel
                fi
                ;;
        esac
    done <<< "$committed_paths"
    if [ -n "$outside_paths" ]; then
        stage_err git/verify "the artifact commit changes paths outside the allowlist ($ARTIFACT_INDEX, $ARTIFACT_CATALOG): outside [$outside_paths]; the whole commit changes [${committed_paths//$'\n'/, }]; a commit may already exist at $head_after and is left for operator handling (nothing was reset, repaired, retried or pushed): $root_phys"
        exit 2
    fi

    # The verified commit that publication must carry: assigned here so the
    # commit path and the no-change path converge on ONE candidate value.
    candidate=$head_after
}

# --- rejoin the commit and no-change paths before publication ---------------
# A no-change run in local-only mode keeps the reviewed behaviour exactly: no
# empty commit, no touching of unrelated staged work, and nothing claimed about
# any remote. In opted-in publication mode it must NOT exit here -- an earlier
# run may have committed (or already tried to publish) an artifact commit that
# these identical bytes cannot re-create, so both paths continue to ONE
# identity-checked candidate and then to ONE publication path.
candidate=''
if [ "$artifact_changed" -eq 0 ]; then
    if [ "$push_enabled" -eq 0 ]; then
        printf '%s: local gate passed: %s (artifact pair validated; no artifact change, nothing committed, nothing pushed or published)\n' \
            "$PROG" "$site_dir" >&2
        exit 0
    fi
    candidate=$(git_root rev-parse --verify --quiet HEAD 2>/dev/null) || candidate=''
    if [ -z "$candidate" ]; then
        stage_err git "cannot resolve HEAD for an unchanged publication run: $root_phys"
        exit 2
    fi
    printf '%s: no artifact change: the publication candidate is the existing commit %s\n' \
        "$PROG" "$candidate" >&2
else
    commit_artifact_pair
fi

# --- compare the COMMITTED and the WORKING bytes to the FROZEN identities --
# The approved pair is the raw byte identity captured at the validation
# observation and carried privately through the receipt. Re-reading the files
# here cannot redefine it, and a Git-normalised blob hash is not a substitute
# for exact-byte identity, so both committed blobs AND both ordinary working
# artifacts are hashed with stdlib SHA-256 and compared against those two
# identities. A hook may therefore NOT substitute the approved bytes: a
# different but individually coherent pair -- which the validator would accept
# again -- fails here, and so does a hook that changed only the working files
# without staging them.
#
# The working comparison additionally requires each artifact to STILL BE an
# ordinary regular file, established with ``lstat`` and a symlink-refusing open
# BEFORE its bytes are read: a ``post-commit`` hook can replace the validated
# file with a link to a copy holding identical bytes, and following that link
# would compare the right bytes at the wrong path. A symlink, a directory or any
# other non-regular type is a verification failure even when the bytes behind it
# are the validated ones.
#
# When the commit (or a hook) already produced a substituted commit, this is
# reported and that commit is left for operator handling: nothing is reset,
# repaired, retried, rewritten or pushed.
identity_rc=0
identity_out=$("$python_bin" - identity "$root_phys" "$candidate" \
    "$catalog_digest" "$index_digest" <<'PY'
import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

if sys.argv[1] != "identity":
    print("publish_site: unexpected identity-check invocation",
          file=sys.stderr)
    raise SystemExit(2)

target = sys.argv[2]
commit = sys.argv[3]
expected = (
    ("site/catalog.json", sys.argv[4]),
    ("site/index.html", sys.argv[5]),
)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def working_bytes(target, rel):
    """Exact bytes of a WORKING artifact that must be an ordinary regular file.

    ``Path.read_bytes`` (and a plain ``open``) FOLLOW a final-component symbolic
    link, so a completed ``post-commit`` hook could replace the validated
    artifact with a link to a copy holding identical bytes and a byte-only
    comparison of the working path would still pass. The file type is therefore
    established WITHOUT following that link -- ``lstat`` first, then an open
    that refuses a symlink -- and the OPENED descriptor is confirmed to be a
    regular file before any byte is read. This inspects the artifacts the
    synchronous hook already left behind; it is not a general hostile
    filesystem race-hardening layer.
    """
    path = Path(target) / rel
    try:
        info = os.lstat(path)
    except OSError as exc:
        print("publish_site: cannot read the working artifact back: "
              f"{rel}: {exc}", file=sys.stderr)
        raise SystemExit(2)
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
        print("publish_site: the working artifact is not an ordinary regular "
              f"file ({detail}): {rel}", file=sys.stderr)
        raise SystemExit(2)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.fdopen(os.open(str(path), flags), "rb")
    except OSError as exc:
        print("publish_site: cannot read the working artifact back: "
              f"{rel}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    with handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            print("publish_site: the working artifact is not an ordinary "
                  f"regular file (it was not one when opened): {rel}",
                  file=sys.stderr)
            raise SystemExit(2)
        return handle.read()


# The committed bytes are checked first: when a hook substituted the pair, the
# commit is what an operator has to deal with, and the diagnostic should say
# so rather than blame the working tree.
for rel, frozen in expected:
    try:
        proc = subprocess.run(
            ["git", "-C", target, "cat-file", "blob", f"{commit}:{rel}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        print("publish_site: cannot run git to read the committed artifact "
              f"back: {rel}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        print(f"publish_site: cannot read the committed artifact back: "
              f"{rel}: {detail}", file=sys.stderr)
        raise SystemExit(2)
    committed = sha256(proc.stdout)
    if committed != frozen:
        print("publish_site: the committed artifact does not match the "
              f"validated bytes: {rel} (validated sha256 {frozen}, "
              f"committed sha256 {committed})", file=sys.stderr)
        raise SystemExit(2)

for rel, frozen in expected:
    working = working_bytes(target, rel)
    actual = sha256(working)
    if actual != frozen:
        print("publish_site: the working artifact does not match the "
              f"validated bytes: {rel} (validated sha256 {frozen}, "
              f"working sha256 {actual})", file=sys.stderr)
        raise SystemExit(2)

# Success receipt: the only thing this program ever writes to stdout, and it
# must stay byte-identical to IDENTITY_RECEIPT in the shell.
print("publish_site: artifact identities match")
PY
) || identity_rc=$?
if [ "$identity_rc" -ne 0 ]; then
    stage_err git/verify "the artifact bytes or working artifact types at the target commit do not match the frozen validated identities; a commit may already exist at $candidate and is left for operator handling (nothing was reset, repaired, retried or pushed): $root_phys"
    exit 2
fi
if [ "$identity_out" != "$IDENTITY_RECEIPT" ]; then
    stage_err git/verify "the post-commit identity check did not confirm execution (expected its private receipt): $root_phys"
    exit 2
fi

# --- local-only success: unchanged wording, no remote command --------------
if [ "$push_enabled" -eq 0 ]; then
    printf '%s: local gate passed: %s (artifact pair validated and committed locally as %s; nothing pushed, deployed or published)\n' \
        "$PROG" "$site_dir" "$candidate" >&2
    exit 0
fi

# --- publication (opt-in): ONE unambiguous Git destination ------------------
# Everything below runs ONLY when the caller explicitly asked for publication,
# so the default mode above returned without a single remote command. ONE
# observation, ONE destination: the attached branch's existing upstream remote
# and branch ref, at that remote's single resolved PUSH URL. Nothing is guessed
# -- not a remote, not a branch name, not push.default, not a pushRemote, not
# the remote's default refspec -- and no configuration is written, so an absent
# or ambiguous upstream fails visibly instead of publishing somewhere else.
if ! command -v timeout >/dev/null 2>&1; then
    stage_err push/config "bounded remote operations require the timeout command; refusing to contact the remote without one: $root_phys"
    exit 2
fi

# A dead endpoint must not be able to hang the gate: every remote command is
# noninteractive (no credential prompt can wait on a terminal) and hard-bounded.
# The environment override exists for a slow operator link, never to make a run
# unbounded, and it cannot influence which destination is chosen.
remote_timeout_s=${PUBLISH_SITE_REMOTE_TIMEOUT_S:-120}
case $remote_timeout_s in
    ''|*[!0-9]*) remote_timeout_s=120 ;;
esac
if [ "$remote_timeout_s" -lt 1 ]; then
    remote_timeout_s=120
fi

remote_git() {
    GIT_TERMINAL_PROMPT=0 timeout -k 5 "$remote_timeout_s" \
        git -C "$root_phys" "$@"
}

# one_config_value <key>: the single configured value, or a distinct status.
# ``git config --get-all`` is used deliberately: ``--get`` silently picks one of
# several values, which is exactly the ambiguity that must be refused here.
one_config_value() {
    local values count
    values=$(git_root config --get-all "$1" 2>/dev/null) || values=''
    if [ -z "$values" ]; then
        return 1
    fi
    count=$(printf '%s\n' "$values" | grep -c '')
    if [ "$count" -ne 1 ]; then
        return 3
    fi
    printf '%s' "$values"
    return 0
}

remote_name=$(one_config_value "branch.$current_branch.remote")
resolution_rc=$?
case $resolution_rc in
    0) : ;;
    1)
        stage_err push/config "the attached branch '$current_branch' has no configured upstream remote (branch.$current_branch.remote); refusing to guess a remote, a pushRemote or a destination: $root_phys"
        exit 2
        ;;
    *)
        stage_err push/config "the upstream remote of the attached branch '$current_branch' is configured more than once (branch.$current_branch.remote); exactly one upstream remote is required: $root_phys"
        exit 2
        ;;
esac
if [ "$remote_name" = "." ]; then
    stage_err push/config "the configured upstream is the local repository ('.'); a named remote with exactly one resolved PUSH URL is required: $root_phys"
    exit 2
fi
case $remote_name in
    *[[:space:]]*)
        stage_err push/config "the configured upstream remote name contains whitespace; exactly one named remote is required (the value is withheld because it may be a credential-bearing URL): $root_phys"
        exit 2
        ;;
    -*)
        stage_err push/config "the configured upstream remote name starts with an option-like '-' and cannot name a remote here; the value is withheld because it may be a credential-bearing URL: $root_phys"
        exit 2
        ;;
esac

# A configured ALIAS -- not a URL -- is the only acceptable value. A
# misconfigured branch.<branch>.remote can hold a credential-bearing URL, so a
# value that is not the name of a configured remote is refused WITHOUT being
# echoed, never normalised into an endpoint and never contacted. ``git remote``
# is a purely local configuration listing: it contacts nothing.
remote_is_configured=0
configured_remotes=$(git_root remote 2>/dev/null) || configured_remotes=''
while IFS= read -r listed_remote; do
    [ -n "$listed_remote" ] || continue
    if [ "$listed_remote" = "$remote_name" ]; then
        remote_is_configured=1
    fi
done <<< "$configured_remotes"
if [ "$remote_is_configured" -eq 0 ]; then
    stage_err push/config "the configured upstream of the attached branch '$current_branch' (branch.$current_branch.remote) is not the name of a configured remote; the value is neither echoed nor resolved into an endpoint, and nothing is contacted: $root_phys"
    exit 2
fi

destination_ref=$(one_config_value "branch.$current_branch.merge")
resolution_rc=$?
case $resolution_rc in
    0) : ;;
    1)
        stage_err push/config "the attached branch '$current_branch' has no configured upstream branch ref (branch.$current_branch.merge); refusing to derive one from push.default or from the local branch name: $root_phys"
        exit 2
        ;;
    *)
        stage_err push/config "the upstream branch ref of the attached branch '$current_branch' is configured more than once (branch.$current_branch.merge); exactly one branch destination is required: $root_phys"
        exit 2
        ;;
esac
case $destination_ref in
    refs/heads/?*) : ;;
    *)
        stage_err push/config "the configured upstream ref is not a single branch under refs/heads/; only one branch destination is publishable: $root_phys"
        exit 2
        ;;
esac
case $destination_ref in
    *[[:space:]]*|*'*'*|*'?'*|*'['*|*..*|*/|*//*)
        stage_err push/config "the configured upstream ref of the attached branch '$current_branch' (branch.$current_branch.merge) carries characters a single branch destination cannot; the raw value is withheld because it may be a credential-bearing URL: $root_phys"
        exit 2
        ;;
esac
if ! git_root check-ref-format "$destination_ref" >/dev/null 2>&1; then
    stage_err push/config "the configured upstream ref of the attached branch '$current_branch' (branch.$current_branch.merge) is not a valid Git ref; the raw value is withheld because it may be a credential-bearing URL: $root_phys"
    exit 2
fi

# A mirror remote pushes EVERY ref by its own definition, so it is not a
# single-branch publication destination.
mirror_value=$(git_root config --bool --get "remote.$remote_name.mirror" 2>/dev/null) \
    || mirror_value=false
if [ "$mirror_value" = true ]; then
    stage_err push/config "the upstream remote '$remote_name' is configured as a mirror remote (remote.$remote_name.mirror); a mirror is not a single-branch publication destination: $root_phys"
    exit 2
fi

# EXACTLY ONE resolved push URL -- and it, NOT the fetch URL that may differ, is
# the endpoint every inspection and read-back below addresses.
url_rc=0
push_urls=$(git_root remote get-url --push --all "$remote_name" 2>/dev/null) \
    || url_rc=$?
if [ "$url_rc" -ne 0 ]; then
    stage_err push/config "cannot resolve the push URL of the upstream remote '$remote_name' (git remote get-url --push --all exited $url_rc): $root_phys"
    exit 2
fi
url_count=$(printf '%s\n' "$push_urls" | grep -c '')
if [ "$url_count" -ne 1 ]; then
    stage_err push/config "the upstream remote '$remote_name' resolves $url_count push URLs; exactly one is required (no URL value is printed): $root_phys"
    exit 2
fi
push_url=$push_urls

# observe_remote_tip: the object id of ONLY the exact destination ref, as the
# PUSH endpoint really reports it. The raw remote output is never forwarded (it
# can carry the endpoint URL), and every malformed, duplicated, foreign or
# unreadable observation is a failure rather than a guess.
remote_problem=''
remote_tip=''
observe_remote_tip() {
    local probe_rc out line ref oid
    remote_problem=''
    remote_tip=''
    probe_rc=0
    out=$(remote_git ls-remote --refs --exit-code "$push_url" "$destination_ref" 2>/dev/null) \
        || probe_rc=$?
    case $probe_rc in
        0) : ;;
        2)
            remote_problem="the destination branch $destination_ref does not exist on the push endpoint (a missing destination branch is not bootstrapped, fetched or repaired automatically)"
            return 1
            ;;
        *)
            remote_problem="the push endpoint could not be queried (git ls-remote exited $probe_rc; the endpoint URL is withheld from diagnostics)"
            return 1
            ;;
    esac
    oid=''
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case $line in
            *$'\t'*) : ;;
            *)
                remote_problem="the push endpoint returned a malformed ref observation"
                return 1
                ;;
        esac
        ref=${line#*$'\t'}
        if [ "$ref" != "$destination_ref" ]; then
            remote_problem="the push endpoint returned a ref other than the requested destination"
            return 1
        fi
        if [ -n "$oid" ]; then
            remote_problem="the push endpoint returned the destination ref more than once"
            return 1
        fi
        oid=${line%%$'\t'*}
    done <<< "$out"
    if [ -z "$oid" ]; then
        remote_problem="the push endpoint returned no observation for the destination ref"
        return 1
    fi
    case $oid in
        *[!0-9a-f]*)
            remote_problem="the observed destination object id is malformed"
            return 1
            ;;
    esac
    case ${#oid} in
        40|64) : ;;
        *)
            remote_problem="the observed destination object id has an unexpected length"
            return 1
            ;;
    esac
    remote_tip=$oid
    return 0
}

# --- the real destination tip decides: current, ahead or unreconcilable ----
# A tracking ref may be stale, so only the endpoint itself is authoritative.
if ! observe_remote_tip; then
    stage_err push/inspect "$remote_problem (nothing was pushed, reset, fetched or repaired): $root_phys"
    exit 2
fi
if [ "$remote_tip" = "$candidate" ]; then
    printf '%s: publish complete: %s (Git destination %s %s at %s is current: the verified artifact commit is already on the remote, so no push and no commit were made; this is a Git ref only -- no Pages deployment, hosted CI or public read-back)\n' \
        "$PROG" "$site_dir" "$remote_name" "$destination_ref" "$candidate" >&2
    exit 0
fi
if ! git_root cat-file -e "$remote_tip^{commit}" 2>/dev/null; then
    stage_err push/inspect "the destination tip $remote_tip is not a known commit in this repository; an unknown remote-tip object fails closed (no fetch, branch bootstrap or repair is attempted): $root_phys"
    exit 2
fi
if ! git_root merge-base --is-ancestor "$remote_tip" "$candidate" 2>/dev/null; then
    stage_err push/inspect "the destination $destination_ref at $remote_tip is not an ancestor of the candidate $candidate (the candidate is behind the destination or the histories have diverged); refusing to force, rebase, merge or reset: $root_phys"
    exit 2
fi

# --- every UNPUBLISHED commit must be a valid artifact-only commit ---------
# The candidate may sit on top of artifact commits an earlier run made but never
# published. That range is inspected as a whole: an ordinary single-parent,
# nonempty, artifact-only commit whose pair the REAL validator accepts is the
# only thing this publisher may carry, so unrelated source/private history can
# never be swept along and a previously failed verification cannot authorize a
# malformed intermediate artifact commit.
# Its stdout is captured, never forwarded: like the read-back gate it proves it
# really executed through ONE fixed private receipt.
range_rc=0
range_out=$("$python_bin" - range "$root_phys" "$repo_root" "$remote_tip" "$candidate" <<'PY'
import subprocess
import sys

MARKER = "publish_site: unpublished range verified commits"
ARTIFACTS = ("site/catalog.json", "site/index.html")

if sys.argv[1] != "range":
    print("publish_site: unexpected range-check invocation", file=sys.stderr)
    raise SystemExit(2)

target = sys.argv[2]
checkout = sys.argv[3]
remote_tip = sys.argv[4]
candidate = sys.argv[5]

sys.path[:] = [entry for entry in sys.path if entry not in ("", ".", target)]
sys.path.insert(0, checkout)


def fail(message):
    print(f"publish_site: unpublished range rejected: {message}", file=sys.stderr)
    raise SystemExit(2)


def git(*args):
    proc = subprocess.run(["git", "-C", target, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        fail("git " + " ".join(args[:1]) + f" exited {proc.returncode}")
    return proc.stdout


try:
    from build_site import validate_artifact_pair
except Exception as exc:  # noqa: BLE001 - every import failure is a gate failure
    fail(f"cannot import the real validator: {type(exc).__name__}: {exc}")

raw = git("rev-list", "--reverse", "--topo-order", f"{remote_tip}..{candidate}")
commits = [line for line in raw.decode("utf-8", "strict").splitlines() if line]
if not commits:
    fail("the unpublished range is empty although the destination tip differs "
         "from the candidate")

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
    blobs = {rel: git("cat-file", "blob", f"{commit}:{rel}") for rel in ARTIFACTS}
    try:
        catalog_text = blobs[ARTIFACTS[0]].decode("utf-8", "strict")
        html_text = blobs[ARTIFACTS[1]].decode("utf-8", "strict")
    except UnicodeDecodeError:
        fail(f"unpublished commit {commit} holds artifacts that are not valid "
             "UTF-8")
    try:
        validate_artifact_pair(catalog_text, html_text)
    except Exception as exc:  # noqa: BLE001 - the real validator decides
        fail(f"unpublished commit {commit} does not hold a pair the real "
             f"validator accepts: {type(exc).__name__}: {exc}")

print(f"{MARKER} {len(commits)}")
PY
) || range_rc=$?
if [ "$range_rc" -ne 0 ]; then
    stage_err push/history "the unpublished commit range from the observed destination tip $remote_tip to the candidate $candidate was rejected (exit $range_rc); nothing was pushed: $root_phys"
    exit 2
fi
range_count=''
range_pattern="^${RANGE_RECEIPT} ([1-9][0-9]*)\$"
if [[ $range_out =~ $range_pattern ]]; then
    range_count=${BASH_REMATCH[1]}
fi
if [ -z "$range_count" ]; then
    stage_err push/history "the unpublished-range check did not confirm execution (expected its private receipt with a positive commit count): $root_phys"
    exit 2
fi

# --- ONE ordinary, explicitly scoped named-remote push of the fixed object --
# The destination is the CONFIGURED REMOTE NAME, not a flattened URL: git hands
# that name to the remote's own hooks (pre-push receives the remote name as its
# first argument for a named push) and applies that remote's own transport
# policy -- remote.<name>.receivepack, for instance -- so an operator policy
# scoped to the alias keeps rejecting and remote-specific settings stay in
# force. The explicit ``<candidate>:<refs/heads/...>`` refspec still means the
# remote's default refspec, push.default, matching/all-branch behaviour and the
# pushRemote cannot widen it, and --no-follow-tags overrides push.followTags so
# no tag is ever published with the branch. The actual single resolved PUSH URL
# remains the endpoint every inspection and read-back above/below addresses, so
# a fetch URL that differs is never used and a remote whose configuration
# resolves to several push URLs was already refused. Nothing is forced,
# deleted, mirrored or verified around: hooks, signing and authentication stay
# in force and there is no hook-bypass flag anywhere in this script. Both
# streams are withheld (the endpoint can appear in ordinary push chatter, and a
# rejected push must not become a success claim): only the exit status counts.
push_rc=0
remote_git push --quiet --no-follow-tags --recurse-submodules=no \
    "$remote_name" "$candidate:$destination_ref" >/dev/null 2>&1 || push_rc=$?
if [ "$push_rc" -ne 0 ]; then
    stage_err push/send "the push of $candidate to $destination_ref was rejected or failed (git push exited $push_rc); no remote success is claimed and the local artifact commit is retained for a later retry (the endpoint URL and the remote's own output are withheld): $root_phys"
    exit 2
fi

# --- read the EXACT destination ref back before claiming anything ----------
# A zero-exit push is not a publication: the ref at the endpoint must equal the
# candidate object. When it does not, the remote may already have changed, which
# is reported honestly -- nothing is rolled back, re-pushed or repaired here.
if ! observe_remote_tip; then
    stage_err push/verify "the push command reported success but the destination ref cannot be read back ($remote_problem); a remote write may already have occurred and is not rolled back, retried or repaired automatically: $root_phys"
    exit 2
fi
if [ "$remote_tip" != "$candidate" ]; then
    stage_err push/verify "the push command reported success but the destination $destination_ref reads back as $remote_tip instead of $candidate; a remote write may already have occurred and is not rolled back, retried or repaired automatically: $root_phys"
    exit 2
fi

printf '%s: publish complete: %s (pushed %s to Git destination %s %s and read that exact ref back; this is a Git ref publication only -- no Pages deployment, hosted CI or public read-back follows)\n' \
    "$PROG" "$site_dir" "$candidate" "$remote_name" "$destination_ref" >&2
exit 0
