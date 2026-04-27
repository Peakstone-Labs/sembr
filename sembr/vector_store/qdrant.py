"""Async Qdrant client wrapper.

Lifespan owns one `AsyncQdrantClient` per process; `/health` calls `ping()`.
This Feature does not create or assume any collection (上游约束: dual-collection design
is owned by later features).
"""
from __future__ import annotations

from qdrant_client import AsyncQdrantClient


class QdrantHandle:
    def __init__(self, url: str) -> None:
        self._url = url
        self._client = AsyncQdrantClient(url=url)

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
