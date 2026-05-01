"""Prompt template discovery, loading, and rendering.

Templates live under ``prompts_dir/{kind}/{name}.md`` where:
- ``kind`` is ``"system"`` or ``"instruction"``
- ``name`` is a bare identifier (no ``.md`` extension, no path separators)

Templates are read from disk on every call (no caching) so edits take effect
on the next tick without a restart.

Rendering uses ``str.format_map`` with a strict whitelist of placeholders:
- system templates: ``{language}``
- instruction templates: ``{intent_text}``, ``{articles}``
Any other ``{...}`` key in the file raises ``TemplateRenderError``.
"""
from __future__ import annotations

import re
from pathlib import Path

# Identifier validation: no leading dot, no / \ .., length 1..100, Unicode ok.
_IDENT_RE = re.compile(r"^(?!\.)(?!.*\.\.)[^/\\]{1,100}$")

_SYSTEM_PLACEHOLDERS: frozenset[str] = frozenset({"language"})
_INSTRUCTION_PLACEHOLDERS: frozenset[str] = frozenset({"intent_text", "articles"})


class TemplateNotFoundError(FileNotFoundError):
    """Raised when the requested template file does not exist."""


class TemplateRenderError(ValueError):
    """Raised when ``str.format_map`` encounters an undeclared placeholder."""


def _validate_name(name: str) -> None:
    if not _IDENT_RE.match(name):
        raise ValueError(
            f"Invalid template name {name!r}: must not start with '.', "
            "must not contain '/', '\\', or '..', length 1–100."
        )


def _safe_path(prompts_dir: Path, kind: str, name: str) -> Path:
    _validate_name(name)
    candidate = (prompts_dir / kind / f"{name}.md").resolve()
    if not candidate.is_relative_to(prompts_dir.resolve()):
        raise ValueError(
            f"Template path {candidate} escapes prompts_dir {prompts_dir}"
        )
    return candidate


def template_exists(prompts_dir: Path, kind: str, name: str) -> bool:
    """Return True if ``prompts_dir/kind/name.md`` exists and is a file."""
    try:
        return _safe_path(prompts_dir, kind, name).is_file()
    except ValueError:
        return False


def list_templates(prompts_dir: Path, kind: str) -> list[str]:
    """Return sorted list of template names (without ``.md``) for *kind*."""
    kind_dir = prompts_dir / kind
    if not kind_dir.is_dir():
        return []
    return sorted(p.stem for p in kind_dir.glob("*.md") if p.is_file() and not p.name.startswith("."))


def load_template(prompts_dir: Path, kind: str, name: str) -> str:
    """Read and return the raw template content.

    Raises:
        ValueError: if *name* fails identifier validation or escapes prompts_dir.
        TemplateNotFoundError: if the file does not exist.
    """
    path = _safe_path(prompts_dir, kind, name)
    if not path.is_file():
        raise TemplateNotFoundError(
            f"Template '{kind}/{name}' not found at {path}"
        )
    return path.read_text(encoding="utf-8")


class _StrictMap(dict):  # type: ignore[type-arg]
    """dict subclass that raises KeyError for any missing key during format_map."""

    def __missing__(self, key: str) -> str:  # noqa: D401
        raise KeyError(key)


def render_system(prompts_dir: Path, name: str, *, language: str) -> str:
    """Load and render a system template, injecting ``{language}``.

    Raises:
        TemplateNotFoundError: file missing.
        TemplateRenderError: unknown placeholder found in file.
    """
    raw = load_template(prompts_dir, "system", name)
    try:
        return raw.format_map(_StrictMap(language=language))
    except KeyError as exc:
        raise TemplateRenderError(
            f"System template '{name}' contains undeclared placeholder {{{exc.args[0]}}}. "
            f"Available placeholders: {{language}}"
        ) from exc


def render_instruction(
    prompts_dir: Path,
    name: str,
    *,
    intent_text: str,
    articles: str,
) -> str:
    """Load and render an instruction template, injecting ``{intent_text}`` and ``{articles}``.

    Raises:
        TemplateNotFoundError: file missing.
        TemplateRenderError: unknown placeholder found in file.
    """
    raw = load_template(prompts_dir, "instruction", name)
    try:
        return raw.format_map(_StrictMap(intent_text=intent_text, articles=articles))
    except KeyError as exc:
        raise TemplateRenderError(
            f"Instruction template '{name}' contains undeclared placeholder {{{exc.args[0]}}}. "
            f"Available placeholders: {{intent_text}}, {{articles}}"
        ) from exc
