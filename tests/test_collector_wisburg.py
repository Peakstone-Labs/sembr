# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sembr.collector.wisburg (WisburgSource).

All HTTP is mocked via respx; no real key ever appears here. Covers the
dev-owned rows of the wisburg-api design Test Strategy table: happy path,
window math (overlap / first-pull / 7d clamp), pagination, N+1 failure
semantics (404 skip vs transient raise), envelope validation, empty key,
and registry/schema wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from sembr.collector.rss import FetchError
from sembr.collector.wisburg import (
    ENDPOINT_URLS,
    WisburgSource,
    _md5_url_title,
    normalize_wisburg_url,
)

REPORTS_URL = "https://api-omen.wisburg.com/api/reports"


def _use_key(monkeypatch, value: str = "test-key") -> None:
    from sembr.config import get_settings

    monkeypatch.setenv("WISBURG_API_KEY", value)
    get_settings.cache_clear()


def _envelope(data: dict) -> dict:
    return {"request_id": "req-1", "code": 200, "status": 0, "message": "success", "data": data}


def _list_page(items: list[dict], end_cursor: str | None = None) -> dict:
    return _envelope({"items": items, "page_info": {"end_cursor": end_cursor}})


def _detail(
    item_id: int,
    *,
    title: str = "标题",
    summary: str = "### 主要观点\n内容",
    dt: str = "2026-06-10T04:36:23+08:00",
    meta: object = None,
) -> dict:
    data: dict = {"id": item_id, "title": title, "datetime": dt, "url": "", "summary": summary}
    if meta is not None:
        data["meta"] = meta
    return _envelope(data)


# ---------------------------------------------------------------------------
# normalize_wisburg_url — parity with FeedCreate's wisburg-report branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://api-omen.wisburg.com/api/reports", REPORTS_URL),
        ("HTTPS://API-OMEN.WISBURG.COM/api/reports/", REPORTS_URL),
        ("http://api-omen.wisburg.com/api/reports", REPORTS_URL),
        (
            "  https://api-omen.wisburg.com/api/am-reports/  ",
            "https://api-omen.wisburg.com/api/am-reports",
        ),
    ],
)
def test_normalize_wisburg_url(raw: str, expected: str) -> None:
    assert normalize_wisburg_url(raw) == expected


def test_endpoint_urls_are_already_normalized() -> None:
    for url in ENDPOINT_URLS:
        assert normalize_wisburg_url(url) == url


# ---------------------------------------------------------------------------
# fetch — happy path
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.anyio
async def test_fetch_happy_path(monkeypatch) -> None:
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page(
                [
                    {
                        "id": 92008,
                        "title": "瑞银：科技股配置",
                        "datetime": "2026-06-10T04:36:23+08:00",
                    },
                    {
                        "id": 92007,
                        "title": "花旗：美债拍卖",
                        "datetime": "2026-06-10T04:36:20+08:00",
                    },
                ]
            ),
        )
    )
    respx.get(f"{REPORTS_URL}/92008").mock(
        return_value=httpx.Response(200, json=_detail(92008, title="瑞银：科技股配置"))
    )
    respx.get(f"{REPORTS_URL}/92007").mock(
        return_value=httpx.Response(200, json=_detail(92007, title="花旗：美债拍卖"))
    )

    articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert [a.url for a in articles] == [f"{REPORTS_URL}/92008", f"{REPORTS_URL}/92007"]
    a = articles[0]
    assert a.title == "瑞银：科技股配置"
    assert a.body.startswith("### 主要观点")
    assert a.content_quality == "summary"
    # +08:00 input → UTC aware
    assert a.published_at == datetime(2026, 6, 9, 20, 36, 23, tzinfo=UTC)
    assert a.feed_md5 == _md5_url_title(a.url, a.title)
    # Bearer header attached to both list and detail calls
    for call in respx.calls:
        assert call.request.headers["authorization"] == "Bearer test-key"


