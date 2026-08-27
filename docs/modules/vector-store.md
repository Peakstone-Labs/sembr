# vector_store

> Async Qdrant wrapper. Owns three collections — `intents_current` (intent vectors, query-side, full-precision), `news_current` (article vectors, on-disk + INT8 quantization) and `news_archive` (expired articles, permanent, disk-first) — and the public/stable aliases that protect callers from the underlying versioned collection names.

## Responsibility

- Construct one shared `AsyncQdrantClient` per process with a non-zero default operation timeout
- Bootstrap both collections idempotently on startup, deriving collection name and vector dimensionality from the embedder
- Expose CRUD that addresses points through the stable aliases (`intents_current`, `news_current`) so model upgrades flip storage without touching call sites
- Maintain the payload indexes the dashboard relies on (`ingested_at_ts`, `feed_id`)
- Tolerate "alias already points elsewhere" without overwriting it — alias migration is a separate flow, not bootstrap

## Not in scope

- Generating vectors (lives in `embedder`)
- Search / ANN logic (lives in `matcher`; this module exposes the raw `client` for read-side callers that need it)
- Multi-tenant collections, sharding, or replication
- Online alias migration during a model upgrade — bootstrap will refuse to silently retarget the alias and the migration will arrive as its own feature

## Public interface

### Handle (`qdrant.py`)

```python
class QdrantHandle:
    def __init__(self, url: str, *, timeout: float = 30.0) -> None
    @property
    def client -> AsyncQdrantClient                # raw async client for callers that need ad-hoc ops
    async def ping() -> bool                       # /health probe; True iff get_collections returns
    async def close() -> None
```

The 30 s default timeout is a per-operation floor — without it, a stuck Qdrant can hang the embedder worker tick or any caller that does not wrap its own `asyncio.wait_for`.

### Intents collection (`intents.py`)

```python
ALIAS_NAME = "intents_current"

def collection_name(model_version: str) -> str    # → f"intents_{model_version}"

async def ensure_intents_collection(client, embedder) -> None
async def upsert_intent_point(client, intent_id: int, vector, payload) -> None
async def update_intent_payload(client, intent_id: int, payload) -> None
async def delete_intent_point(client, intent_id: int) -> None
```

Collection config: `size=embedder.dim`, `distance=COSINE`, `on_disk=False`, **no quantization**. Intent vectors are query-side in the matcher's `query_points` calls, so precision matters more than memory savings at the 1.0 scale (< 1000 intents, ~4 MB raw at 1024-dim).

`update_intent_payload` uses `overwrite_payload` (replace), not `set_payload` (merge), so a payload key that future code stops emitting cannot silently persist in Qdrant — the matcher reads `enabled` and `threshold` from this payload, where stale keys would be a correctness hazard.

`delete_intent_point` does not remove the SQLite row; the API caller is responsible for both halves.

### News collection (`news.py`)

```python
ALIAS_NAME = "news_current"

def collection_name(model_version: str) -> str    # → f"news_{model_version}"

async def ensure_news_collection(client, embedder) -> None
async def upsert_news_points(client, points, *, wait: bool = True) -> None
```

Collection config: `size=embedder.dim`, `distance=COSINE`, `on_disk=True`, **scalar INT8 quantization with `always_ram=True`**. Quantized vectors live in RAM, raw vectors live on disk; the dashboard's "latest articles" listing scrolls through the collection ordered by `ingested_at_ts`.

Two payload indexes are created at bootstrap (idempotent on every startup):

| Field | Type | Why |
|---|---|---|
| `ingested_at_ts` | INTEGER | Required for the dashboard's `scroll(order_by=...)`; Qdrant rejects un-indexed order keys |
| `feed_id` | INTEGER | The Feeds tab drill-down filters by `feed_id`; without this the lookup degrades to a full-collection scan |
| `title` | text (MULTILINGUAL tokenizer) | Dashboard title keyword search; the MULTILINGUAL tokenizer segments CJK at character level — the WORD tokenizer would treat a Chinese title as one long token and drop it |

`upsert_news_points` is a thin alias-routing helper. The caller still owns `PointStruct` construction because the embedder worker has model-version metadata it must inject into payloads; the helper exists to keep the alias name from being duplicated at every write site.

### News archive collection (`news_archive.py`)

```python
ARCHIVE_ALIAS = "news_archive"

def archive_collection_name(model_version: str) -> str    # → f"news_archive_{model_version}"

async def ensure_news_archive_collection(client, embedder) -> None
async def upsert_archive_points(client, points, *, wait: bool = True) -> None

# payload enrichment (pure functions, applied at migration time)
def parse_published_at_ts(published_at) -> int | None
def detect_lang(title, body) -> str               # "zh" / "en" / "other"
def extract_url_domain(url) -> str | None
def build_archive_point(point, matched_intents, archived_at_ts) -> PointStruct | None
```

