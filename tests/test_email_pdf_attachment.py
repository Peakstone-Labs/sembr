# SPDX-License-Identifier: Apache-2.0
"""Tests for the opt-in PDF attachment on EmailChannel digests.

Structure/contract tests mock `_generate_pdf_bytes` so they run without the
WeasyPrint native toolchain. Tests that assert on real PDF content are gated by
`requires_pdf`, which skips when WeasyPrint's native libraries are unavailable
(the full Latin+CJK render is also exercised by the manual Docker round-trip).
"""

from __future__ import annotations

import io
import os
from datetime import datetime
from email.message import Message
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from sembr.notifier.email import EmailChannelConfig, _safe_slug
from sembr.summarizer.models import Citation, SummaryResult

_FAKE_PDF = b"%PDF-1.7\nfake pdf payload\n%%EOF"

# Real WeasyPrint renders are opt-in via SEMBR_PDF_RENDER_TESTS=1 and meant to run
# locally / inside Docker — never in CI. Importing and running WeasyPrint (native
# Pango/Cairo) inside the test process leaves a lingering non-daemon thread on
# Linux that blocks interpreter shutdown, hanging the CI job long after the suite
# has reported its results. CI therefore exercises only the mocked structure and
# contract tests; the actual render path is validated locally and via the Docker
# round-trip. macOS exits cleanly, so local opt-in is safe.
_RUN_PDF_RENDER_TESTS = os.environ.get("SEMBR_PDF_RENDER_TESTS") == "1"


def _weasyprint_renders() -> bool:
    """True only if real-render tests are opted in AND WeasyPrint can render here."""
    if not _RUN_PDF_RENDER_TESTS:
        return False
    try:
        from weasyprint import HTML

        return HTML(string="<p>x</p>").write_pdf().startswith(b"%PDF-")
    except Exception:
        return False


requires_pdf = pytest.mark.skipif(
    not _weasyprint_renders(),
    reason="real-render tests need SEMBR_PDF_RENDER_TESTS=1 plus WeasyPrint native libs",
)


def _cfg(*to: str, attach_pdf: bool = False) -> EmailChannelConfig:
    return EmailChannelConfig(to=list(to) or ["x@example.com"], attach_pdf=attach_pdf)


def _citation(
    article_id: str = "a",
    title: str = "Test Article",
    url: str = "https://example.com/article",
    published_at: str | None = "2026-01-01T00:00:00Z",
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
    summary: str = "Test summary.", citations: list[Citation] | None = None
) -> SummaryResult:
    cits = citations if citations is not None else [_citation()]
    return SummaryResult(
        intent_id=1,
        summary=summary,
        citations=list(cits),
        primary=cits[0] if cits else None,
        other_sources=cits[1:],
    )


def _make_channel():
    from sembr.notifier.email import EmailChannel

    s = MagicMock()
    s.smtp_host = "smtp.example.com"
    s.smtp_port = 587
    s.smtp_username = "user@example.com"
    s.smtp_password.get_secret_value.return_value = "secret"
    s.smtp_from = ""
    s.smtp_use_starttls = True
    s.smtp_use_ssl = False
    return EmailChannel(s)


def _pdf_part(msg: Message):
    for part in msg.walk():
        if part.get_content_type() == "application/pdf":
            return part
    return None


# ---------------------------------------------------------------------------
# Message structure: default vs opt-in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_pdf_false_message_single_part() -> None:
    """Default (attach_pdf=False) keeps the historical single-part text/html."""
    ch = _make_channel()
    captured: list[Message] = []
    with patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)):
        await ch.send(
            _result(),
            config=_cfg("x@example.com", attach_pdf=False),
            intent_name="Intent",
            intent_timezone="UTC",
        )
    msg = captured[0]
    assert not msg.is_multipart()
    assert _pdf_part(msg) is None


