"""BaseChannel ABC for notification channels."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sembr.summarizer.models import SummaryResult


class BaseChannel(ABC):
    @abstractmethod
    async def send(self, result: "SummaryResult", *, to: str, intent_name: str) -> None: ...
