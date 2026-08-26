"""Parse a .env file into a dict. Stdlib only; never logs values."""

def parse_envfile(path):
    """Return {KEY: value} from KEY=VALUE lines. Missing file -> {}.

    Skips blank lines, '#' comments, lines without '=', and empty keys.
    Accepts shell-style 'export KEY=v' lines: a leading 'export ' /
    'export\\t' on the KEY is stripped (the keyword is not part of the
    name — as a literal prefix it made provider keys and webhooks vanish).
    Strips ONE pair of surrounding double quotes from values.
    Splits on the FIRST '=' only (URLs survive).
    Opened with utf-8-sig so an editor-written leading BOM can't poison
    the first key ('\ufeffKEY' silently disabled lookups); plain UTF-8
    files are unaffected.
    """
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except FileNotFoundError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export ") or key.startswith("export\t"):
            key = key[len("export"):].strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        out[key] = value
    return out
