"""Load and validate provider configs."""
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent


def _resolve_json_path(data, path):
    """Resolve dot-separated JSON path like 'providers.nous.access_token'."""
    if not path:
        return None
    current = data
    for key in path.split("."):
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def load_configs():
    """Load all providers/*.json, validate schema, resolve auth, sort by display."""
    configs = []
    for path in sorted((REPO / "providers").glob("*.json")):
        with open(path, encoding="utf-8") as f:
            config = json.load(f)

        required = {"name", "base_url", "detection", "auth"}
        missing = required - set(config.keys())
        if missing:
            raise ValueError(f"Config {path.name} missing: {missing}")

        auth = config.get("auth", {})
        method = auth.get("method")
        if method == "env_var":
            token = os.environ.get(auth.get("env_key", ""), "")
        elif method == "token_file":
            token_path = os.path.expanduser(auth.get("path", ""))
            with open(token_path, encoding="utf-8") as f:
                token_data = json.load(f)
            token = _resolve_json_path(token_data, auth.get("key", ""))
        else:
            token = None

        config["_token"] = token
        configs.append(config)

    return sorted(configs, key=lambda c: c.get("display", 0))