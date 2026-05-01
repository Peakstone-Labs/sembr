"""Unit tests for sembr.summarizer.templates."""
from __future__ import annotations

import pytest
from pathlib import Path

from sembr.summarizer.templates import (
    TemplateNotFoundError,
    TemplateRenderError,
    list_templates,
    load_template,
    render_instruction,
    render_system,
    template_exists,
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
    (prompts_dir / "system" / "bad.md").write_text(
        "Hello {published_at}", encoding="utf-8"
    )
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
        prompts_dir, "default",
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
            prompts_dir, "bad",
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
        prompts_dir, "加密货币",
        intent_text="BTC",
        articles="news",
    )
    assert "BTC" in result
    assert "news" in result
