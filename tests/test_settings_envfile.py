"""Tests for sembr.api.settings_envfile."""
from __future__ import annotations

from pathlib import Path

import pytest

from sembr.api.settings_envfile import (
    USER_ADDITIONS_HEADER,
    EnvFile,
    KEY_PATTERN,
    _quote_for_write,
    _strip_inline_comment_and_quotes,
)


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    return tmp_path / ".env"


SAMPLE = """\
# ── API server ────────────────────────────────────────────────────────────────
API_HOST=0.0.0.0
API_PORT=8000

# ── Embedder ──────────────────────────────────────────────────────────────────
EMBEDDER_API_KEY=sk-original
EMBEDDER_MODEL=BAAI/bge-m3
SMTP_PASSWORD="p@ss with spaces"
NOTE=plain # trailing comment
EMPTY=
"""


def test_parse_preserves_raw_lines(env_path: Path) -> None:
    env_path.write_text(SAMPLE, encoding="utf-8")
    ef = EnvFile.load(env_path)

    keys = ef.keys()
    assert keys == [
        "API_HOST", "API_PORT",
        "EMBEDDER_API_KEY", "EMBEDDER_MODEL",
        "SMTP_PASSWORD", "NOTE", "EMPTY",
    ]
    values = ef.values()
    assert values["API_HOST"] == "0.0.0.0"
    assert values["EMBEDDER_API_KEY"] == "sk-original"
    assert values["SMTP_PASSWORD"] == "p@ss with spaces"
    assert values["NOTE"] == "plain"
    assert values["EMPTY"] == ""


def test_upsert_in_place_keeps_other_lines_byte_identical(env_path: Path) -> None:
    env_path.write_text(SAMPLE, encoding="utf-8")
    ef = EnvFile.load(env_path)
    ef.upsert("EMBEDDER_API_KEY", "sk-newvalue")
    ef.save()

    text = env_path.read_text(encoding="utf-8")
    # Group headers preserved
    assert "# ── API server" in text
    assert "# ── Embedder" in text
    # Untouched lines verbatim
    assert "API_HOST=0.0.0.0" in text
    assert "EMBEDDER_MODEL=BAAI/bge-m3" in text
    assert 'SMTP_PASSWORD="p@ss with spaces"' in text
    # Updated line
    assert "EMBEDDER_API_KEY=sk-newvalue" in text
    assert "sk-original" not in text


def test_upsert_new_key_appends_to_user_additions(env_path: Path) -> None:
    env_path.write_text(SAMPLE, encoding="utf-8")
    ef = EnvFile.load(env_path)
    ef.upsert("TWITTER_COOKIE", "auth_token=abc; ct0=def")
    ef.save()

    text = env_path.read_text(encoding="utf-8")
    assert USER_ADDITIONS_HEADER in text
    # Quoting needed because of `=`/`;`/spaces
    assert 'TWITTER_COOKIE="auth_token=abc; ct0=def"' in text
    # Header appears exactly once
    assert text.count(USER_ADDITIONS_HEADER) == 1


def test_repeated_additions_share_one_header(env_path: Path) -> None:
    env_path.write_text(SAMPLE, encoding="utf-8")
    ef = EnvFile.load(env_path)
    ef.upsert("TWITTER_COOKIE", "v1")
    ef.save()
    ef2 = EnvFile.load(env_path)
    ef2.upsert("GITHUB_ACCESS_TOKEN", "ghp_xxx")
    ef2.save()

    text = env_path.read_text(encoding="utf-8")
    assert text.count(USER_ADDITIONS_HEADER) == 1
    assert "TWITTER_COOKIE=v1" in text
    assert "GITHUB_ACCESS_TOKEN=ghp_xxx" in text


def test_delete_removes_line(env_path: Path) -> None:
    env_path.write_text(SAMPLE, encoding="utf-8")
    ef = EnvFile.load(env_path)
    assert ef.delete("NOTE") is True
    assert ef.delete("NOTE") is False  # idempotent
    ef.save()

    text = env_path.read_text(encoding="utf-8")
    assert "NOTE=" not in text


def test_save_creates_bak_with_prior_content(env_path: Path) -> None:
    env_path.write_text(SAMPLE, encoding="utf-8")
    ef = EnvFile.load(env_path)
    ef.upsert("API_HOST", "127.0.0.1")
    ef.save()

    bak = env_path.with_name(".env.bak")
    assert bak.exists()
    assert bak.read_text(encoding="utf-8") == SAMPLE


