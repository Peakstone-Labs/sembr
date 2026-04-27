"""BGE-M3 embedder implementation.

Model is loaded as a background asyncio.Task in lifespan so FastAPI starts
accepting requests immediately. /health returns 503 until is_loaded=True (D3).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from sembr.embedder.base import BaseEmbedder

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)


class BgeM3Embedder(BaseEmbedder):
    MODEL_NAME = "BAAI/bge-m3"
    MODEL_VERSION = "bge-m3_v1"
    DIM = 1024

    def __init__(self) -> None:
        self._model: SentenceTransformer | None = None
        self._status: Literal["loading", "ok", "error"] = "loading"
        self._error: str | None = None

    @property
    def model_version(self) -> str:
        return self.MODEL_VERSION

    @property
    def is_loaded(self) -> bool:
        return self._status == "ok"

    @property
    def status(self) -> Literal["loading", "ok", "error"]:
        return self._status

    async def load(self) -> None:
        """Background-load model weights. Sets status to 'ok' or 'error'. Never raises.

        Does not retry on failure — a bad HF_HOME path or network outage should
        surface as a persistent 503 so operators notice rather than spin in a retry loop.
        """
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415

            # fp16 + low_cpu_mem_usage cuts peak RAM from ~4.5 GB to ~1.5 GB.
            # low_cpu_mem_usage alone wasn't enough on this path (sentence-transformers
            # 3.4.1 + BGE-M3 still peaked past 6g). fp16 quality loss is negligible
            # for sub-1k-token RSS articles; only ~8k-token inputs risk numerical drift.
            self._model = await asyncio.to_thread(
                lambda: SentenceTransformer(
                    self.MODEL_NAME,
                    model_kwargs={"torch_dtype": "float16", "low_cpu_mem_usage": True},
                )
            )
            self._status = "ok"
            logger.info("bge-m3 loaded (dim=%d)", self.DIM)
        except Exception as exc:
            self._status = "error"
            self._error = str(exc)
            logger.error("bge-m3 load failed: %s", exc, exc_info=True)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            raise RuntimeError("embedder not loaded")
        return self._model.encode(texts, normalize_embeddings=True).tolist()
