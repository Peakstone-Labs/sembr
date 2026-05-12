"""EmailChannel: Jinja2 HTML rendering + smtplib executor delivery."""

from __future__ import annotations

import asyncio
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown as _md
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pydantic import BaseModel, EmailStr, Field

from sembr.notifier.base import BaseChannel
from sembr.summarizer.templates import PROMPTS_DIR

if TYPE_CHECKING:
    from sembr.config import Settings
    from sembr.summarizer.models import Citation, SummaryResult


class EmailChannelConfig(BaseModel):
    """Per-intent email channel config — typed and validated at the API boundary.

    `to` is required (>=1 address). `cc`/`bcc` are optional. RFC-validated by
    pydantic.EmailStr; list bounds prevent fan-out abuse via a single intent.
    """

    type: Literal["email"] = "email"
    to: list[EmailStr] = Field(min_length=1, max_length=50)
    cc: list[EmailStr] = Field(default_factory=list, max_length=20)
    bcc: list[EmailStr] = Field(default_factory=list, max_length=20)


logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_LOGO_PATH = _TEMPLATES_DIR / "assets" / "logo.png"
_LOGO_CID = "sembr-logo"

# Read the logo once at import time. If the file is missing we still send the
# email; the template's <img> tag just degrades to an empty alt-text image,
# which is preferable to a hard failure in the notifier path.
try:
    _LOGO_BYTES: bytes | None = _LOGO_PATH.read_bytes()
except (FileNotFoundError, OSError) as _logo_exc:
    logger.warning(
        "logo not found at %s (%s); emails will render without a logo", _LOGO_PATH, _logo_exc
    )
    _LOGO_BYTES = None

# LLM output sometimes inlines ATX headings or list bullets without a leading
# blank line, which prevents python-markdown from recognising them as block
# elements. These regexes force such markers onto a fresh line so the same
# adapter works regardless of how loose the model's output is.
_HEADING_RE = re.compile(r"(?<!\n)\s*(#{1,6})[ \t]+")
_BULLET_RE = re.compile(r"(?<!\n)[ \t]+(?=\*\s+\*\*)")

# After markdown→HTML, replace each [N] with a superscript anchor link to the
# matching citation. Out-of-range N (LLM hallucination) is silently dropped per
# Q2: keep the prose readable, no broken anchors.
_INLINE_REF_RE = re.compile(r"\[(\d+)\]")


def _normalize_markdown(text: str) -> str:
    text = _HEADING_RE.sub(r"\n\n\1 ", text)
    text = _BULLET_RE.sub("\n\n", text)
    return text.strip()


def _summary_to_html(summary: str, num_citations: int) -> Markup:
    """Render the LLM's Markdown summary to safe-marked HTML for the template.

    `num_citations` bounds valid `[N]` references — anything outside [1, N] is
    stripped to avoid producing dead anchor links.
    """
    normalized = _normalize_markdown(summary)
    html = _md.markdown(normalized, extensions=["extra", "nl2br"], output_format="html")

    def _replace(match: re.Match[str]) -> str:
        n = int(match.group(1))
        if 1 <= n <= num_citations:
            return f'<sup class="cite-ref"><a href="#cite-{n}">[{n}]</a></sup>'
        return ""  # silent drop — LLM hallucinated an out-of-range index

    html = _INLINE_REF_RE.sub(_replace, html)
    return Markup(html)


@dataclass
class _RenderedCitation:
    """Display-ready citation: index, title, url, source label, datetime string, score."""

    index: int
    title: str
    url: str
    source_name: str
    datetime_display: str  # e.g. "2026-04-28 14:32" (intent timezone) or "" when unknown
    score_display: str  # e.g. "0.82" or "" when score not available


def _render_published_at(raw: str | None, tz: ZoneInfo) -> str:
    if not raw:
        return ""
    s = raw.strip()
    # datetime.fromisoformat accepts "Z" suffix on Python 3.11+. Strip it for
    # older runtimes too — we set requires-python==3.12.* but defence is cheap.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Date-only or unparseable: best effort — show the date prefix as-is.
        return raw[:10] if len(raw) >= 10 else raw
    if dt.tzinfo is None:
        # Naive timestamps from feeds are conventionally UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz)
    return local.strftime("%Y-%m-%d %H:%M")


def _format_score(score: float | None) -> str:
    if score is None:
        return ""
    return f"{score:.2f}"


def _build_rendered_citations(citations: list[Citation], tz: ZoneInfo) -> list[_RenderedCitation]:
    return [
        _RenderedCitation(
            index=i,
            title=c.title,
            url=c.url,
            source_name=c.source_name or "Unknown source",
            datetime_display=_render_published_at(c.published_at, tz),
            score_display=_format_score(c.score),
        )
        for i, c in enumerate(citations, 1)
    ]


def _resolve_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("timezone=%r not found; falling back to UTC", name)
        return ZoneInfo("UTC")


