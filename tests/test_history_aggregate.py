# SPDX-License-Identifier: Apache-2.0
"""Integration tests for POST /intents/{id}/history/aggregate and .../send."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.history import router
from sembr.summarizer.llm.base import LLMError


def _make_app(**overrides):
    app = FastAPI()
    app.include_router(router)
    app.state.llm_backend = overrides.get("llm_backend")
    app.state.email_channel = overrides.get("email_channel")
    return app


def _mock_conn():
    return MagicMock()


def _fake_intent(intent_id=1, timezone_str="UTC", channels=None):
    from sembr.models import CronSchedule, Intent

    if channels is None:
        from sembr.notifier.email import EmailChannelConfig

        channels = [EmailChannelConfig(to=["a@x.com"])]

    return Intent(
        id=intent_id,
        name="test-intent",
        text="test",
        threshold=0.75,
        enabled=True,
        channels=channels,
        tags=[],
        schedule=CronSchedule(preset="daily", lookback_seconds=3600, skip_seen=True),
        feed_filter=None,
        timezone=timezone_str,
        language="en",
        system_template="default",
        instruction_template="default",
        extraction_enabled=False,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


def _fake_rows(n=3):
    return [
        {
            "id": i,
            "intent_id": 1,
            "run_at": f"2026-05-{28 - i:02d}T00:00:00Z",
            "summary": f"Day {28 - i} summary.",
            "citations": [
                {
                    "article_id": f"art-{i}",
                    "title": f"Article {i}",
                    "url": f"https://x.com/{i}",
                    "source": 1,
                    "published_at": None,
                    "source_name": "Feed1",
                    "score": 0.85,
                }
            ],
        }
        for i in range(1, n + 1)
    ]


# ---------------------------------------------------------------------------
# Aggregate tests
# ---------------------------------------------------------------------------


class TestAggregateHappyPath:
    @pytest.mark.asyncio
    async def test_returns_summary_and_row_counts(self):
        rows = _fake_rows(3)
        llm = MagicMock()
        llm.max_prompt_chars = 10_000
        llm.summarize = AsyncMock(return_value="mock summary")

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app(llm_backend=llm))
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2026-05-01", "until": "2026-05-28", "prompt": "TL;DR:\n{history}"},
            )

        assert r.status_code == 200
        data = r.json()
        assert data["summary"] == "mock summary"
        assert data["rows_total"] == 3
        assert data["rows_used"] == 3
        assert data["rows_dropped"] == 0

    @pytest.mark.asyncio
    async def test_empty_range_returns_null_summary(self):
        llm = MagicMock()
        llm.max_prompt_chars = 10_000

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=[])),
        ):
            client = TestClient(_make_app(llm_backend=llm))
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2026-05-01", "until": "2026-05-28", "prompt": "{history}"},
            )

        assert r.status_code == 200
        data = r.json()
        assert data["summary"] is None
        assert data["rows_total"] == 0


class TestAggregateErrors:
    @pytest.mark.asyncio
    async def test_intent_not_found_returns_404(self):
        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=None)),
        ):
            client = TestClient(_make_app())
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2026-05-01", "until": "2026-05-28", "prompt": "{history}"},
            )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_placeholder_returns_422(self):
        llm = MagicMock()
        llm.max_prompt_chars = 10_000
        rows = _fake_rows(1)

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app(llm_backend=llm))
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2026-05-01", "until": "2026-05-28", "prompt": "no placeholder"},
            )

        assert r.status_code == 422
        assert "{history}" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_llm_error_returns_502(self):
        llm = MagicMock()
        llm.max_prompt_chars = 10_000
        llm.summarize = AsyncMock(side_effect=LLMError("upstream 500"))
        rows = _fake_rows(1)

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app(llm_backend=llm))
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2026-05-01", "until": "2026-05-28", "prompt": "{history}"},
            )

        assert r.status_code == 502
        assert "upstream 500" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_even_newest_row_overflows_returns_422(self):
        """rows_used==0 and rows_total>0 -> 422 prompt template too long."""
        llm = MagicMock()
        llm.max_prompt_chars = 100  # tiny budget
        rows = [
            {
                "id": 1,
                "intent_id": 1,
                "run_at": "2026-05-28T00:00:00Z",
                "summary": "X" * 10_000,  # far exceeds budget
                "citations": [],
            }
        ]

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app(llm_backend=llm))
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2026-05-01", "until": "2026-05-28", "prompt": "{history}"},
            )

        assert r.status_code == 422
        assert "prompt template too long" in r.json()["detail"]


class TestAggregateDateRange:
    @pytest.mark.asyncio
    async def test_tz_boundary_row_included(self):
        """Row at Shanghai midnight UTC should be included for the right date."""
        row = [
            {
                "id": 1,
                "intent_id": 1,
                "run_at": "2026-05-27T16:30:00Z",  # Shanghai 2026-05-28 00:30
                "summary": "test",
                "citations": [],
            }
        ]
        llm = MagicMock()
        llm.max_prompt_chars = 10_000
        llm.summarize = AsyncMock(return_value="ok")

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch(
                "sembr.api.history.get_intent",
                AsyncMock(return_value=_fake_intent(timezone_str="Asia/Shanghai")),
            ),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=row)),
        ):
            client = TestClient(_make_app(llm_backend=llm))
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2026-05-28", "until": "2026-05-28", "prompt": "{history}"},
            )

        assert r.status_code == 200
        assert r.json()["rows_total"] == 1


class TestAggregateDateRangeErrors:
    """Date-range validation errors for the aggregate POST endpoint."""

    @pytest.mark.asyncio
    async def test_range_exceeds_365_days_returns_422(self):
        """date span > 365 days -> 422."""
        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
        ):
            client = TestClient(_make_app())
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2025-01-01", "until": "2026-05-28", "prompt": "{history}"},
            )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_since_after_until_returns_422(self):
        """since > until -> 422."""
        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
        ):
            client = TestClient(_make_app())
            r = client.post(
                "/intents/1/history/aggregate",
                json={"since": "2026-05-28", "until": "2026-05-01", "prompt": "{history}"},
            )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Aggregate/Send tests
# ---------------------------------------------------------------------------


class TestAggregateSend:
    @pytest.mark.asyncio
    async def test_send_happy_path(self):
        from sembr.notifier.email import EmailChannelConfig

        rows = _fake_rows(2)
        intent = _fake_intent(channels=[EmailChannelConfig(to=["a@x.com"])])
        email_ch = MagicMock()
        email_ch.send_strict = AsyncMock()

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app(email_channel=email_ch))
            r = client.post(
                "/intents/1/history/aggregate/send",
                json={"since": "2026-05-01", "until": "2026-05-28", "markdown": "# Summary"},
            )

        assert r.status_code == 200
        data = r.json()
        assert data["results"][0]["ok"] is True
        assert data["results"][0]["type"] == "email"

    @pytest.mark.asyncio
    async def test_send_all_channels_failed_returns_502(self):
        import smtplib

        from sembr.notifier.email import EmailChannelConfig

        rows = _fake_rows(1)
        intent = _fake_intent(channels=[EmailChannelConfig(to=["a@x.com"])])
        email_ch = MagicMock()
        email_ch.send_strict = AsyncMock(side_effect=smtplib.SMTPException("refused"))

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app(email_channel=email_ch))
            r = client.post(
                "/intents/1/history/aggregate/send",
                json={"since": "2026-05-01", "until": "2026-05-28", "markdown": "# Summary"},
            )

        assert r.status_code == 502
        data = r.json()
        assert data["results"][0]["ok"] is False

    @pytest.mark.asyncio
    async def test_send_citations_merged_and_capped(self):
        from sembr.notifier.email import EmailChannelConfig

        # 60 unique citations — should be capped at 50
        rows = []
        for i in range(60):
            rows.append(
                {
                    "id": i,
                    "intent_id": 1,
                    "run_at": f"2026-05-{28 - (i % 28):02d}T00:00:00Z",
                    "summary": f"Day {i}",
                    "citations": [
                        {
                            "article_id": f"art-{i}",
                            "title": f"Article {i}",
                            "url": f"https://x.com/{i}",
                            "source": 1,
                            "published_at": None,
                            "source_name": "Feed1",
                            "score": 0.8,
                        }
                    ],
                }
            )

        intent = _fake_intent(channels=[EmailChannelConfig(to=["a@x.com"])])
        email_ch = MagicMock()
        email_ch.send_strict = AsyncMock()

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app(email_channel=email_ch))
            r = client.post(
                "/intents/1/history/aggregate/send",
                json={"since": "2026-05-01", "until": "2026-05-28", "markdown": "# Summary"},
            )

        assert r.status_code == 200
        email_ch.send_strict.assert_awaited_once()
        call_args = email_ch.send_strict.call_args
        result_arg = call_args[0][0]  # first positional arg
        assert len(result_arg.citations) <= 50

    @pytest.mark.asyncio
    async def test_send_persists_nothing(self):
        """Send must not write to summary_history (Non-Goals: 聚合产物不持久化)."""
        from sembr.notifier.email import EmailChannelConfig

        rows = _fake_rows(1)
        intent = _fake_intent(channels=[EmailChannelConfig(to=["a@x.com"])])
        email_ch = MagicMock()
        email_ch.send_strict = AsyncMock()

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
            patch("sembr.db.summary_history.save_summary") as mock_save,
        ):
            client = TestClient(_make_app(email_channel=email_ch))
            r = client.post(
                "/intents/1/history/aggregate/send",
                json={"since": "2026-05-01", "until": "2026-05-28", "markdown": "# Summary"},
            )

        assert r.status_code == 200
        mock_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_intent_no_channels_returns_422(self):
        """intent.channels == [] -> 422, no SMTP attempted."""
        rows = _fake_rows(1)
        intent = _fake_intent(channels=[])  # no channels configured
        email_ch = MagicMock()

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app(email_channel=email_ch))
            r = client.post(
                "/intents/1/history/aggregate/send",
                json={"since": "2026-05-01", "until": "2026-05-28", "markdown": "# Summary"},
            )

        assert r.status_code == 422
        assert "no channels" in r.json()["detail"].lower()
