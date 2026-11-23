# tests/test_config_loader.py
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import config_loader


def test_load_configs_loads_all_json(tmp_path, monkeypatch):
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "api-pricing", "auth": {"method": "none"}, "display": 0}
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert len(configs) == 1
    assert configs[0]["name"] == "Test"


def test_load_configs_resolves_token_file(tmp_path, monkeypatch):
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    token_file = tmp_path / "token.json"
    token_file.write_text(json.dumps({"token": "test-token"}))
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "all-free",
              "auth": {"method": "token_file", "path": str(token_file), "key": "token"},
              "display": 0}
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] == "test-token"


def test_load_configs_sorts_by_display(tmp_path, monkeypatch):
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    # Assign display values that are intentionally out-of-name-order
    configs_data = [
        ("B", 1),
        ("A", 0),
        ("C", 2),
    ]
    for name, display in configs_data:
        config = {"name": name, "base_url": "https://example.com/v1",
                  "detection": "all-free", "auth": {"method": "none"}, "display": display}
        (providers_dir / f"{name}.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert [c["name"] for c in configs] == ["A", "B", "C"]


def test_load_configs_env_var(tmp_path, monkeypatch):
    """Config with env_var resolves token from os.environ."""
    monkeypatch.setenv("TEST_API_KEY", "env-token")
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "all-free",
              "auth": {"method": "env_var", "env_key": "TEST_API_KEY"},
              "display": 0}
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] == "env-token"


def _minimal_config(name, display=0):
    return {"name": name, "base_url": f"https://{name.lower()}.example.com/v1",
            "detection": "all-free", "auth": {"method": "none"}, "display": display}


