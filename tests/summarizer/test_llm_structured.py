# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the map-reduce LLM additions:

- ``Settings.reduce_model`` / ``meta_extraction_model`` + ``effective_reduce_model`` fallback
- ``APIBackend.chat`` (model override, json_mode, transient retry, key scrub, empty guard)
- ``BaseLLMBackend.structured`` (validate, repair-on-failure, give-up) + ``extract_json``
"""

from __future__ import annotations

import httpx
import pytest
import respx
from pydantic import BaseModel

from sembr.config import Settings
from sembr.summarizer.llm.api import APIBackend
from sembr.summarizer.llm.base import BaseLLMBackend, LLMError, extract_json

_BASE = "https://api.test/v1"
_URL = f"{_BASE}/chat/completions"


def _backend(**kw) -> APIBackend:
    opts = {
        "base_url": _BASE,
        "api_key": "secret-key-123",
        "model": "default-model",
        "timeout": 5.0,
        "max_prompt_chars": 10_000,
    }
    opts.update(kw)
    return APIBackend(**opts)


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_reduce_model_fields_default_empty():
    s = Settings(_env_file=None, llm_model="base", reduce_model="", meta_extraction_model="")
    assert s.reduce_model == ""
    assert s.meta_extraction_model == ""


def test_effective_reduce_model_falls_back_to_llm_model():
    s = Settings(_env_file=None, llm_model="base", reduce_model="")
    assert s.effective_reduce_model == "base"


def test_effective_reduce_model_uses_reduce_model_when_set():
    s = Settings(_env_file=None, llm_model="base", reduce_model="heavy")
    assert s.effective_reduce_model == "heavy"


def test_reduce_concurrency_default_and_bounds():
    from pydantic import ValidationError

    assert Settings(_env_file=None).reduce_concurrency == 16
    assert Settings(_env_file=None, reduce_concurrency=100).reduce_concurrency == 100
    with pytest.raises(ValidationError):
        Settings(_env_file=None, reduce_concurrency=0)  # ge=1
    with pytest.raises(ValidationError):
        Settings(_env_file=None, reduce_concurrency=257)  # le=256


# --------------------------------------------------------------------------- #
# extract_json
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected_key",
    [
        ('{"a": 1}', "a"),
        ('```json\n{"a": 1}\n```', "a"),
        ('here you go:\n{"a": 1}\ndone', "a"),
    ],
)
def test_extract_json_variants(raw, expected_key):
    import json

    assert json.loads(extract_json(raw))[expected_key] == 1


# --------------------------------------------------------------------------- #
# APIBackend.chat
# --------------------------------------------------------------------------- #
@respx.mock
async def test_chat_success_and_model_override():
    route = respx.post(_URL).mock(return_value=_ok("hello"))
    b = _backend()
    out = await b.chat("hi", system="sys", model="override-model", json_mode=True)
    assert out == "hello"
    sent = route.calls.last.request
    import json

    body = json.loads(sent.content)
    assert body["model"] == "override-model"  # per-call override wins
    assert body["response_format"] == {"type": "json_object"}  # json_mode flag
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    await b.aclose()


@respx.mock
async def test_chat_defaults_to_configured_model_no_json():
    route = respx.post(_URL).mock(return_value=_ok("x"))
    b = _backend()
    await b.chat("hi")
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "default-model"
    assert "response_format" not in body
    await b.aclose()


@pytest.fixture
def _no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr("sembr.summarizer.llm.api.asyncio.sleep", _instant)


@respx.mock
async def test_chat_retries_transient_then_succeeds(_no_sleep):
    route = respx.post(_URL).mock(
        side_effect=[httpx.Response(503, text="overloaded"), _ok("recovered")]
    )
    b = _backend(chat_max_retries=3)
    out = await b.chat("hi")
    assert out == "recovered"
    assert route.call_count == 2  # one 503 + one success
    await b.aclose()


@respx.mock
async def test_chat_non_retryable_status_raises_immediately():
    route = respx.post(_URL).mock(return_value=httpx.Response(400, text="bad request"))
    b = _backend()
    with pytest.raises(LLMError):
        await b.chat("hi")
    assert route.call_count == 1  # 400 is a client error — no retry
    await b.aclose()


@respx.mock
async def test_chat_scrubs_api_key_from_error_body():
    # An upstream proxy that echoes the bearer token must never leak it.
    route = respx.post(_URL).mock(
        return_value=httpx.Response(401, text="bad token: secret-key-123")
    )
    b = _backend()
    with pytest.raises(LLMError) as ei:
        await b.chat("hi")
    assert "secret-key-123" not in str(ei.value)
    assert "***" in str(ei.value)
    assert route.call_count == 1
    await b.aclose()


@respx.mock
async def test_chat_empty_content_raises():
    respx.post(_URL).mock(return_value=_ok("   "))
    b = _backend()
    with pytest.raises(LLMError):
        await b.chat("hi")
    await b.aclose()


@respx.mock
async def test_chat_exhausts_retries_raises(_no_sleep):
    route = respx.post(_URL).mock(return_value=httpx.Response(503, text="down"))
    b = _backend(chat_max_retries=2)
    with pytest.raises(LLMError):
        await b.chat("hi")
    assert route.call_count == 2
    await b.aclose()


@respx.mock
async def test_chat_skips_backoff_on_final_attempt(monkeypatch):
    # Sleep once between 3 attempts, never after the last — no pointless wait
    # before giving up.
    calls = {"n": 0}

    async def _count(_s):
        calls["n"] += 1

    monkeypatch.setattr("sembr.summarizer.llm.api.asyncio.sleep", _count)
    respx.post(_URL).mock(return_value=httpx.Response(503, text="down"))
    b = _backend(chat_max_retries=3)
    with pytest.raises(LLMError):
        await b.chat("hi")
    assert calls["n"] == 2  # max_retries - 1
    await b.aclose()


def test_scrub_redacts_key_straddling_truncation_boundary():
    # Regression: a key crossing the 200-char cut must still be fully redacted —
    # scrub-then-truncate, not truncate-then-scrub (which would leak the prefix).
    b = _backend(api_key="SUPERSECRETKEY123")
    body = "X" * 198 + "SUPERSECRETKEY123" + "Z"
    out = b._scrub(body)
    assert "SUPERSECRETKEY123" not in out
    assert "SUPER" not in out  # no partial-key prefix survives the cut
    assert len(out) <= 200


# --------------------------------------------------------------------------- #
# BaseLLMBackend.structured (repair loop) — driven by a fake chat
# --------------------------------------------------------------------------- #
class _Out(BaseModel):
    n: int
    label: str


class _FakeBackend(BaseLLMBackend):
    """Returns canned chat replies in order; records prompts for repair-feedback asserts."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self._i = 0
        self.prompts: list[str] = []

    @property
    def max_prompt_chars(self) -> int:
        return 10_000

    async def summarize(self, prompt, *, system=None):  # pragma: no cover - unused
        raise NotImplementedError

    async def chat(self, prompt, *, system=None, model=None, json_mode=False):
        self.prompts.append(prompt)
        reply = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        return reply

    async def health(self):  # pragma: no cover - unused
        return True


async def test_structured_valid_first_try():
    b = _FakeBackend(['{"n": 1, "label": "ok"}'])
    out = await b.structured("extract", _Out)
    assert out == _Out(n=1, label="ok")
    assert len(b.prompts) == 1  # no repair needed


async def test_structured_repairs_then_succeeds():
    b = _FakeBackend(['{"n": "not-an-int"}', '{"n": 2, "label": "fixed"}'])
    out = await b.structured("extract", _Out, repair_attempts=2)
    assert out == _Out(n=2, label="fixed")
    assert len(b.prompts) == 2
    # Repair prompt must carry the original prompt + the validation error feedback.
    assert "extract" in b.prompts[1]
    assert "无法通过校验" in b.prompts[1]


async def test_structured_gives_up_after_repairs():
    b = _FakeBackend(['{"bad": 1}'])
    with pytest.raises(LLMError):
        await b.structured("extract", _Out, repair_attempts=2)
    assert len(b.prompts) == 3  # 1 initial + 2 repairs