@pytest.mark.asyncio
async def test_attach_pdf_true_message_is_multipart() -> None:
    """attach_pdf=True wraps html+pdf in multipart/mixed with an attachment part."""
    ch = _make_channel()
    captured: list[Message] = []
    with (
        patch.object(ch, "_generate_pdf_bytes", return_value=_FAKE_PDF),
        patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)),
    ):
        await ch.send(
            _result(),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="Intent",
            intent_timezone="UTC",
        )
    msg = captured[0]
    assert msg.is_multipart()
    assert msg.get_content_subtype() == "mixed"
    # html part still present and findable for downstream readers
    html_parts = [p for p in msg.walk() if p.get_content_type() == "text/html"]
    assert len(html_parts) == 1
    pdf = _pdf_part(msg)
    assert pdf is not None
    assert pdf.get_content_type() == "application/pdf"
    assert pdf.get("Content-Disposition", "").startswith("attachment")


@pytest.mark.asyncio
async def test_pdf_attachment_filename() -> None:
    """Filename is sembr_<slug>_<date>.pdf; subject must not influence it."""
    ch = _make_channel()
    captured: list[Message] = []
    with (
        patch.object(ch, "_generate_pdf_bytes", return_value=_FAKE_PDF),
        patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)),
    ):
        await ch.send(
            _result(),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="My Intent",
            intent_timezone="UTC",
            subject="A Totally Custom Subject 2099",
        )
    expected_date = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%d")
    fname = _pdf_part(captured[0]).get_filename()
    assert fname == f"sembr_My_Intent_{expected_date}.pdf"
    # subject text must not leak into the filename
    assert "Custom" not in fname
    assert "2099" not in fname


# ---------------------------------------------------------------------------
# Failure contract: send swallows, send_strict raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_strict_raises_on_pdf_failure() -> None:
    ch = _make_channel()
    with (
        patch.object(ch, "_generate_pdf_bytes", side_effect=RuntimeError("render boom")),
        patch.object(ch, "_send_sync"),
        pytest.raises(RuntimeError, match="render boom"),
    ):
        await ch.send_strict(
            _result(),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="Intent",
            intent_timezone="UTC",
        )


@pytest.mark.asyncio
async def test_send_continues_on_pdf_failure() -> None:
    """Non-strict send still delivers (single-part, no attachment) on PDF error."""
    ch = _make_channel()
    captured: list[Message] = []
    with (
        patch.object(ch, "_generate_pdf_bytes", side_effect=RuntimeError("render boom")),
        patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)),
    ):
        await ch.send(
            _result(),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="Intent",
            intent_timezone="UTC",
        )
    # delivery happened despite PDF failure, and the message has no attachment
    assert len(captured) == 1
    assert not captured[0].is_multipart()
    assert _pdf_part(captured[0]) is None


# ---------------------------------------------------------------------------
# Slug safety
# ---------------------------------------------------------------------------


def test_slug_cjk_collapsed() -> None:
    # Entirely non-ASCII collapses to the stable fallback.
    assert _safe_slug("中文监控") == "digest"
    # Non-alphanumeric runs collapse to single underscores.
    assert _safe_slug("My Feed!!! News") == "My_Feed_News"
    # Leading/trailing separators are trimmed.
    assert _safe_slug("  a  b  ") == "a_b"


# ---------------------------------------------------------------------------
# Real WeasyPrint render (skipped where native libs are unavailable)
# ---------------------------------------------------------------------------


@requires_pdf
@pytest.mark.asyncio
async def test_pdf_attachment_magic_bytes() -> None:
    """A real render produces a valid PDF payload starting with %PDF-."""
    ch = _make_channel()
    captured: list[Message] = []
    with patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)):
        await ch.send(
            _result(summary="Quarterly revenue surged this period [1]."),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="Intent",
            intent_timezone="UTC",
        )
    payload = _pdf_part(captured[0]).get_payload(decode=True)
    assert payload.startswith(b"%PDF-")


@requires_pdf
@pytest.mark.asyncio
async def test_pdf_contains_summary_text() -> None:
    """Summary prose is present in the rendered PDF's extractable text."""
    from pypdf import PdfReader

    ch = _make_channel()
    captured: list[Message] = []
    with patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)):
        await ch.send(
            _result(summary="Photosynthesis breakthrough reported by labs [1]."),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="Science Watch",
            intent_timezone="UTC",
        )
    payload = _pdf_part(captured[0]).get_payload(decode=True)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(payload)).pages)
    assert "Photosynthesis" in text
    assert "Science Watch" in text


