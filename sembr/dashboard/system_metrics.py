# SPDX-License-Identifier: Apache-2.0
"""Background sampler for the dashboard's per-container system-metrics panel.

Three responsibilities:

1. ``SystemMetricsCollector`` — module-level rolling buffer (deque maxlen=60)
   that holds one timestamped snapshot per sample. Owned by the lifespan and
   handed in to ``build_snapshot`` as a function argument (no module-level
   singleton, no FastAPI Request coupling).
2. ``_take_docker_sample`` — synchronous helper that talks to the docker socket;
   the APScheduler job wraps it in ``asyncio.to_thread`` with a 5 s timeout so
   a slow daemon never blocks the event loop.
3. ``add_system_metrics_job`` — registers the IntervalTrigger sampler with
   ``coalesce=True`` and ``replace_existing=True`` and **never** passes
   ``next_run_time=None`` (paused-job pitfall).

Auto-discovery: containers are filtered by docker compose's
``com.docker.compose.project=<name>`` label. The project name comes
from ``COMPOSE_PROJECT_NAME`` (set by docker compose) or falls back to
``"sembr"`` (the directory name in production).

When the docker socket is unavailable (host without docker, missing socket
mount, etc.) the collector flips to ``available=False``; subsequent
``read()`` calls return ``None`` so ``/snapshot`` reports ``system_metrics:
null`` and the dashboard falls back to a "—" placeholder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sembr.dashboard.schemas import ContainerMetric, SystemMetricsBlock

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# Rolling-window length. 60 × 10s default poll = 10 min of history; sized to keep
# the JSON payload bounded at ~60 × 3 containers × 2 series.
MAXLEN_DEFAULT = 60

# Hard cap on a single sampler tick. Each `container.stats(stream=False)`
# takes ~1.5–2 s on a typical M-series macOS host against the in-VM docker
# daemon. Three containers in parallel ≈ 2–3 s wall-clock; we pad to 12 s so
# transient daemon hiccups don't churn the ``available`` flag. Stays well
# under the lifespan_shutdown_timeout default (~30 s) so a mid-tick shutdown
# still completes inside the graceful window.
SAMPLE_TIMEOUT_SECONDS = 12.0

# Compose label used to auto-discover the sembr stack's containers.
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

# Module-state guard: the silent-misconfig warning (loop 2 💡-2) fires once
# per process to avoid log spam from a permanent misconfiguration.
# Tests that exercise this branch must reset this flag in a fixture (e.g.
# ``monkeypatch.setattr(sm, "_zero_container_warned", False)``) — otherwise
# a previous test's WARNING would silently suppress the assertion.
_zero_container_warned = False


def _emit_zero_container_warning(project: str) -> None:
    """One-shot WARNING when the docker socket is reachable but no compose
    containers match the project label. Most likely cause: user ran
    ``docker compose up`` from a directory whose name isn't ``sembr`` and
    didn't set ``COMPOSE_PROJECT_NAME`` to override the default. Without
    this warning the failure is silent — collector flips to unavailable
    only on socket errors, not on empty result sets."""
    global _zero_container_warned
    if _zero_container_warned:
        return
    # Loop 3 💡-6: log BEFORE setting the guard so a logging handler that
    # raises (uncommon, but possible with custom handlers) doesn't permanently
    # mute future warnings on what's likely a real misconfig.
    logger.warning(
        "system metrics: docker reachable but 0 containers match label "
        "%s=%s — sparkline will stay empty. Set COMPOSE_PROJECT_NAME to "
        "the directory `docker compose up` was run from, or rename the "
        "directory to `sembr`.",
        _COMPOSE_PROJECT_LABEL,
        project,
    )
    _zero_container_warned = True


@dataclass
class _Sample:
    """One sampler tick's per-container snapshot."""

    sampled_at: datetime
    containers: list[ContainerMetric] = field(default_factory=list)


