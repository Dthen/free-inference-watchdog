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


def _validate_and_read(path):
    """Read + schema-validate one providers/*.json. Raises ValueError/OSError
    on any file-level problem (unreadable, invalid JSON, wrong shape, missing
    or invalid required fields, unimplemented auth method)."""
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError(f"expected a JSON object, got {type(config).__name__}")

    required = {"name", "base_url", "detection", "auth"}
    missing = required - set(config.keys())
    if missing:
        raise ValueError(f"missing: {missing}")
    auth = config["auth"]
    if not isinstance(auth, dict):
        raise ValueError(f"auth must be an object, got {type(auth).__name__}")
    if auth.get("method") not in ("none", "env_var", "token_file"):
        # A config naming an auth method the loader does not implement is
        # broken, not keyless — fetching unauthenticated would mask the typo.
        raise ValueError(f"unknown auth method: {auth.get('method')!r}")
    if auth.get("method") == "env_var" and not auth.get("env_key"):
        raise ValueError("env_var auth requires env_key field")
    if auth.get("method") == "token_file":
        # _resolve_auth_token's contract is never-raises; a non-str path or
        # path_env reaches os.path.expanduser/os.environ.get as a TypeError
        # code bug, and token_file with neither field can never resolve.
        # Both are file-level data errors: reject here so the skip+warn
        # path in load_configs handles them.
        path_env = auth.get("path_env")
        path = auth.get("path")
        if path_env is None and path is None:
            raise ValueError("token_file auth requires path_env or path field")
        if path_env is not None and not isinstance(path_env, str):
            raise ValueError(
                f"token_file path_env must be a string, "
                f"got {type(path_env).__name__}")
        if path is not None and not isinstance(path, str):
            raise ValueError(
                f"token_file path must be a string, got {type(path).__name__}")
    display = config.get("display", 0)
    if not isinstance(display, int) or isinstance(display, bool):
        # bool is an int subclass; True/False would sort quietly wrong.
        # A non-int display breaks load_configs' sort — reject per-file.
        raise ValueError(
            f"display must be an integer, got {type(display).__name__}")
    if "ignored_slugs" in config:
        ignored = config["ignored_slugs"]
        # Optional field: when the key is present (even as explicit JSON
        # null) it must be a list of non-empty strings — a bare string or a
        # list of junk would turn the exact-match exclusion in
        # providers.fetch_provider into a silent no-op (or worse,
        # per-character set membership), and null would crash set(None).
        # Reject per-file so the skip+warn path in load_configs handles it.
        if not isinstance(ignored, list):
            raise ValueError(
                f"ignored_slugs must be a list, got {type(ignored).__name__}")
        for entry in ignored:
            # Padded entries (" x ") survive a strip()-emptiness check yet
            # never exact-match a real id downstream — the silent no-op
            # this validation exists to kill. Reject loudly per-file.
            if (not isinstance(entry, str) or not entry.strip()
                    or entry != entry.strip()):
                raise ValueError(
                    "ignored_slugs entries must be non-empty, unpadded "
                    f"strings, got {entry!r}")
    if "roster_key" in config:
        roster_key = config["roster_key"]
        # Optional field: the explicit roster-key override (named roster_key,
        # not key — auth dicts already own a "key" JSON path). A non-string
        # or blank value would silently fall through to the stem fallback
        # and re-route the provider's roster identity — reject per-file.
        if not isinstance(roster_key, str) or not roster_key.strip():
            raise ValueError(
                f"roster_key must be a non-empty string, got {roster_key!r}")
    if "removal_hold_seconds" in config:
        hold = config["removal_hold_seconds"]
        # Optional field, but PRESENT means VALID (same boundary doctrine as
        # display/ignored_slugs/roster_key): junk here would silently disable
        # or crash the hold comparison in pending_removals.settle — reject
        # per-file so load_configs' skip+warn path handles it.
        if not isinstance(hold, int) or isinstance(hold, bool) or hold <= 0:
            raise ValueError(
                f"removal_hold_seconds must be a positive integer, got {hold!r}")
    if "probe" in config:
        probe = config["probe"]
        # Optional block: per-provider zero-credit-probe dialect. Shape
        # errors here would TypeError at probe time (max_tokens) or
        # silently misclassify (paid_signals) — reject per-file.
        if not isinstance(probe, dict):
            raise ValueError(
                f"probe must be an object, got {type(probe).__name__}")
        # Presence is the obligation, not key-not-None: an explicit JSON
        # null must FAIL here, not sail through and defeat probe-time
        # cfg.get(key, default) — a present-but-null key skips the default,
        # shipping null max_tokens (permanent silent DEFER) or None
        # paid_signals (TypeError swallowed into all-DEFER forever). Same
        # boundary doctrine as ignored_slugs: present, even as null, valid.
        if "max_tokens" in probe:
            max_tokens = probe["max_tokens"]
            if (not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
                    or max_tokens <= 0):
                # bool is an int subclass; True would sneak through as 1 token
                # (rejected by b.ai) — same guard display applies.
                raise ValueError(
                    f"probe.max_tokens must be a positive integer, "
                    f"got {max_tokens!r}")
        if "paid_signals" in probe:
            signals = probe["paid_signals"]
            if not isinstance(signals, list) or not signals:
                raise ValueError(
                    "probe.paid_signals must be a non-empty list, "
                    f"got {signals!r}")
            for sig in signals:
                if (not isinstance(sig, dict)
                        or not isinstance(sig.get("status"), int)
                        or isinstance(sig.get("status"), bool)):
                    raise ValueError(
                        "probe.paid_signals entries need integer status, "
                        f"got {sig!r}")
                for key in ("all_of", "any_of"):
                    if key not in sig:
                        continue
                    substrs = sig[key]
                    # An empty list is as vacuous as its absence would be
                    # PAID-classifying every response with that status —
                    # the too-blunt outcome the no-conditions guard below
                    # exists to kill. Padded/blank substrings survive a
                    # strip() check yet never match a lowered body — the
                    # silent-no-op class ignored_slugs validation kills;
                    # same rule here.
                    if (not isinstance(substrs, list) or not substrs
                            or not all(isinstance(s, str) and s.strip()
                                       and s == s.strip() for s in substrs)):
                        raise ValueError(
                            "probe.paid_signals "
                            f"{key} must be a non-empty list of non-empty, "
                            f"unpadded strings, got {substrs!r}")
                if "all_of" not in sig and "any_of" not in sig:
                    # A status-only signal PAIDs every error of that code —
                    # too blunt to be a dialect; require a body condition.
                    raise ValueError(
                        "probe.paid_signals entries need all_of or any_of, "
                        f"got {sig!r}")
    return config


