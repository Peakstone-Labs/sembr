# embedder

> Converts article and intent text to 1024-dim BGE-M3 vectors via the SiliconFlow `/v1/embeddings` API. No local model, no thread pool — pure async `httpx`.

## Responsibility

- Define the `BaseEmbedder` ABC so callers (matcher, intent CRUD) depend only on an interface, not a backend
- Provide the `SiliconFlowEmbedder` concrete backend for the OpenAI-compatible `/v1/embeddings` protocol
- Run a startup probe (`load()`) that validates API key, model, and embedding dimension before flipping `is_loaded` to True
- Drive the embed-then-upsert pipeline: pull pending articles from SQLite, embed them, upsert to Qdrant, then delete from `pending_articles`
- Emit per-batch observability events (`log_embed_event`) for the dashboard

## Not in scope

- Vector storage / search (lives in `vector_store`)
- Local-model inference (no MLX / sentence-transformers — remote API only in this release)
- LLM summary embedding (the summarizer does not embed; it generates text)
- Schema migration or article ingestion (lives in `collector` + `db`)

## Public interface

### Base ABC (`base.py`)

```python
class BaseEmbedder(ABC):
    @property
    @abstractmethod
    def model_version(self) -> str: ...        # persisted in payload as embedding_model_version
    @property
    @abstractmethod
    def dim(self) -> int: ...                  # vector dimensionality; vector store uses this to size collections
    @property
    @abstractmethod
    def max_input_chars(self) -> int: ...      # per-text char cap the worker enforces before aembed
    @property
    @abstractmethod
    def is_loaded(self) -> bool: ...           # False until startup probe succeeds
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    async def aembed(self, texts: list[str], *, timeout: float | None = None) -> list[list[float]]: ...
```

The default `aembed` offloads `embed` to a thread pool (suitable for local/CPU-bound backends). Remote backends override `aembed` directly and raise from `embed` to make the misuse loud.

### SiliconFlow backend (`openai_compat.py`)

```python
class SiliconFlowEmbedder(BaseEmbedder):
    MODEL_VERSION = "bge-m3_v1"
    EXPECTED_DIM = 1024
    MAX_INPUT_CHARS = 8_000   # BGE-M3 8192-token ctx, conservative for pure-Chinese

    def __init__(*, api_key, base_url='https://api.siliconflow.cn/v1',
                 model='BAAI/bge-m3', timeout=30.0)
    async def load() -> None                   # idempotent probe; never raises
    async def aembed(texts, *, timeout=None)   # raises if not loaded
    async def aclose() -> None                 # for shutdown / restart

    @property
    def status -> Literal["loading", "ok", "error", "closed"]
```

`__init__` rejects any `model` whose name does not contain `bge-m3` — the class is bge-m3-specific (1024-dim probe, fixed `MODEL_VERSION`). Other models need their own subclass.

`load()` never raises: a failed probe sets `status="error"` and leaves `is_loaded=False` so the caller's `/health` endpoint reports 503 indefinitely until operators fix the credentials / network.

`aclose()` sets `status="closed"` (not `"error"`) so observability can distinguish a deliberate shutdown from a probe failure.

Errors raised from `aembed`:

| Exception | Meaning | Operator action |
|---|---|---|
| `EmbedderTransportError` | non-2xx HTTP, connect/read failure | check API key, network, SiliconFlow status |
| `EmbedderSchemaError` | response missing `data` / wrong shape | sembr bug or incompatible endpoint |
| `RuntimeError("embedder not loaded")` | called before `load()` succeeded | fix the upstream cause; the probe is gated on `/health` |

API key is stripped from logged response bodies (`safe_body = response.text[:200].replace(self._api_key, "***")`).

### Factory (`factory.py`)

```python
def build_embedder(settings: Settings) -> BaseEmbedder
```

Selects backend by `settings.embedder_backend`. Only `"siliconflow"` is registered today; future backends (Voyage / Jina) are anticipated but not implemented. Raises `ValueError` on empty API key or unknown backend.

### Embed-then-upsert worker (`scheduler.py`)

```python
def add_embedder_worker_job(scheduler, embedder, qdrant, app=None) -> None
async def embedder_worker(embedder, qdrant, app=None) -> None
```

`add_embedder_worker_job` registers an `IntervalTrigger(seconds=30)` job under id `embedder_worker` with `coalesce=True, max_instances=1` and an initial delay of 30 s (gives `load()` time to settle).

`embedder_worker` is the coroutine. One tick = one batch:

