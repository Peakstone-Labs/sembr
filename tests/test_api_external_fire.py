"""Endpoint behaviour for ``POST /api/external/intents/{intent_id}/fire``.

Covers AC1–AC10 of external-fire-api/requirements.md plus v2 additions
(Qdrant→500, empty intent_text, template error, summary_error length, no
match_seen write, no on_match invocation, body overrides). Auth-prefix
integration is handled in ``test_dashboard_auth_external_fire.py`` and is
NOT duplicated here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.external_fire import router as external_fire_router
from sembr.matcher.fire_tasks import _last_fire_at, _reset_for_testing
from sembr.matcher.scan import ScanOptions
from sembr.summarizer.models import Citation, SummaryResult


@pytest.fixture(autouse=True)
def reset_tasks():
    """Per design Implementation Constraint #7: same-process tests share
    ``_last_fire_at`` with ``test_api_fire.py`` — reset on entry/exit so order
    of execution can't trigger spurious 429s."""
    _reset_for_testing()
    yield
    _reset_for_testing()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_intent(intent_id: int = 1, *, threshold: float = 0.75):
    from sembr.models import CronSchedule, Intent

    return Intent(
        id=intent_id,
        name="external-test",
        text="quantum computing",
        threshold=threshold,
        enabled=True,
        channels=[],
        tags=[],
        schedule=CronSchedule(preset="daily", lookback_seconds=3600, skip_seen=True),
        feed_filter=None,
        timezone="UTC",
        language="en",
        system_template="default",
        instruction_template="default",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def _fake_event_intent(intent_id: int = 10):
    from sembr.models import EventSchedule, Intent

    return Intent(
        id=intent_id,
        name="event-intent",
        text="tracking topic",
        threshold=0.75,
        enabled=True,
        channels=[],
        tags=[],
        schedule=EventSchedule(trigger_count=3, max_wait_seconds=1800),
        feed_filter=None,
        timezone="UTC",
        language="en",
        system_template="default",
        instruction_template="default",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def _fake_match(article_id: str = "art-1", score: float = 0.82):
    m = MagicMock()
    m.article_id = article_id
    m.intent_id = 1
    m.score = score
    m.payload = {
        "title": "Test Article",
        "url": "https://example.com",
        "published_at": "2026-05-01T00:00:00Z",
        "feed_id": 7,
    }
    return m


def _make_app(*, summary_pipeline=None, on_match=None) -> FastAPI:
    app = FastAPI()
    app.include_router(external_fire_router)
    app.state.qdrant = MagicMock()
    app.state.qdrant.client = MagicMock()
    app.state.summary_pipeline = summary_pipeline if summary_pipeline is not None else _make_pipeline()
    app.state.on_match = on_match
    return app


def _make_pipeline(summary_text: str = "synthetic digest") -> MagicMock:
    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(
        return_value=SummaryResult(
            intent_id=1,
            summary=summary_text,
            citations=[
                Citation(
                    article_id="art-1",
                    title="Test Article",
                    url="https://example.com",
                    source=7,
                    published_at="2026-05-01T00:00:00Z",
                )
            ],
        )
    )
    return pipeline


# ---------------------------------------------------------------------------
# AC1 — happy path
# ---------------------------------------------------------------------------


def test_post_returns_matches_and_summary() -> None:
    intent = _fake_intent()
    matches = [_fake_match("art-1", 0.85), _fake_match("art-2", 0.78)]
    pipeline = _make_pipeline("multi-article digest")
    app = _make_app(summary_pipeline=pipeline)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["intent_id"] == 1
    assert body["match_count"] == 2
    assert body["summary"] == "multi-article digest"
    assert body["summary_error"] is None
    assert len(body["matches"]) == 2
    assert body["matches"][0]["article_id"] == "art-1"
    assert body["matches"][0]["score"] == 0.85
    assert body["matches"][0]["feed_id"] == 7
    pipeline.compute_summary.assert_awaited_once_with(matches)


# ---------------------------------------------------------------------------
# AC2 — zero hits skip LLM
# ---------------------------------------------------------------------------


def test_zero_matches_skips_llm() -> None:
    intent = _fake_intent()
    pipeline = _make_pipeline()
    app = _make_app(summary_pipeline=pipeline)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=[])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "intent_id": 1,
        "match_count": 0,
        "matches": [],
        "summary": None,
        "summary_error": None,
    }
    pipeline.compute_summary.assert_not_called()


# ---------------------------------------------------------------------------
# AC4 — unknown intent → 404
# ---------------------------------------------------------------------------


def test_unknown_intent_returns_404() -> None:
    app = _make_app()

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=None)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/999/fire", json={})

    assert resp.status_code == 404
    assert resp.json() == {"detail": "intent not found"}


# ---------------------------------------------------------------------------
# AC5 — event-mode → 409
# ---------------------------------------------------------------------------


def test_event_mode_returns_409() -> None:
    intent = _fake_event_intent(10)
    app = _make_app()

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/10/fire", json={})

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    # D9: distinct wording from /intents/{id}/fire so logs disambiguate origin.
    assert "/api/external/" in detail
    assert "cron-mode" in detail