def _resolve_auth_token(config, fname):
    """Resolve the _token for a validated config. Auth/environment problems
    (broken env var, unreadable token file) DEGRADE the provider to
    _token=None with a stderr warning — a healthy provider must not lose its
    config because its credential is momentarily unresolvable. Never raises.
    """
    auth = config["auth"]
    method = auth.get("method")
    try:
        if method == "env_var":
            return os.environ.get(auth["env_key"], "")
        if method == "token_file":
            path_env = auth.get("path_env")
            if path_env:
                token_path_raw = _lookup_env(path_env)
                if not token_path_raw:
                    raise ValueError(
                        f"token_file auth requires env var "
                        f"{path_env} to be set")
            else:
                token_path_raw = auth.get("path", "")
            token_path = os.path.expanduser(token_path_raw)
            try:
                with open(token_path, encoding="utf-8") as f:
                    token_data = json.load(f)
            except FileNotFoundError:
                raise ValueError(f"token file not found: {token_path}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"token file invalid JSON: {exc}") from exc
            return _resolve_json_path(token_data, auth.get("key", ""))
        return None  # method == "none"
    except (ValueError, OSError) as exc:
        # Degrade, don't die: a broken env (e.g. NOUS_AUTH_FILE unset)
        # or an unreadable token file (OSError: missing, permission
        # denied, is-a-directory) must not kill the watchdog at import —
        # the cron wrapper treats exit 1 as routine and stays silent.
        # Log to stderr so the operator sees why a provider is degraded.
        print(f"config_loader: provider {config.get('name', fname)} "
              f"auth degraded: {exc}", file=sys.stderr)
        return None


def load_configs():
    """Load all providers/*.json, validate schema, resolve auth, sort by display.

    Degradation contract: a per-file problem (unreadable file, invalid JSON,
    missing/invalid fields, an auth method the loader does not implement)
    costs EXACTLY that file — it is skipped with a `config: skipping ...`
    warning on stderr and the rest load normally. A missing or unreadable
    providers dir likewise degrades to [] with a stderr warning — as does an
    existing dir that yields no readable *.json (Path.glob swallows
    PermissionError internally, so a chmod-000 dir looks empty). This
    function must never raise for file/dir problems: PROVIDERS is built at
    import time, and inference_watchdog/build_site/mcp_server all crash
    before doing anything if the import raises, silently every hour.
    """
    configs = []
    providers_dir = REPO / "providers"
    try:
        paths = sorted(providers_dir.glob("*.json"))
    except OSError as exc:
        print(f"config: skipping providers dir {providers_dir}: {exc}",
              file=sys.stderr)
        return configs
    if not providers_dir.is_dir():
        # ENOTDIR/unreadable dirs yield an empty glob on some platforms
        # instead of raising — warn explicitly so the degradation is visible.
        print(f"config: skipping providers dir {providers_dir}: "
              f"not a readable directory", file=sys.stderr)
        return configs
    if not paths:
        # is_dir() True but the glob found nothing readable: a legitimately
        # empty dir, or a permission-denied one whose OSError glob() swallows
        # internally. Either way the whole roster just vanished silently —
        # say so loudly; total roster loss must never go unannounced.
        print("config: no readable *.json found in providers/ directory "
              "- degrading to zero providers", file=sys.stderr)
        return configs
    for path in paths:
        try:
            config = _validate_and_read(path)
        except (OSError, ValueError) as exc:
            print(f"config: skipping providers/{path.name}: {exc}",
                  file=sys.stderr)
            continue
        config["_stem"] = path.stem
        config["_token"] = _resolve_auth_token(config, path.name)
        configs.append(config)

    return sorted(configs, key=lambda c: c.get("display", 0))


# ---------- removal hold accessor ----------

def removal_hold(config):
    """Positive hold in seconds, or None = removals alert instantly (default)."""
    hold = config.get("removal_hold_seconds")
    return (hold if isinstance(hold, int) and not isinstance(hold, bool)
            and hold > 0 else None)


# ---------- provider key mapping ----------

def _provider_key(config):
    """Canonical provider key: the explicit "roster_key" field, else the
    config file's stem (nim.json -> nim — the load-time convention), else
    the slugified name (defensive: only hand-built dicts skip
    load_configs)."""
    return (config.get("roster_key")
            or config.get("_stem")
            or config["name"].lower().replace(" ", "_"))


def build_providers():
    """Build PROVIDERS dict mapping provider key -> config dict.
    A duplicate key (two files resolving to one key, e.g. an explicit
    "roster_key" matching another stem) is a config bug: warn, later wins."""
    providers = {}
    for cfg in load_configs():
        key = _provider_key(cfg)
        if key in providers:
            print(f"config: duplicate provider key {key!r} "
                  f"({providers[key].get('name')!r} and {cfg.get('name')!r}) "
                  f"- later display-order file wins; "
                  "rename one file or set a unique roster_key",
                  file=sys.stderr)
        providers[key] = cfg
    return providers


def build_gateway_wiring():
    """Build GATEWAY_WIRING dict mapping provider key -> wiring info."""
    wiring = {}
    for cfg in load_configs():
        key = _provider_key(cfg)
        wiring[key] = {
            "chat_completions_url": f"{cfg['base_url'].rstrip('/')}/chat/completions",
            "api_type": "openai_compatible",
        }
    return wiring


PROVIDERS = build_providers()
GATEWAY_WIRING = build_gateway_wiring()