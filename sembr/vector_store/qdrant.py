"""Async Qdrant client wrapper.

The lifespan owns one `AsyncQdrantClient` per process; `/health` calls `ping()`.
This module does not assume any particular collection layout — collection
bootstrap lives in `vector_store.intents` and `vector_store.news`.
"""
from __future__ import annotations

from qdrant_client import AsyncQdrantClient

# Floor on Qdrant operation timeout. Without this, a stuck server can hang the
# embedder worker tick or a /health probe indefinitely. The dashboard wraps
# its own ping() in asyncio.wait_for; this floor protects every other caller.
_DEFAULT_TIMEOUT_SECONDS = 30.0


class QdrantHandle:
    def __init__(self, url: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self._client = AsyncQdrantClient(url=url, timeout=timeout)

    @property
    def client(self) -> AsyncQdrantClient:
        return self._client

    async def ping(self) -> bool:
        """True iff Qdrant responds to `get_collections`. No collection assumptions."""
        try:
            await self._client.get_collections()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self._client.close()
