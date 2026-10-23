"""Distribution packaging contract and installed-console evidence (units 6a/6e).

This module pins the PEP 621 metadata contract, proves the ACTUAL wheel payload
rather than a spelling of the configuration, and then exercises that wheel as an
INSTALLED console command. It has exactly three unparametrized groups.

The first two are the preserved unit-6a archive/core checks:

1. ``test_packaging_metadata_contract`` reads ``pyproject.toml`` with the
   stdlib ``tomllib`` and the tracked ``LICENSE`` text. It is a file/metadata
   contract, never an AST or regex guess about runtime code, and it never
   imports the SDK, provider configuration or the producer modules to read a
   version. The distribution version is declared statically in the TOML and is
   deliberately NOT duplicated here, so a version bump cannot fail a test that
   is really about the wheel/metadata relationship.

2. ``test_wheel_payload_and_core_smoke`` stages the real package, the reviewed
   ``tools.json`` contract, ``README.md``, ``LICENSE`` and the committed
   ``pyproject.toml`` into owned scratch, adds synthetic unwanted neighbours
   (producer/provider/state/cron/site/assets/test files plus an ``.env``-like
   sentinel, all authored here -- no real credential or state file is read),
   and builds a genuine wheel through ``setuptools.build_meta`` in a bounded
   subprocess. It then inspects the archive with ``zipfile`` and runs the
   WHEEL's own core/resource from a neutral working directory with the source
   tree absent from ``sys.path`` and the SDK/producer/provider imports blocked.
   That group stays a zip-on-``sys.path`` smoke: it is not a pip clean install
   and not a console launch, and it says so.

3. ``test_installed_console_clean_install_and_stdio_isolation`` is the unit-6e
   INTEGRATION evidence for Task 6. It builds the same reviewed payload again,
   audits the exact wheel it installs, hides the owned staged source, creates a
   NEW PEP 405 CPython 3.11 environment from the floor interpreter (no system or
   user site-packages) and installs the real wheel OFFLINE from an owned
   dependency wheelhouse with the reviewed SDK pins as extra constraints -- the
   application's own ``Requires-Dist`` drives resolution, and a missing
   wheelhouse fails with the exact preparation command instead of skipping. It
   proves the fresh prefix/interpreter/site-packages, the installed
   distribution version, the installer-generated console metadata and the
   installed package bytes, then launches ONLY
   ``<fresh-env>/bin/free-inference-watchdog-mcp`` by absolute path from a
   neutral cwd with an empty HOME, an owned cache root and a minimal
   credential-free environment. An owned probe hook blocks the legacy graph
   before any package code runs, blocks the SDK where help/core must not need it,
   refuses every outbound connection except the one owned 127.0.0.1 feed port,
   and reports the child's real pid, cwd, interpreter, prefix, ``sys.path`` and
   per-module import digests, so installed-origin and byte claims never rest on
   the launch command. It covers no-feed ``--help`` plus a missing-file
   selection followed by ``--help`` (each with its own report destination, the
   same origin checker and its own stdout/stderr/side-effect evidence), an
   SDK-blocker positive control, the
   installed pure core/resource in an isolated process, a no-feed stdin-EOF
   launch, a full real-SDK file session, loopback acquisition, last-good cache
   fallback, absent/corrupt-cache source errors and five complete raw
   transcripts. A checkout-shadow negative control proves the same origin
   checker rejects an identical-bytes checkout import, and two real packaging
   variants (wrong console entry point, omitted ``tools.json``) must be rejected
   by the same payload/launch assertions. The transport coverage itself is
   REUSED from the reviewed group-5/6 harness in ``tests/test_mcp_protocol.py``,
   loaded explicitly in the host process, so no second protocol implementation
   exists and no test/helper/repo path is ever given to a server child.

   Group 3 attributes its evidence per invocation: every hook-enabled child (the
   primary help run, each blocker control, the missing-file help, the core probe,
   the EOF launch, every protocol session and the shadow console) writes to its
   OWN report/audit destination, because the owned hook reports from ``atexit``
   and would otherwise overwrite an earlier child's evidence. Retained reports
   are reconciled by label/pid/argv against the saved files both before and after
   the manifest is written, and the shadow control is rejected at the
   ``outside the installed site-packages`` branch specifically -- never by an
   earlier missing-report or foreign-pid branch. Retained ``.session.json``
   records are validated for ``returncode``/``natural_exit``/``stdin_closed``/
   ``protocol_violations`` AND ``forced_cleanup is False`` (with a negative row
   proving that last guard), since the live unit-6d audit cannot speak for a
   saved record. The manifest keeps the declared backend's own build transcript
   for the exact installed wheel (the ``uv build`` frontend transcript stays
   separately labelled), plus SHA-256 of both executed test files.

Honest scope of group 3: this is a LOCAL wheel-only clean installation. Published
Git-source ``uvx --from git+...`` evidence is deliberately absent because no
public ref contains this package yet, and PyPI publication, rollout and operator
configuration changes are out of scope. The dependency wheels come from a
one-time preparation performed OUTSIDE pytest; the test performs no public
network operation and contacts only its own loopback feed.

Build tooling is a prerequisite, not a runtime dependency: group 2 needs a
``setuptools>=77.0.3`` interpreter (``FREE_INFERENCE_WATCHDOG_BUILD_PYTHON``
overrides the default of the current interpreter), and group 3 additionally
needs an exact CPython 3.11
(``FREE_INFERENCE_WATCHDOG_INSTALLED_PYTHON``), the ``uv`` frontend
(``FREE_INFERENCE_WATCHDOG_UV``) and a prepared wheelhouse
(``FREE_INFERENCE_WATCHDOG_WHEELHOUSE``, default
``~/.cache/free-inference-watchdog-mcp/cp311-sdk2.3.0/``). A missing prerequisite
is an actionable failure with the exact preparation command, never a skip.
"""

import hashlib
import json
import os
import pytest
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
README = REPO / "README.md"
LICENSE = REPO / "LICENSE"
PACKAGE = REPO / "watchdog_catalog"
CONTRACT_RESOURCE = PACKAGE / "tools.json"
SNAPSHOT_FIXTURE = REPO / "tests" / "fixtures" / "catalog" / "basic.json"

DIST_NAME = "free-inference-watchdog-mcp"
CONSOLE_COMMAND = "free-inference-watchdog-mcp"
CONSOLE_TARGET = "watchdog_catalog.__main__:main"
MINIMUM_BACKEND = (77, 0, 3)

# The only legal SDK import in this wheel arrives with the transport modules in
# a later unit; the pure core and its resource must import neither the SDK nor
# any producer/provider/config module.
BLOCKED_IMPORTS = (
    "mcp",
    "mcp_types",
    "pydantic",
    "config_loader",
    "providers",
    "inference_watchdog",
    "catalog_export",
    "envfile",
    "notify",
    "diffing",
    "detection",
    "alive",
    "build_site",
    "pending_removals",
    "probe_select",
    "probe_state",
    "probe_zero_credit",
    "state",
)

# Synthetic forbidden neighbours. Every byte is authored here: the test never
# copies real operator state, credentials or producer code into the scratch
# build tree.
UNWANTED_NEIGHBOURS = {
    "inference_watchdog.py": "PRODUCER_SENTINEL = 'synthetic-producer-module'\n",
    "catalog_export.py": "EXPORT_SENTINEL = 'synthetic-catalog-export'\n",
    "config_loader.py": "PROVIDERS = ()\n",
    "providers/synthetic-provider.json": '{"id": "synthetic-provider"}\n',
    "state/roster.json": '{"providers": {"synthetic": {}}}\n',
    "state/alive.json": "{}\n",
    "cron/watchdog.cron": "17 * * * * synthetic-cron-entry\n",
    "site/catalog.json": '{"synthetic": true}\n',
    "assets/style.css": "body { color: #000; }\n",
    "scripts/wrapper.sh": "#!/bin/sh\nexit 0\n",
    "tests/test_synthetic_neighbour.py": "def test_synthetic():\n    assert True\n",
    ".env": "SYNTHETIC_SENTINEL_TOKEN=not-a-real-credential\n",
    "watchdog_catalog/scratch-notes.txt": "unreviewed package-local data\n",
}

_INERT_ROOT_PROBE = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import watchdog_catalog\n"
    "assert not hasattr(watchdog_catalog, '__version__'), "
    "'the package root must not expose a runtime version; metadata is static'\n"
    "for forbidden in sys.argv[2:]:\n"
    "    assert forbidden not in sys.modules, "
    "'package root imported ' + forbidden\n"
)

_SMOKE_SCRIPT = (
    "import hashlib, importlib.abc, json, os, sys\n"
    "wheel, fixture, blocked = sys.argv[1], sys.argv[2], set(sys.argv[3].split(','))\n"
    "class Blocker(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, fullname, path=None, target=None):\n"
    "        if fullname.split('.')[0] in blocked:\n"
    "            raise ImportError('blocked import during wheel smoke: ' + fullname)\n"
    "        return None\n"
    "original_path = list(sys.path)\n"
    "sys.meta_path.insert(0, Blocker())\n"
    "sys.path.insert(0, wheel)\n"
    "from importlib.resources import files\n"
    "import watchdog_catalog\n"
    "from watchdog_catalog import catalog, query\n"
    "application_modules = {\n"
    "    name: getattr(module, '__file__', None)\n"
    "    for name, module in sys.modules.items()\n"
    "    if name.split('.')[0] == 'watchdog_catalog'\n"
    "}\n"
    "resource = files('watchdog_catalog').joinpath('tools.json').read_bytes()\n"
    "with open(fixture, 'r', encoding='utf-8') as handle:\n"
    "    snapshot = catalog.load_snapshot(handle.read())\n"
    "page = query.search_models(\n"
    "    snapshot, {'query': 'alpha', 'min_context_length': 16000, 'limit': 1}\n"
    ")\n"
    "print(json.dumps({\n"
    "    'application_modules': application_modules,\n"
    "    'contract_names': [entry['name'] for entry in json.loads(resource)],\n"
    "    'contract_sha256': hashlib.sha256(resource).hexdigest(),\n"
    "    'search': {\n"
    "        'ok': page['ok'],\n"
    "        'total_matches': page['total_matches'],\n"
    "        'returned_count': page['returned_count'],\n"
    "        'model_ids': [row['model_id'] for row in page['endpoints']],\n"
    "        'next_offset': page['next_offset'],\n"
    "        'filters': page['filters'],\n"
    "    },\n"
    "    'original_path': original_path,\n"
    "    'cwd': os.getcwd(),\n"
    "}))\n"
)


# --------------------------------------------------------------------------
# small parsers and environment helpers
# --------------------------------------------------------------------------
def _requirement(text):
    """Split one requirement into a normalized name and compact specifier."""
    match = re.fullmatch(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*?)\s*", text)
    assert match, f"unparsable requirement: {text!r}"
    return match.group(1).lower(), re.sub(r"\s+", "", match.group(2))


def _release_version(text):
    """A plain static PEP 440 release (``X.Y.Z``), never an epoch/pre/post."""
    return re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)",
                        text) is not None


def _version_tuple(text):
    parts = []
    for token in text.split("."):
        digits = re.match(r"\d+", token)
        parts.append(int(digits.group(0)) if digits else 0)
    return tuple(parts)


def _clean_environment():
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV",
                       "SETUPTOOLS_SCM_PRETEND_VERSION")
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _scratch_directory(prefix):
    base = Path(os.environ.get("TMPDIR")
                or (Path.home() / ".hermes" / "cache" / "scratch"))
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))


def _floor_python():
    """A >= 3.11 interpreter for out-of-process smokes, preferring 3.11."""
    candidates = [os.environ.get("FREE_INFERENCE_WATCHDOG_SMOKE_PYTHON"),
                  shutil.which("python3.11"), sys.executable]
    tried = []
    for candidate in candidates:
        if not candidate:
            continue
        probe = subprocess.run(
            [candidate, "-I", "-c",
             "import sys; print('.'.join(str(part) for part in sys.version_info[:2]))"],
            capture_output=True, text=True, timeout=60, env=_clean_environment())
        if probe.returncode != 0:
            tried.append(f"{candidate}: cannot run ({probe.stderr.strip()[:200]})")
            continue
        version = tuple(int(part) for part in probe.stdout.strip().split("."))
        if version < (3, 11):
            tried.append(f"{candidate}: Python {version} is below the declared floor")
            continue
        return candidate
    raise AssertionError(
        "no Python >= 3.11 interpreter available for the wheel smoke; tried: "
        + "; ".join(tried))