# ---------------------------------------------------------------------------
# fetch — meta source preamble (path A: feed source_org enrichment)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.anyio
async def test_fetch_meta_prepended_to_body(monkeypatch) -> None:
    """name + description → both rendered as a markdown preamble above the
    summary, which is preserved verbatim after it."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page([{"id": 1, "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"}]),
        )
    )
    respx.get(f"{REPORTS_URL}/1").mock(
        return_value=httpx.Response(
            200,
            json=_detail(
                1,
                summary="### 主要观点\n内容",
                meta={"name": "花旗", "description": "引用了Citi研究员的报告，原文12页。"},
            ),
        )
    )

    articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    body = articles[0].body
    assert body == (
        "> 发布机构：花旗\n> 来源说明：引用了Citi研究员的报告，原文12页。\n\n### 主要观点\n内容"
    )


@respx.mock
@pytest.mark.anyio
async def test_fetch_meta_name_only(monkeypatch) -> None:
    """am-reports shape: description is null → only the institution line is
    added (no empty 来源说明 stub)."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page([{"id": 1, "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"}]),
        )
    )
    respx.get(f"{REPORTS_URL}/1").mock(
        return_value=httpx.Response(
            200,
            json=_detail(1, summary="正文", meta={"name": "CharlesSchwab", "description": None}),
        )
    )

    articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert articles[0].body == "> 发布机构：CharlesSchwab\n\n正文"


