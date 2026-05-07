# matcher

> Per-intent ANN search and event-driven scoring. Cron-mode intents run an APScheduler tick that queries Qdrant for new articles in a recency window; event-mode intents are scored in-process against every embedding batch as it lands. Both paths emit a `Match` list to `app.state.on_match`, which the notifier replaces at startup.

## Responsibility

- Run one APScheduler job per enabled cron-mode intent on its configured trigger (hourly / daily / weekly), each tick filtering Qdrant `news_current` by `ingested_at_ts` recency window and the intent's `feed_filter`
- Maintain an in-process cache of event-mode intent vectors and score every freshly embedded article batch against them in pure Python (cosine via dot product)
- Buffer event-mode hits per intent in `event_pending`, merging similar titles into groups, and flush either on `trigger_count` reached or on `max_wait_seconds` elapsed (sweeper job)
- Track which `(intent_id, article_id)` pairs have already fired through `match_seen` so cron-mode notifications don't repeat across ticks (event-mode does not write `match_seen`)
- Provide a manual fire path that reuses the cron scan logic but bypasses `match_seen` so an operator can see what an intent would match right now
- Surface a stable `on_match` injection seam so notifier wiring happens at lifespan startup, not at module import time

## Not in scope

- Article ingestion (lives in `collector`)
- Embedding (lives in `embedder`)
- Collection bootstrap and alias management (lives in `vector_store`)
- Summary text generation and channel delivery (lives in `summarizer` and `notifier`) — matcher only produces a list of `Match` objects
- Cross-process coordination — the event cache, sweeper, and fire-task store are single-process

## Public interface

### Match callback (`callback.py`)

```python
@dataclass
class Match:
    intent_id: int
    article_id: str       # UUID string matching Qdrant point id
    score: float
    payload: dict         # title / url / body / feed_id / published_at

OnMatchCallback = Callable[[list[Match]], Awaitable[None]]

async def log_matches(matches: list[Match]) -> None
```

`Match.payload` carries the article payload as Qdrant returned it; downstream consumers should not assume more fields than `title`, `url`, `body`, `feed_id`, `published_at`. `log_matches` is the placeholder default — a real notifier replaces `app.state.on_match` during lifespan startup.

### Cron scan (`scan.py`)

```python
@dataclass
class ScanOptions:
    lookback_seconds: int
    threshold: float
    skip_seen: bool        # filter already-seen ids before returning
    feed_ids: list[int] | None  # None = all feeds, [] = nothing
    write_match_seen: bool  # cron path = True, fire path = False

async def scan_once(intent, options, conn, qdrant_client) -> list[Match]
async def run_intent_scan(intent_id: int, app: FastAPI) -> None
```

`scan_once` is the shared core for scheduled ticks and manual fires. `run_intent_scan` is what APScheduler invokes — it loads the Intent from SQLite, short-circuits if the intent has been disabled or deleted between job registration and tick, runs `scan_once` with `write_match_seen=True`, and forwards the result to `app.state.on_match`.

A failed `on_match` is logged but never re-raised: `match_seen` has already been written, so the tick is "done" from the matcher's perspective even if the downstream pipeline blew up.

### Job lifecycle (`jobs.py`)

```python
def register_intent_job(scheduler, intent, app, *, fire_immediately=False) -> None
def unregister_intent_job(scheduler, intent_id) -> None
def reregister_intent_job(scheduler, intent, app) -> None    # alias for register
async def register_all_enabled(scheduler, intents, app, qdrant_client) -> None
```

Job id is `matcher-intent-{intent_id}`; `replace_existing=True` makes re-registration idempotent. Triggers are built with `coalesce=True`, `max_instances=1`, and `misfire_grace_time=None` so a delayed wakeup never silently skips a fire. The trigger's tzinfo is passed as a string (not a `ZoneInfo` object) — APScheduler 3.11 sorts due jobs by datetime and a tz-mismatch silently prevents firing.

