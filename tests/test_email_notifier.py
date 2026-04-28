"""Unit tests for EmailChannel (UT-1 through UT-8, design SC1–SC6)."""
from __future__ import annotations

import smtplib
from datetime import date
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sembr.summarizer.models import Citation, SummaryResult


def _citation(
    article_id: str,
    title: str = "Test Article",
    url: str = "https://example.com/1",
    published_at: str | None = None,
) -> Citation:
    return Citation(
        article_id=article_id,
        title=title,
        url=url,
        source=1,
        published_at=published_at,
    )


def _result(
    primary: Citation,
    other_sources: list[Citation] | None = None,
    intent_id: int = 1,
    summary: str = "Test summary.",
) -> SummaryResult:
    return SummaryResult(
        intent_id=intent_id,
        summary=summary,
        primary=primary,
        other_sources=other_sources or [],
    )


def _make_settings(smtp_host: str = "smtp.example.com") -> MagicMock:
    s = MagicMock()
    s.smtp_host = smtp_host
    s.smtp_port = 587
    s.smtp_username = "user@example.com"
    s.smtp_password.get_secret_value.return_value = "secret"
    s.smtp_from = ""
    s.smtp_use_starttls = True
    s.smtp_use_ssl = False
    return s


def _make_channel(smtp_host: str = "smtp.example.com"):
    from sembr.notifier.email import EmailChannel
    return EmailChannel(_make_settings(smtp_host=smtp_host))


# ---------------------------------------------------------------------------
# UT-1: date grouping — citations across 3 days + 1 None
# ---------------------------------------------------------------------------


def test_group_by_date_three_days_plus_none() -> None:
    ch = _make_channel()
    citations = [
        _citation("a", published_at="2026-01-03T10:00:00Z"),
        _citation("b", published_at="2026-01-01T09:00:00Z"),
        _citation("c", published_at="2026-01-02T12:00:00Z"),
        _citation("d", published_at=None),
    ]
    grouped = ch._group_by_date(citations)

    date_keys = [k for k, _ in grouped]
    assert date_keys == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
        None,
    ]
    assert len(grouped) == 4


# ---------------------------------------------------------------------------
# UT-2: date grouping — all None
# ---------------------------------------------------------------------------


def test_group_by_date_all_none() -> None:
    ch = _make_channel()
    citations = [_citation("a"), _citation("b")]
    grouped = ch._group_by_date(citations)
    assert len(grouped) == 1
    assert grouped[0][0] is None
    assert len(grouped[0][1]) == 2


# ---------------------------------------------------------------------------
# UT-3: HTML rendering — href count and date headings
# ---------------------------------------------------------------------------


def test_render_html_href_and_heading_counts() -> None:
    ch = _make_channel()
    citations = [
        _citation("a", title="Article One", url="https://ex.com/1", published_at="2026-01-01T00:00:00Z"),
        _citation("b", title="Article Two", url="https://ex.com/2", published_at="2026-01-01T00:00:00Z"),
        _citation("c", title="Article Three", url="https://ex.com/3", published_at="2026-01-02T00:00:00Z"),
    ]
    result = _result(citations[0], citations[1:])
    grouped = ch._group_by_date(citations)
    html = ch._render_html(result, grouped, "Test Intent")

    assert html.count('<a href=') == 3
    assert "2026-01-01" in html
    assert "2026-01-02" in html


# ---------------------------------------------------------------------------
# UT-4: XSS — title with <script> must be escaped
# ---------------------------------------------------------------------------


def test_render_html_xss_escape() -> None:
    ch = _make_channel()
    evil = _citation("x", title='<script>alert("xss")</script>', url="https://ex.com/x")
    result = _result(evil)
    grouped = ch._group_by_date([evil])
    html = ch._render_html(result, grouped, "Intent")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# UT-5: SMTP failure must not re-raise; logger.error called; quit() still called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_failure_does_not_reraise() -> None:
    ch = _make_channel()

    with patch("smtplib.SMTP") as mock_smtp_cls, \
         patch("sembr.notifier.email.logger") as mock_logger:
        instance = MagicMock()
        instance.starttls = MagicMock()
        instance.login = MagicMock(side_effect=smtplib.SMTPAuthenticationError(535, b"auth failed"))
        instance.send_message = MagicMock()
        instance.quit = MagicMock()
        mock_smtp_cls.return_value = instance

        result = _result(_citation("a", published_at="2026-01-01T00:00:00Z"))
        # Must not raise
        await ch.send(result, to="dest@example.com", intent_name="Test")

    mock_logger.error.assert_called_once()
    # Connection must be cleaned up even when login raises.
    instance.quit.assert_called_once()


# ---------------------------------------------------------------------------
# UT-6: smtp_host empty → early return, smtplib never called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_smtp_host_skips_send() -> None:
    ch = _make_channel(smtp_host="")

    with patch("smtplib.SMTP") as mock_smtp_cls, \
         patch("smtplib.SMTP_SSL") as mock_ssl_cls:
        result = _result(_citation("a"))
        await ch.send(result, to="x@example.com", intent_name="Intent")

    mock_smtp_cls.assert_not_called()
    mock_ssl_cls.assert_not_called()


# ---------------------------------------------------------------------------
# UT-7: subject format — singular and plural
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("n,expected_word", [(1, "1 matched article"), (3, "3 matched articles")])
async def test_subject_format(n: int, expected_word: str) -> None:
    ch = _make_channel()

    captured_msgs: list[MIMEText] = []

    def fake_send_sync(msg: MIMEText) -> None:
        captured_msgs.append(msg)

    with patch.object(ch, "_send_sync", side_effect=fake_send_sync):
        citations = [
            _citation(str(i), published_at="2026-01-01T00:00:00Z") for i in range(n)
        ]
        result = _result(citations[0], citations[1:])
        await ch.send(result, to="x@example.com", intent_name="My Intent")

    assert len(captured_msgs) == 1
    assert expected_word in captured_msgs[0]["Subject"]
    assert "My Intent" in captured_msgs[0]["Subject"]


# ---------------------------------------------------------------------------
# UT-8: smtp_use_ssl=True → SMTP_SSL used, SMTP and starttls not called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smtp_ssl_branch_uses_smtp_ssl() -> None:
    from sembr.notifier.email import EmailChannel
    settings = _make_settings()
    settings.smtp_use_ssl = True
    settings.smtp_use_starttls = False
    settings.smtp_port = 465
    ch = EmailChannel(settings)

    with patch("smtplib.SMTP_SSL") as mock_ssl, patch("smtplib.SMTP") as mock_plain:
        instance = MagicMock()
        instance.login = MagicMock()
        instance.send_message = MagicMock()
        instance.quit = MagicMock()
        mock_ssl.return_value = instance

        result = _result(_citation("a", published_at="2026-01-01T00:00:00Z"))
        await ch.send(result, to="x@example.com", intent_name="I")

    mock_ssl.assert_called_once_with("smtp.example.com", 465)
    mock_plain.assert_not_called()
    instance.starttls.assert_not_called()
