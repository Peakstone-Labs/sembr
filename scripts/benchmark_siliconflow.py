"""Empirically measure SiliconFlow /v1/embeddings latency vs batch size + chars.

Pulls real articles out of dead_articles (or pending_articles if dead is
empty), truncates each to the same per-text cap the production embedder
uses (8 000 chars), and runs a series of calls with NO timeout cap so we
can see actual server-side processing time. Reports per-call elapsed
time, total chars, and chars/sec throughput.

Designed to answer:
  1. Is the time-vs-chars relationship linear, or super-linear?
  2. What's the realistic chars/sec rate, so we can pick a sane formula?
  3. Does batch size matter independently of total chars?

Usage (run inside api container — uses /app paths and SiliconFlow key from env):
    docker compose exec -T api python scripts/benchmark_siliconflow.py
    docker compose exec -T api python scripts/benchmark_siliconflow.py --source pending
    docker compose exec -T api python scripts/benchmark_siliconflow.py --runs 2

Environment: reads EMBEDDER_API_KEY and EMBEDDER_API_BASE_URL from the same
env vars the running embedder uses (see .env.example). Run inside the api
container so these are already set.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import statistics
import sys
import time
from typing import Sequence

import httpx

_EMBED_CHARS_MAX = 8_000
DEFAULT_BATCH_SIZES: Sequence[int] = (1, 4, 8, 16, 32)


def _load_articles(db_path: str, source: str, limit: int) -> list[tuple[str, str]]:
    """Return a list of (title, body) tuples from the requested table."""
    table = {"dead": "dead_articles", "pending": "pending_articles"}[source]
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"SELECT title, body FROM {table} ORDER BY length(body) DESC LIMIT ?",
        (limit,),
    ).fetchall()
    con.close()
    return [(r["title"], r["body"]) for r in rows]


def _truncate(title: str, body: str) -> str:
    return (title + "\n\n" + body)[:_EMBED_CHARS_MAX]


async def _call_once(
    client: httpx.AsyncClient,
    api_key: str,
    base_url: str,
    model: str,
    texts: list[str],
) -> tuple[float, int]:
    """Return (elapsed_seconds, status_code). Raises on transport error."""
    t0 = time.perf_counter()
    resp = await client.post(
        f"{base_url}/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": texts, "encoding_format": "float"},
    )
    elapsed = time.perf_counter() - t0
    return elapsed, resp.status_code


async def _bench(
    articles: list[tuple[str, str]],
    api_key: str,
    base_url: str,
    model: str,
    batch_sizes: Sequence[int],
    runs: int,
) -> None:
    # No timeout cap — let the server take as long as it needs.
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        # Warmup: 1 short call so connection pool / TLS handshake doesn't
        # skew the smallest-batch measurement.
        try:
            warm_t, warm_status = await _call_once(
                client, api_key, base_url, model, ["warmup"]
            )
            print(f"warmup: {warm_t:.2f}s status={warm_status}\n")
        except Exception as exc:
            print(f"warmup failed: {type(exc).__name__}: {exc!r}")
            return

        print(f"{'batch':>5} {'total_chars':>12} {'elapsed':>10} {'chars/sec':>11} {'sec/item':>10}  status")
        print("-" * 70)
        for n in batch_sizes:
            if n > len(articles):
                print(f"{n:>5}  (skipped — only {len(articles)} articles available)")
                continue
            sample = articles[:n]
            texts = [_truncate(t, b) for t, b in sample]
            total_chars = sum(len(t) for t in texts)

            elapsed_runs: list[float] = []
            last_status = 0
            for _ in range(runs):
                try:
                    elapsed, status = await _call_once(
                        client, api_key, base_url, model, texts
                    )
                except Exception as exc:
                    print(
                        f"{n:>5} {total_chars:>12} {'ERROR':>10}  "
                        f"{type(exc).__name__}: {exc!r}"
                    )
                    last_status = -1
                    break
                elapsed_runs.append(elapsed)
                last_status = status
            if not elapsed_runs:
                continue
            avg = statistics.mean(elapsed_runs)
            cps = total_chars / avg if avg > 0 else 0
            spi = avg / n
            tag = "" if last_status == 200 else f" !!! HTTP {last_status}"
            extra = (
                f"  (runs={runs}, min={min(elapsed_runs):.2f}s, max={max(elapsed_runs):.2f}s)"
                if runs > 1 else ""
            )
            print(
                f"{n:>5} {total_chars:>12} {avg:>9.2f}s {cps:>11,.0f} {spi:>9.2f}s  "
                f"{last_status}{tag}{extra}"
            )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="/app/data/sembr.db")
    p.add_argument(
        "--source",
        choices=["dead", "pending"],
        default="dead",
        help="Pull articles from dead_articles or pending_articles",
    )
    p.add_argument(
        "--batches",
        type=int,
        nargs="+",
        default=list(DEFAULT_BATCH_SIZES),
        help="Batch sizes to test (default: 1 4 8 16 32)",
    )
    p.add_argument("--runs", type=int, default=1, help="Repeats per batch size")
    p.add_argument(
        "--api-key-env",
        default="EMBEDDER_API_KEY",
        help="Env var holding the SiliconFlow API key (matches .env.example)",
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get(
            "EMBEDDER_API_BASE_URL", "https://api.siliconflow.cn/v1"
        ),
    )
    p.add_argument("--model", default="BAAI/bge-m3")
    args = p.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(
            f"ERROR: {args.api_key_env} not set in env "
            "(run inside the api container)",
            file=sys.stderr,
        )
        sys.exit(2)

    max_n = max(args.batches)
    articles = _load_articles(args.db, args.source, max_n)
    if not articles:
        print(f"no articles found in {args.source}_articles", file=sys.stderr)
        sys.exit(2)
    if len(articles) < max_n:
        print(
            f"warning: only {len(articles)} articles available, "
            f"largest batches will be skipped",
            file=sys.stderr,
        )

    char_lens = [len(_truncate(t, b)) for t, b in articles]
    print(
        f"loaded {len(articles)} articles from {args.source}_articles\n"
        f"  per-text chars: min={min(char_lens)} avg={int(sum(char_lens)/len(char_lens))} "
        f"max={max(char_lens)} (capped at {_EMBED_CHARS_MAX})\n"
        f"  base_url={args.base_url}  model={args.model}\n"
    )

    asyncio.run(
        _bench(
            articles,
            api_key,
            args.base_url.rstrip("/"),
            args.model,
            args.batches,
            args.runs,
        )
    )


if __name__ == "__main__":
    main()
