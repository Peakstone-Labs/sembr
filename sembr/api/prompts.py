"""Prompt template CRUD — list / get / create / update / delete / rename.

All write endpoints follow the design's fail-fast validation order (D14):
Pydantic body schema → builtin-name guard → source-template existence
(POST only) → target-template non/existence → strict try-render gate.

Rename is the only endpoint that touches SQLite (cascade UPDATE on the
intents table per D2/D15); POST/PUT/DELETE are filesystem-only.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from sembr.db.intents import list_template_refs, rename_intent_template
from sembr.db.sqlite import get_conn, transaction
from sembr.summarizer import templates as _templates
from sembr.summarizer.templates import (
    BUILTIN_NAMES,
    MAX_TEMPLATE_BYTES,
    TemplateNotFoundError,
    TemplateRenderError,
    delete_template,
    list_templates,
    load_template,
    rename_template,
    save_template_atomic,
    template_path,
    try_render,
)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])
logger = logging.getLogger(__name__)

_VALID_KINDS = frozenset({"system", "instruction"})

# Mirrors the on-disk identifier rule from `sembr/summarizer/templates.py::_IDENT_RE`
# and `sembr/models.py:129`. The duplication is intentional per existing project
# convention (one regex per enforcement layer; never centralised through
# cross-package imports). Compiled re module — Pydantic's `pattern=` uses Rust
# regex which doesn't support look-around, so we apply this through a
# field_validator instead.
_TEMPLATE_IDENT_RE = re.compile(r"^(?!\.)(?!.*\.\.)[^/\\]{1,100}$")


def _validate_ident(v: str, *, field: str) -> str:
    if not _TEMPLATE_IDENT_RE.match(v):
        raise ValueError(
            f"Invalid {field} {v!r}: must not start with '.', "
            "must not contain '/', '\\', or '..', length 1–100."
        )
    return v


# ---------------------------------------------------------------------------
# Response models (D6 / D20)
# ---------------------------------------------------------------------------


class IntentRef(BaseModel):
    id: int
    name: str


class TemplateInfo(BaseModel):
    name: str
    kind: Literal["system", "instruction"]
    is_builtin: bool
    ref_count: int
    ref_intents: list[IntentRef]
    size_bytes: int
    mtime: float


class TemplateList(BaseModel):
    system: list[TemplateInfo]
    instruction: list[TemplateInfo]


class TemplateDetail(BaseModel):
    name: str
    kind: Literal["system", "instruction"]
    content: str
    size_bytes: int
    mtime: float
    is_builtin: bool
    ref_intents: list[IntentRef]


# ---------------------------------------------------------------------------
# Request body models (D14)
# ---------------------------------------------------------------------------


class TemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _validate_ident(v, field="name")

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _validate_ident(v, field="source")


class TemplateUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_TEMPLATE_BYTES)


class TemplateRenameRequest(BaseModel):
    new_name: str = Field(min_length=1, max_length=100)

    @field_validator("new_name")
    @classmethod
    def _check_new_name(cls, v: str) -> str:
        return _validate_ident(v, field="new_name")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_kind(kind: str) -> None:
    if kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"kind must be one of: {sorted(_VALID_KINDS)}",
        )


def _build_info(
    prompts_dir: Path,
    kind: str,
    name: str,
    refs_index: dict[tuple[str, str], list[tuple[int, str]]],
) -> TemplateInfo:
    """Build a TemplateInfo row for an on-disk template name."""
    path = template_path(prompts_dir, kind, name)
    stat = path.stat()
    refs = refs_index.get((kind, name), [])
    return TemplateInfo(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        is_builtin=name in BUILTIN_NAMES,
        ref_count=len(refs),
        ref_intents=[IntentRef(id=i, name=n) for i, n in refs],
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
    )


async def _refs_index() -> dict[tuple[str, str], list[tuple[int, str]]]:
    return await list_template_refs(get_conn())


def _builtin_block(name: str) -> None:
    if name in BUILTIN_NAMES:
        # 422 when the *target* name is reserved (D9). The `default` name is the
        # one and only builtin in 1.0; reserved on both kinds per D17.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"field": "name", "reason": f"name '{name}' is reserved"},
        )


def _builtin_write_guard(name: str) -> None:
    if name in BUILTIN_NAMES:
        # 403 when the *resource being written/deleted/renamed* is a builtin.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"built-in template '{name}' is read-only",
        )


# ---------------------------------------------------------------------------
# GET /templates — rich listing per D4 / D6
# ---------------------------------------------------------------------------


@router.get("/templates", response_model=TemplateList)
async def list_all_templates(request: Request) -> TemplateList:
    """List all on-disk templates with `ref_intents`/`is_builtin` per D4/D6."""
    prompts_dir: Path = _templates.PROMPTS_DIR
    refs = await _refs_index()
    out: dict[str, list[TemplateInfo]] = {"system": [], "instruction": []}
    for kind in ("system", "instruction"):
        for name in list_templates(prompts_dir, kind):
            out[kind].append(_build_info(prompts_dir, kind, name, refs))
    return TemplateList(system=out["system"], instruction=out["instruction"])


# ---------------------------------------------------------------------------
# GET /templates/{kind}/{name}
# ---------------------------------------------------------------------------


@router.get("/templates/{kind}/{name}", response_model=TemplateDetail)
async def get_template(kind: str, name: str, request: Request) -> TemplateDetail:
    """Return the raw content + metadata of a single template."""
    _ensure_kind(kind)
    prompts_dir: Path = _templates.PROMPTS_DIR
    try:
        path = template_path(prompts_dir, kind, name)
        content = load_template(prompts_dir, kind, name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except TemplateNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"template '{kind}/{name}' not found",
        )

    stat = path.stat()
    refs = await _refs_index()
    return TemplateDetail(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        content=content,
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
        is_builtin=name in BUILTIN_NAMES,
        ref_intents=[IntentRef(id=i, name=n) for i, n in refs.get((kind, name), [])],
    )


# ---------------------------------------------------------------------------
# POST /templates/{kind} — create from default OR an existing source template
# ---------------------------------------------------------------------------


@router.post(
    "/templates/{kind}",
    response_model=TemplateInfo,
    status_code=status.HTTP_201_CREATED,
)
async def create_template(kind: str, body: TemplateCreateRequest, request: Request) -> TemplateInfo:
    _ensure_kind(kind)
    # D14 step 2 — builtin guard applies to *target* only (allows `source: default`).
    _builtin_block(body.name)

    prompts_dir: Path = _templates.PROMPTS_DIR
    source_name = body.source if body.source is not None else "default"

    # D14 step 3 — source-template existence check.
    try:
        source_content = load_template(prompts_dir, kind, source_name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"field": "source", "value": source_name, "reason": str(exc)},
        ) from exc
    except TemplateNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "field": "source",
                "value": source_name,
                "reason": "source template not found",
            },
        )

    # D14 step 4 — target non-existence check (creation, not overwrite).
    try:
        target_path = template_path(prompts_dir, kind, body.name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"field": "name", "value": body.name, "reason": str(exc)},
        ) from exc
    # R7/D21: TOCTOU pre-check, accepted under R4 single-admin model. POSIX rename(2)
    # silently overwrites; renameat2(RENAME_NOREPLACE) lacks a portable Python binding.
    if target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"field": "name", "value": body.name, "reason": "target already exists"},
        )

    # D14 step 5 — try_render with empty placeholders (defence-in-depth in case
    # the source contains unknown placeholders too — should not for builtins,
    # but a user duplicating a custom template gets the same gate as PUT).
    try:
        try_render(kind, source_content)
    except TemplateRenderError as exc:
        logger.info("create_template try_render rejected source: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"field": "source", "reason": str(exc)},
        ) from exc

    try:
        save_template_atomic(prompts_dir, kind, body.name, source_content)
    except OSError as exc:
        logger.error("create_template save failed: kind=%s name=%s: %s", kind, body.name, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="save failed"
        ) from exc

    refs = await _refs_index()
    return _build_info(prompts_dir, kind, body.name, refs)


# ---------------------------------------------------------------------------
# PUT /templates/{kind}/{name} — overwrite content
# ---------------------------------------------------------------------------


@router.put("/templates/{kind}/{name}", response_model=TemplateInfo)
async def update_template(
    kind: str, name: str, body: TemplateUpdateRequest, request: Request
) -> TemplateInfo:
    _ensure_kind(kind)
    _builtin_write_guard(name)  # 403: cannot edit builtin

    prompts_dir: Path = _templates.PROMPTS_DIR
    try:
        target_path = template_path(prompts_dir, kind, name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if not target_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"template '{kind}/{name}' not found",
        )

    try:
        try_render(kind, body.content)
    except TemplateRenderError as exc:
        logger.info("update_template try_render rejected: kind=%s name=%s: %s", kind, name, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"field": "content", "reason": str(exc)},
        ) from exc

    try:
        save_template_atomic(prompts_dir, kind, name, body.content)
    except OSError as exc:
        logger.error("update_template save failed: kind=%s name=%s: %s", kind, name, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="save failed"
        ) from exc

    refs = await _refs_index()
    return _build_info(prompts_dir, kind, name, refs)


# ---------------------------------------------------------------------------
# DELETE /templates/{kind}/{name} — guarded by ref_count == 0 (D19)
# ---------------------------------------------------------------------------


@router.delete("/templates/{kind}/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_template_endpoint(kind: str, name: str, request: Request):
    _ensure_kind(kind)
    _builtin_write_guard(name)  # 403: cannot delete builtin

    prompts_dir: Path = _templates.PROMPTS_DIR
    refs = await _refs_index()
    intents = refs.get((kind, name), [])
    if intents:
        logger.info(
            "delete_template_endpoint blocked by refs: kind=%s name=%s ref_count=%d",
            kind,
            name,
            len(intents),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "field": "name",
                "value": name,
                "ref_count": len(intents),
                "ref_intents": [{"id": i, "name": n} for i, n in intents],
                "reason": "template is referenced by intents",
            },
        )

    try:
        delete_template(prompts_dir, kind, name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except TemplateNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"template '{kind}/{name}' not found",
        )

    return None  # 204


# ---------------------------------------------------------------------------
# POST /templates/{kind}/{name}/rename — D2 three-step orchestration
# ---------------------------------------------------------------------------


@router.post("/templates/{kind}/{name}/rename", response_model=TemplateInfo)
async def rename_template_endpoint(
    kind: str, name: str, body: TemplateRenameRequest, request: Request
) -> TemplateInfo:
    _ensure_kind(kind)
    _builtin_write_guard(name)  # 403: cannot rename builtin source
    _builtin_block(body.new_name)  # 422: cannot rename to reserved name

    prompts_dir: Path = _templates.PROMPTS_DIR
    try:
        old_path = template_path(prompts_dir, kind, name)
        new_path = template_path(prompts_dir, kind, body.new_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # D2 step (a) — pre-existence check on target; rejects the common collision.
    if not old_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"template '{kind}/{name}' not found",
        )
    if old_path == new_path:
        # No-op rename — accept and return the existing row.
        refs = await _refs_index()
        return _build_info(prompts_dir, kind, name, refs)
    # R7/D21: TOCTOU pre-check, accepted under R4 single-admin model. POSIX rename(2)
    # silently overwrites; renameat2(RENAME_NOREPLACE) lacks a portable Python binding.
    if new_path.exists():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "field": "new_name",
                "value": body.new_name,
                "reason": "target name already exists",
            },
        )

    # D2 step (b) — filesystem rename outside any DB transaction.
    try:
        rename_template(prompts_dir, kind, name, body.new_name)
    except (ValueError, TemplateNotFoundError) as exc:
        # ValueError already covered by template_path above; keep a defensive arm.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except OSError as exc:
        logger.error(
            "rename_template os.rename failed: kind=%s old=%s new=%s: %s",
            kind,
            name,
            body.new_name,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="rename failed at filesystem layer",
        ) from exc

    # D2 step (c) — SQLite cascade UPDATE inside transaction.
    try:
        async with transaction() as txn:
            await rename_intent_template(txn, kind, name, body.new_name)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # Best-effort reverse-rename so file/DB stay in sync even if uvicorn cancels
        # this task (graceful shutdown / client disconnect). Do NOT swallow the
        # cancellation — it must propagate so the loop tears down cleanly.
        try:
            os.rename(new_path, old_path)
        except OSError as rev_exc:
            logger.error(
                "rename rollback during cancellation: kind=%s old=%s new=%s reverse_err=%r",
                kind,
                name,
                body.new_name,
                rev_exc,
            )
        raise
    except Exception as db_exc:
        # Reverse the filesystem rename so file & DB stay in sync.
        try:
            os.rename(new_path, old_path)
        except OSError as rev_exc:
            logger.error(
                "rename rollback failed: kind=%s old=%s new=%s db_err=%r reverse_err=%r",
                kind,
                name,
                body.new_name,
                db_exc,
                rev_exc,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "rename rollback failed — file and intents table now diverge; "
                    "manual recovery required (see logs)"
                ),
            ) from db_exc
        logger.error(
            "rename SQLite UPDATE failed, filesystem reversed: kind=%s old=%s new=%s err=%r",
            kind,
            name,
            body.new_name,
            db_exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="rename failed at database layer; filesystem state restored",
        ) from db_exc

    refs = await _refs_index()
    return _build_info(prompts_dir, kind, body.new_name, refs)
