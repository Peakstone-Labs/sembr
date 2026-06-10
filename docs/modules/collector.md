# collector

> Article ingestion pipeline. Polls each feed on its own APScheduler interval, normalises entries into `RawArticle`, deduplicates by `MD5(url+title)`, and writes to `pending_articles` for the embedder to pick up. Per-host concurrency is gated by a process-local semaphore.

## Responsibility

- Define the source ABC (`BaseSource`) so additional source types (HN, Reddit, custom HTTP) can be plugged in without touching the scheduler
- Ship a working RSS source (`RSSSource`) for the bundled feed seeds
- Run one APScheduler `IntervalTrigger` job per enabled feed, with a deterministic phase offset and per-fire jitter so polls do not synchronise across restarts
- Cap concurrent fetches against the same host (or the same first-path-segment behind a known proxy) via `HostLimiter`
- Write per-fetch observability rows (`feed_fetch_log`) — events that distinguish "fetch failed" from "fetch ok, no new items"
- Provide a manual feed-fire entry point with a 60 s real-fire rate limit and dry-run mode (`fire_tasks`)
- Seed an initial set of feeds on first startup if no feed has ever been seeded (`initial_feeds`)

## Not in scope

- Embedding (lives in `embedder`)
- Vector storage (lives in `vector_store`)
- Article body parsing past HTML-stripping + entity decode — full readability extraction is a future feature
- Distributed multi-worker coordination — the limiter and fire-task store are process-local

## Public interface

### Source ABC (`base.py`)

```python
@dataclass
class RawArticle:
    url: str
    title: str
    body: str
    content_quality: Literal["full", "summary", "stub", "title_only"]
    published_at: datetime | None
    feed_md5: str          # MD5(url + title), 32 lowercase hex chars

class BaseSource(ABC):
    async def fetch(self, since: datetime | None = None) -> list[RawArticle]
    async def health() -> bool
    @classmethod
    def config_schema() -> dict
```

`content_quality` lets downstream stages decide how to handle near-empty articles (e.g., a `title_only` entry is fine for matching but not for summary). The `feed_md5` field is the dedup fingerprint and the article id used downstream as the deterministic Qdrant point UUID.

### RSS source (`rss.py`)

```python
class RSSSource(BaseSource):
    def __init__(self, url: str, timeout: float = 30.0)

class FetchError(Exception): ...
```

Per-fetch behaviour:

- `httpx.AsyncClient(timeout=..., follow_redirects=True)` with `User-Agent: sembr/0.1 feedparser`
- Raises `FetchError` on HTTP failure or unparseable feed (no entries + bozo or unrecognised version) so the caller does not advance the cursor and re-tries the same `since` window next tick
- Falls back through `entry.content` → `entry.summary` → `entry.title`, classifying as `full` / `summary` / `stub` / `title_only`
- Strips HTML tags AND decodes entities (`&amp;` → `&`) so the embedder sees the text the article meant
- `since` filtering is "err on inclusion": entries without a usable `published_parsed` / `updated_parsed` are kept and rely on the downstream MD5 dedup

### Wisburg source (`wisburg.py`)

```python
class WisburgSource(BaseSource):
    def __init__(self, url: str, timeout: float = 30.0)

ENDPOINT_URLS: frozenset[str]        # the three whitelisted endpoint URLs
def normalize_wisburg_url(s) -> str  # shared with FeedCreate's url validator
```

`source_type="wisburg-report"` — Wisburg open-API research-note streams
(`/api/reports`, `/api/earningscalls`, `/api/am-reports`). The endpoint
identity is the `feed.url` itself (whitelist-validated on write), so the
feeds-table UNIQUE constraint blocks duplicate feeds per endpoint. Auth is a
single Bearer key (`WISBURG_API_KEY`); empty key → `FetchError` on fetch and
`health() == False`.

Per-fetch behaviour:

- **N+1 fetch**: the list endpoint returns only `{id,title,datetime}`; each
  item costs one extra `GET <endpoint>/<id>` for the markdown `summary`
  (stored as `body`, `content_quality="summary"`). Detail calls run
  sequentially — upstream allows 1000 req/h and daily volume is tens of items
- **Watermark**: `startTime = max(since − 1h, now − 7d)`; first pull uses
  `now − 1d`. The 1h overlap re-reads the trailing edge (MD5 dedup absorbs
  it); the 7d clamp keeps a stale cursor from turning into a backfill
