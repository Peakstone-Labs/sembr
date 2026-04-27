"""Measure BGE-M3 encode latency at different max_seq_length values.

Stop the api container first so this script doesn't compete with embedder_worker
for CPU, then run inside a one-off container that shares the data volume:

    docker compose stop api
    docker compose run --rm api python /app/scripts/benchmark_embedder.py
    docker compose start api  # restore service after measuring

Picks the longest article currently in pending_articles as the worst-case input,
plus a batch of 16 to mirror the production BATCH_SIZE.
"""
from __future__ import annotations

import sqlite3
import time

from sentence_transformers import SentenceTransformer

DB_PATH = "/app/data/sembr.db"
SEQ_LENGTHS = [256, 512, 1024, 2048, 4096, 8192]
BATCH_SEQ_LENGTHS = [512, 1024, 2048, 8192]
BATCH_SIZE = 16


def main() -> None:
    print("Loading model with prod settings (fp16, low_cpu_mem_usage)...")
    t0 = time.perf_counter()
    model = SentenceTransformer(
        "BAAI/bge-m3",
        model_kwargs={"dtype": "float16", "low_cpu_mem_usage": True},
    )
    print(f"Model loaded in {time.perf_counter() - t0:.1f}s")
    print(f"Default max_seq_length: {model.max_seq_length}")
    print(f"Tokenizer: {type(model.tokenizer).__name__}\n")

    conn = sqlite3.connect(DB_PATH)

    row = conn.execute(
        "SELECT title, body, length(body) FROM pending_articles "
        "ORDER BY length(body) DESC LIMIT 1"
    ).fetchone()
    if row is None:
        print("No pending articles to benchmark against. Aborting.")
        raise SystemExit(1)

    title, body, body_len = row
    text = f"{title}\n\n{body}"
    char_count = len(text)
    tokens = model.tokenizer.encode(text, add_special_tokens=False)
    token_count = len(tokens)
    print("Test article (longest in pending):")
    print(f"  title={len(title)} chars, body={body_len} chars, total={char_count} chars")
    print(f"  token count (untruncated): {token_count}\n")

    print("Single-article encode time at various max_seq_length (after warmup):")
    for msl in SEQ_LENGTHS:
        model.max_seq_length = msl
        model.encode([text])  # warmup
        t0 = time.perf_counter()
        model.encode([text])
        dt = time.perf_counter() - t0
        effective = min(msl, token_count)
        print(f"  msl={msl:>4} (effective {effective:>4} tok): {dt * 1000:>7.0f} ms")

    rows = conn.execute(
        "SELECT title, body FROM pending_articles LIMIT ?",
        (BATCH_SIZE,),
    ).fetchall()
    if len(rows) < BATCH_SIZE:
        print(f"\nOnly {len(rows)} pending rows available — batch test will be smaller.")
    texts = [f"{r[0]}\n\n{r[1]}" for r in rows]
    avg_toks = sum(
        len(model.tokenizer.encode(t, add_special_tokens=False)) for t in texts
    ) / len(texts)
    print(f"\nBatch of {len(texts)} articles, avg {avg_toks:.0f} tokens each:")
    for msl in BATCH_SEQ_LENGTHS:
        model.max_seq_length = msl
        model.encode(texts)  # warmup
        t0 = time.perf_counter()
        model.encode(texts)
        dt = time.perf_counter() - t0
        print(
            f"  msl={msl:>4}: {dt:>5.1f} s for batch of {len(texts)} "
            f"({dt / len(texts) * 1000:>5.0f} ms/article)"
        )


if __name__ == "__main__":
    main()
