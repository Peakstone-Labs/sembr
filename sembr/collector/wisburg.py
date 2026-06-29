# SPDX-License-Identifier: Apache-2.0
"""Wisburg open-API source (research-report note streams).

``WisburgSource`` is a per-feed ``BaseSource`` covering three structurally
identical endpoints (list + per-id detail, both behind one Bearer key):
``/api/reports``, ``/api/earningscalls``, ``/api/am-reports``. The endpoint
identity is encoded in ``feed.url`` (a member of ``ENDPOINT_URLS``) so the
feeds-table UNIQUE constraint blocks duplicate feeds per endpoint and
``HostLimiter`` groups all wisburg feeds onto one host semaphore.

Wire shape (probed against the live API, 2026-06-10):

- list  ``GET <endpoint>?first=100&startTime=<iso>&after=<cursor>`` returns
  only ``{id,title,datetime}`` per item — the body requires one extra
  ``GET <endpoint>/<id>`` per item (N+1). Detail returns
  ``{id,title,datetime,url,summary}`` where ``summary`` is markdown, plus
  an optional ``meta`` object ``{name,description}`` — a per-article
  publisher + provenance line added upstream ~2026-06-27 (probed: present on
  reports, name-only on am-reports, absent on earningscalls). We fold it into
  the body via ``_meta_preamble`` so the map-extraction LLM can attribute the
  real institution to ``source_org`` instead of the generic feed label.
- every response is wrapped in ``{request_id,code,status,message,data}``;
  ``code==200 and status==0`` is the only success shape.
- ``end_cursor`` is an offset counter; deep pagination fails with code 1004,
  so incremental sync leans on ``startTime`` watermarks, never deep paging.
- rate limit 1000 req/h (response headers; not in their docs).

Failure semantics: the cursor in ``collect_feed`` advances
unconditionally after a successful fetch, so returning a partial batch would
permanently lose the failed items. Any transient failure (HTTP error,
timeout, bad envelope) therefore raises ``FetchError`` — nothing was
inserted this round, and the next tick retries the same window for free.
Only per-item terminal misses (detail 404/410, empty summary) are skipped.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from sembr.collector.base import BaseSource, RawArticle
from sembr.collector.rss import FetchError
from sembr.config import get_settings

logger = logging.getLogger(__name__)

WISBURG_BASE_URL = "https://api-omen.wisburg.com"

# The three streams shipped in this round share one source_type because their
# list/detail schemas are identical; a future wisburg stream with a different
# shape gets its own source_type.
ENDPOINT_URLS: frozenset[str] = frozenset(
    f"{WISBURG_BASE_URL}/api/{slug}" for slug in ("reports", "earningscalls", "am-reports")
)

# Window constants. Module constants, not Settings —
# they encode probed upstream behaviour, not user preference.
#
# _OVERLAP: wisburg's `datetime` looks like batch-ingestion time (items
# cluster in the same minute), but that's unverified upstream semantics; the
# overlap re-reads the trailing hour so late-stamped items aren't lost to an
# exact-since boundary. MD5 dedup absorbs the re-reads at insert time.
_OVERLAP = timedelta(hours=1)
# _MAX_WINDOW: clamp for stale cursors (feed re-enabled after weeks). Keeps
# the worst-case window aligned with the "no historical backfill" requirement
# and bounds the N+1 detail spend against the 1000 req/h budget.
_MAX_WINDOW = timedelta(days=7)
# _FIRST_PULL_WINDOW: since=None bootstrap, matching newsapi's now-1d
# first-pull philosophy (collector/newsapi.py:_date_window).
_FIRST_PULL_WINDOW = timedelta(days=1)
_PAGE_SIZE = 100
# Defensive list-pagination cap: 5 × 100 sits far above the probed daily
# volume (tens/endpoint) yet keeps a runaway window from walking into the
# code-1004 offset limit. Hitting it delivers what we have + a loud warning
# (raising instead would deadlock the feed: same window, same cap, forever).
_MAX_PAGES = 5


def normalize_wisburg_url(s: str) -> str:
    """Shared normalizer for the ``FeedCreate.url`` wisburg-report branch.

    Lowercase is safe here (host is case-insensitive, whitelisted paths are
    all-lowercase slugs); plus scheme upgrade and trailing-slash strip so
    pasted variants land on the canonical ``ENDPOINT_URLS`` member.
    """
    out = s.strip().lower().rstrip("/")
    if out.startswith("http://"):
        out = "https://" + out[len("http://") :]
    return out


def _md5_url_title(url: str, title: str) -> str:
    # Same algorithm as collector.rss / collector.newsapi so the fingerprint
    # space stays unified across source_types.
    return hashlib.md5((url + title).encode(), usedforsecurity=False).hexdigest()


def _parse_datetime_or_none(raw: Any) -> datetime | None:
    """Wisburg ``datetime`` is ISO with offset (e.g. '+08:00'). Tolerant
    parse → UTC aware; anything malformed degrades to None rather than
    poisoning the batch (mirrors newsapi's published_at contract)."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _meta_preamble(detail: dict[str, Any]) -> str:
    """Path-A source enrichment: render the detail's ``meta`` object as a short
    markdown preamble to prepend to the body.

    Wisburg detail grew an optional per-article ``meta`` (``{name,description}``)
    where ``name`` is the publishing institution (e.g. '花旗') and
    ``description`` is provenance prose (analyst, page count, publish date).
    Surfacing it at the top of the body lets the map-extraction LLM lift the
    institution into ``source_org`` rather than falling back to the generic feed
    label ('外资研报'). Prepended, not appended, so it survives the downstream
    body-length cap (db/articles.py).

    Upstream coverage is uneven — ``meta`` is absent on earningscalls, name-only
    on am-reports — so each field degrades to omission, never a stub line; a
    missing/malformed ``meta`` yields an empty string (body unchanged).
    """
    meta = detail.get("meta")
    if not isinstance(meta, dict):
        return ""
    lines: list[str] = []
    name = str(meta.get("name") or "").strip()
    desc = str(meta.get("description") or "").strip()
    if name:
        lines.append(f"> 发布机构：{name}")
    if desc:
        lines.append(f"> 来源说明：{desc}")
    return "\n".join(lines) + "\n\n" if lines else ""


def _unwrap_envelope(data: Any, *, where: str) -> Any:
    """Validate the uniform ``{code,status,data}`` envelope; return ``data``.

    A 2xx HTTP response with a non-success envelope means the upstream
    contract drifted (or the key lost permission mid-flight) — surface it as
    FetchError instead of silently treating it as an empty result.
    """
    if not isinstance(data, dict):
        raise FetchError(f"wisburg {where}: response is not a JSON object")
    code = data.get("code")
    status = data.get("status")
    if code != 200 or status != 0:
        raise FetchError(
            f"wisburg {where}: envelope code={code!r} status={status!r} "
            f"message={data.get('message')!r}"
        )
    return data.get("data")


class WisburgSource(BaseSource):
    """Same ``__init__(url, timeout)`` shape as RSSSource/NewsApiSource so
    ``collect_feed`` (scheduler.py:127) and the fire path (feeds_fire.py:55)
    construct it without a source_type branch."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self._url = normalize_wisburg_url(url)
        self._timeout = timeout

    async def fetch(self, since: datetime | None = None) -> list[RawArticle]:
        api_key = get_settings().wisburg_api_key.get_secret_value()
        if not api_key:
            # Configuration error expressed as FetchError so the collect_feed
            # contract (no cursor advance, fetch_event ok=False) holds —
            # same shape as the newsapi empty-key path.
            raise FetchError("WISBURG_API_KEY is empty; cannot fetch wisburg feed")
        if self._url not in ENDPOINT_URLS:
            # FeedCreate validates on write, but feeds created before this
            # guard (or rows edited out-of-band) must not silently GET an
            # arbitrary URL with our Bearer key attached.
            raise FetchError(f"wisburg feed url {self._url!r} is not a known endpoint")

        now = datetime.now(UTC)
        start = (now - _FIRST_PULL_WINDOW) if since is None else (since - _OVERLAP)
        floor = now - _MAX_WINDOW
        if start < floor:
            logger.warning(
                "wisburg fetch[%s]: window start %s clamped to %s (stale cursor; "
                "items between them are skipped by design — no historical backfill)",
                self._url,
                start.isoformat(),
                floor.isoformat(),
            )
            start = floor

        headers = {"Authorization": f"Bearer {api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
            items = await self._fetch_list(client, start)
            articles = await self._fetch_details(client, items)
        if items and not articles:
            # Whole-window skip (all details missing title/summary). The
            # cursor still advances after we return, so these items only get
            # retried while the _OVERLAP window covers them — leave a loud
            # breadcrumb for the periodic ingest-gap audit.
            logger.warning(
                "wisburg fetch[%s]: %d list items but 0 delivered (all skipped); "
                "cursor will still advance — items rely on the %s overlap to be "
                "retried next tick",
                self._url,
                len(items),
                _OVERLAP,
            )
        return articles

    async def _fetch_list(self, client: httpx.AsyncClient, start: datetime) -> list[dict]:
        """Walk the offset cursor until an empty/short page or _MAX_PAGES."""
        items: list[dict] = []
        after: str | None = None
        for page in range(1, _MAX_PAGES + 1):
            params: dict[str, Any] = {"first": _PAGE_SIZE, "startTime": start.isoformat()}
            if after is not None:
                params["after"] = after
            try:
                resp = await client.get(self._url, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise FetchError(
                    f"wisburg list HTTP error for {self._url!r}: {type(exc).__name__}: {exc!s}"
                ) from exc
            try:
                payload = resp.json()
            except ValueError as exc:
                raise FetchError(
                    f"wisburg list JSON parse failed for {self._url!r}: {exc}"
                ) from exc
            data = _unwrap_envelope(payload, where=f"list[{self._url}]")
            page_items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(page_items, list):
                raise FetchError(f"wisburg list[{self._url}]: data.items is not a list")
            items.extend(it for it in page_items if isinstance(it, dict))
            if len(page_items) < _PAGE_SIZE:
                # Short page = last page under offset pagination; saves the
                # extra empty-page request every poll.
                return items
            if page == _MAX_PAGES:
                logger.warning(
                    "wisburg fetch[%s]: pagination cap %d pages reached "
                    "(window start=%s); delivering first %d items, tail dropped",
                    self._url,
                    _MAX_PAGES,
                    start.isoformat(),
                    len(items),
                )
                return items
            page_info = data.get("page_info") if isinstance(data, dict) else None
            after = page_info.get("end_cursor") if isinstance(page_info, dict) else None
            if not after:
                # Full page but no cursor to continue — treat as exhausted.
                return items
        return items

    async def _fetch_details(
        self, client: httpx.AsyncClient, items: list[dict]
    ) -> list[RawArticle]:
        """Sequential N+1 detail pass (~300ms/req × tens of items per day;
        serialism + HostLimiter makes 429 practically unreachable)."""
        articles: list[RawArticle] = []
        bad_id_count = 0
        for item in items:
            item_id = item.get("id")
            if not isinstance(item_id, int):
                # Isolated malformed item: skip. But if EVERY item has a
                # non-int id the upstream id contract drifted — that's
                # handled after the loop as a FetchError (same tier as the
                # envelope guards), not a silent empty success that
                # would advance the cursor past the whole window.
                bad_id_count += 1
                logger.warning("wisburg fetch[%s]: list item without int id: %r", self._url, item)
                continue
            detail_url = f"{self._url}/{item_id}"
            try:
                resp = await client.get(detail_url)
                if resp.status_code in (404, 410):
                    # Item deleted upstream — retrying next tick can't help,
                    # and FetchError here would wedge the feed on a dead id.
                    logger.warning(
                        "wisburg fetch[%s]: detail %d → %d, skipped",
                        self._url,
                        item_id,
                        resp.status_code,
                    )
                    continue
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise FetchError(
                    f"wisburg detail HTTP error for {detail_url!r}: {type(exc).__name__}: {exc!s}"
                ) from exc
            try:
                payload = resp.json()
            except ValueError as exc:
                raise FetchError(
                    f"wisburg detail JSON parse failed for {detail_url!r}: {exc}"
                ) from exc
            detail = _unwrap_envelope(payload, where=f"detail[{detail_url}]")
            if not isinstance(detail, dict):
                raise FetchError(f"wisburg detail[{detail_url}]: data is not an object")

            title = str(detail.get("title") or "").strip()
            summary = str(detail.get("summary") or "").strip()
            if not title:
                logger.warning(
                    "wisburg fetch[%s]: detail %d has no title, skipped", self._url, item_id
                )
                continue
            if not summary:
                # Skip rather than insert a stub: MD5 dedup would lock the
                # stub in forever; the 1h overlap re-reads the
                # item next tick once upstream fills the summary in.
                logger.warning(
                    "wisburg fetch[%s]: detail %d has empty summary, skipped", self._url, item_id
                )
                continue

            articles.append(
                RawArticle(
                    # API detail URL: stable, unique per id, and a real
                    # resolvable resource — chosen over a guessed
                    # www.wisburg.com page link (that route does not
                    # exist; a later pattern change would also shift the
                    # md5 fingerprint and re-import everything).
                    url=detail_url,
                    title=title,
                    # Prepend the meta source line (institution + provenance)
                    # so the map-extraction LLM can attribute source_org; no-op
                    # when meta is absent (earningscalls) — body == summary.
                    body=_meta_preamble(detail) + summary,
                    content_quality="summary",
                    published_at=_parse_datetime_or_none(detail.get("datetime")),
                    feed_md5=_md5_url_title(detail_url, title),
                )
            )
        if items and bad_id_count == len(items):
            raise FetchError(
                f"wisburg list[{self._url}]: all {len(items)} items have non-int "
                "id — upstream id contract drifted; refusing to advance the cursor"
            )
        return articles

    async def health(self) -> bool:
        """Key-presence check only — no remote probe, so /health stays free
        of wisburg rate-limit spend (same trade-off as NewsApiSource)."""
        return bool(get_settings().wisburg_api_key.get_secret_value())

    @classmethod
    def config_schema(cls) -> dict:
        # No per-feed knobs: endpoint identity lives in feed.url, auth in
        # Settings. Empty properties → dashboard skips the config section.
        return {"type": "object", "properties": {}}
