# SPDX-License-Identifier: Apache-2.0
"""POST /intents/translate — stateless one-shot translation via the summarizer LLM.

The endpoint is "stateless" in the sense that it does not bind to an intent_id —
the create/edit form can call it before the intent is persisted. Errors:
- 422: validated by Pydantic (length / charset) on TranslateRequest.
- 502: any LLMError from the backend; body is scrubbed via APIBackend's
       safe_body truncation (no full prompt or API key leakage).

The translation prompt is a hardcoded module constant (not pulled from
prompts/templates). Temperature and max_tokens are not passed; the backend's
defaults govern.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from sembr.models import TranslateRequest, TranslateResponse
from sembr.summarizer.llm.base import LLMError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/intents", tags=["intents"])

# Translation prompt — hardcoded module constant. {target} is substituted from
# TranslateRequest.target_language; the validator on that field guarantees the
# value matches `[A-Za-z][A-Za-z0-9_\\- ]*` so there is no SQL / format-string
# injection risk.
_TRANSLATE_SYSTEM_PROMPT = (
    "You are a translator. Translate the user's text into {target}, preserving the original "
    "meaning and tone. Output only the translation, no quotes, no preamble, no commentary."
)


@router.post("/translate", response_model=TranslateResponse)
async def translate(body: TranslateRequest, request: Request) -> TranslateResponse:
    llm = getattr(request.app.state, "llm_backend", None)
    if llm is None:
        # lifespan didn't finish wiring app.state.llm_backend yet — 503 is the
        # documented pattern for "service not ready" (matches embedder readiness).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="translator backend not ready",
        )

    try:
        translated = await llm.summarize(
            prompt=body.source_text,
            system=_TRANSLATE_SYSTEM_PROMPT.format(target=body.target_language),
        )
    except LLMError as exc:
        # Scrubbed message — APIBackend already truncates / strips upstream body
        # so we re-surface its text as the 502 detail.
        logger.warning("translate: LLM failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"translation failed: {exc}",
        ) from exc

    return TranslateResponse(text=translated)
