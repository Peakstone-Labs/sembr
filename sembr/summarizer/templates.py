# SPDX-License-Identifier: Apache-2.0
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

import logging
import os
import re
import time
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

# Identifier validation: no leading dot, no / \ .., length 1..100, Unicode ok.
_IDENT_RE = re.compile(r"^(?!\.)(?!.*\.\.)[^/\\]{1,100}$")

_SYSTEM_PLACEHOLDERS: frozenset[str] = frozenset({"language"})
_INSTRUCTION_PLACEHOLDERS: frozenset[str] = frozenset({"intent_text", "articles"})

# Prompts root constant. Replaces the older Settings.prompts_dir field.
PROMPTS_DIR: Final[Path] = Path("/app/prompts")

# Built-in (read-only) template names; reserved for both kinds.
BUILTIN_NAMES: frozenset[str] = frozenset({"default"})

# Per-write content size cap (bytes, UTF-8 encoded).
MAX_TEMPLATE_BYTES: Final[int] = 64 * 1024

# kind → allowed placeholder names, used by try_render's strict format_map.
_PLACEHOLDERS_BY_KIND: dict[str, frozenset[str]] = {
    "system": _SYSTEM_PLACEHOLDERS,
    "instruction": _INSTRUCTION_PLACEHOLDERS,
}


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


def template_path(prompts_dir: Path, kind: str, name: str) -> Path:
    """Resolve and validate the on-disk path for ``prompts_dir/kind/name.md``.

    Raises ``ValueError`` if *name* fails identifier validation or the resolved
    path escapes *prompts_dir*. Callers that need both the bytes and the path
    (e.g. for ``stat()``) should use this helper to keep validation in lockstep
    with file access — never rebuild the path manually.
    """
    _validate_name(name)
    candidate = (prompts_dir / kind / f"{name}.md").resolve()
    if not candidate.is_relative_to(prompts_dir.resolve()):
        raise ValueError(f"Template path {candidate} escapes prompts_dir {prompts_dir}")
    return candidate


def template_exists(prompts_dir: Path, kind: str, name: str) -> bool:
    """Return True if ``prompts_dir/kind/name.md`` exists and is a file."""
    try:
        return template_path(prompts_dir, kind, name).is_file()
    except ValueError:
        return False


def list_templates(prompts_dir: Path, kind: str) -> list[str]:
    """Return sorted list of template names (without ``.md``) for *kind*."""
    kind_dir = prompts_dir / kind
    if not kind_dir.is_dir():
        return []
    return sorted(
        p.stem for p in kind_dir.glob("*.md") if p.is_file() and not p.name.startswith(".")
    )


def load_template(prompts_dir: Path, kind: str, name: str) -> str:
    """Read and return the raw template content.

    Raises:
        ValueError: if *name* fails identifier validation or escapes prompts_dir.
        TemplateNotFoundError: if the file does not exist.
    """
    path = template_path(prompts_dir, kind, name)
    if not path.is_file():
        raise TemplateNotFoundError(f"Template '{kind}/{name}' not found at {path}")
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


def try_render(kind: str, content: str) -> None:
    """Dry-render *content* with empty-string placeholders to surface unknown
    placeholders at save-time. Raises ``TemplateRenderError`` on the first
    violation; returns ``None`` on success.

    The strict map carries one empty-string entry per allowed placeholder for
    *kind*; any other ``{...}`` key in the file triggers ``KeyError`` via
    ``_StrictMap.__missing__`` and is surfaced as ``TemplateRenderError``.
    """
    if kind not in _PLACEHOLDERS_BY_KIND:
        raise ValueError(f"unknown template kind {kind!r}")
    allowed = _PLACEHOLDERS_BY_KIND[kind]
    strict = _StrictMap({k: "" for k in allowed})
    try:
        content.format_map(strict)
    except KeyError as exc:
        raise TemplateRenderError(
            f"{kind} template contains undeclared placeholder {{{exc.args[0]}}}. "
            f"Available placeholders: {{{', '.join(sorted(allowed))}}}"
        ) from exc


def save_template_atomic(
    prompts_dir: Path,
    kind: str,
    name: str,
    content: str,
) -> Path:
    """Atomic write of ``prompts_dir/kind/name.md`` via tmp file + ``os.replace``.

    Validates *name* (raises ``ValueError`` on identifier failure) and resolves
    the path through ``template_path`` to keep escape-check in lockstep. The tmp
    filename starts with '.' so ``list_templates`` already filters it out
    (``sembr/summarizer/templates.py`` glob excludes hidden files).

    Returns the final on-disk path. Raises ``OSError`` on filesystem failure.
    """
    if kind not in _PLACEHOLDERS_BY_KIND:
        raise ValueError(f"unknown template kind {kind!r}")
    final_path = template_path(prompts_dir, kind, name)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{name}.md.tmp.{os.getpid()}.{time.monotonic_ns()}"
    tmp_path = final_path.parent / tmp_name
    data = content.encode("utf-8")
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)
    except BaseException:
        # Best-effort cleanup so a crash mid-write doesn't leak the tmp file
        # (list_templates already filters hidden names, but admins reading the
        # directory directly shouldn't trip over orphans).
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return final_path


def delete_template(prompts_dir: Path, kind: str, name: str) -> None:
    """Unlink ``prompts_dir/kind/name.md`` after identifier validation.

    Raises ``TemplateNotFoundError`` if the file is missing — explicit, not silent,
    so callers can translate to 404. Does not check builtin status — that's the
    API layer's responsibility.
    """
    if kind not in _PLACEHOLDERS_BY_KIND:
        raise ValueError(f"unknown template kind {kind!r}")
    path = template_path(prompts_dir, kind, name)
    if not path.is_file():
        raise TemplateNotFoundError(f"Template '{kind}/{name}' not found at {path}")
    os.unlink(path)


def rename_template(
    prompts_dir: Path,
    kind: str,
    old_name: str,
    new_name: str,
) -> Path:
    """Pure-filesystem rename helper.

    Validates both names + ``os.rename(old, new)`` and returns the new path. Does
    NOT pre-check existence (the caller does that to differentiate 404 from
    rename failure) and does NOT touch SQLite — the caller orchestrates the
    cross-boundary 2PC between filesystem rename and SQLite cascade-rename.

    Raises:
        ValueError: identifier validation failure on either name.
        TemplateNotFoundError: the old file does not exist.
        OSError: rare same-fs rename failure (caller logs + 500).
    """
    if kind not in _PLACEHOLDERS_BY_KIND:
        raise ValueError(f"unknown template kind {kind!r}")
    old_path = template_path(prompts_dir, kind, old_name)
    new_path = template_path(prompts_dir, kind, new_name)
    if not old_path.is_file():
        raise TemplateNotFoundError(f"Template '{kind}/{old_name}' not found at {old_path}")
    os.rename(old_path, new_path)
    return new_path
