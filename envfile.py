"""Parse a .env file into a dict. Stdlib only; never logs values."""

def parse_envfile(path):
    """Return {KEY: value} from KEY=VALUE lines. Missing file -> {}.

    Skips blank lines, '#' comments, lines without '=', and empty keys.
    Strips ONE pair of surrounding double quotes from values.
    Splits on the FIRST '=' only (URLs survive).
    """
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        out[key] = value
    return out
