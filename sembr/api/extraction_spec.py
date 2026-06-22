# SPDX-License-Identifier: Apache-2.0
"""Extraction-spec lifecycle endpoints (spec-autogen): generate / read / save / enable.

Paths are written in full as ``/api/intents/{intent_id}/extraction-spec*`` (the
router has no prefix) so they sit under the ``/api/intents/`` auth-protected
prefix and an unauthenticated call gets a 401 JSON, not a 302 redirect — same
pattern the map sub-feature uses in ``api/history.py`` (design D6). No auth
changes needed.

Generate is synchronous with a 60s cap (design D1); save and enable are
deliberately separate (save writes a draft, enable points the intent at it).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from sembr.db.intents import get_intent, update_intent
from sembr.db.sqlite import get_conn
from sembr.db.summary_history import list_summaries
from sembr.models import IntentUpdate
from sembr.summarizer.llm.base import LLMError
from sembr.summarizer.spec import SpecError, SpecNotFoundError, load_spec
from sembr.summarizer.spec_gen import (
    SpecBaseMissingError,
    derive_spec_name,
    generate_spec,
    has_errors,
    load_base,
    read_spec_raw,
    save_spec_atomic,
    truncate_digest,
    validate_spec_payload,
)
from sembr.summarizer.templates import PROMPTS_DIR, TemplateNotFoundError, load_template

logger = logging.getLogger(__name__)

router = APIRouter(tags=["intents"])

_GENERATE_TIMEOUT_S = 300.0


class GenerateSpecRequest(BaseModel):
    use_digest: bool = False


class EnableSpecRequest(BaseModel):
    enabled: bool = True


class SaveSpecRequest(BaseModel):
    md: str
    # accept the wire key "json"; expose as json_text in code (avoid BaseModel.json clash).
    # Annotated form so the alias actually binds (plain `= Field(alias=)` warns).
    json_text: Annotated[str, Field(alias="json")]
    model_config = {"populate_by_name": True}


async def _require_intent(intent_id: int):
    intent = await get_intent(get_conn(), intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")
    return intent


async def _recent_digest(intent_id: int) -> str | None:
    """Most recent digest summary (by run_at DESC), truncated; None if none."""
    rows = await list_summaries(get_conn(), intent_id, limit=1)
    if not rows or not (rows[0].get("summary") or "").strip():
        return None
    return truncate_digest(rows[0]["summary"])


async def _has_recent_digest(intent_id: int) -> bool:
    """Cheap availability check for the digest toggle — avoids truncating a
    summary we only want a bool for (the GET path)."""
    rows = await list_summaries(get_conn(), intent_id, limit=1)
    return bool(rows and (rows[0].get("summary") or "").strip())


# --------------------------------------------------------------------------- #
# GET — current spec for the Advanced panel (own / fallback / none)
# --------------------------------------------------------------------------- #
@router.get("/api/intents/{intent_id}/extraction-spec")
async def get_extraction_spec(intent_id: int) -> dict[str, Any]:
    intent = await _require_intent(intent_id)
    own_name = derive_spec_name(intent_id)

    md, json_text, source, exists = "", "", "none", False
    own = read_spec_raw(own_name, PROMPTS_DIR)
    if own is not None:
        md, json_text = own
        source, exists = "own", True
    else:
        # Fall back to the system_template's spec as a read-only starting point —
        # saving still writes own_name, never the shared template (design D3).
        fallback_name = intent.system_template
        fb = read_spec_raw(fallback_name, PROMPTS_DIR) if fallback_name else None
        if fb is not None:
            md, json_text = fb
            source = "fallback"

    digest_available = await _has_recent_digest(intent_id)
    return {
        "md": md,
        "json": json_text,
        "exists": exists,
        "enabled": intent.extraction_enabled,
        "source": source,
        "digest_available": digest_available,
    }


# --------------------------------------------------------------------------- #
# POST generate — meta-LLM draft (synchronous, 60s cap). Does NOT write to disk.
# --------------------------------------------------------------------------- #
@router.post("/api/intents/{intent_id}/extraction-spec/generate")
async def post_generate_spec(
    intent_id: int, body: GenerateSpecRequest, request: Request
) -> dict[str, Any]:
    intent = await _require_intent(intent_id)
    try:
        system_tpl = load_template(PROMPTS_DIR, "system", intent.system_template)
        instruction_tpl = load_template(PROMPTS_DIR, "instruction", intent.instruction_template)
    except (TemplateNotFoundError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "template_missing", "message": str(exc)},
        ) from exc
    try:
        base = load_base(PROMPTS_DIR)
    except SpecBaseMissingError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
        ) from exc

    digest = await _recent_digest(intent_id) if body.use_digest else None
    backend = request.app.state.llm_backend
    model = request.app.state.settings.effective_meta_extraction_model

    try:
        md, json_obj = await asyncio.wait_for(
            generate_spec(
                system_tpl=system_tpl,
                instruction_tpl=instruction_tpl,
                base=base,
                digest=digest,
                backend=backend,
                model=model,
            ),
            timeout=_GENERATE_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Generation timed out (>300s); please retry.",
        ) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Model generation failed; please retry: {exc}",
        ) from exc

    json_text = json.dumps(json_obj, ensure_ascii=False, indent=2)
    # Soft validation so the panel can flag what to fix; never blocks generation.
    warnings = [i.model_dump() for i in validate_spec_payload(md, json_text)]
    return {"md": md, "json": json_text, "warnings": warnings}


# --------------------------------------------------------------------------- #
# POST save — validate + atomic write draft (does NOT enable)
# --------------------------------------------------------------------------- #
@router.post("/api/intents/{intent_id}/extraction-spec/save")
async def post_save_spec(intent_id: int, body: SaveSpecRequest) -> dict[str, Any]:
    await _require_intent(intent_id)
    issues = validate_spec_payload(body.md, body.json_text)
    if has_errors(issues):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "spec_invalid",
                "errors": [i.model_dump() for i in issues if i.severity == "error"],
            },
        )
    name = derive_spec_name(intent_id)
    try:
        save_spec_atomic(name, body.md, body.json_text, PROMPTS_DIR)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"write failed: {exc}"
        ) from exc
    return {
        "ok": True,
        "name": name,
        "warnings": [i.model_dump() for i in issues if i.severity == "warning"],
    }


# --------------------------------------------------------------------------- #
# POST enable — point the intent at its own spec (requires it to exist + load)
# --------------------------------------------------------------------------- #
@router.post("/api/intents/{intent_id}/extraction-spec/enable")
async def post_enable_spec(intent_id: int, body: EnableSpecRequest | None = None) -> dict[str, Any]:
    """Toggle structured extraction for this intent (the extraction_enabled bool).
    enabled=True requires the intent's spec (intent-{id}) to exist + load; the spec
    file and its name are never touched — only the bool. enabled=False just turns
    it off. Body optional; absent → enabled=True (backward compatible)."""
    await _require_intent(intent_id)
    name = derive_spec_name(intent_id)
    enabled = body.enabled if body is not None else True

    if not enabled:
        await update_intent(get_conn(), intent_id, IntentUpdate(extraction_enabled=False))
        return {"ok": True, "enabled": False}

    try:
        load_spec(name, PROMPTS_DIR)  # re-load guards against missing/broken (design D5)
    except SpecNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "spec_not_found", "message": "Save the spec before enabling."},
        ) from exc
    except (SpecError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "spec_invalid", "message": f"spec failed to load: {exc}"},
        ) from exc

    await update_intent(get_conn(), intent_id, IntentUpdate(extraction_enabled=True))
    return {"ok": True, "enabled": True}
