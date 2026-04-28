"""LLM backend ABC."""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(Exception):
    """Raised by LLM backends on non-recoverable errors (non-200, timeout, bad response)."""


class BaseLLMBackend(ABC):
    @abstractmethod
    async def summarize(self, prompt: str, *, system: str | None = None) -> str:
        """Return a summary string or raise LLMError.

        `system` carries role/format rules sent as the system message; `prompt`
        carries the per-call content (intent + articles). Backends that don't
        support a system role should prepend `system` to `prompt` themselves.
        """

    @abstractmethod
    async def health(self) -> bool:
        """Return True if the backend is reachable."""

    async def aclose(self) -> None:
        """Release any held resources (e.g. httpx client). Default is a no-op."""
