"""Tests for README ops contracts that must not silently regress (F-R2-4).

The cron fail-wrapper is the operator's only page on a dead monitor; the
exit-1 exemption is part of that contract, so it's pinned here.
"""

import shlex
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def _unescaped(text):
    """Undo the escaping the README applies for its outer --command quoting."""
    return text.replace("\\$", "$").replace('\\"', '"')


def test_cron_wrapper_exempts_routine_exit_1():
    """F-R2-4: the mandated cron wrapper must stay SILENT on bare exit 1
    (routine partial outage — the stale line in the alert explains why) and
    still page on every other non-zero exit."""
    text = _unescaped(README.read_text(encoding="utf-8"))
    assert "[ $c -eq 1 ] || echo" in text, "README cron wrapper lost the exit-1 exemption"
    assert "c=$?" in text, "wrapper must capture the real exit code into c"
    # the explanatory sentence exists and names both halves of the contract
    assert "exit 1" in text and "routine" in text.lower()


def test_cron_wrapper_guards_cd_before_python():
    """Fix-round-4 #1: a failed `cd` must PAGE, never fall into the exit-1
    exemption — under bash a failed `cd` IS exit 1, so the old
    `cd ... && python3 ... || {c=$?; [ $c -eq 1 ] || ...}` chain went
    permanently silent the moment the project dir moved/renamed (exactly the
    dead-monitor-indistinguishable-from-silence scenario). The wrapper must
    split into a separate cd-guard followed by the python stage."""
    text = _unescaped(README.read_text(encoding="utf-8"))
    assert ('cd /home/kimbo/projects/free-inference-monitor || '
            '{ echo "inference-monitor FAILED (cannot cd)"; exit 1; }') in text, \
        "README cron wrapper lost the separate cannot-cd guard"
    # On the wrapper's own command line the cd-guard must precede the python
    # stage (a bare-text index would false-trip on the Quick-start mention).
    line = next(l for l in _unescaped(README.read_text(encoding="utf-8"))
                .splitlines() if "[ $c -eq 1 ]" in l)
    assert line.index("cannot cd") < line.index("python3 inference_monitor.py"), \
        "cd-guard must run BEFORE the python invocation"


def test_readme_wrapper_block_is_wellformed_shell():
    """The documented --command value (unescaped) must parse as valid shell —
    guards against quoting drift in the README example."""
    lines = [_unescaped(l) for l in
             README.read_text(encoding="utf-8").splitlines()]
    line = next(l for l in lines if "[ $c -eq 1 ]" in l)
    cmd = line.strip().rstrip("\\").strip()
    cmd = cmd.split("--command ", 1)[1].strip().strip('"')
    tokens = shlex.split(cmd)          # raises ValueError if not valid shell
    assert any("inference_monitor.py" in t for t in tokens)
    assert any("FAILED" in t for t in tokens)


def test_readme_init_documents_bak_overwrite():
    """Fix-round-5 #5 (cosmetic): the --init section must state that each
    successful init OVERWRITES a prior roster.json.bak — the archive-site code
    comment says '(documented in README)', so the README must actually say it."""
    text = README.read_text(encoding="utf-8")
    assert "roster.json.bak" in text, "--init section lost the .bak mention"
    assert "overwrit" in text.lower(), ".bak overwrite behavior not documented"
