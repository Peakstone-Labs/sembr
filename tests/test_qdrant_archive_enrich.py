# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the archive payload enrichment pure functions."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from sembr.vector_store.news_archive import (
    build_archive_point,
    detect_lang,
    extract_url_domain,
    parse_published_at_ts,
)

# ---------------------------------------------------------------------------
# parse_published_at_ts
# ---------------------------------------------------------------------------


def test_parse_published_at_aware_iso():
    raw = "2026-08-19T17:01:24+00:00"
    expected = int(datetime.fromisoformat(raw).timestamp())
    assert parse_published_at_ts(raw) == expected


def test_parse_published_at_non_utc_offset_normalizes():
    # +08:00 offset is still tz-aware; epoch seconds are timezone-agnostic.
    raw = "2026-08-20T01:01:24+08:00"
    assert parse_published_at_ts(raw) == int(
        datetime(2026, 8, 19, 17, 1, 24, tzinfo=UTC).timestamp()
    )


def test_parse_published_at_naive_is_absent():
    # Collectors only emit tz-aware datetimes; a naive string means an
    # unknown writer, and guessing a timezone would silently shift hours.
    assert parse_published_at_ts("2026-08-19T17:01:24") is None


def test_parse_published_at_garbage_and_none():
    assert parse_published_at_ts(None) is None
    assert parse_published_at_ts("") is None
    assert parse_published_at_ts("not-a-date") is None
    assert parse_published_at_ts(1234567890) is None


# ---------------------------------------------------------------------------
# detect_lang
# ---------------------------------------------------------------------------


def test_detect_lang_chinese():
    assert detect_lang("伊朗常驻联合国代表团声明", "霍尔木兹海峡局势升级," * 50) == "zh"


def test_detect_lang_english():
    assert detect_lang("Fed holds rates steady", "The Federal Reserve kept rates " * 20) == "en"


def test_detect_lang_other_when_no_letters():
    assert detect_lang("123 456", "789 —— 000") == "other"


def test_detect_lang_mixed_leans_zh_at_threshold():
    # CJK share ≥ 0.30 of informative chars → zh even with plenty of latin.
    title = "美联储观察 Fed watch"
    body = "会议纪要中的鹰派信号非常明显 hawkish tone"
    cjk = sum(1 for ch in title + body if "一" <= ch <= "鿿")
    latin = sum(1 for ch in title + body if ch.isascii() and ch.isalpha())
    assert cjk / (cjk + latin) >= 0.30  # guard: fixture actually sits above threshold
    assert detect_lang(title, body) == "zh"


# ---------------------------------------------------------------------------
# extract_url_domain
# ---------------------------------------------------------------------------


def test_extract_url_domain_basic_and_www():
    assert extract_url_domain("https://www.reuters.com/world/x") == "reuters.com"
    assert extract_url_domain("http://Bloomberg.com/a?b=c") == "bloomberg.com"


def test_extract_url_domain_bad_inputs():
    assert extract_url_domain(None) is None
    assert extract_url_domain("") is None
    assert extract_url_domain("not a url") is None
    assert extract_url_domain(42) is None


# ---------------------------------------------------------------------------
# build_archive_point
# ---------------------------------------------------------------------------


def _point(payload: dict, vector=None) -> SimpleNamespace:
    return SimpleNamespace(
        id="00000000-0000-0000-0000-0000000000aa",
        payload=payload,
        vector=vector if vector is not None else [0.1] * 4,
    )


def test_build_archive_point_enriches_payload():
    payload = {
        "url": "https://www.reuters.com/world/africa/x",
        "title": "Vodafone ups guidance",
        "body": "Vodafone raised its outlook " * 10,
        "published_at": "2026-07-27T06:49:53+00:00",
        "feed_id": 3,
        "embedding_model_version": "bge-m3_v1",
        "ingested_at_ts": 1785000000,
    }
    pt = build_archive_point(_point(payload), matched_intents=[29, 31], archived_at_ts=1790000000)
    assert pt is not None
    assert pt.id == "00000000-0000-0000-0000-0000000000aa"
    assert pt.vector == [0.1] * 4
    p = pt.payload
    # original fields survive untouched
    assert p["url"] == payload["url"]
    assert p["body"] == payload["body"]
    assert p["embedding_model_version"] == "bge-m3_v1"
    # derived fields
    assert p["published_at_ts"] == int(
        datetime.fromisoformat("2026-07-27T06:49:53+00:00").timestamp()
    )
    assert p["body_len"] == len(payload["body"])
    assert p["lang"] == "en"
    assert p["url_domain"] == "reuters.com"
    assert p["matched_intents"] == [29, 31]
    assert p["archived_at_ts"] == 1790000000


def test_build_archive_point_bad_published_at_key_absent():
    pt = build_archive_point(
        _point({"title": "t", "body": "b", "published_at": None}),
        matched_intents=[],
        archived_at_ts=1,
    )
    assert pt is not None
    assert "published_at_ts" not in pt.payload
    # matched_intents is always written, even empty, so the field is filterable
    assert pt.payload["matched_intents"] == []


def test_build_archive_point_no_vector_is_poisoned():
    p = SimpleNamespace(id="x", payload={"title": "t", "body": "b"}, vector=None)
    assert build_archive_point(p, matched_intents=[], archived_at_ts=1) is None


def test_build_archive_point_non_string_body_and_title():
    pt = build_archive_point(
        _point({"title": None, "body": 123}),
        matched_intents=[],
        archived_at_ts=1,
    )
    assert pt is not None
    assert pt.payload["body_len"] == 0
    assert pt.payload["lang"] == "other"
