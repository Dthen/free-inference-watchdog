"""Units 9b-9d: publisher lock, validated-build gate, local commit, opt-in push.

The helper is a local publication gate: it takes ONE dedicated publish lock,
runs the real ``build_site.py`` against an explicit ``--root``, reads the EXACT
final ``site/catalog.json`` + ``site/index.html`` bytes back and hands them to
the builder's own ``validate_artifact_pair``, and -- only after that pair is
accepted -- commits exactly those two allowlisted paths in the selected root's
own Git repository, inside the SAME lock lifetime. Without ``--push`` it contacts
no remote at all: exit 0 means only that the local artifact pair was validated
and its local commit handled, never that GitHub or the public site changed.

With the explicit ``--push`` opt-in (unit 9d) the SAME lock lifetime then covers
publication: the verified candidate commit is pushed to the selected root's one
configured upstream branch ref at its one resolved PUSH URL, and the exact
destination ref is read back before any success is claimed. A no-artifact-change
run rejoins that path instead of exiting early, so a previously committed (or
previously rejected) artifact commit is retried rather than forgotten. A
zero-exit push is never trusted, a remote write is never rolled back, and no
force, rebase, reset, fetch, branch bootstrap, config write or tag publication is
authorized. Ref equality is a Git-ref publication, NOT proof of Pages deployment,
hosted CI or any public read-back.

Pinned here, through real Bash subprocess execution against owned synthetic
roots (no provider call, credential, installed Hermes state, public remote or
live roster):

* a genuine build from a synthetic roster passes from an unrelated cwd with
  spaces in the root AND interpreter paths, writes both artifacts, emits an
  EMPTY stdout and leaves a pair the existing validator accepts;
* a missing/corrupt roster exits 2 with ``stage=builder`` and the validation
  step is never entered -- a valid last-good pair cannot rescue it;
* a builder that "succeeds" but leaves a missing, mismatching, invalid-JSON or
  non-UTF-8 pair is rejected by the INDEPENDENT read-back gate
  (``stage=validate``), with the real validator untouched, and a target root
  carrying its own permissive ``build_site`` / ``watchdog_catalog`` cannot
  shadow it;
* exit 0 alone never proves the read-back validator executed: ``/bin/true``
  and any executable that ignores its arguments and stdin (also over a valid
  last-good pair) fail at ``stage=validate`` because the validator's ONE fixed
  private stdout receipt is missing, while a real successful validation whose
  owned wrapper drops, corrupts, duplicates or misroutes that receipt fails
  there too -- even though its child exited zero on a genuinely valid pair;
* bad arguments, an unusable root, ``--help``, an unopenable lock path and a
  host without the ``flock`` command all fail (or, for ``--help``, succeed)
  before the builder starts, and the default root plus the builder path follow
  the helper's own checkout rather than the cwd or ``--root``;
* one exclusive publish lock covers build, read-back and validation: two helper
  processes cannot overlap their critical sections, a nonblocking acquisition
  during the final validation fails, the lock inode is retained across runs,
  and success, failure and a SIGKILLed helper all release it;
* the local Git phase commits ONLY ``site/index.html`` and ``site/catalog.json``
  with ``--only`` path-limited semantics in the selected root's own repository:
  a commit may change EITHER artifact alone or both, its COMPLETE changed-path
  set must be a nonempty subset of those two literals, unrelated
  tracked/staged/untracked work keeps its bytes, mode, blob and stage
  identity and never enters the commit, an unchanged pair commits nothing and
  leaves HEAD alone, a rejecting hook or a failing ``git add`` fails at exit 2
  without advancing HEAD, a failed build or read-back never invokes Git at all,
  and non-repository / nested-root / detached-HEAD targets are refused without
  touching an ancestor or this helper's own checkout;
* the identity of the validated pair is FROZEN at the read-back observation --
  raw SHA-256 of the exact bytes the real validator accepted, carried in the
  private receipt -- and the committed blobs AND the ordinary working artifacts
  are compared against those frozen identities after the real commit, so a real
  ``pre-commit`` hook that stages an invalid catalogue, a hook that substitutes
  a different but individually COHERENT builder-made pair (which the real
  validator still accepts), a ``post-commit`` hook that rewrites only the
  working bytes and a ``post-commit`` hook that leaves a same-bytes SYMLINK in
  place of either working artifact are all rejected at ``stage=git/verify``; a
  non-mutating hook still publishes normally with the committed and working
  bytes equal to the hook-time witness of the validated pair;
* the private record is accepted only in its EXACT format -- fixed marker,
  literal ``sha256``, one space, lowercase 64-hex catalogue digest, one space,
  lowercase 64-hex index digest, nothing else -- so an appended space or tab, a
  doubled space after ``sha256`` or a tab between the digests is rejected at
  ``stage=validate`` before any Git command runs, while an otherwise exact
  record followed by trailing newline(s) is still accepted (the one deliberate
  normalisation, performed by Bash command substitution);
* the publication lock is still held INSIDE a real commit hook: a second
  helper cannot enter its builder while the hook runs, and an independent
  nonblocking acquisition of the same inode only succeeds afterwards;
* the explicit ``--push`` opt-in publishes the verified candidate to the one
  configured upstream branch of the one configured upstream remote, refuses to
  guess a remote, branch, ``push.default`` or tag set, retries a previously
  committed but unpushed candidate on a no-change run, refuses a divergent
  destination or an unpublished range that carries a foreign path, and never
  claims success from a zero-exit push or a stale ref read-back (see the
  publication test below);
* that publication is an ordinary explicitly scoped NAMED-remote push, so the
  remote's own ``pre-push`` hooks still receive the configured remote name (a
  hook policy scoped to that alias keeps rejecting) and its remote-specific
  transport settings -- ``remote.<name>.receivepack``, for instance -- stay in
  force, while inspection and read-back still address the remote's actual
  single PUSH URL rather than a possibly different fetch URL;
* a URL-shaped invalid ``branch.<branch>.remote`` or destination ref is refused
  at ``stage=push/config`` without echoing the value: such a value is never
  normalised into an endpoint, never contacted, and its userinfo/token
  fragments never reach stderr.

Fault injection and coordination live ONLY in this test module: the production
helper exposes no test switch, callback or fake-success mode. Artifact commits
and pushes are created only in owned temporary repositories: every push and every
remote read-back in this module targets a local BARE repository created under the
test's own tmp dir, never the public repository, never a provider and never an
installed wrapper.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "scripts" / "publish_site.sh"

#: Explicit POSIX platform guard (unit 9f). This suite drives a POSIX Bash
#: helper that takes a ``flock`` advisory lock and invokes git, so it belongs to
#: the Linux CI job and the operator's POSIX host -- never to the Windows
#: package/core smoke (``tests/consumer_smoke.py``). The guard SKIPS with an
#: actionable reason instead of failing collection on a missing binary or a
#: non-POSIX ``os.name``, and it never weakens an assertion on a supported host.
MISSING_POSIX_TOOLS = tuple(
    tool for tool in ("bash", "flock", "git") if shutil.which(tool) is None)
pytestmark = pytest.mark.skipif(
    os.name != "posix" or bool(MISSING_POSIX_TOOLS),
    reason=("the publish wrapper gate is POSIX-only: os.name=%r, missing=%r; "
            "run it in the Linux CI job" % (os.name, MISSING_POSIX_TOOLS)))

SITE = Path("site")
CATALOG = SITE / "catalog.json"
INDEX = SITE / "index.html"
PUBLISH_LOCK = SITE / ".publish.lock"
BUILD_LOCK = SITE / ".build.lock"

# The helper's ONE fixed private success receipt. It is an internal handshake:
# it must never appear in an artifact or any public output field.
READBACK_RECEIPT = "publish_site: read-back validation passed"

# The reproduced whitespace family: an otherwise EXACT private record with one
# non-newline byte added or substituted. Each is emitted by an owned wrapper
# that first ran the REAL validator on the REAL pair and kept its zero exit, so
# the record must be compared without losing distinctions -- Bash ``read`` with
# the default IFS collapses runs of space/tab/newline and ignores leading and
# trailing whitespace, which silently normalises every one of them back into
# the exact record.
WHITESPACE_RECEIPT_FAULTS = (
    "receipt_trailing_space",
    "receipt_trailing_tab",
    "receipt_double_space",
    "receipt_tab_separator",
)

# Synthetic roster of the two registry gateways used by these fixtures. No live
# roster, provider config or credential is read.
ROSTER = {
    "tick_epoch": 1787721434,
    "providers": {
        "nous": ["vendor-x/synthetic-alpha:free", "vendor-y/synthetic-beta-free"],
        "kilo": ["vendor-x/synthetic-alpha:free"],
    },
    "stale_providers": [],
}

# A roster the real builder accepts and renders, but describing DIFFERENT
# content: used to inject a genuinely valid-but-mismatching catalog.json.
OTHER_ROSTER = {
    "tick_epoch": 1787000000,
    "providers": {"nous": ["vendor-q/other-model:free"]},
    "stale_providers": [],
}

# A second roster the real builder accepts and renders DIFFERENT content for:
# the Git phase must see genuinely changed artifacts (never a synthetic edit).
ROSTER2 = {
    "tick_epoch": 1787729999,
    "providers": {
        "nous": ["vendor-x/synthetic-alpha:free", "vendor-y/synthetic-beta-free",
                 "vendor-z/synthetic-gamma:free"],
        "kilo": ["vendor-x/synthetic-alpha:free"],
    },
    "stale_providers": [],
}

# The ONLY paths the local commit may touch (in git's sorted order): one commit
# may change either of them alone or both, but never any third path.
ARTIFACTS = ("site/catalog.json", "site/index.html")

# The root's .gitignore policy: the two public artifacts are the only tracked
# entries under site/, and state/ (synthetic private state) stays local-only.
# Build and publish locks fall under site/*.
SITE_IGNORE = (
    "state/\n"
    "site/*\n"
    "!site/index.html\n"
    "!site/catalog.json\n"
)

# Synthetic tracked fixtures of the Git-phase repository, and the unrelated
# dirt the helper must preserve: unstaged, staged, distinct staged+unstaged,
# a staged new file, a staged deletion and an untracked sentinel.
TRACKED_FILES = {
    "notes.md": "line one\nline two\n",
    "plain.txt": "plain v1\n",
    "twin.txt": "twin v0\n",
    "obsolete.md": "removed later\n",
}
DIRTY_PATHS = (
    "notes.md",
    "plain.txt",
    "twin.txt",
    "added later — ünïcode.txt",
    "obsolete.md",
    "sentinel ☂ note.txt",
)

BOOT_S = 90.0  # upper bound for one helper invocation
HOLD_S = 30.0  # upper bound for a coordinated hold inside the helper
WAIT_S = 45.0  # upper bound for an async helper to finish


# --------------------------------------------------------------------------
# owned harness: a pass-through interpreter that records and can pause/corrupt
# --------------------------------------------------------------------------

_WRAPPER = '''#!__SHEBANG__
"""Owned test interpreter: delegate to the genuine CPython, unchanged.

The helper receives this file as ONE executable --python path (it lives under a
directory whose name contains spaces). Build/validation calls are forwarded
verbatim to the real interpreter, so the real builder and the real
validate_artifact_pair run untouched; the only additions are an append-only
call record, an optional coordinated pause, optional corruption of the
builder's OWN outputs after a successful build, and optional removal or
corruption of the read-back program's stdout receipt AFTER it really validated
the pair.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REAL = __REAL__
LOG = Path(os.environ["PUBLISH_TEST_LOG"])
MODE = os.environ.get("PUBLISH_TEST_MODE", "pass")
HOLD_FILE = os.environ.get("PUBLISH_TEST_HOLD")
PIDFILE = os.environ.get("PUBLISH_TEST_PIDFILE")

# Modes that let the REAL validator run on the REAL pair and then damage only
# its success receipt, so the child process still exits zero with a pair the
# real validator accepts. The last four are the reproduced whitespace family:
# each one is an otherwise EXACT record with one non-newline byte added or
# substituted, which a word-splitting parse silently normalises back into the
# exact record.
RECEIPT_FAULTS = ("receipt_missing", "receipt_wrong", "receipt_extra",
                  "receipt_stderr_only", "receipt_no_digests",
                  "receipt_trailing_space", "receipt_trailing_tab",
                  "receipt_double_space", "receipt_tab_separator",
                  "receipt_trailing_newlines")

if PIDFILE:
    Path(PIDFILE).write_text(str(os.getpid()), encoding="utf-8")


def record(kind):
    line = f"{kind} {time.time_ns()}\\n"
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)
    LOG.with_name(LOG.name + "." + kind).write_text(line, encoding="utf-8")


def mark(kind, text):
    """Owned side channel for facts about a call (never appended to the log)."""
    LOG.with_name(LOG.name + "." + kind).write_text(text, encoding="utf-8")


def hold():
    deadline = time.monotonic() + float(os.environ.get("PUBLISH_TEST_HOLD_S", "30"))
    while time.monotonic() < deadline:
        if HOLD_FILE and Path(HOLD_FILE).exists():
            return
        time.sleep(0.005)
    record("holdtimeout")
    raise SystemExit(3)


args = sys.argv[1:]

if MODE == "ignore_program":
    # A selected executable that ignores its arguments AND the Python program
    # on stdin: it performs no work whatsoever and returns zero. Exit status
    # alone must never be able to prove that the validator executed.
    if not args or args[0] != "-":
        record("builder")
    else:
        record("verify" if len(args) > 1 and args[1] == "identity" else "validate")
    raise SystemExit(0)

if args and args[0] == "-" and len(args) > 1 and args[1] == "identity":
    # The post-commit identity check is a DIFFERENT program
    # (``python - identity <root> <commit> <catalog> <index>``); it is recorded
    # separately so a test can tell it apart from the read-back validation.
    record("verify")
    os.execv(REAL, [REAL] + args)

if args and args[0] == "-":
    # The read-back validation invocation (``python - <root> <checkout>``).
    record("validate")
    if MODE == "hold_validate":
        hold()
    if MODE in RECEIPT_FAULTS:
        # The genuine validator runs unchanged against the genuine pair; only
        # the stdout record it emits is then dropped, corrupted, duplicated,
        # misrouted, respaced or followed by blank lines. Every variant still
        # exits zero on a pair the real validator accepts, so only an exact,
        # distinction-preserving parse in the helper can reject the bad ones.
        NL = chr(10)
        TAB = chr(9)
        proc = subprocess.run([REAL] + args, stdout=subprocess.PIPE)
        raw = proc.stdout.decode("utf-8", "replace")
        mark("validator", f"{proc.returncode} {raw!r}")
        # The exact record the real program emitted, without its own newline.
        record_text = raw[:-1] if raw.endswith(NL) else raw
        emitted = record_text + NL
        if MODE == "receipt_missing":
            emitted = ""
        elif MODE == "receipt_wrong":
            emitted = "publish_site: validation skipped" + NL
        elif MODE == "receipt_no_digests":
            # The marker alone, without the two frozen identities: the record
            # the shell accepts must carry BOTH digests exactly.
            emitted = "publish_site: read-back validation passed" + NL
        elif MODE == "receipt_extra":
            emitted = record_text + "and one more line" + NL
        elif MODE == "receipt_stderr_only":
            sys.stderr.write(emitted)
            emitted = ""
        elif MODE == "receipt_trailing_space":
            emitted = record_text + " " + NL
        elif MODE == "receipt_trailing_tab":
            emitted = record_text + TAB + NL
        elif MODE == "receipt_double_space":
            emitted = record_text.replace(" sha256 ", " sha256  ", 1) + NL
        elif MODE == "receipt_tab_separator":
            head, _, last = record_text.rpartition(" ")
            emitted = head + TAB + last + NL
        elif MODE == "receipt_trailing_newlines":
            emitted = record_text + NL * 3
        mark("validate_stdout", repr(emitted))
        sys.stdout.write(emitted)
        raise SystemExit(proc.returncode)
    os.execv(REAL, [REAL] + args)

record("builder")
if MODE == "hold_build":
    hold()

rc = subprocess.run([REAL] + args).returncode
if rc != 0:
    raise SystemExit(rc)

