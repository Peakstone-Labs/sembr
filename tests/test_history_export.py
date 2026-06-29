# SPDX-License-Identifier: Apache-2.0
"""Integration tests for GET /intents/{id}/history/export."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.history import router


def _make_app():
    app = FastAPI()
    app.include_router(router)
    return app


def _mock_conn():
    return MagicMock()


def _fake_intent(intent_id=1, timezone_str="UTC"):
    from sembr.models import CronSchedule, Intent

    return Intent(
        id=intent_id,
        name="test-intent",
        text="test",
        threshold=0.75,
        enabled=True,
        channels=[],
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
                    "source_name": None,
                    "score": 0.85,
                }
            ],
        }
        for i in range(1, n + 1)
    ]


class TestExportHappyPath:
    @pytest.mark.asyncio
    async def test_returns_json_array_with_content_disposition(self):
        rows = _fake_rows(3)

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app())
            r = client.get("/intents/1/history/export?since=2026-05-01&until=2026-05-28")

        assert r.status_code == 200
        assert (
            r.headers["content-disposition"]
            == "attachment; filename=intent-1-2026-05-01-2026-05-28.json"
        )
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 3

    @pytest.mark.asyncio
    async def test_empty_range_returns_empty_array(self):
        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=[])),
        ):
            client = TestClient(_make_app())
            r = client.get("/intents/1/history/export?since=2026-05-01&until=2026-05-28")

        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_schema_matches_list_summaries(self):
        """Export row fields must match list_summaries return shape."""
        rows = _fake_rows(1)

        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
            patch("sembr.api.history.list_summaries_between", AsyncMock(return_value=rows)),
        ):
            client = TestClient(_make_app())
            r = client.get("/intents/1/history/export?since=2026-05-01&until=2026-05-28")

        row = r.json()[0]
        expected_keys = {"id", "intent_id", "run_at", "summary", "citations"}
        assert set(row.keys()) == expected_keys


class TestExportErrors:
    @pytest.mark.asyncio
    async def test_intent_not_found_returns_404(self):
        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=None)),
        ):
            client = TestClient(_make_app())
            r = client.get("/intents/1/history/export?since=2026-05-01&until=2026-05-28")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_range_exceeds_365_days_returns_422(self):
        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
        ):
            client = TestClient(_make_app())
            r = client.get("/intents/1/history/export?since=2025-01-01&until=2026-05-28")
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_since_after_until_returns_422(self):
        with (
            patch("sembr.api.history.get_conn", return_value=_mock_conn()),
            patch("sembr.api.history.get_intent", AsyncMock(return_value=_fake_intent())),
        ):
            client = TestClient(_make_app())
            r = client.get("/intents/1/history/export?since=2026-05-28&until=2026-05-01")
        assert r.status_code == 422
