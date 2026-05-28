# SPDX-License-Identifier: Apache-2.0
"""LLM backend ABC."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(Exception):
    """Raised by LLM backends on non-recoverable errors (non-200, timeout, bad response)."""


class BaseLLMBackend(ABC):
    @property
    @abstractmethod
    def max_prompt_chars(self) -> int:
        """Total prompt-side character budget the backend can accept.

        Counts every character that goes into the request: system prompt +
        instruction template + the rendered articles block. The pipeline subtracts
        a safety reserve for the LLM's response and for instruction overhead and
        water-fills article bodies so the assembled prompt never exceeds this.

        Tied to the backend model's context window, not to a generic input cap —
        a backend that fronts multiple models must publish the budget for the
        actually-configured one. Charactes (not tokens) because the pipeline
        operates on strings; tokens-per-character varies by language and
        tokenizer, so callers should set this conservatively.
        """

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

    async def aclose(self) -> None:  # noqa: B027 (no-op default — concrete backends override)
        """Release any held resources (e.g. httpx client). Default is a no-op."""