root = Path(args[args.index("--root") + 1])
site = root / "site"
if MODE == "mismatch":
    other = Path(tempfile.mkdtemp(prefix="other-root-", dir=str(root.parent)))
    (other / "state").mkdir(parents=True, exist_ok=True)
    (other / "state" / "roster.json").write_text(
        json.dumps(__OTHER_ROSTER__), encoding="utf-8")
    rc = subprocess.run(
        [REAL, args[0], "--root", str(other)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    if rc != 0:
        raise SystemExit(rc)
    shutil.copyfile(other / "site" / "catalog.json", site / "catalog.json")
    shutil.rmtree(other, ignore_errors=True)
elif MODE == "missing_html":
    (site / "index.html").unlink()
elif MODE == "invalid_json":
    (site / "catalog.json").write_bytes(b'{"schema_version": 1, "snapshot": ')
elif MODE == "non_utf8":
    (site / "catalog.json").write_bytes(
        b'{"schema_version": 1, "note": "\\xff\\xfe"}')
elif MODE == "postbuild_symlink":
    # The builder just rewrote the pair (atomically, so a pre-arranged symlink
    # could not survive it). Replace the published page with a link to a copy
    # holding EXACTLY those bytes: a byte-only comparison cannot see the
    # difference, and ``git diff`` is made to report no change by the owned
    # launcher -- only the frozen working-artifact TYPE check can refuse this.
    saved = site / "index.html.saved"
    shutil.copyfile(site / "index.html", saved)
    (site / "index.html").unlink()
    (site / "index.html").symlink_to(saved.name)
raise SystemExit(0)
'''


def _env():
    """Hermetic base environment for helpers, builders and owned Git launchers.

    No ``PYTHONPATH`` (a synthetic root must never become importable) and no
    ambient Git configuration: every synthetic repository carries its own
    disposable local identity, and nothing here depends on the operator's
    global hooks or signing policy.
    """
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def _harness(tmp_path, mode="pass", hold=None, pidfile=None):
    """Create the owned interpreter and the environment that configures it."""
    harness_dir = tmp_path / "harness dir"
    harness_dir.mkdir(parents=True, exist_ok=True)
    log = harness_dir / f"calls-{mode}-{time.monotonic_ns()}.log"
    interpreter = harness_dir / "interp with spaces"
    interpreter.write_text(
        _WRAPPER.replace("__SHEBANG__", sys.executable)
        .replace("__REAL__", repr(sys.executable))
        .replace("__OTHER_ROSTER__", repr(OTHER_ROSTER)),
        encoding="utf-8")
    interpreter.chmod(0o755)

    env = _env()
    env.update(
        PUBLISH_TEST_LOG=str(log),
        PUBLISH_TEST_MODE=mode,
        PUBLISH_TEST_HOLD_S=str(HOLD_S),
    )
    if hold is not None:
        env["PUBLISH_TEST_HOLD"] = str(hold)
    if pidfile is not None:
        env["PUBLISH_TEST_PIDFILE"] = str(pidfile)
    return interpreter, log, env


def _write_roster(root, roster):
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "roster.json").write_text(json.dumps(roster), encoding="utf-8")


def _write_raw_roster(root, text):
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "roster.json").write_text(text, encoding="utf-8")


def _args(root, interpreter=None):
    args = ["--root", str(root)]
    if interpreter is not None:
        args += ["--python", str(interpreter)]
    return args


def _run(root, interpreter, env, cwd, extra=()):
    """Run the helper synchronously, capturing stdout and stderr separately.

    ``extra`` carries only interface flags the helper really exposes (the
    explicit ``--push`` opt-in); nothing here drives the helper internals.
    """
    return subprocess.run(
        [str(HELPER)] + _args(root, interpreter) + list(extra),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd),
        timeout=BOOT_S,
    )


class _Helper:
    """One asynchronous helper process, with its output kept in owned files.

    Output goes to files rather than pipes so a paused or chatty helper can
    never stall the test on a full pipe buffer, and so the captured text is
    still available for diagnostics after the process is gone.
    """

    def __init__(self, root, interpreter, env, cwd, tag, extra=()):
        out_dir = Path(env["PUBLISH_TEST_LOG"]).parent
        self.stdout_path = out_dir / f"{tag}.stdout"
        self.stderr_path = out_dir / f"{tag}.stderr"
        self._handles = (self.stdout_path.open("wb"), self.stderr_path.open("wb"))
        self.proc = subprocess.Popen(
            [str(HELPER)] + _args(root, interpreter) + list(extra),
            stdout=self._handles[0],
            stderr=self._handles[1],
            env=env,
            cwd=str(cwd),
            start_new_session=True,
        )

    @property
    def pid(self):
        return self.proc.pid

    @property
    def returncode(self):
        return self.proc.returncode

    def poll(self):
        return self.proc.poll()

    def wait(self, timeout=WAIT_S):
        return self.proc.wait(timeout=timeout)

    def output(self):
        """(stdout bytes, stderr text) produced so far."""
        stdout_bytes = (self.stdout_path.read_bytes()
                        if self.stdout_path.exists() else b"")
        stderr_text = (self.stderr_path.read_text(encoding="utf-8", errors="replace")
                       if self.stderr_path.exists() else "")
        return stdout_bytes, stderr_text

    def reap(self, kill_group=False):
        """Bound this helper's lifecycle (optionally killing its whole group)."""
        if kill_group:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                pass
        elif self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=WAIT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
        for handle in self._handles:
            if not handle.closed:
                handle.close()


def _wait_marker(log, kind, timeout=HOLD_S):
    """Wait for one recorded call (deterministic handshake, strict deadline)."""
    marker = log.with_name(log.name + "." + kind)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        time.sleep(0.005)
    calls = _kinds(log)
    raise AssertionError(
        f"helper never reached the {kind!r} stage within {timeout}s; "
        f"recorded calls: {calls}")


def _kinds(log):
    if not log.exists():
        return []
    return [line.split()[0] for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _stamps(log):
    return {line.split()[0]: int(line.split()[1])
            for line in log.read_text(encoding="utf-8").splitlines() if line.strip()}


def _marker(log, kind):
    """Read one owned side-channel marker written by the wrapper (or '')."""
    path = log.with_name(log.name + "." + kind)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _assert_not_success(proc, label):
    """No success claim, no success output: the gate must look like a failure."""
    assert proc.stdout == "", (label, f"gate stdout must stay empty: {proc.stdout!r}")
    assert "local gate passed" not in proc.stderr, (label, proc.stderr)
    assert "artifact pair validated" not in proc.stderr, (label, proc.stderr)


def _probe(lock_path):
    """Independent nonblocking acquisition of the SAME lock path.

    True means the lock was free (acquired by this probe and released
    immediately), False means a live holder exists.
    """
    return subprocess.run(
        ["flock", "-n", str(lock_path), "true"],
        capture_output=True, text=True, timeout=BOOT_S,
    ).returncode == 0


def _probe_until(lock_path, want_acquired, timeout=10.0):
    deadline = time.monotonic() + timeout
    seen = _probe(lock_path)
    while seen is not want_acquired and time.monotonic() < deadline:
        time.sleep(0.01)
        seen = _probe(lock_path)
    return seen


def _read_pair(root):
    """Exact final artifact bytes, decoded strictly, as a publisher would."""
    return (
        (root / CATALOG).read_bytes().decode("utf-8", "strict"),
        (root / INDEX).read_bytes().decode("utf-8", "strict"),
    )


def _inode(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino)


# --------------------------------------------------------------------------
# owned Git scaffolding for the local-commit phase
# --------------------------------------------------------------------------

def _git(root, *args, check=True):
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, env=_env(), timeout=BOOT_S)
    if check:
        assert proc.returncode == 0, (
            root, args, proc.returncode, proc.stdout, proc.stderr)
    return proc


def _git_bytes(root, *args):
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          env=_env(), timeout=BOOT_S)
    assert proc.returncode == 0, (root, args, proc.returncode, proc.stderr)
    return proc.stdout


def _head(root, rev="HEAD"):
    return _git(root, "rev-parse", "--verify", rev).stdout.strip()


def _parents(root, rev="HEAD"):
    fields = _git(root, "rev-list", "--parents", "-n", "1", rev).stdout.split()
    return fields[1:]


def _commit_count(root):
    return int(_git(root, "rev-list", "--count", "HEAD").stdout.strip())


def _commit_paths(root, rev="HEAD"):
    """Every path the commit changed (sorted, renames not collapsed)."""
    out = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r",
               "--no-renames", rev).stdout
    return sorted(line for line in out.splitlines() if line.strip())


def _blob(root, rev, rel):
    """Exact committed bytes of one path at one revision."""
    return _git_bytes(root, "cat-file", "blob", f"{rev}:{rel}")


def _status(root):
    """Porcelain worktree/index state, one entry per path (sorted)."""
    raw = _git_bytes(root, "status", "--porcelain", "-z", "--untracked-files=all")
    return sorted(record.decode("utf-8") for record in raw.split(b"\0") if record)


def _index_snapshot(root):
    """Every index entry as (mode, blob, stage), keyed by path."""
    entries = {}
    for record in _git_bytes(root, "ls-files", "-s", "-z").split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, sha, stage = meta.split()
        entries[path.decode("utf-8")] = (mode.decode(), sha.decode(), int(stage))
    return entries


def _worktree(root, rel):
    """Exact working bytes of a path, or None when it does not exist."""
    path = root / rel
    return path.read_bytes() if path.exists() else None


def _plant_tracked(root):
    """Write the baseline tracked fixtures of the synthetic repository."""
    root.mkdir(parents=True, exist_ok=True)
    for name, text in TRACKED_FILES.items():
        (root / name).write_text(text, encoding="utf-8")


def _init_repo(root, tracked=()):
    """A tiny INDEPENDENT repository: disposable local identity, one commit."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.name", "Synthetic Publisher")
    _git(root, "config", "user.email", "publisher@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text(SITE_IGNORE, encoding="utf-8")
    _git(root, "add", "--", ".gitignore", *tracked)
    _git(root, "commit", "-q", "-m", "synthetic initial commit")
    return _head(root)


def _make_dirty(root):
    """Unrelated work of every shape the artifact commit must leave alone."""
    (root / "notes.md").write_text("line one changed\nline two\n", encoding="utf-8")
    (root / "plain.txt").write_text("plain v2 staged\n", encoding="utf-8")
    _git(root, "add", "--", "plain.txt")
    (root / "twin.txt").write_text("twin staged\n", encoding="utf-8")
    _git(root, "add", "--", "twin.txt")
    (root / "twin.txt").write_text("twin worktree after staging\n", encoding="utf-8")
    (root / "added later — ünïcode.txt").write_text("staged new file\n", encoding="utf-8")
    _git(root, "add", "--", "added later — ünïcode.txt")
    _git(root, "rm", "-q", "--", "obsolete.md")
    (root / "sentinel ☂ note.txt").write_text("untracked sentinel\n", encoding="utf-8")


def _dirty_snapshot(root):
    """Working bytes plus index identity of every unrelated dirty path."""
    index = _index_snapshot(root)
    return {rel: (_worktree(root, rel), index.get(rel)) for rel in DIRTY_PATHS}


def _sha256(data):
    """Raw byte identity: stdlib SHA-256, never a git-normalised hash."""
    return hashlib.sha256(data).hexdigest()


def _install_hook(root, name, lines):
    """Owned fault-injection hook inside a synthetic repository's .git."""
    hook = root / ".git" / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env bash\nset -u\n" + "".join(lines),
                    encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _witness_lines(witness, root):
    """Hook lines copying the pair git is about to commit into owned scratch.

    The copy is taken INSIDE the hook, i.e. after the helper's read-back
    validation and before the commit exists, so it is the exact pre-hook pair
    whose identity the helper froze.
    """
    witness.mkdir(parents=True, exist_ok=True)
    return [
        f"cp -f -- {str(root / CATALOG)!r} {str(witness / 'catalog.json')!r}\n",
        f"cp -f -- {str(root / INDEX)!r} {str(witness / 'index.html')!r}\n",
    ]


def _witness_pair(witness):
    """(catalog bytes, index bytes) as the hook observed them."""
    return ((witness / "catalog.json").read_bytes(),
            (witness / "index.html").read_bytes())


def _build_pair(roster, root):
    """A coherent pair produced by the REAL builder from another roster."""
    root.mkdir(parents=True, exist_ok=True)
    _write_roster(root, roster)
    proc = subprocess.run(
        [sys.executable, str(REPO / "build_site.py"), "--root", str(root)],
        capture_output=True, text=True, env=_env(), timeout=BOOT_S)
    assert proc.returncode == 0, proc.stderr
    return ((root / CATALOG).read_bytes(), (root / INDEX).read_bytes())


def _git_shim(tmp_path, name, fail_on=None, swallow=None, widen=None):
    """Owned ``git`` launcher: records every invocation, can fail, fake or widen one.

    ``fail_on`` makes the named subcommand fail; ``swallow`` makes it exit zero
    without doing anything at all (a safe-looking mutation that never happened);
    ``widen`` replaces the helper's PATH-LIMITED commit with a REAL full-index
    commit that also carries one named outside path, so a test can inspect an
    actual stored path list containing a third path. The real git still performs
    every other command, including the post-commit read-back.
    """
    real = shutil.which("git")
    assert real, "the git-phase tests require the git executable"
    shim_dir = tmp_path / name
    shim_dir.mkdir()
    log = tmp_path / f"{name}.log"
    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        f"printf '%s\\n' \"$*\" >> {str(log)!r}",
    ]
    if fail_on is not None:
        lines.append(
            f'case " $* " in *" {fail_on} "*) '
            f'echo "owned git shim: simulating {fail_on} failure" >&2; exit 99;; esac')
    if swallow is not None:
        lines.append(f'case " $* " in *" {swallow} "*) exit 0;; esac')
    if widen is not None:
        # The REAL git is invoked by absolute path: calling plain ``git`` here
        # would re-enter this launcher recursively.
        lines.append(
            'if [ "${1:-}" = "-C" ] && [ "${3:-}" = "commit" ]; then '
            f'{real} -C "$2" add -- {widen!r} && '
            f'{real} -C "$2" commit -q -m "synthetic widened artifact commit"; '
            'exit $?; fi')
    lines.append(f'exec {real} "$@"')
    shim = shim_dir / "git"
    shim.write_text("\n".join(lines) + "\n", encoding="utf-8")
    shim.chmod(0o755)
    return shim_dir, log


def _with_path(env, shim_dir):
    """Prepend an owned launcher directory to a helper's PATH."""
    env = dict(env)
    env["PATH"] = f"{shim_dir}:{env.get('PATH', os.defpath)}"
    return env


def _shim_calls(log):
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


# --------------------------------------------------------------------------
# owned publication scaffolding: local BARE destinations, scoped launchers
# --------------------------------------------------------------------------

def _bare(tmp_path, name):
    """An owned local BARE repository: the only push destination in these tests."""
    path = tmp_path / name
    _git(tmp_path, "init", "-q", "--bare", "--initial-branch=main", str(path))
    return path


def _remote_refs(bare):
    """Every ref of an owned bare destination, as ``{refname: object id}``."""
    out = _git(bare, "for-each-ref", "--format=%(refname) %(objectname)").stdout
    refs = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        name, _, oid = line.partition(" ")
        refs[name] = oid.strip()
    return refs


def _remote_head(bare):
    """The destination branch tip actually stored in the owned bare repo."""
    return _remote_refs(bare).get("refs/heads/main")


def _configure_upstream(root, destination, remote="origin", branch="main"):
    """Explicit synthetic upstream: one named remote, one branch destination."""
    _git(root, "remote", "add", remote, str(destination))
    _git(root, "config", f"branch.{branch}.remote", remote)
    _git(root, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")


def _receive_hook(bare, name, lines):
    """Owned receive-side hook inside a bare destination (real, executed)."""
    hook = bare / "hooks" / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env bash\nset -u\n" + "".join(lines),
                    encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _git_branch_shim(tmp_path, name, subcommand, lines, extra_handlers=None):
    """Owned git launcher: SUBCOMMAND runs LINES instead of the real git.

    The real git still performs every other subcommand, and the named ones too
    when their LINES fall through without exiting, so a test can make a zero-exit
    push that pushed nothing, a real push whose ref is then rewound, an
    unreadable post-push endpoint, or Git's own "no change" observation --
    without any switch in the production helper.
    """
    real = shutil.which("git")
    assert real, "these publication tests require the git executable"
    shim_dir = tmp_path / name
    shim_dir.mkdir()
    log = tmp_path / f"{name}.log"
    handlers = {}
    if subcommand is not None:
        handlers[subcommand] = list(lines)
    handlers.update({key: list(value)
                     for key, value in (extra_handlers or {}).items()})
    body = [
        "#!/usr/bin/env bash",
        "set -u",
        f"printf '%s\\n' \"$*\" >> {str(log)!r}",
    ]
    for handled, handled_lines in handlers.items():
        body.append(f'case " $* " in *" {handled} "*)')
        body += ["    " + line for line in handled_lines]
        body += ["    ;;", "esac"]
    body.append(f'exec {real} "$@"')
    shim = shim_dir / "git"
    shim.write_text("\n".join(body) + "\n", encoding="utf-8")
    shim.chmod(0o755)
    return shim_dir, log


def _wait_path(path, timeout=HOLD_S):
    """Wait for an owned handshake file, with a strict deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s waiting for {path}")


def _non_repo_base():
    """A directory whose ancestry really holds no git repository.

    pytest's tmp base can itself live inside an operator-managed repository (a
    git-tracked home directory), where a "not a repository" control would
    silently exercise the NESTED-root refusal instead. The candidate is probed
    for real, and the control fails loudly rather than degrade if the host
    offers no git-free scratch area.
    """
    for candidate in ("/tmp", "/var/tmp", "/dev/shm", tempfile.gettempdir()):
        base = Path(candidate)
        if not base.is_dir():
            continue
        probe = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, env=_env(), timeout=BOOT_S)
        if probe.returncode != 0:
            return base
    raise AssertionError(
        "no git-free scratch directory is available for the non-repository "
        "control; refusing to weaken it into a nested-root check")


# --------------------------------------------------------------------------
# 1. the success path
# --------------------------------------------------------------------------

def test_gate_passes_a_genuine_build_with_empty_stdout(tmp_path):
    """A real build + read-back validation passes, silently, from anywhere.

    Two runs: one with the genuine 3.11 interpreter straight from its real
    path, one through an owned pass-through interpreter living under a
    directory whose name contains spaces (the helper must treat --python as ONE
    executable path, never a shell-split command string). Both roots and both
    interpreter paths contain spaces and both are launched from an unrelated
    cwd.
    """
    from catalog_export import validate_artifact_pair

    cwd = tmp_path / "unrelated cwd"
    cwd.mkdir()

    plain_root = tmp_path / "plain root"
    _init_repo(plain_root)
    _write_roster(plain_root, ROSTER)
    plain_env = _env()
    plain = _run(plain_root, sys.executable, plain_env, cwd)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == "", f"successful gate must be stdout-silent: {plain.stdout!r}"

    root = tmp_path / "root with spaces"
    _init_repo(root)
    _write_roster(root, ROSTER)
    interpreter, log, env = _harness(tmp_path, mode="pass")
    assert " " in str(interpreter)

    proc = _run(root, interpreter, env, cwd)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"successful gate must be stdout-silent: {proc.stdout!r}"

    # Both artifacts exist and the EXISTING validator accepts their read-back.
    catalog_text, html_text = _read_pair(root)
    snapshot = validate_artifact_pair(catalog_text, html_text)
    assert snapshot["providers"]["nous"]["model_ids"] == [
        "vendor-x/synthetic-alpha:free", "vendor-y/synthetic-beta-free"]
    assert "vendor-x/synthetic-alpha" in html_text

    # The independent read-back gate really ran, and only after the builder;
    # the post-commit identity check then ran against the frozen identities.
    assert _kinds(log) == ["builder", "validate", "verify"], _kinds(log)

    # The dedicated publish lock is its own inode, retained (never unlinked)
    # and distinct from the builder's own build lock.
    assert (root / PUBLISH_LOCK).is_file()
    assert (root / BUILD_LOCK).is_file()
    assert _inode(root / PUBLISH_LOCK) != _inode(root / BUILD_LOCK)

    # Nothing but the generated pair and the two lock files was created.
    assert sorted(p.name for p in (root / SITE).iterdir()) == [
        ".build.lock", ".publish.lock", "catalog.json", "index.html"]


# --------------------------------------------------------------------------
# 2. the gate's rejection paths
# --------------------------------------------------------------------------

def test_gate_rejects_build_and_pair_faults_at_the_right_stage(tmp_path):
    """Every fault fails nonzero at its OWN stage, never as a blanket error.

    Covers the launch contract (bad/unknown/missing arguments, --help, a
    non-directory or missing root, the default root and builder path following
    the helper's own checkout, an unopenable lock path and a host without the
    flock command) and the two gate stages: a failed builder -- missing or
    corrupt roster, with a valid last-good pair already on disk -- stops before
    validation, while a builder that returns 0 but leaves a missing,
    mismatching, invalid-JSON or non-UTF-8 pair is caught by the independent
    read-back gate, which a target root cannot shadow.

    The last group is the positive-execution receipt: exit status 0 is not
    proof that the read-back validator ran (``/bin/true`` and any executable
    that ignores its arguments and stdin return 0 without building or
    validating), so the gate also requires the validator's ONE fixed private
    stdout receipt, which now carries the two frozen artifact identities. A
    removed, wrong, duplicated, misrouted or digest-less receipt fails at
    ``stage=validate`` even though the child exited zero with a genuinely valid
    pair.
    """
    from catalog_export import validate_artifact_pair

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = _env()

    # --- bad arguments: nonzero, usage on stderr, no build ----------------
    for bad in (["--root"], ["--python"], ["--root", ""], ["--bogus"],
                ["--root", str(tmp_path), "extra"]):
        proc = subprocess.run([str(HELPER)] + bad, capture_output=True,
                              text=True, env=env, cwd=str(cwd), timeout=BOOT_S)
        assert proc.returncode == 2, (bad, proc.returncode, proc.stderr)
        assert "usage:" in proc.stderr, (bad, proc.stderr)
        assert "stage=args" in proc.stderr, (bad, proc.stderr)

    # --- --help: succeeds, has no build/lock side effects -----------------
    help_root = tmp_path / "help root"
    help_root.mkdir()
    proc = subprocess.run([str(HELPER), "--root", str(help_root), "--help"],
                          capture_output=True, text=True, env=env, cwd=str(cwd),
                          timeout=BOOT_S)
    assert proc.returncode == 0, proc.stderr
    assert "usage: publish_site.sh" in proc.stdout
    assert not (help_root / SITE).exists(), "--help must not create site/ or a lock"
    assert not (help_root / "state").exists()

    # --- default root and builder path follow the helper's own checkout ---
    # A copy of the helper in an isolated checkout, run with NO --root from an
    # unrelated cwd: the default root must be that checkout, and the builder
    # path must be derived from the helper's location (never the cwd, never the
    # target root). The stub builder records its own argv and fails.
    checkout = tmp_path / "copied checkout"
    (checkout / "scripts").mkdir(parents=True)
    copied_helper = checkout / "scripts" / "publish_site.sh"
    copied_helper.write_bytes(HELPER.read_bytes())
    copied_helper.chmod(0o755)
    stub_record = tmp_path / "stub argv.json"
    (checkout / "build_site.py").write_text(
        "import json, sys\n"
        f"open({str(stub_record)!r}, 'w', encoding='utf-8')"
        ".write(json.dumps(sys.argv))\n"
        "sys.exit(2)\n",
        encoding="utf-8")
    proc = subprocess.run([str(copied_helper)], capture_output=True, text=True,
                          env=env, cwd=str(cwd), timeout=BOOT_S)
    assert proc.returncode == 2, proc.stderr
    assert "stage=builder" in proc.stderr, proc.stderr
    stub_argv = json.loads(stub_record.read_text(encoding="utf-8"))
    assert stub_argv[0] == str(checkout / "build_site.py"), stub_argv
    assert stub_argv[1:] == ["--root", str(checkout)], stub_argv

    # --- unusable roots fail before the builder ---------------------------
    missing_root = tmp_path / "absent root"
    proc = _run(missing_root, sys.executable, env, cwd)
    assert proc.returncode == 2, proc.stderr
    assert "stage=root" in proc.stderr, proc.stderr
    assert not (missing_root / SITE).exists()

    file_root = tmp_path / "a file, not a directory"
    file_root.write_text("not a directory", encoding="utf-8")
    proc = _run(file_root, sys.executable, env, cwd)
    assert proc.returncode == 2, proc.stderr
    assert "stage=root" in proc.stderr, proc.stderr

    # --- an unopenable lock path fails explicitly, builder never starts ---
    lock_root = tmp_path / "lockfault root"
    _write_roster(lock_root, ROSTER)
    (lock_root / SITE).mkdir(parents=True)
    (lock_root / PUBLISH_LOCK).mkdir()  # a directory cannot be opened as a lock
    interpreter, log, lock_env = _harness(tmp_path, mode="pass")
    proc = _run(lock_root, interpreter, lock_env, cwd)
    assert proc.returncode == 2, proc.stderr
    assert "stage=lock" in proc.stderr, proc.stderr
    assert _kinds(log) == [], f"builder ran without the lock: {_kinds(log)}"
    assert not (lock_root / CATALOG).exists()

    # --- a site path that cannot be created is a lock-stage failure too ---
    sitefile_root = tmp_path / "site is a file"
    _write_roster(sitefile_root, ROSTER)
    (sitefile_root / SITE).write_text("not a directory", encoding="utf-8")
    proc = _run(sitefile_root, sys.executable, env, cwd)
    assert proc.returncode == 2, proc.stderr
    assert "stage=lock" in proc.stderr, proc.stderr
    assert "cannot create the site directory" in proc.stderr, proc.stderr

    # --- a host without the flock command fails explicitly ---------------
    sparse_bin = tmp_path / "sparse bin"
    sparse_bin.mkdir()
    for tool in ("bash", "dirname", "readlink", "mkdir"):
        resolved_tool = shutil.which(tool)
        assert resolved_tool, f"{tool} is required for this control"
        (sparse_bin / tool).symlink_to(resolved_tool)
    nolock_root = tmp_path / "no flock root"
    _write_roster(nolock_root, ROSTER)
    proc = subprocess.run(
        [str(HELPER), "--root", str(nolock_root)],
        capture_output=True, text=True, cwd=str(cwd), timeout=BOOT_S,
        env=dict(env, PATH=str(sparse_bin)))
    assert proc.returncode == 2, proc.stderr
    assert "stage=lock" in proc.stderr and "flock" in proc.stderr, proc.stderr
    assert not (nolock_root / SITE).exists(), "no lock => no site dir and no build"

    # --- failed builder: valid last-good pair cannot rescue it ------------
    stale_root = tmp_path / "stale root"
    _init_repo(stale_root)
    _write_roster(stale_root, ROSTER)
    interpreter, log, stale_env = _harness(tmp_path, mode="pass")
    first = _run(stale_root, interpreter, stale_env, cwd)
    assert first.returncode == 0, first.stderr
    last_good = ((stale_root / CATALOG).read_bytes(), (stale_root / INDEX).read_bytes())

    _write_raw_roster(stale_root, "THIS IS NOT JSON{{")
    interpreter, log, stale_env = _harness(tmp_path, mode="pass")
    proc = _run(stale_root, interpreter, stale_env, cwd)
    assert proc.returncode == 2, proc.stderr
    assert "stage=builder" in proc.stderr, proc.stderr
    assert "stage=validate" not in proc.stderr, proc.stderr
    # Test instrumentation proves the read-back gate was never entered, even
    # though a valid pair was sitting on disk.
    assert _kinds(log) == ["builder"], _kinds(log)
    assert ((stale_root / CATALOG).read_bytes(),
            (stale_root / INDEX).read_bytes()) == last_good

    # --- a missing roster fails the same way ------------------------------
    empty_root = tmp_path / "empty root"
    (empty_root / "state").mkdir(parents=True)
    interpreter, log, empty_env = _harness(tmp_path, mode="pass")
    proc = _run(empty_root, interpreter, empty_env, cwd)
    assert proc.returncode == 2, proc.stderr
    assert "stage=builder" in proc.stderr, proc.stderr
    assert _kinds(log) == ["builder"], _kinds(log)
    assert not (empty_root / CATALOG).exists()

    # --- an interpreter that fails with 1 is normalized to exit 2 ---------
    normal_root = tmp_path / "normalize root"
    _write_roster(normal_root, ROSTER)
    proc = _run(normal_root, "/bin/false", env, cwd)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "stage=builder" in proc.stderr, proc.stderr

    # --- exit 0 while NOTHING ran is not a validated pair -----------------
    # ``/bin/true`` ignores its arguments and the Python program on stdin, does
    # no work and returns zero. Exit status alone used to be read as
    # "artifact pair validated" although neither artifact existed; the required
    # positive receipt must turn that into a validation-stage failure.
    true_root = tmp_path / "ignored program root"
    _write_roster(true_root, ROSTER)
    proc = _run(true_root, "/bin/true", env, cwd)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    _assert_not_success(proc, "/bin/true")
    assert "stage=validate" in proc.stderr, proc.stderr
    assert "did not confirm execution" in proc.stderr, proc.stderr
    assert not (true_root / CATALOG).exists()
    assert not (true_root / INDEX).exists()

    # The same zero-exit no-op against a VALID last-good pair: mere file
    # existence must not substitute for positive execution either.
    pair_root = tmp_path / "last good root"
    _init_repo(pair_root)
    _write_roster(pair_root, ROSTER)
    interpreter, _, good_env = _harness(tmp_path, mode="pass")
    built = _run(pair_root, interpreter, good_env, cwd)
    assert built.returncode == 0, built.stderr
    last_good = ((pair_root / CATALOG).read_bytes(),
                 (pair_root / INDEX).read_bytes())
    # The receipt stays internal: it is not an artifact or public output field.
    assert READBACK_RECEIPT.encode("utf-8") not in last_good[0]
    assert READBACK_RECEIPT.encode("utf-8") not in last_good[1]

    interpreter, log, ignore_env = _harness(tmp_path, mode="ignore_program")
    proc = _run(pair_root, interpreter, ignore_env, cwd)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    _assert_not_success(proc, "ignore_program")
    assert "stage=validate" in proc.stderr, proc.stderr
    assert "did not confirm execution" in proc.stderr, proc.stderr
    # It was invoked for both stages and did nothing in either.
    assert _kinds(log) == ["builder", "validate"], _kinds(log)
    # The pair on disk is unchanged and still validates: the ONLY difference
    # from success is that no validator proved it by executing.
    assert ((pair_root / CATALOG).read_bytes(),
            (pair_root / INDEX).read_bytes()) == last_good
    validate_artifact_pair(*_read_pair(pair_root))

    # --- a real, successful validation whose receipt is removed/corrupted --
    # Each wrapper runs the REAL validator on the REAL pair and then damages
    # only the stdout receipt, so the child still exits zero with accepted
    # artifacts. Only the receipt handshake can reject these.
    for mode in ("receipt_missing", "receipt_wrong", "receipt_extra",
                 "receipt_stderr_only", "receipt_no_digests"):
        root = tmp_path / f"receipt {mode}"
        _write_roster(root, ROSTER)
        interpreter, log, fault_env = _harness(tmp_path, mode=mode)
        proc = _run(root, interpreter, fault_env, cwd)
        assert proc.returncode == 2, (mode, proc.returncode, proc.stderr)
        _assert_not_success(proc, mode)
        assert "stage=validate" in proc.stderr, (mode, proc.stderr)
        assert "stage=builder" not in proc.stderr, (mode, proc.stderr)
        assert "did not confirm execution" in proc.stderr, (mode, proc.stderr)
        assert _kinds(log) == ["builder", "validate"], (mode, _kinds(log))
        # The child really validated: exit 0 and the expected receipt text.
        assert _marker(log, "validator").startswith("0 "), (
            mode, _marker(log, "validator"))
        assert READBACK_RECEIPT in _marker(log, "validator"), (
            mode, _marker(log, "validator"))
        # Both artifacts exist and the EXISTING validator accepts the pair.
        validate_artifact_pair(*_read_pair(root))

    # --- the reproduced whitespace family of that same receipt --------------
    # Each of these is an otherwise EXACT private record with ONE non-newline
    # byte added or substituted -- an appended space, an appended tab, a doubled
    # space after ``sha256``, a tab between the two digests -- emitted after the
    # real validator really accepted the real pair (child exit 0). Bash ``read``
    # with the default IFS throws those distinctions away: it collapses runs of
    # space/tab/newline and ignores leading and trailing whitespace, so every
    # one of them used to be accepted as the exact record. The root is a REAL
    # repository, so the pre-fix helper not only accepted the record but went on
    # to commit and claim local success; the owned git launcher proves that the
    # corrected helper instead rejects each one at ``stage=validate`` before a
    # single git command runs.
    receipt_shim_dir, receipt_git_log = _git_shim(tmp_path, "receipt git")
    for mode in WHITESPACE_RECEIPT_FAULTS:
        root = tmp_path / f"receipt {mode}"
        _plant_tracked(root)
        _init_repo(root, tracked=sorted(TRACKED_FILES))
        _write_roster(root, ROSTER)
        interpreter, log, fault_env = _harness(tmp_path, mode=mode)
        fault_env = _with_path(fault_env, receipt_shim_dir)
        proc = _run(root, interpreter, fault_env, cwd)
        assert proc.returncode == 2, (mode, proc.returncode, proc.stdout,
                                      proc.stderr)
        _assert_not_success(proc, mode)
        assert "stage=validate" in proc.stderr, (mode, proc.stderr)
        assert "stage=builder" not in proc.stderr, (mode, proc.stderr)
        assert "stage=git" not in proc.stderr, (mode, proc.stderr)
        assert "did not confirm execution" in proc.stderr, (mode, proc.stderr)
        assert _kinds(log) == ["builder", "validate"], (mode, _kinds(log))
        assert _marker(log, "validator").startswith("0 "), (
            mode, _marker(log, "validator"))
        # The fault really was emitted, and the ONLY difference from the exact
        # record the real validator printed is whitespace: no schema, digest or
        # validator detail could have caught it.
        emitted = ast.literal_eval(_marker(log, "validate_stdout"))
        mutated = emitted[:-1]
        real_record = ast.literal_eval(
            _marker(log, "validator").split(" ", 1)[1])[:-1]
        if mode == "receipt_trailing_space":
            expected = real_record + " "
        elif mode == "receipt_trailing_tab":
            expected = real_record + "\t"
        elif mode == "receipt_double_space":
            expected = real_record.replace(" sha256 ", " sha256  ", 1)
        else:
            head, _, last = real_record.rpartition(" ")
            expected = head + "\t" + last
        assert mutated == expected, (mode, emitted, real_record)
        assert mutated != real_record, (mode, "the fault was a no-op")
        # Both artifacts exist and the EXISTING validator still accepts them:
        # the pair was never the problem, only the record's exact bytes.
        validate_artifact_pair(*_read_pair(root))
        # Rejected at validation: no git command ran and no commit was made.
        assert _shim_calls(receipt_git_log) == [], (
            mode, _shim_calls(receipt_git_log))
        assert _commit_count(root) == 1, (
            mode, "a malformed record must never reach a commit")
    assert _shim_calls(receipt_git_log) == [], _shim_calls(receipt_git_log)

    # --- builder exits 0 but leaves a pair the read-back gate rejects -----
    for mode, expect in (
        ("mismatch", "Catalogue artifacts do not match"),
        ("missing_html", "cannot read back"),
        ("invalid_json", "rejected"),
        ("non_utf8", "not valid UTF-8"),
    ):
        root = tmp_path / f"fault {mode}"
        _write_roster(root, ROSTER)
        interpreter, log, fault_env = _harness(tmp_path, mode=mode)
        proc = _run(root, interpreter, fault_env, cwd)
        assert proc.returncode == 2, (mode, proc.returncode, proc.stderr)
        assert proc.stdout == "", (mode, proc.stdout)
        assert "stage=validate" in proc.stderr, (mode, proc.stderr)
        assert "stage=builder" not in proc.stderr, (mode, proc.stderr)
        assert expect in proc.stderr, (mode, proc.stderr)
        # the read-back gate was entered only after the builder "succeeded"
        assert _kinds(log) == ["builder", "validate"], (mode, _kinds(log))

        if mode == "mismatch":
            # Both artifacts are individually WELL-FORMED: the rejection is the
            # pair identity check, not a parse failure.
            catalog_bytes = (root / CATALOG).read_bytes()
            assert json.loads(catalog_bytes.decode("utf-8"))["snapshot_id"]
            assert b"vendor-q/other-model" in catalog_bytes
            assert b"vendor-q/other-model" not in (root / INDEX).read_bytes()

    # --- the target root cannot shadow the helper's builder or validator --
    # A synthetic root carrying its own permissive build_site.py and a bogus
    # watchdog_catalog package must not be importable by the read-back gate:
    # the mismatching pair is still rejected by the REAL validator.
    shadow_root = tmp_path / "shadow root"
    _write_roster(shadow_root, ROSTER)
    (shadow_root / "build_site.py").write_text(
        "def validate_artifact_pair(catalog_text, html_text):\n"
        "    return {}\n", encoding="utf-8")
    (shadow_root / "watchdog_catalog").mkdir()
    (shadow_root / "watchdog_catalog" / "__init__.py").write_text("", encoding="utf-8")
    interpreter, log, shadow_env = _harness(tmp_path, mode="mismatch")
    proc = _run(shadow_root, interpreter, shadow_env, cwd)
    assert proc.returncode == 2, proc.stderr
    assert "stage=validate" in proc.stderr, proc.stderr
    assert "Catalogue artifacts do not match" in proc.stderr, proc.stderr


# --------------------------------------------------------------------------
# 3. the lock's lifetime, exclusivity and release
# --------------------------------------------------------------------------

def test_publish_lock_is_exclusive_through_validation_and_always_released(tmp_path):
    """One lock covers build -> read-back -> validation, then is released.

    Coordination is an owned handshake (marker files + a release file with a
    strict deadline), never a sleep-based guess: helper A pauses INSIDE the
    final validation while holding the lock, an independent ``flock -n`` probe
    on the SAME ``.publish.lock`` must fail, helper B must stay blocked in the
    builder, and B's builder must only start after A exits (timestamped call
    records). Release is then proven by a real later acquisition, by a second
    real invocation, and by a SIGKILLed helper with a bounded child lifecycle.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    # --- baseline: a first real invocation retains a stable lock inode ----
    root = tmp_path / "lock root"
    _init_repo(root)
    _write_roster(root, ROSTER)
    interpreter, _, env = _harness(tmp_path, mode="pass")
    first = _run(root, interpreter, env, cwd)
    assert first.returncode == 0, first.stderr
    lock_path = root / PUBLISH_LOCK
    assert lock_path.is_file()
    baseline = _inode(lock_path)
    build_lock_baseline = _inode(root / BUILD_LOCK)
    assert baseline != build_lock_baseline
    assert _probe(lock_path) is True, "a finished helper must not still hold the lock"

    # --- A pauses inside validation, holding the lock ---------------------
    hold = tmp_path / "A.go"
    interpreter_a, log_a, env_a = _harness(
        tmp_path, mode="hold_validate", hold=hold,
        pidfile=tmp_path / "A.pid")
    a = _Helper(root, interpreter_a, env_a, cwd, "A")
    b = None
    try:
        _wait_marker(log_a, "validate")
        # The builder already finished; the lock must still cover the final gate.
        assert _probe(lock_path) is False, (
            "the publish lock was not held during the final validation")

        interpreter_b, log_b, env_b = _harness(tmp_path, mode="pass")
        b = _Helper(root, interpreter_b, env_b, cwd, "B")

        # B is alive and has NOT entered its builder: it is contending on the
        # lock A holds. (Bounded observation; the hard proof is the failed
        # nonblocking probe above plus the timestamp ordering below.)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            assert b.poll() is None, (
                f"second helper exited instead of contending: "
                f"{b.output()}")
            assert not log_b.with_name(log_b.name + ".builder").exists(), (
                "second helper entered its builder while the lock was held")
            time.sleep(0.01)

        hold.write_text("go", encoding="utf-8")
        release_stamp = time.time_ns()
        a.wait(timeout=WAIT_S)
        a_stdout, a_stderr = a.output()
        assert a.returncode == 0, a_stderr
        assert a_stdout == b""

        b.wait(timeout=WAIT_S)
        b_stdout, b_stderr = b.output()
        assert b.returncode == 0, b_stderr
        assert b_stdout == b""

        # B progressed only after A released: its builder started later than the
        # release handshake, i.e. only after A's build+validation finished.
        assert _kinds(log_b) == ["builder", "validate"], _kinds(log_b)
        b_builder_stamp = _stamps(log_b)["builder"]
        assert b_builder_stamp > release_stamp, (
            f"second builder started before the first helper released the lock "
            f"({b_builder_stamp} <= {release_stamp})")
    finally:
        hold.write_text("go", encoding="utf-8")
        for helper in (a, b):
            if helper is not None and helper.poll() is None:
                helper.reap(kill_group=True)

    # Released after success, and the lock inode was neither replaced nor
    # unlinked by any of those invocations.
    assert _probe(lock_path) is True
    assert _inode(lock_path) == baseline
    assert lock_path.is_file()

    # --- a failing gate releases it too, and a later run still works ------
    _write_raw_roster(root, "not json at all")
    interpreter_f, log_f, env_f = _harness(tmp_path, mode="pass")
    failed = _run(root, interpreter_f, env_f, cwd)
    assert failed.returncode == 2, failed.stderr
    assert "stage=builder" in failed.stderr, failed.stderr
    assert _probe_until(lock_path, True, timeout=10.0) is True, (
        "the publish lock was not released after a failed build")
    assert _inode(lock_path) == baseline, "the lock inode must be retained"

    _write_roster(root, ROSTER)
    interpreter_r, _, env_r = _harness(tmp_path, mode="pass")
    again = _run(root, interpreter_r, env_r, cwd)
    assert again.returncode == 0, again.stderr
    assert _inode(lock_path) == baseline

    # --- the lock is still held INSIDE a real commit hook ----------------
    # The critical section now ends after the artifact commit, not at the end
    # of validation: the hook blocks while git holds the repository, and the
    # publication lock must still be held there -- otherwise a second
    # publisher could start building on top of an in-flight commit.
    hook_root = tmp_path / "hook lock root"
    _plant_tracked(hook_root)
    _init_repo(hook_root, tracked=sorted(TRACKED_FILES))
    _write_roster(hook_root, ROSTER)
    interpreter_s, _, seed_env = _harness(tmp_path, mode="pass")
    seed = _run(hook_root, interpreter_s, seed_env, cwd)
    assert seed.returncode == 0, seed.stderr

    deeds = tmp_path / "hook handshake"
    deeds.mkdir()
    hook = hook_root / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f"deeds={str(deeds)!r}\n"
        "printf '%s' \"$$\" > \"$deeds/hook.pid\"\n"
        ": > \"$deeds/hook.started\"\n"
        "deadline=$((SECONDS + 30))\n"
        "while [ ! -e \"$deeds/hook.go\" ]; do\n"
        "    if [ \"$SECONDS\" -ge \"$deadline\" ]; then\n"
        "        echo 'synthetic commit hook timed out' >&2\n"
        "        exit 1\n"
        "    fi\n"
        "    sleep 0.01\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8")
    hook.chmod(0o755)

    # A genuinely different roster, so this run really commits.
    _write_roster(hook_root, ROSTER2)
    hook_lock = hook_root / PUBLISH_LOCK
    hook_inode = _inode(hook_lock)
    interpreter_ha, log_ha, env_ha = _harness(tmp_path, mode="pass")
    a_hook = _Helper(hook_root, interpreter_ha, env_ha, cwd, "hookA")
    b_hook = None
    try:
        _wait_path(deeds / "hook.started")
        assert _probe(hook_lock) is False, (
            "the publish lock was not held inside the commit hook")

        interpreter_hb, log_hb, env_hb = _harness(tmp_path, mode="pass")
        b_hook = _Helper(hook_root, interpreter_hb, env_hb, cwd, "hookB")

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            assert b_hook.poll() is None, b_hook.output()
            assert not log_hb.with_name(log_hb.name + ".builder").exists(), (
                "the second helper entered its builder while the commit hook "
                "held the publish lock")
            time.sleep(0.01)

        (deeds / "hook.go").write_text("go", encoding="utf-8")
        release_stamp = time.time_ns()
        a_hook.wait(timeout=WAIT_S)
        a_hook_stdout, a_hook_stderr = a_hook.output()
        assert a_hook.returncode == 0, a_hook_stderr
        assert a_hook_stdout == b""

        b_hook.wait(timeout=WAIT_S)
        b_hook_stdout, b_hook_stderr = b_hook.output()
        assert b_hook.returncode == 0, b_hook_stderr
        assert b_hook_stdout == b""

        assert _kinds(log_hb) == ["builder", "validate"], _kinds(log_hb)
        assert _stamps(log_hb)["builder"] > release_stamp, (
            "the second builder started before the commit hook released the lock")

        # The first helper really committed the artifact pair inside that hook.
        assert _commit_paths(hook_root) == list(ARTIFACTS)
    finally:
        (deeds / "hook.go").write_text("go", encoding="utf-8")
        for helper_proc in (a_hook, b_hook):
            if helper_proc is not None and helper_proc.poll() is None:
                helper_proc.reap(kill_group=True)

    assert _probe_until(hook_lock, True, timeout=10.0) is True, (
        "the publish lock was not released after the commit hook finished")
    assert _inode(hook_lock) == hook_inode

    # --- a SIGKILLed helper after it acquired the lock -------------------
    killed_root = tmp_path / "killed root"
    _init_repo(killed_root)
    _write_roster(killed_root, ROSTER)
    killed_lock = killed_root / PUBLISH_LOCK
    hold_c = tmp_path / "C.go"
    pidfile = tmp_path / "C.pid"
    interpreter_c, log_c, env_c = _harness(
        tmp_path, mode="hold_build", hold=hold_c, pidfile=pidfile)
    c = _Helper(killed_root, interpreter_c, env_c, cwd, "C")
    pgid = os.getpgid(c.pid)
    child_pid = None
    orphan_held = None
    killed_baseline = None
    try:
        _wait_marker(log_c, "builder")
        child_pid = int(pidfile.read_text(encoding="utf-8"))
        assert child_pid != c.pid
        assert _probe(killed_lock) is False, "the lock was not held before the kill"
        killed_baseline = _inode(killed_lock)

        os.kill(c.pid, signal.SIGKILL)
        c.wait(timeout=WAIT_S)
        # Whether or not the surviving builder child still holds the inherited
        # descriptor is host behaviour; what matters is that the test bounds the
        # child's lifecycle instead of leaving a silent holder behind.
        orphan_held = _probe(killed_lock) is False
    finally:
        hold_c.write_text("go", encoding="utf-8")
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except OSError:
                pass
        c.reap(kill_group=True)

    assert _probe_until(killed_lock, True, timeout=10.0) is True, (
        "the publish lock survived a killed helper "
        f"(surviving child held it: {orphan_held})")
    assert _inode(killed_lock) == killed_baseline, (
        "the killed helper must not have replaced or unlinked the lock inode")
    assert killed_lock.is_file()

    interpreter_k, _, env_k = _harness(tmp_path, mode="pass")
    after_kill = _run(killed_root, interpreter_k, env_k, cwd)
    assert after_kill.returncode == 0, after_kill.stderr
    assert after_kill.stdout == ""


# --------------------------------------------------------------------------
# 4. the local Git phase: restricted staging and a path-limited commit
# --------------------------------------------------------------------------

def test_local_git_phase_commits_only_the_validated_artifact_pair(tmp_path):
    """Only the two validated artifacts are ever committed, in the right root.

    Grouped so the whole Git contract is read in one place: the first
    publication commits both previously untracked artifacts, a second changed
    roster commits a second exact artifact-only commit, unrelated work of every
    shape (unstaged, staged, distinct staged+unstaged, staged new file, staged
    deletion, untracked sentinel with spaces and non-ASCII) keeps its bytes and
    its mode/blob/stage identity and never enters either commit, an unchanged
    pair commits nothing at all, a rejecting hook and a failing ``git add``
    fail at exit 2 without advancing HEAD, earlier failures never invoke Git,
    and non-repository / nested-root / detached-HEAD targets are refused
    without touching an ancestor repository or this helper's source checkout.

    One group covers a LEGITIMATE one-artifact commit: a synthetic baseline
    commit changes only ``site/index.html`` (an HTML comment) or only
    ``site/catalog.json`` (a trailing newline), the next genuine build restores
    just that artifact, and the helper must commit it and pass -- the complete
    changed-path set is a nonempty subset of the allowlist -- while both
    committed blobs and both working files still equal the approved pair. The
    same group proves an ACTUAL commit carrying an outside path is rejected
    even when both artifacts match their frozen bytes, and that a
    ``post-commit`` hook mutating the artifact the commit did NOT change fails
    the run instead of being masked by the single-path success.

    The identity group freezes the validated identity across the repository's
    OWN hooks with real commits: a non-mutating hook publishes normally with
    the committed and working bytes equal to the hook-time witness, while a
    hook that stages an invalid catalogue, a hook that substitutes a different
    but individually coherent builder-made pair (which the real validator
    accepts), a post-commit hook that rewrites only the working bytes and a
    post-commit hook that leaves a same-bytes SYMLINK in place of either
    working artifact are all rejected at ``stage=git/verify`` -- even though the
    original post-hook comparison would have accepted the substituted pair. An
    exact private record followed by trailing newlines is the positive control:
    it still publishes and commits.
    """
    from catalog_export import validate_artifact_pair

    cwd = tmp_path / "git cwd"
    cwd.mkdir()
    env = _env()
    helper_source_head = _head(REPO)

    # --- a tiny independent repository with an explicit initial commit ----
    root = tmp_path / "git root"
    _plant_tracked(root)
    initial = _init_repo(root, tracked=sorted(TRACKED_FILES))
    _write_roster(root, ROSTER)
    assert _commit_count(root) == 1
    assert _status(root) == []

    # --- first publication: both previously untracked artifacts committed -
    first_run = _run(root, sys.executable, env, cwd)
    assert first_run.returncode == 0, first_run.stderr
    assert first_run.stdout == "", first_run.stdout
    first = _head(root)
    assert first != initial
    assert _parents(root) == [initial]
    assert _commit_paths(root) == list(ARTIFACTS)
    committed_catalog = _blob(root, "HEAD", "site/catalog.json")
    committed_index = _blob(root, "HEAD", "site/index.html")
    assert committed_catalog == (root / CATALOG).read_bytes()
    assert committed_index == (root / INDEX).read_bytes()
    snapshot = validate_artifact_pair(
        committed_catalog.decode("utf-8"), committed_index.decode("utf-8"))
    assert snapshot["providers"]["nous"]["model_ids"] == [
        "vendor-x/synthetic-alpha:free", "vendor-y/synthetic-beta-free"]
    # Locks and synthetic private state stay out of the commit and the index.
    assert (root / PUBLISH_LOCK).is_file() and (root / BUILD_LOCK).is_file()
    assert (root / "state" / "roster.json").is_file()
    assert _status(root) == []
    assert _index_snapshot(root).keys() == {
        ".gitignore", "notes.md", "obsolete.md", "plain.txt", "twin.txt",
        "site/catalog.json", "site/index.html"}
    assert "committed locally" in first_run.stderr

    # --- unrelated work of every shape, then a second changed roster ------
    _make_dirty(root)
    before_dirty = _dirty_snapshot(root)
    before_status = _status(root)
    assert before_status, "the synthetic repository must really be dirty"
    _write_roster(root, ROSTER2)
    interpreter, log, harness_env = _harness(tmp_path, mode="pass")
    second_run = _run(root, interpreter, harness_env, cwd)
    assert second_run.returncode == 0, second_run.stderr
    assert second_run.stdout == ""
    second = _head(root)
    assert second != first
    assert _parents(root) == [first]
    assert _commit_paths(root) == list(ARTIFACTS)
    catalog_bytes = (root / CATALOG).read_bytes()
    index_bytes = (root / INDEX).read_bytes()
    assert catalog_bytes != committed_catalog, "the roster change must reach the artifacts"
    assert _blob(root, "HEAD", "site/catalog.json") == catalog_bytes
    assert _blob(root, "HEAD", "site/index.html") == index_bytes
    validate_artifact_pair(catalog_bytes.decode("utf-8"),
                           index_bytes.decode("utf-8"))
    # Nothing unrelated entered the commit, and nothing unrelated moved.
    for rel in DIRTY_PATHS:
        assert rel not in _commit_paths(root), (rel, _commit_paths(root))
    assert _dirty_snapshot(root) == before_dirty
    assert _status(root) == before_status

    # --- unchanged artifacts commit nothing, even with staged work --------
    commits_before = _commit_count(root)
    no_change = _run(root, sys.executable, env, cwd)
    assert no_change.returncode == 0, no_change.stderr
    assert no_change.stdout == ""
    assert _head(root) == second, "a no-change run must not create a commit"
    assert _commit_count(root) == commits_before
    assert _dirty_snapshot(root) == before_dirty
    assert _status(root) == before_status
    assert "no artifact change" in no_change.stderr
    assert "committed locally" not in no_change.stderr

    # --- a real rejecting commit hook: exit 2, HEAD unchanged -------------
    hook_marker = tmp_path / "commit hook ran"
    rejecting = root / ".git" / "hooks" / "pre-commit"
    rejecting.write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'ran' > {str(hook_marker)!r}\n"
        "echo 'synthetic hook rejecting the commit' >&2\n"
        "exit 1\n",
        encoding="utf-8")
    rejecting.chmod(0o755)
    _write_roster(root, ROSTER)  # artifacts differ from HEAD again
    interpreter, log, hook_env = _harness(tmp_path, mode="pass")
    failed_commit = _run(root, interpreter, hook_env, cwd)
    assert failed_commit.returncode == 2, failed_commit.stderr
    _assert_not_success(failed_commit, "rejecting commit hook")
    assert "stage=git/commit" in failed_commit.stderr, failed_commit.stderr
    assert hook_marker.exists(), "the repository's own pre-commit hook never ran"
    assert _head(root) == second, "a failed commit must not move HEAD"
    assert _commit_count(root) == commits_before
    assert _dirty_snapshot(root) == before_dirty
    rejecting.unlink()

    # --- a failing git add: exit 2 before any commit ----------------------
    add_shim_dir, add_log = _git_shim(tmp_path, "add failure git", fail_on="add")
    interpreter, log, add_env = _harness(tmp_path, mode="pass")
    add_env = _with_path(add_env, add_shim_dir)
    failed_add = _run(root, interpreter, add_env, cwd)
    assert failed_add.returncode == 2, failed_add.stderr
    _assert_not_success(failed_add, "git add failure")
    assert "stage=git/add" in failed_add.stderr, failed_add.stderr
    assert "simulating add failure" in failed_add.stderr, failed_add.stderr
    assert _head(root) == second
    assert _commit_count(root) == commits_before
    calls = _shim_calls(add_log)
    assert any(" add " in call for call in calls), calls
    assert not any(" commit " in call for call in calls), calls

    # --- a commit that exits 0 without committing is caught by read-back ---
    # Exit status alone never proves the commit happened: the helper must read
    # the target HEAD, its parent and the committed blobs back before claiming
    # local success.
    lying_dir, lying_log = _git_shim(tmp_path, "lying commit git", swallow="commit")
    interpreter, log, lying_env = _harness(tmp_path, mode="pass")
    lying_env = _with_path(lying_env, lying_dir)
    head_before_lie = _head(root)
    lying = _run(root, interpreter, lying_env, cwd)
    assert lying.returncode == 2, lying.stderr
    _assert_not_success(lying, "lying commit")
    assert "stage=git/verify" in lying.stderr, lying.stderr
    assert _head(root) == head_before_lie, "no commit can result from a faked commit"
    assert any(" commit " in call for call in _shim_calls(lying_log)), _shim_calls(lying_log)

    # --- the validated identity is frozen ACROSS the repository's hooks ---
    # Validation happens before Git runs, so the repository's OWN hooks can
    # still move the bytes afterwards. The approved pair is the raw byte
    # identity of exactly what the real validator accepted; re-reading the
    # working tree or committing whatever the hook left must never redefine it.
    # Every case below is a REAL commit in an owned repository, and each hook
    # copies the pair it saw into owned scratch (the "witness"), so the claim
    # is compared byte for byte instead of being re-checked by schema alone.
    hook_root = tmp_path / "identity hook root"
    _plant_tracked(hook_root)
    _init_repo(hook_root, tracked=sorted(TRACKED_FILES))
    _write_roster(hook_root, ROSTER)
    seed_run = _run(hook_root, sys.executable, env, cwd)
    assert seed_run.returncode == 0, seed_run.stderr
    _make_dirty(hook_root)
    hook_dirty = _dirty_snapshot(hook_root)
    hook_status = _status(hook_root)

    # (a) a non-mutating hook still publishes normally: the committed AND
    #     working bytes are exactly the hook-time witness of the validated
    #     pair, and every unrelated index/worktree entry is untouched.
    witness_ok = tmp_path / "witness non-mutating"
    _install_hook(hook_root, "pre-commit", _witness_lines(witness_ok, hook_root))
    _write_roster(hook_root, ROSTER2)
    clean_hook = _run(hook_root, sys.executable, env, cwd)
    assert clean_hook.returncode == 0, clean_hook.stderr
    assert clean_hook.stdout == ""
    assert "committed locally" in clean_hook.stderr, clean_hook.stderr
    witness_catalog, witness_index = _witness_pair(witness_ok)
    assert _blob(hook_root, "HEAD", "site/catalog.json") == witness_catalog
    assert _blob(hook_root, "HEAD", "site/index.html") == witness_index
    assert (hook_root / CATALOG).read_bytes() == witness_catalog
    assert (hook_root / INDEX).read_bytes() == witness_index
    assert _commit_paths(hook_root) == list(ARTIFACTS)
    validate_artifact_pair(witness_catalog.decode("utf-8"),
                           witness_index.decode("utf-8"))
    assert _dirty_snapshot(hook_root) == hook_dirty
    assert _status(hook_root) == hook_status
    (hook_root / ".git" / "hooks" / "pre-commit").unlink()

    # (b) a hook substitutes BOTH artifacts with a different, individually
    #     COHERENT pair produced by the REAL builder from another synthetic
    #     roster. The committed pair passes the real validator, so a second
    #     schema check cannot reject it: only the frozen identity can.
    other_pair_root = tmp_path / "other coherent pair"
    substituted_catalog, substituted_index = _build_pair(OTHER_ROSTER,
                                                         other_pair_root)
    validate_artifact_pair(substituted_catalog.decode("utf-8"),
                           substituted_index.decode("utf-8"))
    assert substituted_catalog != witness_catalog
    witness_coherent = tmp_path / "witness coherent"
    _install_hook(hook_root, "pre-commit", _witness_lines(witness_coherent, hook_root) + [
        f"cp -f -- {str(other_pair_root / CATALOG)!r} {str(hook_root / CATALOG)!r}\n",
        f"cp -f -- {str(other_pair_root / INDEX)!r} {str(hook_root / INDEX)!r}\n",
        f"cd {str(hook_root)!r} && git add -- site/catalog.json site/index.html\n",
    ])
    _write_roster(hook_root, ROSTER)
    coherent = _run(hook_root, sys.executable, env, cwd)
    assert coherent.returncode == 2, coherent.stderr
    _assert_not_success(coherent, "coherent pair substitution")
    assert "stage=git/verify" in coherent.stderr, coherent.stderr
    assert "committed artifact does not match the validated bytes" in coherent.stderr, coherent.stderr
    assert "may already exist" in coherent.stderr, coherent.stderr
    frozen_catalog, frozen_index = _witness_pair(witness_coherent)
    committed_catalog_now = _blob(hook_root, "HEAD", "site/catalog.json")
    committed_index_now = _blob(hook_root, "HEAD", "site/index.html")
    assert (committed_catalog_now, committed_index_now) == (substituted_catalog,
                                                            substituted_index)
    validate_artifact_pair(committed_catalog_now.decode("utf-8"),
                           committed_index_now.decode("utf-8"))
    assert committed_catalog_now != frozen_catalog
    assert _sha256(committed_catalog_now) != _sha256(frozen_catalog)
    # The diagnostic names BOTH digests, so the comparison is against the
    # frozen identity and not against a fresh read of the same hook bytes.
    assert _sha256(committed_catalog_now) in coherent.stderr, coherent.stderr
    assert _sha256(frozen_catalog) in coherent.stderr, coherent.stderr
    # Negative control, metadata-free: the ORIGINAL comparison (committed blob
    # against the re-read POST-HOOK working file) accepts this pair, and so
    # would any revalidation of the committed pair on its own. That is exactly
    # why the frozen identity is required.
    assert committed_catalog_now == (hook_root / CATALOG).read_bytes()
    assert committed_index_now == (hook_root / INDEX).read_bytes()
    assert _git(hook_root, "diff", "--quiet", "--no-ext-diff", "HEAD", "--",
                *ARTIFACTS, check=False).returncode == 0
    assert _dirty_snapshot(hook_root) == hook_dirty
    (hook_root / ".git" / "hooks" / "pre-commit").unlink()

    # (c) the reproduced defect: a pre-commit hook writes an INVALID catalogue
    #     and stages it after a successful validation. The commit really
    #     happens with those bytes, so the run must report the mismatch
    #     honestly (the bad commit is left for the operator) and claim nothing.
    witness_invalid = tmp_path / "witness invalid"
    _install_hook(hook_root, "pre-commit", _witness_lines(witness_invalid, hook_root) + [
        "printf '%s' '{\"not\":\"a validated catalogue\"}' > "
        f"{str(hook_root / CATALOG)!r}\n",
        f"cd {str(hook_root)!r} && git add -- site/catalog.json\n",
    ])
    _write_roster(hook_root, ROSTER2)
    head_before_invalid = _head(hook_root)
    invalid_hook = _run(hook_root, sys.executable, env, cwd)
    assert invalid_hook.returncode == 2, invalid_hook.stderr
    _assert_not_success(invalid_hook, "invalid catalogue hook")
    assert "stage=git/verify" in invalid_hook.stderr, invalid_hook.stderr
    assert "committed artifact does not match the validated bytes" in invalid_hook.stderr, invalid_hook.stderr
    assert "may already exist" in invalid_hook.stderr, invalid_hook.stderr
    frozen_invalid_catalog, _ = _witness_pair(witness_invalid)
    committed_invalid = _blob(hook_root, "HEAD", "site/catalog.json")
    committed_invalid_index = _blob(hook_root, "HEAD", "site/index.html")
    assert committed_invalid == b'{"not":"a validated catalogue"}'
    assert committed_invalid != frozen_invalid_catalog
    assert _sha256(committed_invalid) != _sha256(frozen_invalid_catalog)
    assert _sha256(committed_invalid) in invalid_hook.stderr, invalid_hook.stderr
    assert _head(hook_root) != head_before_invalid, (
        "the hook's substituted commit really exists")
    rejected = False
    try:
        validate_artifact_pair(committed_invalid.decode("utf-8"),
                               committed_invalid_index.decode("utf-8"))
    except Exception:  # noqa: BLE001 - the real validator must reject it
        rejected = True
    assert rejected, "the real validator accepted the hook's invalid pair"
    assert _dirty_snapshot(hook_root) == hook_dirty
    (hook_root / ".git" / "hooks" / "pre-commit").unlink()

    # (d) a post-commit hook rewrites only the WORKING bytes: the committed
    #     pair is untouched, so comparing the commit alone would succeed. The
    #     working artifacts must still match the frozen identity.
    witness_post = tmp_path / "witness post-commit"
    _install_hook(hook_root, "post-commit", _witness_lines(witness_post, hook_root) + [
        "printf '%s\\n' '<html>hook substituted the working bytes</html>' > "
        f"{str(hook_root / INDEX)!r}\n",
    ])
    _write_roster(hook_root, ROSTER)
    post_run = _run(hook_root, sys.executable, env, cwd)
    assert post_run.returncode == 2, post_run.stderr
    _assert_not_success(post_run, "post-commit working mutation")
    assert "stage=git/verify" in post_run.stderr, post_run.stderr
    assert "working artifact does not match the validated bytes" in post_run.stderr, post_run.stderr
    assert "site/index.html" in post_run.stderr, post_run.stderr
    post_catalog, post_index = _witness_pair(witness_post)
    assert _blob(hook_root, "HEAD", "site/catalog.json") == post_catalog
    assert _blob(hook_root, "HEAD", "site/index.html") == post_index
    assert (hook_root / CATALOG).read_bytes() == post_catalog
    assert (hook_root / INDEX).read_bytes() != post_index
    assert _sha256((hook_root / INDEX).read_bytes()) in post_run.stderr, post_run.stderr
    validate_artifact_pair(post_catalog.decode("utf-8"), post_index.decode("utf-8"))
    assert _dirty_snapshot(hook_root) == hook_dirty
    (hook_root / ".git" / "hooks" / "post-commit").unlink()

    # (e) the second reproduced defect: a post-commit hook REPLACES a working
    #     artifact with a SYMLINK whose target holds exactly the same bytes.
    #     The commit was already made, so the committed blob is the validated
    #     one, and following the link yields identical bytes -- a byte-only
    #     comparison of the working path cannot tell this from a real artifact.
    #     Each current working artifact must therefore be established as an
    #     ORDINARY REGULAR FILE, without following its final symlink, BEFORE its
    #     bytes are read. Checked for both allowlisted paths.
    for case, (rel, roster) in enumerate(((INDEX, ROSTER2), (CATALOG, ROSTER)), 1):
        saved_rel = rel.with_name(rel.name + ".saved")
        witness_link = tmp_path / f"witness symlink {case}"
        _install_hook(hook_root, "post-commit",
                      _witness_lines(witness_link, hook_root) + [
            f"cd {str(hook_root)!r}\n",
            f"mv -- {str(rel)!r} {str(saved_rel)!r}\n",
            f"ln -s -- {saved_rel.name!r} {str(rel)!r}\n",
        ])
        _write_roster(hook_root, roster)
        head_before_link = _head(hook_root)
        link_run = _run(hook_root, sys.executable, env, cwd)
        assert link_run.returncode == 2, (rel, link_run.stderr)
        _assert_not_success(link_run, f"post-commit symlink {rel}")
        assert "stage=git/verify" in link_run.stderr, (rel, link_run.stderr)
        assert "not an ordinary regular file" in link_run.stderr, (rel, link_run.stderr)
        assert str(rel) in link_run.stderr, (rel, link_run.stderr)
        # The honest diagnostic: the commit may already exist and is the
        # operator's to handle.
        assert "may already exist" in link_run.stderr, (rel, link_run.stderr)
        linked_catalog, linked_index = _witness_pair(witness_link)
        frozen = linked_catalog if rel == CATALOG else linked_index
        # The commit really happened and holds exactly the validated bytes,
        # so only the working-artifact TYPE distinguishes this run.
        assert _head(hook_root) != head_before_link, (
            rel, "the hook's commit must really exist")
        assert _blob(hook_root, "HEAD", str(rel)) == frozen
        assert _commit_paths(hook_root) == list(ARTIFACTS)
        # Both COMMITTED blobs are the witness pair (a blob read never consults
        # the working tree, which is now a symlink).
        assert _blob(hook_root, "HEAD", "site/catalog.json") == linked_catalog
        assert _blob(hook_root, "HEAD", "site/index.html") == linked_index
        # The working path is a symlink and STILL yields the identical bytes.
        assert (hook_root / rel).is_symlink(), (
            rel, "the fault hook must install a symlink")
        assert (hook_root / rel).read_bytes() == frozen
        assert _sha256((hook_root / rel).read_bytes()) == _sha256(frozen)
        assert _dirty_snapshot(hook_root) == hook_dirty
        # Restore an ordinary file for the next step (the fault hook moved the
        # original aside, so no byte is invented here).
        (hook_root / rel).unlink()
        (hook_root / saved_rel).replace(hook_root / rel)
        assert not (hook_root / rel).is_symlink()
        assert (hook_root / rel).read_bytes() == frozen
        (hook_root / ".git" / "hooks" / "post-commit").unlink()

    # (f) positive control: an EXACT record followed by trailing NEWLINES is
    #     still the exact record. Only trailing newlines are normalised (Bash
    #     command substitution strips them); everything else stays byte-exact,
    #     so a genuine publication through this wrapper really commits.
    _write_roster(hook_root, ROSTER2)
    interpreter, log, newline_env = _harness(tmp_path, mode="receipt_trailing_newlines")
    head_before_newlines = _head(hook_root)
    newline_run = _run(hook_root, interpreter, newline_env, cwd)
    assert newline_run.returncode == 0, newline_run.stderr
    assert newline_run.stdout == ""
    assert "committed locally" in newline_run.stderr, newline_run.stderr
    emitted_newlines = ast.literal_eval(_marker(log, "validate_stdout"))
    assert emitted_newlines.endswith("\n\n\n"), emitted_newlines
    assert _marker(log, "validator").startswith("0 "), (
        _marker(log, "validator"))
    assert _kinds(log) == ["builder", "validate", "verify"], _kinds(log)
    assert _head(hook_root) != head_before_newlines
    assert _commit_paths(hook_root) == list(ARTIFACTS)
    assert _blob(hook_root, "HEAD", "site/catalog.json") == (hook_root / CATALOG).read_bytes()
    assert _blob(hook_root, "HEAD", "site/index.html") == (hook_root / INDEX).read_bytes()
    assert _dirty_snapshot(hook_root) == hook_dirty

    # --- EITHER artifact alone is a legitimate artifact commit -------------
    # The reproduced defect: a normal publication is followed by an unrelated
    # baseline change committed INTO one artifact only (an HTML comment in the
    # page, or a trailing newline in the catalogue). Both baseline pairs pass
    # the real validator, and the next genuine build restores just that one
    # artifact. The allowed changed-path set is therefore a NONEMPTY SUBSET of
    # the two literals -- either alone or both -- while the approved PAIR (both
    # frozen identities, committed AND in the working tree) is still required.
    single_root = tmp_path / "single artifact root"
    _plant_tracked(single_root)
    _init_repo(single_root, tracked=sorted(TRACKED_FILES))
    _write_roster(single_root, ROSTER)
    single_env = _env()
    single_seed = _run(single_root, sys.executable, single_env, cwd)
    assert single_seed.returncode == 0, single_seed.stderr
    approved_catalog = _blob(single_root, "HEAD", "site/catalog.json")
    approved_index = _blob(single_root, "HEAD", "site/index.html")
    assert (approved_catalog, approved_index) == (
        (single_root / CATALOG).read_bytes(), (single_root / INDEX).read_bytes())
    validate_artifact_pair(approved_catalog.decode("utf-8"),
                           approved_index.decode("utf-8"))
    _make_dirty(single_root)
    single_dirty = _dirty_snapshot(single_root)
    single_unrelated = [entry for entry in _status(single_root)
                        if "site/" not in entry]
    assert len(single_unrelated) == len(DIRTY_PATHS), single_unrelated

    one_artifact_cases = (
        # (label, the single artifact the BASELINE commit changes, its bytes)
        ("index.html only", INDEX,
         approved_index + b"<!-- synthetic baseline comment -->\n"),
        ("catalog.json only", CATALOG, approved_catalog + b"\n"),
    )
    for label, rel, baseline_bytes in one_artifact_cases:
        # An explicit synthetic baseline commit changes exactly this artifact,
        # and the resulting baseline PAIR still passes the real validator.
        (single_root / rel).write_bytes(baseline_bytes)
        _git(single_root, "add", "--", str(rel))
        # Path-limited, like the helper: the staged unrelated work must stay out.
        _git(single_root, "commit", "-q", "--only", "-m",
             f"synthetic baseline: {label}", "--", str(rel))
        baseline_head = _head(single_root)
        assert _commit_paths(single_root) == [str(rel)], label
        validate_artifact_pair((single_root / CATALOG).read_bytes().decode("utf-8"),
                               (single_root / INDEX).read_bytes().decode("utf-8"))
        # The genuine builder now restores just that one artifact.
        one = _run(single_root, sys.executable, single_env, cwd)
        assert one.returncode == 0, (label, one.stderr)
        assert one.stdout == "", (label, one.stdout)
        assert "committed locally" in one.stderr, (label, one.stderr)
        one_head = _head(single_root)
        assert one_head != baseline_head, (label, "no commit was made")
        assert _parents(single_root) == [baseline_head], label
        # The actual stored commit changes EXACTLY the intended single path.
        assert _commit_paths(single_root) == [str(rel)], (label,
                                                          _commit_paths(single_root))
        # BOTH committed blobs AND BOTH ordinary working artifacts still equal
        # the approved pair: the unchanged artifact is part of the contract.
        assert _blob(single_root, "HEAD", "site/catalog.json") == approved_catalog, label
        assert _blob(single_root, "HEAD", "site/index.html") == approved_index, label
        assert (single_root / CATALOG).read_bytes() == approved_catalog, label
        assert (single_root / INDEX).read_bytes() == approved_index, label
        validate_artifact_pair(
            _blob(single_root, "HEAD", "site/catalog.json").decode("utf-8"),
            _blob(single_root, "HEAD", "site/index.html").decode("utf-8"))
        assert _git(single_root, "diff", "--quiet", "--no-ext-diff", "HEAD",
                    "--", *ARTIFACTS, check=False).returncode == 0, label
        # All six unrelated dirty/index shapes survived untouched.
        assert _dirty_snapshot(single_root) == single_dirty, label
        assert [entry for entry in _status(single_root)
                if "site/" not in entry] == single_unrelated, label

    # A second identical run takes the no-change path: no commit, HEAD stable,
    # unrelated entries untouched and stdout silent.
    single_commits = _commit_count(single_root)
    single_head_now = _head(single_root)
    repeat = _run(single_root, sys.executable, single_env, cwd)
    assert repeat.returncode == 0, repeat.stderr
    assert repeat.stdout == ""
    assert "no artifact change" in repeat.stderr, repeat.stderr
    assert "committed locally" not in repeat.stderr, repeat.stderr
    assert _head(single_root) == single_head_now, "a no-change run must not commit"
    assert _commit_count(single_root) == single_commits
    assert _dirty_snapshot(single_root) == single_dirty

    # A one-path commit still depends on BOTH frozen identities: a real
    # ``post-commit`` hook mutates the artifact this commit does NOT change, so
    # checking only the changed file would pass. The commit itself is exactly
    # the single catalogue path and holds the approved pair.
    (single_root / CATALOG).write_bytes(approved_catalog + b"\n")
    _git(single_root, "add", "--", str(CATALOG))
    _git(single_root, "commit", "-q", "--only", "-m",
         "synthetic baseline: catalog newline", "--", str(CATALOG))
    guarded_head = _head(single_root)
    assert _commit_paths(single_root) == [str(CATALOG)]
    witness_unchanged = tmp_path / "witness unchanged artifact"
    _install_hook(single_root, "post-commit",
                  _witness_lines(witness_unchanged, single_root) + [
        "printf '%s' '<!-- hook mutated the unchanged artifact -->' >> "
        f"{str(single_root / INDEX)!r}\n",
    ])
    guarded = _run(single_root, sys.executable, single_env, cwd)
    assert guarded.returncode == 2, guarded.stderr
    _assert_not_success(guarded, "post-commit mutation of the unchanged artifact")
    assert "stage=git/verify" in guarded.stderr, guarded.stderr
    assert "working artifact does not match the validated bytes" in guarded.stderr, (
        guarded.stderr)
    assert "site/index.html" in guarded.stderr, guarded.stderr
    assert "may already exist" in guarded.stderr, guarded.stderr
    assert _head(single_root) != guarded_head
    assert _parents(single_root) == [guarded_head]
    assert _commit_paths(single_root) == [str(CATALOG)], _commit_paths(single_root)
    assert _blob(single_root, "HEAD", "site/catalog.json") == approved_catalog
    assert _blob(single_root, "HEAD", "site/index.html") == approved_index
    guarded_catalog, guarded_index = _witness_pair(witness_unchanged)
    assert (guarded_catalog, guarded_index) == (approved_catalog, approved_index)
    assert (single_root / INDEX).read_bytes() != approved_index
    assert _sha256((single_root / INDEX).read_bytes()) in guarded.stderr, guarded.stderr
    # Restore the approved working bytes from the hook's own witness copy (no
    # byte is invented here), so the later assertions see the approved pair.
    (single_root / INDEX).write_bytes(guarded_index)
    assert _dirty_snapshot(single_root) == single_dirty
    (single_root / ".git" / "hooks" / "post-commit").unlink()

    # --- an OUTSIDE path in a real commit is rejected even when BOTH
    #     artifacts still match their frozen bytes ---------------------------
    # The fault is created by an owned Git launcher that turns the helper's
    # PATH-LIMITED commit into a genuine full-index commit carrying one named
    # outside path, so the stored commit object itself is what gets inspected.
    wide_root = tmp_path / "outside path root"
    _plant_tracked(wide_root)
    _init_repo(wide_root, tracked=sorted(TRACKED_FILES))
    _write_roster(wide_root, ROSTER)
    wide_env = _env()
    wide_seed = _run(wide_root, sys.executable, wide_env, cwd)
    assert wide_seed.returncode == 0, wide_seed.stderr
    wide_catalog = _blob(wide_root, "HEAD", "site/catalog.json")
    wide_index = _blob(wide_root, "HEAD", "site/index.html")
    _make_dirty(wide_root)
    # A one-artifact baseline, so the outside path is the ONLY fault: without
    # it this run would be a legitimate single-artifact commit.
    (wide_root / INDEX).write_bytes(wide_index + b"<!-- synthetic baseline comment -->\n")
    _git(wide_root, "add", "--", str(INDEX))
    _git(wide_root, "commit", "-q", "--only", "-m",
         "synthetic baseline: index only", "--", str(INDEX))
    wide_baseline = _head(wide_root)
    widen_dir, widen_log = _git_shim(tmp_path, "widening git", widen="notes.md")
    interpreter, log, widen_env = _harness(tmp_path, mode="pass")
    widened = _run(wide_root, interpreter, _with_path(widen_env, widen_dir), cwd)
    assert widened.returncode == 2, widened.stderr
    _assert_not_success(widened, "outside path in the artifact commit")
    assert "stage=git/verify" in widened.stderr, widened.stderr
    assert "outside" in widened.stderr, widened.stderr
    assert "notes.md" in widened.stderr, widened.stderr
    assert "may already exist" in widened.stderr, widened.stderr
    assert any(" commit " in call for call in _shim_calls(widen_log)), (
        _shim_calls(widen_log))
    # The fault-repository really made that commit: its STORED path list has a
    # third path, while both allowlisted artifacts still hold approved bytes.
    wide_head = _head(wide_root)
    assert wide_head != wide_baseline
    assert _parents(wide_root) == [wide_baseline]
    stored_paths = _commit_paths(wide_root)
    assert "notes.md" in stored_paths, stored_paths
    assert set(stored_paths) - set(ARTIFACTS), stored_paths
    assert _blob(wide_root, "HEAD", "site/catalog.json") == wide_catalog
    assert _blob(wide_root, "HEAD", "site/index.html") == wide_index
    # A PATH-FILTERED inspection would have accepted that commit -- exactly the
    # bounded weakening the whole-commit assertion above exists to catch.
    filtered_paths = _git(wide_root, "diff-tree", "--no-commit-id", "--name-only",
                          "-r", "--no-renames", "HEAD", "--", *ARTIFACTS
                          ).stdout.split()
    assert sorted(filtered_paths) == [str(INDEX)], filtered_paths

    # --- earlier failures never reach the Git phase at all ----------------
    log_shim_dir, git_log = _git_shim(tmp_path, "logging git")
    head_now = _head(root)
    index_now = _dirty_snapshot(root)

    _write_raw_roster(root, "THIS IS NOT JSON{{")
    interpreter, log, build_env = _harness(tmp_path, mode="pass")
    build_env = _with_path(build_env, log_shim_dir)
    broken_build = _run(root, interpreter, build_env, cwd)
    assert broken_build.returncode == 2, broken_build.stderr
    assert "stage=builder" in broken_build.stderr, broken_build.stderr
    assert _head(root) == head_now
    assert _dirty_snapshot(root) == index_now
    assert _shim_calls(git_log) == [], _shim_calls(git_log)

    _write_roster(root, ROSTER)
    interpreter, log, validate_env = _harness(tmp_path, mode="mismatch")
    validate_env = _with_path(validate_env, log_shim_dir)
    broken_pair = _run(root, interpreter, validate_env, cwd)
    assert broken_pair.returncode == 2, broken_pair.stderr
    assert "stage=validate" in broken_pair.stderr, broken_pair.stderr
    assert _head(root) == head_now
    assert _dirty_snapshot(root) == index_now
    assert _shim_calls(git_log) == [], _shim_calls(git_log)

    # --- refused Git targets: no repository, nested root, detached HEAD ---
    plain_root = Path(tempfile.mkdtemp(prefix="publish-site-no-repo-",
                                       dir=str(_non_repo_base())))
    try:
        _write_roster(plain_root, ROSTER)
        proc = _run(plain_root, sys.executable, env, cwd)
        assert proc.returncode == 2, proc.stderr
        _assert_not_success(proc, "non-repository root")
        assert "stage=git" in proc.stderr, proc.stderr
        assert "not a git repository" in proc.stderr, proc.stderr
        assert not (plain_root / ".git").exists(), "the helper must never git-init a root"
        assert (plain_root / CATALOG).is_file(), "the build still ran before the refusal"
    finally:
        shutil.rmtree(plain_root, ignore_errors=True)

    parent = tmp_path / "ancestor repo"
    _plant_tracked(parent)
    parent_head = _init_repo(parent, tracked=sorted(TRACKED_FILES))
    nested = parent / "nested root"
    _write_roster(nested, ROSTER)
    proc = _run(nested, sys.executable, env, cwd)
    assert proc.returncode == 2, proc.stderr
    _assert_not_success(proc, "nested root")
    assert "stage=git" in proc.stderr, proc.stderr
    assert "top level" in proc.stderr and str(parent) in proc.stderr, proc.stderr
    assert not (nested / ".git").exists()
    assert _head(parent) == parent_head
    assert _commit_count(parent) == 1
    assert _git(parent, "diff", "--quiet", "--no-ext-diff", "HEAD",
                check=False).returncode == 0
    assert _git(parent, "diff", "--cached", "--quiet",
                check=False).returncode == 0

    detached = tmp_path / "detached root"
    _plant_tracked(detached)
    detached_head = _init_repo(detached, tracked=sorted(TRACKED_FILES))
    _write_roster(detached, ROSTER)
    _git(detached, "checkout", "-q", "--detach", "HEAD")
    proc = _run(detached, sys.executable, env, cwd)
    assert proc.returncode == 2, proc.stderr
    _assert_not_success(proc, "detached HEAD")
    assert "stage=git" in proc.stderr, proc.stderr
    assert "detached" in proc.stderr.lower(), proc.stderr
    assert _head(detached) == detached_head
    assert _commit_count(detached) == 1

    # --- missing git is reported explicitly, after a genuine build --------
    sparse_bin = tmp_path / "no git bin"
    sparse_bin.mkdir()
    for tool in ("bash", "dirname", "readlink", "mkdir", "flock"):
        resolved = shutil.which(tool)
        assert resolved, f"{tool} is required for this control"
        (sparse_bin / tool).symlink_to(resolved)
    nogit_root = tmp_path / "no git root"
    _plant_tracked(nogit_root)
    _init_repo(nogit_root, tracked=sorted(TRACKED_FILES))
    _write_roster(nogit_root, ROSTER)
    proc = _run(nogit_root, sys.executable, dict(env, PATH=str(sparse_bin)), cwd)
    assert proc.returncode == 2, proc.stderr
    _assert_not_success(proc, "missing git")
    assert "stage=git" in proc.stderr, proc.stderr
    assert "not found" in proc.stderr, proc.stderr
    assert _commit_count(nogit_root) == 1

    # --- the remaining state guards are straightforward and structural ----
    source = HELPER.read_text(encoding="utf-8")
    for token in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
                  "rebase-apply", "--unmerged", "--is-bare-repository",
                  "--show-toplevel", "symbolic-ref", "--absolute-git-dir",
                  "--only", "O_NOFOLLOW", "lstat", "S_ISREG"):
        assert token in source, token
    # The allowlist is literal: no glob, no directory, no computed path.
    assert "readonly ARTIFACT_INDEX='site/index.html'" in source
    assert "readonly ARTIFACT_CATALOG='site/catalog.json'" in source
    assert "no-verify" not in source, "the helper must never bypass hooks"
    assert "git_root init" not in source, "the helper must never initialise a repository"
    # Publication is ONE explicitly scoped, non-forcing refspec per push, sent
    # through the bounded noninteractive remote wrapper to the resolved PUSH
    # URL -- never through the root's default refspec, push.default, a mirror,
    # a forced update, a tag publication or an upstream configuration write.
    assert source.count("push --quiet --no-follow-tags") == 1, "one scoped push"
    push_command = [line for line in source.splitlines()
                    if "push --quiet --no-follow-tags" in line][0]
    for forbidden in ("--force", "--delete", "--mirror", "--all", "--tags",
                      "set-upstream", "no-verify"):
        assert forbidden not in push_command, (forbidden, push_command)
    for token in ("--no-follow-tags", "--recurse-submodules=no",
                  "remote get-url --push --all", "ls-remote --refs --exit-code",
                  "merge-base --is-ancestor", "push/config", "push/inspect",
                  "push/history", "push/send", "push/verify",
                  "GIT_TERMINAL_PROMPT=0"):
        assert token in source, token
    # No git command line may force, delete, mirror, fetch, initialise, rewrite
    # configuration or create an upstream/tracking relationship.
    command_lines = [line.strip() for line in source.splitlines()
                     if line.strip().startswith(("remote_git ", "git_root "))]
    for forbidden in ("--force", "--delete", "--mirror", "remote add",
                      "remote set-url", "remote update", "remote prune",
                      " fetch ", " init ", " config --add", " config --set",
                      " config --replace-all", " config --unset"):
        assert not any(forbidden in line for line in command_lines), (
            forbidden, command_lines)

    # --- this helper's own checkout was never a commit target -------------
    assert _head(REPO) == helper_source_head
# --------------------------------------------------------------------------
# 5. opt-in publication: one destination, retry, exact read-back
# --------------------------------------------------------------------------

def test_push_mode_publishes_retries_and_reads_the_exact_destination(tmp_path):
    """Opt-in ``--push``: one destination, retry after failure, exact read-back.

    Every push here targets an owned local BARE repository created under this
    test's tmp dir; no provider config, credential, public remote or installed
    wrapper is involved and no command here contacts the network.

    (A) first publication: the real artifact commit reaches the single
        configured upstream branch and its remote blobs are the verified local
        candidate; only that branch moves (``push.default=matching`` and a
        second local branch change nothing) and an unrelated annotated tag stays
        unpublished even with ``push.followTags`` configured; all six unrelated
        dirty/index shapes survive; a later default (no ``--push``) invocation
        commits locally while issuing NO remote command;
    (B) a real rejecting ``pre-receive`` hook: exit 2 at ``stage=push/send``, the
        local artifact commit is kept, the destination stays at the seeded tip
        and the endpoint path is never echoed; after the owned rejector is
        removed the SAME bytes are retried with no new local commit, and a
        further identical run takes the already-current path without a push;
    (C) two artifact-only commits that were never pushed publish in order, and
        the earlier one is really on the destination afterwards;
    (D) a failed build, an invalid pair and a rejected local commit issue no
        push at all, and a same-bytes no-change run whose candidate blobs do not
        match the frozen identities -- or whose working artifact was replaced by
        a same-bytes symlink -- neither pushes nor claims already-current (with
        an owned launcher that hides Git's own no-diff observation);
    (E) an absent/duplicated upstream, the local repository as upstream, two
        push URLs, a mirror remote and a missing destination branch are refused
        instead of guessed;
    (F) discovery and read-back use the ACTUAL push URL when the fetch URL is a
        different repository;
    (G) the history boundary fails closed: a divergent destination tip that is
        known locally, an ahead range carrying an unrelated private path, and an
        artifact-only range whose intermediate pair the real validator rejects;
    (H) a zero-exit push that pushed nothing, a ref that reads back stale and an
        endpoint that cannot be read back at all after a real push are all
        rejected with an honest "a remote write may already have occurred";
    (I) the publish lock covers publication AND the post-push read-back: a
        blocking receive hook and a blocking read-back launcher both find the
        same lock inode held, and a second helper cannot enter its builder;
    (J) the SEND keeps the configured remote NAME: a real ``pre-push`` hook
        that records both destination arguments and rejects only when the first
        equals the selected configured alias stops the publication (exit 2 at
        ``stage=push/send``, destination unchanged, local candidate retained),
        and after the owned policy deliberately allows it the unchanged retry
        publishes with no new commit -- the recorded arguments prove an
        unrelated error cannot satisfy this;
    (K) a documented remote-specific transport setting
        (``remote.<name>.receivepack``) stays in force: an owned recording
        wrapper that delegates to the real ``git-receive-pack`` is invoked by
        an ordinary named-remote positive control AND by the helper's send;
    (L) URL-shaped invalid configuration is refused at ``stage=push/config``
        with empty stdout, no resolved or contacted remote, no push, and
        neither the full value nor its synthetic userinfo/token fragments in
        stderr (a credential-bearing ``branch.<branch>.remote`` and a raw
        URL-shaped destination ref), while ordinary publication still works.
    """
    from catalog_export import validate_artifact_pair

    cwd = tmp_path / "push cwd"
    cwd.mkdir()
    env = _env()
    real_git = shutil.which("git")
    assert real_git, "the publication tests require the git executable"

    # --- (A) first publication to one owned bare destination --------------
    root = tmp_path / "push root"
    _plant_tracked(root)
    baseline = _init_repo(root, tracked=sorted(TRACKED_FILES))
    bare = _bare(tmp_path, "destination bare.git")
    _configure_upstream(root, bare)
    # The destination is seeded EXPLICITLY from the synthetic baseline.
    _git(root, "push", "-q", str(bare), "HEAD:refs/heads/main")
    assert _remote_refs(bare) == {"refs/heads/main": baseline}
    # An unrelated branch on the destination, a second LOCAL branch and
    # push.default=matching: only the configured upstream branch may move.
    _git(bare, "branch", "unrelated-branch", baseline)
    _git(root, "branch", "local-only", baseline)
    _git(root, "config", "push.default", "matching")
    # An unrelated annotated tag plus tag-following configured: tag refs are
    # never part of a branch publication.
    _git(root, "tag", "-a", "unrelated-synthetic", "-m",
         "synthetic annotated tag", baseline)
    _git(root, "config", "push.followTags", "true")
    _write_roster(root, ROSTER)
    _make_dirty(root)
    dirty_before = _dirty_snapshot(root)
    status_before = _status(root)

    published = _run(root, sys.executable, env, cwd, extra=("--push",))
    assert published.returncode == 0, published.stderr
    assert published.stdout == "", published.stdout
    candidate = _head(root)
    assert candidate != baseline
    assert _parents(root) == [baseline]
    assert _commit_paths(root) == list(ARTIFACTS)
    assert f"pushed {candidate}" in published.stderr, published.stderr
    assert "read that exact ref back" in published.stderr, published.stderr
    assert "no Pages deployment" in published.stderr, published.stderr

    # The destination carries exactly the verified local candidate, and only
    # the intended branch moved.
    assert _remote_refs(bare) == {
        "refs/heads/main": candidate,
        "refs/heads/unrelated-branch": baseline,
    }, _remote_refs(bare)
    remote_catalog = _blob(bare, candidate, "site/catalog.json")
    remote_index = _blob(bare, candidate, "site/index.html")
    assert remote_catalog == _blob(root, "HEAD", "site/catalog.json")
    assert remote_index == _blob(root, "HEAD", "site/index.html")
    assert remote_catalog == (root / CATALOG).read_bytes()
    assert remote_index == (root / INDEX).read_bytes()
    validate_artifact_pair(remote_catalog.decode("utf-8"),
                           remote_index.decode("utf-8"))
    assert "refs/heads/local-only" not in _remote_refs(bare), _remote_refs(bare)
    assert not any(ref.startswith("refs/tags/") for ref in _remote_refs(bare)), (
        "an annotated tag must never ride along with a branch publication")
    assert _dirty_snapshot(root) == dirty_before
    assert _status(root) == status_before

    # --- a default invocation on the same root never contacts the remote ---
    _write_roster(root, ROSTER2)
    no_push_shim, no_push_log = _git_shim(tmp_path, "no-push git")
    interpreter, log, no_push_env = _harness(tmp_path, mode="pass")
    local_only = _run(root, interpreter, _with_path(no_push_env, no_push_shim), cwd)
    assert local_only.returncode == 0, local_only.stderr
    assert local_only.stdout == ""
    assert "committed locally" in local_only.stderr, local_only.stderr
    second_candidate = _head(root)
    assert second_candidate != candidate
    assert _remote_head(bare) == candidate, "the default run must not push"
    remote_calls = [call for call in _shim_calls(no_push_log)
                    if " push " in call or " ls-remote " in call or " get-url " in call]
    assert remote_calls == [], remote_calls
    assert _dirty_snapshot(root) == dirty_before
    assert _status(root) == status_before

    # --- (B) a real rejecting receive hook, then the SAME bytes retried ---
    retry_root = tmp_path / "retry root"
    _plant_tracked(retry_root)
    retry_baseline = _init_repo(retry_root, tracked=sorted(TRACKED_FILES))
    retry_bare = _bare(tmp_path, "retry bare.git")
    _configure_upstream(retry_root, retry_bare)
    _git(retry_root, "push", "-q", str(retry_bare), "HEAD:refs/heads/main")
    _write_roster(retry_root, ROSTER)
    _make_dirty(retry_root)
    retry_dirty = _dirty_snapshot(retry_root)
    retry_status = _status(retry_root)
    rejector = _receive_hook(retry_bare, "pre-receive", [
        "echo 'synthetic pre-receive rejection' >&2\n",
        "exit 1\n",
    ])
    rejected = _run(retry_root, sys.executable, env, cwd, extra=("--push",))
    assert rejected.returncode == 2, (rejected.returncode, rejected.stderr)
    _assert_not_success(rejected, "rejected push")
    assert "stage=push/send" in rejected.stderr, rejected.stderr
    assert "retained for a later retry" in rejected.stderr, rejected.stderr
    assert str(retry_bare) not in rejected.stderr, (
        "the endpoint path must never be echoed into diagnostics", rejected.stderr)
    pending = _head(retry_root)
    assert _parents(retry_root) == [retry_baseline]
    assert _commit_paths(retry_root) == list(ARTIFACTS)
    assert _remote_refs(retry_bare) == {"refs/heads/main": retry_baseline}
    assert _dirty_snapshot(retry_root) == retry_dirty
    assert _status(retry_root) == retry_status

    # Remove the owned rejector and retry the SAME roster/artifact bytes: no new
    # local commit is made, and the previously committed candidate is pushed.
    rejector.unlink()
    commits_before = _commit_count(retry_root)
    retried_shim, retried_log = _git_shim(tmp_path, "retried push git")
    retried = _run(retry_root, sys.executable, _with_path(_env(), retried_shim),
                   cwd, extra=("--push",))
    assert retried.returncode == 0, retried.stderr
    assert retried.stdout == ""
    assert _commit_count(retry_root) == commits_before, (
        "a retry must not create another artifact commit")
    assert _head(retry_root) == pending
    assert f"pushed {pending}" in retried.stderr, retried.stderr
    push_calls = [call for call in _shim_calls(retried_log) if " push " in call]
    assert len(push_calls) == 1, push_calls
    assert _remote_refs(retry_bare) == {"refs/heads/main": pending}
    assert _blob(retry_bare, pending, "site/catalog.json") == (retry_root / CATALOG).read_bytes()
    assert _blob(retry_bare, pending, "site/index.html") == (retry_root / INDEX).read_bytes()
    assert _dirty_snapshot(retry_root) == retry_dirty

    # A further identical run is already current: it inspects the remote, then
    # pushes nothing and commits nothing.
    current_shim, current_log = _git_shim(tmp_path, "already-current git")
    already = _run(retry_root, sys.executable, _with_path(_env(), current_shim),
                   cwd, extra=("--push",))
    assert already.returncode == 0, already.stderr
    assert already.stdout == ""
    assert "is current" in already.stderr, already.stderr
    assert _commit_count(retry_root) == commits_before
    assert _head(retry_root) == pending
    assert _remote_refs(retry_bare) == {"refs/heads/main": pending}
    current_calls = _shim_calls(current_log)
    assert any(" ls-remote " in call for call in current_calls), current_calls
    assert not any(" push " in call for call in current_calls), current_calls
    assert _dirty_snapshot(retry_root) == retry_dirty

    # --- (C) two never-pushed artifact commits publish in order -----------
    order_root = tmp_path / "order root"
    _plant_tracked(order_root)
    order_baseline = _init_repo(order_root, tracked=sorted(TRACKED_FILES))
    order_bare = _bare(tmp_path, "order bare.git")
    _configure_upstream(order_root, order_bare)
    _git(order_root, "push", "-q", str(order_bare), "HEAD:refs/heads/main")
    _write_roster(order_root, ROSTER)
    local_first = _run(order_root, sys.executable, env, cwd)
    assert local_first.returncode == 0, local_first.stderr
    first_commit = _head(order_root)
    assert _parents(order_root) == [order_baseline]
    _write_roster(order_root, ROSTER2)
    local_second = _run(order_root, sys.executable, env, cwd)
    assert local_second.returncode == 0, local_second.stderr
    second_commit = _head(order_root)
    assert _parents(order_root) == [first_commit]
    assert _commit_paths(order_root) == list(ARTIFACTS)
    assert _remote_refs(order_bare) == {"refs/heads/main": order_baseline}
    order_commits = _commit_count(order_root)
    ordered = _run(order_root, sys.executable, env, cwd, extra=("--push",))
    assert ordered.returncode == 0, ordered.stderr
    assert ordered.stdout == ""
    assert _commit_count(order_root) == order_commits
    assert _remote_refs(order_bare) == {"refs/heads/main": second_commit}
    assert _git(order_bare, "merge-base", "--is-ancestor", order_baseline,
                first_commit, check=False).returncode == 0
    assert _git(order_bare, "merge-base", "--is-ancestor", first_commit,
                second_commit, check=False).returncode == 0
    assert _blob(order_bare, first_commit, "site/catalog.json") == _blob(
        order_root, first_commit, "site/catalog.json")
    assert _blob(order_bare, second_commit, "site/index.html") == (
        order_root / INDEX).read_bytes()
    assert f"pushed {second_commit}" in ordered.stderr, ordered.stderr

    # --- (D) failed build, pair, commit or identity never reaches a push ---
    gate_root = tmp_path / "gate root"
    _plant_tracked(gate_root)
    _init_repo(gate_root, tracked=sorted(TRACKED_FILES))
    gate_bare = _bare(tmp_path, "gate bare.git")
    _configure_upstream(gate_root, gate_bare)
    _git(gate_root, "push", "-q", str(gate_bare), "HEAD:refs/heads/main")
    _write_roster(gate_root, ROSTER)
    gate_seed = _run(gate_root, sys.executable, env, cwd, extra=("--push",))
    assert gate_seed.returncode == 0, gate_seed.stderr
    approved_head = _head(gate_root)
    approved_catalog = _blob(gate_root, "HEAD", "site/catalog.json")
    approved_index = _blob(gate_root, "HEAD", "site/index.html")
    assert _remote_refs(gate_bare) == {"refs/heads/main": approved_head}
    _make_dirty(gate_root)
    gate_dirty = _dirty_snapshot(gate_root)
    gate_shim, gate_log = _git_shim(tmp_path, "gate git")

    _write_raw_roster(gate_root, "THIS IS NOT JSON{{")
    interpreter, log, build_env = _harness(tmp_path, mode="pass")
    broken = _run(gate_root, interpreter, _with_path(build_env, gate_shim), cwd,
                  extra=("--push",))
    assert broken.returncode == 2, broken.stderr
    assert "stage=builder" in broken.stderr, broken.stderr

    _write_roster(gate_root, ROSTER2)
    interpreter, log, pair_env = _harness(tmp_path, mode="mismatch")
    bad_pair = _run(gate_root, interpreter, _with_path(pair_env, gate_shim), cwd,
                    extra=("--push",))
    assert bad_pair.returncode == 2, bad_pair.stderr
    assert "stage=validate" in bad_pair.stderr, bad_pair.stderr

    commit_marker = tmp_path / "gate commit hook ran"
    rejecting = _install_hook(gate_root, "pre-commit", [
        f"printf 'ran' > {str(commit_marker)!r}\n",
        "echo 'synthetic hook rejecting the commit' >&2\n",
        "exit 1\n",
    ])
    rejected_commit = _run(gate_root, sys.executable,
                           _with_path(_env(), gate_shim), cwd, extra=("--push",))
    assert rejected_commit.returncode == 2, rejected_commit.stderr
    _assert_not_success(rejected_commit, "rejected local commit")
    assert "stage=git/commit" in rejected_commit.stderr, rejected_commit.stderr
    assert commit_marker.exists(), "the repository's own hook never ran"
    rejecting.unlink()
    assert _head(gate_root) == approved_head
    assert _remote_refs(gate_bare) == {"refs/heads/main": approved_head}
    assert _dirty_snapshot(gate_root) == gate_dirty
    assert not any(" push " in call or " ls-remote " in call
                   for call in _shim_calls(gate_log)), _shim_calls(gate_log)

    # Re-sync the approved pair locally (no publication is needed: the remote
    # already holds it), then make Git itself report NO artifact change while
    # the committed candidate blob is not the frozen identity.
    _write_roster(gate_root, ROSTER)
    resync = _run(gate_root, sys.executable, env, cwd, extra=("--push",))
    assert resync.returncode == 0, resync.stderr
    assert "is current" in resync.stderr, resync.stderr
    (gate_root / CATALOG).write_bytes(approved_catalog + b"\n")
    _git(gate_root, "add", "--", str(CATALOG))
    _git(gate_root, "commit", "-q", "--only", "-m",
         "synthetic baseline: catalog newline", "--", str(CATALOG))
    assert _commit_paths(gate_root) == [str(CATALOG)]
    (gate_root / CATALOG).write_bytes(approved_catalog)
    # The real Git DOES see the difference; the owned launcher reports none, so
    # only the frozen candidate read-back can refuse this run.
    assert _git(gate_root, "diff", "--quiet", "--no-ext-diff", "HEAD",
                "--", str(CATALOG), check=False).returncode == 1
    nodiff_shim, nodiff_log = _git_branch_shim(tmp_path, "no-diff git", "diff",
                                               ["exit 0"])
    hidden = _run(gate_root, sys.executable, _with_path(_env(), nodiff_shim), cwd,
                  extra=("--push",))
    assert hidden.returncode == 2, hidden.stderr
    _assert_not_success(hidden, "hidden candidate blob difference")
    assert "stage=git/verify" in hidden.stderr, hidden.stderr
    assert "does not match the validated bytes" in hidden.stderr, hidden.stderr
    assert not any(" push " in call for call in _shim_calls(nodiff_log)), (
        _shim_calls(nodiff_log))
    assert _remote_refs(gate_bare) == {"refs/heads/main": approved_head}

    # Restore the approved catalogue, then let an OWNED post-build step replace
    # the freshly written page with a symlink holding exactly the approved
    # bytes (the real build rewrites the pair atomically, so this cannot be
    # pre-arranged in the tree). The reviewed pre-Git type guard refuses this
    # before the diff, the commit or any remote contact; Git is told "no change"
    # as well, so the run still cannot push or claim already-current.
    (gate_root / CATALOG).write_bytes(approved_catalog)
    _git(gate_root, "add", "--", str(CATALOG))
    _git(gate_root, "commit", "-q", "--only", "-m",
         "synthetic baseline: restore catalog", "--", str(CATALOG))
    assert _blob(gate_root, "HEAD", "site/catalog.json") == approved_catalog
    interpreter, log, linked_env = _harness(tmp_path, mode="postbuild_symlink")
    linked = _run(gate_root, interpreter, _with_path(linked_env, nodiff_shim), cwd,
                  extra=("--push",))
    assert linked.returncode == 2, linked.stderr
    _assert_not_success(linked, "working artifact type")
    assert "stage=git" in linked.stderr, linked.stderr
    assert "not a regular file" in linked.stderr, linked.stderr
    assert "is current" not in linked.stderr, linked.stderr
    assert not any(" push " in call for call in _shim_calls(nodiff_log))
    assert _remote_refs(gate_bare) == {"refs/heads/main": approved_head}
    assert (gate_root / INDEX).is_symlink(), (
        "the owned post-build step must really install the link")
    assert (gate_root / INDEX).read_bytes() == approved_index
    (gate_root / INDEX).unlink()
    (gate_root / INDEX).write_bytes(approved_index)
    assert _dirty_snapshot(gate_root) == gate_dirty

    # --- (E) one unambiguous destination, or a visible refusal ------------
    refuse_root = tmp_path / "refuse root"
    _plant_tracked(refuse_root)
    _init_repo(refuse_root, tracked=sorted(TRACKED_FILES))
    refuse_bare = _bare(tmp_path, "refuse bare.git")
    empty_bare = _bare(tmp_path, "empty bare.git")
    _configure_upstream(refuse_root, refuse_bare)
    _git(refuse_root, "push", "-q", str(refuse_bare), "HEAD:refs/heads/main")
    _write_roster(refuse_root, ROSTER)
    refuse_seed = _run(refuse_root, sys.executable, env, cwd, extra=("--push",))
    assert refuse_seed.returncode == 0, refuse_seed.stderr
    refuse_head = _head(refuse_root)
    refuse_shim, refuse_log = _git_shim(tmp_path, "refusal git")

    def refused(label, expected, stage="push/config"):
        proc = _run(refuse_root, sys.executable, _with_path(_env(), refuse_shim),
                    cwd, extra=("--push",))
        assert proc.returncode == 2, (label, proc.returncode, proc.stderr)
        _assert_not_success(proc, label)
        assert f"stage={stage}" in proc.stderr, (label, proc.stderr)
        assert expected in proc.stderr, (label, proc.stderr)
        assert "is current" not in proc.stderr, (label, proc.stderr)
        assert _remote_refs(refuse_bare) == {"refs/heads/main": refuse_head}, label
        assert _remote_refs(empty_bare) == {}, label

    _git(refuse_root, "config", "--unset", "branch.main.remote")
    refused("no upstream remote", "no configured upstream remote")
    _git(refuse_root, "config", "branch.main.remote", "origin")
    _git(refuse_root, "config", "--add", "branch.main.merge", "refs/heads/second")
    refused("two upstream branch refs", "configured more than once")
    _git(refuse_root, "config", "--unset-all", "branch.main.merge")
    _git(refuse_root, "config", "branch.main.merge", "refs/heads/main")
    _git(refuse_root, "config", "branch.main.remote", ".")
    refused("upstream is the local repository", "local repository")
    _git(refuse_root, "config", "branch.main.remote", "origin")
    _git(refuse_root, "remote", "set-url", "--push", "origin", str(empty_bare))
    _git(refuse_root, "remote", "set-url", "--push", "--add", "origin",
         str(refuse_bare))
    refused("two push URLs", "push URLs")
    _git(refuse_root, "config", "remote.origin.mirror", "true")
    refused("mirror remote", "mirror remote")
    _git(refuse_root, "config", "--unset", "remote.origin.mirror")
    # A single push URL again, now pointing at a destination with no branch.
    _git(refuse_root, "config", "--unset-all", "remote.origin.pushurl")
    _git(refuse_root, "remote", "set-url", "--push", "origin", str(empty_bare))
    refused("missing destination branch", "does not exist", stage="push/inspect")
    assert _head(refuse_root) == refuse_head
    assert not any(" push " in call for call in _shim_calls(refuse_log)), (
        _shim_calls(refuse_log))

    # --- (F) the ACTUAL push URL is the endpoint for discovery/read-back ---
    fetch_bare = _bare(tmp_path, "fetch endpoint.git")
    push_bare = _bare(tmp_path, "push endpoint.git")
    stranger = tmp_path / "stranger source"
    _plant_tracked(stranger)
    stranger_head = _init_repo(stranger, tracked=sorted(TRACKED_FILES))
    _git(stranger, "push", "-q", str(fetch_bare), "HEAD:refs/heads/main")
    assert _remote_refs(fetch_bare) == {"refs/heads/main": stranger_head}
    endpoint_root = tmp_path / "endpoint root"
    _plant_tracked(endpoint_root)
    _init_repo(endpoint_root, tracked=sorted(TRACKED_FILES))
    _git(endpoint_root, "push", "-q", str(push_bare), "HEAD:refs/heads/main")
    _git(endpoint_root, "remote", "add", "origin", str(fetch_bare))
    _git(endpoint_root, "remote", "set-url", "--push", "origin", str(push_bare))
    _git(endpoint_root, "config", "branch.main.remote", "origin")
    _git(endpoint_root, "config", "branch.main.merge", "refs/heads/main")
    assert _git(endpoint_root, "remote", "get-url", "--push", "--all",
                "origin").stdout.strip() == str(push_bare)
    assert _git(endpoint_root, "remote", "get-url", "--all",
                "origin").stdout.strip() == str(fetch_bare)
    _write_roster(endpoint_root, ROSTER)
    at_push_endpoint = _run(endpoint_root, sys.executable, env, cwd,
                            extra=("--push",))
    assert at_push_endpoint.returncode == 0, at_push_endpoint.stderr
    endpoint_candidate = _head(endpoint_root)
    assert _remote_refs(push_bare) == {"refs/heads/main": endpoint_candidate}
    assert _remote_refs(fetch_bare) == {"refs/heads/main": stranger_head}, (
        "the fetch URL must never be used as the publication endpoint")

    # --- (G) the history boundary fails closed ----------------------------
    # (G1) a divergent destination tip that IS a known local commit object.
    diverge_root = tmp_path / "diverge root"
    _plant_tracked(diverge_root)
    _init_repo(diverge_root, tracked=sorted(TRACKED_FILES))
    diverge_bare = _bare(tmp_path, "diverge bare.git")
    _configure_upstream(diverge_root, diverge_bare)
    _git(diverge_root, "push", "-q", str(diverge_bare), "HEAD:refs/heads/main")
    _write_roster(diverge_root, ROSTER)
    diverge_seed = _run(diverge_root, sys.executable, env, cwd, extra=("--push",))
    assert diverge_seed.returncode == 0, diverge_seed.stderr
    diverge_published = _head(diverge_root)
    other_work = tmp_path / "out of band source"
    _plant_tracked(other_work)
    _init_repo(other_work, tracked=sorted(TRACKED_FILES))
    (other_work / "out of band.txt").write_text("out of band\n", encoding="utf-8")
    _git(other_work, "add", "--", "out of band.txt")
    _git(other_work, "commit", "-q", "-m", "synthetic out-of-band commit")
    out_of_band = _head(other_work)
    _git(other_work, "push", "-q", "--force", str(diverge_bare),
         "HEAD:refs/heads/main")
    assert _remote_refs(diverge_bare) == {"refs/heads/main": out_of_band}
    # The operator already has that object locally (it is NOT a branch here).
    _git(diverge_root, "fetch", "-q", str(diverge_bare), "refs/heads/main")
    _make_dirty(diverge_root)
    diverge_dirty = _dirty_snapshot(diverge_root)
    _write_roster(diverge_root, ROSTER2)
    diverged = _run(diverge_root, sys.executable, env, cwd, extra=("--push",))
    assert diverged.returncode == 2, diverged.stderr
    _assert_not_success(diverged, "diverged destination")
    assert "stage=push/inspect" in diverged.stderr, diverged.stderr
    assert "not an ancestor" in diverged.stderr, diverged.stderr
    # The diagnostic names the CANDIDATE as behind (or the histories as
    # diverged), never the remote: the observed tip is the newer side here.
    assert ("the candidate is behind the destination or the histories "
            "have diverged") in diverged.stderr, diverged.stderr
    assert "the remote is behind" not in diverged.stderr, diverged.stderr
    assert _remote_refs(diverge_bare) == {"refs/heads/main": out_of_band}
    assert _dirty_snapshot(diverge_root) == diverge_dirty
    assert _parents(diverge_root) == [diverge_published]

    # (G2) an ahead range carrying an unrelated private path.
    foreign_root = tmp_path / "foreign root"
    _plant_tracked(foreign_root)
    _init_repo(foreign_root, tracked=sorted(TRACKED_FILES))
    foreign_bare = _bare(tmp_path, "foreign bare.git")
    _configure_upstream(foreign_root, foreign_bare)
    _git(foreign_root, "push", "-q", str(foreign_bare), "HEAD:refs/heads/main")
    foreign_baseline = _remote_head(foreign_bare)
    _write_roster(foreign_root, ROSTER)
    foreign_local = _run(foreign_root, sys.executable, env, cwd)
    assert foreign_local.returncode == 0, foreign_local.stderr
    (foreign_root / "private").mkdir()
    (foreign_root / "private" / "source.py").write_text(
        "PRIVATE = 'not for publication'\n", encoding="utf-8")
    _git(foreign_root, "add", "--", "private/source.py")
    _git(foreign_root, "commit", "-q", "-m", "synthetic private commit")
    private_commit = _head(foreign_root)
    assert _commit_paths(foreign_root) == ["private/source.py"]
    foreign = _run(foreign_root, sys.executable, env, cwd, extra=("--push",))
    assert foreign.returncode == 2, foreign.stderr
    _assert_not_success(foreign, "unrelated path in the unpublished range")
    assert "stage=push/history" in foreign.stderr, foreign.stderr
    assert "outside the public artifact pair" in foreign.stderr, foreign.stderr
    assert "private/source.py" in foreign.stderr, foreign.stderr
    assert _remote_refs(foreign_bare) == {"refs/heads/main": foreign_baseline}, (
        "a foreign unpushed commit must never be carried along")
    assert _head(foreign_root) == private_commit

    # (G3) an artifact-only range whose intermediate pair is invalid.
    middle_root = tmp_path / "middle root"
    _plant_tracked(middle_root)
    _init_repo(middle_root, tracked=sorted(TRACKED_FILES))
    middle_bare = _bare(tmp_path, "middle bare.git")
    _configure_upstream(middle_root, middle_bare)
    _git(middle_root, "push", "-q", str(middle_bare), "HEAD:refs/heads/main")
    middle_baseline = _remote_head(middle_bare)
    _write_roster(middle_root, ROSTER)
    middle_local = _run(middle_root, sys.executable, env, cwd)
    assert middle_local.returncode == 0, middle_local.stderr
    _, other_index = _build_pair(OTHER_ROSTER, tmp_path / "other pair root")
    (middle_root / INDEX).write_bytes(other_index)
    _git(middle_root, "add", "--", str(INDEX))
    _git(middle_root, "commit", "-q", "--only", "-m",
         "synthetic baseline: mismatching index", "--", str(INDEX))
    assert _commit_paths(middle_root) == [str(INDEX)]
    middle_restored = _run(middle_root, sys.executable, env, cwd)
    assert middle_restored.returncode == 0, middle_restored.stderr
    assert "committed locally" in middle_restored.stderr, middle_restored.stderr
    final_commit = _head(middle_root)
    invalid_range = _run(middle_root, sys.executable, env, cwd, extra=("--push",))
    assert invalid_range.returncode == 2, invalid_range.stderr
    _assert_not_success(invalid_range, "invalid intermediate pair")
    assert "stage=push/history" in invalid_range.stderr, invalid_range.stderr
    assert "does not hold a pair the real validator accepts" in invalid_range.stderr, (
        invalid_range.stderr)
    assert _remote_refs(middle_bare) == {"refs/heads/main": middle_baseline}
    assert _head(middle_root) == final_commit

    # --- (H) a zero-exit push and a stale read-back are both rejected ------
    lie_root = tmp_path / "lie root"
    _plant_tracked(lie_root)
    lie_baseline = _init_repo(lie_root, tracked=sorted(TRACKED_FILES))
    lie_bare = _bare(tmp_path, "lie bare.git")
    _configure_upstream(lie_root, lie_bare)
    _git(lie_root, "push", "-q", str(lie_bare), "HEAD:refs/heads/main")
    _write_roster(lie_root, ROSTER)
    lie_shim, lie_log = _git_shim(tmp_path, "lying push git", swallow="push")
    lying = _run(lie_root, sys.executable, _with_path(_env(), lie_shim), cwd,
                 extra=("--push",))
    assert lying.returncode == 2, lying.stderr
    _assert_not_success(lying, "lying push")
    assert "stage=push/verify" in lying.stderr, lying.stderr
    assert "may already have occurred" in lying.stderr, lying.stderr
    lie_candidate = _head(lie_root)
    assert _parents(lie_root) == [lie_baseline]
    assert _remote_refs(lie_bare) == {"refs/heads/main": lie_baseline}, (
        "a zero-exit push that pushed nothing must never be believed")
    assert any(" push " in call for call in _shim_calls(lie_log)), _shim_calls(lie_log)

    stale_root = tmp_path / "stale root"
    _plant_tracked(stale_root)
    stale_baseline = _init_repo(stale_root, tracked=sorted(TRACKED_FILES))
    stale_bare = _bare(tmp_path, "stale bare.git")
    _configure_upstream(stale_root, stale_bare)
    _git(stale_root, "push", "-q", str(stale_bare), "HEAD:refs/heads/main")
    _write_roster(stale_root, ROSTER)
    # The owned launcher performs a REAL push and then rewinds the destination:
    # a remote write really happened, and the read-back must say so.
    stale_shim, stale_log = _git_branch_shim(
        tmp_path, "rewinding push git", "push", [
            f'{real_git} "$@"',
            f"{real_git} -C {str(stale_bare)!r} update-ref refs/heads/main "
            f"{stale_baseline}",
            "exit 0",
        ])
    stale = _run(stale_root, sys.executable, _with_path(_env(), stale_shim), cwd,
                 extra=("--push",))
    assert stale.returncode == 2, stale.stderr
    _assert_not_success(stale, "stale read-back")
    assert "stage=push/verify" in stale.stderr, stale.stderr
    assert "reads back as" in stale.stderr, stale.stderr
    assert "may already have occurred" in stale.stderr, stale.stderr
    stale_candidate = _head(stale_root)
    assert stale_baseline in stale.stderr, stale.stderr
    assert stale_candidate in stale.stderr, stale.stderr
    assert _remote_refs(stale_bare) == {"refs/heads/main": stale_baseline}
    assert _head(stale_root) == stale_candidate, (
        "the local artifact commit is left for operator handling")

    # The push really lands but the endpoint can no longer be read: the run must
    # refuse to claim a publication it cannot see, and say that a remote write
    # may already have happened (which here it really did).
    unseen_root = tmp_path / "unseen root"
    _plant_tracked(unseen_root)
    _init_repo(unseen_root, tracked=sorted(TRACKED_FILES))
    unseen_bare = _bare(tmp_path, "unseen bare.git")
    _configure_upstream(unseen_root, unseen_bare)
    _git(unseen_root, "push", "-q", str(unseen_bare), "HEAD:refs/heads/main")
    _write_roster(unseen_root, ROSTER)
    pushed_marker = tmp_path / "unseen pushed"
    unseen_shim, unseen_log = _git_branch_shim(
        tmp_path, "unreadable readback git", None, [],
        extra_handlers={
            "push": [f'{real_git} "$@"', f": > {str(pushed_marker)!r}", "exit 0"],
            "ls-remote": [f"if [ -e {str(pushed_marker)!r} ]; then exit 1; fi"],
        })
    unseen = _run(unseen_root, sys.executable, _with_path(_env(), unseen_shim),
                  cwd, extra=("--push",))
    assert unseen.returncode == 2, unseen.stderr
    _assert_not_success(unseen, "unreadable read-back")
    assert "stage=push/verify" in unseen.stderr, unseen.stderr
    assert "cannot be read back" in unseen.stderr, unseen.stderr
    assert "may already have occurred" in unseen.stderr, unseen.stderr
    assert pushed_marker.exists(), "the owned launcher must really create the marker"
    assert any(" push " in call for call in _shim_calls(unseen_log)), (
        _shim_calls(unseen_log))
    unseen_candidate = _head(unseen_root)
    assert _remote_refs(unseen_bare) == {"refs/heads/main": unseen_candidate}, (
        "the push really landed: the honest diagnostic is the only alternative to "
        "a false success claim")
    assert _head(unseen_root) == unseen_candidate

    # --- (I) the lock covers publication AND the read-back ----------------
    conc_root = tmp_path / "concurrent root"
    _plant_tracked(conc_root)
    _init_repo(conc_root, tracked=sorted(TRACKED_FILES))
    conc_bare = _bare(tmp_path, "concurrent bare.git")
    _configure_upstream(conc_root, conc_bare)
    _git(conc_root, "push", "-q", str(conc_bare), "HEAD:refs/heads/main")
    _write_roster(conc_root, ROSTER)
    deeds = tmp_path / "publication deeds"
    deeds.mkdir()
    _receive_hook(conc_bare, "pre-receive", [
        f": > {str(deeds / 'push.started')!r}\n",
        "deadline=$((SECONDS + 30))\n",
        f"while [ ! -e {str(deeds / 'push.go')!r} ]; do\n",
        "    if [ \"$SECONDS\" -ge \"$deadline\" ]; then\n",
        "        echo 'synthetic receive hook timed out' >&2\n",
        "        exit 1\n",
        "    fi\n",
        "    sleep 0.01\n",
        "done\n",
        "exit 0\n",
    ])
    readback_shim, readback_log = _git_branch_shim(
        tmp_path, "blocking readback git", "ls-remote", [
            f"if [ -e {str(deeds / 'push.go')!r} ] && "
            f"[ ! -e {str(deeds / 'readback.done')!r} ]; then",
            f"    : > {str(deeds / 'readback.started')!r}",
            "    deadline=$((SECONDS + 30))",
            f"    while [ ! -e {str(deeds / 'readback.go')!r} ]; do",
            "        if [ \"$SECONDS\" -ge \"$deadline\" ]; then exit 1; fi",
            "        sleep 0.01",
            "    done",
            "fi",
        ])
    conc_lock = conc_root / PUBLISH_LOCK
    interpreter_a, log_a, env_a = _harness(tmp_path, mode="pass")
    env_a = _with_path(env_a, readback_shim)
    a = _Helper(conc_root, interpreter_a, env_a, cwd, "pushA", extra=("--push",))
    b = None
    try:
        _wait_path(deeds / "push.started")
        # The receive hook runs INSIDE the push: the publish lock is held there.
        assert _probe(conc_lock) is False, (
            "the publish lock was not held during publication")
        interpreter_b, log_b, env_b = _harness(tmp_path, mode="pass")
        b = _Helper(conc_root, interpreter_b, env_b, cwd, "pushB", extra=("--push",))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            assert b.poll() is None, b.output()
            assert not log_b.with_name(log_b.name + ".builder").exists(), (
                "the second helper entered its builder while the first was "
                "publishing")
            time.sleep(0.01)
        (deeds / "push.go").write_text("go", encoding="utf-8")
        _wait_path(deeds / "readback.started")
        assert _probe(conc_lock) is False, (
            "the publish lock was not held during the post-push read-back")
        (deeds / "readback.go").write_text("go", encoding="utf-8")
        (deeds / "readback.done").write_text("go", encoding="utf-8")
        release_stamp = time.time_ns()
        a.wait(timeout=WAIT_S)
        a_stdout, a_stderr = a.output()
        assert a.returncode == 0, a_stderr
        assert a_stdout == b"", a_stdout
        b.wait(timeout=WAIT_S)
        b_stdout, b_stderr = b.output()
        assert b.returncode == 0, b_stderr
        assert b_stdout == b"", b_stdout
        assert _stamps(log_b)["builder"] > release_stamp, (
            "the second builder started before the first publication released "
            "the lock")
        assert _remote_refs(conc_bare) == {"refs/heads/main": _head(conc_root)}
        assert "is current" in b_stderr, b_stderr
        assert any(" ls-remote " in call for call in _shim_calls(readback_log)), (
            _shim_calls(readback_log))
    finally:
        (deeds / "push.go").write_text("go", encoding="utf-8")
        (deeds / "readback.go").write_text("go", encoding="utf-8")
        (deeds / "readback.done").write_text("go", encoding="utf-8")
        for helper_proc in (a, b):
            if helper_proc is not None and helper_proc.poll() is None:
                helper_proc.reap(kill_group=True)
    assert _probe_until(conc_lock, True, timeout=10.0) is True, (
        "the publish lock was not released after publication")

    # --- (J) the SEND names the configured remote, so hooks see it --------
    # A pre-push hook receives the remote NAME as its first argument for a named
    # push, and the URL only when the send is a raw URL. A policy scoped to the
    # configured alias therefore has to keep working: this hook records both
    # arguments and the destination line, and rejects ONLY the alias.
    policy_root = tmp_path / "policy root"
    _plant_tracked(policy_root)
    _init_repo(policy_root, tracked=sorted(TRACKED_FILES))
    policy_bare = _bare(tmp_path, "policy bare.git")
    _configure_upstream(policy_root, policy_bare, remote="publish-alias")
    _git(policy_root, "push", "-q", str(policy_bare), "HEAD:refs/heads/main")
    policy_baseline = _remote_head(policy_bare)
    _write_roster(policy_root, ROSTER)
    policy_record = tmp_path / "pre-push record"
    policy_allow = tmp_path / "pre-push allows the alias"
    recorded = repr(str(policy_record))
    allowed_marker = repr(str(policy_allow))
    _install_hook(policy_root, "pre-push", [
        'printf "arg %s\\n" "$1" >> ' + recorded + "\n",
        'printf "arg %s\\n" "$2" >> ' + recorded + "\n",
        "while read -r local_ref local_oid remote_ref remote_oid; do\n",
        '    printf "ref %s %s %s %s\\n" "$local_ref" "$local_oid" '
        '"$remote_ref" "$remote_oid" >> ' + recorded + "\n",
        "done\n",
        'if [ "${1:-}" = "publish-alias" ] && [ ! -e ' + allowed_marker
        + " ]; then\n",
        '    echo "owned policy: named-remote publication is not allowed yet"'
        " >&2\n",
        "    exit 1\n",
        "fi\n",
        "exit 0\n",
    ])
    rejected_by_alias = _run(policy_root, sys.executable, env, cwd,
                            extra=("--push",))
    assert rejected_by_alias.returncode == 2, rejected_by_alias.stderr
    _assert_not_success(rejected_by_alias, "alias-scoped pre-push rejection")
    assert "publish complete" not in rejected_by_alias.stderr, (
        rejected_by_alias.stderr)
    assert "stage=push/send" in rejected_by_alias.stderr, rejected_by_alias.stderr
    assert "retained for a later retry" in rejected_by_alias.stderr, (
        rejected_by_alias.stderr)
    policy_candidate = _head(policy_root)
    assert policy_candidate != policy_baseline
    assert _parents(policy_root) == [policy_baseline]
    assert _commit_paths(policy_root) == list(ARTIFACTS)
    assert _remote_refs(policy_bare) == {"refs/heads/main": policy_baseline}, (
        "an alias-scoped hook rejection must leave the destination unchanged")
    # The hook really ran, really saw the CONFIGURED NAME as its first argument,
    # and really saw this run's destination line: an unrelated failure cannot
    # satisfy these assertions.
    observed = policy_record.read_text(encoding="utf-8").splitlines()
    assert observed[0] == "arg publish-alias", observed
    assert observed[1] == f"arg {policy_bare}", observed
    destination_lines = [line for line in observed if line.startswith("ref ")]
    assert destination_lines, observed
    assert any(line.split()[3] == "refs/heads/main"
               and line.split()[4] == policy_baseline
               for line in destination_lines), observed

    # Deliberately ALLOW the owned policy, then rerun the unchanged helper
    # input: a normal retry publishes the SAME candidate, with no new commit.
    policy_allow.write_text("allowed\n", encoding="utf-8")
    policy_commits = _commit_count(policy_root)
    record_before = len(observed)
    allowed = _run(policy_root, sys.executable, env, cwd, extra=("--push",))
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == ""
    assert f"pushed {policy_candidate}" in allowed.stderr, allowed.stderr
    assert "read that exact ref back" in allowed.stderr, allowed.stderr
    assert _commit_count(policy_root) == policy_commits, (
        "the retry must not create another local commit")
    assert _head(policy_root) == policy_candidate
    assert _remote_refs(policy_bare) == {"refs/heads/main": policy_candidate}
    assert _blob(policy_bare, policy_candidate, "site/catalog.json") == _blob(
        policy_root, policy_candidate, "site/catalog.json")
    assert _blob(policy_bare, policy_candidate, "site/index.html") == (
        policy_root / INDEX).read_bytes()
    retried_record = policy_record.read_text(encoding="utf-8").splitlines()
    assert retried_record[record_before] == "arg publish-alias", retried_record

    # --- (K) a remote-specific transport setting stays in force ------------
    # ``remote.<name>.receivepack`` is documented remote-scoped transport
    # policy. The owned wrapper only records its invocation and delegates to the
    # real git-receive-pack, so the push still lands and nothing is bypassed.
    transport_wrapper = tmp_path / "receive_pack_wrapper.sh"
    transport_record = tmp_path / "receive-pack record"
    transport_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'printf "receive-pack %s\\n" "$*" >> ' + repr(str(transport_record)) + "\n"
        'exec git-receive-pack "$@"\n',
        encoding="utf-8")
    transport_wrapper.chmod(0o755)

    control_root = tmp_path / "transport control root"
    _plant_tracked(control_root)
    _init_repo(control_root, tracked=sorted(TRACKED_FILES))
    control_bare = _bare(tmp_path, "transport control bare.git")
    _configure_upstream(control_root, control_bare, remote="policy-remote")
    _git(control_root, "push", "-q", str(control_bare), "HEAD:refs/heads/main")
    _git(control_root, "config", "remote.policy-remote.receivepack",
         str(transport_wrapper))
    (control_root / "control.txt").write_text("control\n", encoding="utf-8")
    _git(control_root, "add", "--", "control.txt")
    _git(control_root, "commit", "-q", "-m", "synthetic control commit")
    control_head = _head(control_root)
    # The POSITIVE CONTROL: an ordinary named-remote push uses the setting.
    _git(control_root, "push", "-q", "policy-remote", "HEAD:refs/heads/main")
    control_calls = [line for line in
                     transport_record.read_text(encoding="utf-8").splitlines()
                     if line.startswith("receive-pack ")]
    assert control_calls == [f"receive-pack {control_bare}"], control_calls
    assert _remote_head(control_bare) == control_head

    transport_root = tmp_path / "transport root"
    _plant_tracked(transport_root)
    _init_repo(transport_root, tracked=sorted(TRACKED_FILES))
    transport_bare = _bare(tmp_path, "transport bare.git")
    _configure_upstream(transport_root, transport_bare, remote="policy-remote")
    _git(transport_root, "push", "-q", str(transport_bare),
         "HEAD:refs/heads/main")
    _git(transport_root, "config", "remote.policy-remote.receivepack",
         str(transport_wrapper))
    _write_roster(transport_root, ROSTER)
    transport_calls_before = len(
        transport_record.read_text(encoding="utf-8").splitlines())
    transported = _run(transport_root, sys.executable, env, cwd,
                       extra=("--push",))
    assert transported.returncode == 0, transported.stderr
    assert transported.stdout == ""
    transport_candidate = _head(transport_root)
    assert _commit_paths(transport_root) == list(ARTIFACTS)
    assert _remote_refs(transport_bare) == {"refs/heads/main": transport_candidate}
    transport_calls = transport_record.read_text(
        encoding="utf-8").splitlines()[transport_calls_before:]
    assert transport_calls == [f"receive-pack {transport_bare}"], transport_calls
    assert f"pushed {transport_candidate}" in transported.stderr, (
        transported.stderr)

    # --- (L) URL-shaped invalid configuration: refused, never echoed -------
    # ``branch.<branch>.remote`` is operator configuration, but a misconfigured
    # value can be a credential-bearing URL rather than an alias. It must be
    # refused as a non-configured remote WITHOUT being echoed, normalised into a
    # usable endpoint or contacted -- and the same holds for a raw, unvalidated
    # URL-shaped destination ref.
    sentinel_user = "synthetic-user"
    sentinel_token = "synthetic-S3cr3t-5entinel"
    url_shaped_remote = (
        f"https://{sentinel_user}:{sentinel_token}"
        "@invalid.example.invalid/synthetic/repo.git")
    url_shaped_ref = (f"refs/heads//https://{sentinel_user}:{sentinel_token}"
                      "@invalid.example.invalid/synthetic/branch")
    leak_root = tmp_path / "leak root"
    _plant_tracked(leak_root)
    _init_repo(leak_root, tracked=sorted(TRACKED_FILES))
    leak_bare = _bare(tmp_path, "leak bare.git")
    _configure_upstream(leak_root, leak_bare)
    _git(leak_root, "push", "-q", str(leak_bare), "HEAD:refs/heads/main")
    _write_roster(leak_root, ROSTER)
    leak_seed = _run(leak_root, sys.executable, env, cwd, extra=("--push",))
    assert leak_seed.returncode == 0, leak_seed.stderr
    leak_head = _head(leak_root)
    assert _remote_refs(leak_bare) == {"refs/heads/main": leak_head}
    leak_shim, leak_log = _git_shim(tmp_path, "leaking config git")

    def refused_without_echo(label, expected):
        calls_before = len(_shim_calls(leak_log))
        proc = _run(leak_root, sys.executable, _with_path(_env(), leak_shim),
                    cwd, extra=("--push",))
        assert proc.returncode == 2, (label, proc.returncode, proc.stderr)
        assert proc.stdout == "", (label, proc.stdout)
        assert "publish complete" not in proc.stderr, (label, proc.stderr)
        assert "local gate passed" not in proc.stderr, (label, proc.stderr)
        assert "stage=push/config" in proc.stderr, (label, proc.stderr)
        assert expected in proc.stderr, (label, proc.stderr)
        for fragment in (url_shaped_remote, url_shaped_ref, sentinel_token,
                         sentinel_user):
            assert fragment not in proc.stderr, (label, fragment, proc.stderr)
        new_calls = _shim_calls(leak_log)[calls_before:]
        for call in new_calls:
            assert sentinel_token not in call, (label, new_calls)
            for verb in (" get-url ", " ls-remote ", " push "):
                assert verb not in call, (label, verb, new_calls)
        assert _remote_refs(leak_bare) == {"refs/heads/main": leak_head}, label

    _git(leak_root, "config", "branch.main.remote", url_shaped_remote)
    refused_without_echo("URL-shaped branch.main.remote",
                         "is not the name of a configured remote")
    _git(leak_root, "config", "branch.main.remote", "origin")
    _git(leak_root, "config", "branch.main.merge", url_shaped_ref)
    refused_without_echo("URL-shaped branch.main.merge",
                         "carries characters a single branch destination cannot")
    _git(leak_root, "config", "branch.main.merge", url_shaped_remote)
    refused_without_echo("raw URL-shaped destination ref",
                         "not a single branch under refs/heads/")
    _git(leak_root, "config", "branch.main.merge", "refs/heads/main")

    # Ordinary configured-alias publication still succeeds afterwards.
    _write_roster(leak_root, ROSTER2)
    healed = _run(leak_root, sys.executable, env, cwd, extra=("--push",))
    assert healed.returncode == 0, healed.stderr
    assert healed.stdout == ""
    healed_head = _head(leak_root)
    assert healed_head != leak_head
    assert _remote_refs(leak_bare) == {"refs/heads/main": healed_head}
    assert f"pushed {healed_head}" in healed.stderr, healed.stderr
