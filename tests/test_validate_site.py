"""Unit 9e: the read-only Pages artifact validator and its deployment gate.

Two focused groups, both stdlib-only and fully synthetic:

* the CLI group runs the REAL ``scripts/validate_site.py`` as an isolated
  ``-I -S`` child from an unrelated cwd (with spaces and non-ASCII characters
  in every owned path) against one coherent pair and then against every
  rejected artifact shape. It pins exit 0/2, empty stdout, a bounded
  non-reflective stderr diagnostic, byte-for-byte artifact and sentinel
  preservation, the absence of bytecode/companion writes, and -- through a
  fresh-process audit hook around the SUCCESSFUL path -- that the exercised
  route imports only this checkout's pure validator and opens exactly the two
  named artifacts, with no socket, provider, config/state, renderer, builder
  or SDK involvement. A cwd/site import-shadow sentinel proves the supplied
  directory never becomes the validator.
* the workflow group reads the ACTUAL checked-in ``deploy-dashboard.yml`` with a
  minimal indentation extractor (not a YAML implementation), asserts the
  fail-closed wiring -- managed CPython 3.11 bound by value, one unconditional
  validator step invoking the real CLI against the SAME uploaded directory,
  strictly before the preserved upload/deploy actions, with full trigger paths
  and no bypass -- and then EXECUTES the extracted validation command under
  bounded local Bash against a good pair (an upload/deploy sentinel is
  reached) and a broken pair (it is not).

Both groups end with metadata-free negative controls: a scratch CLI copy whose
real pair validation is bypassed must make this file's own rejection helper
FAIL, and a scratch workflow copy with the gate weakened or removed must make
this file's own wiring/sentinel checks FAIL. Those controls live only in
owned temporary directories; the tracked source, the tests and their
expectations are untouched.

Nothing here publishes, deploys, installs, fetches or reads private state: the
catalogue input is the accepted synthetic golden fixture
``tests/fixtures/catalog/basic.json`` and the page is an explicitly synthetic
whole document carrying the same JSON island. ``validate_artifact_pair`` (the
real shared validator) is called directly to prove each accepted fixture is a
coherent pair.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISFIFO

import pytest

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "validate_site.py"
WORKFLOW = REPO / ".github" / "workflows" / "deploy-dashboard.yml"
TEST_WORKFLOW = REPO / ".github" / "workflows" / "test.yml"
FIXTURES = REPO / "tests" / "fixtures" / "catalog"

CHILD_TIMEOUT_S = 120
DIAGNOSTIC_LIMIT = 300

#: Modules that must never enter the validator route (the guarded MCP SDK, the
#: producer/configuration/state/monitor stack, the renderer/builder and every
#: catalogue module outside the pure raw/structural core).
FORBIDDEN_IMPORTS = [
    "mcp", "mcp_server", "pydantic", "anyio", "build_site", "state",
    "config_loader", "providers", "inference_watchdog", "notify", "envfile",
    "diffing", "detection", "alive", "pending_removals", "probe_select",
    "probe_state", "probe_zero_credit", "anthropic", "httpx", "requests",
    "watchdog_catalog.query", "watchdog_catalog.server",
    "watchdog_catalog.adapter", "watchdog_catalog.source",
]

#: A fixed token planted only in a sabotaged artifact: it must never be echoed.
BROKEN_MARKER = "validate-site-tampered-marker"


# ---------------------------------------------------------------------------
# synthetic artifacts (accepted golden catalogue + an explicitly synthetic page)
# ---------------------------------------------------------------------------


def _catalog_text(name="basic.json"):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _island(catalog_text):
    return ('<script type="application/json" id="catalog-data">\n'
            + catalog_text + "</script>")


def _page(catalog_text):
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head><meta charset=\"utf-8\">\n"
            "<title>synthetic standalone dashboard fixture</title>\n</head>\n"
            "<body>\n<main>synthetic fixture</main>\n"
            + _island(catalog_text) + "\n</body>\n</html>\n")


def _mutated_catalog(mutate):
    document = json.loads(_catalog_text())
    mutate(document)
    return json.dumps(document)


def _make_site(site, catalog_text=None, html_text=None):
    site.mkdir(parents=True, exist_ok=True)
    (site / "catalog.json").write_bytes(
        (catalog_text if catalog_text is not None else _catalog_text())
        .encode("utf-8"))
    (site / "index.html").write_bytes(
        (html_text if html_text is not None else _page(_catalog_text()))
        .encode("utf-8"))
    return site


def _copy_checkout(destination):
    """A private copy of the CLI plus ONLY the pure validator modules it needs.

    ``watchdog_catalog/``'s transport modules need the SDK and are copied too
    only so the layout matches a real checkout; the audit below proves they are
    never imported.
    """
    (destination / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(CLI, destination / "scripts" / "validate_site.py")
    shutil.copy2(REPO / "catalog_export.py", destination / "catalog_export.py")
    shutil.copytree(REPO / "watchdog_catalog", destination / "watchdog_catalog",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return destination


def _child_env(home, tmpdir):
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
    }


def _run_cli(script, site_dir, *, cwd, home, tmpdir, extra=()):
    args = [sys.executable, "-I", "-S", str(script)]
    if site_dir is not None:
        args.extend(["--site-dir", str(site_dir)])
    args.extend(extra)
    return subprocess.run(
        args, cwd=str(cwd), env=_child_env(home, tmpdir),
        capture_output=True, text=True, timeout=CHILD_TIMEOUT_S)


def _tree(root):
    """Layout digest: kind, mode and content hash, never following links."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            relative = str(path.relative_to(root))
            info = path.lstat()
            if path.is_symlink():
                entries[relative] = ("link", os.readlink(path))
            elif S_ISDIR(info.st_mode):
                entries[relative] = ("dir",)
            elif S_ISFIFO(info.st_mode):
                entries[relative] = ("fifo", S_IMODE(info.st_mode))
            else:
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    entries[relative] = ("file", S_IMODE(info.st_mode), digest)
                except OSError:
                    entries[relative] = ("unreadable", S_IMODE(info.st_mode))
    return entries