class SystemMetricsCollector:
    """In-memory rolling buffer of docker stats snapshots.

    Thread-affinity: ``append`` is called from the asyncio sampler job
    (event-loop thread); ``read`` is called from request handlers (also
    event-loop thread). No locking needed.
    """

    def __init__(self, *, interval_seconds: int, maxlen: int = MAXLEN_DEFAULT) -> None:
        self._interval_seconds = int(interval_seconds)
        self._points: deque[_Sample] = deque(maxlen=maxlen)
        self._available = True

    @property
    def interval_seconds(self) -> int:
        return self._interval_seconds

    @property
    def available(self) -> bool:
        return self._available

    def mark_unavailable(self) -> None:
        """Flip to unavailable so ``read()`` short-circuits to ``None``.

        Called when ``_take_docker_sample`` catches a ``DockerException`` —
        typically docker socket missing or unreachable. ``read()`` can re-flip
        on the next successful sample.
        """
        if self._available:
            logger.warning("system metrics: docker unavailable, sparkline will be null")
        self._available = False

    def mark_available(self) -> None:
        if not self._available:
            logger.info("system metrics: docker available again")
        self._available = True

    def append(self, sample: _Sample) -> None:
        self._points.append(sample)

    def read(self) -> SystemMetricsBlock | None:
        """Snapshot block for the current ``/snapshot`` response.

        Returns ``None`` when the docker socket is unavailable or no sample
        has been collected yet (first ``pollInterval`` after lifespan startup).
        """
        if not self._available:
            return None
        if not self._points:
            return None

        latest = self._points[-1]

        # Build per-container sparkline series from the rolling buffer.
        # The deque holds raw _Sample objects; we collapse to per-container
        # lists keyed by container name. A container that disappears between
        # samples gets None for the missing slots so the series length always
        # equals len(self._points) — frontend draws aligned x-axes that way.
        names: list[str] = []
        seen: set[str] = set()
        for snap in self._points:
            for cm in snap.containers:
                if cm.name not in seen:
                    seen.add(cm.name)
                    names.append(cm.name)

        cpu_series: dict[str, list[float | None]] = {n: [] for n in names}
        mem_series: dict[str, list[int | None]] = {n: [] for n in names}
        for snap in self._points:
            by_name = {cm.name: cm for cm in snap.containers}
            for n in names:
                cm = by_name.get(n)
                cpu_series[n].append(cm.cpu_percent if cm else None)
                mem_series[n].append(cm.mem_used_bytes if cm else None)

        latest_by_name = {cm.name: cm for cm in latest.containers}
        out_containers = []
        for n in names:
            cm = latest_by_name.get(n)
            out_containers.append(
                ContainerMetric(
                    name=n,
                    uptime_seconds=cm.uptime_seconds if cm else None,
                    cpu_percent=cm.cpu_percent if cm else None,
                    mem_used_bytes=cm.mem_used_bytes if cm else None,
                    mem_limit_bytes=cm.mem_limit_bytes if cm else None,
                    cpu_history=cpu_series[n],
                    mem_history=mem_series[n],
                )
            )

        return SystemMetricsBlock(
            sampled_at=latest.sampled_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            interval_seconds=self._interval_seconds,
            containers=out_containers,
        )


# ── docker stats helpers ──────────────────────────────────────────────────────


def _compute_cpu_percent(stats: dict) -> float | None:
    """Standard docker stats CPU% formula.

    ``(cpu_delta / sys_delta) * online_cpus * 100``

    Returns ``None`` when:
    - ``precpu_stats`` is missing or zero (very first sample after the daemon
      starts tracking the container — there is no baseline yet)
    - ``system_cpu_usage`` is zero (some kernels / cgroup v2 hosts that
      docker hasn't populated yet)
    - ``cpu_delta < 0`` (counter rollover or clock skew — treat as missing
      data rather than emit a misleading negative percentage; an idle
      container with ``cpu_delta == 0`` correctly returns ``0.0``)

    A None propagates through to ``ContainerMetric.cpu_percent`` and the UI
    renders "—" instead of a misleading 0%.
    """
    try:
        cpu_stats = stats.get("cpu_stats") or {}
        precpu_stats = stats.get("precpu_stats") or {}
        cpu_usage = (cpu_stats.get("cpu_usage") or {}).get("total_usage")
        precpu_usage = (precpu_stats.get("cpu_usage") or {}).get("total_usage")
        sys_now = cpu_stats.get("system_cpu_usage")
        sys_pre = precpu_stats.get("system_cpu_usage")
        if cpu_usage is None or precpu_usage is None:
            return None
        if not sys_now or not sys_pre:
            return None
        cpu_delta = cpu_usage - precpu_usage
        sys_delta = sys_now - sys_pre
        if sys_delta <= 0 or cpu_delta < 0:
            return None
        online = cpu_stats.get("online_cpus")
        if not online:
            percpu = (cpu_stats.get("cpu_usage") or {}).get("percpu_usage")
            online = len(percpu) if percpu else 1
        return round((cpu_delta / sys_delta) * online * 100.0, 2)
    except Exception:
        return None


