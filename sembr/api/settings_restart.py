# SPDX-License-Identifier: Apache-2.0
"""Container restart orchestration for the settings editor.

Two responsibilities:

1. **RSSHub** restart — when the user adds/changes a passthrough variable
   (TWITTER_AUTH_TOKEN etc.) the RSSHub container must be restarted to re-read
   the bind-mounted `.env`. We drive this via `docker compose up --force-recreate`
   using the compose CLI available inside the API container.

2. **API self-restart** — sembr changed its own ``.env`` and needs the new
   values to land in ``os.environ`` (env_file: bakes values at container
   *creation* time; ``docker restart`` of the same container keeps the old
   env). We spawn a **helper container** (reusing our own image) that runs
   ``docker compose up -d --force-recreate --no-deps api`` against the
   host daemon via the bind-mounted docker socket. The helper runs in
   its own container namespace, so when the daemon stops the api
   container as part of the recreate, the helper is unaffected and
   proceeds to create+start the new api container.

Earlier attempts called ``os.kill(SIGTERM)`` (broke env_file reload — restart
of an existing container does not re-read env_file, only ``up --force-recreate``
does) and then spawned a *detached* compose CLI inside our own container
(broke because the daemon's container-stop step SIGKILL'd our entire pid
namespace including the in-flight compose CLI between its stop+create
steps, leaving a Created-but-never-started new container).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)

DEFAULT_SELF_SHUTDOWN_DELAY = 1.5  # seconds — long enough for an HTTP response to flush
RSSHUB_SERVICE_NAME = "rsshub"  # docker compose service name (for `compose up`)
API_SERVICE_NAME = "api"  # docker compose service name for self
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
        NOT the container name (``sembr-rsshub``). Runs in a thread so the
        synchronous subprocess call does not block the event loop. Errors
        propagate as RuntimeError; the router maps that to 200 + warning so the
        user-facing settings save still reports success even if the optional
        side-channel restart fails.
        """
        await asyncio.to_thread(self._restart_rsshub_sync, service_name)

    def _restart_rsshub_sync(self, service_name: str) -> None:
        cmd = [
            "docker",
            "compose",
            "-f",
            COMPOSE_FILE_PATH,
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            service_name,
        ]
        try:
            result = self._subprocess_runner(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"docker compose recreate timed out after 60s: {exc}") from exc
        if result.returncode != 0:
            raise RuntimeError(f"docker compose recreate failed: {result.stderr.strip()}")
        logger.info("rsshub service %s recreated via compose", service_name)

    # ── API self-restart ──────────────────────────────────────────────────

    def schedule_self_restart(self, delay: float = DEFAULT_SELF_SHUTDOWN_DELAY) -> None:
        """Schedule a helper-container-driven force-recreate of self after
        ``delay`` seconds.

        See module docstring for why we can't simply spawn compose inside
        our own container's pid namespace. ``delay`` is the time the
        HTTP response that triggered this needs to flush before the
        helper container's compose actually stops us.
        """
        loop = self._loop or asyncio.get_event_loop()
        loop.call_later(delay, _spawn_self_force_recreate)
        logger.info(
            "api self-restart scheduled in %.2fs (helper container → compose force-recreate)",
            delay,
        )


def _self_compose_context() -> tuple[str, str, str]:
    """Return (host_working_dir, project_name, image_tag) for our own
    container by inspecting it via the bind-mounted docker socket.

    Used to drive the helper container's bind mounts and the compose
    invocation; we can't hardcode the host paths because the user's
    install location varies.
    """
    short_id = socket.gethostname()
    fmt = (
        '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
        '\t{{index .Config.Labels "com.docker.compose.project"}}'
        "\t{{.Config.Image}}"
    )
    result = subprocess.run(
        ["docker", "inspect", "--format", fmt, short_id],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    parts = result.stdout.strip().split("\t")
    if len(parts) != 3 or not all(parts):
        raise RuntimeError(f"could not introspect compose context from labels: {result.stdout!r}")
    return parts[0], parts[1], parts[2]


def _spawn_self_force_recreate() -> None:
    """Launch a helper container that runs the compose force-recreate.

    Helper inherits our docker socket + image (so docker CLI + compose
    plugin are available) and bind-mounts the host's compose project dir
    read-only at /project. After a 2s sleep — long enough for any
    in-flight HTTP response from the api to drain — it runs
    ``docker compose --project-name <ours> up -d --force-recreate
    --no-deps api`` and exits. ``--rm`` cleans up.

    Falls back to SIGTERM-self if introspection or spawn fails — keeps
    the api process from being permanently wedged with stale env when
    the helper path is unavailable, at the cost of NOT picking up the
    .env changes (user must re-run docker compose by hand).
    """
    global _RESTART_REQUESTED
    _RESTART_REQUESTED = True
    try:
        host_wd, project_name, image = _self_compose_context()
    except Exception:  # noqa: BLE001
        logger.exception("self-recreate introspection failed; falling back to SIGTERM-self")
        os.kill(os.getpid(), signal.SIGTERM)
        return

    # Mount the host project dir at the SAME path inside the helper so
    # compose's relative volume paths (./data, ./.env, etc. in the api
    # service block) resolve to identical strings on both sides. The
    # daemon validates host paths against Docker Desktop's file-sharing
    # allow-list; if compose sends "./data" resolved against the helper-
    # internal mount point (e.g. /project/data), the daemon rejects it
    # ("path is not shared from the host"). Mounting at host_wd keeps the
    # path string identical, so the daemon accepts it. Read-only because
    # compose only reads the YAML + .env from this mount.
    helper_inner = (
        "sleep 2 && "
        f"cd {host_wd} && "
        f"docker compose --project-name {project_name} "
        f"up -d --force-recreate --no-deps {API_SERVICE_NAME}"
    )
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        f"sembr-api-recreate-{os.getpid()}",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{host_wd}:{host_wd}:ro",
        "--entrypoint",
        "sh",
        image,
        "-c",
        helper_inner,
    ]
    logger.info(
        "api self-restart firing now (helper container; project=%s host_wd=%s image=%s)",
        project_name,
        host_wd,
        image,
    )
    try:
        subprocess.Popen(  # noqa: S603 — controlled argv, no shell expansion
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("helper container spawn failed; falling back to SIGTERM-self")
        os.kill(os.getpid(), signal.SIGTERM)