def _assert_accepted(proc):
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "a successful validation must stay stdout-silent"
    assert "Traceback" not in proc.stderr
    assert proc.stderr.strip(), "a concise stderr diagnostic is expected"
    assert len(proc.stderr) <= DIAGNOSTIC_LIMIT


def _assert_rejected(proc, *, path_echo=(), content_echo=()):
    """The fixed failure contract: exit 2, silent stdout, bounded diagnostic."""
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stdout == "", "a rejected pair must stay stdout-silent"
    assert "Traceback" not in proc.stderr, proc.stderr
    diagnostic = proc.stderr.strip()
    assert diagnostic, "a rejection must explain itself on stderr"
    assert len(proc.stderr) <= DIAGNOSTIC_LIMIT, proc.stderr
    assert proc.stderr.count("\n") <= 3, proc.stderr
    for token in path_echo:
        assert str(token) not in proc.stderr, proc.stderr
    for token in content_echo:
        assert token not in proc.stderr, proc.stderr


#: Fresh-process audit guard: install the hook, then execute the REAL CLI through
#: ``runpy`` so the successful validation route itself is audited.
_AUDIT_WRAPPER = r'''
import json
import os
import runpy
import sys

report_path = os.environ["VALIDATE_AUDIT_REPORT"]
events = {"imports": [], "opens": [], "sockets": []}


def _audit(event, args):
    if event == "import":
        events["imports"].append(args[0])
    elif event == "open":
        target = args[0]
        if isinstance(target, (str, bytes)):
            events["opens"].append(os.fsdecode(target))
    elif event.startswith("socket."):
        events["sockets"].append(event)


sys.addaudithook(_audit)
cli = sys.argv[1]
sys.argv = [cli] + sys.argv[2:]
code = 0
try:
    runpy.run_path(cli, run_name="__main__")
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else 1
report = {
    "exit": code,
    "imports": list(events["imports"]),
    "opens": list(events["opens"]),
    "sockets": list(events["sockets"]),
    "modules": sorted(sys.modules),
    "catalog_export_file": getattr(
        sys.modules.get("catalog_export"), "__file__", None),
}
with open(report_path, "w", encoding="utf-8") as handle:
    json.dump(report, handle)
sys.exit(code)
'''


