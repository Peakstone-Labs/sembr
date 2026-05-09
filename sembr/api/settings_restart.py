"""Container restart orchestration for the settings editor.

Two responsibilities:

1. **RSSHub** restart — when the user adds/changes a passthrough variable
   (TWITTER_AUTH_TOKEN etc.) the RSSHub container must be restarted to re-read
   the bind-mounted `.env`. We drive this via `docker compose up --force-recreate`
   using the compose CLI available inside the API container (see design.md D1-D7).

2. **API self-restart** — sembr changed its own ``.env`` and needs the new
   values to land in ``os.environ`` (env_file: bakes values at container
   *creation* time; ``docker restart`` of the same container keeps the old
   env). We spawn a *detached* ``docker compose up -d --force-recreate api``
   on a 1.5s delay; compose sends SIGTERM to the running api so FastAPI's
   lifespan shutdown chain still runs cleanly, then creates a fresh
   container which re-reads the modified ``.env``. Detached so the spawned
   compose process survives our own death.

Earlier design (D8-D11) called ``os.kill(SIGTERM)`` and expected
``restart: unless-stopped`` to "rehydrate" the container with the new
``.env``. That assumption was wrong — restart of an existing container
does not re-read ``env_file``, only ``up --force-recreate`` does. Symptom
was the Settings page reporting "overridden by shell env" after every
save because pydantic-settings was reading the stale baked-in env values
that took priority over the freshly-written ``.env``.
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
API_SERVICE_NAME = "api"                # docker compose service name for self
COMPOSE_FILE_PATH = "/app/docker-compose.yml"

# Module-level flag: set to True by _spawn_self_force_recreate so the lifespan
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

    ``subprocess_runner`` is injectable for unit tests; default is
    ``subprocess.run``.
    """

    def __init__(
        self,
        subprocess_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
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
        """Spawn a detached ``docker compose up -d --force-recreate api``
        after ``delay`` seconds.

        Detached (``start_new_session=True``) so the compose process survives
        the SIGTERM compose itself sends to us. The new container reads the
        freshly-written ``.env``, replacing the stale env baked in at the
        previous container's creation time. Lifespan shutdown still runs
        cleanly because compose sends SIGTERM (not SIGKILL) and waits for
        the docker stop grace period (memory: feedback_lifespan).
        """
        loop = self._loop or asyncio.get_event_loop()
        loop.call_later(delay, _spawn_self_force_recreate)
        logger.info(
            "api self-restart scheduled in %.2fs (compose force-recreate → lifespan shutdown)",
            delay,
        )


def _spawn_self_force_recreate() -> None:
    """Spawn a detached ``docker compose up -d --force-recreate api`` and set
    the lifespan exit flag.

    Why detached: the compose command will SIGTERM us as part of its
    "stop the existing container, start a new one" flow. If we spawned
    it in our own process group, the SIGTERM that propagates would also
    kill the compose child before it gets to start the new container.
    ``start_new_session=True`` puts compose in its own session so it
    survives our death.

    The lifespan finally-block reads ``_RESTART_REQUESTED`` and calls
    ``_force_exit`` on shutdown completion to make sure docker's
    grace-period logic doesn't get to SIGKILL us mid-cleanup.
    """
    global _RESTART_REQUESTED
    _RESTART_REQUESTED = True
    cmd = [
        "docker", "compose",
        "-f", COMPOSE_FILE_PATH,
        "up", "-d", "--force-recreate", "--no-deps",
        API_SERVICE_NAME,
    ]
    logger.info("api self-restart firing now (spawning %s detached)", " ".join(cmd))
    try:
        subprocess.Popen(  # noqa: S603 — controlled argv, no shell expansion
            cmd,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:  # noqa: BLE001 — last-ditch fallback so we don't leave a dead container
        logger.exception("compose spawn failed; falling back to SIGTERM-self")
        os.kill(os.getpid(), signal.SIGTERM)
