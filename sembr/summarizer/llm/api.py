# SPDX-License-Identifier: Apache-2.0
"""OpenAI-compatible chat/completions LLM backend.

Reuses the httpx.AsyncClient pattern from SiliconFlowEmbedder (no new deps).
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from sembr.summarizer.llm.base import BaseLLMBackend, LLMError

logger = logging.getLogger(__name__)

# Transient upstream statuses worth a backoff retry on the chat() path.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class APIBackend(BaseLLMBackend):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
        max_prompt_chars: int,
        chat_max_retries: int = 3,
    ) -> None:
        self._model = model
        self._chat_max_retries = chat_max_retries
        # Held so error paths can scrub the key from any echoed response body
        # (some upstream proxies reflect Authorization headers in 401 bodies).
        self._api_key = api_key
        self._max_prompt_chars = max_prompt_chars
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            # httpx defaults (max_connections=100, keepalive=20) would throttle
            # the source-extraction fan-out: at high reduce_concurrency the pool,
            # not the Semaphore, becomes the cap, and >20 calls pay a fresh TLS
            # handshake each. Size the pool above the reduce_concurrency ceiling
            # (256) so the Semaphore is the only limiter.
            limits=httpx.Limits(max_connections=320, max_keepalive_connections=80),
        )

    @property
    def max_prompt_chars(self) -> int:
        return self._max_prompt_chars

    async def summarize(self, prompt: str, *, system: str | None = None) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        try:
            resp = await self._client.post("/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM request timed out: {exc}") from exc
        except httpx.RequestError as exc:
            raise LLMError(f"LLM request error: {exc}") from exc

        if not resp.is_success:
            raise LLMError(f"LLM API returned {resp.status_code}: {self._scrub(resp.text)}")

        try:
            text = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {exc}") from exc

        if not isinstance(text, str) or not text.strip():
            raise LLMError("LLM returned empty or non-string content")

        return text

    async def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        json_mode: bool = False,
    ) -> str:
        """Chat round-trip with a per-call model override + optional JSON mode.

        Unlike ``summarize`` (the cron pipeline's single-attempt path, kept that
        way to preserve tick timing), ``chat`` retries 429/5xx/timeout with
        exponential backoff: it backs the interactive extraction path where a
        whole digest's worth of concurrent calls can briefly trip a provider
        rate limit, and a transient failure there should not drop an article.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, object] = {
            "model": model or self._model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_exc: Exception | None = None
        last_attempt = self._chat_max_retries - 1
        for attempt in range(self._chat_max_retries):
            try:
                resp = await self._client.post("/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last_exc = LLMError(f"LLM request error: {exc}")
                if attempt < last_attempt:  # no point sleeping before giving up
                    await asyncio.sleep(min(2**attempt, 20))
                continue

            if resp.status_code in _RETRY_STATUSES:
                last_exc = LLMError(
                    f"LLM API returned {resp.status_code}: {self._scrub(resp.text)}"
                )
                if attempt < last_attempt:
                    await asyncio.sleep(min(2**attempt, 20))
                continue
            if not resp.is_success:
                raise LLMError(f"LLM API returned {resp.status_code}: {self._scrub(resp.text)}")

            try:
                text = resp.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as exc:
                raise LLMError(f"Unexpected LLM response shape: {exc}") from exc
            if not isinstance(text, str) or not text.strip():
                raise LLMError("LLM returned empty or non-string content")
            return text

        raise LLMError(f"LLM chat failed after {self._chat_max_retries} attempts: {last_exc}")

    def _scrub(self, body: str) -> str:
        # Some upstream proxies reflect the Authorization header in error bodies;
        # never let the key leak into logs / HTTP error details. Scrub BEFORE
        # truncating — truncating first could cut the key across the 200-char
        # boundary so the replace() no longer matches and a half-key survives.
        safe = body
        if self._api_key:
            safe = safe.replace(self._api_key, "***")
        return safe[:200]

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
