# api

> FastAPI REST layer. Owns the public HTTP surface — feed and intent CRUD, on-demand fire endpoints, prompt template inspection, the runtime settings editor, and the health probe. The dashboard's bundled JS is the primary consumer; the same routes are also documented contracts that an external integration can call.

Interactive API docs are auto-generated at **`/docs`** (Swagger UI) and **`/redoc`** as long as the API container is running.

## Responsibility

- Expose every read-and-write surface that a user or operator needs after the container is up
- Validate request bodies via Pydantic before any side effect runs
- Coordinate the multi-store writes that an intent or feed change requires (SQLite + Qdrant + APScheduler + the in-process event-cache) so the four stores stay consistent or roll back together
- Return precise HTTP status codes — `404` when a resource truly doesn't exist, `422` for schema/template validation, `409` for shape conflicts (e.g. firing an event-mode intent), `429` for the per-resource fire throttle, `503` for "embedder still loading", `500` only when an underlying store is broken
- Provide a CSRF-resistant control plane for editing the on-disk `.env` and triggering the matching container restart

## Not in scope

- Any HTML rendering — `dashboard` owns that
- Per-job business logic — the routers call into `collector` / `matcher` / `summarizer` / `notifier` and only orchestrate
- Background work — fire endpoints kick off `asyncio.create_task` and return a polling URL; `dashboard.routes` and `logs_routes` own anything that needs SSE
- Authentication beyond the simple `DASHBOARD_TOKEN` shared secret — multi-user auth is out of scope for the MVP

## Routers and prefixes

| Module | Prefix | Routes |
|---|---|---|
| `health.py` | (none) | `GET /health` |
| `feeds.py` | `/feeds` | `POST /`, `GET /`, `PATCH /{id}`, `PATCH /{id}/tags`, `DELETE /{id}` |
| `feeds_fire.py` | `/feeds` | `POST /{id}/fire?dry_run=`, `GET /{id}/fire/{task_id}` |
| `intents.py` | `/intents` | `POST /`, `GET /`, `GET /{id}`, `PUT /{id}`, `DELETE /{id}` |
| `fire.py` | (none) | `POST /intents/{id}/fire`, `GET /intents/{id}/fire/{task_id}` |
| `prompts.py` | `/api/prompts` | `GET /templates`, `GET /templates/{kind}/{name}` |
| `settings.py` | `/api/settings` | `GET /schema`, `GET /values`, `POST /save` |

`fire.py` and `feeds_fire.py` are deliberately separate from the CRUD modules because they own a different lifecycle — they create a `FireTask` in memory, dispatch a background coroutine, and expose a polling endpoint. Splitting them keeps the CRUD routers small and lets the fire-task lifecycle evolve without disturbing the create/update path.

## Request flow patterns

### Multi-store writes (intent CRUD)

Creating, updating, and deleting an intent touches four stores in a fixed order. The pattern below is what `intents.py` enforces; an integration cannot create an intent directly in any single store and expect the others to follow.

**`POST /intents`**

1. Validate prompts (system + instruction templates exist on disk)
2. Insert the SQLite row (autoincrement assigns the id)
3. Embed the intent text once
4. Upsert the Qdrant point under that id with the matcher payload (`text`, `threshold`, `enabled`, `tags`, `embedding_model_version`, timestamps)
5. If the intent's schedule is event-mode, add it to the in-process `event_intent_cache`
6. If the intent is enabled, register the matcher job (cron-mode only — event-mode dispatches via the cache)

If any step after the SQLite insert fails, the prior steps are rolled back in reverse order: cache → Qdrant → SQLite. The HTTP response is `500` and the failure is logged.

**`PUT /intents/{id}`** distinguishes between text changes and metadata-only changes — only a text change re-embeds and replaces the Qdrant vector. Metadata-only updates use `update_intent_payload` (Qdrant payload write without re-embedding). When the text changes, `match_seen` is also cleared so the re-embedded vector can re-match articles it would otherwise have seen.

**`DELETE /intents/{id}`** unregisters the matcher job first (so no new ticks fire during the delete), removes the cache entry, then deletes the Qdrant point, then the SQLite row. If the SQLite delete fails after Qdrant succeeded, the row remains visible via `GET` but the matcher won't consume it (its vector is gone) — the operator gets a structured error log so they can reconcile by retrying the delete.

### Multi-store writes (feed CRUD)