1. Skip silently if `embedder.is_loaded` is False (no event row written — only actual call attempts produce events)
2. `pull_pending_batch(BATCH_SIZE=32, MAX_ATTEMPTS=3)` — empty queue is a no-op, no event row
3. Truncate each `(title + "\n\n" + body)` to `embedder.max_input_chars` (8000 for the bge-m3 backend)
4. Compute dynamic timeout `max(30, total_chars / 1500)` — ~1500 chars/sec is a conservative throughput floor that covers server queueing + BGE-M3 forward + RTT
5. `embedder.aembed(texts, timeout=...)` — on failure: `increment_retry`; if retry+1 ≥ MAX_ATTEMPTS for a row, `demote_md5s_to_dead` for that row only (per-row attribution, not whole batch)
6. Build `PointStruct` with deterministic UUID = `UUID(hex=md5)` so re-runs are idempotent
7. `qdrant.upsert(collection_name="news_current", points, wait=True)` — transient `httpx.ConnectError`/`TimeoutException` → log + return without incrementing the retry counter (next tick retries); other errors → increment retry, demote if at limit
8. Emit success event row with batch size + total chars + timeout
9. If `app` is passed, fire `event_match_batch` before delete (event-driven matching path)
10. `delete_pending(md5s)` — failure here is logged but swallowed; the deterministic UUID makes the next tick's re-embed safe

Module constants:

| Constant | Value | Why |
|---|---|---|
| `BATCH_SIZE` | 32 | SiliconFlow single-request input cap |
| `MAX_ATTEMPTS` | 3 | Embed+upsert attempts before demote to `dead_articles` |
| `POLL_INTERVAL_SECONDS` | 30 | Interval trigger; matches the matcher cadence |
| `ALIAS_NAME` | `"news_current"` | Qdrant alias targeted; actual collection switched at upgrade time |

## Configuration

`pydantic-settings` fields (see `sembr/config.py`):

| Field | Default | Notes |
|---|---|---|
| `embedder_backend` | `"siliconflow"` | only this value is registered |
| `embedder_api_base_url` | `https://api.siliconflow.cn/v1` | non-HTTPS / non-localhost emits a startup warning (cleartext key risk) |
| `embedder_api_key` | `""` (SecretStr) | empty fails fast in `build_embedder` |
| `embedder_model` | `BAAI/bge-m3` | must contain `bge-m3`; assertion in `__init__` |
| `embedder_timeout_seconds` | `30.0` | probe timeout + httpx default; batch path overrides via dynamic calculation |

## Upstream dependencies

- `db.articles` — `pull_pending_batch`, `delete_pending`, `increment_retry`, `demote_md5s_to_dead`
- `db.sqlite` — `get_conn` for the shared aiosqlite connection
- `dashboard.events.log_embed_event` — best-effort observability (failures never poison the worker)

## Downstream consumers

- `vector_store.qdrant.QdrantHandle` — receives `upsert` calls with `news_current` alias
- `matcher.event_match.event_match_batch` — invoked synchronously after Qdrant upsert when `app` is passed (event-driven matching path). The import is local to break a circular dependency between `embedder` and `matcher`; the planned long-term fix is to invert the direction via the in-process log bus so the embedder only emits events and the matcher subscribes.
- `api.health` and `dashboard.read_model` — read `embedder.status` for `/health` and dashboard render

## Known constraints

- **bge-m3 only**: `MODEL_VERSION` and `EXPECTED_DIM=1024` are class constants; swapping models requires a new subclass, not a config flip.
- **Non-HTTPS warning, not a hard fail**: localhost / `127.x` are allowed for development; everything else triggers a one-time warning that the API key will travel cleartext. IPv6 localhost (`http://[::1]`) is not whitelisted.
- **No retry on probe failure**: `load()` runs the probe exactly once. A bad key or network outage stays as `status="error"` until the process restarts — by design, so operators see persistent 503 instead of a silent retry loop.
- **`aembed` empty input → empty output**: returns `[]` without a network call, but `texts=[""]` (single empty string) does call SiliconFlow.
- **Idempotency relies on deterministic UUID**: `_md5_to_uuid(md5)` is the Qdrant point id. If the article md5 changes (it shouldn't — md5 is `MD5(url + title)`), the same article would write a duplicate point.
- **Worker max_instances=1**: long-running batches block the next tick. With `total_chars/1500` per batch and `BATCH_SIZE=32 × 8000 chars = 171 s` worst case, the 30 s tick can drift but `coalesce=True` prevents backlog.
- **Event-driven matching coupling**: `embedder_worker` calls into `matcher` via a local import to avoid a top-level cycle. This is a deliberate, documented smell — the matcher should subscribe to embedder-emitted events through the log bus rather than be invoked synchronously. Until that flip lands, treat the local import as load-bearing, not accidental.
