# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sembr.summarizer.templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from sembr.summarizer.templates import (
    BUILTIN_NAMES,
    MAX_TEMPLATE_BYTES,
    PROMPTS_DIR,
    TemplateNotFoundError,
    TemplateRenderError,
    delete_template,
    list_templates,
    load_template,
    rename_template,
    render_instruction,
    render_system,
    save_template_atomic,
    template_exists,
    try_render,
)


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text(
        "You are an assistant. Language: {language}", encoding="utf-8"
    )
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n\n{articles}", encoding="utf-8"
    )
    return tmp_path


# --- template_exists ---


def test_template_exists_true(prompts_dir: Path) -> None:
    assert template_exists(prompts_dir, "system", "default") is True


def test_template_exists_false(prompts_dir: Path) -> None:
    assert template_exists(prompts_dir, "system", "ghost") is False


def test_template_exists_invalid_name_returns_false(prompts_dir: Path) -> None:
    assert template_exists(prompts_dir, "system", "../../etc/passwd") is False
    assert template_exists(prompts_dir, "system", "/etc/passwd") is False
    assert template_exists(prompts_dir, "system", "a/b") is False


def test_validate_name_rejects_embedded_double_dot(prompts_dir: Path) -> None:
    assert template_exists(prompts_dir, "system", "a..b") is False
    assert template_exists(prompts_dir, "system", "foo..") is False
    with pytest.raises(ValueError):
        from sembr.summarizer.templates import _validate_name

        _validate_name("a..b")


# --- list_templates ---


def test_list_templates_returns_sorted(prompts_dir: Path) -> None:
    (prompts_dir / "system" / "beta.md").write_text("x", encoding="utf-8")
    (prompts_dir / "system" / "alpha.md").write_text("x", encoding="utf-8")
    result = list_templates(prompts_dir, "system")
    assert result == ["alpha", "beta", "default"]


def test_list_templates_missing_kind_dir(tmp_path: Path) -> None:
    result = list_templates(tmp_path, "system")
    assert result == []


def test_list_templates_excludes_hidden_files(prompts_dir: Path) -> None:
    (prompts_dir / "system" / ".hidden.md").write_text("x", encoding="utf-8")
    result = list_templates(prompts_dir, "system")
    assert ".hidden" not in result
    assert "default" in result


# --- load_template ---


def test_load_template_reads_content(prompts_dir: Path) -> None:
    content = load_template(prompts_dir, "system", "default")
    assert "Language" in content


def test_load_template_missing_raises(prompts_dir: Path) -> None:
    with pytest.raises(TemplateNotFoundError):
        load_template(prompts_dir, "system", "ghost")


def test_load_template_path_traversal_raises(prompts_dir: Path) -> None:
    with pytest.raises(ValueError):
        load_template(prompts_dir, "system", "../etc/passwd")


def test_load_template_absolute_path_raises(prompts_dir: Path) -> None:
    with pytest.raises(ValueError):
        load_template(prompts_dir, "system", "/etc/passwd")


def test_load_template_subdir_raises(prompts_dir: Path) -> None:
    with pytest.raises(ValueError):
        load_template(prompts_dir, "system", "a/b")


def test_load_template_leading_dot_raises(prompts_dir: Path) -> None:
    with pytest.raises(ValueError):
        load_template(prompts_dir, "system", ".hidden")


# --- render_system ---


def test_render_system_injects_language(prompts_dir: Path) -> None:
    result = render_system(prompts_dir, "default", language="English")
    assert "Language: English" in result


def test_render_system_unknown_placeholder(prompts_dir: Path) -> None:
    (prompts_dir / "system" / "bad.md").write_text("Hello {published_at}", encoding="utf-8")
    with pytest.raises(TemplateRenderError) as exc_info:
        render_system(prompts_dir, "bad", language="English")
    assert "published_at" in str(exc_info.value)
    assert "Available placeholders" in str(exc_info.value)


def test_render_system_missing_raises(prompts_dir: Path) -> None:
    with pytest.raises(TemplateNotFoundError):
        render_system(prompts_dir, "ghost", language="English")


# --- render_instruction ---


def test_render_instruction_injects_both(prompts_dir: Path) -> None:
    result = render_instruction(
        prompts_dir,
        "default",
        intent_text="AI news",
        articles="[1] Article one\n[2] Article two",
    )
    assert "AI news" in result
    assert "Article one" in result


def test_render_instruction_unknown_placeholder(prompts_dir: Path) -> None:
    (prompts_dir / "instruction" / "bad.md").write_text(
        "Topic: {intent_text}\n{published_at}", encoding="utf-8"
    )
    with pytest.raises(TemplateRenderError) as exc_info:
        render_instruction(
            prompts_dir,
            "bad",
            intent_text="AI news",
            articles="...",
        )
    assert "published_at" in str(exc_info.value)
    assert "Available placeholders" in str(exc_info.value)


def test_render_instruction_missing_raises(prompts_dir: Path) -> None:
    with pytest.raises(TemplateNotFoundError):
        render_instruction(prompts_dir, "ghost", intent_text="x", articles="x")


# --- Unicode filename ---


def test_unicode_filename_loads(prompts_dir: Path) -> None:
    (prompts_dir / "instruction" / "加密货币.md").write_text(
        "{intent_text} — {articles}", encoding="utf-8"
    )
    result = render_instruction(
        prompts_dir,
        "加密货币",
        intent_text="BTC",
        articles="news",
    )
    assert "BTC" in result
    assert "news" in result


