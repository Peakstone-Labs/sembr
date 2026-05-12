# SPDX-License-Identifier: Apache-2.0
"""RingBufferHandler — logging.Handler that feeds LogBus."""

from __future__ import annotations

import logging
from typing import Any

from sembr.logbus.bus import get_bus
from sembr.logbus.router import route


class RingBufferHandler(logging.Handler):
    """Append every record to the LogBus ring buffer (after tag routing).

    Level is always set to DEBUG so all records reach us; per-tag level
    filtering happens inside LogBus.emit().
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tag = route(record)
            exc_text: str | None = None
            if record.exc_info:
                exc_text = self.formatException(record.exc_info)
            entry: dict[str, Any] = {
                "ts": int(record.created * 1000),  # ms epoch
                "level": record.levelname,
                "level_no": record.levelno,
                "logger": record.name,
                "tag": tag,
                "message": record.getMessage(),
                "exc": exc_text,
            }
            get_bus().emit(tag, entry)
        except Exception:
            self.handleError(record)
