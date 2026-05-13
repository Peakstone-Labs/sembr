# Architecture

## Reverse RAG

sembr inverts the standard RAG query pattern:

| | Traditional RAG | sembr (Reverse RAG) |
|-|-----------------|---------------------|
| When | On user query | Continuously, on each intent's own schedule |
| What is stored | Documents | User intent vectors |
| What is searched | Documents for a query | Articles for each intent |
| Latency | Query-time | Background job |

Intent vectors are computed **once** at creation time and stored in Qdrant. Each scheduled tick (or each freshly-ingested article, in event mode) drives an ANN search against the news collection — never a re-embed of the intent text.

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
[vector_store / news_current alias] ──► Qdrant, payload includes ingested_at_ts + feed_id
      │
      │  per-intent schedule (cron or event)
      ▼
[matcher] ──► query_points(query=intent_vector, score_threshold=..., query_filter=...)
      │
      ▼
[summarizer] ──► chat completions (Jinja2 templates, per-intent system + instruction)
      │
      ▼
[notifier / email] ──► SMTP digest, rendered in the intent's timezone
```

The same diagram holds for both schedule modes — the matcher's trigger differs (APScheduler tick vs ingestion-driven event buffer drain) but everything downstream is identical.

## Per-intent schedules

Two modes, picked per intent at creation time:

- **Cron mode**: `preset: "hourly" | "daily" | "weekly"`, `hour` / `minute` / `weekday` as appropriate, `lookback_seconds` (default 86400, range 5 min – 30 days), `skip_seen` (default true). The matcher job runs against articles ingested within the lookback window
- **Event mode**: `trigger_count` (1–10, default 3) and `max_wait_seconds` (60–86400, default 1800). Articles matching the intent buffer in `event_pending` and fire the summarizer once either the count is reached or the wait expires — whichever comes first

`schedule.mode` is **immutable** — changing modes requires DELETE + POST. Within a mode, every other field is editable via PUT.

## Dual-collection Qdrant design

Two Qdrant collections, both accessed via aliases so a model swap can re-point them atomically:

- `intents_current` — pre-computed intent vectors, one point per intent, full precision (no quantization — query-side precision matters)
- `news_current` (today: `news_bge-m3_v1`) — article vectors with INT8 scalar quantization in RAM and full vectors on disk; payload index on `ingested_at_ts` and `feed_id`

The matcher calls `query_points(query=vector, score_threshold=..., query_filter=...)` per intent. (The older `search_batch` API was removed in qdrant-client 1.10.) Intent vectors live in their own collection so the matcher can refresh just one side independently.

**Collection naming** follows `news_{model}_{version}` to enable zero-downtime model upgrades: provision a new collection in parallel, re-embed in background, then atomically switch the `news_current` alias. Every payload carries `embedding_model_version` so a partial cutover is identifiable.

## Deduplication

Two layers:

1. **Exact**: `MD5(url + title)` fingerprint stored in `feed_items`; collector skips already-seen articles before they reach `pending_articles`
2. **Per-intent semantic**: `match_seen` rows record `(intent_id, article_id)` after each successful summarize; cron-mode intents that re-scan the same lookback window won't re-fire the same article

`match_seen` cascades on intent delete. A PUT that changes the intent's text clears `match_seen` for that intent so the re-embedded vector can re-match articles it would otherwise have skipped.

## Prompt templates

Templates are flat `.md` files under `/app/prompts/{system,instruction}/`, bind-mounted **read-write** from the host's `./prompts/` so the dashboard can edit them at runtime. The summarizer reads the file on every tick (`sembr/summarizer/templates.py::load_template`) — there is no in-memory cache to invalidate, so a host-side or dashboard-driven save reaches the next digest with no restart.

```
templates layer (filesystem-only on save/delete; cross-boundary on rename):
  POST   /api/prompts/templates/{kind}                  ──► save_template_atomic (.tmp + os.replace)
  PUT    /api/prompts/templates/{kind}/{name}           ──► save_template_atomic
  DELETE /api/prompts/templates/{kind}/{name}           ──► delete_template (after 409 ref-check)
  POST   /api/prompts/templates/{kind}/{name}/rename    ──► os.rename → db.transaction() UPDATE intents
                                                            (UPDATE failure → reverse os.rename)