def _audit_cli(checkout, site_dir, *, cwd, home, tmpdir, report):
    wrapper = checkout / "audit_wrapper.py"
    wrapper.write_text(_AUDIT_WRAPPER, encoding="utf-8")
    env = _child_env(home, tmpdir)
    env["VALIDATE_AUDIT_REPORT"] = str(report)
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(wrapper),
         str(checkout / "scripts" / "validate_site.py"),
         "--site-dir", str(site_dir)],
        cwd=str(cwd), env=env, capture_output=True, text=True,
        timeout=CHILD_TIMEOUT_S)
    return proc, json.loads(report.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# group 1: the read-only standalone CLI
# ---------------------------------------------------------------------------


def test_cli_validates_one_pair_read_only_and_fails_closed(tmp_path):
    from catalog_export import validate_artifact_pair

    assert sys.implementation.name == "cpython"
    assert CLI.is_file()

    checkout = _copy_checkout(tmp_path / "own checkout é")
    script = checkout / "scripts" / "validate_site.py"
    home = tmp_path / "home"
    home.mkdir()
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    neutral = tmp_path / "neutral cwd é"
    neutral.mkdir()

    # -- (a) one coherent pair is accepted, from an unrelated cwd -----------
    accepted = _make_site(tmp_path / "accepted site é")
    (accepted / "unrelated-sentinel.txt").write_text("untouched\n")
    (checkout / "scripts" / "unrelated-sentinel.txt").write_text("untouched\n")
    (neutral / "unrelated-sentinel.txt").write_text("untouched\n")

    # An import shadow next to the page and in the cwd: neither may be loaded.
    shadow_marker = tmp_path / "shadow-imported.txt"
    shadow = ("import pathlib\n"
              "pathlib.Path(%r).write_text('shadow imported\\n')\n"
              % str(shadow_marker))
    (accepted / "catalog_export.py").write_text(shadow, encoding="utf-8")
    (accepted / "watchdog_catalog.py").write_text(shadow, encoding="utf-8")
    (neutral / "catalog_export.py").write_text(shadow, encoding="utf-8")
    (neutral / "watchdog_catalog.py").write_text(shadow, encoding="utf-8")

    watched = {
        "site": accepted,
        "checkout": checkout,
        "neutral": neutral,
        "home": home,
    }
    before = {name: _tree(root) for name, root in watched.items()}
    catalog_before = (accepted / "catalog.json").read_bytes()
    html_before = (accepted / "index.html").read_bytes()

    # The fixture itself is a REAL coherent pair, checked by the shared
    # validator the CLI delegates to.
    published = validate_artifact_pair(
        catalog_before.decode("utf-8"), html_before.decode("utf-8"))
    assert published["snapshot_id"] == json.loads(
        catalog_before.decode("utf-8"))["snapshot_id"]

    _assert_accepted(_run_cli(script, accepted, cwd=neutral, home=home,
                              tmpdir=tmpdir))
    assert (accepted / "catalog.json").read_bytes() == catalog_before
    assert (accepted / "index.html").read_bytes() == html_before
    assert not shadow_marker.exists(), "a shadow module became the validator"
    for name, root in watched.items():
        assert _tree(root) == before[name], (name, "changed by a successful run")
    assert not list(checkout.rglob("__pycache__")), "bytecode was written"
    assert not list(checkout.rglob("*.pyc"))
    assert _tree(home) == {}, "the validator touched HOME"

    # -- (b) fresh-process audit of the SUCCESSFUL route -------------------
    audit_report = tmp_path / "audit.json"
    audit_proc, audit = _audit_cli(checkout, accepted, cwd=neutral, home=home,
                                   tmpdir=tmpdir, report=audit_report)
    assert audit_proc.returncode == 0, audit_proc.stderr
    assert audit["exit"] == 0
    assert audit["sockets"] == []
    leaked = [name for name in audit["modules"]
              if any(name == prefix or name.startswith(prefix + ".")
                     for prefix in FORBIDDEN_IMPORTS)]
    assert leaked == []
    for imported in audit["imports"]:
        assert not any(imported == prefix or imported.startswith(prefix + ".")
                       for prefix in FORBIDDEN_IMPORTS), imported
    # Positive control: the audited child really did run the pure validator.
    assert "catalog_export" in audit["imports"]
    assert "watchdog_catalog.catalog" in audit["imports"]
    assert os.path.realpath(audit["catalog_export_file"]).startswith(
        os.path.realpath(checkout) + os.sep), \
        "the validator was imported from outside its own checkout"
    site_opens = [path for path in audit["opens"]
                  if os.path.realpath(path).startswith(
                      os.path.realpath(accepted) + os.sep)]
    assert sorted(os.path.realpath(path) for path in site_opens) == sorted([
        os.path.realpath(accepted / "catalog.json"),
        os.path.realpath(accepted / "index.html"),
    ]), site_opens

    # The same audit over a REJECTED pair: still no provider/SDK/network route.
    broken_site = _make_site(tmp_path / "broken site é", html_text=(
        "<!DOCTYPE html><html><body><p>no island</p></body></html>\n"))
    audit_proc, audit = _audit_cli(checkout, broken_site, cwd=neutral, home=home,
                                   tmpdir=tmpdir, report=audit_report)
    assert audit_proc.returncode == 2, audit_proc.stderr
    assert audit["exit"] == 2 and audit["sockets"] == []
    assert [name for name in audit["modules"]
            if any(name == prefix or name.startswith(prefix + ".")
                   for prefix in FORBIDDEN_IMPORTS)] == []

    # -- (c) conventional help and explicit argument errors ----------------
    help_before = {name: _tree(root) for name, root in watched.items()}
    help_proc = _run_cli(script, None, cwd=neutral, home=home, tmpdir=tmpdir,
                        extra=["--help"])
    assert help_proc.returncode == 0
    assert "--site-dir" in help_proc.stdout
    assert help_proc.stderr == ""
    for name, root in watched.items():
        assert _tree(root) == help_before[name], (name, "help had side effects")

    for label, extra in (
            ("missing --site-dir", [str(script)]),
            ("abbreviated long option", [str(script), "--site", str(accepted)]),
            ("unknown option",
             [str(script), "--site-dir", str(accepted), "--nope"]),
            ("absent site directory",
             [str(script), "--site-dir", str(tmp_path / "absent site")]),
    ):
        proc = subprocess.run(
            [sys.executable, "-I", "-S"] + extra, cwd=str(neutral),
            env=_child_env(home, tmpdir), capture_output=True, text=True,
            timeout=CHILD_TIMEOUT_S)
        assert proc.returncode == 2, (label, proc.returncode, proc.stderr)
        assert proc.stdout == "", label
        assert proc.stderr.strip(), label
        assert "Traceback" not in proc.stderr, label
    a_file = tmp_path / "not-a-directory"
    a_file.write_text("not a site\n", encoding="utf-8")
    _assert_rejected(_run_cli(script, a_file, cwd=neutral, home=home,
                              tmpdir=tmpdir), path_echo=[tmp_path])

    # -- (d) every rejected artifact shape fails closed, without writes ----
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "catalog.json").write_bytes(_catalog_text().encode("utf-8"))

    def _symlinked(site):
        (site / "catalog.json").unlink()
        (site / "catalog.json").symlink_to(outside / "catalog.json")

    def _directory(site):
        (site / "index.html").unlink()
        (site / "index.html").mkdir()

    def _fifo(site):
        (site / "catalog.json").unlink()
        os.mkfifo(site / "catalog.json")

    def _unreadable(site):
        os.chmod(site / "catalog.json", 0o000)

    def _missing_catalog(site):
        (site / "catalog.json").unlink()

    def _missing_html(site):
        (site / "index.html").unlink()

    tampered = _mutated_catalog(
        lambda document: document["providers"]["synthetic-a"].update(
            {"display_name": BROKEN_MARKER}))
    malformed = _catalog_text()
    unsupported = _mutated_catalog(
        lambda document: document.update({"schema_version": 2}))

    cases = [
        ("malformed JSON", {"catalog_text": malformed[: len(malformed) // 2]}, None),
        ("unsupported schema version", {"catalog_text": unsupported,
                                        "html_text": _page(unsupported)}, None),
        ("malformed island",
         {"html_text": _page(_catalog_text()).replace("</script>", "", 1)}, None),
        ("absent island",
         {"html_text": "<!DOCTYPE html><html><body><p>none</p></body></html>\n"},
         None),
        ("duplicate island",
         {"html_text": "<!DOCTYPE html><html><body>" + _island(_catalog_text())
          + _island(_catalog_text()) + "</body></html>\n"}, None),
        ("valid but different snapshot",
         {"html_text": _page(_catalog_text("ordering.json"))}, None),
        ("same-id tampered embedded data", {"html_text": _page(tampered)}, None),
        ("invalid UTF-8 catalogue", {"catalog_bytes": b'{"\xff\xfe": 1}'}, None),
        ("invalid UTF-8 page",
         {"html_bytes": _page(_catalog_text()).encode("utf-8") + b"\xff"}, None),
        ("missing catalogue", {}, _missing_catalog),
        ("missing page", {}, _missing_html),
        ("symlinked catalogue", {}, _symlinked),
        ("directory page", {}, _directory),
    ]
    if hasattr(os, "mkfifo"):
        cases.append(("FIFO catalogue", {}, _fifo))
    if os.geteuid() != 0:
        cases.append(("unreadable catalogue", {}, _unreadable))

    for index, (label, overrides, mutate) in enumerate(cases):
        site = tmp_path / ("case %02d é" % index)
        site.mkdir()
        (site / "catalog.json").write_bytes(
            overrides.get("catalog_bytes",
                          overrides.get("catalog_text", _catalog_text())
                          .encode("utf-8")))
        (site / "index.html").write_bytes(
            overrides.get("html_bytes",
                          overrides.get("html_text", _page(_catalog_text()))
                          .encode("utf-8")))
        (site / "unrelated-sentinel.txt").write_text("untouched\n", encoding="utf-8")
        if mutate is not None:
            mutate(site)
        case_before = {name: _tree(root) for name, root in watched.items()}
        case_before["case"] = _tree(site)
        proc = _run_cli(script, site, cwd=neutral, home=home, tmpdir=tmpdir)
        _assert_rejected(proc, path_echo=[tmp_path], content_echo=[BROKEN_MARKER])
        assert _tree(site) == case_before["case"], (label, "artifacts changed")
        for name, root in watched.items():
            assert _tree(root) == case_before[name], (label, name, "wrote outside")
        assert (site / "unrelated-sentinel.txt").read_text() == "untouched\n"

    # -- (e) negative control: bypass the REAL pair validation ------------
    control = _copy_checkout(tmp_path / "control checkout é")
    control_script = control / "scripts" / "validate_site.py"
    control_text = control_script.read_text(encoding="utf-8")
    assert "validate_artifact_pair(" in control_text
    control_script.write_text(
        control_text.replace("validate_artifact_pair(", "_bypassed_pair(")
        .replace('if __name__ == "__main__":',
                 "def _bypassed_pair(*args, **kwargs):\n"
                 "    return {\"snapshot_id\": \"control\"}\n\n\n"
                 'if __name__ == "__main__":', 1),
        encoding="utf-8")
    control_proc = _run_cli(control_script, broken_site, cwd=neutral, home=home,
                            tmpdir=tmpdir)
    assert "Traceback" not in control_proc.stderr, control_proc.stderr
    assert control_proc.returncode == 0, "the control did not bypass validation"
    with pytest.raises(AssertionError):
        _assert_rejected(control_proc)
    assert shadow_marker.exists() is False


# ---------------------------------------------------------------------------
# group 2: the Pages workflow gate
# ---------------------------------------------------------------------------


def _workflow_steps(text):
    """The job's step blocks, split by indentation only (no YAML library)."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line == "    steps:":
            start = index + 1
            break
    assert start is not None, "the workflow has no job-level steps: block"
    steps = []
    current = None
    for line in lines[start:]:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if stripped and indent <= 4:
            break
        if indent == 6 and stripped.startswith("- "):
            if current is not None:
                steps.append("\n".join(current))
            current = [stripped[2:]]
        elif current is not None:
            current.append(line)
    if current is not None:
        steps.append("\n".join(current))
    return steps


def _step_key(step, key):
    lines = step.splitlines()
    for index, line in enumerate(lines):
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent not in (0, 8) or not stripped.startswith(key + ":"):
            continue
        value = stripped[len(key) + 1:].strip()
        if value[:1] in ("|", ">"):
            block = []
            for follow in lines[index + 1:]:
                if not follow.strip():
                    block.append("")
                    continue
                findent = len(follow) - len(follow.lstrip(" "))
                if findent <= indent:
                    break
                block.append(follow[indent + 2:]
                             if findent >= indent + 2 else follow.strip())
            return "\n".join(block).strip("\n")
        return value
    return None


def _step_mapping(step, key):
    values = {}
    inside = False
    for line in step.splitlines():
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent in (0, 8) and stripped == key + ":":
            inside = True
            continue
        if not inside:
            continue
        if not stripped:
            continue
        if indent != 10:
            inside = False
            continue
        name, _, value = stripped.partition(":")
        values[name.strip()] = value.strip()
    return values


def _pin(uses):
    return (uses or "").split()[0] if uses else ""


def _resolved_site_dir(run_text):
    """Resolve the shell word after ``--site-dir`` through simple assignments."""
    assignments = dict(re.findall(
        r"(?m)^([A-Za-z_][A-Za-z0-9_]*)=['\"]?([^'\"\n]+?)['\"]?$", run_text))
    match = re.search(r"--site-dir\s+(\"[^\"]*\"|'[^']*'|\S+)", run_text)
    assert match is not None, "the validation step does not pass --site-dir"
    argument = match.group(1).strip("\"'")
    return assignments.get(argument.lstrip("$"), argument)


def _trigger_paths(text):
    block = text.split("    paths:\n", 1)[1]
    paths = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("- "):
            break
        paths.append(stripped[2:].strip().strip("'\""))
    return paths


def _assert_workflow_gate(text):
    """The fail-closed wiring contract, asserted structurally (no YAML library)."""
    steps = _workflow_steps(text)
    uses = [_pin(_step_key(step, "uses")) for step in steps]
    runs = [_step_key(step, "run") or "" for step in steps]

    upload = [index for index, use in enumerate(uses)
              if use.startswith("actions/upload-pages-artifact@")]
    deploy = [index for index, use in enumerate(uses)
              if use.startswith("actions/deploy-pages@")]
    assert len(upload) == 1, "exactly one artifact upload"
    assert len(deploy) == 1, "exactly one Pages deployment"
    assert uses[upload[0]] == "actions/upload-pages-artifact@v3"
    assert uses[deploy[0]] == "actions/deploy-pages@v4"

    validators = [index for index, run in enumerate(runs)
                  if "scripts/validate_site.py" in run]
    assert len(validators) == 1, "exactly one validator step"
    validate_index = validators[0]
    assert validate_index < upload[0] < deploy[0], "validation must precede upload"

    validate_step = steps[validate_index]
    run = runs[validate_index]
    assert re.search(r"(?m)^set -euo pipefail$", run), "not fail-closed"
    assert re.search(r"-I\s+-S\s+\S*scripts/validate_site\.py", run), \
        "the real CLI is not invoked with an isolated interpreter"
    for swallowed in ("|| true", "|| echo", "|| :", "set +e"):
        assert swallowed not in run, swallowed
    assert re.search(r"(?m)^\s*if\s", run) is None, "a conditional gate"
    assert _step_key(validate_step, "uses") is None
    assert _step_mapping(validate_step, "if") == {}
    assert "continue-on-error" not in validate_step

    # The SAME directory is validated and uploaded.
    upload_target = _step_mapping(steps[upload[0]], "with")["path"].strip("'\"")
    assert _resolved_site_dir(run) == upload_target == "site"

    # A managed CPython 3.11 is set up before the gate and bound BY VALUE.
    setups = [index for index, use in enumerate(uses)
              if use.startswith("actions/setup-python@")]
    assert len(setups) == 1
    assert setups[0] < validate_index
    setup_step = steps[setups[0]]
    assert _step_mapping(setup_step, "with")["python-version"].strip("'\"") == "3.11"
    setup_id = _step_key(setup_step, "id")
    assert setup_id
    reference = "${{ steps.%s.outputs.python-path }}" % setup_id
    environ = _step_mapping(validate_step, "env")
    bound = [name for name, value in environ.items() if value == reference]
    assert len(bound) == 1, "the interpreter is not bound from setup-python"
    assert '"$%s"' % bound[0] in run

    # The reviewed pins are the ones already in the full test workflow.
    test_uses = [_pin(_step_key(step, "uses"))
                 for step in _workflow_steps(TEST_WORKFLOW.read_text())]
    for index in setups + [index for index, use in enumerate(uses)
                           if use.startswith("actions/checkout@")]:
        assert uses[index] in test_uses, (uses[index], "unreviewed pin")

    # Triggers/limits preserved, with the validator's own inputs watched.
    paths = _trigger_paths(text)
    for required in ("site/**", ".github/workflows/deploy-dashboard.yml",
                     "scripts/validate_site.py", "catalog_export.py",
                     "watchdog_catalog/**", "build_site.py"):
        assert required in paths, required
    assert "branches: [master]" in text
    assert re.search(r"(?m)^  workflow_dispatch:", text)
    for preserved in ("  contents: read", "  pages: write", "  id-token: write",
                      "      name: github-pages", "  group: pages",
                      "  cancel-in-progress: true"):
        assert preserved in text, preserved
    assert "steps.deployment.outputs.page_url" in text

    # One job only: no second uploader/deployer can bypass the gate.
    jobs_block = text.split("\njobs:\n", 1)[1]
    assert re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", jobs_block) == ["deploy"]
    assert text.count("actions/upload-pages-artifact@") == 1
    assert text.count("actions/deploy-pages@") == 1

    # No bypass anywhere: no skipped step, no swallowed command, no rebuild.
    assert re.search(r"(?m)^\s+continue-on-error:", text) is None
    assert re.search(r"(?m)^\s+if:", text) is None
    for run_text in runs:
        for swallowed in ("|| true", "|| echo", "set +e"):
            assert swallowed not in run_text, swallowed
        for forbidden in ("pip install", "build_site.py", "inference_watchdog"):
            assert forbidden not in run_text, forbidden


def _gate_run(text, root, *, sentinel, home, tmpdir):
    """Execute the workflow's own extracted validation command, then a sentinel.

    The sentinel stands for the upload/deploy steps: it is reached only when the
    executed command exits 0, exactly as a GitHub step failure stops the job.
    """
    steps = _workflow_steps(text)
    selected = [step for step in steps
                if "scripts/validate_site.py" in (_step_key(step, "run") or "")]
    assert len(selected) == 1
    step = selected[0]
    run_text = _step_key(step, "run")
    environ = _step_mapping(step, "env")
    reference = [name for name, value in environ.items()
                 if ".outputs.python-path" in value]
    assert len(reference) == 1
    env = _child_env(home, tmpdir)
    env[reference[0]] = sys.executable
    proc = subprocess.run(["/bin/bash", "-c", run_text], cwd=str(root), env=env,
                          capture_output=True, text=True,
                          timeout=CHILD_TIMEOUT_S)
    reached = False
    if proc.returncode == 0:
        sentinel.write_text("upload/deploy reached\n", encoding="utf-8")
        reached = True
    return proc, reached


def _without_step(text, needle):
    lines = text.splitlines()
    kept = []
    dropping = False
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if indent == 6 and stripped.startswith("- "):
            dropping = needle in stripped
            if dropping:
                continue
        elif dropping and stripped and indent <= 4:
            dropping = False
        if not dropping:
            kept.append(line)
    return "\n".join(kept) + "\n"


def _weakened_gate(text):
    """Append ``|| true`` to whichever command line invokes the real CLI."""
    lines = text.splitlines()
    # The LAST match is the executed command: earlier ones are trigger paths.
    for index in reversed(range(len(lines))):
        line = lines[index]
        if "scripts/validate_site.py" in line and not line.strip().startswith("#"):
            lines[index] = line + " || true"
            return "\n".join(lines) + "\n"
    raise AssertionError("no CLI invocation to weaken")


def test_pages_workflow_gates_upload_on_the_real_validator(tmp_path):
    workflow_text = WORKFLOW.read_text(encoding="utf-8")

    # -- (a) the checked-in wiring satisfies the contract ------------------
    _assert_workflow_gate(workflow_text)

    # -- (b) execute the ACTUAL extracted command, then the sentinel -------
    # One owned checkout-shaped root per run: the workflow's relative
    # `scripts/validate_site.py` and `site` resolve from this cwd exactly as
    # they do from $GITHUB_WORKSPACE on a runner.
    good_root = _copy_checkout(tmp_path / "workflow checkout é")
    good = _make_site(good_root / "site")
    home = tmp_path / "home"
    home.mkdir()
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    sentinel = tmp_path / "upload-deploy-sentinel"
    before = _tree(good)

    proc, reached = _gate_run(workflow_text, good_root, sentinel=sentinel,
                              home=home, tmpdir=tmpdir)
    assert proc.returncode == 0, proc.stderr
    assert reached and sentinel.exists()
    assert proc.stdout == ""
    assert "Traceback" not in proc.stderr
    assert _tree(good) == before, "validation wrote to the uploaded directory"
    assert _tree(home) == {}

    broken_root = _copy_checkout(tmp_path / "workflow broken checkout é")
    broken = _make_site(broken_root / "site", html_text=(
        "<!DOCTYPE html><html><body><p>no island</p></body></html>\n"))
    broken_sentinel = tmp_path / "broken-sentinel"
    proc, reached = _gate_run(workflow_text, broken_root,
                              sentinel=broken_sentinel, home=home, tmpdir=tmpdir)
    assert proc.returncode == 2, proc.stderr
    assert not reached and not broken_sentinel.exists(), \
        "a rejected pair reached the upload/deploy step"
    assert proc.stdout == "" and "Traceback" not in proc.stderr

    # -- (c) negative controls: weakened and removed gates ----------------
    weakened = _weakened_gate(workflow_text)
    with pytest.raises(AssertionError):
        _assert_workflow_gate(weakened)
    control_sentinel = tmp_path / "control-sentinel"
    proc, reached = _gate_run(weakened, broken_root, sentinel=control_sentinel,
                              home=home, tmpdir=tmpdir)
    assert reached and control_sentinel.exists(), \
        "the weakened control did not reach the sentinel"
    # The same expectation this file asserts above now fails for real behavior.
    with pytest.raises(AssertionError):
        assert not reached and not control_sentinel.exists()

    removed = _without_step(workflow_text, "Validate the site")
    assert not [step for step in _workflow_steps(removed)
                if "scripts/validate_site.py" in (_step_key(step, "run") or "")]
    with pytest.raises(AssertionError):
        _assert_workflow_gate(removed)
