# Provider Configs

Each provider is a JSON file. The watchdog loads all `*.json` at startup.
To add a provider: drop in a new JSON file. To remove: delete the file.
A file the loader cannot use (invalid JSON, missing required fields, an
auth method it does not implement) is skipped with a
`config: skipping providers/<name>.json: <reason>` warning on stderr and
costs exactly that provider — the rest of the roster still loads.

## Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Human-readable name |
| base_url | string | yes | API base URL (no trailing /) |
| detection | string | yes | Detection method |
| auth | object | yes | Authentication config |
| auth.method | string | yes | `token_file`, `env_var`, or `none` |
| auth.env_key | string | if env_var | Environment variable name |
| auth.path_env | string | if token_file | Env var holding a path to a JSON token file (takes precedence over `path`) |
| auth.path | string | if token_file | Literal path to a JSON token file (legacy alternative to `path_env`) |
| auth.key | string | if token_file | Dot-separated JSON path to the token inside the file (e.g. `providers.nous.access_token`) |
| display | int | yes | Column order (0 = first) |
| ignored_slugs | list of strings | no | Exact model ids to exclude from tracking (see below) |

## Ignored slugs (`ignored_slugs`)

Optional per-provider list of **exact-match** model ids that are dropped
after detection so they never enter the roster — e.g. auto-router
endpoints like `kilo-auto/free` or `openrouter/free`, which pass the
free-id rule but are not real models. Add unwanted ids here, directly in
the provider's JSON; no code change is needed. Excluded ids drop
silently. A value that is not a list of non-empty strings breaks
validation: the whole config file is skipped with the standard
`config: skipping ...` stderr warning.

## Detection Methods

- `api-pricing` — pricing.prompt == "0" AND pricing.completion == "0"
- `api-flag` — isFree == true
- `id-suffix` — id ends with ":free" or "-free"
- `all-free` — every model in catalog (AMD, NVIDIA NIM)
- `zero-credit-probe` — fire a minimal 3-token completion per model (slow, deferred)