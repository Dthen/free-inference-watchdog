"""Load and validate provider configs."""
import json
import os
import sys
from pathlib import Path

def _lookup_env(key):
    """Resolve an env var for auth: os.environ first, then the project-local
    .env (same source the webhook uses).

    The cron wrapper never sources .env into the process environment — it
    stays a file read at tick time so the repo is agent-agnostic — so a
    value that lives only in .env must still be resolvable here. REPO is read
    at call time (tests point it at a tmp dir).
    """
    value = os.environ.get(key, "")
    if value:
        return value
    try:
        from envfile import parse_envfile
        return parse_envfile(REPO / ".env").get(key, "")
    except Exception:
        return ""  # unreadable .env -> no fallback value; caller decides


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
        token = None
        try:
            if method == "env_var":
                env_key = auth.get("env_key")
                if not env_key:
                    raise ValueError(f"Config {path.name}: env_var auth requires env_key field")
                token = os.environ.get(env_key, "")
            elif method == "token_file":
                path_env = auth.get("path_env")
                if path_env:
                    token_path_raw = _lookup_env(path_env)
                    if not token_path_raw:
                        raise ValueError(
                            f"Config {path.name}: token_file auth requires env var "
                            f"{path_env} to be set")
                else:
                    token_path_raw = auth.get("path", "")
                token_path = os.path.expanduser(token_path_raw)
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
        except (ValueError, OSError) as exc:
            # Degrade, don't die: a broken env (e.g. NOUS_AUTH_FILE unset)
            # or an unreadable token file (OSError: missing, permission
            # denied, is-a-directory) must not kill the watchdog at import —
            # the cron wrapper treats exit 1 as routine and stays silent.
            # Log to stderr so the operator sees why a provider is degraded.
            print(f"config_loader: provider {config.get('name', path.name)} auth degraded: {exc}",
                  file=sys.stderr)
            token = None

        config["_token"] = token
        configs.append(config)

    return sorted(configs, key=lambda c: c.get("display", 0))


# ---------- provider key mapping ----------

_PROVIDER_KEY_MAP = {
    "Nous Portal": "nous",
    "TokenRouter": "tokenrouter",
    "Kilo Gateway": "kilo",
    "OpenRouter": "openrouter",
    "AMD Radeon": "amd",
    "B.AI": "bai",
}


def _provider_key(config):
    """Map a config to its canonical provider key."""
    return _PROVIDER_KEY_MAP.get(config["name"], config["name"].lower().replace(" ", "_"))


def build_providers():
    """Build PROVIDERS dict mapping provider key -> config dict."""
    return {_provider_key(cfg): cfg for cfg in load_configs()}


def build_gateway_wiring():
    """Build GATEWAY_WIRING dict mapping provider key -> wiring info."""
    wiring = {}
    for cfg in load_configs():
        key = _provider_key(cfg)
        auth = cfg.get("auth", {})
        method = auth.get("method", "none")
        if method == "env_var":
            env_key = auth.get("env_key", "")
            auth_str = f"Bearer <your {env_key}>"
        elif method == "token_file":
            auth_str = "Bearer <from token file>"
        else:
            auth_str = "Bearer <your API key>"
        wiring[key] = {
            "chat_completions_url": f"{cfg['base_url'].rstrip('/')}/chat/completions",
            "auth": auth_str,
            "api_type": "openai_compatible",
        }
    return wiring


PROVIDERS = build_providers()
GATEWAY_WIRING = build_gateway_wiring()