`register_all_enabled` is the startup entry point: for every enabled cron-mode intent it confirms a vector is present in Qdrant before registering. An intent whose Qdrant point was lost (partial-delete failure, manual cleanup) is skipped with an error so it does not produce an unbounded stream of "no vector" warnings every tick.

Event-mode intents do NOT register an APScheduler job — they live entirely in the event-driven path below.

### Event-driven path (`event_match.py`, `event_cache.py`, `event_buffer.py`)

```python
class EventIntentCache:
    def add(intent_id, entry) -> None
    def remove(intent_id) -> None
    def get(intent_id) -> EventIntentEntry | None

@dataclass
class EventIntentEntry:
    vector: list[float]
    threshold: float
    feed_filter_ids: list[int] | None
    schedule: EventSchedule

async def load_event_cache(cache, qdrant_handle, conn) -> None
async def event_match_batch(app, points, conn) -> None
async def absorb(conn, intent_id, batch_matches, schedule) -> bool
async def flush(conn, app, intent_id) -> None
async def sweep_timed_out(conn, app, event_intent_cache) -> None
```

`event_match_batch` is called by the embedder worker after each Qdrant upsert. It iterates the cache, scores every (intent, article) pair via dot product (valid as cosine because the embedder is contracted to be unit-normalized — see `BaseEmbedder.is_unit_normalized`), and feeds hits to `absorb`. The whole call is wrapped in a try/except that logs and swallows: an event-path bug must not abort embedder ingestion.

`absorb` opens an explicit `BEGIN IMMEDIATE` transaction, merges the new `batch_matches` into `event_pending` by title similarity (≥0.85 SequenceMatcher ratio), and returns True when the per-intent buffered group count meets `schedule.trigger_count`. Article-level dedup inside each group prevents double-counting when the embedder retries a batch.

`flush` performs an atomic `DELETE … RETURNING members_json` on `event_pending` for the intent, commits, and only then awaits `on_match`. A failed `on_match` is logged but never re-raised — same contract as the cron path: the buffer is already cleared.

`sweep_timed_out` runs every 30 s as a separate APScheduler job (`event_y_sweeper` in `main.lifespan`). It picks the oldest buffered group per intent and flushes any whose age exceeds `schedule.max_wait_seconds`. Each intent's flush is isolated so one bad pipeline call does not stop the others.

### Manual fire (`fire_tasks.py`)

```python
@dataclass
class FireTask:
    task_id: str
    intent_id: int
    status: str               # "running" | "done" | "error"
    started_at: datetime
    finished_at: datetime | None
    match_count: int
    matches: list[dict]
    pushed: bool
    push_error: str | None

def create_task(intent_id) -> FireTask
def get_task(task_id) -> FireTask | None
def throttle_check(intent_id, rate_seconds=60) -> bool
def sweep_expired(ttl_seconds=3600) -> int
```

In-memory state, single-process. `throttle_check` consults `_last_fire_at[intent_id]`; `create_task` updates that map at task creation, so the rate-limit window starts at the accepted request, not at each rejection. APScheduler runs `sweep_expired` every five minutes to keep the task map bounded to ~1 h of recent fires.

The fire path runs `scan_once` with `write_match_seen=False`, so manually firing an intent never updates `match_seen` — it shows the operator what would match right now without affecting the next scheduled tick.

## Configuration

| Field | Default | Notes |
|---|---|---|
| `Intent.threshold` | model-dependent (typically 0.5–0.7 for BGE-M3) | per-intent; passed to Qdrant `query_points(score_threshold=...)` |
| `CronSchedule.lookback_seconds` | configured per intent | window of `ingested_at_ts` the cron scan considers |
| `CronSchedule.skip_seen` | `True` | when False, the scan re-emits matches every tick (notify-every-time mode) |
| `EventSchedule.trigger_count` | configured per intent | flush when this many groups accumulate |
| `EventSchedule.max_wait_seconds` | configured per intent | flush when the oldest group reaches this age |
| `_SEARCH_LIMIT` | `100` | upper bound on Qdrant results per cron tick |
| `_FIRE_RATE_LIMIT_SECONDS` | `60` | per-intent manual fire rate limit |
| `_TASK_TTL_SECONDS` | `3600` | manual fire task retention |
| `SEMBR_DEBUG_MATCHER` | unset | env var; when set, an empty cron tick runs two extra Qdrant queries to log whether the time filter or the threshold was the limiting factor — off by default because empty results are common and the extra queries multiply Qdrant load |

