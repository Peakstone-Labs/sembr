"""Unit tests for EmailChannel rendering, TZ conversion, citation anchors, SMTP."""
from __future__ import annotations

import smtplib
from email.message import Message
from email.mime.text import MIMEText
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sembr.summarizer.models import Citation, SummaryResult


def _extract_html(msg: Message) -> str:
    """Pull the text/html body out of a single-part MIMEText or multipart/related."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_payload(decode=True).decode("utf-8")
        raise AssertionError("no text/html part found in multipart message")
    return msg.get_payload(decode=True).decode("utf-8")


def _citation(
    article_id: str,
    title: str = "Test Article",
    url: str = "https://example.com/1",
    published_at: str | None = None,
    source_name: str | None = "Example Feed",
) -> Citation:
    return Citation(
        article_id=article_id,
        title=title,
        url=url,
        source=1,
        published_at=published_at,
        source_name=source_name,
    )


def _result(
    citations: list[Citation],
    intent_id: int = 1,
    summary: str = "Test summary.",
) -> SummaryResult:
    return SummaryResult(
        intent_id=intent_id,
        summary=summary,
        citations=list(citations),
        primary=citations[0] if citations else None,
        other_sources=citations[1:],
    )


def _make_settings(
    smtp_host: str = "smtp.example.com",
    display_timezone: str = "UTC",
) -> MagicMock:
    s = MagicMock()
    s.smtp_host = smtp_host
    s.smtp_port = 587
    s.smtp_username = "user@example.com"
    s.smtp_password.get_secret_value.return_value = "secret"
    s.smtp_from = ""
    s.smtp_use_starttls = True
    s.smtp_use_ssl = False
    s.display_timezone = display_timezone
    return s


def _make_channel(
    smtp_host: str = "smtp.example.com",
    display_timezone: str = "UTC",
):
    from sembr.notifier.email import EmailChannel
    return EmailChannel(_make_settings(smtp_host=smtp_host, display_timezone=display_timezone))


# ---------------------------------------------------------------------------
# UT-1: published_at rendered in configured timezone
# ---------------------------------------------------------------------------


def test_published_at_rendered_in_shanghai_tz() -> None:
    """A UTC timestamp should display in Asia/Shanghai when configured."""
    from sembr.notifier.email import _render_published_at
    from zoneinfo import ZoneInfo

    out = _render_published_at("2026-01-01T10:00:00Z", ZoneInfo("Asia/Shanghai"))
    # 10:00 UTC == 18:00 CST. Format includes TZ name; allow either CST or +0800.
    assert "2026-01-01" in out
    assert "18:00" in out


def test_published_at_naive_treated_as_utc() -> None:
    from sembr.notifier.email import _render_published_at
    from zoneinfo import ZoneInfo

    out = _render_published_at("2026-01-01T10:00:00", ZoneInfo("UTC"))
    assert "2026-01-01" in out
    assert "10:00" in out


def test_published_at_empty_returns_empty() -> None:
    from sembr.notifier.email import _render_published_at
    from zoneinfo import ZoneInfo

    assert _render_published_at(None, ZoneInfo("UTC")) == ""
    assert _render_published_at("", ZoneInfo("UTC")) == ""


def test_unknown_timezone_falls_back_to_utc() -> None:
    """display_timezone=garbage must not crash channel construction."""
    ch = _make_channel(display_timezone="Not/A/Real_Zone")
    # Construction succeeds and tz attr is UTC.
    assert ch._tz.key == "UTC"


# ---------------------------------------------------------------------------
# UT-2: citation anchors — valid [N] become <sup><a>; out-of-range dropped
# ---------------------------------------------------------------------------


def test_summary_inline_refs_render_as_anchors() -> None:
    from sembr.notifier.email import _summary_to_html

    html = str(_summary_to_html("Iran proposes reopening [1]. Multiple sources [2][3].", num_citations=3))
    assert 'href="#cite-1"' in html
    assert 'href="#cite-2"' in html
    assert 'href="#cite-3"' in html
    # Each ref wrapped in sup.cite-ref
    assert html.count("cite-ref") == 3


def test_summary_out_of_range_refs_are_dropped() -> None:
    """LLM hallucinating [99] when only 5 articles exist must not produce a dead link."""
    from sembr.notifier.email import _summary_to_html

    html = str(_summary_to_html("This is wrong [99].", num_citations=5))
    assert "[99]" not in html
    assert "cite-99" not in html


# ---------------------------------------------------------------------------
# UT-3: end-to-end render — citation list with index, source, time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_includes_indexed_sources() -> None:
    ch = _make_channel(display_timezone="UTC")
    citations = [
        _citation("a", title="Article One", url="https://ex.com/1",
                  published_at="2026-01-01T00:00:00Z", source_name="Reuters"),
        _citation("b", title="Article Two", url="https://ex.com/2",
                  published_at="2026-01-02T00:00:00Z", source_name="BBC"),
    ]
    result = _result(citations, summary="First fact [1]. Second fact [2].")

    captured: list[MIMEText] = []

    def fake_send_sync(msg: MIMEText) -> None:
        captured.append(msg)

    with patch.object(ch, "_send_sync", side_effect=fake_send_sync):
        await ch.send(result, to="x@example.com", intent_name="My Intent")

    assert len(captured) == 1
    body = _extract_html(captured[0])
    assert 'id="cite-1"' in body
    assert 'id="cite-2"' in body
    assert 'href="#cite-1"' in body
    assert 'href="#cite-2"' in body
    assert "Reuters" in body
    assert "BBC" in body
    # Date heading section "Sources" replaces the old per-day grouping
    assert ">Sources<" in body or "Sources" in body


# ---------------------------------------------------------------------------
# UT-3b: logo embedded as multipart/related with cid:sembr-logo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logo_embedded_as_inline_image() -> None:
    ch = _make_channel()
    citations = [_citation("a", published_at="2026-01-01T00:00:00Z")]
    result = _result(citations)

    captured: list = []
    with patch.object(ch, "_send_sync", side_effect=lambda m: captured.append(m)):
        await ch.send(result, to="x@example.com", intent_name="Intent")

    msg = captured[0]
    assert msg.is_multipart(), "expected multipart/related when logo present"
    image_parts = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(image_parts) == 1
    cid = image_parts[0].get("Content-ID", "")
    assert cid == "<sembr-logo>", f"Content-ID is {cid!r}"
    body = _extract_html(msg)
    assert 'src="cid:sembr-logo"' in body
    assert "Peakstone Labs" in body


# ---------------------------------------------------------------------------
# UT-4: XSS — title with <script> must be escaped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_xss_escape_in_title() -> None:
    ch = _make_channel()
    evil = _citation("x", title='<script>alert("xss")</script>', url="https://ex.com/x")
    result = _result([evil])

    captured: list[MIMEText] = []
    with patch.object(ch, "_send_sync", side_effect=lambda m: captured.append(m)):
        await ch.send(result, to="x@example.com", intent_name="Intent")

    body = _extract_html(captured[0])
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# UT-5: SMTP failure must not re-raise; quit() still called
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

        result = _result([_citation("a", published_at="2026-01-01T00:00:00Z")])
        await ch.send(result, to="dest@example.com", intent_name="Test")

    mock_logger.error.assert_called_once()
    instance.quit.assert_called_once()


# ---------------------------------------------------------------------------
# UT-6: smtp_host empty → early return, smtplib never called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_smtp_host_skips_send() -> None:
    ch = _make_channel(smtp_host="")

    with patch("smtplib.SMTP") as mock_smtp_cls, \
         patch("smtplib.SMTP_SSL") as mock_ssl_cls:
        result = _result([_citation("a")])
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
        result = _result(citations)
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

        result = _result([_citation("a", published_at="2026-01-01T00:00:00Z")])
        await ch.send(result, to="x@example.com", intent_name="I")

    mock_ssl.assert_called_once_with("smtp.example.com", 465)
    mock_plain.assert_not_called()
    instance.login.assert_called_once()
    instance.send_message.assert_called_once()
    instance.quit.assert_called_once()
    instance.starttls.assert_not_called()


# Silence unused-import warning when AsyncMock is referenced by some tests but not all.
_ = AsyncMock
