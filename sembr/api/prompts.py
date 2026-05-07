"""GET /api/prompts/templates — list and read prompt templates."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from sembr.summarizer.templates import (
    TemplateNotFoundError,
    list_templates,
    load_template,
    template_path,
)

router = APIRouter(prefix="/api/prompts", tags=["prompts"])

_VALID_KINDS = frozenset({"system", "instruction"})


class TemplateList(BaseModel):
    system: list[str]
    instruction: list[str]


class TemplateDetail(BaseModel):
    name: str
    kind: str
    content: str
    size_bytes: int
    mtime: float


@router.get("/templates", response_model=TemplateList)
async def list_all_templates(request: Request) -> TemplateList:
    """List all available template names for each kind."""
    prompts_dir: Path = request.app.state.settings.prompts_dir
    return TemplateList(
        system=list_templates(prompts_dir, "system"),
        instruction=list_templates(prompts_dir, "instruction"),
    )


@router.get("/templates/{kind}/{name}", response_model=TemplateDetail)
async def get_template(kind: str, name: str, request: Request) -> TemplateDetail:
    """Return the raw content and metadata of a single template."""
    if kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"kind must be one of: {sorted(_VALID_KINDS)}",
        )
    prompts_dir: Path = request.app.state.settings.prompts_dir
    try:
        path = template_path(prompts_dir, kind, name)
        content = load_template(prompts_dir, kind, name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except TemplateNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"template '{kind}/{name}' not found",
        )

    stat = path.stat()
    return TemplateDetail(
        name=name,
        kind=kind,
        content=content,
        size_bytes=stat.st_size,
        mtime=stat.st_mtime,
    )
