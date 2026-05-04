"""Tests for sembr.api.settings_restart."""
from __future__ import annotations

import asyncio
import signal
from unittest.mock import MagicMock

import pytest

from sembr.api import settings_restart
from sembr.api.settings_restart import (
    DEFAULT_SELF_SHUTDOWN_DELAY,
    RSSHUB_CONTAINER_NAME,
    RestartController,
)


@pytest.mark.asyncio
async def test_restart_rsshub_invokes_sdk_with_default_name() -> None:
    fake_container = MagicMock()
    fake_client = MagicMock()
    fake_client.containers.get.return_value = fake_container
    rc = RestartController(docker_client_factory=lambda: fake_client)

    await rc.restart_rsshub()

    fake_client.containers.get.assert_called_once_with(RSSHUB_CONTAINER_NAME)
    fake_container.restart.assert_called_once_with()


@pytest.mark.asyncio
async def test_restart_rsshub_supports_custom_name() -> None:
    fake_container = MagicMock()
    fake_client = MagicMock()
    fake_client.containers.get.return_value = fake_container
    rc = RestartController(docker_client_factory=lambda: fake_client)

    await rc.restart_rsshub("custom-rsshub")

    fake_client.containers.get.assert_called_once_with("custom-rsshub")
    fake_container.restart.assert_called_once_with()


@pytest.mark.asyncio
async def test_restart_rsshub_propagates_get_failure() -> None:
    fake_client = MagicMock()
    fake_client.containers.get.side_effect = Exception("not found")
    rc = RestartController(docker_client_factory=lambda: fake_client)

    with pytest.raises(RuntimeError, match="not found"):
        await rc.restart_rsshub()


@pytest.mark.asyncio
async def test_schedule_self_restart_uses_call_later(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []
    monkeypatch.setattr(
        settings_restart, "_send_sigterm_to_self", lambda: sent.append(signal.SIGTERM)
    )

    loop = asyncio.get_running_loop()
    rc = RestartController(loop=loop)
    rc.schedule_self_restart(delay=0.05)

    # Wait long enough for the call_later to fire.
    await asyncio.sleep(0.15)
    assert sent == [signal.SIGTERM]


@pytest.mark.asyncio
async def test_schedule_self_restart_default_delay_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """delay= default is the documented constant; rc.schedule with no arg uses it.

    We patch loop.call_later instead of waiting 1.5s.
    """
    captured: list[tuple[float, object]] = []
    loop = asyncio.get_running_loop()

    def fake_call_later(delay, fn):
        captured.append((delay, fn))

    monkeypatch.setattr(loop, "call_later", fake_call_later)
    rc = RestartController(loop=loop)
    rc.schedule_self_restart()

    assert len(captured) == 1
    assert captured[0][0] == DEFAULT_SELF_SHUTDOWN_DELAY


def test_send_sigterm_to_self_calls_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        settings_restart.os, "kill", lambda pid, sig: calls.append((pid, sig))
    )
    settings_restart._send_sigterm_to_self()
    assert len(calls) == 1
    pid, sig = calls[0]
    assert sig == signal.SIGTERM
    assert pid > 0