- **All-or-nothing failure**: any transient failure (HTTP error, timeout,
  non-success response envelope) raises `FetchError` so the cursor doesn't
  advance past never-inserted items. Only terminal per-item misses are
  skipped with a warning: detail 404/410, missing title, empty summary
- Article `url` is the API detail URL
  (`https://api-omen.wisburg.com/api/<endpoint>/<id>`) — stable and unique,
  which keeps the `MD5(url+title)` fingerprint stable across ticks

### Deterministic phase + jitter (`phase.py`)

```python
def derive_phase_seconds(feed_id: int, period_seconds: int) -> int
def derive_jitter_seconds(period_seconds: int) -> int
```

Phase is `MD5("feed-{id}")` mod period — survives restarts, spreads first-run timing across feeds. Jitter is clamped to `[60, 600]` seconds and applied per-fire by the `IntervalTrigger`.

### Per-host concurrency (`host_limiter.py`)

```python
def derive_group_key(url: str, proxy_hosts: frozenset[str]) -> str
class HostLimiter:
    def __init__(self, proxy_hosts: frozenset[str], max_per_host: int = 2)
    def group_key_for(url) -> str
    @asynccontextmanager
    async def acquire(group_key: str)
```

Default group key is `host[:port]`; when the host appears in `proxy_hosts` (e.g. an RSSHub instance fronting many backends), the first path segment is appended so different upstream feeds do not collapse onto the same semaphore. The `asyncio.Lock` used for lazy-creating semaphores is itself lazily initialised — `HostLimiter` can be constructed before an event loop exists (static tests / module-level construction).

### Scheduler entry points (`scheduler.py`)

```python
SOURCE_REGISTRY: dict[str, type[BaseSource]] = {"rss": RSSSource}

def register_source(source_type: str, cls: type[BaseSource]) -> None
def make_scheduler() -> AsyncIOScheduler
def set_host_limiter(limiter: HostLimiter | None) -> None

async def collect_feed(
    feed_id: int, feed_name: str, feed_url: str, source_type: str, config: dict,
) -> tuple[int, int, list[dict]]                # (items_seen, items_new, articles)

async def add_feed_job(scheduler: AsyncIOScheduler, feed: Feed) -> None
def remove_feed_job(scheduler: AsyncIOScheduler, feed_id: int) -> None
```

`collect_feed` is the coroutine APScheduler invokes on every tick. One tick:

1. Look up the source class; unknown type returns `(0, 0, [])` and writes no event row (config error, not a fetch attempt)
2. Read `feeds.last_collected_at` to compute `since`; missing/unparseable falls back to `None`
3. Acquire the host-limiter slot for this feed's group key (or `nullcontext()` if no limiter is wired — production always wires one in `main.lifespan`, tests can call directly)
4. `source.fetch(since=since)` — `FetchError` and unexpected exceptions both write a failure event row and return without advancing the cursor
5. For each fetched article: `insert_article_pending` (returns True on insert, False on dedup); per-article failures are logged but do not abort the batch
6. `update_last_collected` advances the cursor only after a successful fetch (the `since` window will not re-process anything)
7. Emit the success event row with `(items_seen, items_new)`

The function records two timestamps internally: a queued-at (entered the limiter context) and a started-at (acquired the slot). The fetch event's `elapsed_ms` reflects actual fetch time, not queue-wait time, so the dashboard's per-feed throughput stays meaningful when many feeds queue against the same host.

`add_feed_job` registers the per-feed `IntervalTrigger` job with `coalesce=True`, `max_instances=1`, and `next_run_time = now + phase_s`. `remove_feed_job` ignores `JobLookupError` — useful when the service restarted between a feed's deletion and its job removal.

### Manual fire (`fire_tasks.py`)

```python
@dataclass
class FeedFireTask:
    task_id: str
    feed_id: int
    dry_run: bool
    status: str               # "running" | "done" | "error"
    started_at: datetime
    finished_at: datetime | None
    articles_fetched: int
    articles_new: int
    articles: list[dict]
    error: str | None

def create_task(feed_id: int, dry_run: bool) -> FeedFireTask
def get_task(task_id: str) -> FeedFireTask | None
def throttle_check(feed_id: int, rate_seconds: int = 60) -> bool
def sweep_expired(ttl_seconds: int = 3600) -> int
```