def test_load_configs_missing_required_field_skips_file(tmp_path, monkeypatch, capsys):
    """A config missing a required field must cost ONLY that file: skip it
    with a stderr warning that names it, load the rest, never raise."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "incomplete.json").write_text(json.dumps(
        {"name": "Test", "base_url": "https://example.com/v1",
         "detection": "all-free"}))  # missing auth
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"], "only the valid file survives"
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower()
    assert "incomplete.json" in stderr, "warning must name the skipped file"


def test_load_configs_malformed_json_skips_file(tmp_path, monkeypatch, capsys):
    """Invalid JSON in one providers/*.json must skip exactly that file with a
    stderr warning — a hand-mangled config must not crash the loader import
    and take every healthy gateway down with it."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "bad.json").write_text("not json{{{")
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower()
    assert "bad.json" in stderr, "warning must name the skipped file"


def test_load_configs_non_dict_json_skips_file(tmp_path, monkeypatch, capsys):
    """A providers/*.json holding valid JSON of the wrong shape (a list, a
    bare string) is a file-level problem: skip it, warn, keep the rest."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "listshape.json").write_text(json.dumps([1, 2, 3]))
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise (AttributeError class)
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "listshape.json" in stderr


def test_load_configs_junk_auth_method_skips_file(tmp_path, monkeypatch, capsys):
    """A config naming an auth method the loader does not implement is a
    broken config, not a keyless one: skip the file with a warning rather
    than silently fetching unauthenticated."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "telepathy.json").write_text(json.dumps(
        {**_minimal_config("Telepathy"), "auth": {"method": "telepathy"}}))
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "telepathy.json" in stderr


def test_load_configs_non_dict_auth_skips_file(tmp_path, monkeypatch, capsys):
    """auth present but not an object ('auth': 'none' as a string) fails
    field validation: skip that file, not the process."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "strauth.json").write_text(json.dumps(
        {**_minimal_config("StrAuth"), "auth": "none"}))
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise (AttributeError class)
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "strauth.json" in stderr


def test_load_configs_missing_providers_dir_returns_empty(tmp_path, monkeypatch, capsys):
    """No providers/ directory at all is a directory-level problem: [] plus a
    stderr warning, never an import-time crash."""
    monkeypatch.setattr(config_loader, "REPO", tmp_path)  # tmp_path/providers absent
    configs = config_loader.load_configs()  # must not raise
    assert configs == []
    stderr = capsys.readouterr().err
    assert stderr.strip(), "missing providers dir must warn on stderr"


def test_load_configs_unreadable_providers_dir_degrades(tmp_path, monkeypatch, capsys):
    """An unreadable providers/ dir (or one replaced by a regular file) must
    degrade to [] with a stderr warning, not raise OSError out of the glob."""
    # A NON-DIRECTORY where providers/ is expected: is_dir() is False and any
    # iteration attempt is an error class — both branches must degrade.
    (tmp_path / "providers").write_text("not a directory")
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert configs == []
    stderr = capsys.readouterr().err
    assert stderr.strip(), "unreadable providers dir must warn on stderr"


def test_build_providers_degrades_to_valid_subset(tmp_path, monkeypatch, capsys):
    """The resolved registry inherits the per-file skip: a broken file costs
    exactly its own provider key; healthy keys survive intact."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "nous.json").write_text(json.dumps(_minimal_config("Nous Portal", 0)))
    (providers_dir / "kilo.json").write_text("{ broken")
    (providers_dir / "amd.json").write_text(json.dumps(_minimal_config("AMD Radeon", 1)))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    providers = config_loader.build_providers(config_loader.load_configs())
    assert set(providers) == {"nous", "amd"}
    assert "kilo" not in providers, "the broken file must cost only kilo"


def test_import_and_help_never_acquire_configuration(tmp_path):
    """Importing the three entry points — and either CLI's ``--help`` — must
    touch no provider file, resolve no credential and print no config warning.

    The copied checkout's ``providers/`` is a REGULAR FILE, so any call into
    the loader would fail its ``Path.glob`` / ``is_dir`` guard and emit
    ``config: skipping ...`` on stderr. A subprocess gives a genuinely fresh
    import, so what these assertions pin is the import-time and pre-argparse
    contract; the per-file degradation path itself is covered by the
    ``load_configs`` tests around this one.
    """
    repo = Path(config_loader.__file__).resolve().parent
    src = tmp_path / "src"
    src.mkdir()
    for path in repo.glob("*.py"):
        shutil.copy(path, src)
    shutil.copytree(repo / "watchdog_catalog", src / "watchdog_catalog")
    shutil.copytree(repo / "web", src / "web")
    (src / "providers").write_text("not a directory", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(src)}

    imported = subprocess.run(
        [sys.executable, "-c",
         "import config_loader, inference_watchdog, build_site"],
        cwd=src, capture_output=True, text=True, timeout=60, env=env,
    )
    assert imported.returncode == 0, f"import raised: {imported.stderr}"
    assert imported.stdout == "" and imported.stderr == "", (
        "importing an entry point must not acquire configuration: "
        f"{imported.stdout!r} / {imported.stderr!r}")

    for module in ("inference_watchdog", "build_site"):
        helped = subprocess.run(
            [sys.executable, "-c",
             f"import {module}; {module}.main(['--help'])"],
            cwd=src, capture_output=True, text=True, timeout=60, env=env,
        )
        assert helped.returncode == 0, f"{module} --help failed: {helped.stderr}"
        assert "usage:" in helped.stdout, f"{module} --help printed no usage"
        assert helped.stderr == "", (
            f"{module} --help acquired configuration: {helped.stderr!r}")


# ---------- token_file auth via path_env (Nous path-pointer design) ----------


def _write_provider(providers_dir, auth):
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "all-free", "auth": auth, "display": 0}
    (providers_dir / "test.json").write_text(json.dumps(config))


def test_load_configs_token_file_path_env_resolves(tmp_path, monkeypatch):
    """token_file + path_env: the env var holds a PATH to a JSON token file,
    never the token itself. The loader resolves env var -> path -> file ->
    dot-path key -> token at load time."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    token_file = tmp_path / "auth.json"
    token_file.write_text(json.dumps({"token": "file-token"}))
    monkeypatch.setenv("TEST_AUTH_FILE", str(token_file))
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "token"})
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] == "file-token"


def test_load_configs_token_file_path_env_nested_key(tmp_path, monkeypatch):
    """token_file + path_env: the dot-path key resolves through nested JSON
    (the real ~/.hermes/auth.json shape: providers.nous.access_token)."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    token_file = tmp_path / "auth.json"
    token_file.write_text(json.dumps(
        {"providers": {"nous": {"access_token": "nested-token"}}}))
    monkeypatch.setenv("TEST_AUTH_FILE", str(token_file))
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "providers.nous.access_token"})
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] == "nested-token"


def test_load_configs_token_file_path_env_unset_degrades(tmp_path, monkeypatch, capsys):
    """token_file + path_env with the env var unset (neither in os.environ nor
    the project .env) must NOT raise — it degrades the provider (_token=None)
    and logs a warning to stderr. A broken env must never kill the watchdog
    at import (the cron wrapper treats exit 1 as routine and stays silent)."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    monkeypatch.delenv("TEST_AUTH_FILE", raising=False)
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "token"})
    # REPO points at tmp_path: no .env there, so no fallback can resolve it.
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert len(configs) == 1
    assert configs[0]["_token"] is None
    stderr = capsys.readouterr().err
    assert "degraded" in stderr.lower()


def test_load_configs_token_file_path_env_falls_back_to_project_env(tmp_path, monkeypatch):
    """The cron wrapper never sources .env into the process environment (the
    webhook is read the same way), so a path_env lookup that only consulted
    os.environ would miss keys that live only in the project-local .env and
    the tick would die at import. The loader must fall back to REPO/.env."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    token_file = tmp_path / "auth.json"
    token_file.write_text(json.dumps({"token": "dotenv-token"}))
    (tmp_path / ".env").write_text(f"TEST_AUTH_FILE={token_file}\n")
    monkeypatch.delenv("TEST_AUTH_FILE", raising=False)
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "token"})
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] == "dotenv-token"


def test_load_configs_token_file_path_env_expands_user(tmp_path, monkeypatch):
    """A '~'-relative path in the env var must be expanduser'd (the real
    deployment stores ~/.hermes/auth.json, not an absolute path)."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    token_file = tmp_path / "auth.json"
    token_file.write_text(json.dumps({"token": "home-token"}))
    monkeypatch.setenv("TEST_AUTH_FILE", f"~/{token_file.name}")
    # Point expanduser's home at tmp_path so '~' resolves deterministically.
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "token"})
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] == "home-token"


def test_load_configs_token_file_path_env_missing_file_degrades(tmp_path, monkeypatch, capsys):
    """path_env resolves but the file does not exist -> degrade (_token=None)
    + log to stderr. A broken env must not kill the watchdog at import."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    monkeypatch.setenv("TEST_AUTH_FILE", str(tmp_path / "nope.json"))
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "token"})
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] is None
    stderr = capsys.readouterr().err
    assert "degraded" in stderr.lower()


def test_load_configs_token_file_path_env_takes_precedence_over_path(tmp_path, monkeypatch):
    """Backward compat: the legacy literal `path` field still works, but a
    set path_env wins when both are present."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    env_file = tmp_path / "env_auth.json"
    env_file.write_text(json.dumps({"token": "via-path-env"}))
    legacy_file = tmp_path / "legacy_auth.json"
    legacy_file.write_text(json.dumps({"token": "via-path"}))
    monkeypatch.setenv("TEST_AUTH_FILE", str(env_file))
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "path": str(legacy_file),
                                    "key": "token"})
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] == "via-path-env"


# ---------- repo pins: the committed Nous config is a path pointer ----------


def test_nous_config_uses_path_pointer_auth():
    """providers/nous.json must reference the token via token_file + path_env
    (NOUS_AUTH_FILE) + dot-path key — never a literal token copy and never a
    hardcoded ~/.hermes path in the public repo."""
    repo = Path(config_loader.__file__).resolve().parent
    cfg = json.loads((repo / "providers" / "nous.json").read_text(encoding="utf-8"))
    auth = cfg["auth"]
    assert auth["method"] == "token_file"
    assert auth["path_env"] == "NOUS_AUTH_FILE"
    assert auth["key"] == "providers.nous.access_token"
    assert "env_key" not in auth, "env_var/NOUS_ACCESS_TOKEN design must not return"
    assert "~/.hermes" not in json.dumps(cfg), "no hardcoded agent paths in the committed config"


# ---------- ignored_slugs: optional, validated list-of-non-empty-strings ----------


def test_load_configs_valid_ignored_slugs_passes_through(tmp_path, monkeypatch, capsys):
    """A well-formed ignored_slugs list is optional and loads untouched."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "api-pricing", "auth": {"method": "none"},
              "display": 0, "ignored_slugs": ["openrouter/free"]}
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert len(configs) == 1
    assert configs[0]["ignored_slugs"] == ["openrouter/free"]
    assert "skipping" not in capsys.readouterr().err


@pytest.mark.parametrize("bad", [
    "openrouter/free",          # bare string, not a list
    None,                       # explicit JSON null (set(None) would crash)
    42,                         # number
    {"a": 1},                   # object
    [""],                       # empty string element
    ["ok", None],               # non-string element
    ["ok", "  "],               # blank (whitespace-only) element
    [123],                      # int element
    [" padded "],               # padded element — passes strip-check, never exact-matches
    ["lead "],                  # trailing-space padding
    [" trail"],                 # leading-space padding
], ids=["str", "null", "int", "dict", "empty-str", "none-elem", "blank-elem", "int-elem",
        "padded", "lead-space", "trail-space"])
def test_load_configs_invalid_ignored_slugs_skips_file(tmp_path, monkeypatch, capsys, bad):
    """A malformed ignored_slugs costs exactly that file: ValueError -> the
    existing per-file skip + stderr-warning contract."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "api-pricing", "auth": {"method": "none"},
              "display": 0, "ignored_slugs": bad}
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs == []
    stderr = capsys.readouterr().err
    assert "skipping" in stderr and "ignored_slugs" in stderr


def test_load_configs_empty_ignored_slugs_list_ok(tmp_path, monkeypatch, capsys):
    """[] is a valid (vacuously satisfied) list — file loads, no warning."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "api-pricing", "auth": {"method": "none"},
              "display": 0, "ignored_slugs": []}
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert len(configs) == 1
    assert "skipping" not in capsys.readouterr().err


def test_shipped_configs_declare_ignored_slugs():
    """Pins the shipped kilo.json and openrouter.json ignored_slugs literals
    to the exact router ids previously filtered by hardcoded display logic."""
    repo = Path(config_loader.__file__).resolve().parent
    kilo = json.loads((repo / "providers" / "kilo.json").read_text(encoding="utf-8"))
    orouter = json.loads((repo / "providers" / "openrouter.json").read_text(encoding="utf-8"))
    assert kilo["ignored_slugs"] == ["kilo-auto/free", "openrouter/free"]
    assert orouter["ignored_slugs"] == ["openrouter/free"]


def test_provider_key_slug_fallback_for_hand_built_dicts():
    """Defensive fallback: a hand-built dict (no roster_key, no _stem —
    only possible when bypassing load_configs) slugifies its name."""
    cfg = {"name": "NVIDIA NIM"}
    assert config_loader._provider_key(cfg) == "nvidia_nim"


def test_real_configs_keys_unchanged_by_stem_migration():
    """Migration safety net: the shipped configs resolve to the SAME roster
    keys as before the _PROVIDER_KEY_MAP deletion. nim.json is still ON DISK
    but carries enabled:false, so it is absent from the registry. Update the
    expected keys when a gateway is legitimately added/removed OR
    enabled/disabled."""
    expected = {"nous", "tokenrouter", "kilo", "openrouter", "amd", "bai",
                "vercel", "felo", "tokenra", "infron", "aimlapi"}
    registry = config_loader.build_providers(config_loader.load_configs())
    assert set(registry) == expected


def test_disabled_provider_config_file_survives_on_disk():
    """The point of the flag: untracking must NOT delete the config.
    nim.json exists, is valid, and is simply gated out — so re-enabling
    is a one-character edit with no code change and no re-registration."""
    from pathlib import Path
    nim = Path(config_loader.REPO) / "providers" / "nim.json"
    assert nim.is_file(), "providers/nim.json must survive being disabled"
    cfg = json.loads(nim.read_text(encoding="utf-8"))
    assert cfg["enabled"] is False
    assert cfg["auth"]["env_key"] == "NVIDIA_API_KEY"   # credential intact


# ---------- roster_key: optional, validated non-empty string ----------


@pytest.mark.parametrize("bad", [123, "", "   ", True, None],
                         ids=["int", "empty", "blank", "bool", "null"])
def test_roster_key_field_must_be_nonempty_string(tmp_path, monkeypatch, capsys, bad):
    """Optional "roster_key" field: when present it must be a non-empty
    string — a bad value silently re-routes the provider's roster identity."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {"name": "X", "base_url": "https://x.com/v1", "detection": "all-free",
           "auth": {"method": "none"}, "display": 0, "roster_key": bad}
    (providers_dir / "x.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert config_loader.load_configs() == []
    assert "roster_key must be a non-empty string" in capsys.readouterr().err


def test_provider_key_falls_back_to_file_stem(tmp_path, monkeypatch):
    """A multi-word name needs NO map entry: the config FILE STEM is the
    roster key (nvidia-nim.json -> "nvidia-nim"), not the slugified name."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {"name": "NVIDIA NIM", "base_url": "https://x.com/v1",
           "detection": "all-free", "auth": {"method": "none"}, "display": 0}
    (providers_dir / "nvidia-nim.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert "nvidia-nim" in config_loader.build_providers(
        config_loader.load_configs())


def test_roster_key_overrides_stem(tmp_path, monkeypatch):
    """The explicit roster_key field wins over the file stem."""
    cfg = {"name": "NVIDIA NIM", "base_url": "https://x.com/v1",
           "detection": "all-free", "auth": {"method": "none"}, "display": 0,
           "roster_key": "nim"}
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "nvidia-nim.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert set(config_loader.build_providers(
        config_loader.load_configs())) == {"nim"}


def test_roster_key_used_verbatim(tmp_path, monkeypatch):
    """An explicit roster_key is used VERBATIM as the roster key — including
    odd-but-valid characters (interior space). Operator's explicit override;
    no normalization is applied (A1 rejects blank/non-string, that's all)."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {"name": "Weird Name", "base_url": "https://x.com/v1",
           "detection": "all-free", "auth": {"method": "none"}, "display": 0,
           "roster_key": "my key"}
    (providers_dir / "weird.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert set(config_loader.build_providers(
        config_loader.load_configs())) == {"my key"}


# ---------- probe: optional per-provider zero-credit-probe dialect ----------


@pytest.mark.parametrize("bad", ["x", 42, None, [1]],
                         ids=["str", "int", "null", "list"])
def test_invalid_probe_block_skips_file(tmp_path, monkeypatch, capsys, bad):
    """The optional "probe" block must be an object — anything else is a
    file-level shape error: skip + warn, keep the rest of the roster."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {**_minimal_config("ProbeBlock"), "probe": bad}
    (providers_dir / "probetest.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert config_loader.load_configs() == []
    stderr = capsys.readouterr().err
    assert "skipping" in stderr and "probetest.json" in stderr
    assert "probe must be an object" in stderr


@pytest.mark.parametrize("bad", [0, "3", True, -1, 1.5, None],
                         ids=["zero", "str", "bool", "negative", "float",
                              "null"])
def test_invalid_probe_max_tokens_skips_file(tmp_path, monkeypatch, capsys, bad):
    """probe.max_tokens must be a positive integer (bool is an int-subclass
    — rejected like display). A non-int reaches json-serialized arithmetic
    wrong and a <=0 value makes every probe a guaranteed 400: skip + warn."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {**_minimal_config("ProbeMaxTokens"), "probe": {"max_tokens": bad}}
    (providers_dir / "maxtok.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert config_loader.load_configs() == []
    stderr = capsys.readouterr().err
    assert "skipping" in stderr and "maxtok.json" in stderr
    assert "probe.max_tokens must be a positive integer" in stderr


@pytest.mark.parametrize("bad", [
    "x",                        # not a list
    [],                         # empty list (useless dialect)
    [{"status": "400"}],        # status not an int
    [{"status": True}],         # bool is an int-subclass
    [{"status": 400, "all_of": "deposit"}],       # all_of bare string
    [{"status": 400, "any_of": ["", "  "]}],      # empty/blank substrings
    [{"status": 400, "all_of": [" padded "]}],    # padded substring
    [{}],                       # missing status
    [{"status": 400}],          # needs all_of or any_of
    [42],                       # entry not an object
    None,                       # explicit null (present key, junk value)
    [{"status": 400, "all_of": []}],    # empty all_of vacuously PAIDs any 400
    [{"status": 400, "any_of": []}],    # same for any_of
    [{"status": 400, "all_of": None}],  # explicit-null condition list
], ids=["str", "empty-list", "str-status", "bool-status", "all-of-str",
        "blank-subs", "padded-sub", "no-status", "no-conditions", "int-elem",
        "null", "empty-all-of", "empty-any-of", "null-all-of"])
def test_invalid_paid_signals_skips_file(tmp_path, monkeypatch, capsys, bad):
    """probe.paid_signals must be a non-empty list of {status:int,
    all_of/any_of: list of non-empty unpadded strings} objects — a junk
    signal silently misclassifies (or TypeErrors at probe time). Skip+warn."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {**_minimal_config("PaidSignals"), "probe": {"paid_signals": bad}}
    (providers_dir / "signals.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert config_loader.load_configs() == []
    stderr = capsys.readouterr().err
    assert "skipping" in stderr and "signals.json" in stderr
    assert "paid_signals" in stderr


@pytest.mark.parametrize("good", [
    {},                                              # empty dict = no-op
    {"max_tokens": 5},                               # dialect override only
    {"max_tokens": 3, "paid_signals": [
        {"status": 403, "all_of": ["deposit"]}]},    # full valid block
    {"paid_signals": [{"status": 402, "any_of": ["top up"]}]},  # any_of alone
], ids=["empty", "max-tokens-only", "full", "any-of-only"])
def test_valid_probe_block_loads(tmp_path, monkeypatch, capsys, good):
    """Well-formed probe blocks (including the vacuous {}) load untouched —
    the block is optional and its keys are individually optional."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {**_minimal_config("ProbeGood"), "probe": good}
    (providers_dir / "good.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert len(configs) == 1
    assert configs[0]["probe"] == good
    assert "skipping" not in capsys.readouterr().err


def test_bai_config_carries_its_probe_dialect():
    """Pin: b.ai's probe dialect (max_tokens 3 + its three paid signals)
    lives in providers/bai.json, not only in probe_zero_credit.py defaults —
    the config is its real home and the DEFAULT_* constants are just the
    generic fallback for configs that omit the block."""
    registry = config_loader.build_providers(config_loader.load_configs())
    bai = registry["bai"]
    assert bai["probe"]["max_tokens"] == 3
    assert len(bai["probe"]["paid_signals"]) == 3


def test_duplicate_provider_keys_warn(tmp_path, monkeypatch, capsys):
    """Two files resolving to one key warn on stderr; later display wins."""
    d = tmp_path / "providers"; d.mkdir()
    base = {"base_url": "https://x.com/v1", "detection": "all-free",
            "auth": {"method": "none"}}
    (d / "a.json").write_text(json.dumps({**base, "name": "A", "display": 0}))
    (d / "b.json").write_text(json.dumps({**base, "name": "B", "display": 1,
                                          "roster_key": "a"}))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    providers = config_loader.build_providers(config_loader.load_configs())
    assert set(providers) == {"a"} and providers["a"]["name"] == "B"
    assert "duplicate provider key" in capsys.readouterr().err


def test_env_example_documents_path_pointer_not_token_copy():
    """.env.example must document NOUS_AUTH_FILE (path pointer) and must not
    solicit a literal NOUS_ACCESS_TOKEN."""
    repo = Path(config_loader.__file__).resolve().parent
    text = (repo / ".env.example").read_text(encoding="utf-8")
    assert "NOUS_AUTH_FILE=" in text
    assert "NOUS_ACCESS_TOKEN" not in text


# ---------- degradation: a broken provider must not kill the watchdog ----------


def test_load_configs_degrades_provider_with_broken_auth(tmp_path, monkeypatch):
    """CRITICAL: if one provider's auth fails to resolve (e.g. NOUS_AUTH_FILE
    unset on a broken env), load_configs must NOT raise — the watchdog dies
    silently at import and the cron wrapper treats exit 1 as routine. Instead
    it degrades that provider (_token=None) and loads the rest."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    # One healthy env_var provider
    monkeypatch.setenv("HEALTHY_KEY", "ok-token")
    healthy = {"name": "Healthy", "base_url": "https://example.com/v1",
               "detection": "all-free",
               "auth": {"method": "env_var", "env_key": "HEALTHY_KEY"},
               "display": 0}
    (providers_dir / "healthy.json").write_text(json.dumps(healthy))
    # One broken token_file provider (path_env unset, no fallback .env)
    broken = {"name": "Broken", "base_url": "https://broken.com/v1",
              "detection": "all-free",
              "auth": {"method": "token_file",
                       "path_env": "UNSET_AUTH_FILE",
                       "key": "token"},
              "display": 1}
    (providers_dir / "broken.json").write_text(json.dumps(broken))
    monkeypatch.delenv("UNSET_AUTH_FILE", raising=False)
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    # Must not raise
    configs = config_loader.load_configs()
    names = [c["name"] for c in configs]
    assert "Healthy" in names, "healthy provider should still load"
    assert "Broken" in names, "broken provider should still be present (degraded)"
    broken_cfg = next(c for c in configs if c["name"] == "Broken")
    assert broken_cfg["_token"] is None, "degraded provider must have _token=None"
    healthy_cfg = next(c for c in configs if c["name"] == "Healthy")
    assert healthy_cfg["_token"] == "ok-token", "healthy provider unaffected"


def test_load_configs_missing_dot_key_does_not_fetch_keyless(tmp_path, monkeypatch):
    """If the dot-path key is missing in the token file, _resolve_json_path
    returns None → _token=None (degraded), never a silent keyless fetch."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    token_file = tmp_path / "auth.json"
    # File exists but does NOT contain the expected key
    token_file.write_text(json.dumps({"other": "value"}))
    monkeypatch.setenv("TEST_AUTH_FILE", str(token_file))
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "providers.nous.access_token"})
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()
    assert configs[0]["_token"] is None, (
        "missing dot-key must degrade to _token=None, not fetch keyless")


def test_load_configs_degraded_provider_logs_warning(tmp_path, monkeypatch, capsys):
    """Degrading a provider must log a warning to stderr so the operator can
    see why a provider is silently absent without digging through code."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    broken = {"name": "Broken", "base_url": "https://broken.com/v1",
              "detection": "all-free",
              "auth": {"method": "token_file",
                       "path_env": "UNSET_AUTH_FILE",
                       "key": "token"},
              "display": 0}
    (providers_dir / "broken.json").write_text(json.dumps(broken))
    monkeypatch.delenv("UNSET_AUTH_FILE", raising=False)
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    config_loader.load_configs()
    stderr = capsys.readouterr().err
    assert "degraded" in stderr.lower() or "broken" in stderr.lower(), (
        "degradation should log a warning mentioning the failing provider")


# ---------- OSError degradation: unreadable token files must not crash ----------


def test_load_configs_missing_auth_file_degrades_not_crashes(tmp_path, monkeypatch, capsys):
    """FileNotFoundError (an OSError) from a missing literal-`path` token file
    must degrade the provider (_token=None, stderr warning), not raise — the
    degrade-don't-die contract covers deleted/moved auth files too."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "all-free",
              "auth": {"method": "token_file", "path": str(tmp_path / "nonexistent.json"), "key": "token"},
              "display": 0}
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    # Must not raise — a deleted auth file is a degraded provider, not a
    # dead watchdog (FileNotFoundError is an OSError, NOT a ValueError).
    configs = config_loader.load_configs()
    assert len(configs) == 1
    assert configs[0]["_token"] is None
    stderr = capsys.readouterr().err
    assert "degraded" in stderr.lower()


def test_load_configs_token_file_oserror_degrades_not_crashes(tmp_path, monkeypatch, capsys):
    """Any OSError while opening the token file (e.g. IsADirectoryError when
    the path points at a directory, PermissionError on an unreadable file)
    must degrade the provider, not crash the watchdog at import. The outer
    handler only caught ValueError, and the inner FileNotFoundError clause
    only covers the missing-file case — the wider OSError family escaped
    the degrade handler entirely."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    # Point the token path at a DIRECTORY: open() raises IsADirectoryError,
    # an OSError that is neither a FileNotFoundError nor a ValueError.
    dir_as_token = tmp_path / "auth_dir"
    dir_as_token.mkdir()
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "all-free",
              "auth": {"method": "token_file", "path": str(dir_as_token), "key": "token"},
              "display": 0}
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert len(configs) == 1
    assert configs[0]["_token"] is None
    stderr = capsys.readouterr().err
    assert "degraded" in stderr.lower()


# ---------- NVIDIA NIM gateway (all-free, Sept-9 refresh spec plan gap) ----------


def test_provider_key_map_deleted_stem_is_the_convention():
    """The name->key _PROVIDER_KEY_MAP is deleted by design: file stems ARE
    the roster keys (nim.json -> nim), with roster_key as the explicit
    override. No map means no code edit when a gateway is added."""
    assert not hasattr(config_loader, "_PROVIDER_KEY_MAP")


def test_real_configs_include_nim_last_display():
    """providers/nim.json exists in the real repo config dir: all-free
    detection against https://integrate.api.nvidia.com/v1 with NVIDIA_API_KEY
    env_var auth, and display 6. The file survives being disabled, but
    enabled:false keeps it out of load_configs(), so the enabled configs sort
    by their own display values and aimlapi (display 11) sorts last. This is a
    REAL-registry inventory test, so the offline P4 witness source-inspects it
    rather than executing it."""
    repo = Path(config_loader.__file__).resolve().parent
    cfg = json.loads((repo / "providers" / "nim.json").read_text(encoding="utf-8"))
    assert cfg["name"] == "NVIDIA NIM"
    assert cfg["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert cfg["detection"] == "all-free"
    assert cfg["auth"] == {"method": "env_var", "env_key": "NVIDIA_API_KEY"}
    assert cfg["display"] == 6
    configs = config_loader.load_configs()
    assert len(configs) == 11, "real providers/ dir must hold eleven enabled configs"
    assert configs[-1]["name"] == "AI/ML API"
    assert configs[-1]["display"] == 11


def test_build_gateway_wiring_shape_excludes_disabled():
    """The wiring map has one entry per gateway; the 'auth' field is GONE
    (operator: 'Bearer <your API key>' read the same for every gateway — it
    was dropped from all site/MCP surfaces), and only the two remaining
    fields survive. nim.json is on disk but enabled:false, so it must
    leave no dangling wiring entry."""
    registry = config_loader.build_providers(config_loader.load_configs())
    wiring = config_loader.build_gateway_wiring(registry)
    assert "nim" not in wiring
    for gw, w in wiring.items():
        assert set(w) == {"chat_completions_url", "api_type"}, f"{gw} wiring fields drifted"


def test_env_example_documents_nvidia_api_key():
    """.env.example must solicit NVIDIA_API_KEY — the NIM key lives in
    ~/.hermes/.env on this box, never a literal token copy anywhere."""
    repo = Path(config_loader.__file__).resolve().parent
    text = (repo / ".env.example").read_text(encoding="utf-8")
    assert "NVIDIA_API_KEY=" in text


# ---------- f745288 quality follow-up: mis-typed fields must cost one file ----------


def test_load_configs_non_integer_display_skips_file(tmp_path, monkeypatch, capsys):
    """{"display": "one"} is a data error, not a code bug: it must cost ONLY
    its own file. Left to reach load_configs' sort it raises TypeError and
    takes every healthy gateway down — the exact import crash f745288
    exists to prevent."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "bad_display.json").write_text(json.dumps(
        {**_minimal_config("BadDisplay"), "display": "one"}))
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "bad_display.json" in stderr


def test_load_configs_boolean_display_skips_file(tmp_path, monkeypatch, capsys):
    """bool is an int subclass, so isinstance(x, int) alone lets True/False
    through as a display value; it must be rejected explicitly."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "bool_display.json").write_text(json.dumps(
        {**_minimal_config("BoolDisplay"), "display": True}))
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "bool_display.json" in stderr


def test_load_configs_token_file_non_string_path_skips_file(tmp_path, monkeypatch, capsys):
    """{"method": "token_file", "path": 123}: os.path.expanduser(int) raises
    TypeError inside _resolve_auth_token, whose contract is never-raises.
    The type error is a file-level data problem: skip + warn instead."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    _write_provider(providers_dir, {"method": "token_file", "path": 123,
                                    "key": "token"})
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "test.json" in stderr


def test_load_configs_token_file_non_string_path_env_skips_file(tmp_path, monkeypatch, capsys):
    """{"method": "token_file", "path_env": 123}: os.environ.get(int) raises
    TypeError inside the never-raises resolver — same class as `path`: skip
    + warn the file."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    _write_provider(providers_dir, {"method": "token_file", "path_env": 123,
                                    "key": "token"})
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "test.json" in stderr


def test_load_configs_token_file_missing_path_fields_skips_file(tmp_path, monkeypatch, capsys):
    """token_file with neither path nor path_env is a broken config: it can
    never resolve a token, so it must cost its own file with a warning, not
    load as a permanently-degraded ghost provider."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    _write_provider(providers_dir, {"method": "token_file", "key": "token"})
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"]
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "test.json" in stderr


# ---------- I-2: the providers dir must never empty the roster silently ----------


def test_load_configs_empty_providers_dir_warns(tmp_path, monkeypatch, capsys):
    """providers/ exists but holds no *.json: zero configs must come with a
    stderr warning — silent total roster loss is the failure being closed."""
    (tmp_path / "providers").mkdir()
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    configs = config_loader.load_configs()  # must not raise
    assert configs == []
    stderr = capsys.readouterr().err
    assert "no readable" in stderr and "degrading to zero providers" in stderr, (
        "an empty providers dir must warn, not go silent")


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
def test_load_configs_unreadable_providers_dir_warns(tmp_path, monkeypatch, capsys):
    """A chmod-000 providers/ dir is a real, reviewer-verified silent-death
    case: Path.glob swallows the PermissionError internally and returns []
    while is_dir() stays True — neither existing branch fires. The empty-
    glob guard must warn anyway."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "nous.json").write_text(json.dumps(_minimal_config("Nous Portal")))
    providers_dir.chmod(0o000)
    try:
        monkeypatch.setattr(config_loader, "REPO", tmp_path)
        configs = config_loader.load_configs()  # must not raise
        assert configs == []
        stderr = capsys.readouterr().err
        assert "no readable" in stderr and "degrading to zero providers" in stderr, (
            "a permission-denied providers dir must warn, not go silent")
    finally:
        providers_dir.chmod(0o755)  # restore so tmp cleanup works


# ---------- removal_hold_seconds: optional, validated positive int ----------


@pytest.mark.parametrize("bad", [123.5, 0, -5, "1800", True, None],
                         ids=["float", "zero", "neg", "str", "bool", "null"])
def test_removal_hold_seconds_field_must_be_positive_int(tmp_path, monkeypatch,
                                                         capsys, bad):
    """Optional "removal_hold_seconds" field: when present it must be a
    positive integer — junk here would silently disable or crash the hold
    comparison in pending_removals.settle — and it costs EXACTLY that file:
    skip + stderr warning naming it, matching the sibling skip tests."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {**_minimal_config("HoldTest"), "removal_hold_seconds": bad}
    (providers_dir / "holdtest.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert config_loader.load_configs() == []
    err = capsys.readouterr().err
    assert "skipping" in err and "holdtest.json" in err
    assert "removal_hold_seconds must be a positive integer" in err


def test_removal_hold_seconds_valid_roundtrips(tmp_path, monkeypatch, capsys):
    """A valid positive int survives load_configs verbatim, no skip warning."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {**_minimal_config("HoldTest"), "removal_hold_seconds": 1800}
    (providers_dir / "holdtest.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert config_loader.load_configs()[0]["removal_hold_seconds"] == 1800
    assert "skipping" not in capsys.readouterr().err


def test_removal_hold_accessor():
    """Accessor semantics: positive int passes; bool/str/neg/None/missing all
    return None. The accessor never trusts config raw — belt guard even
    though the loader already rejects junk."""
    from config_loader import removal_hold
    assert removal_hold({"removal_hold_seconds": 1800}) == 1800
    assert removal_hold({}) is None
    assert removal_hold({"removal_hold_seconds": True}) is None   # belt: bool
    assert removal_hold({"removal_hold_seconds": "1800"}) is None  # belt: str
    assert removal_hold({"removal_hold_seconds": -1}) is None
    assert removal_hold({"removal_hold_seconds": None}) is None   # belt: null


def test_amd_ships_removal_hold():
    """Decision 8: real AMD config carries the 30-min hold; nous stays None."""
    from config_loader import removal_hold
    registry = config_loader.build_providers(config_loader.load_configs())
    assert removal_hold(registry["amd"]) == 1800
    assert removal_hold(registry["nous"]) is None


# ---------- enabled: optional, strict bool, present-means-valid ----------


@pytest.mark.parametrize("bad", ["false", "no", 0, 1, None, [], {}],
                         ids=["str-false", "str-no", "int-0", "int-1",
                              "null", "list", "obj"])
def test_enabled_field_must_be_bool(tmp_path, monkeypatch, capsys, bad):
    """Optional "enabled": when present it must be a real bool. A JSON
    "false" string is truthy in Python and would silently keep the
    provider ON — the opposite of intent, invisible until someone
    notices the dashboard."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {"name": "X", "base_url": "https://x.com/v1",
           "detection": "all-free", "auth": {"method": "none"},
           "display": 0, "enabled": bad}
    (providers_dir / "x.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert config_loader.load_configs() == []
    assert "enabled must be a boolean" in capsys.readouterr().err


def test_enabled_true_loads(tmp_path, monkeypatch):
    """`enabled: true` is a well-formed bool and is accepted: the config
    loads and stays tracked. This test deliberately covers ONLY the true
    case — a false case here would assert the pre-gate behaviour and then
    break the moment Task 2's gate lands (see the note below)."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {"name": "X", "base_url": "https://x.com/v1",
           "detection": "all-free", "auth": {"method": "none"},
           "display": 0, "enabled": True}
    (providers_dir / "x.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert len(config_loader.load_configs()) == 1


def test_enabled_false_is_valid_but_not_yet_gated(tmp_path, monkeypatch, capsys):
    """The VALIDATOR accepts `enabled: false` — it is a real bool, so the
    config must NOT be skipped with a type warning. Whether it is then
    EXCLUDED from the roster is Task 2's gate, which lands after this
    task; asserting exclusion here would make this test fail in Task 1
    and pass in Task 2, i.e. a test that changes meaning mid-plan.

    This asserts only what Task 1 owns: no type-validation error.
    """
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    cfg = {"name": "X", "base_url": "https://x.com/v1",
           "detection": "all-free", "auth": {"method": "none"},
           "display": 0, "enabled": False}
    (providers_dir / "x.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    config_loader.load_configs()
    # The validator accepted it — no skip-with-warning. The config is then
    # loaded (pre-gate) or dropped by the gate (post-gate); Task 1 asserts
    # neither, because both are Task 2's business.
    #
    # CORRECTED 2026-09-28 (review finding, BLOCKING). This test previously
    # called capsys.readouterr() twice. The first call DRAINS the capture
    # buffer; the second returns CaptureResult(out='', err='') whatever
    # happened, so the "skipping" assertion could never fail — the test
    # asserted nothing about its stated purpose. Proven by execution: a
    # control asserting the message IS present in the second read fails,
    # while a single-read variant passes. Capture ONCE, assert against it.
    captured = capsys.readouterr()
    assert "enabled must be a boolean" not in captured.err
    assert "skipping" not in captured.err


# ---------- enabled: the gate — a validated false is dropped from the roster ----------


def test_disabled_provider_excluded_from_registry(tmp_path, monkeypatch, capsys):
    """enabled:false drops the provider from load_configs() -> the registry
    and its wiring map, so the tick never fetches it, the dashboard
    never renders a column, and the MCP server never lists it. The
    config FILE stays on disk, so re-enabling is a one-character edit."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "on.json").write_text(json.dumps(
        {"name": "On", "base_url": "https://on.com/v1", "detection": "all-free",
         "auth": {"method": "none"}, "display": 0, "enabled": True}))
    (providers_dir / "off.json").write_text(json.dumps(
        {"name": "Off", "base_url": "https://off.com/v1", "detection": "all-free",
         "auth": {"method": "none"}, "display": 1, "enabled": False}))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    registry = config_loader.build_providers(config_loader.load_configs())
    assert set(registry) == {"on"}
    # The wiring projection is gated by the same load_configs() call — a
    # disabled provider must not leave a dangling wiring entry.
    assert set(config_loader.build_gateway_wiring(registry)) == {"on"}
    assert "off" in capsys.readouterr().err       # visible, not silent


def test_absent_enabled_defaults_to_on(tmp_path, monkeypatch):
    """The field is OPTIONAL: every config predating it keeps working.
    Absent must mean enabled, or adding this field would switch off
    every provider in the repo."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "legacy.json").write_text(json.dumps(
        {"name": "Legacy", "base_url": "https://l.com/v1",
         "detection": "all-free", "auth": {"method": "none"}, "display": 0}))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert set(config_loader.build_providers(
        config_loader.load_configs())) == {"legacy"}


def test_disabling_is_silent_no_mass_removal(tmp_path, monkeypatch):
    """Regression guard for the property this whole feature rests on:
    a provider leaving the registry must NOT produce a 🔴 removal
    alert. load_filtered_roster drops non-registry keys and
    compute_events only walks the fetched map, so the tick is quiet."""
    import diffing
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "on.json").write_text(json.dumps(
        {"name": "On", "base_url": "https://on.com/v1", "detection": "all-free",
         "auth": {"method": "none"}, "display": 0}))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    reg = config_loader.build_providers(
        config_loader.load_configs())                # only "on"
    roster = {"tick_epoch": 0, "providers": {"on": ["a"], "off": ["x", "y"]}}
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(roster), encoding="utf-8")
    loaded = diffing.load_filtered_roster(roster_path, set(reg))
    assert loaded is not None
    assert "off" not in loaded["providers"]
    new_map, stale = diffing.apply_sticky(loaded["providers"], {"on": ["a"]})
    events, first_run = diffing.compute_events(loaded, new_map, registry=set(reg))
    assert events == {} and stale == [] and first_run is False


# ---------- catalogue seam fields: catalog_url / catalog_format (P1) ----------

CATALOG_BASE = {"name": "Catalog", "base_url": "https://api.example.com/v1",
                "detection": "all-free", "auth": {"method": "none"},
                "display": 0}


def _write_catalog_provider(tmp_path, monkeypatch, fname, config):
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir(parents=True, exist_ok=True)
    (providers_dir / fname).write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(config_loader, "REPO", tmp_path)


def _load_one(tmp_path, monkeypatch, **overrides):
    config = dict(CATALOG_BASE)
    config.update(overrides)
    _write_catalog_provider(tmp_path, monkeypatch, "catalog.json", config)
    return config_loader.load_configs()


def test_absent_catalog_fields_keep_the_legacy_config(tmp_path, monkeypatch):
    configs = _load_one(tmp_path, monkeypatch)
    assert len(configs) == 1
    assert "catalog_url" not in configs[0]
    assert "catalog_format" not in configs[0]


def test_catalog_url_and_format_are_accepted_and_bytes_preserved(
        tmp_path, monkeypatch):
    literal = ("https://API.example.com/v1/models"
               "?include=pricing%2Cmodalities&type=openai/chat-completions")
    configs = _load_one(tmp_path, monkeypatch,
                        detection="catalog-pricing",
                        catalog_url=literal,
                        catalog_format="input-output-models")
    assert len(configs) == 1
    assert configs[0]["catalog_url"] == literal, "URL bytes must not be normalised"
    assert configs[0]["catalog_format"] == "input-output-models"


def test_url_only_override_with_the_legacy_shape_is_allowed(
        tmp_path, monkeypatch):
    configs = _load_one(tmp_path, monkeypatch, detection="api-pricing",
                        catalog_url="https://mirror.example.net/models?page=1")
    assert len(configs) == 1
    assert configs[0]["catalog_url"] == "https://mirror.example.net/models?page=1"


def test_explicit_legacy_format_keeps_legacy_detection_choices(
        tmp_path, monkeypatch):
    configs = _load_one(tmp_path, monkeypatch, detection="id-suffix",
                        catalog_format="openai-models")
    assert len(configs) == 1
    assert configs[0]["catalog_format"] == "openai-models"


def test_anonymous_cross_origin_override_is_allowed(tmp_path, monkeypatch):
    configs = _load_one(tmp_path, monkeypatch, detection="catalog-pricing",
                        catalog_url="https://mirror.example.net/v1/models",
                        catalog_format="input-output-models")
    assert len(configs) == 1


def test_percent_encoded_hash_stays_literal_url_data(tmp_path, monkeypatch):
    """Only a literal `#` is a fragment delimiter; `%23` is URL data.

    Paired positive control for the empty-fragment rejection: the encoded form
    survives validation byte for byte in both path and query.
    """
    literal = "https://api.example.com/v1/mo%23dels?filter=a%23b"
    configs = _load_one(tmp_path, monkeypatch, detection="catalog-pricing",
                        catalog_url=literal,
                        catalog_format="input-output-models")
    assert len(configs) == 1
    assert configs[0]["catalog_url"] == literal


def test_same_origin_credential_bearing_override_keeps_existing_auth(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CATALOG_TEST_KEY", "env-token")
    configs = _load_one(
        tmp_path, monkeypatch, detection="catalog-pricing",
        auth={"method": "env_var", "env_key": "CATALOG_TEST_KEY"},
        catalog_url="https://api.example.com/v2/models",
        catalog_format="input-output-models")
    assert len(configs) == 1
    assert configs[0]["_token"] == "env-token"


def test_build_gateway_wiring_reads_base_url_not_catalog_url():
    config = dict(CATALOG_BASE, detection="catalog-pricing",
                  catalog_url="https://mirror.example.net/v1/models",
                  catalog_format="input-output-models")
    wiring = config_loader.build_gateway_wiring({"catalog": config})
    assert wiring["catalog"]["chat_completions_url"] == (
        "https://api.example.com/v1/chat/completions")


BAD_CATALOG_FIELDS = [
    ("url-null", {"catalog_url": None}),
    ("url-int", {"catalog_url": 7}),
    ("url-list", {"catalog_url": ["https://api.example.com/v1/models"]}),
    ("url-blank", {"catalog_url": ""}),
    ("url-padded", {"catalog_url": " https://api.example.com/v1/models"}),
    ("url-trailing-space", {"catalog_url": "https://api.example.com/v1/models "}),
    ("url-http", {"catalog_url": "http://api.example.com/v1/models"}),
    ("url-relative", {"catalog_url": "//api.example.com/v1/models"}),
    ("url-no-host", {"catalog_url": "https:///v1/models"}),
    ("url-userinfo", {"catalog_url": "https://u:p@api.example.com/v1/models"}),
    ("url-fragment", {"catalog_url": "https://api.example.com/v1/models#frag"}),
    ("url-empty-fragment",
     {"catalog_url": "https://api.example.com/v1/models#"}),
    ("url-empty-fragment-after-query",
     {"catalog_url": "https://api.example.com/v1/models?a=1#"}),
    ("url-control", {"catalog_url": "https://api.example.com/v1/mo\u0000dels"}),
    ("url-inner-space", {"catalog_url": "https://api.example.com/v1/a b"}),
    ("url-bad-port", {"catalog_url": "https://api.example.com:nope/v1/models"}),
    ("format-null", {"catalog_format": None}),
    ("format-int", {"catalog_format": 1}),
    ("format-blank", {"catalog_format": ""}),
    ("format-padded", {"catalog_format": " input-output-models"}),
    # Deliberately bogus, not a roadmap name: `provider-models` is P4's family
    # and must not be spent as an "unknown" negative here (P3 SPEC F1).
    ("format-unknown", {"catalog_format": "not-a-catalog-format"}),
    ("format-legacy-case", {"catalog_format": "OpenAI-Models"}),
    ("pair-missing-format", {"detection": "catalog-pricing"}),
    ("pair-legacy-format", {"detection": "catalog-pricing",
                            "catalog_format": "openai-models"}),
    ("pair-wrong-detection", {"detection": "api-pricing",
                              "catalog_format": "input-output-models"}),
    ("pair-all-free", {"detection": "all-free",
                       "catalog_format": "input-output-models"}),
    ("cred-env-cross-origin",
     {"detection": "catalog-pricing",
      "catalog_format": "input-output-models",
      "auth": {"method": "env_var", "env_key": "CATALOG_TEST_KEY"},
      "catalog_url": "https://mirror.example.net/v1/models"}),
    ("cred-token-file-cross-origin",
     {"detection": "catalog-pricing",
      "catalog_format": "input-output-models",
      "auth": {"method": "token_file", "path": "/nonexistent/token.json",
               "key": "token"},
      "catalog_url": "https://mirror.example.net/v1/models"}),
]


@pytest.mark.parametrize("case,overrides", BAD_CATALOG_FIELDS,
                         ids=[case for case, _ in BAD_CATALOG_FIELDS])
def test_bad_catalog_field_skips_only_that_file(
        tmp_path, monkeypatch, capsys, case, overrides):
    """Every rejection class costs exactly one file; a healthy sibling loads.

    The credential-bearing cases also prove the rejection happens BEFORE the
    credential lookup: no `auth degraded` line and no token file read.
    """
    bad = dict(CATALOG_BASE, **overrides)
    _write_catalog_provider(tmp_path, monkeypatch, "bad.json", bad)
    _write_catalog_provider(tmp_path, monkeypatch, "good.json",
                    dict(CATALOG_BASE, name="Good"))
    configs = config_loader.load_configs()  # must not raise
    assert [c["name"] for c in configs] == ["Good"], case
    stderr = capsys.readouterr().err
    assert "skipping" in stderr.lower() and "bad.json" in stderr, case
    assert "degraded" not in stderr, (
        f"{case}: rejected before any credential resolution")
    if case == "format-unknown":
        # This case pins the UNKNOWN-FORMAT branch itself. A registered family
        # here (e.g. `newapi-pricing`) would still be skipped, but only because
        # `all-free` is an incompatible detection — the PAIRING rule, not the
        # registration check — so asserting the rejection phrase is what keeps
        # the case honest (P3 SPEC F1).
        assert "unknown catalog_format" in stderr, (
            f"{case}: expected the unknown-format branch, got: {stderr!r}")


# ---------- shipped Felo config (P2) ----------

def test_channel_catalog_format_pairs_with_catalog_pricing(tmp_path, monkeypatch):
    configs = _load_one(
        tmp_path, monkeypatch, detection="catalog-pricing",
        catalog_url="https://openapi.felo.ai/api/platform/model-catalog",
        catalog_format="channel-catalog")
    assert len(configs) == 1
    assert configs[0]["catalog_format"] == "channel-catalog"


def test_shipped_felo_config_loads_from_a_one_file_directory(
        tmp_path, monkeypatch, capsys):
    """The real shipped config, copied verbatim, through the real loader.

    A one-file synthetic providers dir keeps the run off the real registry and
    resolves no credential: `auth: none` must leave `_token` None.
    """
    import shutil

    shipped = Path(config_loader.__file__).resolve().parent / "providers" / "felo.json"
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    shutil.copy2(shipped, providers_dir / "felo.json")
    monkeypatch.setattr(config_loader, "REPO", tmp_path)

    configs = config_loader.load_configs()
    assert [c["name"] for c in configs] == ["Felo"]
    cfg = configs[0]
    assert cfg["base_url"] == "https://openapi.felo.ai/api/v1"
    assert cfg["catalog_url"] == (
        "https://openapi.felo.ai/api/platform/model-catalog")
    assert cfg["catalog_format"] == "channel-catalog"
    assert cfg["detection"] == "catalog-pricing"
    assert cfg["auth"] == {"method": "none"}
    assert cfg["display"] == 8
    assert cfg["signup_url"] == "https://openapi.felo.ai/console/api-keys"
    assert cfg["_stem"] == "felo"
    assert cfg["_token"] is None, "anonymous auth resolves no credential"
    assert "skipping" not in capsys.readouterr().err

    wiring = config_loader.build_gateway_wiring(
        config_loader.build_providers(configs))
    assert wiring["felo"] == {
        "chat_completions_url": "https://openapi.felo.ai/api/v1/chat/completions",
        "api_type": "openai_compatible",
    }


# ---------- shipped TokenRA config (P3) ----------

def test_newapi_pricing_format_pairs_with_catalog_pricing(tmp_path, monkeypatch):
    configs = _load_one(
        tmp_path, monkeypatch, detection="catalog-pricing",
        catalog_url="https://tokenra.io/api/pricing",
        catalog_format="newapi-pricing")
    assert len(configs) == 1
    assert configs[0]["catalog_format"] == "newapi-pricing"


def test_shipped_tokenra_config_loads_from_a_one_file_directory(
        tmp_path, monkeypatch, capsys):
    """The real shipped config, copied verbatim, through the real loader.

    A one-file synthetic providers dir keeps the run off the real registry and
    resolves no credential: `auth: none` must leave `_token` None. The
    published inference URL is still derived from `base_url`, never from
    `catalog_url`.
    """
    import shutil

    shipped = (Path(config_loader.__file__).resolve().parent
               / "providers" / "tokenra.json")
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    shutil.copy2(shipped, providers_dir / "tokenra.json")
    monkeypatch.setattr(config_loader, "REPO", tmp_path)

    configs = config_loader.load_configs()
    assert [c["name"] for c in configs] == ["TokenRA"]
    cfg = configs[0]
    assert cfg["base_url"] == "https://tokenra.io/v1"
    assert cfg["catalog_url"] == "https://tokenra.io/api/pricing"
    assert cfg["catalog_format"] == "newapi-pricing"
    assert cfg["detection"] == "catalog-pricing"
    assert cfg["auth"] == {"method": "none"}
    assert cfg["display"] == 9
    assert cfg["signup_url"] == "https://tokenra.io/register"
    assert cfg["_stem"] == "tokenra"
    assert cfg["_token"] is None, "anonymous auth resolves no credential"
    assert "skipping" not in capsys.readouterr().err

    wiring = config_loader.build_gateway_wiring(
        config_loader.build_providers(configs))
    assert wiring["tokenra"] == {
        "chat_completions_url": "https://tokenra.io/v1/chat/completions",
        "api_type": "openai_compatible",
    }


# ---------- shipped Infron config (P4) ----------

def test_provider_models_format_pairs_with_catalog_pricing(
        tmp_path, monkeypatch):
    configs = _load_one(
        tmp_path, monkeypatch, detection="catalog-pricing",
        catalog_url="https://llm.onerouter.pro/v1/models",
        catalog_format="provider-models")
    assert len(configs) == 1
    assert configs[0]["catalog_format"] == "provider-models"


def test_shipped_infron_config_loads_from_a_one_file_directory(
        tmp_path, monkeypatch, capsys):
    """The real shipped config, copied verbatim, through the real loader.

    A one-file synthetic providers dir keeps the run off the real registry and
    resolves no credential: `auth: none` must leave `_token` None. The published
    inference URL is still derived from `base_url`, never from `catalog_url`,
    and the literal discovery URL is preserved byte for byte.
    """
    import shutil

    shipped = (Path(config_loader.__file__).resolve().parent
               / "providers" / "infron.json")
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    shutil.copy2(shipped, providers_dir / "infron.json")
    monkeypatch.setattr(config_loader, "REPO", tmp_path)

    configs = config_loader.load_configs()
    assert [c["name"] for c in configs] == ["Infron"]
    cfg = configs[0]
    assert cfg["base_url"] == "https://llm.onerouter.pro/v1"
    assert cfg["catalog_url"] == "https://llm.onerouter.pro/v1/models"
    assert cfg["catalog_format"] == "provider-models"
    assert cfg["detection"] == "catalog-pricing"
    assert cfg["auth"] == {"method": "none"}
    assert cfg["display"] == 10
    assert cfg["signup_url"] == "https://infron.ai/login"
    assert "2026-11-22" in cfg["limits_note"]
    assert cfg["_stem"] == "infron"
    assert cfg["_token"] is None, "anonymous auth resolves no credential"
    assert "skipping" not in capsys.readouterr().err

    wiring = config_loader.build_gateway_wiring(
        config_loader.build_providers(configs))
    assert wiring["infron"] == {
        "chat_completions_url": "https://llm.onerouter.pro/v1/chat/completions",
        "api_type": "openai_compatible",
    }


# ---------- shipped AI/ML API config (P5) ----------

def test_typed_unit_models_format_pairs_with_catalog_pricing(
        tmp_path, monkeypatch):
    configs = _load_one(
        tmp_path, monkeypatch, detection="catalog-pricing",
        catalog_url=("https://api.aimlapi.com/v1/models?include=pricing,"
                     "modalities&type=openai/chat-completions"),
        catalog_format="typed-unit-models")
    assert len(configs) == 1
    assert configs[0]["catalog_format"] == "typed-unit-models"


def test_shipped_aimlapi_config_loads_from_a_one_file_directory(
        tmp_path, monkeypatch, capsys):
    """The real shipped config, copied verbatim, through the real loader.

    A one-file synthetic providers dir keeps the run off the real registry and
    resolves no credential: `auth: none` must leave `_token` None. The literal
    discovery URL, including its exact query and pair order, is preserved byte
    for byte; the published inference URL is still derived from `base_url`.
    """
    import shutil

    shipped = (Path(config_loader.__file__).resolve().parent
               / "providers" / "aimlapi.json")
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    shutil.copy2(shipped, providers_dir / "aimlapi.json")
    monkeypatch.setattr(config_loader, "REPO", tmp_path)

    configs = config_loader.load_configs()
    assert [c["name"] for c in configs] == ["AI/ML API"]
    cfg = configs[0]
    assert cfg["base_url"] == "https://api.aimlapi.com/v1"
    assert cfg["catalog_url"] == (
        "https://api.aimlapi.com/v1/models?include=pricing,modalities"
        "&type=openai/chat-completions")
    assert cfg["catalog_format"] == "typed-unit-models"
    assert cfg["detection"] == "catalog-pricing"
    assert cfg["auth"] == {"method": "none"}
    assert cfg["display"] == 11
    assert cfg["signup_url"] == "https://aimlapi.com/app/sign-up"
    assert "2026-11-22" in cfg["limits_note"]
    assert cfg.get("enabled", True) is not False, "shipped gateway stays enabled"
    assert cfg["_stem"] == "aimlapi"
    assert cfg["_token"] is None, "anonymous auth resolves no credential"
    assert "skipping" not in capsys.readouterr().err

    wiring = config_loader.build_gateway_wiring(
        config_loader.build_providers(configs))
    assert wiring["aimlapi"] == {
        "chat_completions_url": "https://api.aimlapi.com/v1/chat/completions",
        "api_type": "openai_compatible",
    }


def test_every_shipped_config_declares_a_distinct_display_and_a_registered_format():
    """Public JSON reads only: no loader, credential or registry execution.

    L2's metadata-only display oracle is not universal, so this checks the
    ACTUAL shipped config files rather than an inventory subset.
    """
    import catalog_adapters

    repo = Path(config_loader.__file__).resolve().parent
    displays = {}
    formats = {}
    for path in sorted((repo / "providers").glob("*.json")):
        cfg = json.loads(path.read_text(encoding="utf-8"))
        key = cfg.get("roster_key") or path.stem
        displays[key] = cfg.get("display", 0)
        formats[key] = cfg.get("catalog_format")
        if cfg.get("catalog_format") is not None:
            assert catalog_adapters.is_registered_format(
                cfg["catalog_format"]), path.name
    assert len(set(displays.values())) == len(displays), displays
    assert formats["infron"] == catalog_adapters.PROVIDER_MODELS_FORMAT
    assert formats["aimlapi"] == catalog_adapters.TYPED_UNIT_MODELS_FORMAT
