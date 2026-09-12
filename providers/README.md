# Provider Configs

Each provider is a JSON file. The watchdog loads all `*.json` at startup.
To add a provider: drop in a new JSON file. To remove: delete the file.

## Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| name | string | yes | Human-readable name |
| base_url | string | yes | API base URL (no trailing /) |
| detection | string | yes | Detection method |
| auth | object | yes | Authentication config |
| auth.method | string | yes | `token_file`, `env_var`, or `none` |
| auth.env_key | string | if env_var | Environment variable name |
| display | int | yes | Column order (0 = first) |

## Detection Methods

- `api-pricing` — pricing.prompt == "0" AND pricing.completion == "0"
- `api-flag` — isFree == true
- `id-suffix` — id ends with ":free" or "-free"
- `all-free` — every model in catalog
- `zero-credit-probe` — fire 1-token completion per model (slow, deferred)