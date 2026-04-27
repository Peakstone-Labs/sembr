"""Benchmark SiliconFlow /v1/embeddings latency at different batch sizes.

Run inside the api container (requires EMBEDDER_API_KEY in the environment):

    docker compose run --rm \\
        -e EMBEDDER_API_KEY=$EMBEDDER_API_KEY \\
        api python /app/scripts/benchmark_embedder.py

Outputs p50 / max wall-clock latency and ms-per-item for batch_size in
[1, 4, 8, 16, 32] — the range covers single-article intent checks through
the production BATCH_SIZE ceiling.

Also validates the worst-case token budget for BATCH_SIZE=32 (design Risk row 5):
if SiliconFlow rejects the request, prints the error and suggests reducing
BATCH_SIZE in sembr/embedder/scheduler.py.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import statistics
import time

import httpx

DB_PATH = "/app/data/sembr.db"
BATCH_SIZES = [1, 4, 8, 16, 32]
REPEATS = 3  # calls per batch size; median smooths first-call TCP setup

BASE_URL = os.environ.get("EMBEDDER_API_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")
MODEL = os.environ.get("EMBEDDER_MODEL", "BAAI/bge-m3")
API_KEY = os.environ.get("EMBEDDER_API_KEY")
if not API_KEY:
    raise SystemExit("EMBEDDER_API_KEY env var is required (see module docstring).")
TIMEOUT = float(os.environ.get("EMBEDDER_TIMEOUT_SECONDS", "30"))


async def _call(client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
    resp = await client.post(
        f"{BASE_URL}/embeddings",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": MODEL, "input": texts, "encoding_format": "float"},
    )
    resp.raise_for_status()
    return [item["embedding"] for item in resp.json()["data"]]


async def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT title, body FROM pending_articles ORDER BY length(body) DESC LIMIT 32"
    ).fetchall()
    if not rows:
        print("No pending articles in DB — run the collector first to populate pending_articles.")
        raise SystemExit(1)

    # Pad to 32 with the longest articles (worst-case token budget)
    while len(rows) < 32:
        rows = rows + rows
    rows = rows[:32]
    texts = [f"{r[0]}\n\n{r[1]}" for r in rows]

    print(f"model  : {MODEL}")
    print(f"endpoint: {BASE_URL}")
    print(f"articles: {len(rows)} (sorted longest-first for worst-case token probe)")
    print()
    print(f"{'batch':>6}  {'p50 ms':>8}  {'max ms':>8}  {'ms/item':>8}  note")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for bs in BATCH_SIZES:
            batch = texts[:bs]
            durations: list[float] = []
            note = ""
            try:
                for _ in range(REPEATS):
                    t0 = time.perf_counter()
                    await _call(client, batch)
                    durations.append((time.perf_counter() - t0) * 1000)
            except httpx.HTTPStatusError as exc:
                note = f"FAIL {exc.response.status_code} — consider reducing BATCH_SIZE"
                print(f"{bs:>6}  {'—':>8}  {'—':>8}  {'—':>8}  {note}")
                continue

            p50 = statistics.median(durations)
            p_max = max(durations)
            if bs == 32:
                sc1_ok = "✓ SC-1 ok" if p50 < 12_000 else "✗ SC-1 FAIL (>12s)"
                note = sc1_ok
            print(f"{bs:>6}  {p50:>8.0f}  {p_max:>8.0f}  {p50 / bs:>8.1f}  {note}")


if __name__ == "__main__":
    asyncio.run(main())