def _build_python():
    """The interpreter that provides the declared build backend."""
    candidates = [os.environ.get("FREE_INFERENCE_WATCHDOG_BUILD_PYTHON"),
                  sys.executable]
    tried = []
    for candidate in candidates:
        if not candidate:
            continue
        probe = subprocess.run(
            [candidate, "-c",
             "import setuptools, sys; sys.stdout.write(setuptools.__version__)"],
            capture_output=True, text=True, timeout=120, env=_clean_environment())
        if probe.returncode != 0:
            tried.append(f"{candidate}: setuptools not importable")
            continue
        version = probe.stdout.strip()
        if _version_tuple(version) < MINIMUM_BACKEND:
            tried.append(f"{candidate}: setuptools {version} is older than "
                         f"{'.'.join(str(part) for part in MINIMUM_BACKEND)}")
            continue
        return candidate, version
    raise AssertionError(
        "the declared build backend is not available: install it once, outside "
        "pytest, into the chosen build interpreter with "
        f"`<python> -m pip install 'setuptools>=77.0.3'` (or point "
        "FREE_INFERENCE_WATCHDOG_BUILD_PYTHON at such an interpreter). "
        "Tried: " + "; ".join(tried))


# --------------------------------------------------------------------------
# group 1: static PEP 621 metadata contract
# --------------------------------------------------------------------------
def test_packaging_metadata_contract():
    assert PYPROJECT.is_file(), "pyproject.toml carries the distribution contract"
    with PYPROJECT.open("rb") as handle:
        document = tomllib.load(handle)

    build_system = document["build-system"]
    assert build_system["build-backend"] == "setuptools.build_meta"
    assert "backend-path" not in build_system
    requirements = build_system["requires"]
    assert len(requirements) == 1, "exactly the documented backend requirement"
    backend_name, backend_spec = _requirement(requirements[0])
    assert (backend_name, backend_spec) == ("setuptools", ">=77.0.3")
    assert "scm" not in requirements[0].lower(), "no VCS/dynamic-version plugin"

    project = document["project"]
    assert project["name"] == DIST_NAME
    assert "dynamic" not in project, "every metadata field is declared statically"
    assert _release_version(project["version"]), (
        "the static version is a plain PEP 440 release; the exact value is "
        "deliberately not duplicated in this test")
    assert project["requires-python"] == ">=3.11"
    assert project["readme"] == "README.md" and README.is_file()
    description = project["description"]
    assert description.strip() == description and description
    assert "\n" not in description and len(description) <= 200

    assert project["license"] == "ISC"
    assert project["license-files"] == ["LICENSE"]
    assert LICENSE.is_file(), "the declared license source exists"
    assert not (REPO / "LICENSE.md").exists(), "no stale license filename"
    license_text = LICENSE.read_text(encoding="utf-8")
    assert ("Permission to use, copy, modify, and/or distribute this software "
            "for any" in license_text)
    assert "THE SOFTWARE IS PROVIDED" in license_text
    assert "Apache License" not in license_text
    assert "MIT License" not in license_text

    dependencies = project["dependencies"]
    assert len(dependencies) == 1, "one unconditional runtime dependency"
    dependency_name, dependency_spec = _requirement(dependencies[0])
    assert dependency_name == "mcp"
    assert set(dependency_spec.split(",")) == {">=2", "<3"}
    assert ";" not in dependencies[0], "the main command dependency is not gated"
    assert "[" not in dependencies[0], "no extra is required by the main command"
    assert not project.get("optional-dependencies"), (
        "the SDK is a declared dependency of the console command, not an extra")

    assert project["scripts"] == {CONSOLE_COMMAND: CONSOLE_TARGET}

    tool = document["tool"]["setuptools"]
    assert set(tool) == {"packages", "py-modules", "include-package-data",
                         "package-data"}
    assert tool["packages"] == ["watchdog_catalog"], (
        "explicit package selection, never flat-layout auto-discovery")
    assert tool["py-modules"] == []
    assert tool["include-package-data"] is False
    assert tool["package-data"] == {"watchdog_catalog": ["tools.json"]}
    assert (PACKAGE / "__init__.py").is_file()
    assert CONTRACT_RESOURCE.is_file()

    for legacy in ("setup.py", "setup.cfg", "MANIFEST.in"):
        assert not (REPO / legacy).exists(), (
            "one static configuration source; no legacy metadata file")

    neutral = Path(os.environ.get("TMPDIR")
                   or (Path.home() / ".hermes" / "cache" / "scratch"))
    neutral = neutral / "unit6a-inert-cwd"
    neutral.mkdir(parents=True, exist_ok=True)
    probe = subprocess.run(
        [sys.executable, "-I", "-c", _INERT_ROOT_PROBE, str(REPO),
         *BLOCKED_IMPORTS],
        cwd=str(neutral),
        capture_output=True, text=True, timeout=120,
        env=_clean_environment())
    assert probe.returncode == 0, probe.stderr[-4000:]
    assert probe.stdout == "" and probe.stderr == "", (
        "importing the package root stays silent and inert")


# --------------------------------------------------------------------------
# group 2: real wheel payload plus isolated core/resource smoke
# --------------------------------------------------------------------------
def _stage_source(destination):
    """Stage the public package and synthetic neighbours into owned scratch."""
    package = destination / "watchdog_catalog"
    package.mkdir(parents=True)
    for module in sorted(PACKAGE.glob("*.py")):
        shutil.copyfile(module, package / module.name)
    shutil.copyfile(CONTRACT_RESOURCE, package / "tools.json")
    shutil.copyfile(README, destination / "README.md")
    shutil.copyfile(LICENSE, destination / "LICENSE")
    shutil.copyfile(PYPROJECT, destination / "pyproject.toml")
    for relative, content in UNWANTED_NEIGHBOURS.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return destination


def _build_wheel(source, output, *, log_path=None):
    """Build a genuine wheel through the declared backend, in a subprocess.

    ``log_path`` is an OPTIONAL, keyword-only destination for this invocation's
    own transcript (command, return status, stdout and stderr). It defaults to
    ``None`` so every pre-existing caller keeps the exact same command, build
    behaviour and return value; a caller that passes it gets the REAL transcript
    of the wheel it will install, written before any build assertion can discard
    the diagnostics.
    """
    python, _version = _build_python()
    output.mkdir(parents=True, exist_ok=True)
    marker = output / "wheel-name.txt"
    # distutils rewrites sys.argv while building, so bind both paths first.
    script = (
        "import pathlib, sys\n"
        "import setuptools.build_meta as backend\n"
        "output, marker = sys.argv[1], sys.argv[2]\n"
        "name = backend.build_wheel(output)\n"
        "pathlib.Path(marker).write_text(name, encoding='utf-8')\n"
    )
    command = [python, "-c", script, str(output), str(marker)]
    completed = subprocess.run(
        command,
        cwd=str(source), capture_output=True, text=True, timeout=300,
        env=_clean_environment())
    if log_path is not None:
        _write_log(log_path, completed, command)
    assert completed.returncode == 0, (
        "the declared backend failed to build the wheel:\n"
        + completed.stderr[-6000:])
    assert marker.is_file(), "the build did not report a wheel filename"
    wheel = output / marker.read_text(encoding="utf-8").strip()
    assert wheel.is_file(), f"reported wheel is missing: {wheel}"
    return wheel


