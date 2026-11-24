"""Distribution packaging contract and installed-console evidence.

``test_packaging_metadata_contract`` pins the static PEP 621 metadata;
``test_wheel_payload_and_core_smoke`` audits the genuine wheel built once by the
module-scoped ``primary_wheel`` fixture and runs its pure core/resource with
producer and SDK imports blocked from a neutral cwd.
``test_installed_console_clean_install_and_stdio_isolation`` installs that SAME
wheel OFFLINE into a fresh CPython 3.11 environment and drives the
installer-generated console through help, core, stdin-EOF, SDK file, loopback,
cache-fallback and source-error sessions behind an owned probe hook. The same
installed environment then drives the native one-shot executable (the sibling
``free-inference-watchdog`` console) from a neutral cwd through help, owned-file
search/get/status JSON parity, human freshness output, failure distinctions and
loopback cache fallback.
Prerequisites, probed never assumed: a ``setuptools>=77.0.3`` build interpreter,
an exact CPython 3.11, the real ``uv`` frontend and a prepared wheelhouse (each
with a ``FREE_INFERENCE_WATCHDOG_*`` override); a missing tool fails with its
preparation command and a missing wheelhouse fails inside the native offline
install, never a skip. Scratch roots are ordinary
``tempfile.TemporaryDirectory`` contexts under the caller's explicit ``TMPDIR``.
Nothing here contacts the public network.
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
NATIVE_CLI_COMMAND = "free-inference-watchdog"
NATIVE_CLI_TARGET = "watchdog_catalog.cli:main"
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
    "        'page_len': len(page['endpoints']),\n"
    "        'model_ids': [row['model_id'] for row in page['endpoints']],\n"
    "        'next_offset': page['next_offset'],\n"
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


def _temporary_root(prefix):
    """A temporary root under the caller's explicit ``TMPDIR``, else the default.

    ``TMPDIR`` always owns the artifacts when it is set; otherwise this is the
    ordinary platform temporary directory, never an invented application cache.
    Cleanup belongs to the context itself, so a passing, failing or erroring
    test leaves nothing behind.
    """
    base = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(base))


@pytest.fixture
def scratch_root():
    """This test's own root, cleaned on exit; the module build root stays separate."""
    with _temporary_root("unit16-test-") as path:
        yield Path(path)


def _floor_python():
    """A >= 3.11 interpreter for the archive/core smoke, preferring 3.11.

    This smoke's requirement is the declared ``>=3.11`` floor, NOT proof that
    an exact 3.11 interpreter exists: the exact-floor evidence is
    ``_installed_python()`` below, which probes identity and version.
    """
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
def test_packaging_metadata_contract(scratch_root):
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

    assert project["scripts"] == {CONSOLE_COMMAND: CONSOLE_TARGET,
                                  NATIVE_CLI_COMMAND: NATIVE_CLI_TARGET}

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

    neutral = scratch_root / "inert-cwd"
    neutral.mkdir()
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
    """Stage the public package and synthetic neighbours into a scratch root."""
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


def _build_wheel(source, output, *, python, log_path=None):
    """Build a genuine wheel through the declared backend, in a subprocess.

    ``python`` is the caller's already-selected build interpreter; this helper
    selects nothing. ``log_path`` is an optional destination for this
    invocation's own transcript (command, status, stdout, stderr), written
    before any build assertion can discard the diagnostics.
    """
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


@pytest.fixture(scope="module")
def primary_wheel():
    """The ONE genuine wheel both normal consumers share, and its provenance.

    The staged source is hidden after the real build and the backend transcript
    lives in this root; the ``TemporaryDirectory`` context removes all of it when
    the module ends, including a setup failure before ``yield``.
    Payload and schema claims belong to the consumers.
    """
    with _temporary_root("unit16-primary-") as directory:
        root = Path(directory)
        source = _stage_source(root / "source")
        backend_log = root / "backend-build.log"

        # ONE backend selection, shared by both normal consumers.
        backend_python, _ = _build_python()
        wheel = _build_wheel(source, root / "wheel", python=backend_python,
                             log_path=backend_log)

        # Hide that EXACT staged source once, after the real build: every later
        # launch must work without it. No live checkout is touched.
        hidden_source = source.with_name("staged-source-hidden")
        os.rename(source, hidden_source)
        assert not source.exists() and hidden_source.is_dir()

        yield {
            "source": source,
            "hidden_source": hidden_source,
            "wheel": wheel,
            "backend_python": backend_python,
            "backend_log": backend_log,
        }


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


