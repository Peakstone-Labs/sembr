# SPDX-License-Identifier: Apache-2.0
"""LLM backend ABC."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised by LLM backends on non-recoverable errors (non-200, timeout, bad response)."""


# Strip a leading ```json / ``` fence and a trailing ``` so json-mode replies that
# still wrap their object in a code fence parse cleanly.
_JSON_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def extract_json(text: str) -> str:
    """Best-effort isolate a JSON object from an LLM reply.

    json-mode usually returns a bare object, but some providers still wrap it in
    a markdown fence or prepend a sentence. Strategy: strip fences; if the result
    is already brace-delimited keep it; otherwise slice the first ``{`` .. last
    ``}``. Validation (not this function) decides whether the slice is valid —
    this only widens what ``structured`` will attempt to parse.
    """
    stripped = _JSON_FENCE_RE.sub("", text.strip())
    if stripped.startswith("{") and stripped.rstrip().endswith("}"):
        return stripped
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return stripped


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
    async def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        json_mode: bool = False,
    ) -> str:
        """Single chat/completions round-trip; return the assistant text or raise LLMError.

        Distinct from ``summarize`` (which the cron pipeline depends on with a
        fixed model and prose output): ``chat`` exposes per-call ``model``
        override (map-reduce extraction runs on ``reduce_model``, not
        ``llm_model``) and ``json_mode`` (``response_format: json_object``) for
        the structured-extraction path. Backends without a system role must
        prepend ``system`` themselves.
        """

    async def structured(
        self,
        prompt: str,
        schema: type[T],
        *,
        system: str | None = None,
        model: str | None = None,
        repair_attempts: int = 2,
    ) -> T:
        """Call ``chat`` in JSON mode and validate the reply against *schema*.

        On a ``ValidationError`` the model's own error text is fed back with a
        "fix it" instruction (cheap repair loop, ``repair_attempts`` extra tries)
        — the probe (``probe/common.py:structured``) found this stabilises
        SiliconFlow's json_object output without needing ``json_schema`` support.
        Concrete on the ABC because it is provider-agnostic: it only needs
        ``chat``. Raises ``LLMError`` if every attempt fails validation.
        """
        cur_prompt = prompt
        last_err = ""
        for _ in range(repair_attempts + 1):
            raw = await self.chat(cur_prompt, system=system, model=model, json_mode=True)
            try:
                return schema.model_validate_json(extract_json(raw))
            except (ValidationError, ValueError) as exc:
                last_err = str(exc)[:1500]
                cur_prompt = (
                    f"{prompt}\n\n---\n你上次的输出无法通过校验，错误如下：\n{last_err}\n"
                    "请只输出修正后的、严格符合 schema 的 JSON，不要任何解释。"
                )
        raise LLMError(f"structured() failed schema validation after repair: {last_err}")

    @abstractmethod
    async def health(self) -> bool:
        """Return True if the backend is reachable."""

    async def aclose(self) -> None:  # noqa: B027 (no-op default — concrete backends override)
        """Release any held resources (e.g. httpx client). Default is a no-op."""
