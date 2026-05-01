"""Tests for EmailChannel.send_error."""
from __future__ import annotations

import smtplib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sembr.notifier.email import EmailChannel, EmailChannelConfig


def _make_settings(smtp_host: str = "smtp.example.com") -> MagicMock:
    s = MagicMock()
    s.smtp_host = smtp_host
    s.smtp_port = 587
    s.smtp_username = "user@example.com"
    s.smtp_password.get_secret_value.return_value = "secret"
    s.smtp_from = "sembr@example.com"
    s.smtp_use_ssl = False
    s.smtp_use_starttls = True
    s.display_timezone = "UTC"
    return s


def _make_config() -> EmailChannelConfig:
    return EmailChannelConfig(to=["user@example.com"])


@pytest.mark.asyncio
async def test_send_error_constructs_subject_with_intent_and_template_name() -> None:
    """send_error subject must contain intent name and template name."""
    settings = _make_settings()
    channel = EmailChannel(settings)
    config = _make_config()

    captured_msgs: list = []

    def fake_send_sync(msg, rcpts):
        captured_msgs.append(msg)

    channel._send_sync = fake_send_sync  # type: ignore[method-assign]

    await channel.send_error(
        intent_name="crypto-alerts",
        kind="instruction",
        name="crypto_zh",
        reason="TemplateNotFoundError: template 'instruction/crypto_zh' not found",
        config=config,
    )

    assert captured_msgs, "Expected _send_sync to be called"
    subject = captured_msgs[0]["Subject"]
    assert "crypto-alerts" in subject
    assert "crypto_zh" in subject


@pytest.mark.asyncio
async def test_send_error_never_raises_on_smtp_failure() -> None:
    """send_error must not raise even when SMTP throws."""
    settings = _make_settings()
    channel = EmailChannel(settings)
    config = _make_config()

    def failing_send_sync(msg, rcpts):
        raise smtplib.SMTPException("connection refused")

    channel._send_sync = failing_send_sync  # type: ignore[method-assign]

    # Must not raise
    await channel.send_error(
        intent_name="test-intent",
        kind="system",
        name="missing",
        reason="not found",
        config=config,
    )


@pytest.mark.asyncio
async def test_send_error_skips_when_no_smtp_host() -> None:
    """send_error must silently skip when smtp_host is empty."""
    settings = _make_settings(smtp_host="")
    channel = EmailChannel(settings)
    config = _make_config()

    channel._send_sync = MagicMock()  # type: ignore[method-assign]

    await channel.send_error(
        intent_name="test",
        kind="instruction",
        name="custom",
        reason="not found",
        config=config,
    )

    channel._send_sync.assert_not_called()


@pytest.mark.asyncio
async def test_send_error_html_contains_fix_hint() -> None:
    """HTML body must include the prompts path and available placeholders hint."""
    settings = _make_settings()
    channel = EmailChannel(settings)
    config = _make_config()

    captured_msgs: list = []

    def fake_send_sync(msg, rcpts):
        captured_msgs.append(msg)

    channel._send_sync = fake_send_sync  # type: ignore[method-assign]

    await channel.send_error(
        intent_name="my-intent",
        kind="instruction",
        name="custom_tpl",
        reason="TemplateNotFoundError",
        config=config,
    )

    assert captured_msgs
    raw_payload = captured_msgs[0].get_payload()
    # MIMEText with utf-8 charset base64-encodes the body; decode it.
    if isinstance(raw_payload, bytes):
        payload = raw_payload.decode("utf-8")
    elif isinstance(raw_payload, str):
        import base64
        try:
            payload = base64.b64decode(raw_payload).decode("utf-8")
        except Exception:
            payload = raw_payload
    else:
        payload = str(raw_payload)

    assert "prompts/instruction/custom_tpl.md" in payload
    assert "intent_text" in payload
