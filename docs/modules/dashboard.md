# dashboard

> Read-model aggregation, observability tables, and the JSON endpoints behind the bundled monitoring UI. Owns `/api/dashboard/*` (snapshot, drill-downs, articles, sources, logs SSE) and the auth middleware that gates every other authenticated route. Pure read-side — no business writes happen here.

The static HTML + Alpine.js + Chart.js frontend lives in `web/static/` and is served by `main.py` via `StaticFiles`; it is not part of this module.

## Responsibility

- Build the dashboard's polling snapshot — feeds + last-24h fetch stats per feed + embedder call stats + Qdrant point count + component health + per-container CPU/Mem/uptime samples, in a single response
- Serve drill-down endpoints for the per-feed event log, embedder event log, articles in each pipeline bucket (pending / dead / qdrant — qdrant accepts `ingested_from` / `ingested_to` / `feed_id` / `title_q` filter), and the source-type → JSON-Schema map used by the create-feed form
- Define the two append-only event tables (`feed_fetch_log`, `embed_call_log`) and expose `log_*_event` helpers that the collector and embedder call after every batch
- Run the hourly retention prune that keeps the event tables bounded by both age and per-feed cap
- Run the lifespan-managed system-metrics sampler that polls `docker stats` for the compose stack at the snapshot poll cadence and feeds a 60-point in-memory rolling buffer
- Gate every authenticated route via `DashboardTokenMiddleware` — checked once per request against `Settings.dashboard_token`
- Stream live log records over SSE via `logs_routes.py` (the bus itself lives in [logbus](logbus.md))

## Not in scope

- Writing observability records — collector and embedder call `log_fetch_event` / `log_embed_event` themselves; this module only defines the helpers and reads them back
- Owning the LogBus — `dashboard.logs_routes` is a thin SSE adapter over the bus the [logbus](logbus.md) module owns
- Authoring the frontend — `web/static/*` is not part of this module's review surface
- Multi-user auth — the single shared `DASHBOARD_TOKEN` is the entire authentication model

## Observability tables

Both tables are append-only; no `UPDATE` / `DELETE` happens on the write path. Retention prunes by age and (for `feed_fetch_log`) per-feed FIFO cap.

```sql
feed_fetch_log
  id              INTEGER PK AUTOINCREMENT
  feed_id         INTEGER NOT NULL FK feeds(id) ON DELETE CASCADE
  started_at      TEXT    NOT NULL  -- ISO8601 +00:00 (NEVER mix Z with +00:00)
  elapsed_ms      INTEGER NOT NULL
  ok              INTEGER NOT NULL  -- 0/1
  items_seen      INTEGER NOT NULL
  items_new       INTEGER NOT NULL
  error_class     TEXT
  error_message   TEXT              -- truncated to 500 chars
  INDEX (feed_id, started_at DESC)
  INDEX (started_at)

embed_call_log
  id              INTEGER PK AUTOINCREMENT
  started_at      TEXT    NOT NULL
  elapsed_ms      INTEGER NOT NULL
  ok              INTEGER NOT NULL
  batch_size      INTEGER NOT NULL
  total_chars     INTEGER NOT NULL
  timeout_seconds REAL    NOT NULL
  error_class     TEXT
  error_message   TEXT              -- truncated to 500 chars
  INDEX (started_at)
```

`started_at` strings are written exclusively as `datetime.isoformat()` on a tz-aware UTC datetime, producing `"YYYY-MM-DDTHH:MM:SS+00:00"`. The retention cutoff and the sparkline window comparisons are lexicographic — mixing in a `Z`-suffixed value would silently break range queries because `+00:00` and `Z` lex-sort differently. The snapshot response field `generated_at` is the only place the `Z` shorthand appears, and only because it's response-only and never a query input.

## Public interface

### Routes (`routes.py`)

All under `/api/dashboard`. Every route takes its dependencies from `request.app.state` rather than importing them — keeps tests trivial.

```
GET  /config                                       poll_interval, auth_required, display_timezone (auth-free)
GET  /snapshot                                     top-level dashboard read; one round-trip; includes system_metrics
GET  /feeds                                        paginated feeds list with tags/group/next-run
GET  /feeds/{feed_id}/events?limit=&offset=        per-feed fetch event drill-down
GET  /feeds/{feed_id}/articles?limit=&offset=      articles ingested under one feed (Qdrant scroll)
GET  /sources/schemas                              source_type → JSON-Schema map (form metadata)
GET  /embedder/events?limit=                       embedder call drill-down
GET  /articles?bucket=&limit=&offset=&...          pending / dead / qdrant article list
                                                   bucket=qdrant additionally accepts
                                                     ingested_from=YYYY-MM-DD (UTC, inclusive)
                                                     ingested_to=YYYY-MM-DD   (UTC, exclusive +1d)
                                                     feed_id=<int>
                                                     title_q=<MatchText>      (uses the title text-index)
GET  /articles/{md5}?bucket=                       single article detail
POST /restart                                      api + rsshub double-restart trigger (header-token)
```

