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
