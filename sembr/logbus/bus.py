"""LogBus — in-process ring buffer with per-tag deques and asyncio fan-out.

Thread-safety: a single threading.Lock guards deques, subscribers, and
tag_levels.  The emit() hot path holds the lock for ~µs (dict lookup +
deque.append + N call_soon_threadsafe calls).
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from typing import Any

from sembr.logbus.router import ALL_TAGS


class LogBus:
    """Module-level singleton; created once, referenced by handler and routes."""

    def __init__(self, buffer_per_tag: int = 1000) -> None:
        self._lock = threading.Lock()
        self._deques: dict[str, deque[dict[str, Any]]] = {
            tag: deque(maxlen=buffer_per_tag) for tag in ALL_TAGS
        }
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._tag_levels: dict[str, int] = {tag: 20 for tag in ALL_TAGS}  # INFO=20
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def set_buffer_size(self, buffer_per_tag: int) -> None:
        with self._lock:
            for tag in ALL_TAGS:
                old = self._deques[tag]
                new: deque[dict[str, Any]] = deque(maxlen=buffer_per_tag)
                new.extend(old)
                self._deques[tag] = new

    # ------------------------------------------------------------------
    # Tag level management
    # ------------------------------------------------------------------

    def get_tag_levels(self) -> dict[str, int]:
        with self._lock:
            return dict(self._tag_levels)

    def set_tag_level(self, tag: str, level: int) -> None:
        with self._lock:
            self._tag_levels[tag] = level

    # ------------------------------------------------------------------
    # Emit (called from any thread via RingBufferHandler)
    # ------------------------------------------------------------------

    def emit(self, tag: str, entry: dict[str, Any]) -> None:
        """Append *entry* to the ring buffer and fan-out to active subscribers.

        Drops silently when no event loop is available (pre-lifespan start).
        Queue-full entries are dropped oldest-first (put_nowait on bounded queue
        raises Full; we discard the new entry instead of blocking the caller).
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        with self._lock:
            if entry["level_no"] < self._tag_levels[tag]:
                return
            self._deques[tag].append(entry)
            subs = list(self._subscribers)

        for q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, entry)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe (called from asyncio context)
    # ------------------------------------------------------------------

    def subscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> list[dict[str, Any]]:
        """Atomically snapshot all deques and register *q* as a subscriber.

        Returns a flat list of all buffered entries (history snapshot) in
        insertion order across all tags.  The caller must not yield between
        ``subscribe()`` and draining the returned snapshot — otherwise it
        risks missing or duplicating entries.
        """
        with self._lock:
            snapshot: list[dict[str, Any]] = []
            for tag_deque in self._deques.values():
                snapshot.extend(tag_deque)
            snapshot.sort(key=lambda e: e["ts"])
            self._subscribers.add(q)
        return snapshot

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    # ------------------------------------------------------------------
    # Snapshot for GET /tags (tag + current level)
    # ------------------------------------------------------------------

    def tag_info(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"name": tag, "level": self._tag_levels[tag]}
                for tag in ALL_TAGS
            ]


# Module-level singleton; swapped out in tests via _reset_for_test().
_bus: LogBus = LogBus()


def get_bus() -> LogBus:
    return _bus


def _reset_for_test(buffer_per_tag: int = 1000) -> LogBus:
    """Replace the singleton with a fresh instance and return it (test helper)."""
    global _bus
    _bus = LogBus(buffer_per_tag=buffer_per_tag)
    return _bus