`/api/dashboard/config` is the only auth-free endpoint — the login page calls it before bootstrap so it knows whether a token is required. Everything else is gated by `DashboardTokenMiddleware`.

### SSE (`logs_routes.py`)

```
GET /api/dashboard/logs/tags         {"tags": [{name, level}], "available_levels": [...]}
PUT /api/dashboard/logs/level        {tag, level} → 204
GET /api/dashboard/logs/stream?tag=  text/event-stream — history snapshot then live entries
```

Tag-level changes are process-memory only and do not persist across restart. The `http` tag also resyncs the underlying stdlib loggers (`httpx`, `httpcore`, `uvicorn.access`) via `THIRD_PARTY_LOGGERS_BY_TAG`, the same map [logbus](logbus.md) uses on startup. The SSE generator subscribes with a tag filter so the bus never fans out unwanted entries to a per-tag subscriber.

### Read-model helpers (`read_model.py`)

```python
build_snapshot(conn, qdrant_handle, embedder, metrics_collector=None) -> SnapshotResponse
list_feeds_with_meta(conn, *, limit, offset, tag, q, proxy_hosts, scheduler, now=None) -> FeedListResponse
list_feed_events(conn, feed_id, limit, offset=0) -> list[FeedFetchEvent]
list_embed_events(conn, limit) -> list[EmbedCallEvent]
list_articles_pending(conn, limit, offset) -> list[ArticleListItem]
list_articles_dead(conn, limit, offset) -> list[ArticleListItem]
list_articles_qdrant(qdrant_client, limit, offset, *, ingested_from=None,
                     ingested_to=None, feed_id=None, title_q=None) -> list[ArticleListItem]
list_feed_articles_qdrant(qdrant_client, feed_id, *, limit, offset) -> list[ArticleListItem]
get_article_detail(conn, qdrant_client, md5, bucket) -> ArticleDetail | None
```

`build_snapshot` is the hot path — the dashboard polls it every `dashboard_poll_interval_seconds`. It dispatches the two Qdrant network calls (`ping` inside `_component_status`, and `count`) as `asyncio.create_task`s up front so they overlap with the SQLite work. The two SQLite scans that aggregate `feed_fetch_log` use a window function (`ROW_NUMBER() OVER (PARTITION BY feed_id ORDER BY id DESC)`) to read the last 50 rows per feed in a single index scan rather than a per-feed correlated subquery.

