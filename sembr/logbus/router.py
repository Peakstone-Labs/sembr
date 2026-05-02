"""Maps logging record names to dashboard UI tags."""
from __future__ import annotations

import logging

TAG_PREFIX_MAP: list[tuple[str, str]] = [
    ("sembr.collector",   "collector"),
    ("sembr.embedder",    "embedder"),
    ("sembr.matcher",     "matcher"),
    ("sembr.summarizer",  "matcher"),
    ("sembr.notifier",    "notifier"),
    ("sembr.api",         "api"),
    ("sembr.dashboard",   "api"),
    ("sembr.db",          "api"),
    ("sembr.vector_store","embedder"),
    ("apscheduler",       "scheduler"),
    ("uvicorn.access",    "http"),
    ("uvicorn.error",     "api"),
    ("uvicorn",           "api"),
    ("httpx",             "http"),
    ("httpcore",          "http"),
    ("sembr",             "api"),
]

_DEFAULT_TAG = "api"

ALL_TAGS: tuple[str, ...] = (
    "collector", "embedder", "matcher", "notifier", "api", "scheduler", "http"
)


def route(record: logging.LogRecord) -> str:
    """Return the UI tag for *record*, falling back to ``"api"``."""
    name = record.name
    for prefix, tag in TAG_PREFIX_MAP:
        if name == prefix or name.startswith(prefix + "."):
            return tag
    return _DEFAULT_TAG
