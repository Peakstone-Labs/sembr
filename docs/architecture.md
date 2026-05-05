# Architecture

## Reverse RAG

sembr inverts the standard RAG query pattern:

| | Traditional RAG | sembr (Reverse RAG) |
|-|-----------------|---------------------|
| When | On user query | Continuously, every 5 min |
| What is stored | Documents | User intent vectors |
| What is searched | Documents for a query | Intents for each new article |
| Latency | Query-time | Background job |

Intent vectors are computed **once** at creation time. Scanning is O(intents × new_articles) via `search_batch` — not O(queries).

## Data flow

```
[RSS sources]
      │  HTTP poll (APScheduler, per-feed interval)
      ▼
[collector] ──► article text + metadata
      │
      ▼
[embedder] ──► BGE-M3 via SiliconFlow /v1/embeddings (batch=32)
      │
      ▼
[vector_store / news collection] ──► Qdrant, payload: ingested_at_ts
      │
      │  every 5 min (APScheduler)
      ▼
[matcher] ──► search_batch(intent_vector, score_threshold, filter: ingested_at_ts > cutoff)
      │
      ▼
[summarizer] ──► LLM chat completions (Jinja2 templates)
      │
      ▼
[notifier] ──► Telegram / Discord / Slack / email
      │
      ▼
[db / notification_log] ──► state: pending → sent / failed → dead
```

## Dual-collection Qdrant design

Two Qdrant collections:

- `intents` — pre-computed intent vectors, one point per intent
- `news_bge-m3_v1` (aliased as `news_current`) — article vectors with `ingested_at_ts` payload index

The matcher calls `query_points(query=vector, score_threshold=..., query_filter=...)` using `lookup_from` to reference intent vectors by point ID without re-embedding.

**Collection naming** follows `news_{model}_{version}` to enable zero-downtime model upgrades: create new collection in parallel, re-embed in background, then atomically switch the `news_current` alias.

## Deduplication (two layers)

1. **Exact**: `MD5(url + title)` fingerprint — skips already-seen articles
2. **Semantic**: within the same intent, merge hits where score delta < 0.05 and title similarity > 0.9

## SQLite state machine

The `notification_log` table tracks delivery state:

```
pending ──► sent
   │
   └──► failed ──► dead  (after N retries)
```

WAL mode (`journal_mode=WAL; synchronous=NORMAL`) ensures readers never block writers.

## APScheduler integration

`AsyncIOScheduler` started inside the FastAPI `lifespan` context manager. All jobs use `coalesce=True` to prevent backlog on recovery. Shutdown uses `wait=False` to avoid blocking uvicorn teardown.

## Plugin architecture

Sources and channels register via `pyproject.toml` entry points (`sembr.sources`, `sembr.channels`). `config_schema()` returns a JSON Schema that the dashboard auto-renders as a UI form.

## Docker Compose limits (16 GB M4)

```yaml
api:    mem_limit: 3g
qdrant: mem_limit: 4g, mem_reservation: 2g
```

Qdrant stores quantized vectors in RAM (`always_ram=True`) and raw vectors on disk (`on_disk=True`) using Scalar int8 quantization. 10 M vectors at 1024-dim ≈ 600 MB RAM.
