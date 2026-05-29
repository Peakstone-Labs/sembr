# SPDX-License-Identifier: Apache-2.0
"""Channel dispatch — shared by cron and on-demand (aggregate send) paths.

Extracted from ``main.py::_dispatch_notification`` so both callers reuse the
same ``isinstance`` per-channel loop without duplicating it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

    from sembr.notifier.email import EmailChannel, EmailChannelConfig
    from sembr.summarizer.models import SummaryResult

logger = logging.getLogger(__name__)


@dataclass
class ChannelOutcome:
    type: str
    ok: bool
    error: str | None


async def dispatch_summary(
    conn: aiosqlite.Connection,
    email_ch: EmailChannel,
    result: SummaryResult,
    *,
    strict: bool = False,
    subject: str | None = None,
) -> list[ChannelOutcome]:
    """Deliver *result* to every email channel configured on its intent.

    *strict=False* (cron default) — calls :meth:`EmailChannel.send` (never-raise),
    returns ``ok=True`` for every channel regardless of delivery outcome.  This
    preserves the existing cron-path invariant: a single broken channel must not
    abort the remaining groups in the same ``SummaryPipeline`` tick.

    *strict=True* (aggregate send path) — calls :meth:`EmailChannel.send_strict`,
    catches exceptions per-channel, and returns the real outcome for each.

    *subject* overrides the auto-generated email subject line and template date
    label.  When ``None`` the cron-digest default is used.
    """
    from sembr.db.intents import get_intent

    intent = await get_intent(conn, result.intent_id)
    if intent is None:
        return []

    outcomes: list[ChannelOutcome] = []
    for ch in intent.channels:
        if isinstance(ch, _email_config_type()):
            outcomes.append(
                await _dispatch_one(
                    email_ch, result, ch, intent.name, intent.timezone, strict, subject
                )
            )
    return outcomes


async def _dispatch_one(
    email_ch: EmailChannel,
    result: SummaryResult,
    config: EmailChannelConfig,
    intent_name: str,
    intent_timezone: str,
    strict: bool,
    subject: str | None = None,
) -> ChannelOutcome:
    channel_type = config.type
    if strict:
        try:
            await email_ch.send_strict(
                result,
                config=config,
                intent_name=intent_name,
                intent_timezone=intent_timezone,
                subject=subject,
            )
            return ChannelOutcome(type=channel_type, ok=True, error=None)
        except Exception as exc:
            return ChannelOutcome(type=channel_type, ok=False, error=str(exc))
    else:
        await email_ch.send(
            result,
            config=config,
            intent_name=intent_name,
            intent_timezone=intent_timezone,
            subject=subject,
        )
        return ChannelOutcome(type=channel_type, ok=True, error=None)


def _email_config_type() -> type:
    from sembr.notifier.email import EmailChannelConfig

    return EmailChannelConfig
