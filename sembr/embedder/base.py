"""Embedder abstract base.

设计决策 #7 / Option Set D-2: only the smallest surface this Feature can justify is fixed
now. Real model loading lands in a later feature; nothing in this scaffold imports
this module yet. Subclasses MAY add async / batching variants without breaking callers.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod


class BaseEmbedder(ABC):
    @property
    @abstractmethod
    def model_version(self) -> str:
        """Identifier persisted in payload `embedding_model_version` for collection aliasing."""

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        """False until the underlying model weights are in memory."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Synchronous inference. Consumers must call `await aembed(...)` instead."""

    async def aembed(
        self, texts: list[str], *, timeout: float | None = None
    ) -> list[list[float]]:
        """Async wrapper that offloads sync `embed` to a thread pool.

        Remote/async backends can override this directly without changing the
        sync signature — the only requirement for subclasses is implementing `embed`.

        `timeout` is honoured by remote backends (e.g. OpenAI-compatible HTTP)
        and ignored by local backends where it has no meaning.
        """
        return await asyncio.to_thread(self.embed, texts)
