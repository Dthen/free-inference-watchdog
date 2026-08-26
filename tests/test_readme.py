"""Tests for README ops contracts that must not silently regress (F-R2-4).

Delivery topology (operator decision, 2026-08-26): the webhook in
~/.hermes/.env is the ONLY alert path; the cron job is silent (`--deliver
local`). The wrapper's job is diagnosability, not paging: exit 1 (routine
partial outage) stays fully silent; anything else prints to STDERR and exits
non-zero for the operator to find. The registration section is pinned to the
real Hermes cron CLI surface: `hermes cron register` does not exist and
`discord-home` is not a valid --deliver token — both invalid forms regressed
here once already, so their absence is pinned below.
"""

import re
import subprocess
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"


def _readme():
    return README.read_text(encoding="utf-8")


def _fenced_blocks(text):
    """Return the contents of every ``` fenced code block in the README."""
    return re.findall(r"```[a-z]*\n(.*?)```", text, re.S)


def test_cron_wrapper_exempts_routine_exit_1():
    """The wrapper must stay SILENT on bare exit 1 (routine partial outage —
    the stale line in the alert explains why) and route every other failure to
    stderr with a non-zero exit. Cron stdout must NEVER become a page channel."""
    text = _readme()
    assert "-eq 1 ] ||" in text, "README cron wrapper lost the exit-1 exemption"
    assert "c=$?" in text, "wrapper must capture the real exit code into c"
    assert '>&2' in text, "failure echoes must go to stderr, not stdout"
    # delivery topology stated plainly: webhook delivers, cron is local-only
    assert "--deliver local" in text, "cron registration must be silent (--deliver local)"
    assert "exit 1" in text and "routine" in text.lower()


def test_cron_wrapper_guards_cd_before_python():
    """Fix-round-4 #1: a failed `cd` must PAGE, never fall into the exit-1
    exemption — under bash a failed `cd` IS exit 1, so chaining both stages
    into one exemption clause went permanently silent the moment the project
    dir moved/renamed (exactly the dead-monitor-indistinguishable-from-silence
    scenario). The documented wrapper must keep a separate cd-guard stage that
    runs BEFORE the python stage."""
    blocks = _fenced_blocks(_readme())
    wrapper = next(b for b in blocks if "cannot cd" in b)
    lines = [l for l in wrapper.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    cd_idx = next(i for i, l in enumerate(lines) if "cannot cd" in l)
    py_idx = next(i for i, l in enumerate(lines) if "python3 inference_watchdog.py" in l)
    assert cd_idx < py_idx, "cd-guard must run BEFORE the python invocation"
    assert "exit 1" in lines[cd_idx], "cd-guard must page (exit non-zero), not fall through"


def test_readme_wrapper_block_is_wellformed_shell():
    """The documented wrapper block must parse as valid shell under bash -n —
    guards against quoting drift in the README example."""
    blocks = _fenced_blocks(_readme())
    wrapper = next(b for b in blocks if "cannot cd" in b)
    proc = subprocess.run(["bash", "-n"], input=wrapper, text=True, capture_output=True)
    assert proc.returncode == 0, f"wrapper block is not valid shell:\n{proc.stderr}"
    assert "inference_watchdog.py" in wrapper
    assert "FAILED" in wrapper


def test_no_invalid_cron_tokens_in_readme():
    """Regression pin: this build has no `hermes cron register` subcommand and
    no `discord-home` deliver token; neither string may reappear anywhere in
    the README (the old registration example used both)."""
    text = _readme()
    assert "cron register" not in text, \
        "invalid subcommand 'hermes cron register' back in README"
    assert "discord-home" not in text, \
        "invalid deliver token 'discord-home' back in README"


def test_documented_create_command_is_silent_script_mode():
    """The documented `hermes cron create` invocation must be script-mode
    (--script + --no-agent) and SILENT (--deliver local) — the webhook in
    ~/.hermes/.env is the only alert path, never cron delivery."""
    blocks = _fenced_blocks(_readme())
    cmd_block = next(b for b in blocks if "hermes cron create" in b)
    joined = " ".join(cmd_block.split())
    assert "--script" in joined, "create command must use script mode (--script)"
    assert "--no-agent" in joined, "create command must skip the LLM (--no-agent)"
    assert "--deliver local" in joined, "cron job must be silent (--deliver local)"


def test_readme_init_documents_bak_overwrite():
    """Fix-round-5 #5 (cosmetic): the --init section must state that each
    successful init OVERWRITES a prior roster.json.bak — the archive-site code
    comment says '(documented in README)', so the README must actually say it."""
    text = _readme()
    assert "roster.json.bak" in text, "--init section lost the .bak mention"
    assert "overwrit" in text.lower(), ".bak overwrite behavior not documented"