```

The reserved name `default` is enforced both at the API layer (HTTP 403 on writes / 422 on reuse-as-target) and in the `BUILTIN_NAMES` frozenset in `sembr/summarizer/templates.py`. Per-file size cap is 64 KiB. Strict placeholder validation (`try_render`) runs on every save so a typo in `{intent_text}` cannot poison the next digest. Rename is the only template operation that crosses into SQLite: the file move runs first, the cascade `UPDATE intents SET {kind}_template = ?` runs inside `db.sqlite.transaction()` afterwards, and a SQLite failure triggers reverse `os.rename` so file and DB never diverge.

## Delivery

Email is the only channel that ships. `EmailChannel` is a `BaseChannel` subclass with no abstract methods (the marker-ABC pattern — per-channel `send` signatures diverge enough that a common ABC would erase typing). The dispatcher in `main.py` pattern-matches on the channel config type:

```python
for ch in intent.channels:
    if isinstance(ch, EmailChannelConfig):
        await email_ch.send(result, config=ch, intent_name=intent.name,
                            intent_timezone=intent.timezone)
```

Adding a channel — Telegram, Discord, Slack — is additive: a new `XConfig: BaseModel` with its own `Literal["x"]` discriminator value, a new channel class, and a new `isinstance` arm. The discriminated union on `Intent.channels` makes the API boundary validate channel-specific parameters before any side effect runs.

There is **no `notification_log` retry/DLQ machinery** in 1.0 — a failed send is logged and dropped. Cron-driven intents pick up missed deliveries on the next tick by virtue of their lookback window; event-driven intents lose the buffered tick on send failure. A stateful retry pipeline is post-1.0 work.

## SQLite + WAL

WAL mode (`journal_mode=WAL; synchronous=NORMAL; cache_size=-64000; busy_timeout=5000`) is initialized at startup and verified by reading the pragma back. Readers never block writers; the application uses a single shared `aiosqlite` connection per process and serializes multi-statement writes through an asyncio lock exposed as `transaction()`.

## APScheduler integration

`AsyncIOScheduler` started inside the FastAPI `lifespan` context manager. Every job sets `coalesce=True` to prevent backlog on recovery; shutdown uses `wait=False` so uvicorn teardown isn't blocked by an in-flight tick. The same scheduler runs the per-feed RSS polls, the per-intent matcher jobs (cron-mode), the embedder worker, and the hourly dashboard log retention prune.

## Source registration

The 1.0 source registry is a hardcoded `SOURCE_REGISTRY: dict[str, type[BaseSource]]` in `collector.scheduler` (currently `{"rss": RSSSource}`). The dashboard reads this dict to populate the create-feed form, so adding an HTTP/JSON source means subclassing `BaseSource` and registering in that dict — the form picks it up on the next dashboard page load. A `pyproject.toml` entry-points-driven plugin discovery layer is on the post-1.0 roadmap but does not exist today.

## Docker Compose memory limits

```yaml
api:    mem_limit: 1500m, mem_reservation: 512m
qdrant: mem_limit: 2g,    mem_reservation: 1g
rsshub: mem_limit: 512m
```

Right-sized against live measurement (api ~125 MiB, rsshub ~355 MiB, qdrant ~520 MiB at the default 53-source workload — total ~1 GB in use). Each limit leaves ~4× headroom for cron-mode batch scans, concurrent LLM summarizer calls, and Qdrant ANN bursts. Raise `qdrant.mem_limit` to 4G+ if you ingest millions of articles; raise `api.mem_limit` to 3G if you run tens of intents with concurrent fire bursts.

Qdrant stores quantized vectors in RAM (`always_ram=True`) and raw vectors on disk (`on_disk=True`) using INT8 scalar quantization. 10 M vectors at 1024-dim ≈ 600 MB RAM.