class EmailChannel(BaseChannel):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Fail fast at startup if template file is missing, not silently at send time.
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "jinja2"]),
        )

    async def send(
        self,
        result: SummaryResult,
        *,
        config: EmailChannelConfig,
        intent_name: str,
        intent_timezone: str,
    ) -> None:
        try:
            await self._send(
                result,
                config=config,
                intent_name=intent_name,
                intent_timezone=intent_timezone,
            )
        except Exception:
            logger.error(
                "EmailChannel.send failed for intent_id=%d to=%r",
                result.intent_id,
                list(config.to),
                exc_info=True,
            )

    async def _send(
        self,
        result: SummaryResult,
        *,
        config: EmailChannelConfig,
        intent_name: str,
        intent_timezone: str,
    ) -> None:
        s = self._settings
        if not s.smtp_host:
            logger.warning(
                "EmailChannel: smtp_host not configured, skipping send to %r", list(config.to)
            )
            return

        if result.citations:
            citations: list[Citation] = list(result.citations)
        elif result.primary is not None:
            citations = [result.primary, *result.other_sources]
        else:
            citations = []

        tz = _resolve_zoneinfo(intent_timezone)
        rendered = _build_rendered_citations(citations, tz)
        summary_html = _summary_to_html(result.summary, len(rendered))
        digest_date = datetime.now(tz).strftime("%Y%m%d")
        html_body = self._render_html(intent_name, summary_html, rendered, digest_date)

        subject = f"[Sembr] {intent_name} - {digest_date}"

        # multipart/related so the inline logo (cid:sembr-logo) is part of the
        # same MIME tree as the HTML. SpamAssassin's MIME_HTML_ONLY penalty
        # targets multipart/alternative with a missing text/plain — `related`
        # is unaffected. When the logo bytes are unavailable we still ship a
        # single-part HTML message (same as before).
        if _LOGO_BYTES is not None:
            msg: MIMEText | MIMEMultipart = MIMEMultipart("related")
            msg.attach(MIMEText(html_body, "html", "utf-8"))
            img = MIMEImage(_LOGO_BYTES, "png")
            img.add_header("Content-ID", f"<{_LOGO_CID}>")
            img.add_header("Content-Disposition", "inline", filename="logo.png")
            msg.attach(img)
        else:
            msg = MIMEText(html_body, "html", "utf-8")

        msg["Subject"] = subject
        msg["From"] = s.smtp_from or s.smtp_username
        # EmailStr is a subclass of str; explicit cast keeps the smtplib API contract clean.
        to_addrs = [str(a) for a in config.to]
        cc_addrs = [str(a) for a in config.cc]
        bcc_addrs = [str(a) for a in config.bcc]
        msg["To"] = ", ".join(to_addrs)
        if cc_addrs:
            msg["Cc"] = ", ".join(cc_addrs)
        # Bcc is intentionally NOT placed in headers — it only goes into the
        # SMTP envelope (RCPT TO) so recipients can't see who's copied.
        all_rcpts = [*to_addrs, *cc_addrs, *bcc_addrs]

        # asyncio.to_thread is the 3.9+ idiomatic way to offload sync-blocking I/O;
        # get_event_loop() is deprecated in 3.12 when called from a running coroutine.
        await asyncio.to_thread(self._send_sync, msg, all_rcpts)

    def _render_html(
        self,
        intent_name: str,
        summary_html: Markup,
        citations: list[_RenderedCitation],
        digest_date: str,
    ) -> str:
        tmpl = self._env.get_template("email_digest.html.jinja2")
        return tmpl.render(
            intent_name=intent_name,
            summary_html=summary_html,
            citations=citations,
            digest_date=digest_date,
        )

    async def send_error(
        self,
        intent_name: str,
        kind: str,
        name: str,
        reason: str,
        *,
        config: EmailChannelConfig,
    ) -> None:
        """Send a template-error notification email. Never raises."""
        try:
            await self._send_error(intent_name, kind, name, reason, config=config)
        except Exception:
            logger.error(
                "EmailChannel.send_error failed for intent=%r template=%s/%s",
                intent_name,
                kind,
                name,
                exc_info=True,
            )

    async def _send_error(
        self,
        intent_name: str,
        kind: str,
        name: str,
        reason: str,
        *,
        config: EmailChannelConfig,
    ) -> None:
        s = self._settings
        if not s.smtp_host:
            logger.warning(
                "EmailChannel.send_error: smtp_host not configured, skipping for intent=%r",
                intent_name,
            )
            return

        short_reason = reason.split("\n")[0][:120]
        subject = f"[Sembr][error] {intent_name} — {kind} template '{name}' — {short_reason}"
        html_body = self._render_error_html(intent_name, kind, name, reason)
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = s.smtp_from or s.smtp_username
        to_addrs = [str(a) for a in config.to]
        cc_addrs = [str(a) for a in config.cc]
        bcc_addrs = [str(a) for a in config.bcc]
        msg["To"] = ", ".join(to_addrs)
        if cc_addrs:
            msg["Cc"] = ", ".join(cc_addrs)
        all_rcpts = [*to_addrs, *cc_addrs, *bcc_addrs]
        await asyncio.to_thread(self._send_sync, msg, all_rcpts)

    def _render_error_html(
        self,
        intent_name: str,
        kind: str,
        name: str,
        reason: str,
    ) -> str:
        tmpl = self._env.get_template("email_template_error.html.jinja2")
        return tmpl.render(
            intent_name=intent_name,
            kind=kind,
            name=name,
            reason=reason,
            prompts_dir=PROMPTS_DIR.as_posix(),
        )

    def _send_sync(self, msg, rcpts: list[str]) -> None:  # MIMEText | MIMEMultipart
        s = self._settings
        if s.smtp_use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port)
            if s.smtp_use_starttls:
                server.starttls()
        try:
            if s.smtp_username:
                server.login(s.smtp_username, s.smtp_password.get_secret_value())
            # Pass `to_addrs=rcpts` explicitly so Bcc receivers get the message
            # without appearing in headers; without this, send_message would
            # only RCPT-TO the addresses present in To/Cc headers.
            server.send_message(msg, to_addrs=rcpts)
        finally:
            # Suppress quit() errors so they don't shadow the original send/login exception.
            try:
                server.quit()
            except smtplib.SMTPException:
                pass
