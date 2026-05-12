# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sembr.dashboard.system_metrics (design D2/D3/D4/D5).

Covers:
- ``SystemMetricsCollector`` rolling-window behaviour (maxlen, available flag)
- ``_compute_cpu_percent`` standard formula + edge cases (None when no baseline)
- ``_take_docker_sample`` mark_unavailable on DockerException
- ``add_system_metrics_job`` registers with coalesce=True + replace_existing=True
  and never passes next_run_time (memory: feedback_apscheduler_next_run_time)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from sembr.dashboard import system_metrics as sm
from sembr.dashboard.schemas import ContainerMetric


# ── _Sample / Collector ───────────────────────────────────────────────────────


def _make_sample(t: datetime, cpu: float | None = 1.0, mem: int = 1024) -> sm._Sample:
    return sm._Sample(
        sampled_at=t,
        containers=[
            ContainerMetric(
                name="sembr-api",
                uptime_seconds=10,
                cpu_percent=cpu,
                mem_used_bytes=mem,
                mem_limit_bytes=4 * 1024 * 1024 * 1024,
            )
        ],
    )


def test_collector_append_respects_maxlen():
    c = sm.SystemMetricsCollector(interval_seconds=10, maxlen=3)
    base = datetime(2026, 5, 8, tzinfo=timezone.utc)
    for i in range(5):
        c.append(_make_sample(base + timedelta(seconds=i), cpu=float(i)))
    block = c.read()
    assert block is not None
    # Only the last 3 samples (cpu=2, 3, 4) should remain
    series = block.containers[0].cpu_history
    assert series == [2.0, 3.0, 4.0]


def test_collector_unavailable_returns_none():
    c = sm.SystemMetricsCollector(interval_seconds=10)
    c.append(_make_sample(datetime(2026, 5, 8, tzinfo=timezone.utc)))
    c.mark_unavailable()
    assert c.read() is None
    c.mark_available()
    assert c.read() is not None


def test_collector_empty_returns_none():
    c = sm.SystemMetricsCollector(interval_seconds=10)
    assert c.read() is None


def test_collector_disappearing_container_pads_with_none():
    c = sm.SystemMetricsCollector(interval_seconds=10, maxlen=3)
    base = datetime(2026, 5, 8, tzinfo=timezone.utc)
    # First sample: two containers
    c.append(
        sm._Sample(
            sampled_at=base,
            containers=[
                ContainerMetric(name="a", cpu_percent=1.0, mem_used_bytes=100),
                ContainerMetric(name="b", cpu_percent=2.0, mem_used_bytes=200),
            ],
        )
    )
    # Second sample: only container "a" survives
    c.append(
        sm._Sample(
            sampled_at=base + timedelta(seconds=1),
            containers=[
                ContainerMetric(name="a", cpu_percent=1.5, mem_used_bytes=110),
            ],
        )
    )
    block = c.read()
    assert block is not None
    by_name = {cm.name: cm for cm in block.containers}
    assert by_name["a"].cpu_history == [1.0, 1.5]
    # Container "b" still appears in the listing because it was seen at least
    # once in the buffer; the missing slot is None so the chart aligns.
    assert by_name["b"].cpu_history == [2.0, None]


# ── _compute_cpu_percent ──────────────────────────────────────────────────────


def test_cpu_percent_standard_formula():
    """Reproduces the example from docker docs: cpu_delta=20, sys_delta=100,
    online_cpus=4 → (20/100)*4*100 = 80.0%."""
    stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 120},
            "system_cpu_usage": 200,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100},
            "system_cpu_usage": 100,
        },
    }
    assert sm._compute_cpu_percent(stats) == 80.0


def test_cpu_percent_first_sample_returns_none():
    """Daemon with no prior baseline → precpu_stats empty → CPU% must be
    None, not 0% (UX: 'first ~20s blank' is documented in design R3)."""
    stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 120},
            "system_cpu_usage": 200,
            "online_cpus": 4,
        },
        "precpu_stats": {},
    }
    assert sm._compute_cpu_percent(stats) is None


def test_cpu_percent_zero_system_returns_none():
    stats = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 100},
            "system_cpu_usage": 0,
            "online_cpus": 4,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 50},
            "system_cpu_usage": 0,
        },
    }
    assert sm._compute_cpu_percent(stats) is None


def test_cpu_percent_falls_back_to_percpu_length():
    """Some kernels don't populate online_cpus; SDK exposes percpu_usage
    array instead."""
    stats = {
        "cpu_stats": {
            "cpu_usage": {
                "total_usage": 110,
                "percpu_usage": [10, 20, 30, 50],  # 4 cores
            },
            "system_cpu_usage": 200,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100},
            "system_cpu_usage": 100,
        },
    }
    # delta = 10/100 * 4 * 100 = 40.0
    assert sm._compute_cpu_percent(stats) == 40.0


def test_cpu_percent_garbage_returns_none():
    assert sm._compute_cpu_percent({}) is None
    assert sm._compute_cpu_percent({"cpu_stats": None}) is None


# ── _take_docker_sample / sampler ─────────────────────────────────────────────