## Upstream dependencies

- `db.intents` — `get_intent`, `list_intents`
- `db.match_seen` — `insert_unseen_returning_new`
- `db.sqlite.get_conn` — shared aiosqlite connection
- `vector_store.intents.ALIAS_NAME` and `vector_store.news.ALIAS_NAME` — alias names for retrieve/query
- `vector_store.qdrant.extract_point_vector` — named-vector-safe vector extraction
- `summarizer.grouping.GroupingStep` and `normalize` — title-similarity grouping for event buffer
- `embedder.base.BaseEmbedder.is_unit_normalized` — contract that lets `_dot` stand in for cosine

## Downstream consumers

- `app.state.on_match` — set by the notifier (or `log_matches` as the no-op default); both cron and event paths call this with a `list[Match]`
- `api.fire` — uses `fire_tasks.create_task` / `get_task` for the manual-fire endpoint, and `scan_once` (with `write_match_seen=False`) for the actual run
- `embedder.scheduler.embedder_worker` — calls `event_match_batch` after each Qdrant upsert
- `main.lifespan` — registers `register_all_enabled`, `load_event_cache`, the 30 s `sweep_timed_out` job, and the 5 min `sweep_expired` job

## Known constraints

- **`on_match` is single-handler**: `app.state.on_match` is a single async callable. The notifier owns it; if a future feature wants two consumers (e.g., notification + analytics sink) it must fan out inside its own handler. Both cron and event paths call `on_match` once per drain.
- **Event scoring is pure Python**: `_dot` walks 1024 floats per (intent, article) pair without numpy. At 1.0 scale (≤50 event-mode intents, batches up to ~32 articles) the cost is negligible, but a backend swap that increases dim, or scaling event-mode intents into the hundreds, will want a vectorized implementation.
- **Unit-normalization is a contract, not a check**: `_dot` equals cosine only when the embedder returns L2-normalized vectors. The matcher refuses to score when `embedder.is_unit_normalized` is False, but a backend that returns False from that property and silently emits unnormalized vectors will produce wrong scores. A new embedder must implement this property truthfully.
- **`match_seen` is best-effort dedup**: cron-mode writes `match_seen` before calling `on_match`. If `on_match` fails, the tick is silently lost — the row is already marked seen and won't fire again. This is the deliberate trade-off (Risk E1 in the design): better to drop one tick than to flood the user when `on_match` recovers from a transient error.
- **Group merge is first-match-wins**: in `absorb`, a batch group whose normalized title is ≥0.85 similar to two existing groups merges into the first one in `group_id` order. There is no transitive cross-group merging at absorb time — that happens implicitly when the buffer is flushed and the summarizer regroups everything.
- **Event cache and `match_seen` are independent**: deleting an intent removes it from the event cache and unregisters the cron job, but does not delete `match_seen` rows or `event_pending` rows. The intent FK does the cleanup at the SQLite level, so a re-created intent (same id) will not see stale dedup state.
- **APScheduler job lookup is by id only**: `matcher-intent-{intent_id}` reuses the SQLite intent id. Restarting the service while an intent's id is reused (e.g., test fixtures) will produce silent overwrites — `replace_existing=True` is the intended behavior, but it does mean an older job's args are silently replaced.
- **No diagnostic by default**: an empty cron tick used to run two extra Qdrant queries to log whether the time filter or the threshold was the limiting factor. That probe now runs only when `SEMBR_DEBUG_MATCHER` is set in the environment, because empty results are the normal case under cron mode and the extra queries multiplied Qdrant load by ~3× when many intents had nothing new this window.
