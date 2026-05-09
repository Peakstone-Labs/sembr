"""Tests for sembr.api.settings_restart."""
from __future__ import annotations

import asyncio
import signal
import subprocess
import time
from subprocess import CompletedProcess
from unittest.mock import MagicMock

import pytest

from sembr.api import settings_restart
from sembr.api.settings_restart import (
    COMPOSE_FILE_PATH,
    DEFAULT_SELF_SHUTDOWN_DELAY,
    RSSHUB_SERVICE_NAME,
    RestartController,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_restart_flag():
    settings_restart._RESTART_REQUESTED = False
    yield
    settings_restart._RESTART_REQUESTED = False


# ── helpers ──────────────────────────────────────────────────────────────────

def _ok_runner(cmd, **kwargs):
    return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


# ── Phase 1: subprocess tests ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_restart_rsshub_invokes_compose_subprocess() -> None:
    captured: list[tuple[list[str], dict]] = []

    def fake_runner(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    rc = RestartController(subprocess_runner=fake_runner)
    await rc.restart_rsshub()

    assert len(captured) == 1
    cmd, kwargs = captured[0]
    assert "docker" in cmd
    assert "compose" in cmd
    assert "up" in cmd
    assert "-d" in cmd
    assert "--force-recreate" in cmd
    assert "--no-deps" in cmd
    assert RSSHUB_SERVICE_NAME in cmd   # service name "rsshub", not container name "sembr-rsshub"
    # D6: timeout=60 must be passed (design.md guarantees 4× headroom)
    assert kwargs.get("timeout") == 60
    assert kwargs.get("capture_output") is True


@pytest.mark.asyncio
async def test_restart_rsshub_passes_compose_file_path() -> None:
    captured: list[list[str]] = []

    def fake_runner(cmd, **kwargs):
        captured.append(cmd)
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    rc = RestartController(subprocess_runner=fake_runner)
    await rc.restart_rsshub()

    cmd = captured[0]
    assert "-f" in cmd
    f_idx = cmd.index("-f")
    assert cmd[f_idx + 1] == COMPOSE_FILE_PATH


@pytest.mark.asyncio
async def test_restart_rsshub_propagates_subprocess_failure() -> None:
    def fail_runner(cmd, **kwargs):
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="oops")

    rc = RestartController(subprocess_runner=fail_runner)

    with pytest.raises(RuntimeError, match="oops"):
        await rc.restart_rsshub()


@pytest.mark.asyncio
async def test_restart_rsshub_timeout() -> None:
    def timeout_runner(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, timeout=60)

    rc = RestartController(subprocess_runner=timeout_runner)

    with pytest.raises(RuntimeError, match="timed out"):
        await rc.restart_rsshub()


@pytest.mark.asyncio
async def test_restart_rsshub_runs_off_event_loop() -> None:
    """asyncio.to_thread wraps the call: the event loop stays responsive while
    the subprocess blocks.  We verify this by running a concurrent task that
    counts loop ticks while the fake runner sleeps 200ms.
    """
    tick_count = 0

    async def ticker():
        nonlocal tick_count
        for _ in range(20):
            await asyncio.sleep(0.02)
            tick_count += 1

    def slow_runner(cmd, **kwargs):
        time.sleep(0.2)
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    rc = RestartController(subprocess_runner=slow_runner)
    await asyncio.gather(rc.restart_rsshub(), ticker())

    # If subprocess.run ran on the event loop directly, the ticker would be
    # starved and tick_count would be 0.  With to_thread it should be ≥ 5.
    assert tick_count >= 5


# ── Phase 1: schedule_self_restart ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_self_restart_uses_call_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_later wires up the spawn helper. Implementation switched from
    SIGTERM-self to docker-compose-force-recreate-self in the env_file fix
    (see settings_restart docstring); the test pins call_later wiring."""
    fired: list[bool] = []
    monkeypatch.setattr(
        settings_restart, "_spawn_self_force_recreate", lambda: fired.append(True)
    )

    loop = asyncio.get_running_loop()
    rc = RestartController(loop=loop)
    rc.schedule_self_restart(delay=0.05)

    await asyncio.sleep(0.15)
    assert fired == [True]


@pytest.mark.asyncio
async def test_schedule_self_restart_default_delay_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[tuple[float, object]] = []
    loop = asyncio.get_running_loop()

    def fake_call_later(delay, fn):
        captured.append((delay, fn))

    monkeypatch.setattr(loop, "call_later", fake_call_later)
    rc = RestartController(loop=loop)
    rc.schedule_self_restart()

    assert len(captured) == 1
    assert captured[0][0] == DEFAULT_SELF_SHUTDOWN_DELAY


# ── Phase 2: _RESTART_REQUESTED flag ─────────────────────────────────────────

def test_spawn_self_force_recreate_invokes_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    """The detached spawn must invoke docker compose with the api service
    name and force-recreate flag. Captures Popen args without actually
    forking."""
    captured: list[tuple[list[str], dict]] = []

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured.append((argv, kwargs))

    monkeypatch.setattr(settings_restart.subprocess, "Popen", _FakePopen)
    settings_restart._spawn_self_force_recreate()
    assert len(captured) == 1
    argv, kwargs = captured[0]
    assert argv[:2] == ["docker", "compose"]
    assert "--force-recreate" in argv
    assert "--no-deps" in argv
    assert argv[-1] == settings_restart.API_SERVICE_NAME
    assert kwargs.get("start_new_session") is True
    assert kwargs.get("close_fds") is True


def test_spawn_self_force_recreate_sets_restart_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_restart.subprocess, "Popen",
        lambda *a, **k: None,  # suppress real fork
    )
    settings_restart._spawn_self_force_recreate()
    assert settings_restart.is_restart_requested() is True


def test_spawn_self_force_recreate_falls_back_to_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    """If Popen raises (no docker binary, etc.), fall back to SIGTERM-self
    rather than leaving the api process orphaned with a stale env."""
    def _boom(*a, **k):
        raise FileNotFoundError("docker not found")
    monkeypatch.setattr(settings_restart.subprocess, "Popen", _boom)

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        settings_restart.os, "kill", lambda pid, sig: sent.append((pid, sig))
    )
    settings_restart._spawn_self_force_recreate()
    assert len(sent) == 1
    assert sent[0][1] == signal.SIGTERM


def test_is_restart_requested_default_false() -> None:
    assert settings_restart.is_restart_requested() is False