def _parse_started_at(raw: str | None) -> datetime | None:
    """Parse docker's RFC 3339 ``started_at`` ("2026-05-08T12:34:56.789Z")."""
    if not raw:
        return None
    try:
        # docker's nanosecond suffix breaks fromisoformat; trim to microseconds.
        s = raw.replace("Z", "+00:00")
        if "." in s and "+" in s:
            head, _, tail = s.partition(".")
            frac, _, tz = tail.partition("+")
            frac = frac[:6]  # truncate to microseconds
            s = f"{head}.{frac}+{tz}"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _project_name() -> str:
    return os.environ.get("COMPOSE_PROJECT_NAME") or "sembr"


def _take_docker_sample(*, project: str | None = None) -> _Sample | None:
    """Synchronous one-shot sample. Returns ``None`` if docker is unavailable.

    Caller must wrap in ``asyncio.to_thread`` + ``asyncio.wait_for`` — the
    docker SDK is blocking and a slow daemon cannot stall the event loop.
    """
    import docker  # noqa: PLC0415  — keep import lazy: dev box may lack it
    from docker.errors import DockerException  # noqa: PLC0415

    project = project or _project_name()
    try:
        client = docker.from_env()
    except DockerException as exc:
        logger.warning("system metrics: docker.from_env() failed: %s", exc)
        return None

    try:
        containers = client.containers.list(
            filters={"label": f"{_COMPOSE_PROJECT_LABEL}={project}"}
        )
    except DockerException as exc:
        logger.warning("system metrics: containers.list failed: %s", exc)
        with contextlib.suppress(Exception):
            client.close()
        return None

    # Loop 2 💡-2: docker socket reachable but no compose containers matched
    # the project label = silent misconfig (e.g. user renamed repo dir without
    # setting COMPOSE_PROJECT_NAME). Emit a single WARNING per process so
    # operators can spot it. _emit_zero_container_warning is module-state so
    # the warning fires only on the first such sample.
    if not containers:
        _emit_zero_container_warning(project)

    now = datetime.now(UTC)

    def _measure(container: object) -> ContainerMetric:
        """Fetch one container's snapshot. Caller runs us in a thread pool so
        per-container ``stats(stream=False)`` blocking I/O happens in parallel —
        sequential calls cost ≈ N × 2 s per container and consistently trip the
        sampler timeout on typical M-series macOS docker daemons."""
        name = container.name or container.id[:12]
        cpu_percent: float | None = None
        mem_used: int | None = None
        mem_limit: int | None = None
        uptime: int | None = None
        try:
            stats = container.stats(stream=False)
        except DockerException as exc:
            logger.warning("system metrics: stats() failed for %s: %s", name, exc)
            stats = None

        if stats:
            cpu_percent = _compute_cpu_percent(stats)
            mem_stats = stats.get("memory_stats") or {}
            mem_used = mem_stats.get("usage")
            mem_limit = mem_stats.get("limit")
            if isinstance(mem_used, int) and isinstance(mem_limit, int):
                # cgroup v1 reports cache inside usage; subtract if the daemon
                # exposes it so the displayed number lines up with `docker stats`.
                # cgroup v2 hosts (Linux ≥ 5.x without `systemd.unified_cgroup_
                # hierarchy=0`) don't populate `inactive_file` here at all, so
                # the subtraction is a no-op and the reported memory will be a
                # few MB higher than `docker stats --no-stream`'s output.
                # Documented limitation; not worth a separate cgroup-v2
                # codepath until acceptance criterion #8 fails on a v2 host.
                inactive = (mem_stats.get("stats") or {}).get("inactive_file")
                if isinstance(inactive, int) and 0 <= inactive <= mem_used:
                    mem_used = mem_used - inactive

        try:
            attrs = container.attrs or {}
            started_at = _parse_started_at((attrs.get("State") or {}).get("StartedAt"))
            if started_at is not None:
                uptime = max(0, int((now - started_at).total_seconds()))
        except Exception:
            uptime = None

        return ContainerMetric(
            name=name,
            uptime_seconds=uptime,
            cpu_percent=cpu_percent,
            mem_used_bytes=mem_used,
            mem_limit_bytes=mem_limit,
        )

    # Parallel fan-out across containers. Worker count == container count
    # (typically 3); the SDK's HTTPConnection-per-call model is thread-safe
    # for the read paths we use here.
    out: list[ContainerMetric] = []
    if containers:
        with ThreadPoolExecutor(max_workers=len(containers)) as pool:
            futures = [pool.submit(_measure, c) for c in containers]
            for fut in as_completed(futures):
                try:
                    out.append(fut.result())
                except Exception as exc:  # noqa: BLE001 — protect the fan-out from one bad container
                    logger.warning("system metrics: per-container measure failed: %s", exc)

    with contextlib.suppress(Exception):
        client.close()

    return _Sample(sampled_at=now, containers=sorted(out, key=lambda c: c.name))


