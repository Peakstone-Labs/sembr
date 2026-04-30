"""OpenAI-compatible embedding backend (SiliconFlow + any /v1/embeddings endpoint).

Implements BaseEmbedder for remote API backends that speak the OpenAI embeddings
protocol. SiliconFlowEmbedder is the concrete subclass for this MVP; the class
name `openai_compat` leaves room for future Voyage/Jina backends in the same file.
"""
from __future__ import annotations

import logging
from typing import Literal

import httpx

from sembr.embedder.base import BaseEmbedder

logger = logging.getLogger(__name__)


class EmbedderAPIError(Exception):
    """Base class for remote embeddings API errors."""


class EmbedderTransportError(EmbedderAPIError):
    """HTTP / network-level failure — non-2xx response or connection error.

    Operators should check their API key, network route, or SiliconFlow status page.
    """


class EmbedderSchemaError(EmbedderAPIError):
    """Unexpected response schema — missing 'data', wrong embedding type, length mismatch.

    Indicates a sembr bug or an incompatible /v1/embeddings implementation.
    """


class SiliconFlowEmbedder(BaseEmbedder):
    """BGE-M3 via SiliconFlow /v1/embeddings (OpenAI-compatible protocol)."""

    MODEL_VERSION = "bge-m3_v1"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.siliconflow.cn/v1",
        model: str = "BAAI/bge-m3",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        # Baseline used by load() probe and as fallback when callers don't pass
        # a per-request timeout. Batch calls compute a dynamic timeout from
        # total char count and override this via aembed(timeout=...).
        self._timeout = httpx.Timeout(connect=10.0, read=float(timeout), write=10.0, pool=5.0)
        self._status: Literal["loading", "ok", "error"] = "loading"
        self._client: httpx.AsyncClient | None = None
        if not self._base_url.startswith(("https://", "http://localhost", "http://127.")):
            logger.warning(
                "embedder base_url is non-HTTPS: %s — API key will be sent in cleartext",
                self._base_url,
            )

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
        """Run startup probe. Sets status to 'ok' or 'error'. Never raises.

        Does not retry on failure — a bad API key or network outage should
        surface as a persistent 503 so operators notice rather than spin in a retry loop.
        """
        self._client = httpx.AsyncClient(timeout=self._timeout)
        try:
            vectors = await self._call([" "])
            if not vectors or len(vectors[0]) != 1024:
                raise ValueError(
                    f"unexpected probe response: dim={len(vectors[0]) if vectors else 0}"
                )
            self._status = "ok"
            logger.info("siliconflow probe ok, model=%s", self._model)
        except (EmbedderAPIError, ValueError) as exc:
            self._status = "error"
            logger.error("siliconflow probe failed: %s", exc, exc_info=True)

    async def _call(
        self, texts: list[str], *, timeout: float | None = None
    ) -> list[list[float]]:
        if self._client is None:
            raise RuntimeError("call load() before _call()")
        request_timeout: httpx.Timeout | None = (
            httpx.Timeout(connect=10.0, read=float(timeout), write=10.0, pool=5.0)
            if timeout is not None
            else None  # fall back to client default (self._timeout)
        )
        try:
            response = await self._client.post(
                f"{self._base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "input": texts,
                    "encoding_format": "float",
                },
                timeout=request_timeout,
            )
        except httpx.HTTPError as exc:
            raise EmbedderTransportError(
                f"SiliconFlow request failed: {type(exc).__name__}: {exc!s} | {exc!r}"
            ) from exc
        if response.status_code != 200:
            safe_body = response.text[:200].replace(self._api_key, "***")
            raise EmbedderTransportError(
                f"SiliconFlow API error {response.status_code}: {safe_body}"
            )
        data = response.json()
        items = data.get("data")
        if not isinstance(items, list):
            raise EmbedderSchemaError(
                f"unexpected payload, missing 'data' list: {str(data)[:200]}"
            )
        out: list[list[float]] = []
        for it in items:
            emb = it.get("embedding") if isinstance(it, dict) else None
            if not isinstance(emb, list) or not emb or not isinstance(emb[0], (int, float)):
                raise EmbedderSchemaError(
                    f"unexpected embedding shape: {type(emb).__name__}"
                )
            out.append(emb)
        if len(out) != len(texts):
            raise EmbedderSchemaError(
                f"length mismatch: {len(texts)} requested, {len(out)} returned"
            )
        return out

    def _for_testing_set_loaded(self, client: httpx.AsyncClient | None = None) -> None:
        """Test helper — bypasses load() by injecting a client and setting status=ok.

        Use in tests that want to exercise aembed/aclose without hitting a real probe;
        avoids direct access to private _client/_status attributes.
        """
        self._client = client or httpx.AsyncClient()
        self._status = "ok"

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("SiliconFlowEmbedder is async-only; use aembed()")

    async def aembed(
        self, texts: list[str], *, timeout: float | None = None
    ) -> list[list[float]]:
        if self._status != "ok":
            raise RuntimeError("embedder not loaded")
        if not texts:
            return []
        return await self._call(texts, timeout=timeout)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._status = "error"
