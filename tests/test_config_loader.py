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