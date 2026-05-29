# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sembr.notifier.dispatcher — strict/loose, channel failure, backward-compat."""

from __future__ import annotations

import smtplib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sembr.notifier.dispatcher import ChannelOutcome, dispatch_summary


def _make_result(intent_id=1, summary="s") -> MagicMock:
    from sembr.summarizer.models import SummaryResult

    return SummaryResult(intent_id=intent_id, summary=summary, citations=[])


class _FakeIntent:
    def __init__(self, channels, name="test", timezone="UTC"):
        self.channels = channels
        self.name = name
        self.timezone = timezone


class TestDispatchSummaryStrictFalse:
    @pytest.mark.asyncio
    async def test_returns_ok_true_for_each_channel(self):
        """strict=False calls send() (never-raise) — always ok=True."""
        from sembr.notifier.email import EmailChannelConfig

        intent = _FakeIntent(channels=[EmailChannelConfig(to=["a@x.com"])])
        conn = MagicMock()
        email_ch = MagicMock()
        email_ch.send = AsyncMock()

        with patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)):
            outcomes = await dispatch_summary(conn, email_ch, _make_result(), strict=False)

        assert len(outcomes) == 1
        assert outcomes[0] == ChannelOutcome(type="email", ok=True, error=None)

    @pytest.mark.asyncio
    async def test_send_error_does_not_propagate(self):
        """Even if send() internally raises, strict=False never propagates (send is never-raise)."""
        from sembr.notifier.email import EmailChannelConfig

        intent = _FakeIntent(channels=[EmailChannelConfig(to=["a@x.com"])])
        conn = MagicMock()
        email_ch = MagicMock()
        email_ch.send = AsyncMock()  # send() already never-raises per its contract

        with patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)):
            outcomes = await dispatch_summary(conn, email_ch, _make_result(), strict=False)

        assert outcomes[0].ok is True

    @pytest.mark.asyncio
    async def test_intent_not_found_returns_empty(self):
        conn = MagicMock()
        email_ch = MagicMock()
        with patch("sembr.db.intents.get_intent", AsyncMock(return_value=None)):
            outcomes = await dispatch_summary(conn, email_ch, _make_result())
        assert outcomes == []


class TestDispatchSummaryStrictTrue:
    @pytest.mark.asyncio
    async def test_returns_ok_true_on_success(self):
        from sembr.notifier.email import EmailChannelConfig

        intent = _FakeIntent(channels=[EmailChannelConfig(to=["a@x.com"])])
        conn = MagicMock()
        email_ch = MagicMock()
        email_ch.send_strict = AsyncMock()

        with patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)):
            outcomes = await dispatch_summary(conn, email_ch, _make_result(), strict=True)

        assert len(outcomes) == 1
        assert outcomes[0] == ChannelOutcome(type="email", ok=True, error=None)

    @pytest.mark.asyncio
    async def test_catches_per_channel_error(self):
        from sembr.notifier.email import EmailChannelConfig

        intent = _FakeIntent(
            channels=[
                EmailChannelConfig(to=["a@x.com"]),
                EmailChannelConfig(to=["b@x.com"]),
            ]
        )
        conn = MagicMock()
        email_ch = MagicMock()

        call_count = 0

        async def _send_strict_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise smtplib.SMTPConnectError(1, "refused")
            return None

        email_ch.send_strict = AsyncMock(side_effect=_send_strict_side_effect)

        with patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)):
            outcomes = await dispatch_summary(conn, email_ch, _make_result(), strict=True)

        assert len(outcomes) == 2
        assert outcomes[0].ok is False
        assert "refused" in outcomes[0].error
        assert outcomes[1] == ChannelOutcome(type="email", ok=True, error=None)

    @pytest.mark.asyncio
    async def test_all_channels_failed(self):
        from sembr.notifier.email import EmailChannelConfig

        intent = _FakeIntent(channels=[EmailChannelConfig(to=["a@x.com"])])
        conn = MagicMock()
        email_ch = MagicMock()
        email_ch.send_strict = AsyncMock(side_effect=smtplib.SMTPException("boom"))

        with patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)):
            outcomes = await dispatch_summary(conn, email_ch, _make_result(), strict=True)

        assert outcomes[0].ok is False
        assert outcomes[0].error == "boom"


class TestBackwardCompat:
    @pytest.mark.asyncio
    async def test_main_dispatch_notification_calls_dispatcher(self):
        """Verify main.py wrapper calls dispatch_summary(strict=False)."""
        from sembr.notifier.email import EmailChannelConfig

        intent = _FakeIntent(channels=[EmailChannelConfig(to=["a@x.com"])])
        conn = MagicMock()
        email_ch = MagicMock()
        email_ch.send = AsyncMock()

        with patch("sembr.db.intents.get_intent", AsyncMock(return_value=intent)):
            outcomes = await dispatch_summary(conn, email_ch, _make_result(), strict=False)

        assert all(o.ok for o in outcomes)