The archive permanently keeps every article the retention job expires out of `news_current`, vector included, so old news stays semantically searchable through `POST /api/archive/search` (see the api module doc). The maintenance job **moves** points instead of deleting them: retrieve with vectors → enrich the payload → upsert into `news_archive` with `wait=True` → only then delete from `news_current` and cascade the SQLite rows. A failed archive write aborts the run with nothing deleted, so an article can never end up in neither store. Every run logs one summary line with `archived=` / `deleted_qdrant=` counters — on a clean run the two are equal. Setting `QDRANT_ARCHIVE_ENABLED=false` reverts the job to plain deletion (pre-archive behavior); the collection and its endpoints stay readable.

Storage is **disk-first**, deliberately different from `news_current`: INT8 quantization with `always_ram=False`, `hnsw_config.on_disk=True`, and every payload index `on_disk=True`. The archive grows without bound inside the same memory-capped Qdrant container that serves the hot matcher/ingest path, so nothing archive-sized may pin RAM; archive queries are rare ad-hoc lookups where disk latency is acceptable.

Archived payloads keep the original article fields and add six derived ones:

| Field | Meaning |
|---|---|
| `published_at_ts` | epoch seconds parsed from `published_at`; **absent** when the source timestamp is missing, unparseable, or timezone-naive (guessing a timezone would silently shift the article by hours). Absent fields never match a range filter |
| `body_len` | `len(body)` — length-floor filtering to skip stub articles |
| `lang` | `zh` / `en` / `other` from a cheap CJK-vs-latin character heuristic |
| `url_domain` | lowercased hostname without `www.` |
| `matched_intents` | intent ids that had matched the article while it was live — captured at migration time, immediately before the cascade delete erases that history from SQLite. Intents deleted before migration are not represented. Always present (possibly empty) |
| `archived_at_ts` | migration timestamp, for audits |

All eight payload indexes (`ingested_at_ts`, `published_at_ts`, `body_len` as range-only integers; `feed_id`, `matched_intents` as lookup-only integers; `url_domain`, `lang` keywords; `title` MULTILINGUAL text) are created at bootstrap, before the API routers serve.

**Backup**: the archive is the *only* copy of expired articles — their SQLite rows are deleted at migration. Deleting the collection or the Qdrant volume loses them permanently. Snapshot with `curl -X POST http://localhost:6333/collections/news_archive/snapshots` on the Qdrant host and copy the snapshot file off the box.

## Configuration

| Field | Default | Notes |
|---|---|---|
| `qdrant_url` | `http://qdrant:6333` | passed straight to `AsyncQdrantClient` |

The 30 s operation timeout is currently a module constant (`_DEFAULT_TIMEOUT_SECONDS` in `qdrant.py`); promote to a setting if a deployment needs to tune it.

## Upstream dependencies

- `embedder.base.BaseEmbedder` — both bootstrap helpers read `embedder.model_version` and `embedder.dim` to derive collection name and vector size in lockstep with the embedding backend

## Downstream consumers

- `api.intents` — full CRUD (`upsert_intent_point`, `update_intent_payload`, `delete_intent_point`)
- `embedder.scheduler.embedder_worker` — `upsert_news_points` after each batch embed
- `matcher` — reads raw `client` to run `query_points` against `news_current` and `intents_current`
- `dashboard.read_model` — reads raw `client` to scroll articles for the dashboard panels
- `main.lifespan` — `QdrantHandle` construction + `ensure_*_collection` calls at startup

## Known constraints

- **Single-process bootstrap**: `ensure_*_collection` does check-then-create, which is not atomic. Two containers racing at startup can both decide the collection is missing and one will fail at `create_collection`. Single-container Docker deployments are unaffected; multi-instance setups need an external lock or a catch-and-ignore wrapper.
- **Alias migration is out of band**: when `intents_current` / `news_current` already points to a different collection at startup, bootstrap logs a warning and leaves it alone. Switching the alias for a model upgrade is the upgrade flow's job, not bootstrap's.
- **Lockstep with embedder model identity**: collection names and vector dimensionality are derived from `embedder.model_version` / `embedder.dim`. Subclass the embedder rather than monkey-patching either property — the rest of the stack assumes both stay stable for the lifetime of a process.
- **`PointStruct` construction stays at call sites**: write helpers do not synthesize point payloads because the worker / API layer owns the payload schema (notably `embedding_model_version`, `ingested_at_ts`, intent metadata). The helpers only own the alias and the wait/timeout policy.
- **Quantization asymmetry**: the news collection is quantized; the intents collection is not. Search-time precision was prioritized over memory on the query-side; a future intents collection that grows past ~10× the 1.0 target should reconsider this.
