# SPDX-License-Identifier: Apache-2.0
"""HostLimiter caps concurrency per group_key; derive_group_key uses path for proxy hosts."""

from __future__ import annotations

import asyncio

import pytest

from sembr.collector.host_limiter import HostLimiter, derive_group_key

PROXY = frozenset({"rsshub:1200"})


def test_group_key_direct_host() -> None:
    assert derive_group_key("https://feeds.bbci.co.uk/news/rss.xml", PROXY) == "feeds.bbci.co.uk"


def test_group_key_proxy_host_uses_first_path_segment() -> None:
    # Two backends behind one RSSHub must split.
    a = derive_group_key("http://rsshub:1200/twitter/user/foo", PROXY)
    b = derive_group_key("http://rsshub:1200/github/issue/bar", PROXY)
    assert a == "rsshub:1200:twitter"
    assert b == "rsshub:1200:github"
    assert a != b


def test_group_key_proxy_host_no_path_falls_back_to_host() -> None:
    assert derive_group_key("http://rsshub:1200/", PROXY) == "rsshub:1200"


def test_group_key_handles_uppercase_host() -> None:
    assert derive_group_key("HTTPS://Example.COM/feed", PROXY) == "example.com"


@pytest.mark.asyncio
async def test_acquire_caps_concurrency_per_group() -> None:
    limiter = HostLimiter(PROXY, max_per_host=2)
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def task() -> None:
        nonlocal active, peak
        async with limiter.acquire("example.com"):
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.05)
            async with lock:
                active -= 1

    await asyncio.gather(*(task() for _ in range(5)))
    assert peak <= 2


@pytest.mark.asyncio
async def test_distinct_groups_are_not_blocked() -> None:
    limiter = HostLimiter(PROXY, max_per_host=1)
    started = asyncio.Event()
    block = asyncio.Event()

    async def slow_a() -> None:
        async with limiter.acquire("a"):
            started.set()
            await block.wait()

    async def fast_b() -> str:
        async with limiter.acquire("b"):
            return "ok"

    t = asyncio.create_task(slow_a())
    await started.wait()
    # If group "b" were blocked by group "a", this would hang.
    result = await asyncio.wait_for(fast_b(), timeout=1.0)
    assert result == "ok"
    block.set()
    await t


def test_max_per_host_must_be_positive() -> None:
    with pytest.raises(ValueError):
        HostLimiter(PROXY, max_per_host=0)
