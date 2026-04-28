"""EmailChannel: Jinja2 HTML rendering + smtplib executor delivery."""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown as _md
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from sembr.notifier.base import BaseChannel

if TYPE_CHECKING:
    from sembr.config import Settings
    from sembr.summarizer.models import Citation, SummaryResult

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"

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
    """Display-ready citation: index, title, url, source label, datetime string."""

    index: int
    title: str
    url: str
    source_name: str
    datetime_display: str  # e.g. "2026-04-28 14:32 CST" or "" when unknown


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
    return local.strftime("%Y-%m-%d %H:%M %Z")


def _build_rendered_citations(
    citations: list[Citation], tz: ZoneInfo
) -> list[_RenderedCitation]:
    return [
        _RenderedCitation(
            index=i,
            title=c.title,
            url=c.url,
            source_name=c.source_name or "Unknown source",
            datetime_display=_render_published_at(c.published_at, tz),
        )
        for i, c in enumerate(citations, 1)
    ]


def _resolve_zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("display_timezone=%r not found; falling back to UTC", name)
        return ZoneInfo("UTC")


class EmailChannel(BaseChannel):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tz = _resolve_zoneinfo(settings.display_timezone)
        # Fail fast at startup if template file is missing, not silently at send time.
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "jinja2"]),
        )

    async def send(self, result: SummaryResult, *, to: str, intent_name: str) -> None:
        try:
            await self._send(result, to=to, intent_name=intent_name)
        except Exception:
            logger.error(
                "EmailChannel.send failed for intent_id=%d to=%s",
                result.intent_id,
                to,
                exc_info=True,
            )

    async def _send(self, result: SummaryResult, *, to: str, intent_name: str) -> None:
        s = self._settings
        if not s.smtp_host:
            logger.warning("EmailChannel: smtp_host not configured, skipping send to %s", to)
            return

        # Prefer the canonical citations list; fall back to primary+other_sources for
        # callers built before that field existed.
        if result.citations:
            citations: list[Citation] = list(result.citations)
        elif result.primary is not None:
            citations = [result.primary, *result.other_sources]
        else:
            citations = []

        rendered = _build_rendered_citations(citations, self._tz)
        summary_html = _summary_to_html(result.summary, len(rendered))
        html_body = self._render_html(intent_name, summary_html, rendered)

        n = len(rendered)
        article_word = "article" if n == 1 else "articles"
        subject = f"[sembr] {intent_name} — {n} matched {article_word}"

        # Single-part HTML message; multipart/alternative with only one part
        # misleads anti-spam filters (SpamAssassin MIME_HTML_ONLY penalty).
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = s.smtp_from or s.smtp_username
        msg["To"] = to

        # asyncio.to_thread is the 3.9+ idiomatic way to offload sync-blocking I/O;
        # get_event_loop() is deprecated in 3.12 when called from a running coroutine.
        await asyncio.to_thread(self._send_sync, msg)

    def _render_html(
        self,
        intent_name: str,
        summary_html: Markup,
        citations: list[_RenderedCitation],
    ) -> str:
        tmpl = self._env.get_template("email_digest.html.jinja2")
        return tmpl.render(
            intent_name=intent_name,
            summary_html=summary_html,
            citations=citations,
        )

    def _send_sync(self, msg: MIMEText) -> None:
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
            server.send_message(msg)
        finally:
            # Suppress quit() errors so they don't shadow the original send/login exception.
            try:
                server.quit()
            except smtplib.SMTPException:
                pass

