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
    """PROVIDERS (via build_providers) inherits the per-file skip: a broken
    file costs exactly its own provider key; healthy keys survive intact."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "nous.json").write_text(json.dumps(_minimal_config("Nous Portal", 0)))
    (providers_dir / "kilo.json").write_text("{ broken")
    (providers_dir / "amd.json").write_text(json.dumps(_minimal_config("AMD Radeon", 1)))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    providers = config_loader.build_providers()
    assert set(providers) == {"nous", "amd"}
    assert "kilo" not in providers, "the broken file must cost only kilo"


def test_import_never_raises_with_a_broken_config(tmp_path):
    """End-to-end proof of the locked contract: importing config_loader with
    one invalid providers/*.json on disk succeeds, exposing only the valid
    subset in PROVIDERS. A subprocess gives a genuinely fresh import, so the
    module-level build is what runs."""
    repo = Path(config_loader.__file__).resolve().parent
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(repo / "config_loader.py", src)
    shutil.copy(repo / "envfile.py", src)
    providers_dir = src / "providers"
    providers_dir.mkdir()
    (providers_dir / "good.json").write_text(json.dumps(_minimal_config("Good")))
    (providers_dir / "bad.json").write_text("}{ not json")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import config_loader, json; "
         "print(json.dumps(sorted(config_loader.PROVIDERS)))"],
        cwd=src, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(src)},
    )
    assert proc.returncode == 0, f"import raised: {proc.stderr}"
    assert json.loads(proc.stdout.strip()) == ["good"]
    assert "bad.json" in proc.stderr


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
    """kilo/openrouter configs carry the exact router ids previously filtered
    by hardcoded display logic (cross-checked against state/roster.json)."""
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
    """Migration safety net: the 7 shipped configs must resolve to the
    SAME roster keys as before the _PROVIDER_KEY_MAP deletion (they were
    map values: nous, tokenrouter, kilo, openrouter, amd, bai, nim).
    Update expected when a gateway file is legitimately added/removed."""
    expected = {"nous", "tokenrouter", "kilo", "openrouter", "amd", "bai", "nim"}
    assert set(config_loader.PROVIDERS) == expected


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
    assert "nvidia-nim" in config_loader.build_providers()


def test_roster_key_overrides_stem(tmp_path, monkeypatch):
    """The explicit roster_key field wins over the file stem."""
    cfg = {"name": "NVIDIA NIM", "base_url": "https://x.com/v1",
           "detection": "all-free", "auth": {"method": "none"}, "display": 0,
           "roster_key": "nim"}
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "nvidia-nim.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    assert set(config_loader.build_providers()) == {"nim"}


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
    assert set(config_loader.build_providers()) == {"my key"}


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
    bai = config_loader.PROVIDERS["bai"]
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
    providers = config_loader.build_providers()
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
    env_var auth, and display 6 so NIM sorts AFTER bai in every user-visible
    surface."""
    repo = Path(config_loader.__file__).resolve().parent
    cfg = json.loads((repo / "providers" / "nim.json").read_text(encoding="utf-8"))
    assert cfg["name"] == "NVIDIA NIM"
    assert cfg["base_url"] == "https://integrate.api.nvidia.com/v1"
    assert cfg["detection"] == "all-free"
    assert cfg["auth"] == {"method": "env_var", "env_key": "NVIDIA_API_KEY"}
    assert cfg["display"] == 6
    configs = config_loader.load_configs()
    assert len(configs) == 7, "real providers/ dir must hold seven configs"
    assert configs[-1]["name"] == "NVIDIA NIM", "nim must sort last"


def test_build_gateway_wiring_shape_and_nim():
    """GATEWAY_WIRING has one entry per gateway; the 'auth' field is GONE
    (operator: 'Bearer <your API key>' read the same for every gateway — it
    was dropped from all site/MCP surfaces), and only the two remaining
    fields survive."""
    wiring = config_loader.build_gateway_wiring()
    assert "nim" in wiring
    assert wiring["nim"] == {
        "chat_completions_url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "api_type": "openai_compatible",
    }
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
