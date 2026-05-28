# db

> SQLite persistence layer. Single-file durable state for feeds, intents, articles, and the matching pipeline.

For column-by-column schema (types, defaults, constraints, indexes), see [db Schema Reference](db-schema.md).

## Responsibility

- Open and own a single global `aiosqlite` connection per process
- Apply WAL mode pragmas at startup and verify they actually stuck
- Provide an `async with transaction()` context manager that serialises multi-statement writes via an asyncio lock
- Define and migrate table schemas (idempotent DDL — `CREATE TABLE IF NOT EXISTS`)
- Expose async CRUD helpers for each table

## Not in scope

- Vector storage (lives in `vector_store`, on Qdrant)
- Connection pooling — sembr uses a single shared connection
- Schema versioning beyond `ALTER TABLE … ADD COLUMN` idempotent migrations
- Transactions across processes — single-process only

## Tables

| Table | Owner | Purpose |
|-------|-------|---------|
| `feeds` | `feeds.py` | Feed registry (name, url, poll_interval, enabled) |
| `feed_items` | `feeds.py` | MD5 fingerprint dedup — one row per ingested article |
| `feed_tags` | `feed_tags.py` | (feed_id, tag) composite-PK tag set, `ON DELETE CASCADE` |
| `seeded_feeds` | `feeds.py` | URLs ever seeded from `INITIAL_FEEDS` — never re-seeded once present, even after user delete |
| `pending_articles` | `articles.py` | Articles awaiting embedding — row presence is the only state indicator |
| `dead_articles` | `articles.py` | Articles that exhausted retry budget — kept for forensics, no FK cascade |
| `intents` | `intents.py` | User-defined monitoring intents (text, threshold, schedule, channels) |
| `match_seen` | `match_seen.py` | (intent_id, article_id) dedup log — `ON DELETE CASCADE` from intents |
| `event_pending` | `event_buffer.py` | Event-driven matcher buffer — DDL only; logic in `matcher.event_buffer` |
| `summary_history` | `summary_history.py` | Persisted cron summary rows — `(intent_id, run_at)` unique, raw LLM output + citations JSON |

## Public interface

### Connection lifecycle (`sqlite.py`)

```python
await init_sqlite(path: str) -> aiosqlite.Connection
get_conn() -> aiosqlite.Connection
await close_sqlite() -> None
await sqlite_ok() -> bool                # /health probe
async with transaction() as txn: ...     # serialised BEGIN…COMMIT
```

### Feeds (`feeds.py`)

```python
await init_feed_tables(conn)
await seed_initial_feeds(conn) -> int
await create_feed(conn, name, url, *, source_type='rss', config=None,
                  poll_interval_minutes=30, tags=None) -> Feed
await list_feeds(conn) -> list[Feed]                  # no tags
await list_feeds_with_tags(conn) -> list[Feed]        # single tag scan, no N+1
await get_feed(conn, feed_id) -> Feed | None
await get_feed_names(conn, feed_ids) -> dict[int, str]
await update_feed(conn, feed_id, *, tags=None, **fields) -> Feed | None
await delete_feed(conn, feed_id) -> bool
await update_last_collected(conn, feed_id)
await fingerprint_exists(conn, md5) -> bool
await insert_fingerprint(conn, md5, feed_id)
```

`_UPDATABLE_FEED_COLS = {name, config, poll_interval_minutes, enabled}` — any other field passed to `update_feed` raises `ValueError`.

### Intents (`intents.py`)

```python
await init_intent_tables(conn)
await create_intent(conn, body: IntentCreate) -> Intent
await list_intents(conn, enabled: bool | None = None) -> list[Intent]
await get_intent(conn, intent_id) -> Intent | None
await update_intent(conn, intent_id, body: IntentUpdate) -> Intent
await update_intent_raw(conn, intent_id, snapshot: Intent)        # PUT rollback
await delete_intent(conn, intent_id) -> bool
await intents_remove_feed_id(conn, feed_id) -> list[int]          # caller commits
```

`update_intent` uses `body.model_fields_set` to distinguish *omitted* from *explicit null* — only `feed_filter` is meaningfully nullable, so it's the only field that benefits.

`intents_remove_feed_id` is the **only** intent function that does not commit; it is designed to run in the same transaction as `delete_feed` so feed deletion and intent filter cleanup land atomically.

### Articles (`articles.py`)

```python
await init_article_tables(conn)
await insert_article_pending(conn, article: RawArticle, feed_id) -> bool
await pull_pending_batch(conn, batch_size, max_retry) -> list[PendingRow]
await increment_retry(conn, md5s)
await delete_pending(conn, md5s)
await demote_to_dead(conn, max_retry, error_message) -> int        # global cleanup
await demote_md5s_to_dead(conn, md5s, error_message) -> int        # batch-scoped attribution
```

