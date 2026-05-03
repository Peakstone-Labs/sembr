"""SC#1: feed_tags persist across reconnect; FK CASCADE removes tags on feed delete."""
from __future__ import annotations

import os
import tempfile

import pytest

from sembr.db.feed_tags import get_tags, list_all_tags
from sembr.db.feeds import (
    create_feed,
    delete_feed,
    init_feed_tables,
)
from sembr.db.sqlite import close_sqlite, get_conn, init_sqlite


@pytest.fixture
async def tmp_db_path():
    """Real on-disk DB so PRAGMA foreign_keys=ON + WAL behave authentically."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = await init_sqlite(path)
        await init_feed_tables(conn)
        yield path
    finally:
        await close_sqlite()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass


@pytest.fixture
async def tmp_db(tmp_db_path):
    yield get_conn()


@pytest.mark.asyncio
async def test_create_feed_with_tags_persists(tmp_db) -> None:
    feed = await create_feed(
        get_conn(),
        name="t1",
        url="https://example.com/a.rss",
        tags=["ai", "news"],
    )
    assert sorted(feed.tags) == ["ai", "news"]
    # Re-read via list_all_tags to confirm persistence layer (not just in-memory).
    all_tags = await list_all_tags(get_conn())
    assert sorted(all_tags[feed.id]) == ["ai", "news"]


@pytest.mark.asyncio
async def test_tags_survive_reconnect(tmp_db_path) -> None:
    feed = await create_feed(get_conn(), name="t2", url="https://example.com/b.rss", tags=["x"])
    feed_id = feed.id
    await close_sqlite()
    conn = await init_sqlite(tmp_db_path)
    await init_feed_tables(conn)
    tags = await get_tags(conn, feed_id)
    assert tags == ["x"]


@pytest.mark.asyncio
async def test_delete_feed_cascades_tags(tmp_db) -> None:
    conn = get_conn()
    feed = await create_feed(conn, name="t3", url="https://example.com/c.rss", tags=["a", "b", "c"])
    assert await get_tags(conn, feed.id) == ["a", "b", "c"]

    deleted = await delete_feed(conn, feed.id)
    assert deleted is True
    # FK CASCADE must have removed all feed_tags rows for this feed.
    assert await get_tags(conn, feed.id) == []
    async with conn.execute(
        "SELECT COUNT(*) FROM feed_tags WHERE feed_id=?", (feed.id,)
    ) as cur:
        count = (await cur.fetchone())[0]
    assert count == 0


@pytest.mark.asyncio
async def test_tags_dedup_via_pk(tmp_db) -> None:
    """Composite PK enforces uniqueness; replace_tags collapses duplicates safely."""
    from sembr.db.feed_tags import insert_tags_in_tx

    conn = get_conn()
    feed = await create_feed(conn, name="t4", url="https://example.com/d.rss")
    # Insert duplicates: PK collision is silently ignored by INSERT OR IGNORE.
    await insert_tags_in_tx(conn, feed.id, ["a", "a", "b"])
    await conn.commit()
    assert await get_tags(conn, feed.id) == ["a", "b"]