`metrics_collector` is the lifespan-owned `SystemMetricsCollector` (see [System metrics sampler](#system-metrics-sampler-system_metricspy)). It is injected as a function argument rather than read from `app.state` so this module never imports FastAPI types — the route handler in `routes.py` is the single boundary that touches `request.app.state.metrics_collector`. When the collector is `None` (no docker socket / dev box) or has flipped to `available=False` (docker daemon unreachable), `system_metrics` is `null` in the response and the dashboard renders a "—" placeholder.

The Qdrant article-list helpers (`list_articles_qdrant`, `list_feed_articles_qdrant`) share a `_scroll_articles_qdrant` helper because Qdrant's scroll API takes an opaque `next_page_offset` cursor rather than a numeric offset — the helper walks forward `limit + offset` items and drops the first `offset`. That makes deep pagination linear in `offset`; the UI default of 50/page keeps it cheap.

`list_articles_qdrant` accepts four keyword-only filter args. The route layer parses `ingested_from` / `ingested_to` from `YYYY-MM-DD` UTC dates and shifts the upper bound by +1 day so the underlying ts comparison is half-open `[from, to)` while the UI presents it as inclusive. All three filterable fields (`ingested_at_ts`, `feed_id`, `title`) have payload indexes — `MatchText` against `title` requires the text-index added in `vector_store.news.ensure_news_collection` (lifespan-startup idempotent), without which Qdrant silently degrades to a full collection scan.

### System metrics sampler (`system_metrics.py`)

```python
class SystemMetricsCollector:
    """Module-level rolling buffer (deque maxlen=60) of docker stats samples."""
    def append(self, sample) -> None
    def read(self) -> SystemMetricsBlock | None  # None when unavailable / empty
    def mark_unavailable(self) -> None
    def mark_available(self) -> None

add_system_metrics_job(scheduler, collector, interval_seconds) -> None
```

An APScheduler `IntervalTrigger` job ticks every `dashboard_poll_interval_seconds`, calls `_take_docker_sample` in a thread (5 s `asyncio.wait_for` cap), and appends the result to the collector. Containers are auto-discovered by docker compose's `com.docker.compose.project=<project>` label so adding/renaming a service in `docker-compose.yml` shows up automatically.

The standard docker-stats CPU% formula is used: `(cpu_delta / sys_delta) * online_cpus * 100`. The very first sample after lifespan startup has no `precpu_stats` baseline, so CPU% returns `None` for that sample only — the dashboard renders "—" while uptime and memory are already valid. Subsequent samples are real percentages.

The `add_job` call passes `coalesce=True, replace_existing=True` and **never** passes `next_run_time=None` — the latter is APScheduler's pause sentinel and would silently leave the sampler dormant.

### Event log helpers (`events.py`)

```python
log_fetch_event(*, feed_id, started_at, elapsed_ms, ok, items_seen, items_new,
                error_class, error_message) -> None
log_embed_event(*, started_at, elapsed_ms, ok, batch_size, total_chars,
                timeout_seconds, error_class, error_message) -> None
```

Each call opens its own `transaction()` so it never shares a `BEGIN` with the business path that triggered it. Callers must wrap the call in `try / except` and only `logger.warning` on failure — observability faults must not poison the collect/embed loop. `error_message` is truncated to 500 characters.

### Retention (`retention.py`)

```python
add_log_retention_job(scheduler, settings) -> None
```

Registers an APScheduler job (id `dashboard_log_retention`, hourly, `coalesce=True`, `replace_existing=True`) that runs three deletes in one transaction:

1. `DELETE FROM feed_fetch_log WHERE started_at < cutoff`
2. `DELETE FROM embed_call_log  WHERE started_at < cutoff`
3. `DELETE FROM feed_fetch_log WHERE id IN (...)` — keep newest `dashboard_log_max_per_feed` rows per feed via window function

Failures are logged at WARNING and swallowed; the next hourly tick retries.

### Auth (`auth.py`)

```python
class DashboardTokenMiddleware(BaseHTTPMiddleware):
    """Per-request gate on /dashboard/*, /api/dashboard/*, /api/prompts/*,
    /api/settings/*, /intents*, /feeds*. No-op when DASHBOARD_TOKEN is empty."""
```

- Lookup order for the supplied token: `X-Dashboard-Token` header, then `sembr_dashboard_token` cookie. Constant-time compare via `secrets.compare_digest`
- The login page (`/dashboard/login.html`) and vendor JS (`/dashboard/vendor/...`) are unconditionally exempt so the user can bootstrap a cookie
- Failed auth on `/api/*` returns `401 {"error": "unauthorized"}`; on the page surface it returns a 302 redirect to the login page
- `/api/settings/*` and `/api/prompts/*` are listed here as a defense-in-depth gate, but `/api/settings/*` ALSO requires an `X-Dashboard-Token` header via its own `Depends(require_header_token)` — the middleware's cookie path is CSRF-able from a logged-in browser, so the settings router insists on the header

The middleware's protected list is by inclusion (allowlist of namespaces, not denylist), so `/health` and other unspecified routes pass through untouched.

## Configuration

| Field | Default | Notes |
|---|---|---|
| `dashboard_token` | `""` (SecretStr) | Empty disables auth. Set in production. Constant-time compared with whatever the client supplies |
| `dashboard_poll_interval_seconds` | `10` | Bounded `[2, 120]`. Returned via `/config` and used by the bundled JS |
| `dashboard_log_retention_days` | `7` | Age cutoff for the hourly retention prune |
| `dashboard_log_max_per_feed` | `1000` | Per-feed FIFO cap on `feed_fetch_log` rows |
| `display_timezone` | `Asia/Shanghai` | Returned via `/config` so the dashboard can render timestamps in the operator's preferred zone. Email rendering uses the per-intent timezone instead — see [notifier](notifier.md) |

## Upstream dependencies

- `sembr.config` — `Settings.dashboard_*` fields
- `sembr.db.sqlite` — `get_conn`, `transaction`, `sqlite_ok`
- `sembr.db.feeds`, `sembr.db.feed_tags` — feed list + tag map for the snapshot
- `sembr.collector.scheduler.SOURCE_REGISTRY` — read once for `/sources/schemas`; lets a plugin source registered via `entry_points` appear in the create-feed form without a code change
- `sembr.collector.host_limiter.derive_group_key` — group key for the feeds-list response so the UI can show per-host concurrency grouping
- `sembr.vector_store.news.ALIAS_NAME` — every Qdrant call routes through this alias (`news_current`); a model-version swap re-points the alias and the dashboard sees the new collection without code change
- `sembr.logbus` — the bus and tag map; SSE adapter lives in `logs_routes.py`
- `apscheduler.AsyncIOScheduler` — used by `retention.add_log_retention_job` and (via main.py lifespan) for the dashboard log-retention cron

## Downstream consumers

- `web/static/*` — the bundled monitoring frontend. No external integration calls these endpoints today, but they are documented under `/docs`
- `main.py` — wires `DashboardTokenMiddleware` into the FastAPI app, includes `routes.router` and `logs_router`, and calls `add_log_retention_job` during lifespan startup

## Templates tab

The dashboard's Templates tab (between Intents and Logs) is the runtime editor for prompt templates that the summarizer feeds the LLM on every digest tick. State / network logic lives in `web/static/templates.js` (`templatesTab()` Alpine component), markup + modals in `web/static/index.html`, hash routing in `web/static/app.js`. The component consumes `GET/POST/PUT/DELETE/POST-rename /api/prompts/templates/*` (see `docs/modules/api.md`) and writes are auth-gated by the same `DashboardTokenMiddleware` as every other dashboard surface.

UI behaviour:

- Two columns (system / instruction) list every on-disk template. Each row shows `is_builtin`, `ref_count`, the list of referencing intents, file size, and mtime so the operator can see at a glance which templates are in use and which can be deleted safely.
- Per-row actions: **Edit / View** (read-only for builtins), **Duplicate** (open the create modal pre-seeded with this row as `source`), **Rename**, **Delete** — the latter two are disabled on builtin rows; the server still enforces 403/422 if a client bypasses the UI.
- The editor is a `<textarea>` (mtime / size badges in the header). Save sends `PUT { content }`; the server runs the strict-placeholder dry-render and rejects unknown `{...}` keys with HTTP 422. The error message names the exact placeholder that failed.
- Delete is gated by `ref_count == 0` from the cached list. A race (an intent grabbing the template after the cache and before the request) returns HTTP 409 with `ref_intents` in the body — the UI pops a toast and refetches the list so the operator can see the new dependency.
- Rename is the only action that crosses into SQLite. The frontend treats it as one request: file move + `UPDATE intents SET {kind}_template = ?` happens server-side, with reverse-rename rollback if the SQLite step fails. After success, the UI refetches the full list because every referencing intent's row is also affected.

If the rename rollback itself fails (filesystem error during reverse `os.rename`), the server returns HTTP 500 with a manual-recovery message. The operator's recovery path is: read the LogBus entry (logs at ERROR with both old and new paths plus the SQLite error), `docker compose exec api ls /app/prompts/{kind}/`, and either move the file back manually or run a UPDATE statement to align the intents column with the actual on-disk filename.

## Known constraints

- **Single-process state**: the SSE subscriber registry, the LogBus ring buffer, and the in-process tag-level overrides all live in module-level Python state. A multi-worker uvicorn deployment shows each worker its own slice of logs — a tab open against worker 1 sees nothing emitted from worker 2. The 1.0 topology is single-worker; multi-worker deployments need an external aggregator (Loki, Vector, etc.)
- **Polling, not push**: the snapshot endpoint is designed for short-interval polling (default 10 s) rather than a server-push channel. Live log streaming is the only push surface and it deliberately covers logs only — feed/embedder stats refresh at the polling cadence
- **Qdrant `qdrant_count` returns -1 on error**: a Qdrant outage during a snapshot poll is reported as a sentinel `-1` count in the response body so the UI can render a "—" rather than crash. The component health block in the same response signals the underlying state. Future work should split this into `qdrant_count` plus an explicit `qdrant_count_error` field
- **Article-list deep pagination is linear**: Qdrant's scroll cursor is opaque, so `offset=N` walks `limit + N` points then drops the first `N`. The UI defaults to 50/page and rarely paginates beyond a few pages, so this is acceptable today
- **Article detail by `md5` accepts any string**: `get_article_detail` calls `uuid.UUID(hex=md5)` for the Qdrant bucket; an invalid hex string raises and the route returns 404 (via the surrounding try/except). A more strictly-validated route would return 422, but the practical UX is identical
- **Retention failures are silent in the UI**: the prune job logs at WARNING but the dashboard surface has no widget for "retention last ran at ...". Operators rely on the LogBus / docker logs to spot a sustained failure
