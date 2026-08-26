"""Tests for envfile.parse_envfile — tiny .env parser, stdlib only."""

from envfile import parse_envfile


def _write(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return p


def test_basic_pairs(tmp_path):
    d = parse_envfile(_write(tmp_path, "FOO=bar\nBAZ=qux\n"))
    assert d == {"FOO": "bar", "BAZ": "qux"}


def test_comments_blanks_and_crlf(tmp_path):
    d = parse_envfile(
        _write(tmp_path, "# header comment\r\n\r\nFOO=bar\r\n   \n#another\n")
    )
    assert d == {"FOO": "bar"}


def test_quoted_value_stripped_once(tmp_path):
    d = parse_envfile(_write(tmp_path, 'QUOTED="x y z"\n'))
    assert d == {"QUOTED": "x y z"}


def test_split_on_first_equals_only(tmp_path):
    d = parse_envfile(_write(tmp_path, "URL=http://x/?a=b=c\n"))
    assert d == {"URL": "http://x/?a=b=c"}


def test_missing_file_is_empty_dict(tmp_path):
    assert parse_envfile(tmp_path / "nope.env") == {}


def test_malformed_lines_skipped_without_crash(tmp_path):
    d = parse_envfile(_write(tmp_path, "NOEQUALS\n=emptykey\nOK=fine\n"))
    assert d == {"OK": "fine"}


# ---------- F4: shell-style 'export KEY=v' ----------

def test_export_prefix_stripped_from_key(tmp_path):
    """F4: 'export KEY=v' must yield the real key — the literal prefix made
    provider keys/webhooks vanish silently (lookup by 'OPENCODE_ZEN_API_KEY'
    missed a file whose line was written shell-style)."""
    d = parse_envfile(_write(tmp_path, "export OPENCODE_ZEN_API_KEY=x\n"))
    assert d == {"OPENCODE_ZEN_API_KEY": "x"}


def test_export_tab_separator_stripped_too(tmp_path):
    d = parse_envfile(_write(tmp_path, "export\tTABKEY=z\n"))
    assert d == {"TABKEY": "z"}


def test_export_quoted_value_still_unquoted(tmp_path):
    d = parse_envfile(_write(tmp_path, 'export KEY="a b"\n'))
    assert d == {"KEY": "a b"}


def test_bare_export_and_exporting_words_not_mangled(tmp_path):
    """Only 'export' followed by whitespace is a keyword: 'export=x' keeps
    the key 'export', and words merely starting with 'export' are untouched."""
    d = parse_envfile(
        _write(tmp_path, "export=x\nexporting=y\nexports_left=z\n")
    )
    assert d == {"export": "x", "exporting": "y", "exports_left": "z"}


# ---------- F5: UTF-8 BOM on the first line ----------

def test_utf8_bom_stripped_from_first_key(tmp_path):
    """F5: an editor-written BOM made the first key '\ufeffKEY', silently
    disabling webhook/key lookups. utf-8-sig eats it; the rest is untouched."""
    p = tmp_path / ".env"
    p.write_bytes(
        b"\xef\xbb\xbfDISCORD_WEBHOOK_INFERENCE_WATCHDOG=https://example/hook\n"
        b"PLAIN=v\n"
    )
    assert parse_envfile(p) == {
        "DISCORD_WEBHOOK_INFERENCE_WATCHDOG": "https://example/hook",
        "PLAIN": "v",
    }


def test_no_bom_file_unchanged(tmp_path):
    d = parse_envfile(_write(tmp_path, "FOO=bar\n"))
    assert d == {"FOO": "bar"}
