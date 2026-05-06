"""OpenAI-compatible chat/completions LLM backend.

Reuses the httpx.AsyncClient pattern from SiliconFlowEmbedder (no new deps).
"""
from __future__ import annotations

import logging

import httpx

from sembr.summarizer.llm.base import BaseLLMBackend, LLMError

logger = logging.getLogger(__name__)


class APIBackend(BaseLLMBackend):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
    ) -> None:
        self._model = model
        # Held so error paths can scrub the key from any echoed response body
        # (some upstream proxies reflect Authorization headers in 401 bodies).
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

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
            safe_body = resp.text[:200]
            if self._api_key:
                safe_body = safe_body.replace(self._api_key, "***")
            raise LLMError(f"LLM API returned {resp.status_code}: {safe_body}")

        try:
            text = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {exc}") from exc

        if not isinstance(text, str) or not text.strip():
            raise LLMError("LLM returned empty or non-string content")

        return text

    async def health(self) -> bool:
        try:
            resp = await self._client.get("/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
