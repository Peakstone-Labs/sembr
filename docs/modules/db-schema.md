# db Schema Reference

Column-by-column reference for every SQLite table sembr creates. The DDL source of truth is in `sembr/db/*.py` — this page mirrors it for SQL/ops use. If they ever disagree, the DDL wins.

All `TEXT` timestamp columns store ISO-8601 in UTC (e.g. `2026-05-05T12:34:56Z`). All boolean-ish columns are `INTEGER` (0/1) — SQLite has no native bool.

---

## `feeds`

User-facing feed registry. One row per RSS source.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `id` | `INTEGER` | autoincrement | `PRIMARY KEY` | Surrogate key |
| `name` | `TEXT` | — | `NOT NULL` | Display name |
| `url` | `TEXT` | — | `NOT NULL UNIQUE` | Feed URL — unique key for dedup at create time |
| `source_type` | `TEXT` | `'rss'` | `NOT NULL` | Source plugin name; only `rss` ships in 1.0 |
| `config` | `TEXT` | `'{}'` | `NOT NULL` | JSON-encoded source-specific config |
| `poll_interval_minutes` | `INTEGER` | `30` | `NOT NULL` | APScheduler interval (5–1440) |
| `last_collected_at` | `TEXT` | `NULL` | — | Last successful fetch (UTC ISO-8601) |
| `created_at` | `TEXT` | `datetime('now')` | `NOT NULL` | Row creation time |
| `enabled` | `INTEGER` | `1` | `NOT NULL` | 0 = paused (added via idempotent migration `_ensure_enabled_column`) |

**Indexes**: PK on `id`, UNIQUE on `url`.

---

## `feed_items`

MD5 fingerprint dedup. One row per article ever ingested from any feed.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `md5` | `TEXT` | — | `PRIMARY KEY` | `MD5(url + title)`, 32 lowercase hex chars |
| `feed_id` | `INTEGER` | — | `NOT NULL`, FK → `feeds(id)` `ON DELETE CASCADE` | Owning feed |
| `collected_at` | `TEXT` | `datetime('now')` | `NOT NULL` | Ingestion time |

**Indexes**: PK on `md5`, `idx_feed_items_feed_id` on `feed_id` (used for per-feed scans).

**Lifecycle**: written inside `insert_article_pending` together with `pending_articles`; survives forever as the dedup ledger. Cascade-deleted when the feed is deleted.

---

## `feed_tags`

Tag set per feed.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `feed_id` | `INTEGER` | — | `NOT NULL`, FK → `feeds(id)` `ON DELETE CASCADE` | Feed owner |
| `tag` | `TEXT` | — | `NOT NULL` | Tag string |

**Primary key**: composite `(feed_id, tag)`.

**Indexes**: `idx_feed_tags_tag` on `(tag, feed_id)` — supports "list all feeds with tag X" without table scan.

---

## `seeded_feeds`

Tracks every URL ever seeded from `INITIAL_FEEDS` so deletes are sticky.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `url` | `TEXT` | — | `PRIMARY KEY` | Feed URL ever auto-seeded |

**Invariant**: once a URL is in `seeded_feeds` it stays forever, even if the feed row is deleted. `seed_initial_feeds` skips URLs already here, so user-deleted seed feeds never come back on restart.

**Scope**: only `INITIAL_FEEDS` entries — user `POST /feeds` writes do **not** populate this table.

---

## `pending_articles`

Articles fetched but not yet embedded. Row presence is the only state indicator — there is no `status` column.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `md5` | `TEXT` | — | `PRIMARY KEY` | Same `MD5(url + title)` as `feed_items.md5` |
| `feed_id` | `INTEGER` | — | `NOT NULL`, FK → `feeds(id)` `ON DELETE CASCADE` | Owning feed |
| `url` | `TEXT` | — | `NOT NULL` | Article URL |
| `title` | `TEXT` | — | `NOT NULL` | Article title |
| `body` | `TEXT` | — | `NOT NULL` | Article body, hard-capped at 1 MB (`_BODY_CAP_BYTES`) |
| `published_at` | `TEXT` | `NULL` | — | Article publish time as reported by source |
| `retry_count` | `INTEGER` | `0` | `NOT NULL` | Embedding-attempt counter |
| `created_at` | `TEXT` | `datetime('now')` | `NOT NULL` | When inserted into the queue |

**Indexes**:

- PK on `md5`
- `idx_pending_articles_feed_id` on `feed_id`
- `idx_pending_articles_retry` on `retry_count` — covers `WHERE retry_count < ?`; ORDER BY rowid within the filtered set is FIFO

**Lifecycle**: inserted by collector inside `insert_article_pending`; pulled in batches by embedder via `pull_pending_batch` (FIFO by rowid); deleted on success or moved to `dead_articles` after `retry_count >= max_retry`.

---

## `dead_articles`

Articles that exhausted retry budget. Kept for forensics — no FK cascade, so deleting a feed leaves its dead articles for postmortem.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `md5` | `TEXT` | — | `PRIMARY KEY` | Same fingerprint |
| `feed_id` | `INTEGER` | — | nullable, no FK | Original feed (NULL if feed since deleted by admin) |
| `url` | `TEXT` | — | `NOT NULL` | Article URL |
| `title` | `TEXT` | — | `NOT NULL` | Article title |
| `body` | `TEXT` | — | `NOT NULL` | Captured body |
| `published_at` | `TEXT` | `NULL` | — | As reported by source |
| `error_message` | `TEXT` | `NULL` | — | Last exception that caused demotion (per-batch attribution from `demote_md5s_to_dead`) |
| `failed_at` | `TEXT` | `datetime('now')` | `NOT NULL` | When demoted |