def _read_wheel(wheel):
    """Read every artifact fact this contract asserts, with stdlib only."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        dist_infos = sorted({name.split("/")[0] for name in names
                             if name.split("/")[0].endswith(".dist-info")})
        assert len(dist_infos) == 1, f"expected one dist-info, saw {dist_infos}"
        prefix = dist_infos[0]

        def member(name):
            path = f"{prefix}/{name}"
            return archive.read(path) if path in names else None

        resource_name = "watchdog_catalog/tools.json"
        return {
            "names": names,
            "prefix": prefix,
            "metadata": (member("METADATA") or b"").decode("utf-8"),
            "wheel_metadata": (member("WHEEL") or b"").decode("utf-8"),
            "entry_points": (member("entry_points.txt") or b"").decode("utf-8"),
            "record": (member("RECORD") or b"").decode("utf-8"),
            "licenses": {name: archive.read(name) for name in names
                         if name.startswith(prefix + "/licenses/")},
            "tools_json": (archive.read(resource_name)
                           if resource_name in names else None),
            "package_entries": sorted(name for name in names
                                      if name.startswith("watchdog_catalog/")),
            "python_entries": sorted(name for name in names
                                     if name.startswith("watchdog_catalog/")
                                     and name.endswith(".py")),
        }


def _expected_payload():
    python_entries = sorted(f"watchdog_catalog/{module.name}"
                            for module in PACKAGE.glob("*.py"))
    return python_entries, CONTRACT_RESOURCE.read_bytes()


def _payload_violations(facts, expected_python, expected_resource):
    """Every archive-level reason this wheel is not the reviewed payload."""
    violations = []
    prefix = facts["prefix"] + "/"
    for name in facts["names"]:
        if name.endswith("/"):
            continue
        if not (name.startswith("watchdog_catalog/") or name.startswith(prefix)):
            violations.append(
                f"application payload entry outside the reviewed package: {name}")
        lowered = name.lower()
        for fragment in (".env", "state/", "providers/", "cron/", "site/",
                         "assets/", "tests/", "__pycache__", ".pyc"):
            if fragment in lowered:
                violations.append(
                    f"forbidden payload fragment {fragment!r} in {name}")

    for name in facts["package_entries"]:
        if name.endswith(".py") or name == "watchdog_catalog/tools.json":
            continue
        violations.append(f"unreviewed non-Python package data: {name}")

    if facts["python_entries"] != expected_python:
        missing = sorted(set(expected_python) - set(facts["python_entries"]))
        extra = sorted(set(facts["python_entries"]) - set(expected_python))
        violations.append(
            f"package Python payload differs (missing={missing}, extra={extra})")

    resource_names = [name for name in facts["names"]
                      if name.endswith("tools.json")]
    if resource_names != ["watchdog_catalog/tools.json"]:
        violations.append(
            f"the tool-contract resource is duplicated or misplaced: {resource_names}")
    if facts["tools_json"] is None:
        violations.append(
            "missing the reviewed tool-contract resource watchdog_catalog/tools.json")
    elif facts["tools_json"] != expected_resource:
        violations.append(
            "watchdog_catalog/tools.json differs from the reviewed contract bytes")

    recorded = {line.split(",", 1)[0] for line in facts["record"].splitlines()
                if line.strip()}
    payload = {name for name in facts["names"] if not name.endswith("/")}
    unrecorded = sorted(payload - recorded - {prefix + "RECORD"})
    if unrecorded:
        violations.append(f"archive entries absent from RECORD: {unrecorded}")
    return violations


def test_wheel_payload_and_core_smoke():
    assert PYPROJECT.is_file(), "the wheel needs the committed packaging metadata"
    root = _scratch_directory("unit6a-dist-")
    stage = _stage_source(root / "source")
    wheelhouse = root / "wheelhouse"
    wheel = _build_wheel(stage, wheelhouse)
    assert wheel.parent == wheelhouse
    fixture = root / "fixture-basic.json"
    shutil.copyfile(SNAPSHOT_FIXTURE, fixture)

    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert wheel.name == (
        f"free_inference_watchdog_mcp-{project['version']}-py3-none-any.whl"), (
        "the wheel version comes from the TOML metadata, never a frozen copy")

    facts = _read_wheel(wheel)
    expected_python, expected_resource = _expected_payload()
    violations = _payload_violations(facts, expected_python, expected_resource)
    assert violations == [], "wheel payload is not the reviewed public payload:\n" + \
        "\n".join(violations)

    top_levels = {name.split("/")[0] for name in facts["names"]}
    assert top_levels == {"watchdog_catalog", facts["prefix"]}, (
        "one package plus generated dist-info, nothing else")

    metadata_lines = facts["metadata"].splitlines()
    assert f"Name: {DIST_NAME}" in metadata_lines
    assert f"Version: {project['version']}" in metadata_lines
    assert "Requires-Python: >=3.11" in metadata_lines
    assert "License-Expression: ISC" in metadata_lines
    assert "License-File: LICENSE" in metadata_lines
    requires_dist = [line[len("Requires-Dist: "):] for line in metadata_lines
                     if line.startswith("Requires-Dist: ")]
    assert len(requires_dist) == 1
    dependency_name, dependency_spec = _requirement(requires_dist[0])
    assert dependency_name == "mcp"
    assert set(dependency_spec.split(",")) == {">=2", "<3"}
    assert "extra ==" not in requires_dist[0].lower()

    assert facts["licenses"] == {facts["prefix"] + "/licenses/LICENSE":
                                 LICENSE.read_bytes()}, (
        "the tracked ISC license is the only legal file in the wheel")

    wheel_lines = facts["wheel_metadata"].splitlines()
    assert wheel_lines[0] == "Wheel-Version: 1.0"
    assert wheel_lines[1].startswith("Generator: setuptools ")
    assert "Root-Is-Purelib: true" in facts["wheel_metadata"].splitlines()
    assert "Tag: py3-none-any" in facts["wheel_metadata"].splitlines()

    entry_lines = [line.strip() for line in facts["entry_points"].splitlines()
                   if line.strip()]
    assert entry_lines == ["[console_scripts]",
                           f"{CONSOLE_COMMAND} = {CONSOLE_TARGET}"], (
        "the console command is declared for the planned CLI module; this test "
        "does not claim the command is runnable yet")

    neutral = root / "neutral-cwd"
    neutral.mkdir()
    smoke = subprocess.run(
        [_floor_python(), "-I", "-c", _SMOKE_SCRIPT, str(wheel),
         str(fixture), ",".join(BLOCKED_IMPORTS)],
        cwd=str(neutral), capture_output=True, text=True, timeout=180,
        env=_clean_environment())
    assert smoke.returncode == 0, smoke.stderr[-6000:]
    assert smoke.stderr == "", "the smoke must not emit incidental diagnostics"
    output_lines = smoke.stdout.splitlines()
    assert len(output_lines) == 1, (
        "the smoke prints exactly one structured result, got: "
        f"{output_lines!r}")
    result = json.loads(output_lines[0])

    assert Path(result["cwd"]).resolve() == neutral.resolve()
    assert str(REPO) not in result["original_path"], (
        "the smoke must not see the implementation checkout on sys.path")
    assert str(stage) not in result["original_path"]

    assert set(result["application_modules"]) == {
        "watchdog_catalog", "watchdog_catalog.catalog", "watchdog_catalog.query"}
    for name, path in result["application_modules"].items():
        assert path and path.startswith(str(wheel) + os.sep), (
            f"{name} was imported from outside the wheel: {path}")

    contract_names = [entry["name"] for entry in json.loads(expected_resource)]
    assert result["contract_names"] == contract_names
    assert result["contract_sha256"] == hashlib.sha256(
        expected_resource).hexdigest(), (
        "importlib.resources reads the exact reviewed contract from the wheel")

    search = result["search"]
    assert search["ok"] is True
    assert search["total_matches"] == 1
    assert search["returned_count"] == 1
    assert search["model_ids"] == ["alpha-1:free"]
    assert search["next_offset"] is None
    assert search["filters"] == {
        "provider": None,
        "min_context_length": 16000,
        "input_modalities": None,
        "supported_parameters": None,
    }, "the pure query applies real filters on the wheel's own core"


# --------------------------------------------------------------------------
# group 3: wheel-only clean install, the INSTALLED console and its real
# transport, cache, error and isolation sessions
#
# Group 2 ends with a zip-on-``sys.path`` smoke. This group is the installed
# evidence Task 6 asks for: a genuine wheel built by the declared backend, a NEW
# PEP 405 Python 3.11 environment created from the floor interpreter with no
# system or user site-packages, an offline install from an owned dependency
# wheelhouse, and the INSTALLER-GENERATED ``free-inference-watchdog-mcp``
# console script launched by absolute path from a neutral cwd. The
# transport/cache/error sessions REUSE the reviewed group-5/6 harness of
# ``test_mcp_protocol`` -- the real SDK ``ClientSession`` over a real child's
# real stdin/stdout -- loaded explicitly into this HOST process, so no
# test/helper/repo path is ever handed to a server child and no second
# handwritten protocol implementation exists. The host computes every oracle;
# the child only runs the installed wheel.
#
# Honest scope: LOCAL wheel-only clean installation. No published Git-source
# ``uvx`` run (no public ref contains this package yet), no PyPI publication and
# no rollout. The dependency wheels come from an owned one-time preparation
# OUTSIDE pytest; this group performs no public network operation and contacts
# only the loopback feed it owns. A missing offline prerequisite fails with the
# exact preparation command -- never a skip.
# --------------------------------------------------------------------------
WHEELHOUSE_ENVIRONMENT = "FREE_INFERENCE_WATCHDOG_WHEELHOUSE"
INSTALLED_PYTHON_ENVIRONMENT = "FREE_INFERENCE_WATCHDOG_INSTALLED_PYTHON"
UV_ENVIRONMENT = "FREE_INFERENCE_WATCHDOG_UV"

#: The owned durable wheelhouse of genuine cp311 wheels for the reviewed SDK.
DEFAULT_WHEELHOUSE = (Path.home() / ".cache" / "free-inference-watchdog-mcp"
                      / "cp311-sdk2.3.0")

#: Reviewed test-environment CONSTRAINTS. They are additional constraints on
#: the resolver, never a substitute for the application's own metadata.
SDK_PINS = ("mcp==2.3.0", "pydantic==2.14.1", "anyio==4.15.1")
SDK_PIN_VERSIONS = {"mcp": "2.3.0", "pydantic": "2.14.1", "anyio": "4.15.1"}
INSTALLED_MINOR = (3, 11)

#: The owned probe hook: it refuses the named modules at interpreter start
#: (BEFORE any package code runs), refuses every outbound connection except the
#: one owned loopback feed port, and reports this exact child's interpreter,
#: cwd, sys.path and per-module import origins with their byte digests.
INSTALLED_HOOK_ID = "unit6e-installed-console-probe"

_INSTALLED_HOOK = '''\
"""Owned probe hook: refuse modules, guard the network, report import origins."""
import atexit
import hashlib
import importlib.abc
import json
import os
import socket
import sys

BLOCKED = frozenset(__BLOCKED__)
REPORT = os.environ.get("WD_PROBE_REPORT")
AUDIT = os.environ.get("WD_GUARD_AUDIT")
PORT = os.environ.get("WD_ALLOWED_FEED_PORT")
ALLOWED_PORT = int(PORT) if PORT else None
HOOK_ID = __HOOK_ID__
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in BLOCKED or fullname.split(".")[0] in BLOCKED:
            raise ImportError(
                "blocked import during installed-console probe: " + fullname)
        return None


sys.meta_path.insert(0, _Blocker())

REJECTED = []


def _reject(message):
    REJECTED.append(message)
    if AUDIT:
        try:
            with open(AUDIT, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(),
                                         "rejected": message}) + "\\n")
        except OSError:
            pass
    raise RuntimeError(
        "blocked outbound network during installed-console probe: " + message)


def _allowed(address):
    if not isinstance(address, tuple) or len(address) < 2:
        return False
    return (str(address[0]) in LOOPBACK and ALLOWED_PORT is not None
            and address[1] == ALLOWED_PORT)


_CONNECT = socket.socket.connect
_CONNECT_EX = socket.socket.connect_ex
_GETADDRINFO = socket.getaddrinfo


def _guard_connect(original):
    def guarded(self, address):
        if self.family == getattr(socket, "AF_UNIX", None):
            return original(self, address)
        if _allowed(address):
            return original(self, address)
        _reject("connect %r (allowed port: %r)" % (address, ALLOWED_PORT))
    return guarded


def _guard_getaddrinfo(host, port, *args, **kwargs):
    if host is None or str(host) in LOOPBACK:
        return _GETADDRINFO(host, port, *args, **kwargs)
    _reject("resolve %r (allowed port: %r)" % (host, ALLOWED_PORT))


socket.socket.connect = _guard_connect(_CONNECT)
socket.socket.connect_ex = _guard_connect(_CONNECT_EX)
socket.getaddrinfo = _guard_getaddrinfo


def _fact(name):
    module = sys.modules.get(name)
    if module is None:
        return None
    path = getattr(module, "__file__", None)
    digest = None
    if path and os.path.isfile(path):
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    return {"file": path, "sha256": digest}


def _report():
    if not REPORT:
        return
    loaded = sorted(name for name in sys.modules
                    if name == "watchdog_catalog"
                    or name.startswith("watchdog_catalog."))
    payload = {
        "hook": HOOK_ID,
        "blocked": sorted(BLOCKED),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "executable": sys.executable,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "version": list(sys.version_info[:3]),
        "sys_path": list(sys.path),
        "argv": list(sys.argv),
        "application_modules": {name: _fact(name) for name in loaded},
        "loaded_roots": sorted({name.split(".")[0] for name in sys.modules}),
        "allowed_port": ALLOWED_PORT,
        "rejected_connections": list(REJECTED),
    }
    with open(REPORT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)


atexit.register(_report)
'''

_VERSION_PROBE = (
    "import json, sys, sysconfig\n"
    "print(json.dumps({\n"
    "    'version': list(sys.version_info[:3]),\n"
    "    'prefix': sys.prefix,\n"
    "    'base_prefix': sys.base_prefix,\n"
    "    'executable': sys.executable,\n"
    "    'purelib': sysconfig.get_paths()['purelib'],\n"
    "    'sys_path': [entry for entry in sys.path if entry],\n"
    "}))\n"
)

_INSTALLED_FACTS_PROBE = (
    "import json, os, sys, sysconfig\n"
    "import importlib.metadata as metadata\n"
    "name, command, pins = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "dist = metadata.distribution(name)\n"
    "purelib = sysconfig.get_paths()['purelib']\n"
    "markers = sorted(entry for entry in os.listdir(purelib)\n"
    "                 if entry.endswith(('.pth', '.egg-link'))\n"
    "                 or entry.startswith('__editable__'))\n"
    "console = os.path.join(os.path.dirname(sys.executable), command)\n"
    "shebang = None\n"
    "if os.path.isfile(console):\n"
    "    with open(console, 'rb') as handle:\n"
    "        shebang = handle.readline().decode('utf-8', 'replace').strip()\n"
    "versions = {}\n"
    "for pin in pins.split(','):\n"
    "    try:\n"
    "        versions[pin] = metadata.version(pin)\n"
    "    except metadata.PackageNotFoundError:\n"
    "        versions[pin] = None\n"
    "manifest = dist.read_text('direct_url.json')\n"
    "print(json.dumps({\n"
    "    'version': dist.version,\n"
    "    'requires': sorted(str(item) for item in (dist.requires or [])),\n"
    "    'entry_points': [{'name': entry.name, 'value': entry.value,\n"
    "                      'group': entry.group}\n"
    "                     for entry in dist.entry_points],\n"
    "    'installer': (dist.read_text('INSTALLER') or '').strip(),\n"
    "    'direct_url': json.loads(manifest) if manifest else None,\n"
    "    'purelib': purelib,\n"
    "    'prefix': sys.prefix,\n"
    "    'base_prefix': sys.base_prefix,\n"
    "    'versions': versions,\n"
    "    'markers': markers,\n"
    "    'console': console,\n"
    "    'console_shebang': shebang,\n"
    "    'console_executable': os.access(console, os.X_OK),\n"
    "    'sys_path': [entry for entry in sys.path if entry],\n"
    "    'version_info': list(sys.version_info[:3]),\n"
    "}))\n"
)

#: The installed pure core/resource probe: it installs its OWN blocker for the
#: SDK and the legacy graph and then imports only the pure core, so the
#: stdlib-only claim is proven INSIDE the installed environment rather than
#: assumed from the source tree.
_INSTALLED_CORE_PROBE = (
    "import hashlib, importlib.abc, json, sys\n"
    "fixture, blocked = sys.argv[1], set(sys.argv[2].split(','))\n"
    "class Blocker(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, fullname, path=None, target=None):\n"
    "        if fullname in blocked or fullname.split('.')[0] in blocked:\n"
    "            raise ImportError(\n"
    "                'blocked import during installed core probe: ' + fullname)\n"
    "        return None\n"
    "sys.meta_path.insert(0, Blocker())\n"
    "import watchdog_catalog\n"
    "from importlib.resources import files\n"
    "from watchdog_catalog import catalog, query, source\n"
    "resource = files('watchdog_catalog').joinpath('tools.json').read_bytes()\n"
    "with open(fixture, 'r', encoding='utf-8') as handle:\n"
    "    snapshot = catalog.load_snapshot(handle.read())\n"
    "page = query.search_models(\n"
    "    snapshot, {'query': 'alpha', 'min_context_length': 16000, 'limit': 1})\n"
    "modules = {name: getattr(module, '__file__', None)\n"
    "           for name, module in sys.modules.items()\n"
    "           if name.split('.')[0] == 'watchdog_catalog'}\n"
    "print(json.dumps({\n"
    "    'application_modules': modules,\n"
    "    'contract_names': [entry['name']\n"
    "                       for entry in json.loads(resource)],\n"
    "    'contract_sha256': hashlib.sha256(resource).hexdigest(),\n"
    "    'search': page,\n"
    "    'legacy_loaded': sorted({name.split('.')[0] for name in sys.modules}\n"
    "                            & blocked),\n"
    "    'prefix': sys.prefix,\n"
    "    'version': list(sys.version_info[:3]),\n"
    "    'sys_path': [entry for entry in sys.path if entry],\n"
    "}))\n"
)

#: Legacy producer/config/state roots that must never load in a child. The SDK
#: is deliberately NOT here: an ordinary transport run may use it.
INSTALLED_LEGACY_ROOTS = (
    "config_loader", "providers", "inference_watchdog", "catalog_export",
    "envfile", "notify", "diffing", "detection", "alive", "build_site",
    "pending_removals", "probe_select", "probe_state", "probe_zero_credit",
    "state",
)

#: Help/core paths additionally refuse the SDK graph: parsing help and importing
#: the pure core must not need it.
INSTALLED_SDK_ROOTS = ("mcp", "mcp_types", "pydantic")


# --------------------------------------------------------------------------
# group 3 helpers: prerequisites, environments, installs, probes
# --------------------------------------------------------------------------
class _ConsoleRun:
    """One completed child run: its REAL pid, exit status and captured streams."""

    def __init__(self, pid, completed):
        self.pid = pid
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr


def _host_probe_environment():
    """The minimal environment for a HOST-side interpreter probe."""
    return {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1"}


def _installed_python():
    """The exact CPython 3.11 interpreter the fresh environment is created from."""
    override = os.environ.get(INSTALLED_PYTHON_ENVIRONMENT)
    candidates = [override, "/home/kimbo/.local/bin/python3.11",
                  shutil.which("python3.11")]
    tried = []
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-I", "-c",
             "import json, sys; print(json.dumps(list(sys.version_info[:2])))"],
            capture_output=True, text=True, timeout=60,
            env=_host_probe_environment())
        if probe.returncode != 0:
            tried.append(f"{candidate}: cannot run ({probe.stderr.strip()[:200]})")
            continue
        if tuple(json.loads(probe.stdout)) != INSTALLED_MINOR:
            tried.append(f"{candidate}: {probe.stdout.strip()} is not exactly "
                         f"{'.'.join(str(part) for part in INSTALLED_MINOR)}")
            continue
        return str(candidate)
    raise AssertionError(
        "the installed-console evidence needs an exact CPython "
        f"{'.'.join(str(part) for part in INSTALLED_MINOR)} interpreter to "
        "create a fresh environment from; tried: " + "; ".join(tried)
        + f". Install one once outside pytest and/or set "
        f"{INSTALLED_PYTHON_ENVIRONMENT}=/path/to/python3.11.")


def _uv_binary():
    """The real installer frontend, or an actionable failure."""
    candidates = [os.environ.get(UV_ENVIRONMENT), shutil.which("uv"),
                  "/home/kimbo/.local/bin/uv"]
    tried = []
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        probe = subprocess.run([candidate, "--version"], capture_output=True,
                               text=True, timeout=60,
                               env=_host_probe_environment())
        if probe.returncode == 0 and probe.stdout.strip():
            return str(candidate), probe.stdout.strip()
        tried.append(f"{candidate}: {probe.stderr.strip()[:200]}")
    raise AssertionError(
        "the offline installer frontend is unavailable: install uv once outside "
        "pytest (`pipx install uv` or the standalone installer) or set "
        f"{UV_ENVIRONMENT}=/path/to/uv. Tried: " + "; ".join(tried))


def _dependency_wheelhouse(floor):
    """The owned genuine-wheel dependency source, or the exact preparation command."""
    override = os.environ.get(WHEELHOUSE_ENVIRONMENT)
    wheelhouse = Path(override) if override else DEFAULT_WHEELHOUSE
    names = (sorted(entry.name for entry in wheelhouse.iterdir()
                    if entry.suffix == ".whl")
             if wheelhouse.is_dir() else [])
    present = {}
    for name in names:
        parts = name[:-len(".whl")].split("-")
        if len(parts) >= 2:
            present.setdefault(parts[0].lower(), set()).add(parts[1])
    missing = {dist: version for dist, version in SDK_PIN_VERSIONS.items()
               if version not in present.get(dist, set())}
    if missing:
        command = (
            "env -i HOME=%s PATH=/usr/bin:/bin PIP_CONFIG_FILE=/dev/null "
            "PIP_DISABLE_PIP_VERSION_CHECK=1 %s -m pip download "
            "--only-binary=:all: --dest %s --index-url https://pypi.org/simple "
            "%s" % (wheelhouse.parent, floor, wheelhouse,
                    " ".join(f"'{pin}'" for pin in SDK_PINS)))
        raise AssertionError(
            "the offline dependency wheelhouse is not prepared (missing "
            f"{sorted(missing)} in {wheelhouse}, found {len(names)} wheels). "
            "This test downloads nothing itself. Prepare it ONCE outside "
            "pytest, from genuine PyPI wheels:\n  " + command
            + f"\n(or set {WHEELHOUSE_ENVIRONMENT}=<prepared directory>).")
    return wheelhouse


def _tool_environment(home, temp):
    """The explicit minimal environment for every installer/build subprocess."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(temp),
        "UV_CACHE_DIR": str(Path(home) / "uv-cache"),
        "UV_NO_CONFIG": "1",
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _probe_environment(home, cache_home, temp):
    """The minimal credential-free environment of an installed child."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache_home),
        "TMPDIR": str(temp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _installed_environment(home, cache_home, temp, hooks, report, audit,
                           feed_port=None):
    """A credential-free child environment with ONLY the owned probe hook path."""
    environment = _probe_environment(home, cache_home, temp)
    environment["PYTHONPATH"] = str(hooks)
    environment["WD_PROBE_REPORT"] = str(report)
    environment["WD_GUARD_AUDIT"] = str(audit)
    if feed_port is not None:
        environment["WD_ALLOWED_FEED_PORT"] = str(feed_port)
    return environment


def _write_hook(directory, blocked):
    """Write the owned probe hook into its own directory and return that path."""
    directory.mkdir(parents=True, exist_ok=True)
    text = (_INSTALLED_HOOK
            .replace("__BLOCKED__", repr(tuple(sorted(blocked))))
            .replace("__HOOK_ID__", repr(INSTALLED_HOOK_ID)))
    (directory / "sitecustomize.py").write_text(text, encoding="utf-8")
    return directory


def _write_log(path, completed, command=None):
    """Retain an installer/build transcript, whatever the verdict."""
    chunks = []
    if command is not None:
        chunks.append("COMMAND: " + json.dumps([str(part) for part in command]))
    chunks.append("RETURNCODE: %r" % (completed.returncode,))
    chunks.append("--- STDOUT ---\n" + (completed.stdout or ""))
    chunks.append("--- STDERR ---\n" + (completed.stderr or ""))
    Path(path).write_text("\n".join(chunks), encoding="utf-8")


def _create_environment(uv, floor, directory, log_path, home, temp):
    """Create a NEW PEP 405 environment and prove its isolation facts."""
    completed = subprocess.run(
        [uv, "venv", "--no-config", "--no-python-downloads", "--python",
         str(floor), str(directory)],
        capture_output=True, text=True, timeout=300,
        env=_tool_environment(home, temp))
    _write_log(log_path, completed)
    assert completed.returncode == 0, (
        "the fresh environment could not be created:\n"
        + completed.stderr[-4000:])
    config = (directory / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "include-system-site-packages = false" in config, config
    interpreter = directory / "bin" / "python"
    assert interpreter.exists(), config
    probe = subprocess.run(
        [str(interpreter), "-I", "-B", "-c", _VERSION_PROBE],
        cwd=str(directory), capture_output=True, text=True, timeout=120,
        env=_tool_environment(home, temp))
    assert probe.returncode == 0, probe.stderr[-4000:]
    facts = json.loads(probe.stdout)
    assert tuple(facts["version"][:2]) == INSTALLED_MINOR, facts["version"]
    assert os.path.realpath(facts["prefix"]) == os.path.realpath(str(directory))
    assert os.path.realpath(facts["base_prefix"]) != os.path.realpath(
        facts["prefix"])
    assert Path(facts["executable"]) == interpreter, facts["executable"]
    purelib = Path(facts["purelib"])
    assert os.path.realpath(purelib).startswith(
        os.path.realpath(str(directory)) + os.sep), purelib
    host_roots = {os.path.realpath(sys.prefix), os.path.realpath(str(REPO))}
    site_entries = [entry for entry in facts["sys_path"]
                    if entry.endswith("site-packages")]
    assert [os.path.realpath(entry) for entry in site_entries] == [
        os.path.realpath(purelib)], site_entries
    for entry in facts["sys_path"]:
        assert os.path.realpath(entry) not in host_roots, (
            "the fresh environment can see a host path: %r" % (entry,))
    return facts


def _install_offline(uv, env_python, wheel, wheelhouse, log_path, home, temp):
    """Install the REAL wheel offline; the app's own metadata drives resolution."""
    assert wheel.suffix == ".whl" and wheel.is_file(), (
        "only a built wheel may be installed, never a source/editable tree: "
        f"{wheel}")
    command = [uv, "pip", "install", "--no-config", "--offline", "--no-index",
               "--find-links", str(wheelhouse), "--no-cache",
               "--link-mode=copy", "--strict", "--python", str(env_python),
               str(wheel), *SDK_PINS]
    completed = subprocess.run(command, capture_output=True, text=True,
                               timeout=600,
                               env=_tool_environment(home, temp))
    _write_log(log_path, completed, command)
    assert completed.returncode == 0, (
        "the offline install of the built wheel failed:\n"
        + completed.stderr[-6000:])
    return completed


