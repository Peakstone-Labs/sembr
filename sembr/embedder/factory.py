"""Embedder factory: build_embedder(settings) → BaseEmbedder.

Mirrors the pattern in sembr/summarizer/factory.py. Callers depend only on
BaseEmbedder; the concrete backend is selected by Settings.embedder_backend.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sembr.config import Settings
    from sembr.embedder.base import BaseEmbedder


def build_embedder(settings: "Settings") -> "BaseEmbedder":
    if settings.embedder_backend == "siliconflow":
        from sembr.embedder.openai_compat import SiliconFlowEmbedder

        api_key = settings.embedder_api_key.get_secret_value()
        if not api_key.strip():
            raise ValueError(
                "EMBEDDER_API_KEY must be set when using the siliconflow backend"
            )
        return SiliconFlowEmbedder(
            api_key=api_key,
            base_url=settings.embedder_api_base_url,
            model=settings.embedder_model,
            timeout=settings.embedder_timeout_seconds,
        )

    raise ValueError(f"Unknown embedder backend: {settings.embedder_backend!r}")