Body is hard-capped at 1 MB (`_BODY_CAP_BYTES`). MD5 must be 32 lowercase hex chars (`_MD5_RE`); invalid input raises `ValueError`.

`demote_md5s_to_dead` is preferred over `demote_to_dead` when the caller knows the exact exception that exhausted retries — it preserves per-batch error attribution.

### match_seen (`match_seen.py`)

```python
await init_match_seen_tables(conn)
await insert_unseen_returning_new(conn, intent_id, article_ids) -> list[str]
await clear_intent(conn, intent_id)             # call when intent.text mutates
```

`insert_unseen_returning_new` uses a single multi-row `INSERT OR IGNORE … RETURNING` (SQLite 3.35+) — no N round-trips. Safe up to `SQLITE_MAX_VARIABLE_NUMBER` (32 766 bound params) — the matcher's `_SEARCH_LIMIT=100` produces 200 params, well under the limit.

### feed_tags (`feed_tags.py`)

```python
await init_feed_tag_tables(conn)
await insert_tags_in_tx(conn, feed_id, tags)    # caller manages BEGIN/COMMIT
await replace_tags_in_tx(conn, feed_id, tags)   # caller manages BEGIN/COMMIT
await get_tags(conn, feed_id) -> list[str]
await list_all_tags(conn) -> dict[int, list[str]]
```

`*_in_tx` helpers do **not** commit and are designed to be called inside a `transaction()` block.

### event_buffer (`event_buffer.py`)

DDL-only module. Business logic for absorb/flush lives in `sembr/matcher/event_buffer.py`.

```python
await init_event_buffer_tables(conn)
```

### summary_history (`summary_history.py`)

```python
await init_summary_history_table(conn)
await migrate_summary_history_unique_index(conn)  # one-shot DDL migration
await save_summary(conn, intent_id, run_at, summary, citations) -> int
await save_summary_or_skip(conn, intent_id, run_at, summary, citations) -> int | None
await list_summaries(conn, intent_id, *, limit=50, offset=0) -> list[dict]
await list_summaries_between(conn, intent_id, since_utc, until_utc) -> list[dict]
await delete_summary(conn, row_id) -> bool
await format_history_text(conn, intent_id, limit=50) -> str
```

`summary_history` stores one row per cron tick that produced a summary. The `(intent_id, run_at)` unique constraint prevents duplicate rows from a concurrent backfill and a normal cron tick firing for the same fire-time.

`save_summary_or_skip` returns the row id on insert and `None` on unique-constraint conflict — the caller can distinguish "row persisted" from "already present" without an extra query.

`format_history_text` joins the most recent N rows into a single Markdown-formatted string suitable for `{history}` placeholder injection in the summarizer pipeline.

## Concurrency model

- One shared `aiosqlite.Connection` per process
- aiosqlite serialises commands through an internal queue, so no two statements execute simultaneously
- `_WRITE_LOCK: asyncio.Lock` serialises all *multi-statement* writes — without it, two coroutines racing to `BEGIN` produces SQLite's "cannot start a transaction within a transaction" error
- WAL mode admits one writer at a time anyway, so the lock costs nothing in throughput

**Rule for new write functions**: if you write to SQLite, do it inside `async with transaction() as txn:`. Never call `conn.commit()` directly in module code — a bare commit inside another coroutine's open transaction will prematurely commit a partial write.

The single exception is `intents_remove_feed_id`, which is documented as caller-commit because it must compose with `delete_feed` in one transaction.

## Pragmas

Set once in `init_sqlite` and verified to have stuck:

```sql
PRAGMA journal_mode=WAL
PRAGMA synchronous=NORMAL
PRAGMA cache_size=-64000        # ~64 MiB page cache
PRAGMA busy_timeout=5000        # 5 s wait for writers
PRAGMA foreign_keys=ON
```

`init_sqlite` raises `RuntimeError` if `journal_mode` does not actually become `wal` — this catches network-share filesystems where WAL silently falls back to journal mode and corrupts data.

## Upstream dependencies

None. `db` is the foundation of the stack.

## Downstream consumers

Everything else: `collector` (feeds, articles, fingerprints), `embedder` (pulls pending batch), `matcher` (intents, match_seen), `summarizer` (intents), `notifier` (notification_log via `db`), `api` (CRUD facade), `dashboard` (read-model aggregation).

## Known constraints

- **Filesystem**: WAL mode is unsafe on NFS / SMB / virtio-9p. Keep `./data/` on a local POSIX-ish filesystem (ext4 / APFS / NTFS local).
- **Single process**: No connection sharing across processes. The dashboard's `RestartController` flow assumes the API container restarts cleanly to release the connection.
- **Schema drop limit**: `ALTER TABLE … DROP COLUMN` requires SQLite 3.35+ (Python 3.12 ships with 3.40+, so this is safe but worth knowing).
- **Config column shadowing**: `feeds.config` is JSON-encoded as text; nested updates require fetching, mutating in Python, and re-writing.