# ---------------------------------------------------------------------------
# AC6 — rate limit shared with internal fire
# ---------------------------------------------------------------------------


def test_rate_limit_shared_with_internal_fire() -> None:
    """Sync external fire and async internal fire must compete for the same
    1/intent/60s bucket. Stamp _last_fire_at[1] via ``create_task`` (the async
    path's record point) and verify external fire returns 429 immediately."""
    from sembr.matcher.fire_tasks import create_task

    create_task(intent_id=1)  # async path stamped its slot first

    intent = _fake_intent()
    app = _make_app()

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 429
    assert "rate limit" in resp.json()["detail"]


def test_rate_limit_two_external_fires_back_to_back() -> None:
    intent = _fake_intent()
    app = _make_app()

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=[])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        first = client.post("/api/external/intents/1/fire", json={})
        second = client.post("/api/external/intents/1/fire", json={})

    assert first.status_code == 200
    assert second.status_code == 429


# ---------------------------------------------------------------------------
# AC7 — no match_seen write (write_match_seen=False)
# ---------------------------------------------------------------------------


def test_no_match_seen_write() -> None:
    """Captures the ScanOptions handed to scan_once and asserts
    write_match_seen=False so no match_seen row is ever produced."""
    intent = _fake_intent()
    captured: dict[str, Any] = {}

    async def fake_scan_once(intent, options, conn, qdrant_client):
        captured["options"] = options
        return []

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=fake_scan_once),
    ):
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 200
    assert isinstance(captured["options"], ScanOptions)
    assert captured["options"].write_match_seen is False
    # D-A6: external endpoint must opt into the propagating mode so a Qdrant
    # outage cannot masquerade as a 0-hit response.
    assert captured["options"].propagate_qdrant_errors is True


# ---------------------------------------------------------------------------
# AC8 — on_match is never invoked (no notification)
# ---------------------------------------------------------------------------


def test_no_on_match_invocation() -> None:
    on_match = AsyncMock()
    intent = _fake_intent()
    matches = [_fake_match()]

    app = _make_app(on_match=on_match)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 200
    on_match.assert_not_awaited()


# ---------------------------------------------------------------------------
# AC9 — body overrides reach scan_once
# ---------------------------------------------------------------------------


def test_body_overrides_passed_to_scan_once() -> None:
    intent = _fake_intent()
    captured: dict[str, Any] = {}

    async def fake_scan_once(intent, options, conn, qdrant_client):
        captured["options"] = options
        return []

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=fake_scan_once),
    ):
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post(
            "/api/external/intents/1/fire",
            json={
                "lookback_seconds": 600,
                "threshold": 0.80,
                "skip_seen": False,
                "feed_ids": [3, 5],
            },
        )

    assert resp.status_code == 200
    opts: ScanOptions = captured["options"]
    assert opts.lookback_seconds == 600
    assert abs(opts.threshold - 0.80) < 1e-9
    assert opts.skip_seen is False
    assert opts.feed_ids == [3, 5]


def test_empty_feed_ids_returns_zero_matches_without_qdrant() -> None:
    """``feed_ids=[]`` semantics: scan_once short-circuits → 0 matches; LLM never
    runs. Verify nothing in the endpoint mistakes the empty list for None."""
    intent = _fake_intent()
    captured: dict[str, Any] = {}

    qdrant_calls = []

    async def fake_scan_once(intent, options, conn, qdrant_client):
        captured["options"] = options
        if options.feed_ids == []:
            return []
        qdrant_calls.append(True)
        return [_fake_match()]

    pipeline = _make_pipeline()
    app = _make_app(summary_pipeline=pipeline)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=fake_scan_once),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/external/intents/1/fire", json={"feed_ids": []}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["match_count"] == 0
    assert captured["options"].feed_ids == []
    pipeline.compute_summary.assert_not_called()
    assert qdrant_calls == []


# ---------------------------------------------------------------------------
# AC10 — LLM error → summary=null + summary_error
# ---------------------------------------------------------------------------


def test_llm_error_returns_summary_error_field() -> None:
    intent = _fake_intent()
    matches = [_fake_match()]
    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(
        side_effect=httpx.TimeoutException("upstream stalled at /v1/chat")
    )

    app = _make_app(summary_pipeline=pipeline)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] is None
    assert body["summary_error"]
    assert len(body["matches"]) == 1
    assert "TimeoutException" in body["summary_error"]
    # R5 / constraint #8: summary_error must not leak tracebacks, filesystem
    # paths, or upstream URLs. Scrub strips both `/` (URL / posix path) and
    # `\\` (windows path) — the LLM provider URL ``/v1/chat`` in the source
    # exception must therefore disappear.
    se = body["summary_error"]
    assert len(se) <= 250
    assert "Traceback" not in se
    assert "\n" not in se
    assert "/" not in se
    assert "\\" not in se
    assert "/v1/chat" not in se  # explicit anti-regression for the seeded path
    assert "\t" not in se


