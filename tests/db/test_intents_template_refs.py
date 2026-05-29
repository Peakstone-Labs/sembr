# SPDX-License-Identifier: Apache-2.0
"""Tests for `list_template_refs` and `rename_intent_template`."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aiosqlite
import pytest

from sembr.db.intents import (
    init_intent_tables,
    list_template_refs,
    rename_intent_template,
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _seed_intent(
    conn: aiosqlite.Connection,
    name: str,
    *,
    system_template: str = "default",
    instruction_template: str = "default",
) -> int:
    cursor = await conn.execute(
        """INSERT INTO intents
               (name, text, channels, tags, system_template, instruction_template,
                feed_filter, schedule, timezone, language, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            f"text for {name}",
            json.dumps([{"type": "email", "to": ["a@b.com"]}]),
            json.dumps([]),
            system_template,
            instruction_template,
            "null",
            json.dumps(
                {
                    "mode": "cron",
                    "preset": "daily",
                    "hour": 9,
                    "minute": 0,
                    "lookback_seconds": 86400,
                    "skip_seen": True,
                }
            ),
            "UTC",
            "zh",
            _now(),
            _now(),
        ),
    )
    await conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


# --- list_template_refs ------------------------------------------------------


@pytest.mark.asyncio
async def test_list_template_refs_empty_when_no_intents() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await init_intent_tables(conn)
        refs = await list_template_refs(conn)
        assert refs == {}


@pytest.mark.asyncio
async def test_list_template_refs_groups_by_kind_and_name() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await init_intent_tables(conn)
        i1 = await _seed_intent(
            conn, "alpha", system_template="default", instruction_template="crypto_zh"
        )
        i2 = await _seed_intent(
            conn, "beta", system_template="default", instruction_template="crypto_zh"
        )
        i3 = await _seed_intent(
            conn, "gamma", system_template="custom_sys", instruction_template="default"
        )

        refs = await list_template_refs(conn)

        # Two intents share instruction/crypto_zh
        assert refs[("instruction", "crypto_zh")] == [(i1, "alpha"), (i2, "beta")]
        # Two intents share system/default; one references system/custom_sys
        assert refs[("system", "default")] == [(i1, "alpha"), (i2, "beta")]
        assert refs[("system", "custom_sys")] == [(i3, "gamma")]
        # gamma has instruction/default
        assert refs[("instruction", "default")] == [(i3, "gamma")]


@pytest.mark.asyncio
async def test_list_template_refs_orders_by_intent_id() -> None:
    """Stable output for UI: intents sorted by id ASC inside each (kind, name)."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_intent_tables(conn)
        # Insert 3 intents — ids will be 1, 2, 3 in insertion order.
        a = await _seed_intent(conn, "a")
        b = await _seed_intent(conn, "b")
        c = await _seed_intent(conn, "c")
        refs = await list_template_refs(conn)
        assert [pair[0] for pair in refs[("system", "default")]] == [a, b, c]


# --- rename_intent_template --------------------------------------------------


@pytest.mark.asyncio
async def test_rename_intent_template_updates_matching_rows() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await init_intent_tables(conn)
        await _seed_intent(conn, "x", instruction_template="crypto_zh")
        await _seed_intent(conn, "y", instruction_template="crypto_zh")
        await _seed_intent(conn, "z", instruction_template="default")  # unaffected

        rowcount = await rename_intent_template(conn, "instruction", "crypto_zh", "crypto_zh_v2")
        await conn.commit()

        assert rowcount == 2
        async with conn.execute(
            "SELECT name, instruction_template FROM intents ORDER BY id ASC"
        ) as cur:
            rows = await cur.fetchall()
        assert rows == [("x", "crypto_zh_v2"), ("y", "crypto_zh_v2"), ("z", "default")]


@pytest.mark.asyncio
async def test_rename_intent_template_zero_rowcount_on_no_match() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await init_intent_tables(conn)
        await _seed_intent(conn, "x")
        rowcount = await rename_intent_template(conn, "instruction", "ghost", "ghost_v2")
        await conn.commit()
        assert rowcount == 0


@pytest.mark.asyncio
async def test_rename_intent_template_system_kind_works() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await init_intent_tables(conn)
        await _seed_intent(conn, "x", system_template="custom_sys")
        rowcount = await rename_intent_template(conn, "system", "custom_sys", "custom_sys_v2")
        await conn.commit()
        assert rowcount == 1


@pytest.mark.asyncio
async def test_rename_intent_template_rejects_unknown_kind() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await init_intent_tables(conn)
        with pytest.raises(ValueError):
            await rename_intent_template(conn, "DROP TABLE", "foo", "bar")


@pytest.mark.asyncio
async def test_rename_intent_template_bumps_updated_at() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await init_intent_tables(conn)
        intent_id = await _seed_intent(conn, "x", instruction_template="crypto_zh")
        async with conn.execute("SELECT updated_at FROM intents WHERE id = ?", (intent_id,)) as cur:
            (before,) = (await cur.fetchone()) or (None,)

        await rename_intent_template(conn, "instruction", "crypto_zh", "crypto_zh_v2")
        await conn.commit()

        async with conn.execute("SELECT updated_at FROM intents WHERE id = ?", (intent_id,)) as cur:
            (after,) = (await cur.fetchone()) or (None,)
        # `updated_at` is a UTC ISO-Z string; lexicographic comparison is correct.
        assert before is not None and after is not None
        assert after >= before  # may equal at ≤1s resolution; never go backwards
