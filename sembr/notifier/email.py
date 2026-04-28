"""EmailChannel: Jinja2 HTML rendering + smtplib executor delivery."""
from __future__ import annotations

import asyncio
import logging
import smtplib
from datetime import date
from email.mime.text import MIMEText
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader, select_autoescape

from sembr.notifier.base import BaseChannel

if TYPE_CHECKING:
    from sembr.config import Settings
    from sembr.summarizer.models import Citation, SummaryResult

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class EmailChannel(BaseChannel):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
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

        all_citations: list[Citation] = [result.primary, *result.other_sources]
        grouped = self._group_by_date(all_citations)
        html_body = self._render_html(result, grouped, intent_name)

        n = len(all_citations)
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

    def _group_by_date(
        self, citations: list[Citation]
    ) -> list[tuple[date | None, list[Citation]]]:
        buckets: dict[date | None, list[Citation]] = {}
        for c in citations:
            d: date | None = None
            if c.published_at:
                try:
                    d = date.fromisoformat(c.published_at[:10])
                except (ValueError, TypeError):
                    d = None
            buckets.setdefault(d, []).append(c)

        known = sorted(k for k in buckets if k is not None)
        result: list[tuple[date | None, list[Citation]]] = [(k, buckets[k]) for k in known]
        if None in buckets:
            result.append((None, buckets[None]))
        return result

    def _render_html(
        self,
        result: SummaryResult,
        grouped: list[tuple[date | None, list[Citation]]],
        intent_name: str,
    ) -> str:
        tmpl = self._env.get_template("email_digest.html.jinja2")
        return tmpl.render(
            intent_name=intent_name,
            summary=result.summary,
            grouped=grouped,
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
