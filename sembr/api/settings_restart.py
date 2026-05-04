"""Container restart orchestration for the settings editor.

Two responsibilities:

1. **RSSHub** restart — when the user adds/changes a passthrough variable
   (TWITTER_COOKIE etc.) the RSSHub container must be restarted to re-read
   the bind-mounted `.env`. We drive this through the docker socket using
   the official ``docker`` SDK (see design.md O3a).

2. **API self-restart** — sembr changed its own ``.env`` and needs
   ``get_settings()``'s ``lru_cache`` cleared. The simplest correct path is
   to let the FastAPI lifespan run its full shutdown chain and rely on
   ``restart: unless-stopped`` to bring us back up. We schedule a delayed
   ``SIGTERM`` so the response that triggered the restart can be flushed
   to the browser first (design.md O2a / Decision #2).

Why not ``containers.get("sembr-api").restart()`` from inside ourselves:
that bypasses lifespan shutdown — APScheduler / Qdrant / sqlite cleanup
gets SIGKILL'd at the 10s docker stop timeout, defeating the careful
ordering enforced in ``sembr.main.lifespan`` (memory: feedback_lifespan).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_SELF_SHUTDOWN_DELAY = 1.5  # seconds — long enough for an HTTP response to flush
RSSHUB_CONTAINER_NAME = "sembr-rsshub"


class _DockerClientLike(Protocol):
    """Subset of the docker SDK we actually use; lets tests inject a fake."""
    def containers(self): ...  # noqa: D401, E704


class RestartController:
    """Stateless orchestrator. Methods are async-friendly but pure.

    The class is *not* a singleton — instantiated per request inside the
    settings router. Holds no resources between calls; the docker client is
    created lazily so unit tests can inject a fake without touching
    `/var/run/docker.sock`.
    """

    def __init__(
        self,
        docker_client_factory=None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        # ``docker_client_factory`` is a 0-arg callable returning the docker
        # SDK client (or any ``DockerClientLike``). Default delays the import
        # so the ``docker`` package is only required when restart is
        # actually invoked — keeps unit tests of unrelated paths
        # dependency-free.
        self._client_factory = docker_client_factory or _default_docker_client_factory
        self._loop = loop

    # ── RSSHub ────────────────────────────────────────────────────────────

    async def restart_rsshub(self, container_name: str = RSSHUB_CONTAINER_NAME) -> None:
        """Restart the named container. Errors propagate (router maps to 500)."""
        # Run the SDK call in a thread — docker-py is synchronous and would
        # otherwise block the event loop.
        await asyncio.to_thread(self._restart_rsshub_sync, container_name)

    def _restart_rsshub_sync(self, container_name: str) -> None:
        client = self._client_factory()
        try:
            container = client.containers.get(container_name)
        except Exception as exc:  # pragma: no cover - sdk-specific exception types
            raise RuntimeError(
                f"docker container {container_name!r} not found: {exc}"
            ) from exc
        container.restart()
        logger.info("rsshub container %s restarted", container_name)

    # ── API self-restart ──────────────────────────────────────────────────

    def schedule_self_restart(self, delay: float = DEFAULT_SELF_SHUTDOWN_DELAY) -> None:
        """Send ourselves SIGTERM after ``delay`` seconds.

        SIGTERM triggers the FastAPI lifespan shutdown chain
        (scheduler.shutdown, embedder.aclose, qdrant.close, close_sqlite —
        memory: feedback_lifespan); ``restart: unless-stopped`` then has
        docker rehydrate the container, which re-reads the (now-modified)
        bind-mounted ``.env`` and rebuilds Settings.
        """
        loop = self._loop or asyncio.get_event_loop()
        loop.call_later(delay, _send_sigterm_to_self)
        logger.info("api self-restart scheduled in %.2fs (SIGTERM → lifespan shutdown)", delay)


def _send_sigterm_to_self() -> None:
    # Separate function so tests can monkeypatch it without owning the
    # full call_later scheduling machinery.
    logger.info("api self-restart firing now (SIGTERM to PID %d)", os.getpid())
    os.kill(os.getpid(), signal.SIGTERM)


def _default_docker_client_factory():
    # Lazy import keeps `docker` out of the import-time graph; the dep is
    # only required when settings actually trigger a restart.
    import docker  # noqa: PLC0415
    return docker.from_env()