`feeds.py` follows the same pattern at smaller scale: SQLite first (with auto-rollback on scheduler failure), then `add_feed_job` to register the polling cron, with try/except deletion of the SQLite row if scheduler registration raises. Patch handlers compute the diff against the loaded current row and only touch the scheduler when `enabled` actually toggles or `poll_interval_minutes` changes.

`DELETE /feeds/{id}` cascades feed-id removal across every intent's `feed_filter.ids` array in the same SQLite transaction as the delete itself, then re-registers the affected intents' matcher jobs so the updated filter takes effect immediately. A re-registration failure is logged but does not fail the request — the next process restart picks up the correct state from disk.

### Fire endpoints

Both `/intents/{id}/fire` and `/feeds/{id}/fire` follow the same shape:

1. Validate the resource exists and is in a fireable state (cron-mode only for intents; any state for feeds)
2. Throttle check — 1 fire per resource per 60 seconds, in-memory state
3. Create an in-memory `FireTask` with a UUID
4. Dispatch the work via `asyncio.create_task`; the task is held in a module-level `set` to keep it alive against the GC, with `add_done_callback(set.discard)` removing it on completion
5. Return `202 Accepted` with `task_id` and `status_url`

The status endpoint reads the same in-memory task. Because the storage is per-process, a multi-worker uvicorn deployment would route the GET to a worker that may not own the task. The MVP topology is single-worker.

`POST /feeds/{id}/fire?dry_run=true` reuses the same host rate limiter that the scheduler uses (`get_host_limiter()` from `collector.scheduler`) so a dry-run cannot bypass the per-host concurrency ceiling.

### Settings editor

`/api/settings/*` is the only router that writes to the host filesystem and triggers container restarts. It enforces a stricter auth model and rejects values that violate the passthrough whitelist.

- **Header-only auth**: every endpoint depends on an `X-Dashboard-Token` HTTP header, never a cookie. The cookie path that the dashboard middleware accepts elsewhere would let any logged-in browser tab CSRF a settings save; the explicit header dependency closes that hole. Empty `dashboard_token` is treated as "no auth configured" so dev mode stays usable, but production deployments must set one.
- **Schema-driven UI**: `GET /schema` introspects `Settings.model_fields` to derive each field's type (`str` / `int` / `float` / `bool` / `secret` / `enum` / `path`), constraints (`ge`/`le`), and description. The frontend renders a form from this without hardcoding any field. Hidden fields (currently `EMBEDDER_BACKEND`) stay on disk untouched but are filtered out of the schema and values responses.
- **Mask sentinel**: secret-typed fields and passthrough variables whose name contains a sensitive substring (`TOKEN`, `COOKIE`, `SECRET`, `KEY`, `PASSWORD`, `SESSION`) are returned as `••••••`. When the client sends the same sentinel back via `POST /save`, the existing on-disk value is preserved untouched. This is what lets the operator save unrelated changes without ever exposing the secret in the HTTP body. Adding a brand-new key with the literal sentinel as its value is rejected with `422`.
- **Passthrough whitelist**: keys outside the sembr settings model must match the strict `^[A-Z][A-Z0-9_]*$` pattern AND begin with one of `TWITTER_`, `TELEGRAM_`, `GITHUB_`, `RSSHUB_`, `SOCIAL_`, `OPENAI_`. The whitelist exists so RSSHub can add new sources without a sembr code change but a malicious key can't slip into the file.
- **Restart orchestration**: a save that touches a sembr field triggers an api self-restart (delayed `SIGTERM` so the response can flush first, then `restart: unless-stopped` brings the container back); a save that touches a passthrough field triggers a force-recreate of the RSSHub service via `docker compose up -d --force-recreate --no-deps rsshub`. RSSHub failures are downgraded to a `200` response with `rsshub_restart_failed=true` so the api self-restart always still happens — disk and process state converge regardless of the RSSHub outcome.

The `.env` writer (`settings_envfile.py`) is hand-rolled rather than `python-dotenv` because the latter rewrites the whole file on every save and drops the section header comments operators rely on for navigation. The implementation preserves comments, blank lines, and group ordering verbatim, and is backed by a `.env.bak` copy taken before each write — direct in-place writes (rather than tmp+rename) avoid `EBUSY` from Docker Desktop's bind-mounted file system.

## Health

`GET /health` is the K8s/docker readiness probe.

- Returns `503 starting` when lifespan hasn't finished setting `app.state.qdrant` / `app.state.embedder` (start-up race protection)
- Otherwise returns `200 ok` iff Qdrant ping succeeds AND SQLite is reachable AND the embedder reports `"ok"`. `"loading"` and `"error"` both fail the probe
- Real-time — there is no caching layer, every probe re-pings each component

