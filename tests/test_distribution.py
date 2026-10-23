"""Distribution packaging contract for the standalone public catalogue (unit 6a).

This module pins the PEP 621 metadata contract and proves the ACTUAL wheel
payload, not a spelling of the configuration. It has exactly two
unparametrized groups:

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

Honest scope: this is a zip-on-``sys.path`` wheel smoke, not a pip clean
install, not a console launch, not stdio negotiation and not a transport
check. ``watchdog_catalog.__main__:main`` is DECLARED here because the CLI
module arrives in a later unit; the declaration is asserted in the built
``entry_points.txt`` and nothing claims the command already runs.

Build tooling is a prerequisite, not a runtime dependency: the test needs a
``setuptools>=77.0.3`` interpreter (``FREE_INFERENCE_WATCHDOG_BUILD_PYTHON``
overrides the default of the current interpreter). A missing backend is an
actionable failure with the exact preparation command, never a skip.
"""

import hashlib
import json
import os
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


def _build_wheel(source, output):
    """Build a genuine wheel through the declared backend, in a subprocess."""
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
    completed = subprocess.run(
        [python, "-c", script, str(output), str(marker)],
        cwd=str(source), capture_output=True, text=True, timeout=300,
        env=_clean_environment())
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
