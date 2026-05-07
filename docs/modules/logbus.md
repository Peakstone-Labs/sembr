# logbus

> In-process log capture, tagging, ring-buffering, and asyncio fan-out. The dashboard's Logs tab subscribes to it via Server-Sent Events to show a live, tag-filtered stream without any external broker.

## Responsibility

- Capture every `logging.LogRecord` in the process via a `logging.Handler` attached to the root logger
- Route each record to one of seven UI tags (`collector`, `embedder`, `matcher`, `notifier`, `api`, `scheduler`, `http`) by logger-name prefix
- Keep the most recent N records per tag in an in-memory ring buffer so a freshly-opened dashboard tab can show context without round-tripping to disk
- Fan out new records to async subscribers (one queue per dashboard SSE connection) without blocking the logging thread
- Expose runtime per-tag level changes (e.g. raise `embedder` to DEBUG for ten minutes during an incident) without persisting them — process restart returns to the configured default

## Not in scope

- Persisting logs to disk or shipping them to an external system (stdout/stderr `StreamHandler` continues to do that independently)
- Cross-process aggregation — each FastAPI worker has its own bus
- Authentication or authorization on the SSE stream — that's the dashboard router's job
- Structured-logging formatting (JSON, OTel) — records are stored as plain Python dicts

## Architecture at a glance

```
logging.LogRecord
    │
    ▼
RingBufferHandler.emit          (any thread — sync logging API)
    │
    ▼  router.route(record) → tag
LogBus.emit(tag, entry)         (still on the calling thread)
    │       ├── deque[tag].append(entry)        (lock held ~µs)
    │       └── for each subscriber whose tag filter matches:
    │                loop.call_soon_threadsafe(_put_drop_oldest, q, entry)
    ▼
asyncio.Queue per subscriber    (only the matching tag, never others)
    │
    ▼
SSE generator (dashboard)       (async — no work on logging thread)
```

## Public interface

### Tag routing (`router.py`)

```python
ALL_TAGS: tuple[str, ...] = (
    "collector", "embedder", "matcher", "notifier",
    "api", "scheduler", "http",
)

TAG_PREFIX_MAP: list[tuple[str, str]]   # logger-name prefix → tag
THIRD_PARTY_LOGGERS_BY_TAG: dict[str, tuple[str, ...]]

def route(record: logging.LogRecord) -> str: ...
```

`route()` walks `TAG_PREFIX_MAP` sorted by prefix length descending — the longest match always wins, so the order in which entries are written in source is **not** load-bearing. An unknown logger name falls back to the `api` tag.

`THIRD_PARTY_LOGGERS_BY_TAG` is the single source of truth for stdlib loggers (`httpx`, `httpcore`, `uvicorn.access`) whose level must follow a UI tag. `install_logbus()` reads it at startup to silence them at WARNING; the dashboard's `PUT /api/dashboard/logs/level` endpoint reads the same map to resync them when the operator raises a tag.

Note that `summarizer` is intentionally tagged as `matcher` and `vector_store` / `db` / `dashboard` are tagged as `api`. The seven tags exist to give an operator a small, fixed set of streams to switch between in the UI; further granularity would clutter the tab strip without adding diagnostic value.

### Ring buffer + fan-out (`bus.py`)

```python
class LogBus:
    def emit(self, tag: str, entry: dict[str, Any]) -> None: ...

    def subscribe(
        self,
        q: asyncio.Queue[dict[str, Any] | None],
        *,
        tag: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def unsubscribe(self, q: asyncio.Queue) -> None: ...

    def set_tag_level(self, tag: str, level: int) -> None: ...
    def get_tag_levels(self) -> dict[str, int]: ...
    def tag_info(self) -> list[dict[str, Any]]: ...

def get_bus() -> LogBus: ...   # process-wide singleton
```

`emit()` is called from arbitrary threads (the logging machinery is sync). It:

