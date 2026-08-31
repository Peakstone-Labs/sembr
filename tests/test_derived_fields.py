# SPDX-License-Identifier: Apache-2.0
"""Shared derived payload fields (``vector_store/derived_fields.py``).

These four keys are what makes one filter surface mean the same thing across
``news_current`` and ``news_archive``, so the tests pin both the omission
semantics (an unparseable value must leave the key ABSENT, not None — absent is
what a Range/Match filter treats as "unknown") and the fact that the ingest
path stamps them.
"""

from __future__ import annotations

from sembr.db.articles import PendingRow
from sembr.embedder.scheduler import _to_point
from sembr.vector_store.derived_fields import build_derived_payload


def _row(**overrides) -> PendingRow:
    base = dict(
        md5="a" * 32,
        feed_id=7,
        url="https://www.reuters.com/world/hormuz",
        title="Fed holds rates steady",
        body="The Federal Reserve kept rates unchanged " * 5,
        published_at="2026-07-27T06:49:53+00:00",
        retry_count=0,
    )
    base.update(overrides)
    return PendingRow(**base)


def test_build_derived_payload_full():
    derived = build_derived_payload(
        {
            "title": "Fed holds rates steady",
            "body": "The Federal Reserve kept rates unchanged " * 5,
            "published_at": "2026-07-27T06:49:53+00:00",
            "url": "https://www.reuters.com/world/x",
        }
    )
    assert derived["body_len"] == len("The Federal Reserve kept rates unchanged " * 5)
    assert derived["lang"] == "en"
    assert derived["published_at_ts"] == 1785134993
    assert derived["url_domain"] == "reuters.com"


def test_build_derived_payload_omits_unparseable():
    """Design D5: unparseable time / URL leave the key ABSENT, not None."""
    derived = build_derived_payload(
        {
            "title": "无时间戳的稿件",
            "body": "霍尔木兹海峡局势升级，" * 20,
            "published_at": "2026-08-19T17:01:24",  # naive → unparseable on purpose
            "url": "not a url",
        }
    )
    assert "published_at_ts" not in derived
    assert "url_domain" not in derived
    # …while the two discriminator fields are still written unconditionally.
    assert derived["lang"] == "zh"
    assert derived["body_len"] > 0


def test_build_derived_payload_unconditional_fields_survive_empty_input():
    """`body_len` is the backfill queue's "already processed" marker: it must
    exist even for a point with no body at all, or that point would be
    re-fetched by IsEmpty(body_len) forever."""
    derived = build_derived_payload({})
    assert derived["body_len"] == 0
    assert derived["lang"] == "other"
    assert set(derived) == {"body_len", "lang"}


def test_build_derived_payload_non_string_title_and_body():
    derived = build_derived_payload({"title": None, "body": 42})
    assert derived["body_len"] == 0
    assert derived["lang"] == "other"


def test_to_point_writes_four_derived_fields():
    """Design D3: the ingest path stamps derived fields, so new points are
    filterable without waiting for the backfill job."""
    point = _to_point(_row(), [0.1] * 4, "bge-m3_v1")
    payload = point.payload

    assert payload["body_len"] == len("The Federal Reserve kept rates unchanged " * 5)
    assert payload["lang"] == "en"
    assert payload["published_at_ts"] == 1785134993
    assert payload["url_domain"] == "reuters.com"
    # base fields untouched
    assert payload["url"] == "https://www.reuters.com/world/hormuz"
    assert payload["feed_id"] == 7
    assert payload["embedding_model_version"] == "bge-m3_v1"
    assert isinstance(payload["ingested_at_ts"], int)


def test_to_point_omits_derived_keys_when_source_unusable():
    point = _to_point(_row(published_at=None, url="mailto:x"), [0.1] * 4, "bge-m3_v1")
    payload = point.payload

    assert "published_at_ts" not in payload
    assert "url_domain" not in payload
    assert payload["body_len"] > 0
    assert payload["lang"] == "en"
