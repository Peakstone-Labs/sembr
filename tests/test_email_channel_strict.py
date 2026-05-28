# SPDX-License-Identifier: Apache-2.0
"""Unit tests for EmailChannel.send_strict — raises on failure vs send never-raise."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

from sembr.notifier.email import EmailChannel, EmailChannelConfig
from sembr.summarizer.models import Citation, SummaryResult


def _make_result(intent_id=1, summary="test summary") -> SummaryResult:
    return SummaryResult(
        intent_id=intent_id,
        summary=summary,
        citations=[
            Citation(article_id="a1", title="T", url="http://x", source=1, published_at=None)
        ],
    )


@pytest.fixture
def email_ch():
    """EmailChannel with smtp_host set so _send doesn't short-circuit."""
    settings = MagicMock()
    settings.smtp_host = "smtp.example.com"
    settings.smtp_port = 587
    settings.smtp_use_ssl = False
    settings.smtp_use_starttls = True
    settings.smtp_username = "user"
    settings.smtp_password.get_secret_value.return_value = "pass"
    settings.smtp_from = "from@example.com"
    return EmailChannel(settings)


@pytest.fixture
def cfg():
    return EmailChannelConfig(to=["to@example.com"])


class TestSendStrictRaises:
    @pytest.mark.asyncio
    async def test_raises_on_smtp_error(self, email_ch, cfg):
        result = _make_result()
        with patch.object(
            email_ch, "_send_sync", side_effect=smtplib.SMTPConnectError(1, "refused")
        ):
            with pytest.raises(smtplib.SMTPConnectError):
                await email_ch.send_strict(
                    result, config=cfg, intent_name="test", intent_timezone="UTC"
                )

    @pytest.mark.asyncio
    async def test_succeeds_normally(self, email_ch, cfg):
        result = _make_result()
        with patch.object(email_ch, "_send_sync"):
            await email_ch.send_strict(
                result, config=cfg, intent_name="test", intent_timezone="UTC"
            )


class TestSendKeepsNeverRaise:
    @pytest.mark.asyncio
    async def test_send_silently_logs_on_error(self, email_ch, cfg):
        result = _make_result()
        with patch.object(email_ch, "_send_sync", side_effect=smtplib.SMTPException("boom")):
            # send() must not raise
            await email_ch.send(result, config=cfg, intent_name="test", intent_timezone="UTC")