1. Bails out silently if no event loop has been registered yet (pre-lifespan-start records — there's nothing to fan out to)
2. Drops the entry if its level is below the tag's current threshold
3. Appends to the per-tag deque (`maxlen = buffer_per_tag`, FIFO eviction)
4. For each subscriber whose tag filter is `None` or equals `tag`, schedules `_put_drop_oldest` on the loop via `call_soon_threadsafe`

The deque append and the fan-out scheduling happen under a single `threading.Lock`. The lock hold is microseconds (a dict lookup, a deque append, and N `call_soon_threadsafe` calls) and is required to keep the deque-write and subscriber-snapshot consistent.

`subscribe(q, tag="embedder")` is the important shape for SSE consumers: the snapshot is just that tag's deque (no flatten, no sort) and the subscriber's queue subsequently only receives entries for that one tag. A subscriber that streams `embedder` does **not** pay for traffic on the other six tags. `subscribe(q)` (no tag) is preserved for tests and any future "raw firehose" consumer; in that mode the snapshot flattens all tag deques and is sorted by timestamp outside the lock.

`_put_drop_oldest` is the queue-write helper. If the subscriber's queue is full it evicts the oldest entry and tries the new one — preferring to lose stale history rather than block emit. If the queue stays full after the eviction (two concurrent emits both racing the same full queue) it drops the new entry. Subscribers see an unsignalled gap, never a stalled stream.

### Handler (`handler.py`)

```python
class RingBufferHandler(logging.Handler):
    """Append every record to LogBus after tag routing."""
```

Set to `logging.DEBUG` so every record reaches the bus; per-tag level filtering happens inside `emit()` so the operator can raise/lower a tag at runtime without changing handler state. Records carry their `level_no` through into the bus entry, which is the value the per-tag threshold compares against.

### Lifespan installation (`install.py`)

```python
def install_logbus(
    loop: asyncio.AbstractEventLoop,
    *,
    buffer_per_tag: int = 1000,
    default_level: int = logging.INFO,
) -> None: ...
```

Called as the first line of the FastAPI lifespan coroutine. Steps it performs, in order:

1. Hand the running event loop to the bus (so `emit()` knows where to schedule fan-out)
2. Resize all tag deques to `buffer_per_tag`
3. Set every tag's level to `default_level`
4. Silence each logger named in `THIRD_PARTY_LOGGERS_BY_TAG.values()` to WARNING (httpx / httpcore / uvicorn.access — they are extremely chatty at INFO)
5. If a `RingBufferHandler` is already attached to the root logger, return early (idempotent on test reruns within the same process)
6. Pin any pre-existing root `StreamHandler` whose level is `NOTSET` to `INFO`, **before** lowering the root logger to `DEBUG`. Without this step, lowering the root would let DEBUG records flood docker logs / stderr through the inherited stream handler
7. Attach a fresh `RingBufferHandler` and lower root to `DEBUG`

## Configuration

| Field | Default | Notes |
|---|---|---|
| `dashboard_log_buffer_per_tag` | `1000` | Records retained per tag in the ring buffer. Bounded `[100, 10000]`. Memory is roughly `7 × buffer_per_tag × ~500 B`, so the max sits around 35 MB |
| `dashboard_log_level` | `"INFO"` | One of `DEBUG / INFO / WARNING / ERROR`. Applied to all 7 tags at startup; the dashboard `PUT /level` endpoint can adjust each tag at runtime |

Runtime tag-level changes via `PUT /api/dashboard/logs/level` are **process-memory only** — they survive until the next restart, then the `dashboard_log_level` default reapplies. This is intentional: the bus is a debugging affordance, not a configuration surface.

## Upstream dependencies

- Python's `logging` module (root logger, handler attachment)
- The asyncio event loop owned by FastAPI's lifespan

That's it. The bus is deliberately self-contained.

## Downstream consumers

- `dashboard.logs_routes` — `GET /api/dashboard/logs/stream?tag=...` opens an SSE connection that calls `bus.subscribe(q, tag=tag)`. `GET /tags` reports `bus.tag_info()`. `PUT /level` calls `bus.set_tag_level(...)`
- Every other module logs through standard `logging.getLogger(__name__)` and is unaware that a bus exists; capture is via root-handler attachment

## Known constraints

- **Single-process only**: the bus is an in-memory ring per Python process. Running multiple uvicorn workers means the dashboard sees only the worker that fielded the SSE request. The MVP topology is single-process, so this is acceptable. A multi-worker deployment needs an external aggregator (Vector, Loki, etc.) — at which point the bus's role narrows to "live preview within one worker"
- **No persistence**: a process restart drops the ring buffer and all runtime tag-level overrides. Anyone investigating a yesterday-night incident reads the underlying stream handler's destination (`docker logs` / stderr / the file sink), not the bus
- **Drop-oldest under backpressure**: a subscriber that can't keep up with `emit()` loses history transparently. There is no "lagged" indication on the wire. The 2000-slot subscriber queue paired with a 1 s SSE poll interval gives several seconds of slack before an eviction happens; in practice we have not seen evictions outside synthetic stress tests
- **Pre-lifespan records are silently dropped**: anything logged before `install_logbus()` runs (e.g., during top-level imports) does not reach the bus because no event loop is registered yet. Module-level code should keep its log volume to a minimum — and operationally, those records still go to the existing stream handler / docker logs
- **Tag granularity is fixed at seven**: adding an eighth tag means changing `ALL_TAGS`, `TAG_PREFIX_MAP`, the dashboard `TagName` enum, and the dashboard's tab strip. The choice is deliberate — a small fixed set keeps the UI readable
