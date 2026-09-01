# SPDX-License-Identifier: Apache-2.0
"""Derived payload fields shared by every news write path.

Three writers put points into the two news collections — ingest
(``embedder/scheduler.py::_to_point``), the backfill job
(``maintenance/derived_backfill.py``) and the TTL migration
(``news_archive.py::build_archive_point``). They must all derive
``published_at_ts`` / ``body_len`` / ``lang`` / ``url_domain`` from the SAME
code: the unified search endpoint applies one filter surface across both
collections, so two implementations that drift by a character would make the
same filter mean different things at different points on the timeline.

Only the pure "payload in, derived keys out" step is shared. The surrounding
write orchestration (id mapping, vector assembly, batching, throttling) stays
with each caller — those genuinely differ.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit


def parse_published_at_ts(published_at: Any) -> int | None:
    """Parse the payload ``published_at`` ISO string to epoch seconds.

    Both collector paths (rss feedparser timestamps, newsapi dateTime) emit
    tz-aware UTC datetimes, so a naive string can only come from an unknown
    writer — treated as unparseable (field stays absent) instead of guessing
    a timezone and silently shifting the article by hours. Absent fields
    never match a Range filter, which is the intended "unknown time" query
    semantics.
    """
    if not published_at or not isinstance(published_at, str):
        return None
    try:
        dt = datetime.fromisoformat(published_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp())


def detect_lang(title: str, body: str) -> str:
    """Cheap zh/en/other tag from CJK-vs-latin letter counts.

    Samples the title plus the first 2000 body chars — enough to classify
    while keeping migration cost flat for very long articles. Only the CJK
    Unified Ideographs block counts as CJK: kana / hangul text lands in
    "other" (current sources are zh/en; revisit if ja/ko feeds arrive).
    """
    sample = f"{title} {body[:2000]}"
    cjk = sum(1 for ch in sample if "一" <= ch <= "鿿")
    latin = sum(1 for ch in sample if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))
    informative = cjk + latin
    if informative and cjk / informative >= 0.30:
        return "zh"
    if latin >= 20:
        return "en"
    return "other"


def extract_url_domain(url: Any) -> str | None:
    """Lowercased hostname without a leading ``www.``, or None."""
    if not url or not isinstance(url, str):
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().removeprefix("www.")
    return host or None


def build_derived_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Derived filter keys for one news payload — the single definition.

    ``body_len`` and ``lang`` are written unconditionally (``len("")`` is 0 and
    ``detect_lang`` always returns a tag), which is what lets the backfill job
    use ``IsEmpty("body_len")`` as a self-consuming queue: every processed
    point necessarily leaves the queue, so progress is monotonic and a
    malformed point can never be re-fetched forever.

    ``published_at_ts`` and ``url_domain`` are omitted when unparseable rather
    than written as None: an absent key never matches a Range/Match filter,
    which is the intended "unknown" semantics, whereas an explicit None would
    still occupy the field and read as a real value to anyone inspecting the
    payload.
    """
    title = payload.get("title")
    body = payload.get("body")
    title = title if isinstance(title, str) else ""
    body = body if isinstance(body, str) else ""

    derived: dict[str, Any] = {
        "body_len": len(body),
        "lang": detect_lang(title, body),
    }
    published_at_ts = parse_published_at_ts(payload.get("published_at"))
    if published_at_ts is not None:
        derived["published_at_ts"] = published_at_ts
    url_domain = extract_url_domain(payload.get("url"))
    if url_domain is not None:
        derived["url_domain"] = url_domain
    return derived
