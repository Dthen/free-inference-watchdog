"""Selection-boundary gate for the explicit operator tick-wrapper opt-in (9a2).

FIXTURE EVIDENCE ONLY. Every wrapper this group reads is a synthetic script
written under the owned pytest ``tmp_path``. Nothing here copies, probes or
executes the host-installed ``~/.hermes/scripts/inference-watchdog-tick.sh``,
so a pass says nothing about deployed state. It says only that the selection
boundary behaves:

* no option            -> exactly two visible skips, actionable opt-in reason,
                          no read/probe/execute of anything installed,
* explicit valid path  -> the two ORIGINAL read-back checks run and pass,
* explicit absent path -> nonzero run with the intended path diagnostic, no
                          skip and no fallback to any HOME-shaped location,
* broken syntax         -> the real ``bash -n`` check rejects it,
* missing log/never-page-> the real publisher-stage assertion rejects it.

The group drives real bounded pytest subprocesses over EXACTLY the two existing
operator node IDs, so it never re-enters itself and never runs the watchdog.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

NODE_IDS = (
    "tests/test_build_site.py::test_wrapper_build_stage_redirects_stderr_to_log",
    "tests/test_build_site.py::test_wrapper_still_parses_bash_n",
)

OPTION = "--operator-tick-wrapper"

# The deployed wrapper's own site-stage contract, reproduced synthetically:
# ONE reviewed publisher call for this checkout (explicit `--root`), the
# explicit `--push` opt-in, both streams appended to the log, never paged, a
# failure absorbed by `|| true`, and the redirect-failure message contained.
PUBLISHER_LINE = (
    '    { bash scripts/publish_site.sh --root "$PWD" --push'
    ' >>state/site_build.log 2>&1; } 2>/dev/null || true\n'
)

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix" or sys.platform.startswith("win"),
    reason=(
        "POSIX-only gate fixture: the operator read-back checks are bash shell "
        "checks, so their selection boundary cannot be exercised on this platform"
    ),
)


def _wrapper_body(sentinel: Path, stage_line: str = PUBLISHER_LINE) -> str:
    """A shell-valid synthetic wrapper whose ``touch`` only fires if EXECUTED.

    ``bash -n`` parses it without running it, so a missing sentinel proves the
    read-back check never executed the script. Not copied from production.
    """
    return (
        "#!/usr/bin/env bash\n"
        "# Synthetic operator fixture; not the deployed watchdog wrapper.\n"
        "set -u\n"
        'PYTHON="${PYTHON:-python3}"\n'
        'ROOT="${ROOT:-$HOME/.hermes/inference-watchdog}"\n'
        f"touch {sentinel}\n"
        'cd "$ROOT" || exit 0\n'
        + stage_line
    )


def _owned_env(home: Path, tmpdir: Path) -> dict:
    """Minimal env: owned HOME/TMPDIR only, inherited pytest options dropped."""
    tmpdir.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }


def _run_nodes(python: str, home: Path, tmpdir: Path, *extra: str):
    return subprocess.run(
        [
            python,
            "-m",
            "pytest",
            "-v",
            "--tb=line",
            "-rf",
            "-rs",
            "-p",
            "no:cacheprovider",
            *extra,
            *NODE_IDS,
        ],
        cwd=str(REPO),
        env=_owned_env(home, tmpdir),
        capture_output=True,
        text=True,
        timeout=180,
    )


@POSIX_ONLY
def test_operator_opt_in_boundary(tmp_path):
    assert shutil.which("bash"), (
        "bash is required to exercise the operator read-back gate on POSIX; "
        "its absence is a real environment failure, not a skip"
    )
    python = sys.executable

    # 1. No option: two visible skips even with a deliberately invalid file
    #    sitting at the OLD implicit location. Presence there must not opt in,
    #    and neither check may read or execute it.
    home_none = tmp_path / "home-none"
    old_dir = home_none / ".hermes" / "scripts"
    old_dir.mkdir(parents=True)
    sentinel_none = tmp_path / "sentinel-none"
    old_wrapper = old_dir / "inference-watchdog-tick.sh"
    old_wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {sentinel_none}\n"
        "if this is deliberately ( invalid\n",
        encoding="utf-8",
    )
    before = old_wrapper.read_bytes()
    none_run = _run_nodes(python, home_none, tmp_path / "tmp-none")
    none_out = none_run.stdout + none_run.stderr
    assert none_run.returncode == 0, none_out
    assert re.search(r"\b2 skipped\b", none_run.stdout), none_out
    assert " passed" not in none_run.stdout, none_out
    assert OPTION in none_out, none_out
    assert not sentinel_none.exists(), "no-option run executed an installed script"
    assert old_wrapper.read_bytes() == before, "no-option run rewrote the script"
    assert [p for p in home_none.rglob("*") if p.is_file()] == [old_wrapper]

    # 2. Explicit valid owned wrapper: both ORIGINAL checks pass under real
    #    pytest/Bash, and the inert sentinel proves `bash -n` never executed it.
    home_valid = tmp_path / "home-valid"
    home_valid.mkdir()
    sentinel_valid = tmp_path / "sentinel-valid"
    valid_wrapper = tmp_path / "synthetic-valid-wrapper.sh"
    valid_wrapper.write_text(
        _wrapper_body(sentinel_valid), encoding="utf-8"
    )
    valid_run = _run_nodes(
        python, home_valid, tmp_path / "tmp-valid", f"{OPTION}={valid_wrapper}"
    )
    valid_out = valid_run.stdout + valid_run.stderr
    assert valid_run.returncode == 0, valid_out
    assert re.search(r"\b2 passed\b", valid_run.stdout), valid_out
    assert "skipped" not in valid_run.stdout, valid_out
    assert not sentinel_valid.exists(), "bash -n read-back executed the fixture"

    # 3. Explicit ABSENT path: nonzero with the intended diagnostic, no skip,
    #    and no rescue from a perfectly valid file at the old implicit location.
    home_absent = tmp_path / "home-absent"
    absent_old = home_absent / ".hermes" / "scripts" / "inference-watchdog-tick.sh"
    absent_old.parent.mkdir(parents=True)
    sentinel_absent = tmp_path / "sentinel-absent"
    absent_old.write_text(_wrapper_body(sentinel_absent), encoding="utf-8")
    absent = tmp_path / "absent-operator-wrapper.sh"
    absent_run = _run_nodes(
        python, home_absent, tmp_path / "tmp-absent", f"{OPTION}={absent}"
    )
    absent_out = absent_run.stdout + absent_run.stderr
    assert absent_run.returncode != 0, absent_out
    assert str(absent) in absent_out, absent_out
    assert OPTION in absent_out, absent_out
    assert re.search(r"\b2 (?:failed|errors)\b", absent_run.stdout), absent_out
    assert " passed" not in absent_run.stdout, absent_out
    assert "skipped" not in absent_run.stdout, absent_out
    assert not sentinel_absent.exists(), "absent-path run fell back to HOME"

    # 4. Syntactically invalid wrapper that KEEPS the valid publisher/log line:
    #    the real `bash -n` check must be the thing that rejects it.
    home_syntax = tmp_path / "home-syntax"
    home_syntax.mkdir()
    bad_syntax = tmp_path / "synthetic-invalid-syntax.sh"
    bad_syntax.write_text(
        "#!/usr/bin/env bash\n"
        "# Synthetic fixture: publisher stage is correct, the syntax is broken.\n"
        "set -u\n"
        'PYTHON="${PYTHON:-python3}"\n'
        'ROOT="${ROOT:-$HOME/.hermes/inference-watchdog}"\n'
        + PUBLISHER_LINE
        + "done\n",
        encoding="utf-8",
    )
    syntax_run = _run_nodes(
        python, home_syntax, tmp_path / "tmp-syntax", f"{OPTION}={bad_syntax}"
    )
    syntax_out = syntax_run.stdout + syntax_run.stderr
    assert syntax_run.returncode != 0, syntax_out
    assert re.search(r"\b1 failed\b", syntax_run.stdout), syntax_out
    assert re.search(r"\b1 passed\b", syntax_run.stdout), syntax_out
    assert re.search(
        r"::test_wrapper_still_parses_bash_n FAILED", syntax_out
    ), syntax_out
    assert re.search(
        r"::test_wrapper_build_stage_redirects_stderr_to_log PASSED", syntax_out
    ), syntax_out
    assert "syntax error" in syntax_out, syntax_out
    assert "build stage lost its log" not in syntax_out, syntax_out

    # 5. Shell-valid wrapper whose publisher stage keeps the delegation, the
    #    root and the never-page tail but drops the stderr log append: the real
    #    log assertion must reject it.
    home_log = tmp_path / "home-log"
    home_log.mkdir()
    no_log = tmp_path / "synthetic-no-log.sh"
    no_log.write_text(
        _wrapper_body(
            tmp_path / "sentinel-no-log",
            stage_line=(
                '    { bash scripts/publish_site.sh --root "$PWD" --push'
                ' >/dev/null 2>&1; } 2>/dev/null || true\n'
            ),
        ),
        encoding="utf-8",
    )
    log_run = _run_nodes(
        python, home_log, tmp_path / "tmp-log", f"{OPTION}={no_log}"
    )
    log_out = log_run.stdout + log_run.stderr
    assert log_run.returncode != 0, log_out
    assert re.search(r"\b1 failed\b", log_run.stdout), log_out
    assert re.search(r"\b1 passed\b", log_run.stdout), log_out
    assert re.search(
        r"::test_wrapper_build_stage_redirects_stderr_to_log FAILED", log_out
    ), log_out
    assert re.search(
        r"::test_wrapper_still_parses_bash_n PASSED", log_out
    ), log_out
    assert "build stage lost its log" in log_out, log_out
    assert "syntax error" not in log_out, log_out