def test_wheel_payload_and_core_smoke(primary_wheel, scratch_root):
    assert PYPROJECT.is_file(), "the wheel needs the committed packaging metadata"
    root = scratch_root
    wheel = primary_wheel["wheel"]
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
                           f"{NATIVE_CLI_COMMAND} = {NATIVE_CLI_TARGET}",
                           f"{CONSOLE_COMMAND} = {CONSOLE_TARGET}"], (
        "both console commands are declared for their shipped modules, in "
        "setuptools' sorted entry-point order")

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
    # The fixture hid the exact staged source before yielding; neither the
    # original (now absent) path nor the real hidden directory may be on path.
    for staged in (primary_wheel["source"], primary_wheel["hidden_source"]):
        assert str(staged) not in result["original_path"], (
            "the smoke must not import from the staged source tree")

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
    assert search["page_len"] == 1
    assert search["model_ids"] == ["alpha-1:free"]
    assert search["next_offset"] is None, (
        "the wheel's own core reports honest pagination")


# --------------------------------------------------------------------------
# group 3: wheel-only clean install, the INSTALLED console and its real
# transport, cache, error and isolation sessions
#
# Group 2 ends with a zip-on-``sys.path`` smoke. This group is the installed
# evidence: the SAME genuine wheel installed into a NEW PEP 405 Python 3.11
# environment created from the floor interpreter with no system or user
# site-packages, from an offline dependency wheelhouse, and the
# INSTALLER-GENERATED ``free-inference-watchdog-mcp`` console script launched by
# absolute path from a neutral cwd. The
# transport/cache/error sessions REUSE the reviewed group-5/6 harness of
# ``test_mcp_protocol`` -- the real SDK ``ClientSession`` over a real child's
# real stdin/stdout -- loaded explicitly into this HOST process, so no
# test/helper/repo path is ever handed to a server child and no second
# handwritten protocol implementation exists. That harness's own audit already
# checks real frames, protocol errors and natural shutdown; this group adds the
# installed-origin, guard and cache/state facts only. The host computes every
# oracle; the child only runs the installed wheel.
#
# Honest scope: LOCAL wheel-only clean installation. No published Git-source
# ``uvx`` run (no public ref contains this package yet), no PyPI publication and
# no rollout. The dependency wheels come from an owned one-time preparation
# OUTSIDE pytest; this group performs no public network operation and contacts
# only the loopback feed it owns. A missing offline tool fails with its exact
# preparation command, and a missing or unsuitable dependency wheelhouse fails
# inside the native offline install -- never a skip.
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
    """The exact CPython 3.11 interpreter the fresh environment is created from.

    An explicit ``FREE_INFERENCE_WATCHDOG_INSTALLED_PYTHON`` is AUTHORITATIVE:
    naming a wrong interpreter there is an actionable failure, never a reason
    to silently try a different installation. Without an override, an ordinary
    ``python3.11`` executable is discovered on PATH and finally the CURRENT
    interpreter is accepted when it really is CPython 3.11 (its executable may
    be named ``python``). A path that merely exists is not evidence: the
    interpreter's own implementation and version are probed every time.
    """
    override = os.environ.get(INSTALLED_PYTHON_ENVIRONMENT)
    candidates = ([override] if override
                  else [shutil.which("python3.11"), sys.executable])
    tried = []
    for candidate in candidates:
        if not candidate:
            continue
        if not Path(candidate).is_file():
            tried.append(f"{candidate}: no such executable file")
            continue
        try:
            probe = subprocess.run(
                [candidate, "-I", "-c",
                 "import json, sys; print(json.dumps({'implementation': "
                 "sys.implementation.name, 'version': "
                 "list(sys.version_info[:2])}))"],
                capture_output=True, text=True, timeout=60,
                env=_host_probe_environment())
        except OSError as error:
            tried.append(f"{candidate}: cannot run ({error})")
            continue
        if probe.returncode != 0:
            tried.append(f"{candidate}: cannot run ({probe.stderr.strip()[:200]})")
            continue
        try:
            facts = json.loads(probe.stdout)
            identity = (facts["implementation"], tuple(facts["version"]))
        except (ValueError, KeyError, TypeError):
            tried.append(f"{candidate}: unreadable interpreter probe "
                         f"{probe.stdout.strip()[:120]!r}")
            continue
        if identity != ("cpython", INSTALLED_MINOR):
            tried.append(f"{candidate}: {identity[0]} "
                         f"{'.'.join(str(part) for part in identity[1])} is not "
                         f"exactly CPython "
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
    candidates = [os.environ.get(UV_ENVIRONMENT), shutil.which("uv")]
    tried = []
    for candidate in candidates:
        if not candidate:
            continue
        if not Path(candidate).is_file():
            tried.append(f"{candidate}: no such executable file")
            continue
        try:
            probe = subprocess.run([candidate, "--version"], capture_output=True,
                                   text=True, timeout=60,
                                   env=_host_probe_environment())
        except OSError as error:
            tried.append(f"{candidate}: cannot run ({error})")
            continue
        if probe.returncode == 0 and probe.stdout.strip():
            return str(candidate), probe.stdout.strip()
        tried.append(f"{candidate}: {probe.stderr.strip()[:200]}")
    raise AssertionError(
        "the offline installer frontend is unavailable: install uv once outside "
        "pytest (`pipx install uv` or the standalone installer) or set "
        f"{UV_ENVIRONMENT}=/path/to/uv. Tried: " + "; ".join(tried))


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
    """An owned copy of the reviewed fixture input."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SNAPSHOT_FIXTURE, path)
    return path


def _owned_catalog_fixture(name, path):
    """An owned copy of one reviewed catalogue fixture, still unmodified."""
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / "tests" / "fixtures" / "catalog" / name, path)
    return path


def _native_console_name():
    """The native one-shot executable name on the LIVE platform."""
    return NATIVE_CLI_COMMAND + (".exe" if os.name == "nt" else "")


def test_installed_console_clean_install_and_stdio_isolation(primary_wheel,
                                                             scratch_root):
    """The REAL shared wheel, clean-installed, driven through its console command."""
    assert PYPROJECT.is_file(), "the wheel needs the committed packaging metadata"
    with PYPROJECT.open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    from watchdog_catalog.query import search_models as host_search_models
    from watchdog_catalog.source import DEFAULT_CATALOG_URL

    floor = _installed_python()
    uv, _ = _uv_binary()
    # The dependency source is only RESOLVED here. The native offline install
    # below rejects a missing, foreign-platform or wrong-version candidate
    # itself, and the installed pins are read back from the environment after.
    override = os.environ.get(WHEELHOUSE_ENVIRONMENT)
    wheelhouse = Path(override) if override else DEFAULT_WHEELHOUSE
    harness = _protocol_harness()
    blocked_legacy = tuple(harness.BLOCKED_LEGACY_ONLY)
    blocked_sdk = INSTALLED_SDK_ROOTS
    blocked_help = blocked_sdk + blocked_legacy + ("watchdog_catalog.server",)

    root = scratch_root
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
    # 1. bind the shared wheel's bytes and provenance to this install
    # ------------------------------------------------------------------
    # The module-scoped fixture built this wheel once and already hid its
    # staged source; this consumer only proves the bytes it will install.
    wheel = primary_wheel["wheel"]
    backend_log = primary_wheel["backend_log"]
    build_python = primary_wheel["backend_python"]
    source = primary_wheel["source"]
    hidden = primary_wheel["hidden_source"]
    # The declared backend's own transcript for this exact build is retained.
    transcript = backend_log.read_text(encoding="utf-8")
    assert wheel.name in transcript, (
        "the backend transcript does not name the wheel it produced: "
        + transcript[-4000:])
    facts = _read_wheel(wheel)
    expected_python, expected_resource = _expected_payload()
    module_hashes, wheel_file_hashes = _installed_module_hashes(wheel)
    assert _entry_point_target(facts) == CONSOLE_TARGET
    # The wheel's package bytes are the REVIEWED SOURCES' bytes, so the later
    # installed-origin digests are anchored in the checkout only at build time.
    for name, digest in wheel_file_hashes.items():
        if name.endswith(".py"):
            reviewed = PACKAGE / name.split("/")[-1]
            assert hashlib.sha256(reviewed.read_bytes()).hexdigest() == digest, (
                f"the wheel's {name} differs from the reviewed source")

    # The shared fixture hid the EXACT staged source after its real build:
    # every later launch must work without it, and the original path is gone.
    assert not source.exists(), "the shared staged source is still present"
    assert hidden.is_dir(), hidden

    # ------------------------------------------------------------------
    # 2. a genuinely fresh Python 3.11 environment, offline install
    # ------------------------------------------------------------------
    environment_dir = root / "fresh-3.11"
    _create_environment(
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
        "name": NATIVE_CLI_COMMAND, "value": NATIVE_CLI_TARGET,
        "group": "console_scripts"}, {
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
    # Each hook-enabled invocation below owns its own report/audit pair.
    help_argv = [str(console), "--help"]
    help_run = _run_console(help_argv, help_env, neutral)
    assert help_run.returncode == 0, help_run.stderr[-4000:]
    assert help_run.stderr == "", "help must keep stderr empty"
    help_stdout = help_run.stdout
    assert help_stdout.splitlines()[0].startswith(
        f"usage: {CONSOLE_COMMAND}"), help_stdout[:400]
    help_words = " ".join(help_stdout.split())
    for fragment in ("--catalog-url URL | --catalog-file PATH",
                     "--cache-dir DIR", DEFAULT_CATALOG_URL):
        assert fragment in help_words, (fragment, help_stdout[:1200])
    assert "--root" not in help_stdout, "the installed command has no --root"
    assert sorted(neutral.iterdir()) == [], "help wrote into its cwd"
    assert sorted(home.iterdir()) == [], "help created HOME state"
    assert sorted(cache_home.iterdir()) == [], "help created cache state"
    _assert_guard_clean(evidence / "help-guard.jsonl", "installed --help")
    _assert_installed_origin(
        evidence / "help-report.json", label="installed --help",
        pid=help_run.pid, cwd=neutral, env_prefix=environment_dir,
        purelib=purelib, module_hashes=module_hashes,
        required_modules={"watchdog_catalog", "watchdog_catalog.__main__",
                          "watchdog_catalog.source"},
        absent_modules=("watchdog_catalog.server",) + blocked_sdk,
        blocked_legacy=blocked_legacy, blocked_sdk=blocked_sdk)

    # An explicit MISSING file selection followed by --help still succeeds:
    # parsing and help happen before any source validation or catalogue read.
    # This invocation keeps its OWN evidence: a distinct report/audit pair, the
    # same installed-origin checker as the primary help run, its own stderr and
    # side-effect snapshots taken around THIS child rather than inherited.
    missing = root / "inputs" / "absent-catalogue.json"
    ordered_argv = [str(console), "--catalog-file", str(missing), "--help"]
    ordered_env = _installed_environment(
        home, cache_home, temp, help_hooks,
        evidence / "help-missing-file-report.json",
        evidence / "help-missing-file-guard.jsonl")
    ordered_before = _directory_entries(neutral, home, cache_home)
    ordered = _run_console(ordered_argv, ordered_env, neutral)
    ordered_after = _directory_entries(neutral, home, cache_home)
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
    _assert_installed_origin(
        evidence / "help-missing-file-report.json",
        label="missing-file --help", pid=ordered.pid, cwd=neutral,
        env_prefix=environment_dir, purelib=purelib,
        module_hashes=module_hashes,
        required_modules={"watchdog_catalog", "watchdog_catalog.__main__",
                          "watchdog_catalog.source"},
        absent_modules=("watchdog_catalog.server",) + blocked_sdk,
        blocked_legacy=blocked_legacy, blocked_sdk=blocked_sdk)

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
    assert "filters" not in core["search"] and "counts" not in core["search"]
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
    # 6. installed console file session: real SDK client, reviewed coverage
    # ------------------------------------------------------------------
    def assert_session_child(label, child, environment):
        """One genuine session's child: minimal env, real guards, installed bytes.

        The reviewed harness already audited the live protocol transcript --
        frames, protocol errors, natural shutdown. What only this group knows is
        that the child stayed credential-free, saw no test/helper/repo path, had
        its network guard reject nothing, and imported every module from the
        installed site-packages with the wheel's own bytes.
        """
        minimal = {"PATH", "HOME", "XDG_CACHE_HOME", "TMPDIR", "PYTHONPATH",
                   "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE",
                   "WD_PROBE_REPORT", "WD_GUARD_AUDIT"}
        assert set(environment) in (minimal, minimal | {"WD_ALLOWED_FEED_PORT"}), (
            "a server child environment must stay minimal and credential-free: "
            + str(sorted(environment)))
        assert environment["PYTHONPATH"] == str(serve_hooks), (
            "a server child must never see a test/helper/repo path")
        _assert_guard_clean(environment["WD_GUARD_AUDIT"], label)
        _assert_installed_origin(
            environment["WD_PROBE_REPORT"], label=label, pid=child.pid,
            cwd=neutral, env_prefix=environment_dir, purelib=purelib,
            module_hashes=module_hashes,
            required_modules={"watchdog_catalog", "watchdog_catalog.__main__",
                              "watchdog_catalog.server",
                              "watchdog_catalog.source",
                              "watchdog_catalog.query",
                              "watchdog_catalog.catalog"},
            blocked_legacy=blocked_legacy)

    file_environment = _installed_environment(
        home, cache_home, temp, serve_hooks,
        evidence / "file-session-report.json",
        evidence / "file-session-guard.jsonl")
    file_cache = root / "cache-file"
    file_child = _installed_child(
        harness, "installed-file",
        [console, "--catalog-file", str(fixture), "--cache-dir", str(file_cache)],
        file_environment, neutral, artifacts)
    harness.anyio.run(harness._file_stdio_session, file_child)
    assert_session_child("installed-file", file_child, file_environment)
    assert sorted(neutral.iterdir()) == [], "the file session wrote into its cwd"
    assert sorted(home.iterdir()) == [], "the file session created HOME state"
    assert file_cache.is_dir() and list(file_cache.glob("*.json")), (
        "the installed file session persisted no cache entry")

    # ------------------------------------------------------------------
    # 7. installed console loopback, cache fallback and source errors
    # ------------------------------------------------------------------
    url_cache = root / "cache-url"
    feed = harness._LoopbackFeed(fixture.read_bytes()).start()
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
    finally:
        feed.stop()
    assert_session_child("installed-url-first", first_child, first_environment)

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
    assert_session_child("installed-url-offline", offline_child,
                         offline_environment)
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
    assert_session_child("installed-url-absent", absent_child,
                         absent_environment)
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
    assert_session_child("installed-url-corrupt", corrupt_child,
                         corrupt_environment)
    assert Path(target).read_bytes() == corrupt_bytes, (
        "a corrupt cache entry was repaired, rewritten or deleted")
    assert sorted(path.name for path in corrupt_cache.iterdir()) == [
        Path(target).name]

    assert sorted(neutral.iterdir()) == [], "a session wrote into its cwd"
    assert sorted(home.iterdir()) == [], "a session created HOME state"
    assert sorted(cache_home.iterdir()) == [], (
        "a session without an explicit cache root used the platform cache")
    # ------------------------------------------------------------------
    # 8. a real `uv build --wheel` frontend produces the same reviewed payload
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
    # 9. the installed NATIVE one-shot console: the sibling executable, run
    #    from a neutral cwd over owned v2 file and loopback feeds
    #
    # The SAME shared wheel, the SAME fresh environment and the SAME probe
    # hooks as the MCP sessions above: no second build, install or harness.
    # JSON parity is against the accepted host pure core; the human form must
    # display freshness/fallback with the raw source URL deliberately absent.
    # ------------------------------------------------------------------
    from watchdog_catalog import catalog as host_catalog
    from watchdog_catalog import query as host_query

    native = console.with_name(_native_console_name())
    assert native.is_file(), f"the native console is missing: {native}"
    assert os.access(str(native), os.X_OK), native
    assert native.parent == console.parent, (
        "the native console is not the MCP console's sibling: "
        f"{native} vs {console}")
    assert native.name == _native_console_name(), native.name

    native_inputs = root / "native-inputs"
    search_fixture = _owned_catalog_fixture(
        "search-text.json", native_inputs / "search-text.json")
    lookup_fixture = _owned_catalog_fixture(
        "lookup.json", native_inputs / "lookup.json")
    basic_fixture = _owned_catalog_fixture(
        "basic.json", native_inputs / "basic.json")
    search_snapshot = host_catalog.load_snapshot(
        search_fixture.read_text(encoding="utf-8"))
    lookup_snapshot = host_catalog.load_snapshot(
        lookup_fixture.read_text(encoding="utf-8"))
    basic_snapshot = host_catalog.load_snapshot(
        basic_fixture.read_text(encoding="utf-8"))
    assert search_snapshot["schema_version"] == 2
    assert lookup_snapshot["schema_version"] == 2
    assert basic_snapshot["schema_version"] == 2

    native_hooks = _write_hook(root / "hooks" / "native", blocked_help)

    def run_native(label, arguments, *, feed_port=None, timeout=180):
        environment = _installed_environment(
            home, cache_home, temp, native_hooks,
            evidence / ("native-" + label + "-report.json"),
            evidence / ("native-" + label + "-guard.jsonl"),
            feed_port=feed_port)
        return _run_console([native, *arguments], environment, neutral,
                            timeout=timeout)

    def assert_native_origin(label, completed):
        _assert_guard_clean(evidence / ("native-" + label + "-guard.jsonl"),
                            label)
        _assert_installed_origin(
            evidence / ("native-" + label + "-report.json"), label=label,
            pid=completed.pid, cwd=neutral, env_prefix=environment_dir,
            purelib=purelib, module_hashes=module_hashes,
            required_modules={"watchdog_catalog", "watchdog_catalog.cli",
                              "watchdog_catalog.source",
                              "watchdog_catalog.catalog",
                              "watchdog_catalog.query"},
            absent_modules=("watchdog_catalog.server",) + blocked_sdk,
            blocked_legacy=blocked_legacy, blocked_sdk=blocked_sdk)

    # 9a. help before any source: no feed, cache, HOME or cwd state
    native_before = _directory_entries(neutral, home, cache_home)
    native_help = run_native("help", ["--help"])
    native_after = _directory_entries(neutral, home, cache_home)
    assert native_help.returncode == 0, native_help.stderr[-4000:]
    assert native_help.stderr == "", "native help must keep stderr empty"
    assert native_help.stdout.splitlines()[0].startswith(
        f"usage: {NATIVE_CLI_COMMAND}"), native_help.stdout[:400]
    for fragment in ("{search,get,status}", "--cache-dir", DEFAULT_CATALOG_URL):
        assert fragment in native_help.stdout, (fragment, native_help.stdout[:1500])
    assert "--root" not in native_help.stdout, "the native command has no --root"
    assert native_after == native_before, (
        "native help wrote cwd/HOME/cache state: "
        + str({key: (native_before[key], native_after[key])
               for key in native_after if native_before[key] != native_after[key]}))
    assert_native_origin("help", native_help)

    # The per-operation help is where the shared source/output flags live.
    search_help = run_native("search-help", ["search", "--help"])
    assert search_help.returncode == 0, search_help.stderr[-4000:]
    assert search_help.stderr == ""
    for fragment in ("--json", "--catalog-url", "--catalog-file",
                     "--cache-dir", "--provider", "--min-context-length",
                     "--input-modality", "--supported-parameter", "--limit",
                     "--offset"):
        assert fragment in search_help.stdout, (
            fragment, search_help.stdout[:1500])
    assert_native_origin("search-help", search_help)

    # 9b. installed-file search: JSON parity plus the human rendering
    search_cache = root / "native-cache-search"
    search_json = run_native("search-json", [
        "search", "reasoning", "--catalog-file", str(search_fixture),
        "--cache-dir", str(search_cache), "--limit", "1", "--json"])
    assert search_json.returncode == 0, search_json.stderr[-4000:]
    assert search_json.stderr == "", search_json.stderr[-2000:]
    document = json.loads(search_json.stdout)
    source_block = document.pop("source")
    assert document == host_query.search_models(
        search_snapshot, {"query": "reasoning", "limit": 1}), (
        "the installed native JSON differs from the accepted host core")
    assert document["ok"] is True and document["total_matches"] == 1
    assert [row["model_id"] for row in document["endpoints"]] == [
        "vendor/literal[ab].*"]
    assert document["endpoints"][0]["name"] == "ÉCHO TEXT"
    assert source_block["mode"] == "file" and source_block["url"] is None
    assert source_block["cached"] is False
    assert {"metadata_missing", "tick_stale"} <= set(source_block["warnings"])
    assert_native_origin("search-json", search_json)

    search_human = run_native("search-human", [
        "search", "reasoning", "--catalog-file", str(search_fixture),
        "--cache-dir", str(search_cache), "--limit", "1"])
    assert search_human.returncode == 0, search_human.stderr[-4000:]
    assert search_human.stderr == "", search_human.stderr[-2000:]
    text = search_human.stdout
    assert text.startswith("search:"), text[:300]
    for fragment in ("total_matches=1", "model_id=vendor/literal[ab].*",
                     "name=ÉCHO TEXT", "source: mode=file cached=no",
                     "source warnings:"):
        assert fragment in text, (fragment, text[:1500])
    assert "metadata_missing" in text and "tick_stale" in text
    assert text.count("source warnings:") == 1, (
        "freshness warnings must appear once per result, not per row")
    assert "url=" not in text, (
        "the human source summary must not print the raw source URL")
    assert_native_origin("search-human", search_human)

    # 9c. installed-file get: exact ids, JSON parity and detail rendering
    get_cache = root / "native-cache-get"
    get_json = run_native("get-json", [
        "get", "synthetic-a", "vendor/shared",
        "--catalog-file", str(lookup_fixture),
        "--cache-dir", str(get_cache), "--json"])
    assert get_json.returncode == 0, get_json.stderr[-4000:]
    assert get_json.stderr == "", get_json.stderr[-2000:]
    document = json.loads(get_json.stdout)
    source_block = document.pop("source")
    assert document == host_query.get_model(
        lookup_snapshot,
        {"provider": "synthetic-a", "model_id": "vendor/shared"}), (
        "the installed native get JSON differs from the accepted host core")
    assert document["endpoint"]["model_id"] == "vendor/shared"
    assert document["endpoint"]["description"].startswith("Synthetic-a")
    assert source_block["mode"] == "file" and source_block["cached"] is False
    assert_native_origin("get-json", get_json)

    get_human = run_native("get-human", [
        "get", "synthetic-a", "vendor/shared",
        "--catalog-file", str(lookup_fixture), "--cache-dir", str(get_cache)])
    assert get_human.returncode == 0, get_human.stderr[-4000:]
    assert get_human.stderr == "", get_human.stderr[-2000:]
    text = get_human.stdout
    assert text.startswith(
        "get: provider=synthetic-a model_id=vendor/shared"), text[:300]
    for fragment in ("description=Synthetic-a", "context_length=131072",
                     "chat_completions_url=https://synthetic-alpha.example/",
                     "api_type=openai", "source: mode=file"):
        assert fragment in text, (fragment, text[:1500])
    assert_native_origin("get-human", get_human)

    # 9d. installed-file status: the clock-derived age is the core's own answer
    status_json = run_native("status-json", [
        "status", "--catalog-file", str(basic_fixture),
        "--cache-dir", str(root / "native-cache-status"), "--json"])
    assert status_json.returncode == 0, status_json.stderr[-4000:]
    assert status_json.stderr == "", status_json.stderr[-2000:]
    document = json.loads(status_json.stdout)
    source_block = document.pop("source")
    reference = document["tick_epoch"] + document["last_tick_age_s"]
    assert document == host_query.watchdog_status(
        basic_snapshot, {}, now=reference), (
        "the installed native status differs from the accepted host core")
    assert document["tick_fresh"] is False
    assert document["counts"] == {"gateways": 2, "endpoints": 2,
                                  "distinct_raw_ids": 2,
                                  "marker_group_labels": 2}
    assert document["metadata_coverage"]["description_known"] == 1
    assert source_block["mode"] == "file" and source_block["cached"] is False
    assert_native_origin("status-json", status_json)

    status_human = run_native("status-human", [
        "status", "--catalog-file", str(basic_fixture),
        "--cache-dir", str(root / "native-cache-status")])
    assert status_human.returncode == 0, status_human.stderr[-4000:]
    assert status_human.stderr == "", status_human.stderr[-2000:]
    text = status_human.stdout
    assert text.startswith("status: snapshot="), text[:300]
    for fragment in ("counts: gateways=2 endpoints=2",
                     "metadata_coverage: endpoints=2",
                     "pending_alerts=unknown site_published=unknown "
                     "site_publish_age_s=unknown",
                     "source: mode=file cached=no"):
        assert fragment in text, (fragment, text[:1500])
    assert "url=" not in text
    assert_native_origin("status-human", status_human)

    # 9e. failure distinctions keep their structured code and exit status
    not_found = run_native("not-found", [
        "get", "synthetic-a", "absent", "--catalog-file", str(lookup_fixture),
        "--cache-dir", str(root / "native-cache-miss"), "--json"])
    assert not_found.returncode == 1, not_found.stderr[-2000:]
    document = json.loads(not_found.stdout)
    assert document["ok"] is False
    assert document["error"]["code"] == "not_found"
    assert "snapshot" not in document and "source" in document
    assert_native_origin("not-found", not_found)

    not_found_human = run_native("not-found-human", [
        "get", "synthetic-a", "absent", "--catalog-file", str(lookup_fixture),
        "--cache-dir", str(root / "native-cache-miss")])
    assert not_found_human.returncode == 1
    assert not_found_human.stdout == "", (
        "a human failure must not print a replacement document")
    assert not_found_human.stderr.startswith(f"{NATIVE_CLI_COMMAND}: "), (
        not_found_human.stderr)
    assert_native_origin("not-found-human", not_found_human)

    unknown_provider = run_native("unknown-provider", [
        "get", "nope", "x", "--catalog-file", str(lookup_fixture),
        "--cache-dir", str(root / "native-cache-unknown"), "--json"])
    assert unknown_provider.returncode == 2, unknown_provider.stderr[-2000:]
    assert json.loads(
        unknown_provider.stdout)["error"]["code"] == "unknown_provider"

    unavailable_cache = root / "native-cache-absent"
    unavailable = run_native("source-unavailable", [
        "status", "--catalog-file", str(native_inputs / "absent.json"),
        "--cache-dir", str(unavailable_cache), "--json"])
    assert unavailable.returncode == 1, unavailable.stderr[-2000:]
    assert json.loads(unavailable.stdout) == {
        "ok": False,
        "error": {"code": "source_unavailable",
                  "message": "Catalogue source unavailable."},
    }
    assert not unavailable_cache.exists(), (
        "a failed acquisition with no cache created cache state")

    # 9f. loopback URL: a fresh process attempts the refresh and only then
    #     serves the persisted last-good snapshot, honestly flagged
    native_url_cache = root / "native-cache-url"
    native_feed = harness._LoopbackFeed(search_fixture.read_bytes()).start()
    native_port = native_feed.server.server_address[1]
    url_arguments = [
        "search", "reasoning", "--catalog-url", native_feed.url,
        "--cache-dir", str(native_url_cache), "--limit", "1", "--json"]
    try:
        assert native_feed.url.startswith("http://127.0.0.1:"), native_feed.url
        first = run_native("url-first", url_arguments, feed_port=native_port)
        assert native_feed.requests == ["/catalog.json"], native_feed.requests
    finally:
        native_feed.stop()
    assert first.returncode == 0, first.stderr[-4000:]
    assert first.stderr == "", first.stderr[-2000:]
    first_document = json.loads(first.stdout)
    first_source = first_document["source"]
    assert first_source["mode"] == "url"
    assert first_source["url"] == native_feed.url
    assert first_source["cached"] is False
    assert isinstance(first_source["fetched_at"], int)
    assert "refresh_failed" not in first_source["warnings"]
    assert_native_origin("url-first", first)
    entry_names = sorted(path.name for path in native_url_cache.iterdir())
    assert len(entry_names) == 1, entry_names

    # The feed is gone: this NEW process still ATTEMPTS the refresh first
    # (the loopback port is closed, so the attempt fails promptly), then serves
    # the persisted snapshot with the original fetched_at and both warnings.
    fallback = run_native("url-fallback", url_arguments,
                          feed_port=native_port)
    assert fallback.returncode == 0, fallback.stderr[-4000:]
    assert fallback.stderr == "", fallback.stderr[-2000:]
    fallback_document = json.loads(fallback.stdout)
    fallback_source = fallback_document["source"]
    assert fallback_source["mode"] == "url"
    assert fallback_source["cached"] is True
    assert fallback_source["fetched_at"] == first_source["fetched_at"]
    assert fallback_source["warnings"][:2] == ["refresh_failed",
                                               "cache_fallback"]
    assert {key: value for key, value in fallback_document.items()
            if key != "source"} == {
        key: value for key, value in first_document.items() if key != "source"}
    assert sorted(path.name for path in native_url_cache.iterdir()) == entry_names
    assert_native_origin("url-fallback", fallback)

    assert sorted(neutral.iterdir()) == [], "a native invocation wrote into its cwd"
    assert sorted(home.iterdir()) == [], "a native invocation created HOME state"
    assert sorted(cache_home.iterdir()) == [], (
        "a native invocation without an explicit cache root used the platform cache")
