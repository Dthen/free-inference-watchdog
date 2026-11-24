"""Read-only Pages artifact gates: the validator CLI and the source checker.

Two stdlib-only, fully synthetic groups:

* the CLI group runs the REAL ``scripts/validate_site.py`` as an isolated
  ``-I -S`` child from an unrelated cwd (with spaces and non-ASCII characters
  in every owned path). It pins exit 0/2, empty stdout, a bounded
  non-reflective stderr diagnostic, byte-for-byte artifact and sentinel
  preservation, the absence of bytecode/companion writes, its own file
  decoding and regular-file/IO boundaries, one representative mismatched pair,
  and -- through a fresh-process audit hook around the SUCCESSFUL path -- that
  the exercised route imports only this checkout's pure validator and opens
  exactly the two named artifacts, with no socket, provider, config/state,
  renderer, builder or SDK involvement. A cwd/site import-shadow sentinel
  proves the supplied directory never becomes the validator.
* the freshness group runs the REAL ``scripts/check_page_sources.py`` against
  synthetic pages built from the maintained sources: the exact bodies are
  accepted read-only, and a missing, reordered, duplicated, extra, mutated,
  truncated or unreadable script (or source) is rejected. It also pins the
  checker's marker table and bootstrap marker to ``build_site``'s own literals,
  so the gate cannot drift away from the builder it is checking.

Nothing here publishes, deploys, installs, fetches or reads private state: the
catalogue input is the accepted synthetic golden fixture
``tests/fixtures/catalog/basic.json`` and the page is an explicitly synthetic
whole document carrying the same JSON island. ``validate_artifact_pair`` (the
real shared validator) is called directly to prove each accepted fixture is a
coherent pair. The maintained ``web/*.js`` sources are the repository's own
committed files, read here only as inert text. The deployment workflow and the
test workflow are out of this file's scope: their real commands are the gates,
not a Python reconstruction of GitHub step behaviour.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISFIFO

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "scripts" / "validate_site.py"
SOURCES_CLI = REPO / "scripts" / "check_page_sources.py"
FIXTURES = REPO / "tests" / "fixtures" / "catalog"
WEB = REPO / "web"

#: The maintained executable page sources, as ``build_site`` names them.
MAINTAINED_SOURCES = (
    ("catalog-query", "catalog-query.js"),
    ("webmcp", "webmcp.js"),
)
BOOTSTRAP_MARKER = "boot"

#: A fixed token planted only in a stale inlined body: it must never be echoed.
SOURCE_ECHO_MARKER = "inline-source-tampered-marker"

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


def _make_site(site, catalog_text=None, html_text=None):
    site.mkdir(parents=True, exist_ok=True)
    (site / "catalog.json").write_bytes(
        (catalog_text if catalog_text is not None else _catalog_text())
        .encode("utf-8"))
    (site / "index.html").write_bytes(
        (html_text if html_text is not None else _page(_catalog_text()))
        .encode("utf-8"))
    return site


def _source_text(name, web=None):
    return ((web if web is not None else WEB) / name).read_text(encoding="utf-8")


def _page_with_scripts(catalog_text, scripts):
    """A synthetic page whose executable slot is exactly ``scripts``.

    ``scripts`` is an ordered sequence of ``(marker, body)`` pairs, rendered
    with the builder's own element shape so the freshness gate sees the same
    markup a real ``build_site`` run emits.
    """
    elements = "\n".join(
        f'<script data-watchdog="{marker}">' + body + "</script>"
        for marker, body in scripts)
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head><meta charset=\"utf-8\">\n"
            "<title>synthetic standalone dashboard fixture</title>\n</head>\n"
            "<body>\n<main>synthetic fixture</main>\n"
            + _island(catalog_text) + "\n" + elements + "\n</body>\n</html>\n")


def _builder_bootstrap_source():
    """The builder's own bootstrap text: the third element's expected body."""
    import build_site

    return build_site.PAGE_BOOTSTRAP_SOURCE


def _fresh_scripts(web=None, *, overrides=None):
    """The maintained source pairs plus the builder's exact bootstrap body."""
    overrides = overrides or {}
    scripts = [(marker, overrides.get(marker, _source_text(name, web)))
               for marker, name in MAINTAINED_SOURCES]
    scripts.append((BOOTSTRAP_MARKER, overrides.get(
        BOOTSTRAP_MARKER, _builder_bootstrap_source())))
    return scripts