@respx.mock
@pytest.mark.anyio
@pytest.mark.parametrize("meta", [None, "not-a-dict", {}, {"name": "", "description": ""}])
async def test_fetch_no_meta_body_is_summary(monkeypatch, meta) -> None:
    """No meta / malformed meta / all-empty fields → body is the bare summary
    (earningscalls keeps working untouched)."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page([{"id": 1, "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"}]),
        )
    )
    respx.get(f"{REPORTS_URL}/1").mock(
        return_value=httpx.Response(200, json=_detail(1, summary="纯摘要", meta=meta))
    )

    articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert articles[0].body == "纯摘要"


# ---------------------------------------------------------------------------
# fetch — window math (overlap / first-pull / clamp)
# ---------------------------------------------------------------------------


def _starttime_of(call) -> datetime:
    qs = parse_qs(urlparse(str(call.request.url)).query)
    return datetime.fromisoformat(qs["startTime"][0])


@respx.mock
@pytest.mark.anyio
async def test_fetch_since_maps_to_starttime_with_overlap(monkeypatch) -> None:
    _use_key(monkeypatch)
    route = respx.get(REPORTS_URL).mock(return_value=httpx.Response(200, json=_list_page([])))
    since = datetime.now(UTC) - timedelta(hours=6)

    await WisburgSource(REPORTS_URL).fetch(since=since)

    assert _starttime_of(route.calls[0]) == since - timedelta(hours=1)


@respx.mock
@pytest.mark.anyio
async def test_fetch_first_pull_window_1d(monkeypatch) -> None:
    _use_key(monkeypatch)
    route = respx.get(REPORTS_URL).mock(return_value=httpx.Response(200, json=_list_page([])))

    await WisburgSource(REPORTS_URL).fetch(since=None)

    start = _starttime_of(route.calls[0])
    expected = datetime.now(UTC) - timedelta(days=1)
    assert abs((start - expected).total_seconds()) < 60


@respx.mock
@pytest.mark.anyio
async def test_fetch_window_clamped_to_7d(monkeypatch, caplog) -> None:
    _use_key(monkeypatch)
    route = respx.get(REPORTS_URL).mock(return_value=httpx.Response(200, json=_list_page([])))
    since = datetime.now(UTC) - timedelta(days=30)

    with caplog.at_level("WARNING", logger="sembr.collector.wisburg"):
        await WisburgSource(REPORTS_URL).fetch(since=since)

    start = _starttime_of(route.calls[0])
    expected = datetime.now(UTC) - timedelta(days=7)
    assert abs((start - expected).total_seconds()) < 60
    assert any("clamped" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# fetch — pagination
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.anyio
async def test_fetch_pagination_follows_cursor_until_short_page(monkeypatch) -> None:
    _use_key(monkeypatch)
    page1 = [
        {"id": i, "title": f"t{i}", "datetime": "2026-06-10T04:00:00+08:00"} for i in range(1, 101)
    ]
    page2 = [{"id": 101, "title": "t101", "datetime": "2026-06-10T04:00:00+08:00"}]

    def list_router(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if "after" not in qs:
            return httpx.Response(200, json=_list_page(page1, end_cursor="100"))
        assert qs["after"] == ["100"]
        return httpx.Response(200, json=_list_page(page2, end_cursor="101"))

    respx.get(REPORTS_URL).mock(side_effect=list_router)
    respx.get(url__regex=rf"{REPORTS_URL}/\d+$").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=_detail(
                int(str(request.url).rsplit("/", 1)[1]),
                title=f"t{str(request.url).rsplit('/', 1)[1]}",
            ),
        )
    )

    articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert len(articles) == 101
    # 2 list calls (full page + short page; short page ends the walk) + 101 details
    list_calls = [c for c in respx.calls if "?" in str(c.request.url)]
    assert len(list_calls) == 2


# ---------------------------------------------------------------------------
# fetch — N+1 failure semantics
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.anyio
async def test_fetch_detail_404_skipped(monkeypatch, caplog) -> None:
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page(
                [
                    {"id": i, "title": f"t{i}", "datetime": "2026-06-10T04:00:00+08:00"}
                    for i in (1, 2, 3)
                ]
            ),
        )
    )
    respx.get(f"{REPORTS_URL}/1").mock(return_value=httpx.Response(200, json=_detail(1)))
    respx.get(f"{REPORTS_URL}/2").mock(return_value=httpx.Response(404))
    respx.get(f"{REPORTS_URL}/3").mock(return_value=httpx.Response(200, json=_detail(3)))

    with caplog.at_level("WARNING", logger="sembr.collector.wisburg"):
        articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert [a.url.rsplit("/", 1)[1] for a in articles] == ["1", "3"]
    assert any("404" in r.message for r in caplog.records)


@respx.mock
@pytest.mark.anyio
async def test_fetch_detail_500_raises_fetcherror(monkeypatch) -> None:
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page(
                [
                    {"id": 1, "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"},
                    {"id": 2, "title": "t2", "datetime": "2026-06-10T04:00:00+08:00"},
                ]
            ),
        )
    )
    respx.get(f"{REPORTS_URL}/1").mock(return_value=httpx.Response(200, json=_detail(1)))
    respx.get(f"{REPORTS_URL}/2").mock(return_value=httpx.Response(500))

    with pytest.raises(FetchError):
        await WisburgSource(REPORTS_URL).fetch(since=None)


@respx.mock
@pytest.mark.anyio
async def test_fetch_all_bad_ids_raises_fetcherror(monkeypatch) -> None:
    """An id contract drift must not become a silent empty success that
    advances the cursor past the whole window."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page(
                [
                    {"id": "92008", "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"},
                    {"id": "92007", "title": "t2", "datetime": "2026-06-10T04:00:00+08:00"},
                ]
            ),
        )
    )

    with pytest.raises(FetchError, match="non-int"):
        await WisburgSource(REPORTS_URL).fetch(since=None)