@requires_pdf
@pytest.mark.asyncio
async def test_pdf_truncates_overlong_summary() -> None:
    """An over-long summary is truncated with a marker rather than ballooning."""
    from pypdf import PdfReader

    ch = _make_channel()
    long_summary = "word " * 9000 + "TAILMARKER"  # ~45k chars, well past the cap
    captured: list[Message] = []
    with patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)):
        await ch.send(
            _result(summary=long_summary),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="Intent",
            intent_timezone="UTC",
        )
    payload = _pdf_part(captured[0]).get_payload(decode=True)
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(payload)).pages)
    assert "truncated for PDF" in text
    assert "TAILMARKER" not in text


@requires_pdf
@pytest.mark.asyncio
async def test_pdf_truncation_safe_across_markup_boundary() -> None:
    """Truncating markup-dense markdown still yields a valid PDF (no broken HTML).

    The cap lands somewhere inside repeated link/bold/citation markdown. Because
    truncation happens on the source markdown before rendering, the renderer still
    emits well-formed HTML and WeasyPrint produces a valid document.
    """
    from pypdf import PdfReader

    ch = _make_channel()
    # ~70k chars of markup; the truncation point falls mid-construct.
    summary = "**bold** [ref](https://example.com/x) body [1] " * 1500
    captured: list[Message] = []
    with patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)):
        await ch.send(
            _result(summary=summary),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="Intent",
            intent_timezone="UTC",
        )
    payload = _pdf_part(captured[0]).get_payload(decode=True)
    assert payload.startswith(b"%PDF-")
    text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(payload)).pages)
    assert "truncated for PDF" in text


# ---------------------------------------------------------------------------
# QA-owner tests (SC1, SC5)
# ---------------------------------------------------------------------------


def test_attach_pdf_config_default_false() -> None:
    """SC1: EmailChannelConfig defaults attach_pdf to False without explicit opt-in."""
    config = EmailChannelConfig(to=["x@x.com"])
    assert config.attach_pdf is False


@requires_pdf
@pytest.mark.asyncio
async def test_pdf_contains_source_url() -> None:
    """SC5: Citation source URL appears in the rendered PDF.

    WeasyPrint renders the URL as both visible link text and a link annotation.
    We assert against extracted text (reliable) and also check link annotations
    as a secondary path for cases where kerning splits the visible text.
    """
    from pypdf import PdfReader

    ch = _make_channel()
    captured: list[Message] = []
    with patch.object(ch, "_send_sync", side_effect=lambda m, _r: captured.append(m)):
        await ch.send(
            _result(
                summary="Notable developments this quarter [1].",
                citations=[_citation(url="https://example.com/article")],
            ),
            config=_cfg("x@example.com", attach_pdf=True),
            intent_name="QA Source URL Check",
            intent_timezone="UTC",
        )
    payload = _pdf_part(captured[0]).get_payload(decode=True)
    assert payload.startswith(b"%PDF-")

    reader = PdfReader(io.BytesIO(payload))
    # Primary check: URL visible as text in the rendered page
    full_text = "".join(p.extract_text() for p in reader.pages)
    # Secondary check: URL present as a hyperlink annotation
    annotation_uris: list[str] = []
    for page in reader.pages:
        if "/Annots" in page:
            for annot_ref in page["/Annots"]:
                obj = annot_ref.get_object()
                if "/A" in obj:
                    uri = obj["/A"].get("/URI", "")
                    if uri:
                        annotation_uris.append(uri)

    assert (
        "https://example.com/article" in full_text
        or "https://example.com/article" in annotation_uris
    ), (
        f"Citation URL not found in PDF text or annotations. "
        f"Text snippet: {full_text[:300]!r}, "
        f"Annotation URIs: {annotation_uris}"
    )
