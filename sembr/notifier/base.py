"""Marker ABC for notification channels.

Each concrete channel module owns its own Pydantic config type
(e.g. EmailChannelConfig) and its own send() signature with that
config — channel-specific params shouldn't leak into a common ABC.

The dispatcher in main.py routes by `isinstance(config, SomeConfig)`.
"""
from __future__ import annotations

from abc import ABC


class BaseChannel(ABC):
    """Marker base. Concrete channels define their own send() signature."""
