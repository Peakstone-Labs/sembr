"""LLM backend factory."""
from __future__ import annotations

import logging

from sembr.config import Settings
from sembr.summarizer.llm.api import APIBackend
from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)


def build_llm_backend(settings: Settings) -> BaseLLMBackend:
    key = settings.llm_api_key.get_secret_value()
    if not key:
        logger.warning("llm_api_key is empty; all LLM summarization calls will fail with 401")
    return APIBackend(
        base_url=settings.llm_api_base_url,
        api_key=key,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_prompt_chars=settings.llm_max_prompt_chars,
    )
