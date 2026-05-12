# SPDX-License-Identifier: Apache-2.0
"""Tests for /api/dashboard/logs/* endpoints."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.dashboard.logs_routes import _log_generator, router
from sembr.logbus.bus import _reset_for_test
from sembr.logbus.install import install_logbus


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture(autouse=True)
def fresh_bus():
    bus = _reset_for_test(buffer_per_tag=100)
    yield bus
    _reset_for_test()


@pytest.fixture()
def client(fresh_bus):
    return TestClient(_make_app(), raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# GET /tags
# ---------------------------------------------------------------------------


def test_get_tags_returns_7(client) -> None:
    resp = client.get("/api/dashboard/logs/tags")
    assert resp.status_code == 200
    data = resp.json()
    assert "tags" in data
    assert len(data["tags"]) == 7
    names = {t["name"] for t in data["tags"]}
    assert names == {"collector", "embedder", "matcher", "notifier", "api", "scheduler", "http"}
    assert "available_levels" in data
    assert set(data["available_levels"]) == {"DEBUG", "INFO", "WARNING", "ERROR"}


# ---------------------------------------------------------------------------
# PUT /level
# ---------------------------------------------------------------------------


def test_put_level_changes_tag_level(client, fresh_bus) -> None:
    resp = client.put(
        "/api/dashboard/logs/level",
        json={"tag": "embedder", "level": "DEBUG"},
    )
    assert resp.status_code == 204
    levels = fresh_bus.get_tag_levels()
    assert levels["embedder"] == logging.DEBUG


def test_put_level_invalid_tag_422(client) -> None:
    resp = client.put(
        "/api/dashboard/logs/level",
        json={"tag": "nonexistent", "level": "INFO"},
    )
    assert resp.status_code == 422


def test_put_level_invalid_level_422(client) -> None:
    resp = client.put(
        "/api/dashboard/logs/level",
        json={"tag": "api", "level": "CRITICAL"},
    )
    assert resp.status_code == 422


def test_put_level_http_syncs_third_party_loggers(client) -> None:
    resp = client.put(
        "/api/dashboard/logs/level",
        json={"tag": "http", "level": "DEBUG"},
    )
    assert resp.status_code == 204
    assert logging.getLogger("httpx").level == logging.DEBUG
    assert logging.getLogger("httpcore").level == logging.DEBUG
    assert logging.getLogger("uvicorn.access").level == logging.DEBUG

    resp2 = client.put(
        "/api/dashboard/logs/level",
        json={"tag": "http", "level": "WARNING"},
    )
    assert resp2.status_code == 204
    assert logging.getLogger("httpx").level == logging.WARNING


# ---------------------------------------------------------------------------
# install_logbus — stderr StreamHandler stays at INFO after root lowered to DEBUG (🟡-2 regression)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_install_logbus_stderr_stays_info(fresh_bus) -> None:
    """install_logbus must pin existing StreamHandlers at INFO before lowering root to DEBUG."""
    root = logging.getLogger()
    # Simulate basicConfig: add a stream handler with NOTSET level if not already present
    stream_h = logging.StreamHandler()
    stream_h.setLevel(logging.NOTSET)
    root.addHandler(stream_h)
    original_root_level = root.level

    try:
        loop = asyncio.get_event_loop()
        fresh_bus.set_loop(loop)
        install_logbus(loop, buffer_per_tag=100, default_level=logging.INFO)
        # Root must be DEBUG (so handler can receive everything)
        assert root.level == logging.DEBUG
        # The stream handler must now be pinned at INFO (not NOTSET)
        assert stream_h.level == logging.INFO
    finally:
        root.removeHandler(stream_h)
        root.setLevel(original_root_level)
        # Remove the RingBufferHandler installed by install_logbus to avoid side effects
        from sembr.logbus.handler import RingBufferHandler

        for h in root.handlers[:]:
            if isinstance(h, RingBufferHandler):
                root.removeHandler(h)
        _reset_for_test()


# ---------------------------------------------------------------------------
# GET /stream  — route-level check (no SSE body drain)
# ---------------------------------------------------------------------------


def test_stream_invalid_tag_422(client) -> None:
    resp = client.get("/api/dashboard/logs/stream?tag=bogus")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# _log_generator unit tests (async, no HTTP layer)
# ---------------------------------------------------------------------------


class _MockRequest:
    """Minimal stand-in for starlette.requests.Request in generator tests."""

    def __init__(self, *, disconnect_after: int = 9999) -> None:
        self._calls = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._disconnect_after


async def _collect_generator(tag: str, request, max_chunks: int = 20) -> list[str]:
    """Drain up to *max_chunks* SSE chunks; return flat list of individual lines."""
    raw_lines: list[str] = []
    count = 0
    async for chunk in _log_generator(tag, request):
        for line in chunk.split("\n"):
            if line:  # skip blank separator lines
                raw_lines.append(line)
        count += 1
        if count >= max_chunks:
            break
    return raw_lines


@pytest.mark.anyio
async def test_stream_generator_history_and_history_end(fresh_bus) -> None:
    """Generator emits buffered history entries then history-end."""
    fresh_bus.set_loop(asyncio.get_event_loop())

    for i in range(3):
        fresh_bus.emit(
            "api",
            {
                "ts": i,
                "level": "INFO",
                "level_no": logging.INFO,
                "logger": "sembr.api",
                "tag": "api",
                "message": f"msg{i}",
                "exc": None,
            },
        )

    req = _MockRequest(disconnect_after=0)  # disconnect immediately after history
    lines = await _collect_generator("api", req, max_chunks=50)

    event_types = [l.split(":", 1)[1].strip() for l in lines if l.startswith("event:")]
    data_lines = [l for l in lines if l.startswith("data:") and l != "data: {}"]

    assert "log" in event_types, f"expected 'log' events, got {event_types}"
    assert "history-end" in event_types, f"expected history-end, got {event_types}"
    assert len(data_lines) == 3  # all 3 pre-populated entries


@pytest.mark.anyio
async def test_stream_generator_filters_by_tag(fresh_bus) -> None:
    """Only entries matching the requested tag are yielded by the generator."""
    fresh_bus.set_loop(asyncio.get_event_loop())

    for tag in ("api", "embedder"):
        fresh_bus.emit(
            tag,
            {
                "ts": 1,
                "level": "INFO",
                "level_no": logging.INFO,
                "logger": "test",
                "tag": tag,
                "message": tag,
                "exc": None,
            },
        )

    req = _MockRequest(disconnect_after=0)
    lines = await _collect_generator("api", req, max_chunks=50)
    data_lines = [l for l in lines if l.startswith("data:") and l != "data: {}"]

    for dl in data_lines:
        payload = json.loads(dl[len("data:") :].strip())
        assert payload["tag"] == "api", f"unexpected tag: {payload}"


@pytest.mark.anyio
async def test_stream_generator_live_entry_after_history_end(fresh_bus) -> None:
    """An entry emitted after subscribe() appears in the live stream."""
    loop = asyncio.get_event_loop()
    fresh_bus.set_loop(loop)

    # Disconnect only after we've received the "log" chunk for the live entry.
    # _log_generator checks is_disconnected() once per poll interval (1s);
    # we collect up to max_chunks=5 which is enough to receive history-end + live entry.
    req = _MockRequest(disconnect_after=5)

    async def _emit_live():
        await asyncio.sleep(0.05)
        fresh_bus.emit(
            "collector",
            {
                "ts": 999,
                "level": "INFO",
                "level_no": logging.INFO,
                "logger": "sembr.collector",
                "tag": "collector",
                "message": "live_entry",
                "exc": None,
            },
        )

    task = asyncio.create_task(_emit_live())
    # max_chunks=3: history-end chunk + live-entry chunk + one more; generator stops on disconnect
    lines = await _collect_generator("collector", req, max_chunks=3)
    await task

    messages = []
    for l in lines:
        if l.startswith("data:") and "live_entry" in l:
            messages.append(json.loads(l[len("data:") :].strip())["message"])

    assert "live_entry" in messages, f"live entry not found in lines: {lines}"