def test_save_overwrites_prior_bak(env_path: Path) -> None:
    env_path.write_text(SAMPLE, encoding="utf-8")
    ef = EnvFile.load(env_path)
    ef.upsert("API_HOST", "10.0.0.1")
    ef.save()
    bak = env_path.with_name(".env.bak")
    first_bak = bak.read_text(encoding="utf-8")

    ef2 = EnvFile.load(env_path)
    ef2.upsert("API_HOST", "10.0.0.2")
    ef2.save()
    second_bak = bak.read_text(encoding="utf-8")

    assert first_bak != second_bak
    assert "API_HOST=10.0.0.1" in second_bak  # bak now contains prev save


def test_direct_write_no_tmp_rename(env_path: Path) -> None:
    # save() was changed to write directly to target (no tmp+rename) to fix
    # EBUSY on Docker Desktop macOS VirtioFS bind-mounts (commit 157aff3).
    env_path.write_text(SAMPLE, encoding="utf-8")
    original = env_path.read_text(encoding="utf-8")
    ef = EnvFile.load(env_path)
    ef.upsert("API_HOST", "1.2.3.4")
    ef.save()

    result = env_path.read_text(encoding="utf-8")
    assert "API_HOST=1.2.3.4" in result

    # No .tmp sibling should be left behind
    tmp_candidates = list(env_path.parent.glob("*.tmp"))
    assert tmp_candidates == [], f"unexpected .tmp files: {tmp_candidates}"

    # Backup must contain the original content
    bak = env_path.with_name(env_path.name + ".bak")
    assert bak.exists(), ".env.bak must be created before overwriting"
    assert bak.read_text(encoding="utf-8") == original


def test_load_directory_raises(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.mkdir()
    with pytest.raises(IsADirectoryError):
        EnvFile.load(env_path)


def test_load_missing_file_returns_empty(env_path: Path) -> None:
    ef = EnvFile.load(env_path)
    assert ef.values() == {}
    ef.upsert("NEW_KEY", "v")
    ef.save()
    assert env_path.exists()
    text = env_path.read_text(encoding="utf-8")
    assert "NEW_KEY=v" in text
    assert USER_ADDITIONS_HEADER in text


def test_strip_inline_comment_and_quotes_unquoted_with_comment() -> None:
    assert _strip_inline_comment_and_quotes("plain # trailing comment") == "plain"


def test_strip_inline_comment_double_quoted_keeps_hash() -> None:
    assert _strip_inline_comment_and_quotes('"value # not a comment"') == "value # not a comment"


def test_strip_inline_comment_single_quoted_keeps_literal() -> None:
    assert _strip_inline_comment_and_quotes("'raw\\nvalue'") == "raw\\nvalue"


def test_strip_inline_comment_double_quote_processes_escape() -> None:
    assert _strip_inline_comment_and_quotes('"line1\\nline2"') == "line1\nline2"


def test_quote_for_write_plain_value() -> None:
    assert _quote_for_write("simple") == "simple"


def test_quote_for_write_with_spaces() -> None:
    assert _quote_for_write("a b") == '"a b"'


def test_quote_for_write_with_hash() -> None:
    assert _quote_for_write("v#x") == '"v#x"'


def test_quote_for_write_empty_string() -> None:
    assert _quote_for_write("") == ""


def test_quote_for_write_escapes_backslash_and_quote() -> None:
    assert _quote_for_write('a"b\\c') == '"a\\"b\\\\c"'


def test_key_pattern_accepts_all_caps() -> None:
    assert KEY_PATTERN.match("TWITTER_COOKIE")
    assert KEY_PATTERN.match("GITHUB_ACCESS_TOKEN_2")


def test_key_pattern_rejects_lowercase_and_unicode() -> None:
    assert not KEY_PATTERN.match("twitter_cookie")
    assert not KEY_PATTERN.match("ＴＷＩＴＴＥＲ")  # fullwidth unicode
    assert not KEY_PATTERN.match("../../etc/passwd")
    assert not KEY_PATTERN.match("KEY-WITH-DASH")
    assert not KEY_PATTERN.match("9DIGIT_FIRST")


def test_round_trip_byte_identical_when_no_changes(env_path: Path) -> None:
    """Loading and saving without mutations must reproduce identical content
    (modulo a trailing newline normalization)."""
    env_path.write_text(SAMPLE, encoding="utf-8")
    ef = EnvFile.load(env_path)
    ef.save()
    text = env_path.read_text(encoding="utf-8")
    # All original non-trailing content preserved
    for line in SAMPLE.splitlines():
        assert line in text


def test_unparseable_lines_preserved(env_path: Path) -> None:
    weird = "valid=ok\nthis is not kv\n=missing_key\n"
    env_path.write_text(weird, encoding="utf-8")
    ef = EnvFile.load(env_path)
    ef.save()
    text = env_path.read_text(encoding="utf-8")
    assert "this is not kv" in text
    assert "=missing_key" in text
