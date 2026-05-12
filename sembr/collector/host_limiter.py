# SPDX-License-Identifier: Apache-2.0
"""Per-host concurrency limiter.

Process-local dict[group_key -> asyncio.Semaphore]. Lazy-create on first
acquire. Single-process sembr deployment makes this sufficient; multi-worker
deployment would need a distributed primitive instead.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse


def derive_group_key(url: str, proxy_hosts: frozenset[str]) -> str:
    """Derive a per-feed grouping key for concurrency control.

    Hostname (with port) is the default key. For hosts in `proxy_hosts`
    (e.g. an RSSHub instance fronting many backends), the first path segment
    is appended so different backends don't collapse onto the same semaphore
    (clarify Q7).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    host_key = f"{host}{port}"
    if host_key in proxy_hosts:
        first_seg = parsed.path.lstrip("/").split("/", 1)[0]
        if first_seg:
            return f"{host_key}:{first_seg}"
    return host_key


class HostLimiter:
    def __init__(self, proxy_hosts: frozenset[str], max_per_host: int = 2) -> None:
        if max_per_host < 1:
            raise ValueError("max_per_host must be >= 1")
        self._proxy_hosts = proxy_hosts
        self._max = max_per_host
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        # Lock is lazy so HostLimiter can be constructed outside a running loop
        # (e.g. module import in static tests). asyncio.Lock binds to the running
        # loop on first await; constructing pre-loop and using post-loop fails on
        # older Pythons. (Loop 2 review #🟢-2)
        self._lock: asyncio.Lock | None = None

    def group_key_for(self, url: str) -> str:
        return derive_group_key(url, self._proxy_hosts)

    async def _get_semaphore(self, group_key: str) -> asyncio.Semaphore:
        sem = self._semaphores.get(group_key)
        if sem is not None:
            return sem
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            sem = self._semaphores.get(group_key)
            if sem is None:
                sem = asyncio.Semaphore(self._max)
                self._semaphores[group_key] = sem
            return sem

    @asynccontextmanager
    async def acquire(self, group_key: str):
        sem = await self._get_semaphore(group_key)
        async with sem:
            yield
