"""Install the RingBufferHandler on the root logger at lifespan start."""
from __future__ import annotations

import asyncio
import logging

from sembr.logbus.bus import get_bus
from sembr.logbus.handler import RingBufferHandler
from sembr.logbus.router import ALL_TAGS, THIRD_PARTY_LOGGERS_BY_TAG


def install_logbus(
    loop: asyncio.AbstractEventLoop,
    *,
    buffer_per_tag: int = 1000,
    default_level: int = logging.INFO,
) -> None:
    """Attach RingBufferHandler to the root logger and configure the bus.

    Must be called from within a running asyncio event loop (i.e. the first
    line of the FastAPI lifespan coroutine) so ``loop`` is the real event loop.

    Args:
        loop: The running event loop; stored on LogBus for call_soon_threadsafe.
        buffer_per_tag: Ring buffer capacity per tag (from Settings).
        default_level: Default tag level applied to all 7 tags (from Settings).
    """
    bus = get_bus()
    bus.set_loop(loop)
    bus.set_buffer_size(buffer_per_tag)

    # Apply default level to all tags.
    for tag in ALL_TAGS:
        bus.set_tag_level(tag, default_level)

    # Suppress noisy third-party loggers at WARNING by default; the dashboard's
    # PUT /level endpoint can relax them by reading the same map.
    for names in THIRD_PARTY_LOGGERS_BY_TAG.values():
        for name in names:
            logging.getLogger(name).setLevel(logging.WARNING)

    # Attach handler to root logger; guard against double-add when
    # install_logbus() runs more than once in the same process (tests).
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, RingBufferHandler):
            return  # already installed

    # Pin existing StreamHandlers (installed by basicConfig) at INFO before we
    # lower root to DEBUG, so docker logs / stderr are not flooded with
    # third-party DEBUG records.
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and h.level == logging.NOTSET:
            h.setLevel(logging.INFO)

    handler = RingBufferHandler()
    root.addHandler(handler)
    # Lower root so DEBUG records can reach our handler (per-tag level filtering
    # happens inside LogBus.emit — the stream handler above is now pinned at INFO).
    root.setLevel(logging.DEBUG)