# --- Module-level constants --------------------------------------------------


def test_prompts_dir_constant() -> None:
    """PROMPTS_DIR is the canonical bind-mount path."""
    assert Path("/app/prompts") == PROMPTS_DIR


def test_builtin_names_includes_default() -> None:
    """'default' is reserved for both kinds via a single frozenset."""
    assert "default" in BUILTIN_NAMES
    assert isinstance(BUILTIN_NAMES, frozenset)


def test_max_template_bytes_is_64kib() -> None:
    """Per-write content size cap is 64 KiB."""
    assert MAX_TEMPLATE_BYTES == 64 * 1024


# --- try_render --------------------------------------------------------------


def test_try_render_accepts_valid_system_placeholder() -> None:
    try_render("system", "Lang: {language}\nNo other vars.")  # no raise = pass


def test_try_render_accepts_valid_instruction_placeholders() -> None:
    try_render("instruction", "T: {intent_text}\n{articles}")


def test_try_render_accepts_no_placeholders() -> None:
    """Pathologically valid content with no placeholders is accepted."""
    try_render("instruction", "Just plain text — no curly braces.")


def test_try_render_rejects_unknown_system_placeholder() -> None:
    with pytest.raises(TemplateRenderError) as exc_info:
        try_render("system", "Lang: {language}\nBad: {published_at}")
    assert "published_at" in str(exc_info.value)


def test_try_render_rejects_unknown_instruction_placeholder() -> None:
    with pytest.raises(TemplateRenderError) as exc_info:
        try_render("instruction", "T: {intent_text}\n{articles}\n{unknown}")
    assert "unknown" in str(exc_info.value)


def test_try_render_rejects_invalid_kind() -> None:
    with pytest.raises(ValueError):
        try_render("notakind", "anything")


# --- save_template_atomic ----------------------------------------------------


def test_save_template_atomic_writes_content(prompts_dir: Path) -> None:
    out = save_template_atomic(
        prompts_dir, "instruction", "crypto_zh", "Topic: {intent_text}\n{articles}"
    )
    assert out == prompts_dir / "instruction" / "crypto_zh.md"
    assert out.read_text(encoding="utf-8") == "Topic: {intent_text}\n{articles}"


def test_save_template_atomic_overwrites(prompts_dir: Path) -> None:
    save_template_atomic(prompts_dir, "system", "default", "First version {language}")
    save_template_atomic(prompts_dir, "system", "default", "Second version {language}")
    assert (prompts_dir / "system" / "default.md").read_text(
        encoding="utf-8"
    ) == "Second version {language}"


def test_save_template_atomic_no_orphan_tmp_files(prompts_dir: Path) -> None:
    save_template_atomic(prompts_dir, "instruction", "crypto_en", "{intent_text}\n{articles}")
    # Hidden tmp files filter out via list_templates, but they must also not linger on disk.
    leftovers = [p.name for p in (prompts_dir / "instruction").iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_save_template_atomic_rejects_invalid_name(prompts_dir: Path) -> None:
    with pytest.raises(ValueError):
        save_template_atomic(prompts_dir, "system", "../etc/passwd", "x")


def test_save_template_atomic_rejects_invalid_kind(prompts_dir: Path) -> None:
    with pytest.raises(ValueError):
        save_template_atomic(prompts_dir, "bad_kind", "ok", "x")


def test_save_template_atomic_creates_kind_dir(tmp_path: Path) -> None:
    """Tolerates a fresh prompts root that doesn't yet have the kind subdir."""
    out = save_template_atomic(tmp_path, "system", "default", "Lang: {language}")
    assert out.exists()


# --- delete_template ---------------------------------------------------------


def test_delete_template_removes_file(prompts_dir: Path) -> None:
    save_template_atomic(prompts_dir, "instruction", "scratch", "{intent_text}\n{articles}")
    delete_template(prompts_dir, "instruction", "scratch")
    assert not (prompts_dir / "instruction" / "scratch.md").exists()


def test_delete_template_missing_raises(prompts_dir: Path) -> None:
    with pytest.raises(TemplateNotFoundError):
        delete_template(prompts_dir, "instruction", "ghost")


def test_delete_template_rejects_invalid_name(prompts_dir: Path) -> None:
    with pytest.raises(ValueError):
        delete_template(prompts_dir, "system", "../etc/passwd")


# --- rename_template (filesystem rename step only) ---------------------------


def test_rename_template_moves_file(prompts_dir: Path) -> None:
    save_template_atomic(prompts_dir, "instruction", "old_name", "{intent_text}\n{articles}")
    new_path = rename_template(prompts_dir, "instruction", "old_name", "new_name")
    assert new_path == prompts_dir / "instruction" / "new_name.md"
    assert new_path.exists()
    assert not (prompts_dir / "instruction" / "old_name.md").exists()


def test_rename_template_missing_old_raises(prompts_dir: Path) -> None:
    with pytest.raises(TemplateNotFoundError):
        rename_template(prompts_dir, "instruction", "ghost", "anything")


def test_rename_template_rejects_invalid_new_name(prompts_dir: Path) -> None:
    save_template_atomic(prompts_dir, "instruction", "src", "{intent_text}\n{articles}")
    with pytest.raises(ValueError):
        rename_template(prompts_dir, "instruction", "src", "../etc/passwd")
