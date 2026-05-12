"""Maps logging record names to dashboard UI tags."""

from __future__ import annotations

import logging

# Source of truth for prefix → tag routing. Order is NOT load-bearing — `route()`
# walks this list sorted by prefix length descending so the longest match always
# wins regardless of how entries are written below.
TAG_PREFIX_MAP: list[tuple[str, str]] = [
    ("sembr.collector", "collector"),
    ("sembr.embedder", "embedder"),
    ("sembr.matcher", "matcher"),
    ("sembr.summarizer", "matcher"),
    ("sembr.notifier", "notifier"),
    ("sembr.api", "api"),
    ("sembr.dashboard", "api"),
    ("sembr.db", "api"),
    ("sembr.vector_store", "embedder"),
    ("apscheduler", "scheduler"),
    ("uvicorn.access", "http"),
    ("uvicorn.error", "api"),
    ("uvicorn", "api"),
    ("httpx", "http"),
    ("httpcore", "http"),
    ("sembr", "api"),
]

_TAG_PREFIX_MAP_SORTED: tuple[tuple[str, str], ...] = tuple(
    sorted(TAG_PREFIX_MAP, key=lambda e: -len(e[0]))
)

_DEFAULT_TAG = "api"

ALL_TAGS: tuple[str, ...] = (
    "collector",
    "embedder",
    "matcher",
    "notifier",
    "api",
    "scheduler",
    "http",
)

# Single source of truth for third-party stdlib loggers whose level must follow
# a UI tag. Consumed by `install_logbus` (initial silencing at WARNING) and by
# `dashboard.logs_routes.put_level` (sync to current UI level on change).
THIRD_PARTY_LOGGERS_BY_TAG: dict[str, tuple[str, ...]] = {
    "http": ("httpx", "httpcore", "uvicorn.access"),
}


def route(record: logging.LogRecord) -> str:
    """Return the UI tag for *record*, falling back to ``"api"``."""
    name = record.name
    for prefix, tag in _TAG_PREFIX_MAP_SORTED:
        if name == prefix or name.startswith(prefix + "."):
            return tag
    return _DEFAULT_TAG