**Indexes**: PK on `md5`, `idx_dead_articles_failed_at` on `failed_at`.

**Conflict policy**: `INSERT OR REPLACE` on demotion preserves the latest `failed_at` and `error_message` if a row was previously demoted then re-queued.

---

## `intents`

User-defined monitoring intents.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `id` | `INTEGER` | autoincrement | `PRIMARY KEY` | Surrogate key |
| `name` | `TEXT` | — | `NOT NULL` | User label |
| `text` | `TEXT` | — | `NOT NULL` | Natural-language intent — embedded into Qdrant `intents` collection |
| `threshold` | `REAL` | `0.75` | `NOT NULL` | Cosine similarity cutoff (0.20–0.95) |
| `enabled` | `INTEGER` | `1` | `NOT NULL` | 0 = paused; matcher skips |
| `channels` | `TEXT` | `'[]'` | `NOT NULL` | JSON array of `ChannelConfig` — discriminated union; `email` is the only built-in `type` today |
| `tags` | `TEXT` | `'[]'` | `NOT NULL` | JSON array of free-form tag strings |
| `system_template` | `TEXT` | `'default'` | `NOT NULL` | Filename (no `.md`) under `prompts/system/` |
| `instruction_template` | `TEXT` | `'default'` | `NOT NULL` | Filename (no `.md`) under `prompts/instruction/` |
| `feed_filter` | `TEXT` | `'null'` | `NOT NULL` | JSON `FeedFilter` or `'null'` to scan all feeds — distinguishes omitted from explicit-null via `model_fields_set` on update |
| `schedule` | `TEXT` | `'{}'` | `NOT NULL` | JSON `Schedule`: `{mode: 'cron', preset, hour, minute, weekday, lookback_seconds, skip_seen}` or `{mode: 'event', ...}` |
| `timezone` | `TEXT` | `'UTC'` | `NOT NULL` | IANA timezone for cron evaluation |
| `language` | `TEXT` | `'zh'` | `NOT NULL` | Output language for LLM summary |
| `created_at` | `TEXT` | `datetime('now')` | `NOT NULL` | Row creation |
| `updated_at` | `TEXT` | `datetime('now')` | `NOT NULL` | Last mutation; `update_intent_raw` rollback intentionally writes the original value |

**Indexes**: PK on `id`. No secondary indexes (intent count is small — < 1000 expected).

---

## `match_seen`

Deduplication log: which articles each intent has already alerted on.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `intent_id` | `INTEGER` | — | `NOT NULL`, FK → `intents(id)` `ON DELETE CASCADE` | Intent that matched |
| `article_id` | `TEXT` | — | `NOT NULL` | Qdrant point ID (string form) of the matched article |
| `first_matched_at` | `TEXT` | `datetime('now')` | `NOT NULL` | When this pair was first seen |

**Primary key**: composite `(intent_id, article_id)`.

**Lifecycle**: written by matcher via `insert_unseen_returning_new` (single multi-row `INSERT OR IGNORE … RETURNING`). Cleared on intent text mutation via `clear_intent` — the new query vector means previously seen articles must be re-evaluated.

---

## `event_pending`

Buffer for the event-driven matcher path. DDL is in `db/event_buffer.py`; absorb/flush logic lives in `matcher/event_buffer.py`.

| Column | Type | Default | Constraints | Meaning |
|--------|------|---------|-------------|---------|
| `intent_id` | `INTEGER` | — | `NOT NULL`, FK → `intents(id)` `ON DELETE CASCADE` | Intent owning the buffered group |
| `group_id` | `INTEGER` | — | `NOT NULL` | Cluster id within this intent (assigned during absorb) |
| `rep_article_id` | `TEXT` | — | `NOT NULL` | Representative article for the cluster (highest-score member) |
| `rep_title_norm` | `TEXT` | — | `NOT NULL` | Normalised title used for semantic-dedup comparison across new arrivals |
| `members_json` | `TEXT` | — | `NOT NULL` | JSON list of `{article_id, score}` cluster members |
| `created_at` | `TEXT` | — | `NOT NULL` | When the cluster was first created |

**Primary key**: composite `(intent_id, group_id)`.

---

## Cascade summary

```
DELETE FROM feeds WHERE id = X
    ├─ deletes feed_items WHERE feed_id = X         (FK CASCADE)
    ├─ deletes feed_tags WHERE feed_id = X          (FK CASCADE)
    ├─ deletes pending_articles WHERE feed_id = X   (FK CASCADE)
    └─ does NOT touch dead_articles                 (no FK — kept for forensics)

DELETE FROM intents WHERE id = Y
    ├─ deletes match_seen WHERE intent_id = Y       (FK CASCADE)
    └─ deletes event_pending WHERE intent_id = Y    (FK CASCADE)
```

`PRAGMA foreign_keys=ON` is required for cascades to fire — set globally in `init_sqlite`.