def _run_console(argv, environment, cwd, *, timeout=180):
    """Run one child by absolute path with an owned, credential-free environment."""
    process = subprocess.Popen(
        [str(part) for part in argv], cwd=str(cwd), env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AssertionError(
            "the child did not exit within %ss: %r" % (timeout, [str(p) for p in argv]))
    return _ConsoleRun(process.pid,
                       subprocess.CompletedProcess(argv, process.returncode,
                                                   stdout, stderr))


def _installed_facts(env_python, cwd, home, temp):
    """Read the installed distribution's real metadata in the fresh environment."""
    completed = subprocess.run(
        [str(env_python), "-I", "-B", "-c", _INSTALLED_FACTS_PROBE, DIST_NAME,
         CONSOLE_COMMAND, ",".join(SDK_PIN_VERSIONS)],
        cwd=str(cwd), capture_output=True, text=True, timeout=180,
        env=_tool_environment(home, temp))
    assert completed.returncode == 0, completed.stderr[-4000:]
    assert completed.stderr == "", completed.stderr[-2000:]
    return json.loads(completed.stdout)


def _installed_module_hashes(wheel):
    """Every packaged module's dotted name mapped to its wheel-byte SHA-256."""
    modules = {}
    files = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if not name.startswith("watchdog_catalog/") or name.endswith("/"):
                continue
            digest = hashlib.sha256(archive.read(name)).hexdigest()
            files[name] = digest
            if name.endswith(".py"):
                modules[name[:-len(".py")].replace("/", ".")] = digest
    modules["watchdog_catalog"] = files["watchdog_catalog/__init__.py"]
    return modules, files


def _entry_point_target(facts):
    """The single console target the built wheel declares."""
    targets = [line.split("=", 1)[1].strip()
               for line in facts["entry_points"].splitlines()
               if line.strip().startswith(CONSOLE_COMMAND + " =")]
    assert len(targets) == 1, facts["entry_points"]
    return targets[0]


def _assert_guard_clean(audit_path, label):
    """No probe ever attempted traffic other than its owned loopback feed."""
    path = Path(audit_path)
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8").strip()
    assert text == "", (
        f"{label}: the network guard rejected outbound traffic:\n"
        + text[:4000])


def _assert_installed_origin(report_path, *, label, pid, cwd, env_prefix, purelib,
                             module_hashes, required_modules, absent_modules=(),
                             blocked_legacy=(), blocked_sdk=()):
    """Prove THIS child imported only the INSTALLED package from site-packages.

    Every claim comes from the child's own report -- its pid, cwd, interpreter,
    prefix and per-module digests -- never from the launch command. A checkout
    or wheel-archive origin fails even when the module bytes happen to be
    identical, because the ORIGIN is what an installed run is evidence for.
    """
    report_path = Path(report_path)
    assert report_path.is_file(), (
        f"{label}: not an installed-origin run: the probe wrote no report at "
        f"{report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["hook"] == INSTALLED_HOOK_ID, (
        f"{label}: not an installed-origin run: the owned hook did not run")
    assert report["pid"] == pid, (
        f"{label}: not an installed-origin run: the report came from pid "
        f"{report['pid']}, not from the launched child {pid}")
    assert os.path.realpath(report["cwd"]) == os.path.realpath(str(cwd)), (
        f"{label}: not an installed-origin run: cwd {report['cwd']!r} is not "
        f"{str(cwd)!r}")
    assert tuple(report["version"][:2]) == INSTALLED_MINOR, report["version"]
    assert os.path.realpath(report["prefix"]) == os.path.realpath(
        str(env_prefix)), (
        f"{label}: not an installed-origin run: prefix {report['prefix']!r} is "
        f"not the fresh environment {str(env_prefix)!r}")
    assert os.path.realpath(report["base_prefix"]) != os.path.realpath(
        report["prefix"]), (
        f"{label}: not an installed-origin run: this is no virtual environment")
    assert os.path.normpath(report["executable"]) == os.path.normpath(
        os.path.join(str(env_prefix), "bin", "python")), report["executable"]

    package_dir = os.path.realpath(
        os.path.join(os.path.realpath(str(purelib)), "watchdog_catalog"))
    modules = report["application_modules"]
    assert "watchdog_catalog" in modules, (
        f"{label}: not an installed-origin run: the application never imported")
    for name in sorted(required_modules):
        assert name in modules, (
            f"{label}: not an installed-origin run: {name} never loaded "
            f"(loaded {sorted(modules)})")
    for name in sorted(absent_modules):
        assert name not in modules, (
            f"{label}: not an installed-origin run: {name} must not have loaded")
    for name in sorted(modules):
        fact = modules[name]
        path = fact["file"]
        assert path, f"{label}: not an installed-origin run: {name} has no origin"
        assert ".whl" not in path, (
            f"{label}: not an installed-origin run: {name} came from the wheel "
            f"archive {path!r}")
        assert os.path.realpath(path).startswith(package_dir + os.sep), (
            f"{label}: not an installed-origin run: {name} imported from "
            f"{path!r}, outside the installed site-packages {package_dir!r} "
            f"(sys.path was {report['sys_path']!r})")
        assert fact["sha256"] == module_hashes[name], (
            f"{label}: {name} bytes differ from the installed wheel "
            f"({fact['sha256']} != {module_hashes[name]})")
    for entry in report["sys_path"]:
        assert ".whl" not in entry, (
            f"{label}: not an installed-origin run: sys.path exposes the wheel "
            f"archive {entry!r}")
        assert not os.path.realpath(entry).startswith(
            os.path.realpath(str(REPO)) + os.sep), (
                f"{label}: not an installed-origin run: sys.path exposes the "
                f"checkout: {entry!r}")
    loaded_roots = set(report["loaded_roots"])
    assert loaded_roots.isdisjoint(blocked_legacy), (
        f"{label}: legacy modules loaded: "
        f"{sorted(loaded_roots & set(blocked_legacy))}")
    assert loaded_roots.isdisjoint(blocked_sdk), (
        f"{label}: SDK modules loaded where they must not be: "
        f"{sorted(loaded_roots & set(blocked_sdk))}")
    assert report["rejected_connections"] == [], (
        f"{label}: the network guard rejected traffic: "
        f"{report['rejected_connections']}")
    return report


def _with_report(environment, *, report, audit):
    """A SCOPED copy of a child environment pointed at its OWN evidence paths.

    The owned hook writes its report from an ``atexit`` handler, so it fires on
    EVERY interpreter exit -- including a blocked-import failure. Two children
    started from one environment dictionary would therefore leave only the
    LAST report on disk, which is how a guard control silently destroys the
    console evidence it was meant to protect. Callers derive a fresh copy with
    unique destinations instead of mutating a shared dictionary.
    """
    scoped = dict(environment)
    scoped["WD_PROBE_REPORT"] = str(report)
    scoped["WD_GUARD_AUDIT"] = str(audit)
    return scoped


def _script_argv(*arguments):
    """The ``sys.argv`` a ``python -c SCRIPT [args...]`` child will observe."""
    return ["-c"] + [str(argument) for argument in arguments]


def _report_entry(index, *, label, kind, run, report, audit, argv):
    """Attribute one hook-enabled invocation to its OWN report destination."""
    index.append({"label": label, "kind": kind, "report": str(report),
                  "audit": str(audit), "pid": run.pid,
                  "argv": [str(part) for part in argv]})


def _assert_report_index(index, evidence):
    """Prove every guarded invocation still owns its report, on disk, at the end.

    Uniqueness of the destinations is what makes accidental reuse impossible,
    and the PID/argv reconciliation is what detects an overwrite that slipped
    through: a surviving report would carry another child's pid or argv, so this
    is a real attribution check rather than a count of files.
    """
    for field, values in (
            ("label", [entry["label"] for entry in index]),
            ("report", [entry["report"] for entry in index]),
            ("audit", [entry["audit"] for entry in index])):
        assert len(set(values)) == len(values), (
            f"two invocations share a {field} destination, so one would "
            f"overwrite the other: "
            f"{sorted({value for value in values if values.count(value) > 1})}")
    for entry in index:
        path = Path(entry["report"])
        assert path.parent == Path(evidence), (entry["label"], str(path))
        assert path.is_file(), (
            f"{entry['label']}: the {entry['kind']} invocation lost its report "
            f"at {path}")
        report = json.loads(path.read_text(encoding="utf-8"))
        assert report["hook"] == INSTALLED_HOOK_ID, (
            f"{entry['label']}: the retained report was not written by the "
            "owned hook")
        assert report["pid"] == entry["pid"], (
            f"{entry['label']}: the retained report belongs to pid "
            f"{report['pid']}, not to the {entry['kind']} child {entry['pid']} "
            "-- an invocation overwrote another's evidence")
        assert report["argv"] == entry["argv"], (
            f"{entry['label']}: the retained argv {report['argv']!r} is not this "
            f"{entry['kind']} invocation's {entry['argv']!r}")
        _assert_guard_clean(entry["audit"], entry["label"])
    return [entry["label"] for entry in index]


def _assert_shadow_console_report(report_path, *, run, console, module_hashes):
    """The negative control's OWN saved state: real console pid/argv, checkout bytes."""
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    assert report["hook"] == INSTALLED_HOOK_ID, (
        "the negative control must keep the guards active")
    assert report["pid"] == run.pid, (
        f"the saved shadow report belongs to pid {report['pid']}, not to the "
        f"shadow console child {run.pid}")
    assert report["argv"] == [str(console), "--help"], report["argv"]
    module = report["application_modules"]["watchdog_catalog"]
    assert module["file"] == str(REPO / "watchdog_catalog" / "__init__.py"), (
        module)
    assert module["sha256"] == module_hashes["watchdog_catalog"], (
        "the negative control needs IDENTICAL module bytes")
    return report


def _assert_retained_session(summary, *, label):
    """Validate a RETAINED ``.session.json`` record, as a later reader sees it.

    This is the DISK/MANIFEST boundary. The reused unit-6d helpers already call
    ``_audit_clean_stdio``, which rejects a LIVE ``forced_cleanup``; nothing in
    that live audit survives into the saved record, and a drain timeout can set
    ``forced_cleanup=True`` while ``natural_exit`` stays true. Graceful EOF is
    therefore only claimed here when the SAVED record says nothing was killed.
    """
    assert summary["returncode"] == 0, (label, summary["returncode"])
    assert summary["natural_exit"] is True, (
        f"{label}: the retained session records no natural exit")
    assert summary["stdin_closed"] is True, (
        f"{label}: the retained session never closed the child's stdin")
    assert summary["protocol_violations"] == [], (
        label, summary["protocol_violations"])
    assert summary["forced_cleanup"] is False, (
        f"{label}: the retained session records forced_cleanup="
        f"{summary['forced_cleanup']!r}; a bounded drain or exit timeout "
        "force-killed the child, so this record does not prove a graceful EOF")
    return summary


def _test_identity(harness):
    """SHA-256 of the two test files THIS run actually executed."""
    distribution = Path(__file__).resolve()
    protocol = Path(harness.__file__).resolve()
    assert distribution == (REPO / "tests" / "test_distribution.py").resolve(), (
        distribution)
    assert protocol == (REPO / "tests" / "test_mcp_protocol.py").resolve(), protocol
    return {
        "distribution_test": {
            "path": str(distribution),
            "sha256": hashlib.sha256(distribution.read_bytes()).hexdigest()},
        "protocol_harness": {
            "path": str(protocol),
            "sha256": hashlib.sha256(protocol.read_bytes()).hexdigest()},
    }


def _protocol_harness():
    """Load the reviewed group-5/6 stdio harness explicitly in THIS process.

    Importing the reviewed test module here creates no tests (collection is
    file-based) and reuses its real ``ClientSession`` stream adapter, raw audit,
    independent-clock comparators and loopback feed unchanged. Nothing from it
    is ever placed in a server child's environment.
    """
    try:
        import tests.test_mcp_protocol as harness
    except ImportError as error:  # pragma: no cover - broken dev environment
        raise AssertionError(
            "the reviewed stdio harness tests/test_mcp_protocol.py cannot be "
            f"loaded in the host: {error}") from error
    for attribute in ("_StdioChild", "_file_stdio_session", "_LoopbackFeed",
                      "_first_url_stdio_session", "_offline_url_stdio_session",
                      "_unavailable_stdio_session", "DiskCache", "SNAPSHOT",
                      "anyio", "BLOCKED_FOR_CLI", "BLOCKED_LEGACY_ONLY"):
        assert hasattr(harness, attribute), (
            f"the reviewed stdio harness lost {attribute!r}")
    return harness


def _installed_child(harness, label, argv, environment, cwd, artifacts):
    return harness._StdioChild(label, [str(part) for part in argv],
                               environment, cwd, artifacts)


def _installed_url_child(harness, label, console, url, cache_dir, environment,
                         cwd, artifacts):
    child = _installed_child(
        harness, label,
        [console, "--catalog-url", url, "--cache-dir", str(cache_dir)],
        environment, cwd, artifacts)
    child.url = url
    child.cache_dir = cache_dir
    return child


def _directory_entries(*directories):
    """The immediate entries of each owned directory: a child's side-effect row.

    Snapshotted around ONE invocation, so a difference is attributable to that
    child rather than to whatever earlier control happened to run first.
    """
    return {str(directory): sorted(entry.name for entry in directory.iterdir())
            for directory in directories}


def _owned_fixture(path):
    """An owned, validated copy of the reviewed fixture input."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = SNAPSHOT_FIXTURE.read_bytes()
    path.write_bytes(data)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(
        data).hexdigest()
    return path


def _dependency_manifest(wheelhouse):
    entries = []
    for path in sorted(wheelhouse.glob("*.whl")):
        entries.append({"name": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size": path.stat().st_size})
    return entries


def _variant_wheel(directory, label, *, entry_point=None, drop_resource=False):
    """Build one real packaging variant of the UNCHANGED application payload."""
    stage = _stage_source(directory / (label + "-source"))
    if entry_point is not None:
        text = (stage / "pyproject.toml").read_text(encoding="utf-8")
        declared = f'{CONSOLE_COMMAND} = "{CONSOLE_TARGET}"'
        assert declared in text, "the staged metadata lost its console declaration"
        (stage / "pyproject.toml").write_text(
            text.replace(declared, f'{CONSOLE_COMMAND} = "{entry_point}"'),
            encoding="utf-8")
    if drop_resource:
        (stage / "watchdog_catalog" / "tools.json").unlink()
    return _build_wheel(stage, directory / (label + "-wheel"))


def test_installed_console_clean_install_and_stdio_isolation():
    """The REAL wheel, clean-installed, driven through its own console command."""
    assert PYPROJECT.is_file(), "the wheel needs the committed packaging metadata"
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    version = project["version"]
    from watchdog_catalog.query import search_models as host_search_models
    from watchdog_catalog.source import DEFAULT_CATALOG_URL

    floor = _installed_python()
    uv, uv_version = _uv_binary()
    wheelhouse = _dependency_wheelhouse(floor)
    build_python, backend_version = _build_python()
    harness = _protocol_harness()
    blocked_legacy = tuple(harness.BLOCKED_LEGACY_ONLY)
    blocked_sdk = INSTALLED_SDK_ROOTS
    blocked_help = blocked_sdk + blocked_legacy + ("watchdog_catalog.server",)

    root = _scratch_directory("unit6e-installed-")
    evidence = root / "evidence"
    neutral = root / "cwd"
    home = root / "home"
    tool_home = root / "tool-home"
    cache_home = root / "xdg-cache"
    temp = root / "tmp"
    artifacts = evidence / "sessions"
    for directory in (evidence, neutral, home, tool_home, cache_home, temp,
                      artifacts):
        directory.mkdir(parents=True)

    # ------------------------------------------------------------------
    # 1. stage, build and audit the EXACT wheel this group installs
    # ------------------------------------------------------------------
    stage = _stage_source(root / "source")
    backend_log = evidence / "backend-build.log"
    wheel = _build_wheel(stage, root / "wheel", log_path=backend_log)
    assert wheel.name == (
        f"free_inference_watchdog_mcp-{version}-py3-none-any.whl"), wheel.name
    # The retained transcript must belong to THIS wheel: it is the declared
    # backend's own command/status/streams for the exact file installed below,
    # not the independent frontend build that runs later on a separate export.
    transcript = backend_log.read_text(encoding="utf-8")
    assert "RETURNCODE: 0" in transcript, transcript[-4000:]
    assert "COMMAND: " in transcript and str(wheel.parent) in transcript, (
        transcript[:2000])
    assert "--- STDOUT ---" in transcript and "--- STDERR ---" in transcript
    assert wheel.name in transcript, (
        "the backend transcript does not name the wheel it produced: "
        + transcript[-4000:])
    facts = _read_wheel(wheel)
    expected_python, expected_resource = _expected_payload()
    violations = _payload_violations(facts, expected_python, expected_resource)
    assert violations == [], (
        "the wheel this group installs is not the reviewed payload:\n"
        + "\n".join(violations))
    top_levels = {name.split("/")[0] for name in facts["names"]}
    assert top_levels == {"watchdog_catalog", facts["prefix"]}, top_levels
    wheel_sha = hashlib.sha256(wheel.read_bytes()).hexdigest()
    module_hashes, wheel_file_hashes = _installed_module_hashes(wheel)
    assert _entry_point_target(facts) == CONSOLE_TARGET
    # The wheel's package bytes are the REVIEWED SOURCES' bytes, so the later
    # installed-origin digests are anchored in the checkout only at build time.
    for name, digest in wheel_file_hashes.items():
        if name.endswith(".py"):
            reviewed = PACKAGE / name.split("/")[-1]
            assert hashlib.sha256(reviewed.read_bytes()).hexdigest() == digest, (
                f"the wheel's {name} differs from the reviewed source")
    assert facts["tools_json"] == CONTRACT_RESOURCE.read_bytes()

    # The OWNED staged source is hidden after the build: every later launch must
    # work without it. No live checkout is touched.
    hidden = stage.with_name("staged-source-hidden")
    os.rename(stage, hidden)
    assert not stage.exists() and hidden.is_dir()
    stage = hidden

    # ------------------------------------------------------------------
    # 2. a genuinely fresh Python 3.11 environment, offline install
    # ------------------------------------------------------------------
    environment_dir = root / "fresh-3.11"
    created = _create_environment(
        uv, floor, environment_dir, evidence / "create-environment.log",
        tool_home, temp)
    env_python = environment_dir / "bin" / "python"
    console = environment_dir / "bin" / CONSOLE_COMMAND
    _install_offline(uv, env_python, wheel, wheelhouse,
                     evidence / "install.log", tool_home, temp)
    installed = _installed_facts(env_python, neutral, tool_home, temp)
    purelib = Path(installed["purelib"])

    assert installed["version"] == version, installed["version"]
    assert tuple(installed["version_info"][:2]) == INSTALLED_MINOR
    assert Path(installed["prefix"]).resolve() == environment_dir.resolve()
    assert Path(installed["prefix"]).resolve() != Path(
        installed["base_prefix"]).resolve()
    assert installed["entry_points"] == [{
        "name": CONSOLE_COMMAND, "value": CONSOLE_TARGET,
        "group": "console_scripts"}], installed["entry_points"]
    assert installed["installer"] == "uv", installed["installer"]
    assert installed["versions"] == SDK_PIN_VERSIONS, installed["versions"]
    dependency = {}
    for requirement in installed["requires"]:
        name, spec = _requirement(requirement)
        dependency[name] = set(spec.split(","))
    assert dependency == {"mcp": {">=2", "<3"}}, installed["requires"]
    assert installed["markers"] == [], installed["markers"]
    assert installed["console"] == str(console), installed["console"]
    assert installed["console_shebang"] == f"#!{env_python}", (
        installed["console_shebang"])
    assert installed["console_executable"] is True
    assert installed["direct_url"] is not None, (
        "the installer recorded no provenance for the application wheel")
    assert "archive_info" in installed["direct_url"], installed["direct_url"]
    assert installed["direct_url"]["url"].endswith(".whl"), (
        "the application was not installed from a wheel: "
        f"{installed['direct_url']}")
    assert "dir_info" not in installed["direct_url"]

    # The installed application FILE SET and BYTES are the wheel's, exactly.
    package_dir = purelib / "watchdog_catalog"
    installed_files = {path.name: path.read_bytes()
                       for path in package_dir.iterdir() if path.is_file()}
    packaged_files = {name.split("/")[-1]: data
                      for name, data in wheel_file_hashes.items()}
    assert set(installed_files) == set(packaged_files), (
        sorted(installed_files), sorted(packaged_files))
    for name, digest in wheel_file_hashes.items():
        assert hashlib.sha256(
            installed_files[name.split("/")[-1]]).hexdigest() == digest, name
    assert installed_files["tools.json"] == expected_resource

    # ------------------------------------------------------------------
    # 3. installed --help without a feed (SDK and legacy graphs blocked)
    # ------------------------------------------------------------------
    help_hooks = _write_hook(root / "hooks" / "help", blocked_help)
    help_env = _installed_environment(
        home, cache_home, temp, help_hooks, evidence / "help-report.json",
        evidence / "help-guard.jsonl")
    # Every hook-enabled invocation below owns a DISTINCT report/audit pair; the
    # index is reconciled against the saved files again at the end of the group.
    reports = []
    help_argv = [str(console), "--help"]
    help_run = _run_console(help_argv, help_env, neutral)
    _report_entry(reports, label="help", kind="console --help", run=help_run,
                  report=evidence / "help-report.json",
                  audit=evidence / "help-guard.jsonl", argv=help_argv)
    assert help_run.returncode == 0, help_run.stderr[-4000:]
    assert help_run.stderr == "", "help must keep stderr empty"
    help_stdout = help_run.stdout
    assert help_stdout.splitlines()[0].startswith(
        f"usage: {CONSOLE_COMMAND}"), help_stdout[:400]
    for fragment in ("--catalog-url URL | --catalog-file PATH",
                     "--cache-dir DIR", DEFAULT_CATALOG_URL):
        assert fragment in help_stdout, (fragment, help_stdout[:1200])
    assert "--root" not in help_stdout, "the installed command has no --root"
    assert sorted(neutral.iterdir()) == [], "help wrote into its cwd"
    assert sorted(home.iterdir()) == [], "help created HOME state"
    assert sorted(cache_home.iterdir()) == [], "help created cache state"
    _assert_guard_clean(evidence / "help-guard.jsonl", "installed --help")
    help_report = _assert_installed_origin(
        evidence / "help-report.json", label="installed --help",
        pid=help_run.pid, cwd=neutral, env_prefix=environment_dir,
        purelib=purelib, module_hashes=module_hashes,
        required_modules={"watchdog_catalog", "watchdog_catalog.__main__",
                          "watchdog_catalog.source"},
        absent_modules=("watchdog_catalog.server",) + blocked_sdk,
        blocked_legacy=blocked_legacy, blocked_sdk=blocked_sdk)

    # The SDK blocker is ACTIVE in that exact environment: a positive control.
    # Each control is a SEPARATE hook-enabled child, so each gets its own
    # report/audit pair: the hook writes on exit, and reusing the console's
    # destination here would replace the help evidence with a ``-c`` child.
    for module in ("mcp", "pydantic"):
        sdk_report = evidence / f"help-sdk-{module}-report.json"
        sdk_audit = evidence / f"help-sdk-{module}-guard.jsonl"
        control_env = _with_report(help_env, report=sdk_report, audit=sdk_audit)
        control = _run_console([env_python, "-B", "-c", f"import {module}"],
                               control_env, neutral)
        assert control.returncode != 0, f"{module} was importable in help mode"
        assert (f"blocked import during installed-console probe: {module}"
                in control.stderr), control.stderr[-2000:]
        assert control.stdout == ""
        control_report = json.loads(sdk_report.read_text(encoding="utf-8"))
        assert control_report["pid"] == control.pid, control_report["pid"]
        assert control_report["application_modules"] == {}, (
            f"the {module} blocker did not raise before any package code: "
            f"{control_report['application_modules']}")
        _report_entry(reports, label=f"help-sdk-{module}",
                      kind=f"SDK blocker control ({module})", run=control,
                      report=sdk_report, audit=sdk_audit, argv=_script_argv())

    # An explicit MISSING file selection followed by --help still succeeds:
    # parsing and help happen before any source validation or catalogue read.
    # This invocation keeps its OWN evidence: a distinct report/audit pair, the
    # same installed-origin checker as the primary help run, its own stderr and
    # side-effect snapshots taken around THIS child rather than inherited.
    missing = root / "inputs" / "absent-catalogue.json"
    ordered_argv = [str(console), "--catalog-file", str(missing), "--help"]
    ordered_env = _with_report(
        help_env, report=evidence / "help-missing-file-report.json",
        audit=evidence / "help-missing-file-guard.jsonl")
    ordered_before = _directory_entries(neutral, home, cache_home)
    ordered = _run_console(ordered_argv, ordered_env, neutral)
    ordered_after = _directory_entries(neutral, home, cache_home)
    _report_entry(reports, label="help-missing-file",
                  kind="console missing-file --help", run=ordered,
                  report=evidence / "help-missing-file-report.json",
                  audit=evidence / "help-missing-file-guard.jsonl",
                  argv=ordered_argv)
    assert ordered.returncode == 0, ordered.stderr[-4000:]
    assert ordered.stderr == "", "missing-file help must keep stderr empty"
    assert ordered.stdout == help_stdout, "help depends on the selection order"
    # No catalogue read, fetch or cache creation for THIS child: the owned
    # directories are unchanged and the selection's own directory was never
    # created. The guard audit below independently forbids any network attempt.
    changed = {key: (ordered_before[key], ordered_after[key])
               for key in ordered_after
               if ordered_before[key] != ordered_after[key]}
    assert ordered_after == ordered_before, (
        "the missing-file help wrote cwd/HOME/cache state: " + str(changed))
    assert not missing.parent.exists(), (
        "the missing-file help created the selection's directory: "
        f"{missing.parent}")
    assert not missing.exists()
    _assert_guard_clean(evidence / "help-missing-file-guard.jsonl",
                        "missing-file --help")
    # Same checker, same environment, this child's pid: the run is installed
    # evidence about the installed console, not about the checkout.
    ordered_report = _assert_installed_origin(
        evidence / "help-missing-file-report.json",
        label="missing-file --help", pid=ordered.pid, cwd=neutral,
        env_prefix=environment_dir, purelib=purelib,
        module_hashes=module_hashes,
        required_modules={"watchdog_catalog", "watchdog_catalog.__main__",
                          "watchdog_catalog.source"},
        absent_modules=("watchdog_catalog.server",) + blocked_sdk,
        blocked_legacy=blocked_legacy, blocked_sdk=blocked_sdk)
    assert ordered_report["argv"] == [str(part) for part in ordered_argv], (
        ordered_report["argv"])

    # ------------------------------------------------------------------
    # 4. the installed pure core/resource with SDK and legacy imports blocked
    # ------------------------------------------------------------------
    fixture = _owned_fixture(root / "inputs" / "fixture-basic.json")
    core_hooks = _write_hook(root / "hooks" / "core", blocked_help)
    core_env = _installed_environment(
        home, cache_home, temp, core_hooks, evidence / "core-report.json",
        evidence / "core-guard.jsonl")
    core_run = _run_console(
        [env_python, "-B", "-c", _INSTALLED_CORE_PROBE, str(fixture),
         ",".join(blocked_help)], core_env, neutral)
    _report_entry(reports, label="core", kind="installed pure core probe",
                  run=core_run, report=evidence / "core-report.json",
                  audit=evidence / "core-guard.jsonl",
                  argv=_script_argv(fixture, ",".join(blocked_help)))
    assert core_run.returncode == 0, core_run.stderr[-4000:]
    assert core_run.stderr == "", core_run.stderr[-2000:]
    core = json.loads(core_run.stdout)
    core_modules = core["application_modules"]
    assert set(core_modules) == {
        "watchdog_catalog", "watchdog_catalog.catalog",
        "watchdog_catalog.query", "watchdog_catalog.source"}, sorted(core_modules)
    assert "watchdog_catalog.server" not in core_modules, (
        "the installed pure core imported the SDK transport")
    assert core["legacy_loaded"] == [], core["legacy_loaded"]
    for name, path in core_modules.items():
        assert os.path.realpath(path).startswith(
            os.path.realpath(str(package_dir)) + os.sep), (name, path)
    assert core["contract_sha256"] == hashlib.sha256(
        expected_resource).hexdigest()
    assert core["contract_names"] == [entry["name"]
                                      for entry in json.loads(expected_resource)]
    host_page = host_search_models(
        harness.SNAPSHOT, {"query": "alpha", "min_context_length": 16000,
                           "limit": 1})
    assert core["search"] == host_page, (
        "the installed core's result differs from the accepted host core")
    assert [row["model_id"] for row in core["search"]["endpoints"]] == [
        "alpha-1:free"]
    assert core["search"]["filters"] == {
        "provider": None, "min_context_length": 16000,
        "input_modalities": None, "supported_parameters": None}
    assert tuple(core["version"][:2]) == INSTALLED_MINOR
    assert os.path.realpath(core["prefix"]) == os.path.realpath(
        str(environment_dir))
    assert all(os.path.realpath(entry) != os.path.realpath(str(REPO))
               for entry in core["sys_path"]), core["sys_path"]
    _assert_guard_clean(evidence / "core-guard.jsonl", "installed core import")
    _assert_installed_origin(
        evidence / "core-report.json", label="installed core import",
        pid=core_run.pid, cwd=neutral, env_prefix=environment_dir,
        purelib=purelib, module_hashes=module_hashes,
        required_modules=set(core_modules),
        absent_modules=("watchdog_catalog.server",) + blocked_sdk,
        blocked_legacy=blocked_legacy, blocked_sdk=blocked_sdk)

    # ------------------------------------------------------------------
    # 5. no-default-feed stdin EOF launch: lazy, silent, no acquisition
    # ------------------------------------------------------------------
    serve_hooks = _write_hook(root / "hooks" / "serve", blocked_legacy)
    eof_env = _installed_environment(
        home, cache_home, temp, serve_hooks, evidence / "eof-report.json",
        evidence / "eof-guard.jsonl")
    eof = _run_console([console], eof_env, neutral)
    _report_entry(reports, label="eof", kind="no-feed stdin-EOF launch",
                  run=eof, report=evidence / "eof-report.json",
                  audit=evidence / "eof-guard.jsonl", argv=[str(console)])
    assert eof.returncode == 0, eof.stderr[-4000:]
    assert eof.stdout == "", "a no-feed EOF launch printed into the protocol stream"
    assert eof.stderr == "", eof.stderr[-2000:]
    _assert_guard_clean(evidence / "eof-guard.jsonl", "no-feed EOF launch")
    _assert_installed_origin(
        evidence / "eof-report.json", label="no-feed EOF launch",
        pid=eof.pid, cwd=neutral, env_prefix=environment_dir, purelib=purelib,
        module_hashes=module_hashes,
        required_modules={"watchdog_catalog", "watchdog_catalog.__main__",
                          "watchdog_catalog.server", "watchdog_catalog.source"},
        blocked_legacy=blocked_legacy)
    assert sorted(neutral.iterdir()) == [], "the launch wrote into its cwd"
    assert sorted(home.iterdir()) == [], "the launch created HOME state"
    assert sorted(cache_home.iterdir()) == [], (
        "a no-feed launch created cache state before any query")

    # ------------------------------------------------------------------
    # 6. installed console file session: real SDK client, full 6d coverage
    # ------------------------------------------------------------------
    sessions = {}

    def record_session(label, child, environment):
        minimal = {"PATH", "HOME", "XDG_CACHE_HOME", "TMPDIR", "PYTHONPATH",
                   "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE",
                   "WD_PROBE_REPORT", "WD_GUARD_AUDIT"}
        assert set(environment) in (minimal, minimal | {"WD_ALLOWED_FEED_PORT"}), (
            "a server child environment must stay minimal and credential-free: "
            + str(sorted(environment)))
        assert environment["PYTHONPATH"] == str(serve_hooks), (
            "a server child must never see a test/helper/repo path")
        _assert_guard_clean(environment["WD_GUARD_AUDIT"], label)
        report = _assert_installed_origin(
            environment["WD_PROBE_REPORT"], label=label, pid=child.pid,
            cwd=neutral, env_prefix=environment_dir, purelib=purelib,
            module_hashes=module_hashes,
            required_modules={"watchdog_catalog", "watchdog_catalog.__main__",
                              "watchdog_catalog.server",
                              "watchdog_catalog.source",
                              "watchdog_catalog.query",
                              "watchdog_catalog.catalog"},
            blocked_legacy=blocked_legacy)
        sessions[label] = {
            "pid": child.pid,
            "argv": list(child.argv),
            "cwd": str(child.cwd),
            "returncode": child.proc.returncode,
            "natural_exit": child.natural_exit,
            "stdin_closed": child.stdin_closed,
            "forced_cleanup": child.forced_cleanup,
            "protocol_violations": list(child.protocol_violations),
            "stdout_frames": len(child.stdout_lines),
            "outbound_frames": len(child.stdin_lines),
            "stdout_bytes": len(child.raw_stdout),
            "artifacts": sorted(str(path) for path in artifacts.glob(label + ".*")),
            "report": environment["WD_PROBE_REPORT"],
            "sys_path": report["sys_path"],
            "import_origins": {name: fact["file"]
                               for name, fact in
                               report["application_modules"].items()},
        }
        _report_entry(reports, label=label,
                      kind="installed console protocol session", run=child,
                      report=environment["WD_PROBE_REPORT"],
                      audit=environment["WD_GUARD_AUDIT"], argv=child.argv)

    # The legacy blocker raises in THIS child environment, before package code.
    # Its own report destination keeps the session's report intact: the blocking
    # child exits through the same hook, so a shared path would hand the session
    # assertion a ``-c`` report instead of the session's.
    file_environment = _installed_environment(
        home, cache_home, temp, serve_hooks,
        evidence / "file-session-report.json",
        evidence / "file-session-guard.jsonl")
    file_control_env = _with_report(
        file_environment, report=evidence / "file-legacy-control-report.json",
        audit=evidence / "file-legacy-control-guard.jsonl")
    control = _run_console([env_python, "-B", "-c", "import config_loader"],
                           file_control_env, neutral)
    _report_entry(reports, label="file-legacy-control",
                  kind="legacy blocker control", run=control,
                  report=evidence / "file-legacy-control-report.json",
                  audit=evidence / "file-legacy-control-guard.jsonl",
                  argv=_script_argv())
    assert control.returncode != 0, "the legacy blocker did not raise"
    assert ("blocked import during installed-console probe: config_loader"
            in control.stderr), control.stderr[-2000:]
    assert control.stdout == ""
    file_control_report = json.loads(
        (evidence / "file-legacy-control-report.json").read_text(
            encoding="utf-8"))
    assert file_control_report["application_modules"] == {}, (
        "the legacy blocker must raise before any package code: "
        f"{file_control_report['application_modules']}")

    file_cache = root / "cache-file"
    file_child = _installed_child(
        harness, "installed-file",
        [console, "--catalog-file", str(fixture), "--cache-dir", str(file_cache)],
        file_environment, neutral, artifacts)
    harness.anyio.run(harness._file_stdio_session, file_child)
    record_session("installed-file", file_child, file_environment)
    assert sorted(neutral.iterdir()) == [], "the file session wrote into its cwd"
    assert sorted(home.iterdir()) == [], "the file session created HOME state"
    assert file_cache.is_dir() and list(file_cache.glob("*.json")), (
        "the installed file session persisted no cache entry")

    # ------------------------------------------------------------------
    # 7. installed console loopback, cache fallback and source errors
    # ------------------------------------------------------------------
    url_cache = root / "cache-url"
    feed = harness._LoopbackFeed(fixture.read_bytes()).start()
    feed_hits = 0
    port = feed.server.server_address[1]
    first_environment = _installed_environment(
        home, cache_home, temp, serve_hooks,
        evidence / "url-first-report.json",
        evidence / "url-first-guard.jsonl", feed_port=port)
    try:
        assert feed.url.startswith("http://127.0.0.1:"), feed.url
        first_child = _installed_url_child(
            harness, "installed-url-first", console, feed.url, url_cache,
            first_environment, neutral, artifacts)
        fetched_at, entry_path = harness.anyio.run(
            harness._first_url_stdio_session, first_child, feed)
        assert feed.requests == ["/catalog.json"], feed.requests
        feed_hits += len(feed.requests)
    finally:
        feed.stop()
    record_session("installed-url-first", first_child, first_environment)

    entry_file = Path(entry_path)
    cache_bytes = entry_file.read_bytes()
    cache_names = sorted(path.name for path in url_cache.iterdir())
    assert cache_names == [entry_file.name]

    offline_warnings = ["refresh_failed", "cache_fallback", "tick_stale"]
    offline_environment = _installed_environment(
        home, cache_home, temp, serve_hooks,
        evidence / "url-offline-report.json",
        evidence / "url-offline-guard.jsonl", feed_port=port)
    offline_child = _installed_url_child(
        harness, "installed-url-offline", console, feed.url, url_cache,
        offline_environment, neutral, artifacts)
    harness.anyio.run(harness._offline_url_stdio_session, offline_child,
                      fetched_at, offline_warnings)
    record_session("installed-url-offline", offline_child, offline_environment)
    assert entry_file.read_bytes() == cache_bytes, (
        "a failed refresh rewrote the persisted last-good cache")
    assert sorted(path.name for path in url_cache.iterdir()) == cache_names

    absent_cache = root / "cache-absent"
    absent_environment = _installed_environment(
        home, cache_home, temp, serve_hooks,
        evidence / "url-absent-report.json",
        evidence / "url-absent-guard.jsonl", feed_port=port)
    absent_child = _installed_url_child(
        harness, "installed-url-absent", console, feed.url, absent_cache,
        absent_environment, neutral, artifacts)
    harness.anyio.run(harness._unavailable_stdio_session, absent_child)
    record_session("installed-url-absent", absent_child, absent_environment)
    assert not absent_cache.exists(), (
        "a failed refresh with no cache created cache state")

    corrupt_cache = root / "cache-corrupt"
    corrupt_cache.mkdir()
    target = harness.DiskCache("url", feed.url,
                               cache_dir=str(corrupt_cache)).path
    assert target is not None
    Path(target).write_bytes(b'{"cache_version": 1, "broken": ')
    corrupt_bytes = Path(target).read_bytes()
    corrupt_environment = _installed_environment(
        home, cache_home, temp, serve_hooks,
        evidence / "url-corrupt-report.json",
        evidence / "url-corrupt-guard.jsonl", feed_port=port)
    corrupt_child = _installed_url_child(
        harness, "installed-url-corrupt", console, feed.url, corrupt_cache,
        corrupt_environment, neutral, artifacts)
    harness.anyio.run(harness._unavailable_stdio_session, corrupt_child)
    record_session("installed-url-corrupt", corrupt_child, corrupt_environment)
    assert Path(target).read_bytes() == corrupt_bytes, (
        "a corrupt cache entry was repaired, rewritten or deleted")
    assert sorted(path.name for path in corrupt_cache.iterdir()) == [
        Path(target).name]

    # Five complete raw sessions, each with its own transcript and natural exit.
    assert sorted(sessions) == [
        "installed-file", "installed-url-absent", "installed-url-corrupt",
        "installed-url-first", "installed-url-offline"], sorted(sessions)
    for label, record in sessions.items():
        assert record["returncode"] == 0, (label, record)
        assert record["natural_exit"] is True, (label, record)
        assert record["stdout_frames"] >= 2, (label, record)
        assert record["outbound_frames"] > 2, (label, record)
        assert len(record["artifacts"]) == 4, (label, record["artifacts"])
        for suffix in (".stdin.raw", ".stdout.raw", ".stderr.raw",
                       ".session.json"):
            path = artifacts / (label + suffix)
            assert path.is_file(), path
        # The retained transcript is re-validated from the RAW FILES: every
        # stdout line is an intact JSON-RPC 2.0 frame and a clean session wrote
        # nothing at all to stderr.
        raw_stdout = (artifacts / (label + ".stdout.raw")).read_text(
            encoding="utf-8")
        assert raw_stdout.endswith("\n"), label
        frames = [json.loads(line) for line in raw_stdout.splitlines()]
        assert frames, label
        for frame in frames:
            assert frame.get("jsonrpc") == "2.0", (label, frame)
            assert (("result" in frame) or ("error" in frame)
                    or isinstance(frame.get("method"), str)), (label, frame)
        assert (artifacts / (label + ".stdin.raw")).stat().st_size > 0, label
        # stderr is diagnostics-only: the SDK's documented "Tool ... not found"
        # line is legal for the unknown-tool rejection, a traceback never is.
        raw_stderr = (artifacts / (label + ".stderr.raw")).read_text(
            encoding="utf-8")
        for marker in ("Traceback (most recent call last)", "was never awaited",
                       "Task exception was never retrieved"):
            assert marker not in raw_stderr, (label, raw_stderr[-2000:])
        summary = json.loads((artifacts / (label + ".session.json")).read_text(
            encoding="utf-8"))
        # The retained record must stand on its own: the live 6d audit above is
        # gone once the process ends, and only these saved fields remain.
        _assert_retained_session(summary, label=label)
    assert sorted(neutral.iterdir()) == [], "a session wrote into its cwd"
    assert sorted(home.iterdir()) == [], "a session created HOME state"
    assert sorted(cache_home.iterdir()) == [], (
        "a session without an explicit cache root used the platform cache")

    # The DISK/MANIFEST boundary, negatively: a retained record that also says
    # the child had to be killed must NOT count as graceful EOF. The reused 6d
    # helpers reject a LIVE ``forced_cleanup`` via ``_audit_clean_stdio``; this
    # row proves the SAVED record is validated just as strictly, from a copy of
    # an authentic summary with nothing changed but that one flag.
    authentic_path = artifacts / "installed-file.session.json"
    authentic_bytes = authentic_path.read_bytes()
    authentic = json.loads(authentic_bytes)
    _assert_retained_session(authentic, label="installed-file")
    tampered = json.loads(authentic_bytes)
    tampered["forced_cleanup"] = True
    with pytest.raises(AssertionError) as rejected_summary:
        _assert_retained_session(tampered, label="forced-cleanup-row")
    retained_reason = str(rejected_summary.value)
    assert "records forced_cleanup=True" in retained_reason, retained_reason
    assert "no natural exit" not in retained_reason, retained_reason
    assert authentic_path.read_bytes() == authentic_bytes, (
        "the evidence-boundary row must never rewrite an authentic session file")

    # ------------------------------------------------------------------
    # 8. strict origin negative control: identical bytes, wrong origin
    # ------------------------------------------------------------------
    shadow_hooks = _write_hook(root / "hooks" / "shadow", blocked_help)
    shadow_env = _installed_environment(
        home, cache_home, temp, shadow_hooks, evidence / "shadow-report.json",
        evidence / "shadow-guard.jsonl")
    # The ONE deliberate difference from a clean install: the reviewed checkout
    # joins the import path, so resolution -- not the bytes -- is wrong.
    shadow_env["PYTHONPATH"] = os.pathsep.join([str(shadow_hooks), str(REPO)])
    shadow_argv = [str(console), "--help"]
    shadow = _run_console(shadow_argv, shadow_env, neutral)
    assert shadow.returncode == 0, shadow.stderr[-4000:]
    assert shadow.stdout == help_stdout
    _report_entry(reports, label="shadow-console",
                  kind="checkout-shadow console --help", run=shadow,
                  report=evidence / "shadow-report.json",
                  audit=evidence / "shadow-guard.jsonl", argv=shadow_argv)
    # The saved state belongs to the real shadow CONSOLE: its pid, its `--help`
    # argv and the identical-byte checkout module. Only then is rejecting it
    # evidence about ORIGIN rather than about a destroyed report.
    _assert_shadow_console_report(
        evidence / "shadow-report.json", run=shadow, console=console,
        module_hashes=module_hashes)

    control = _run_console([env_python, "-B", "-c", "import config_loader"],
                           _with_report(
                               shadow_env,
                               report=evidence / "shadow-control-report.json",
                               audit=evidence / "shadow-control-guard.jsonl"),
                           neutral)
    _report_entry(reports, label="shadow-legacy-control",
                  kind="legacy blocker control with the checkout import path",
                  run=control, report=evidence / "shadow-control-report.json",
                  audit=evidence / "shadow-control-guard.jsonl",
                  argv=_script_argv())
    assert control.returncode != 0 and (
        "blocked import during installed-console probe: config_loader"
        in control.stderr), control.stderr[-2000:]
    # The control could not have overwritten the console report: it is intact.
    _assert_shadow_console_report(
        evidence / "shadow-report.json", run=shadow, console=console,
        module_hashes=module_hashes)
    # The SAME checker used for every successful installed child must reject
    # this saved state AT THE ORIGIN BRANCH -- not earlier at a missing report,
    # a foreign pid, a version or a prefix complaint.
    with pytest.raises(AssertionError) as rejected_shadow:
        _assert_installed_origin(
            evidence / "shadow-report.json", label="checkout-shadow control",
            pid=shadow.pid, cwd=neutral, env_prefix=environment_dir,
            purelib=purelib, module_hashes=module_hashes,
            required_modules={"watchdog_catalog"}, blocked_legacy=blocked_legacy)
    shadow_reason = str(rejected_shadow.value)
    assert "outside the installed site-packages" in shadow_reason, shadow_reason
    assert "watchdog_catalog imported from" in shadow_reason, shadow_reason
    assert str(REPO / "watchdog_catalog" / "__init__.py") in shadow_reason, (
        shadow_reason)
    for earlier_branch in ("wrote no report", "the report came from pid",
                           "is not the fresh environment",
                           "this is no virtual environment",
                           "bytes differ from the installed wheel"):
        assert earlier_branch not in shadow_reason, (
            f"the control was rejected before the origin branch ({earlier_branch}): "
            + shadow_reason)
    _assert_guard_clean(evidence / "shadow-guard.jsonl",
                        "checkout-shadow control")

    # ------------------------------------------------------------------
    # 9. two real packaging variants must be rejected by the SAME assertions
    # ------------------------------------------------------------------
    variants = root / "variants"
    variant_env = variants / "env"
    _create_environment(uv, floor, variant_env,
                        evidence / "variant-environment.log", tool_home, temp)
    variant_python = variant_env / "bin" / "python"
    variant_console = variant_env / "bin" / CONSOLE_COMMAND
    plain_env = _probe_environment(home, cache_home, temp)
    variant_outcomes = {}

    wrong_target = "watchdog_catalog.__main__:main_missing"
    broken_entry = _variant_wheel(variants, "wrong-entry",
                                  entry_point=wrong_target)
    broken_facts = _read_wheel(broken_entry)
    assert _entry_point_target(broken_facts) == wrong_target
    assert _entry_point_target(broken_facts) != CONSOLE_TARGET
    assert _payload_violations(broken_facts, expected_python,
                               expected_resource) == [], (
        "the entry-point variant must keep the reviewed payload")
    _install_offline(uv, variant_python, broken_entry, wheelhouse,
                     evidence / "variant-entry-install.log", tool_home, temp)
    broken_run = _run_console([variant_console, "--help"], plain_env, neutral)
    assert broken_run.returncode != 0, (
        "the wrong console entry point was accepted")
    assert "main_missing" in broken_run.stderr, broken_run.stderr[-2000:]
    assert broken_run.stdout == "", "a broken console wrote to stdout"
    still_importable = _run_console(
        [variant_python, "-I", "-B", "-c",
         "import watchdog_catalog.__main__; print('ok')"], plain_env, neutral)
    assert still_importable.returncode == 0, still_importable.stderr[-2000:]
    assert still_importable.stdout.strip() == "ok", (
        "the rejection must come from the console target, not a broken package")
    variant_outcomes["wrong-entry-point"] = {
        "wheel": str(broken_entry),
        "sha256": hashlib.sha256(broken_entry.read_bytes()).hexdigest(),
        "rejected_by": "declared entry point and installed console launch",
        "exit": broken_run.returncode,
        "diagnostic": broken_run.stderr.strip().splitlines()[-1:],
    }

    missing_resource = _variant_wheel(variants, "missing-tools",
                                      drop_resource=True)
    missing_facts = _read_wheel(missing_resource)
    missing_violations = _payload_violations(missing_facts, expected_python,
                                             expected_resource)
    assert any("missing the reviewed tool-contract resource" in item
               for item in missing_violations), missing_violations
    removal = _run_console([uv, "pip", "uninstall", "--no-config", "--python",
                            str(variant_python), DIST_NAME],
                           _tool_environment(tool_home, temp), neutral)
    assert removal.returncode == 0, removal.stderr[-2000:]
    _install_offline(uv, variant_python, missing_resource, wheelhouse,
                     evidence / "variant-resource-install.log", tool_home,
                     temp)
    resource_help = _run_console([variant_console, "--help"], plain_env, neutral)
    assert resource_help.returncode == 0, (
        "help must not need the tool-contract resource: "
        + resource_help.stderr[-2000:])
    resource_launch = _run_console([variant_console], plain_env, neutral)
    assert resource_launch.returncode != 0, (
        "a wheel without tools.json served a session")
    assert "tools.json" in resource_launch.stderr, resource_launch.stderr[-3000:]
    assert resource_launch.stdout == "", (
        "a broken wheel wrote into the protocol stream")
    variant_outcomes["missing-tools-json"] = {
        "wheel": str(missing_resource),
        "sha256": hashlib.sha256(missing_resource.read_bytes()).hexdigest(),
        "rejected_by": "wheel payload audit and installed launch",
        "audit": [item for item in missing_violations if "tool-contract" in item],
        "exit": resource_launch.returncode,
    }

    # ------------------------------------------------------------------
    # 10. a real `uv build --wheel` frontend produces the same reviewed payload
    # ------------------------------------------------------------------
    frontend_root = root / "frontend"
    frontend_source = _stage_source(frontend_root / "source")
    frontend_output = frontend_root / "wheel"
    frontend_log = evidence / "frontend-build.log"
    frontend_command = [
        uv, "build", "--wheel", "--no-config", "--offline", "--no-cache",
        "--no-build-isolation", "--python", str(build_python),
        "--out-dir", str(frontend_output), str(frontend_source)]
    frontend = subprocess.run(
        frontend_command, cwd=str(frontend_source), capture_output=True,
        text=True, timeout=600, env=_tool_environment(tool_home, temp))
    _write_log(frontend_log, frontend, frontend_command)
    assert frontend.returncode == 0, (
        "the uv build frontend failed:\n" + frontend.stderr[-6000:])
    built_frontend = sorted(frontend_output.glob("*.whl"))
    assert [path.name for path in built_frontend] == [wheel.name], (
        [path.name for path in built_frontend])
    frontend_wheel = built_frontend[0]
    frontend_facts = _read_wheel(frontend_wheel)
    assert _payload_violations(frontend_facts, expected_python,
                               expected_resource) == []
    with zipfile.ZipFile(wheel) as backend_archive, zipfile.ZipFile(
            frontend_wheel) as frontend_archive:
        assert sorted(frontend_archive.namelist()) == sorted(
            backend_archive.namelist())
        for name in frontend_archive.namelist():
            if name.startswith("watchdog_catalog/"):
                assert frontend_archive.read(name) == backend_archive.read(name), (
                    f"the frontend wheel changed package bytes: {name}")

    # ------------------------------------------------------------------
    # 11. retained evidence manifest
    # ------------------------------------------------------------------
    # Every guarded invocation still owns the report it wrote: verify BEFORE the
    # manifest records the index, and again from disk after it is written.
    labels = _assert_report_index(reports, evidence)
    assert labels == [
        "help", "help-sdk-mcp", "help-sdk-pydantic", "help-missing-file", "core",
        "eof", "file-legacy-control", "installed-file", "installed-url-first",
        "installed-url-offline", "installed-url-absent", "installed-url-corrupt",
        "shadow-console", "shadow-legacy-control"], labels
    test_identity = _test_identity(harness)
    manifest = {
        "kind": "LOCAL wheel-only clean installation (no published Git/PyPI)",
        "repo": str(REPO),
        "test_identity": test_identity,
        "distribution": {"name": DIST_NAME, "version": version,
                         "requires_python": project["requires-python"],
                         "requires": installed["requires"]},
        "build": {"transcript_kind": (
                      "declared setuptools.build_meta backend, exact installed "
                      "wheel"),
                  "transcript": str(backend_log),
                  "transcript_sha256": hashlib.sha256(
                      backend_log.read_bytes()).hexdigest(),
                  "backend_python": build_python,
                  "backend_setuptools": backend_version,
                  "wheel": str(wheel), "wheel_sha256": wheel_sha,
                  "wheel_bytes": wheel.stat().st_size,
                  "payload_entries": sorted(facts["names"])},
        "frontend_build": {"transcript_kind": (
                               "independent uv build frontend of a SEPARATE "
                               "staged export; not the installed wheel's build"),
                           "uv": uv_version, "command": frontend_command,
                           "log": str(frontend_log),
                           "wheel": str(frontend_wheel),
                           "wheel_sha256": hashlib.sha256(
                               frontend_wheel.read_bytes()).hexdigest()},
        "wheelhouse": {"path": str(wheelhouse),
                       "wheels": _dependency_manifest(wheelhouse)},
        "environment": {"floor_interpreter": floor,
                        "prefix": installed["prefix"],
                        "base_prefix": installed["base_prefix"],
                        "version": installed["version_info"],
                        "purelib": installed["purelib"],
                        "console": installed["console"],
                        "console_shebang": installed["console_shebang"],
                        "installer": installed["installer"],
                        "sdk_versions": installed["versions"],
                        "create_transcript": str(
                            evidence / "create-environment.log"),
                        "install_transcript": str(evidence / "install.log"),
                        "created": created},
        "installed_module_sha256": module_hashes,
        "help": {"exit": help_run.returncode, "pid": help_run.pid,
                 "report": str(evidence / "help-report.json"),
                 "argv": [str(part) for part in help_argv],
                 "stderr": help_run.stderr,
                 "stdout_sha256": hashlib.sha256(
                     help_run.stdout.encode()).hexdigest(),
                 "modules": sorted(help_report["application_modules"]),
                 "import_origins": {
                     name: fact["file"] for name, fact in
                     help_report["application_modules"].items()},
                 "sys_path": help_report["sys_path"]},
        "sessions": sessions,
        "report_index": reports,
        "loopback_feed_hits": feed_hits,
        "variants": variant_outcomes,
        "hidden_staged_source": str(stage),
        "staged_source_absent": not (root / "source").exists(),
    }
    (evidence / "manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True), encoding="utf-8")
    assert json.loads((evidence / "manifest.json").read_text(
        encoding="utf-8")) == manifest

    # The manifest is only evidence if it still describes the FILES: reconcile
    # every session record with its retained summary, the help block with the
    # primary help report, and the index with every saved report.
    retained_manifest = json.loads((evidence / "manifest.json").read_text(
        encoding="utf-8"))
    assert retained_manifest["report_index"] == reports
    _assert_report_index(retained_manifest["report_index"], evidence)
    _assert_shadow_console_report(
        evidence / "shadow-report.json", run=shadow, console=console,
        module_hashes=module_hashes)
    assert retained_manifest["test_identity"] == test_identity
    for label in ("tests/test_distribution.py", "tests/test_mcp_protocol.py"):
        recorded = next(item for item in retained_manifest[
            "test_identity"].values() if item["path"].endswith(label))
        assert recorded["sha256"] == hashlib.sha256(
            Path(recorded["path"]).read_bytes()).hexdigest(), label
    assert retained_manifest["help"]["report"] == str(
        evidence / "help-report.json")
    retained_help = json.loads(
        (evidence / "help-report.json").read_text(encoding="utf-8"))
    assert retained_help["pid"] == help_run.pid, (
        "the manifest's help report is not the primary help invocation")
    assert retained_help["argv"] == [str(part) for part in help_argv]
    assert set(retained_help["application_modules"]) == set(
        help_report["application_modules"])
    assert retained_manifest["build"]["transcript"] == str(backend_log)
    for label, record in retained_manifest["sessions"].items():
        summary = json.loads((artifacts / (label + ".session.json")).read_text(
            encoding="utf-8"))
        _assert_retained_session(summary, label=label)
        for field in ("pid", "argv", "cwd", "returncode", "natural_exit",
                      "stdin_closed", "forced_cleanup"):
            assert record[field] == summary[field], (label, field)
        assert record["forced_cleanup"] is False, label
        assert record["protocol_violations"] == [], label