@respx.mock
@pytest.mark.anyio
async def test_fetch_isolated_bad_id_skipped(monkeypatch) -> None:
    """A single malformed item must NOT poison the batch (only all-bad raises)."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page(
                [
                    {"id": "bad", "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"},
                    {"id": 2, "title": "t2", "datetime": "2026-06-10T04:00:00+08:00"},
                ]
            ),
        )
    )
    respx.get(f"{REPORTS_URL}/2").mock(return_value=httpx.Response(200, json=_detail(2)))

    articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert [a.url for a in articles] == [f"{REPORTS_URL}/2"]


@respx.mock
@pytest.mark.anyio
async def test_fetch_missing_title_skipped(monkeypatch, caplog) -> None:
    """A detail without title is skipped, not inserted."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page(
                [
                    {"id": 1, "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"},
                    {"id": 2, "title": "t2", "datetime": "2026-06-10T04:00:00+08:00"},
                ]
            ),
        )
    )
    respx.get(f"{REPORTS_URL}/1").mock(return_value=httpx.Response(200, json=_detail(1, title="")))
    respx.get(f"{REPORTS_URL}/2").mock(return_value=httpx.Response(200, json=_detail(2)))

    with caplog.at_level("WARNING", logger="sembr.collector.wisburg"):
        articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert [a.url for a in articles] == [f"{REPORTS_URL}/2"]
    assert any("no title" in r.message for r in caplog.records)


@respx.mock
@pytest.mark.anyio
async def test_fetch_zero_delivered_warns(monkeypatch, caplog) -> None:
    """List non-empty but 0 delivered must leave a loud breadcrumb
    (cursor will still advance; overlap is the only retry)."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page([{"id": 1, "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"}]),
        )
    )
    respx.get(f"{REPORTS_URL}/1").mock(
        return_value=httpx.Response(200, json=_detail(1, summary=""))
    )

    with caplog.at_level("WARNING", logger="sembr.collector.wisburg"):
        articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert articles == []
    assert any("0 delivered" in r.message for r in caplog.records)


@respx.mock
@pytest.mark.anyio
async def test_fetch_list_http_error_raises(monkeypatch) -> None:
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(FetchError):
        await WisburgSource(REPORTS_URL).fetch(since=None)


@respx.mock
@pytest.mark.anyio
async def test_fetch_envelope_code_nonzero_raises(monkeypatch) -> None:
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "request_id": "r",
                "code": 403,
                "status": 1,
                "message": "forbidden",
                "data": None,
            },
        )
    )

    with pytest.raises(FetchError, match="code=403"):
        await WisburgSource(REPORTS_URL).fetch(since=None)


@respx.mock
@pytest.mark.anyio
async def test_fetch_list_json_parse_error_raises(monkeypatch) -> None:
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(return_value=httpx.Response(200, text="<html>not json"))

    with pytest.raises(FetchError, match="JSON parse"):
        await WisburgSource(REPORTS_URL).fetch(since=None)


@pytest.mark.anyio
async def test_fetch_empty_key_raises(monkeypatch) -> None:
    _use_key(monkeypatch, "")

    with pytest.raises(FetchError, match="WISBURG_API_KEY"):
        await WisburgSource(REPORTS_URL).fetch(since=None)


@pytest.mark.anyio
async def test_fetch_unknown_endpoint_raises(monkeypatch) -> None:
    """Rows edited out-of-band must not GET arbitrary URLs with our key."""
    _use_key(monkeypatch)

    with pytest.raises(FetchError, match="not a known endpoint"):
        await WisburgSource("https://api-omen.wisburg.com/api/feed").fetch(since=None)


# ---------------------------------------------------------------------------
# registry + schema wiring
# ---------------------------------------------------------------------------


def test_source_registry_contains_wisburg() -> None:
    from sembr.collector.scheduler import SOURCE_REGISTRY

    assert SOURCE_REGISTRY["wisburg-report"] is WisburgSource


def test_config_schema_empty_properties() -> None:
    schema = WisburgSource.config_schema()
    assert schema["properties"] == {}
    assert json.dumps(schema)  # JSON-serializable for /sources/schemas


@pytest.mark.anyio
async def test_health_reflects_key(monkeypatch) -> None:
    _use_key(monkeypatch, "")
    assert await WisburgSource(REPORTS_URL).health() is False
    _use_key(monkeypatch, "k")
    assert await WisburgSource(REPORTS_URL).health() is True