def test_summary_error_scrubs_paths_and_newlines() -> None:
    """🟡-1 / constraint #8 / R5: even when str(exc) contains posix paths,
    windows paths, multi-line content, tabs, and an upstream URL, the
    response field MUST collapse them all to whitespace before truncation."""
    intent = _fake_intent()
    matches = [_fake_match()]
    raw = (
        "fail at /app/prompts/system/x.md\nat C:\\Users\\app\\state\nURL "
        "https://api.siliconflow.cn/v1/chat/completions\nlast frame\twith\ttabs"
    )
    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(side_effect=RuntimeError(raw))

    app = _make_app(summary_pipeline=pipeline)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    body = resp.json()
    se = body["summary_error"]
    assert se.startswith("RuntimeError: ")
    assert "/" not in se
    assert "\\" not in se
    assert "\n" not in se
    assert "\t" not in se
    assert "/v1/chat/completions" not in se
    assert "C:" in se  # the literal "C:" (without trailing slash) remains; only
    # the separator was stripped, so the type/name fragment is still readable.


def test_summary_error_length_bound_holds_for_long_message() -> None:
    """Verify the ``str(exc)[:200]`` truncation stays bounded under abusive
    upstream messages."""
    long_msg = "x" * 5_000
    intent = _fake_intent()
    matches = [_fake_match()]
    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(side_effect=RuntimeError(long_msg))

    app = _make_app(summary_pipeline=pipeline)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    body = resp.json()
    se = body["summary_error"]
    assert se.startswith("RuntimeError: ")
    # 200 chars body + "RuntimeError: " prefix (~14) → roughly 214; well under 250.
    assert len(se) <= 250


# ---------------------------------------------------------------------------
# v2 — Qdrant failure → 500 (D-A6 / D12 / R6)
# ---------------------------------------------------------------------------


def test_qdrant_failure_returns_500() -> None:
    intent = _fake_intent()
    app = _make_app()

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch(
            "sembr.api.external_fire.scan_once",
            new=AsyncMock(side_effect=RuntimeError("qdrant unreachable")),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 500
    # D12: error body must not leak internal exception detail.
    assert resp.json() == {"detail": "qdrant query failed"}


# ---------------------------------------------------------------------------
# v2 — empty intent_text → summary null, no error (D-A8 / D15)
# ---------------------------------------------------------------------------


def test_empty_intent_text_returns_summary_null_no_error() -> None:
    intent = _fake_intent()
    matches = [_fake_match()]
    pipeline = MagicMock()
    # compute_summary returning None mirrors what the real pipeline does for
    # empty intent_text / budget_deficit / ctx fetch failure (D-A8).
    pipeline.compute_summary = AsyncMock(return_value=None)

    app = _make_app(summary_pipeline=pipeline)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    body = resp.json()
    assert resp.status_code == 200
    assert body["match_count"] == 1
    assert body["summary"] is None
    assert body["summary_error"] is None


# ---------------------------------------------------------------------------
# v2 — template error → summary_error includes type name
# ---------------------------------------------------------------------------


def test_template_error_returns_summary_error() -> None:
    from sembr.summarizer.templates import TemplateRenderError

    intent = _fake_intent()
    matches = [_fake_match()]
    pipeline = MagicMock()
    pipeline.compute_summary = AsyncMock(
        side_effect=TemplateRenderError("Instruction template 'x' contains undeclared placeholder {bad}")
    )

    app = _make_app(summary_pipeline=pipeline)

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] is None
    assert "TemplateRenderError" in body["summary_error"]


# ---------------------------------------------------------------------------
# Defensive — missing app.state.summary_pipeline → 500
# ---------------------------------------------------------------------------


def test_missing_summary_pipeline_returns_500() -> None:
    """Lifespan should always wire summary_pipeline; if it's absent we'd rather
    surface a clean 500 than a misleading ``summary: null`` success."""
    intent = _fake_intent()
    matches = [_fake_match()]

    app = FastAPI()
    app.include_router(external_fire_router)
    app.state.qdrant = MagicMock()
    app.state.qdrant.client = MagicMock()
    # deliberately omit app.state.summary_pipeline

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.external_fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/external/intents/1/fire", json={})

    assert resp.status_code == 500
    assert resp.json() == {"detail": "summary pipeline unavailable"}


# ---------------------------------------------------------------------------
# Pydantic body — extra fields rejected
# ---------------------------------------------------------------------------


def test_extra_body_field_rejected_with_422() -> None:
    """ExternalFireRequest sets ``extra='forbid'`` so unknown keys (including a
    typo'd ``feed_id`` for ``feed_ids``) surface as 422 rather than silently
    being dropped."""
    intent = _fake_intent()
    app = _make_app()

    with (
        patch("sembr.api.external_fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.external_fire.get_intent", new=AsyncMock(return_value=intent)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/external/intents/1/fire", json={"feed_id": [3]}
        )

    assert resp.status_code == 422