In-memory state, process-local. `throttle_check` consults `_last_fire_at[feed_id]`; `create_task` updates that map only when `dry_run=False`, so dry runs are unrate-limited. APScheduler runs `sweep_expired` every five minutes; the API layer also stamps it on every fire to keep the map bounded.

### Initial seeds (`initial_feeds.py`)

```python
INITIAL_FEEDS: list[dict]    # name, url, poll_interval_minutes
```

Loaded by `db.feeds.seed_initial_feeds` on first startup. Already-seeded URLs are recorded in `seeded_feeds` and never re-seeded, so users can delete defaults without them reappearing.

## Configuration

| Field | Default | Notes |
|---|---|---|
| `proxy_hosts_set` | `frozenset()` | hosts whose first path segment becomes part of the group key (RSSHub-style) |
| host-limiter `max_per_host` | `2` | hard-coded in `main.lifespan`; promote to a setting if you need per-deployment tuning |
| feed-fire rate limit | `60 s` | hard-coded in `fire_tasks._FIRE_RATE_LIMIT_SECONDS` |
| feed-fire task TTL | `3600 s` | hard-coded in `fire_tasks._TASK_TTL_SECONDS` |
| `wisburg_api_key` | `""` | Bearer key for `source_type="wisburg-report"`; empty disables wisburg feeds |
| wisburg window constants | `1h` overlap / `7d` clamp / `1d` first pull / 5 pages | hard-coded in `wisburg.py` module constants (they encode probed upstream behaviour, not user preference) |

Per-feed `timeout` lives in the feed's `config` JSON column; defaults to `30.0` if absent.

## Upstream dependencies

- `db.feeds` — `fingerprint_exists`, `insert_fingerprint`, `update_last_collected`, `seed_initial_feeds`
- `db.articles` — `insert_article_pending` (handles dedup + body cap)
- `db.sqlite.get_conn` — shared aiosqlite connection
- `dashboard.events.log_fetch_event` — best-effort observability

## Downstream consumers

- `embedder.scheduler.embedder_worker` — pulls from `pending_articles` written by this module
- `api.feeds` — registers / removes per-feed jobs through `add_feed_job` / `remove_feed_job`
- `api.feeds_fire` — reuses `SOURCE_REGISTRY`, `_LIMITER_REF`, and `collect_feed` for manual fires; reuses `fire_tasks` for state
- `dashboard.read_model` — reads `feed_fetch_log` rows produced here

## Known constraints

- **Single-process state**: `_LIMITER_REF`, `SOURCE_REGISTRY`, and `_feed_fire_tasks` are module-level globals. The deployment model is single-process Docker; multi-worker setups would need a distributed semaphore and an external task store.
- **Dedup is post-fetch**: `RSSSource.fetch` returns every entry that survives the `since` filter; deduplication happens in `db.articles.insert_article_pending` via the `feed_items` MD5 unique key. A feed that re-emits the same article with a slightly different URL or title will be ingested again.
- **Cursor advances only on fetch success**: a `FetchError` keeps `last_collected_at` unchanged so the next tick re-tries the same window — articles published during a brief outage are not lost. Persistent fetch failure means the cursor stays put and the same `since` is re-sent until success.
- **No content extraction**: the body is `feedparser`'s `content` / `summary` / `title` field with HTML stripped and entities decoded. There is no readability-style article-page fetch; for paywalled or summary-only feeds, the embedder works on whatever the RSS itself ships.
- **Limiter-acquire latency is not bounded**: `HostLimiter.acquire` waits indefinitely for a free slot. Combined with `max_instances=1` per feed, a stuck slot can stall a feed; the per-feed timeout protects only the HTTP call, not the queue wait.
- **`fire_tasks.throttle_check` is racy**: two coroutines that both pass the check before either calls `create_task` will both create real fires. The 60 s rate limit is a soft guard against the dashboard's own duplicate clicks, not a hard interlock.
- **Initial seeds are baked in**: `INITIAL_FEEDS` is a Python list in source. Operators who want a different default set must edit the file (or delete defaults after first boot — they will not re-seed). Promoting this to a YAML / settings list is on the list for a future round.
