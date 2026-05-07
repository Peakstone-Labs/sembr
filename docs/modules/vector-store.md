# vector_store

> Async Qdrant wrapper. Owns two collections — `intents_current` (intent vectors, query-side, full-precision) and `news_current` (article vectors, on-disk + INT8 quantization) — and the public/stable aliases that protect callers from the underlying versioned collection name.

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

`upsert_news_points` is a thin alias-routing helper. The caller still owns `PointStruct` construction because the embedder worker has model-version metadata it must inject into payloads; the helper exists to keep the alias name from being duplicated at every write site.

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