def _fake_container(name: str, *, started: datetime | None = None, stats: dict | None = None):
    c = MagicMock()
    c.name = name
    c.id = "0123456789abcdef" * 4
    c.attrs = (
        {"State": {"StartedAt": started.isoformat().replace("+00:00", "Z") + "0"}}
        if started
        else {"State": {}}
    )
    c.stats = MagicMock(
        return_value=stats
        or {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 120},
                "system_cpu_usage": 200,
                "online_cpus": 1,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 100,
            },
            "memory_stats": {"usage": 1_000_000, "limit": 4_000_000_000},
        }
    )
    return c


def test_take_docker_sample_returns_none_on_docker_unavailable(monkeypatch):
    """``docker.from_env()`` raises DockerException when the socket is missing
    or the daemon is unreachable. Sampler must return None — the caller flips
    the collector to unavailable (D5)."""
    from docker.errors import DockerException

    fake_docker = MagicMock()
    fake_docker.from_env.side_effect = DockerException("Cannot connect to socket")
    monkeypatch.setitem(__import__("sys").modules, "docker", fake_docker)
    assert sm._take_docker_sample(project="sembr") is None


def test_take_docker_sample_uses_compose_label_filter(monkeypatch):
    """D3: discovery filter must pin to the docker-compose project label."""
    fake_client = MagicMock()
    fake_client.containers.list.return_value = [
        _fake_container("sembr-api", started=datetime(2026, 5, 8, tzinfo=timezone.utc))
    ]

    fake_docker = MagicMock()
    fake_docker.from_env.return_value = fake_client
    fake_docker.errors = MagicMock()  # not used in success path
    monkeypatch.setitem(__import__("sys").modules, "docker", fake_docker)

    sample = sm._take_docker_sample(project="myproj")
    assert sample is not None
    # Assert filter shape exactly: label key is the compose project label
    args, kwargs = fake_client.containers.list.call_args
    assert kwargs == {"filters": {"label": "com.docker.compose.project=myproj"}}
    assert len(sample.containers) == 1
    cm = sample.containers[0]
    assert cm.name == "sembr-api"
    assert cm.cpu_percent == 20.0  # (20/100)*1*100
    assert cm.mem_used_bytes == 1_000_000


def test_take_docker_sample_warns_once_when_zero_containers(monkeypatch, caplog):
    """Loop 2 💡-2: docker reachable but 0 containers match the project label
    is silent misconfig (e.g. user renamed dir, didn't set
    COMPOSE_PROJECT_NAME). Emit a single WARNING the first time, then stay
    quiet so we don't flood logs every poll interval."""
    import logging as _log

    # Reset module-state guard so the test sees the first-call behaviour.
    monkeypatch.setattr(sm, "_zero_container_warned", False)

    fake_client = MagicMock()
    fake_client.containers.list.return_value = []
    fake_docker = MagicMock()
    fake_docker.from_env.return_value = fake_client
    monkeypatch.setitem(__import__("sys").modules, "docker", fake_docker)

    with caplog.at_level(_log.WARNING, logger="sembr.dashboard.system_metrics"):
        sample1 = sm._take_docker_sample(project="renamed-dir")
        sample2 = sm._take_docker_sample(project="renamed-dir")

    assert sample1 is not None and sample1.containers == []
    assert sample2 is not None and sample2.containers == []
    misconfig_warnings = [
        r
        for r in caplog.records
        if r.levelno == _log.WARNING and "0 containers match label" in r.message
    ]
    assert len(misconfig_warnings) == 1, "warning must fire exactly once per process"


@pytest.mark.asyncio
async def test_run_sampler_marks_unavailable_on_failure(monkeypatch):
    """When ``_take_docker_sample`` returns None (docker unreachable), the
    collector must be flipped to unavailable so /snapshot reports null."""
    monkeypatch.setattr(sm, "_take_docker_sample", lambda: None)
    c = sm.SystemMetricsCollector(interval_seconds=10)
    await sm._run_sampler(c)
    assert c.available is False
    assert c.read() is None


@pytest.mark.asyncio
async def test_run_sampler_recovers_after_failure(monkeypatch):
    """Transient docker hiccup → sampler flips to unavailable; next successful
    sample must flip back so the panel un-mutes."""
    state = {"sample_calls": 0}

    def fake_sample():
        state["sample_calls"] += 1
        if state["sample_calls"] == 1:
            return None  # docker down
        return _make_sample(datetime(2026, 5, 8, tzinfo=timezone.utc))

    monkeypatch.setattr(sm, "_take_docker_sample", fake_sample)
    c = sm.SystemMetricsCollector(interval_seconds=10)
    await sm._run_sampler(c)
    assert c.available is False

    await sm._run_sampler(c)
    assert c.available is True
    assert c.read() is not None


# ── add_system_metrics_job ────────────────────────────────────────────────────


def test_add_system_metrics_job_registers_with_coalesce_and_replace_existing():
    """Per project convention (main.py:179-200) and feedback_apscheduler memo:
    coalesce=True + replace_existing=True; never pass next_run_time."""
    scheduler = MagicMock()
    collector = sm.SystemMetricsCollector(interval_seconds=10)

    sm.add_system_metrics_job(scheduler, collector, interval_seconds=10)

    scheduler.add_job.assert_called_once()
    args, kwargs = scheduler.add_job.call_args
    assert kwargs["id"] == "system-metrics-sample"
    assert kwargs["coalesce"] is True
    assert kwargs["replace_existing"] is True
    # Critical: must NOT pass next_run_time (feedback_apscheduler_next_run_time:
    # explicit None == paused == sampler never runs == sparkline silently empty).
    assert "next_run_time" not in kwargs