def _fresh_page(catalog_text, web=None, *, overrides=None):
    return _page_with_scripts(catalog_text, _fresh_scripts(web, overrides=overrides))


def _copy_checkout(destination):
    """A private copy of BOTH read-only CLIs plus the modules/files they read.

    ``watchdog_catalog/``'s transport modules need the SDK and are copied too
    only so the layout matches a real checkout; the audit below proves they are
    never imported. ``web/`` carries the maintained page sources the freshness
    gate compares against, and it is the copy's OWN ``web/`` directory -- the
    gate resolves its sources from its checkout, never from the site directory.
    """
    (destination / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(CLI, destination / "scripts" / "validate_site.py")
    if SOURCES_CLI.is_file():
        shutil.copy2(SOURCES_CLI, destination / "scripts" / "check_page_sources.py")
    shutil.copy2(REPO / "catalog_export.py", destination / "catalog_export.py")
    # catalog_export imports the pure catalogue seam module, so the private
    # copy must carry it too (no config/credential module is copied).
    shutil.copy2(REPO / "catalog_adapters.py",
                 destination / "catalog_adapters.py")
    shutil.copytree(REPO / "watchdog_catalog", destination / "watchdog_catalog",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(WEB, destination / "web",
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
    # A coherent pair whose island describes a DIFFERENT snapshot is the
    # representative mismatch.
    broken_site = _make_site(tmp_path / "broken site é",
                             html_text=_page(_catalog_text("ordering.json")))
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

    cases = [
        # One representative pair mismatch. The full structural, hash and
        # island matrix is owned by test_catalog_export.
        ("valid but different snapshot",
         {"html_text": _page(_catalog_text("ordering.json"))}, None),
        # The CLI's own file decoding and regular-file/IO boundaries.
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
        _assert_rejected(proc, path_echo=[tmp_path])
        assert _tree(site) == case_before["case"], (label, "artifacts changed")
        for name, root in watched.items():
            assert _tree(root) == case_before[name], (label, name, "wrote outside")
        assert (site / "unrelated-sentinel.txt").read_text() == "untouched\n"

# ---------------------------------------------------------------------------
# group 2: the source-to-inline-artifact freshness gate (unit 9f)
# ---------------------------------------------------------------------------


def _run_sources_cli(script, site_dir, *, cwd, home, tmpdir, web_dir=None,
                     extra=()):
    args = [sys.executable, "-I", "-S", str(script)]
    if site_dir is not None:
        args.extend(["--site-dir", str(site_dir)])
    if web_dir is not None:
        args.extend(["--web-dir", str(web_dir)])
    args.extend(extra)
    return subprocess.run(
        args, cwd=str(cwd), env=_child_env(home, tmpdir),
        capture_output=True, text=True, timeout=CHILD_TIMEOUT_S)


def _assert_freshness_rejected(proc, *, content_echo=(), path_echo=()):
    _assert_rejected(proc, path_echo=path_echo, content_echo=content_echo)
    assert "stale" in proc.stderr or "missing" in proc.stderr \
        or "unexpected" in proc.stderr or "unreadable" in proc.stderr \
        or "UTF-8" in proc.stderr or "regular file" in proc.stderr \
        or "empty" in proc.stderr or "unterminated" in proc.stderr \
        or "cannot be" in proc.stderr, proc.stderr


def test_page_source_checker_accepts_only_the_exact_current_sources(tmp_path):
    assert SOURCES_CLI.is_file(), "the freshness gate CLI is missing"
    checkout = _copy_checkout(tmp_path / "freshness checkout é")
    script = checkout / "scripts" / "check_page_sources.py"
    web = checkout / "web"
    home = tmp_path / "home"
    home.mkdir()
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    neutral = tmp_path / "neutral cwd é"
    neutral.mkdir()
    (neutral / "unrelated-sentinel.txt").write_text("untouched\n", encoding="utf-8")

    # -- (a) the exact current sources are ACCEPTED, read-only -------------
    site = _make_site(tmp_path / "fresh site é",
                      html_text=_fresh_page(_catalog_text(), web))
    (site / "unrelated-sentinel.txt").write_text("untouched\n", encoding="utf-8")
    watched = {"site": site, "checkout": checkout, "neutral": neutral,
               "home": home}
    before = {name: _tree(root) for name, root in watched.items()}
    proc = _run_sources_cli(script, site, cwd=neutral, home=home, tmpdir=tmpdir)
    _assert_accepted(proc)
    for name, root in watched.items():
        assert _tree(root) == before[name], (name, "changed by a successful run")
    assert not list(checkout.rglob("__pycache__")), "bytecode was written"
    assert not list(checkout.rglob("*.pyc"))
    # A page whose bodies are the SAME BYTES but reached through an explicit
    # --web-dir is accepted too: the option selects the expectation, never the
    # published artifact.
    assert _run_sources_cli(script, site, cwd=neutral, home=home, tmpdir=tmpdir,
                            web_dir=web).returncode == 0
    # Prose that merely MENTIONS the attribute cannot satisfy or disturb the
    # gate: the count is tag-scoped, so page text is never read as a script.
    prose = _make_site(
        tmp_path / "prose site é",
        html_text=_fresh_page(_catalog_text(), web).replace(
            "<main>synthetic fixture</main>",
            '<main>mentions data-watchdog="legacy" in prose</main>', 1))
    assert _run_sources_cli(script, prose, cwd=neutral, home=home,
                            tmpdir=tmpdir).returncode == 0

    # -- (b) conventional help and explicit argument errors ----------------
    help_proc = _run_sources_cli(script, None, cwd=neutral, home=home,
                                 tmpdir=tmpdir, extra=["--help"])
    assert help_proc.returncode == 0
    assert "--site-dir" in help_proc.stdout and "--web-dir" in help_proc.stdout
    assert help_proc.stderr == ""
    for label, extra in (
            ("missing --site-dir", [str(script)]),
            ("abbreviated long option", [str(script), "--site", str(site)]),
            ("unknown option",
             [str(script), "--site-dir", str(site), "--nope"]),
            ("absent site directory",
             [str(script), "--site-dir", str(tmp_path / "absent site")]),
            ("absent web directory",
             [str(script), "--site-dir", str(site),
              "--web-dir", str(tmp_path / "absent web")]),
    ):
        proc = subprocess.run(
            [sys.executable, "-I", "-S"] + extra, cwd=str(neutral),
            env=_child_env(home, tmpdir), capture_output=True, text=True,
            timeout=CHILD_TIMEOUT_S)
        assert proc.returncode == 2, (label, proc.returncode, proc.stderr)
        assert proc.stdout == "" and proc.stderr.strip(), label
        assert "Traceback" not in proc.stderr, label
        assert len(proc.stderr) <= DIAGNOSTIC_LIMIT, (label, proc.stderr)

    # -- (c) every stale or malformed page fails closed, without writes ----
    scripts = _fresh_scripts(web)
    fresh = _fresh_page(_catalog_text(), web)

    def _drop(marker):
        return _page_with_scripts(
            _catalog_text(), [pair for pair in scripts if pair[0] != marker])

    def _swap():
        first, second, boot = scripts
        return _page_with_scripts(_catalog_text(), [second, first, boot])

    def _duplicate():
        first, second, boot = scripts
        return _page_with_scripts(_catalog_text(), [first, first, second, boot])

    def _extra():
        return _page_with_scripts(_catalog_text(),
                                  [("legacy", "/* legacy */\n")] + scripts)

    def _stale(marker):
        return _page_with_scripts(
            _catalog_text(),
            [(name, body if name != marker
              else body + "\n// " + SOURCE_ECHO_MARKER + "\n")
             for name, body in scripts])

    def _empty_boot():
        return _page_with_scripts(
            _catalog_text(),
            [(name, "" if name == BOOTSTRAP_MARKER else body)
             for name, body in scripts])

    def _decorated_tag():
        return fresh.replace('<script data-watchdog="catalog-query">',
                             '<script data-watchdog="catalog-query" defer>', 1)

    page_only = [
        ("page from before the page scripts existed", _page(_catalog_text())),
        ("missing maintained script", _drop("catalog-query")),
        ("missing adapter script", _drop("webmcp")),
        ("missing bootstrap", _drop(BOOTSTRAP_MARKER)),
        ("reordered scripts", _swap()),
        ("duplicated script", _duplicate()),
        ("unexpected extra script", _extra()),
        ("stale engine body", _stale("catalog-query")),
        ("stale adapter body", _stale("webmcp")),
        ("stale bootstrap body", _stale(BOOTSTRAP_MARKER)),
        ("empty bootstrap body", _empty_boot()),
        ("unterminated last script element", fresh.rsplit("</script>", 1)[0]),
        ("marker on an unexpected tag shape", _decorated_tag()),
        ("no closing tag at all", fresh.replace("</script>", "")),
    ]

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "catalog-query.js").write_text(_source_text("catalog-query.js"),
                                              encoding="utf-8")

    def _page_symlink(case):
        (case / "index.html").unlink()
        (case / "index.html").symlink_to(outside / "catalog-query.js")

    def _page_directory(case):
        (case / "index.html").unlink()
        (case / "index.html").mkdir()

    def _page_missing(case):
        (case / "index.html").unlink()

    def _page_non_utf8(case):
        (case / "index.html").write_bytes(fresh.encode("utf-8") + b"\xff")

    if hasattr(os, "mkfifo"):
        def _page_fifo(case):
            (case / "index.html").unlink()
            os.mkfifo(case / "index.html")
        page_mutations = [_page_fifo]
    else:
        page_mutations = []

    source_cases = []

    def _source_case(label, mutate):
        case_web = tmp_path / ("web %s é" % label.replace(" ", "-"))
        shutil.copytree(web, case_web)
        mutate(case_web)
        source_cases.append((label, case_web))

    def _source_missing(case_web):
        (case_web / "webmcp.js").unlink()

    def _source_directory(case_web):
        (case_web / "catalog-query.js").unlink()
        (case_web / "catalog-query.js").mkdir()

    def _source_non_utf8(case_web):
        (case_web / "catalog-query.js").write_bytes(
            (case_web / "catalog-query.js").read_bytes() + b"\xff")

    def _source_symlink(case_web):
        (case_web / "catalog-query.js").unlink()
        (case_web / "catalog-query.js").symlink_to(outside / "catalog-query.js")

    _source_case("missing source file", _source_missing)
    _source_case("directory source file", _source_directory)
    _source_case("non-UTF-8 source file", _source_non_utf8)
    _source_case("symlinked source file", _source_symlink)
    if os.geteuid() != 0:
        def _source_unreadable(case_web):
            os.chmod(case_web / "catalog-query.js", 0o000)
        _source_case("unreadable source file", _source_unreadable)

    for index, (label, html_text) in enumerate(page_only):
        case = _make_site(tmp_path / ("page case %02d é" % index),
                          html_text=html_text)
        (case / "unrelated-sentinel.txt").write_text("untouched\n",
                                                     encoding="utf-8")
        case_before = {name: _tree(root) for name, root in watched.items()}
        case_before["case"] = _tree(case)
        proc = _run_sources_cli(script, case, cwd=neutral, home=home,
                                tmpdir=tmpdir)
        _assert_freshness_rejected(proc, content_echo=[SOURCE_ECHO_MARKER],
                                   path_echo=[tmp_path])
        assert _tree(case) == case_before["case"], (label, "artifacts changed")
        for name, root in watched.items():
            assert _tree(root) == case_before[name], (label, name, "wrote outside")

    for index, mutate in enumerate([_page_symlink, _page_directory,
                                    _page_missing, _page_non_utf8]
                                   + page_mutations):
        case = _make_site(tmp_path / ("page shape %02d é" % index),
                          html_text=fresh)
        mutate(case)
        proc = _run_sources_cli(script, case, cwd=neutral, home=home,
                                tmpdir=tmpdir)
        _assert_freshness_rejected(proc, path_echo=[tmp_path])

    for label, case_web in source_cases:
        case = _make_site(tmp_path / ("source case %s é" % label.replace(" ", "-")),
                          html_text=fresh)
        proc = _run_sources_cli(script, case, cwd=neutral, home=home,
                                tmpdir=tmpdir, web_dir=case_web)
        _assert_freshness_rejected(proc, path_echo=[tmp_path])

def test_page_source_table_matches_the_builder_and_stays_stdlib_only():
    assert SOURCES_CLI.is_file()
    import build_site

    spec = importlib.util.spec_from_file_location("check_page_sources",
                                                  SOURCES_CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.PAGE_SOURCE_FILES == tuple(
        (marker, path.as_posix()) for marker, path in build_site.PAGE_SOURCE_FILES)
    assert module.PAGE_BOOTSTRAP_MARKER == build_site.PAGE_BOOTSTRAP_MARKER
    assert module.PAGE_BOOTSTRAP_SOURCE == build_site.PAGE_BOOTSTRAP_SOURCE
    assert module.HTML_NAME == build_site.SITE_REL.name

    tree = ast.parse(SOURCES_CLI.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "argparse", "os", "re", "stat", "sys"}, \
        "the freshness gate must stay stdlib-only and producer-free"

