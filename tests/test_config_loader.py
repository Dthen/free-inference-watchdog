# tests/test_config_loader.py
import pytest
import json
from pathlib import Path

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


def test_load_configs_missing_required_field(tmp_path, monkeypatch):
    """Config with missing required field raises ValueError."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    config = {"name": "Test", "base_url": "https://example.com/v1",
              "detection": "all-free"}  # missing auth
    (providers_dir / "test.json").write_text(json.dumps(config))
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    with pytest.raises(ValueError):
        config_loader.load_configs()


def test_load_configs_malformed_json(tmp_path, monkeypatch):
    """Config with malformed JSON raises ValueError."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    (providers_dir / "bad.json").write_text("not json{{{")
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    with pytest.raises(ValueError):
        config_loader.load_configs()


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
