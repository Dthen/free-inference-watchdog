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
        try:
            with open(path, encoding="utf-8") as f:
                config = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Config {path.name}: invalid JSON: {exc}") from exc

        required = {"name", "base_url", "detection", "auth"}
        missing = required - set(config.keys())
        if missing:
            raise ValueError(f"Config {path.name} missing: {missing}")

        auth = config.get("auth", {})
        method = auth.get("method")
        if method == "env_var":
            env_key = auth.get("env_key")
            if not env_key:
                raise ValueError(f"Config {path.name}: env_var auth requires env_key field")
            token = os.environ.get(env_key, "")
        elif method == "token_file":
            token_path = os.path.expanduser(auth.get("path", ""))
            try:
                with open(token_path, encoding="utf-8") as f:
                    token_data = json.load(f)
            except FileNotFoundError:
                raise ValueError(f"Config {path.name}: token file not found: {token_path}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"Config {path.name}: token file invalid JSON: {exc}")
            token = _resolve_json_path(token_data, auth.get("key", ""))
        else:
            token = None

        config["_token"] = token
        configs.append(config)

    return sorted(configs, key=lambda c: c.get("display", 0))