# tests/test_config_loader.py
import pytest
import json
import os
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


def test_load_configs_token_file_path_env_unset_raises(tmp_path, monkeypatch):
    """token_file + path_env with the env var unset (neither in os.environ nor
    the project .env) must raise ValueError naming the missing env var —
    fail loudly at load time, never silently fetch unauthenticated."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    monkeypatch.delenv("TEST_AUTH_FILE", raising=False)
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "token"})
    # REPO points at tmp_path: no .env there, so no fallback can resolve it.
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    with pytest.raises(ValueError, match="TEST_AUTH_FILE"):
        config_loader.load_configs()


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


def test_load_configs_token_file_path_env_missing_file_raises(tmp_path, monkeypatch):
    """path_env resolves but the file does not exist -> loud ValueError,
    same contract as the legacy path field."""
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    monkeypatch.setenv("TEST_AUTH_FILE", str(tmp_path / "nope.json"))
    _write_provider(providers_dir, {"method": "token_file",
                                    "path_env": "TEST_AUTH_FILE",
                                    "key": "token"})
    monkeypatch.setattr(config_loader, "REPO", tmp_path)
    with pytest.raises(ValueError, match="token file not found"):
        config_loader.load_configs()


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