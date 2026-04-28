"""LLM backend factory."""
from __future__ import annotations

from sembr.config import Settings
from sembr.summarizer.llm.api import APIBackend
from sembr.summarizer.llm.base import BaseLLMBackend


def build_llm_backend(settings: Settings) -> BaseLLMBackend:
    return APIBackend(
        base_url=settings.llm_api_base_url,
        api_key=settings.llm_api_key.get_secret_value(),
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
    )
