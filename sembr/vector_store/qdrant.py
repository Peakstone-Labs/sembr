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


def extract_point_vector(point) -> list[float] | None:
    """Return a flat float list from a Qdrant point's `vector` field.

    Qdrant returns vectors as either a flat list (unnamed-vector collection,
    sembr's default) or a `dict[name, list[float]]` (named-vector collection).
    Returns None when the point carries no vector or carries a named-vector
    dict without a resolvable default — callers must treat None as
    "vector absent" and skip the point rather than crash on type mismatch.
    """
    raw = getattr(point, "vector", None)
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("default") or next(iter(raw.values()), None)
    return list(raw) if raw is not None else None
