"""Container restart orchestration for the settings editor.

Two responsibilities:

1. **RSSHub** restart — when the user adds/changes a passthrough variable
   (TWITTER_COOKIE etc.) the RSSHub container must be restarted to re-read
   the bind-mounted `.env`. We drive this via `docker compose up --force-recreate`
   using the compose CLI available inside the API container (see design.md D1-D7).

2. **API self-restart** — sembr changed its own ``.env`` and needs
   ``get_settings()``'s ``lru_cache`` cleared. The simplest correct path is
   to let the FastAPI lifespan run its full shutdown chain and rely on
   ``restart: unless-stopped`` to bring us back up. We schedule a delayed
   ``SIGTERM`` so the response that triggered the restart can be flushed
   to the browser first (design.md D8-D11).

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
import subprocess
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_SELF_SHUTDOWN_DELAY = 1.5  # seconds — long enough for an HTTP response to flush
RSSHUB_SERVICE_NAME = "rsshub"          # docker compose service name (for `compose up`)
RSSHUB_CONTAINER_NAME = "sembr-rsshub"  # legacy docker SDK name; kept for callers that need it
COMPOSE_FILE_PATH = "/app/docker-compose.yml"

# Module-level flag: set to True by _send_sigterm_to_self so the lifespan
# finally block knows to call _force_exit after graceful shutdown completes.
# Never True in TestClient paths (signal never sent → flag never set).
_RESTART_REQUESTED: bool = False


def is_restart_requested() -> bool:
    """Return True if a self-restart has been scheduled."""
    return _RESTART_REQUESTED


def _force_exit(code: int) -> None:
    """Thin wrapper around os._exit so tests can monkeypatch it."""
    os._exit(code)


class RestartController:
    """Stateless orchestrator. Methods are async-friendly but pure.

    The class is *not* a singleton — instantiated per request inside the
    settings router. Holds no resources between calls.

    ``subprocess_runner`` is injectable for unit tests (design.md D7);
    default is ``subprocess.run``.

    ``docker_client_factory`` is **currently unused** — the SDK restart path
    was replaced by subprocess compose CLI (Phase 1). Retained per design.md
    D12 in case a future caller needs direct SDK access; remove in a follow-up
    PR if that never materialises.
    """

    def __init__(
        self,
        docker_client_factory=None,
        subprocess_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._client_factory = docker_client_factory or _default_docker_client_factory  # unused; see D12
        self._subprocess_runner = subprocess_runner or subprocess.run
        self._loop = loop

    # ── RSSHub ────────────────────────────────────────────────────────────

    async def restart_rsshub(self, service_name: str = RSSHUB_SERVICE_NAME) -> None:
        """Force-recreate the named compose service.

        ``service_name`` is the docker-compose service name (e.g. ``rsshub``),
        NOT the container name (``sembr-rsshub``).  Runs in a thread so the
        synchronous subprocess call does not block the event loop (design.md R6).
        Errors propagate as RuntimeError (router maps to 200+warning per D5).
        """
        await asyncio.to_thread(self._restart_rsshub_sync, service_name)

    def _restart_rsshub_sync(self, service_name: str) -> None:
        cmd = [
            "docker", "compose",
            "-f", COMPOSE_FILE_PATH,
            "up", "-d", "--force-recreate", "--no-deps",
            service_name,
        ]
        try:
            result = self._subprocess_runner(
                cmd, capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"docker compose recreate timed out after 60s: {exc}"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"docker compose recreate failed: {result.stderr.strip()}"
            )
        logger.info("rsshub service %s recreated via compose", service_name)

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
    #
    # Set the flag BEFORE sending the signal: lifespan finally checks it
    # and calls _force_exit only when True, ensuring the process actually
    # exits so Docker restart: unless-stopped can bring it back up.
    global _RESTART_REQUESTED
    _RESTART_REQUESTED = True
    logger.info("api self-restart firing now (SIGTERM to PID %d)", os.getpid())
    os.kill(os.getpid(), signal.SIGTERM)


def _default_docker_client_factory():
    # Currently unreachable: _client_factory is never called after the SDK
    # restart path was replaced by subprocess compose CLI (Phase 1 / D12).
    # Kept so docker_client_factory= injection point stays wire-compatible
    # with any future SDK callers.  Remove together with that parameter if
    # docker-py is dropped as a dependency.
    import docker  # noqa: PLC0415
    return docker.from_env()
