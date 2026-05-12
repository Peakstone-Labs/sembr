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
    sembr's news layout) or a `dict[name, list[float]]` (named-vector). The
    dict fallback (`next(iter(raw.values()))`) here is unreliable for named-vec
    intents — its iteration order is server-response-dependent — so this helper
    is intended only for unnamed-vector callers (news collection / matcher
    article-side). Intent-side callers must use `extract_named_vector(point, slot)`
    so a missing slot returns None instead of silently aliasing onto another slot.
    """
    raw = getattr(point, "vector", None)
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("default") or next(iter(raw.values()), None)
    return list(raw) if raw is not None else None


def extract_named_vector(point, slot: str) -> list[float] | None:
    """Return the vector stored at `slot` on a named-vector point.

    Returns None when:
      - point carries no vector at all,
      - point.vector is not a dict (legacy unnamed-vector layout — caller
        should treat as "wrong layout, skip"),
      - the requested slot is absent from the dict (intent had no sub_text
        for this slot — caller may skip or treat as no-hit-on-this-slot).

    The strict no-fallback contract is required so a `next(iter(...))` fallback
    cannot silently return e.g. alt_0 when the caller asked for main.
    """
    raw = getattr(point, "vector", None)
    if not isinstance(raw, dict):
        return None
    v = raw.get(slot)
    if not isinstance(v, list):
        return None
    return list(v)
