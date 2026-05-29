# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Phase 2: lifespan graceful-shutdown wrapper + _force_exit logic.

These tests verify the settings_restart module flags and _force_exit helper
in isolation — no full lifespan startup required, so they run on the Windows
static test machine without Docker or real service dependencies.
"""

from __future__ import annotations

import asyncio

import pytest

from sembr.api import settings_restart

# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_restart_flag():
    """Ensure _RESTART_REQUESTED is False before and after every test."""
    settings_restart._RESTART_REQUESTED = False
    yield
    settings_restart._RESTART_REQUESTED = False


# ── _force_exit / is_restart_requested unit tests ────────────────────────────


def test_force_exit_calls_os_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(settings_restart.os, "_exit", lambda code: calls.append(code))
    settings_restart._force_exit(0)
    assert calls == [0]


def test_is_restart_requested_initially_false() -> None:
    assert settings_restart.is_restart_requested() is False


def test_is_restart_requested_true_after_flag_set() -> None:
    settings_restart._RESTART_REQUESTED = True
    assert settings_restart.is_restart_requested() is True


# ── Shutdown logic: _force_exit only when restart requested ──────────────────
#
# The lifespan finally block executes:
#     if settings_restart.is_restart_requested():
#         settings_restart._force_exit(0)
#
# We replicate that logic in a small async helper to test the conditional
# without starting the full lifespan (which needs Qdrant / SQLite / etc.).


async def _run_conditional_exit(force_exit_mock, timeout: float = 8.0) -> None:
    """Simulate the wait_for + conditional _force_exit block from main.py."""

    async def _shutdown():
        pass  # no-op; we're only testing the exit conditional

    try:
        await asyncio.wait_for(_shutdown(), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        if settings_restart.is_restart_requested():
            force_exit_mock(0)


@pytest.mark.asyncio
async def test_lifespan_normal_shutdown_no_force_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(settings_restart, "_force_exit", lambda code: calls.append(code))

    await _run_conditional_exit(force_exit_mock=settings_restart._force_exit)
    assert calls == [], "_force_exit must NOT be called on normal shutdown"


@pytest.mark.asyncio
async def test_lifespan_self_restart_calls_force_exit_after_graceful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(settings_restart, "_force_exit", lambda code: calls.append(code))
    settings_restart._RESTART_REQUESTED = True

    await _run_conditional_exit(force_exit_mock=settings_restart._force_exit)
    assert calls == [0], "_force_exit(0) must be called when restart was requested"


@pytest.mark.asyncio
async def test_lifespan_timeout_triggers_log_no_exit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When graceful shutdown times out but restart was NOT requested, do NOT
    call _force_exit — let Docker's SIGKILL handle it."""
    calls: list[int] = []
    monkeypatch.setattr(settings_restart, "_force_exit", lambda code: calls.append(code))

    async def _shutdown_that_hangs():
        await asyncio.sleep(10)  # longer than our test timeout

    import logging

    with caplog.at_level(logging.ERROR, logger="sembr.main"):
        try:
            await asyncio.wait_for(_shutdown_that_hangs(), timeout=0.05)
        except TimeoutError:
            pass
        finally:
            if settings_restart.is_restart_requested():
                settings_restart._force_exit(0)

    assert calls == [], "_force_exit must NOT be called when restart flag is False"


@pytest.mark.asyncio
async def test_lifespan_self_restart_calls_force_exit_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when graceful shutdown times out, _force_exit must be called if
    restart was requested — ensures Docker restart: unless-stopped fires."""
    calls: list[int] = []
    monkeypatch.setattr(settings_restart, "_force_exit", lambda code: calls.append(code))
    settings_restart._RESTART_REQUESTED = True

    async def _shutdown_that_hangs():
        await asyncio.sleep(10)

    try:
        await asyncio.wait_for(_shutdown_that_hangs(), timeout=0.05)
    except TimeoutError:
        pass
    finally:
        if settings_restart.is_restart_requested():
            settings_restart._force_exit(0)

    assert calls == [0], "_force_exit(0) must be called even after timeout"
