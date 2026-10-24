"""Units 9b-9c: publisher lock, validated-build gate and restricted local commit.

The helper is a LOCAL gate: it takes ONE dedicated publish lock, runs the real
``build_site.py`` against an explicit ``--root``, reads the EXACT final
``site/catalog.json`` + ``site/index.html`` bytes back and hands them to the
builder's own ``validate_artifact_pair``, and -- only after that pair is
accepted -- commits exactly those two allowlisted paths in the selected root's
own Git repository, inside the SAME lock lifetime. It pushes nothing, deploys
nothing and never touches the network: exit 0 means only that the local
artifact pair was validated and its local commit handled, never that GitHub or
the public site changed.

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
  unrelated tracked/staged/untracked work keeps its bytes, mode, blob and stage
  identity and never enters the commit, an unchanged pair commits nothing and
  leaves HEAD alone, a rejecting hook or a failing ``git add`` fails at exit 2
  without advancing HEAD, a failed build or read-back never invokes Git at all,
  and non-repository / nested-root / detached-HEAD targets are refused without
  touching an ancestor or this helper's own checkout;
* the publication lock is still held INSIDE a real commit hook: a second
  helper cannot enter its builder while the hook runs, and an independent
  nonblocking acquisition of the same inode only succeeds afterwards.

Fault injection and coordination live ONLY in this test module: the production
helper exposes no test switch, callback or fake-success mode. Artifact commits
are created only in owned temporary repositories; nothing here pushes, and no
remote exists.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "scripts" / "publish_site.sh"

SITE = Path("site")
CATALOG = SITE / "catalog.json"
INDEX = SITE / "index.html"
PUBLISH_LOCK = SITE / ".publish.lock"
BUILD_LOCK = SITE / ".build.lock"

# The helper's ONE fixed private success receipt. It is an internal handshake:
# it must never appear in an artifact or any public output field.
READBACK_RECEIPT = "publish_site: read-back validation passed"

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

# The ONLY paths the local commit may touch, in git's sorted order.
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
# its success receipt, so the child process still exits zero.
RECEIPT_FAULTS = ("receipt_missing", "receipt_wrong", "receipt_extra",
                  "receipt_stderr_only")

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
    record("validate" if args and args[0] == "-" else "builder")
    raise SystemExit(0)

if args and args[0] == "-":
    # The read-back validation invocation (``python - <root> <checkout>``).
    record("validate")
    if MODE == "hold_validate":
        hold()
    if MODE in RECEIPT_FAULTS:
        # The genuine validator runs unchanged against the genuine pair; only
        # its stdout receipt is then dropped or corrupted.
        proc = subprocess.run([REAL] + args, stdout=subprocess.PIPE)
        out = proc.stdout.decode("utf-8", "replace")
        mark("validator", f"{proc.returncode} {out!r}")
        if MODE == "receipt_missing":
            pass
        elif MODE == "receipt_wrong":
            sys.stdout.write("publish_site: validation skipped\\n")
        elif MODE == "receipt_extra":
            sys.stdout.write(out + "and one more line\\n")
        elif MODE == "receipt_stderr_only":
            sys.stderr.write(out)
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


def _run(root, interpreter, env, cwd):
    """Run the helper synchronously, capturing stdout and stderr separately."""
    return subprocess.run(
        [str(HELPER)] + _args(root, interpreter),
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

    def __init__(self, root, interpreter, env, cwd, tag):
        out_dir = Path(env["PUBLISH_TEST_LOG"]).parent
        self.stdout_path = out_dir / f"{tag}.stdout"
        self.stderr_path = out_dir / f"{tag}.stderr"
        self._handles = (self.stdout_path.open("wb"), self.stderr_path.open("wb"))
        self.proc = subprocess.Popen(
            [str(HELPER)] + _args(root, interpreter),
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


def _git_shim(tmp_path, name, fail_on=None, swallow=None):
    """Owned ``git`` launcher: records every invocation, can fail or fake one.

    ``fail_on`` makes the named subcommand fail; ``swallow`` makes it exit zero
    without doing anything at all (a safe-looking mutation that never happened).
    The real git still performs every other command.
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

    # The independent read-back gate really ran, and only after the builder.
    assert _kinds(log) == ["builder", "validate"], _kinds(log)

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
    stdout receipt. A removed, wrong, duplicated or misrouted receipt fails at
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
                 "receipt_stderr_only"):
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
                  "--only"):
        assert token in source, token
    # The allowlist is literal: no glob, no directory, no computed path.
    assert "readonly ARTIFACT_INDEX='site/index.html'" in source
    assert "readonly ARTIFACT_CATALOG='site/catalog.json'" in source
    assert "no-verify" not in source, "the helper must never bypass commit hooks"
    assert "git_root init" not in source, "the helper must never initialise a repository"
    assert not any("git_root" in line and "push" in line
                   for line in source.splitlines()), "no push in this unit"

    # --- this helper's own checkout was never a commit target -------------
    assert _head(REPO) == helper_source_head