# ── APScheduler integration ───────────────────────────────────────────────────


async def _run_sampler(collector: SystemMetricsCollector) -> None:
    """One sampler tick. Caller is APScheduler.

    Wraps the blocking docker call in a thread + 5 s timeout. On failure
    the collector is flipped to unavailable; on success it is flipped back
    so a transient daemon hiccup doesn't permanently mute the panel.
    """
    started = time.monotonic()
    try:
        sample = await asyncio.wait_for(
            asyncio.to_thread(_take_docker_sample),
            timeout=SAMPLE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning("system metrics: sample timed out after %ss", SAMPLE_TIMEOUT_SECONDS)
        collector.mark_unavailable()
        return
    except Exception as exc:  # noqa: BLE001 — never raise out of a scheduler job
        logger.warning("system metrics: sampler failed: %s", exc, exc_info=True)
        collector.mark_unavailable()
        return

    if sample is None:
        collector.mark_unavailable()
        return

    collector.mark_available()
    collector.append(sample)
    logger.debug(
        "system metrics: sampled %d container(s) in %.2fs",
        len(sample.containers),
        time.monotonic() - started,
    )


def add_system_metrics_job(
    scheduler: AsyncIOScheduler,
    collector: SystemMetricsCollector,
    interval_seconds: int,
) -> None:
    """Register the sampler IntervalTrigger job.

    Hard-coded job kwargs (per project convention asserted across main.py:179-200):

    - ``coalesce=True`` so a paused → resumed scheduler doesn't fire a backlog
      of ticks at once that would slam the docker socket.
    - ``replace_existing=True`` so ``uvicorn --reload`` re-imports don't raise
      ``ConflictingIdError``.

    **Never** pass ``next_run_time=None`` here: APScheduler treats that as a
    pause sentinel and the sampler would silently never run, leaving the
    sparkline permanently empty (memory: feedback_apscheduler_next_run_time).
    """
    from apscheduler.triggers.interval import IntervalTrigger  # noqa: PLC0415

    async def _tick() -> None:
        await _run_sampler(collector)

    scheduler.add_job(
        _tick,
        trigger=IntervalTrigger(seconds=int(interval_seconds)),
        id="system-metrics-sample",
        coalesce=True,
        replace_existing=True,
    )
    logger.info(
        "system metrics: sampler scheduled (interval=%ss, project=%s)",
        interval_seconds,
        _project_name(),
    )