## Auth model

Two distinct mechanisms protect the api today, both rooted in the same `DASHBOARD_TOKEN` Settings field:

| Surface | Mechanism | CSRF-safe? |
|---|---|---|
| Dashboard pages, dashboard JSON endpoints, prompts, fire | `DashboardTokenMiddleware` — accepts `X-Dashboard-Token` header **or** `dashboard_token` cookie | No (cookies cross-origin) |
| `/api/settings/*` | `Depends(require_header_token)` — header **only**, constant-time compare via `secrets.compare_digest` | Yes |

When `DASHBOARD_TOKEN` is empty, both surfaces let every request through. This is intentional for the dev experience (`docker compose up` and start clicking) but means a public-internet deployment without a token is fully open. Operators are expected to set a token before exposing the api beyond localhost.

## Configuration

The api router itself reads no configuration directly — all settings come through `Settings` (`sembr.config`) and are accessed via `request.app.state.settings`. The fields that affect routing behavior:

| Field | Used by | Purpose |
|---|---|---|
| `dashboard_token` | settings router auth, dashboard middleware | Shared secret; empty disables auth |
| `prompts_dir` | `prompts.py`, `intents.py` | Where template files live; surfaced to error messages so the operator can find a broken template |

## Upstream dependencies

- `sembr.db.*` — SQLite CRUD helpers for feeds, intents, match_seen
- `sembr.vector_store.intents` — Qdrant point upsert / payload update / delete; `ALIAS_NAME` for collection routing
- `sembr.vector_store.qdrant.extract_point_vector` — used by PUT to re-cache a vector for a previously-disabled event-mode intent
- `sembr.matcher.jobs` — register / unregister / re-register intent jobs in APScheduler
- `sembr.matcher.event_cache` — `EventIntentEntry` for the in-process event-mode cache
- `sembr.matcher.scan` — scan-once execution path for fire endpoints
- `sembr.matcher.fire_tasks` / `sembr.collector.fire_tasks` — in-memory task registries with throttling
- `sembr.collector.scheduler` — `add_feed_job`, `remove_feed_job`, `get_host_limiter()`, `SOURCE_REGISTRY`
- `sembr.summarizer.templates` — `template_path`, `template_exists`, `list_templates`, `load_template`

## Downstream consumers

- `web/static/*` — the bundled dashboard JS calls every endpoint here
- External integrations — same routes, documented under `/docs`
- The summarizer's `on_summary` and `on_template_error` callbacks are wired in `main.py`'s lifespan, not in this module — but they are the things that ultimately deliver an intent's matched articles after the api creates it

## Known constraints

- **Single-process state**: fire-task storage, throttle counters, and the event-mode intent cache all live in module-level Python state. A multi-worker uvicorn deployment would route `GET /fire/{task_id}` to a worker that may not have created the task. The MVP topology is single-worker; a multi-worker deployment requires moving these to Redis or another shared store
- **PUT failure with text-change is destructive on Qdrant**: when the new vector has been written but a downstream step fails, the rollback path deletes the new Qdrant point — the original vector is already gone. The SQLite row is rolled back, but the operator must re-PUT to re-embed before the matcher can find the intent again. The `500` response and the `ERROR`-level log explain this; a fully-symmetric rollback would require capturing the prior vector from Qdrant before the upsert, which is structural work for a follow-up
- **No upper bound on body size for feeds**: the api does not cap `FeedCreate.config` size or `IntentCreate.text` length beyond Pydantic's per-field validators. Misuse could fill a SQLite row with a megabyte of payload. In practice the dashboard caps these client-side; a hostile direct call would be limited only by FastAPI's default body size limit
- **Settings save runs no Pydantic round-trip**: a value that fails `Settings(**values)` validation (e.g. `MATCHER_DEFAULT_THRESHOLD=not-a-number`) is written to disk anyway. The api self-restart then crashes during `Settings()` instantiation and `restart: unless-stopped` puts the container into a crash loop. The dashboard form mirrors the schema constraints, so this is reachable only by hand-crafting the POST body, but a future Pydantic dry-run would close the gap
- **Self-restart only works inside Docker**: the SIGTERM-then-let-restart-policy-bring-us-back path assumes a container runtime with a restart policy (`restart: unless-stopped`). A bare `uvicorn` invocation will exit and stay down. Documented in operator-facing docs
- **Throttle is in-process and short-lived**: 1-fire-per-60s per resource is an anti-foot-gun, not a security control. Any client can wait the throttle out
