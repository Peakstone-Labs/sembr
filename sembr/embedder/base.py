# SPDX-License-Identifier: Apache-2.0
"""Embedder abstract base.

设计决策 #7 / Option Set D-2: only the smallest surface this Feature can justify is fixed.
Subclasses MAY add async / batching variants without breaking callers — the only
required override is `embed`; `aembed` has a thread-pool fallback that suits local
backends, while remote backends (e.g. SiliconFlow) override `aembed` directly.
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
    def dim(self) -> int:
        """Embedding dimensionality. Vector store uses this to size its collections.

        Tied to the backend model. Vector-store callers must read this rather than
        hardcode a literal — a model swap that changes dim would otherwise produce
        a silently mismatched collection.
        """

    @property
    @abstractmethod
    def max_input_chars(self) -> int:
        """Per-text character cap the worker applies before calling `aembed`.

        Tied to the backend model's context window; the worker does not assume a value.
        Subclasses pick the bound from their tokenizer + safety margin.
        """

    @property
    @abstractmethod
    def is_unit_normalized(self) -> bool:
        """True iff the backend returns L2-normalized vectors.

        When True, callers may compute cosine similarity as a plain dot product
        (the event-driven matcher relies on this to score in pure Python without
        a numpy dependency). When False, callers must divide by the L2 norms or
        ask Qdrant to compute cosine. A backend that lies here will produce
        silently wrong scores — there is no runtime guard.
        """

    @property
    @abstractmethod
    def is_loaded(self) -> bool:
        """False until the underlying model weights are in memory."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Synchronous inference. Consumers must call `await aembed(...)` instead."""

    async def aembed(self, texts: list[str], *, timeout: float | None = None) -> list[list[float]]:
        """Async wrapper that offloads sync `embed` to a thread pool.

        Remote/async backends can override this directly without changing the
        sync signature — the only requirement for subclasses is implementing `embed`.

        `timeout` is honoured by remote backends (e.g. OpenAI-compatible HTTP)
        and ignored by local backends where it has no meaning.
        """
        return await asyncio.to_thread(self.embed, texts)
