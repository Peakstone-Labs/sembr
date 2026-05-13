# SPDX-License-Identifier: Apache-2.0
"""FastAPI entrypoint.

设计决策 #16 / #17: lifespan startup = SQLite init → Qdrant client init;
shutdown reverses order. Qdrant client construction never blocks on server readiness —
if Qdrant isn't up yet, /health reports 503 and the platform's readiness probe retries.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# Attach a stream handler so INFO-level app logs actually reach stderr.
# Setting only the level is not enough: without a handler, the lastResort
# handler kicks in at WARNING, silently dropping every logger.info() call.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# NOTE: do NOT pin `sembr` logger to INFO here. install_logbus() lowers the
# root logger to DEBUG and keeps the basicConfig StreamHandler pinned at INFO,
# so DEBUG records reach RingBufferHandler for per-tag filtering in LogBus
# without flooding stderr. Pinning `sembr` at INFO would short-circuit the
# Logs panel's level dropdown — DEBUG records never escape isEnabledFor().
logging.getLogger("sembr").setLevel(logging.DEBUG)

import aiosqlite

from sembr.api import settings_restart
from sembr.api.external_fire import router as external_fire_router
from sembr.api.feeds import router as feeds_router
from sembr.api.feeds_fire import router as feeds_fire_router
from sembr.api.fire import router as fire_router
from sembr.api.health import router as health_router
from sembr.api.intents import router as intents_router
from sembr.api.maintenance import router as maintenance_router
from sembr.api.prompts import router as prompts_router
from sembr.api.restart import router as restart_router
from sembr.api.settings import router as settings_router
from sembr.api.translate import router as translate_router
from sembr.collector.host_limiter import HostLimiter
from sembr.collector.scheduler import add_feed_job, make_scheduler, set_host_limiter
from sembr.config import get_settings
from sembr.dashboard.auth import DashboardTokenMiddleware
from sembr.dashboard.events import init_event_log_tables
from sembr.dashboard.logs_routes import router as logs_router
from sembr.dashboard.retention import add_log_retention_job
from sembr.dashboard.routes import router as dashboard_router
from sembr.dashboard.system_metrics import (
    SystemMetricsCollector,
    add_system_metrics_job,
)
from sembr.logbus.install import install_logbus
from sembr.db.articles import init_article_tables
from sembr.db.feeds import get_feed_names, init_feed_tables, list_feeds, seed_initial_feeds
from sembr.db.event_buffer import init_event_buffer_tables
from sembr.db.intents import get_intent, init_intent_tables, list_intents
from sembr.db.match_seen import init_match_seen_tables
from sembr.db.sqlite import close_sqlite, init_sqlite
from sembr.matcher.event_buffer import sweep_timed_out as _event_sweep_timed_out
from sembr.matcher.event_cache import EventIntentCache, load_event_cache
from sembr.embedder.factory import build_embedder
from sembr.embedder.scheduler import add_embedder_worker_job
from sembr.collector.fire_tasks import sweep_expired as feed_fire_sweep_expired
from sembr.maintenance import (
    add_dead_ttl_job,
    add_qdrant_ttl_job,
    add_reconcile_job,
    manual_prune_sweep_expired,
)
from sembr.matcher.fire_tasks import sweep_expired
from sembr.matcher.jobs import register_all_enabled
from sembr.notifier.email import EmailChannel, EmailChannelConfig
from sembr.summarizer.llm.factory import build_llm_backend
from sembr.summarizer.models import SummaryResult
from sembr.summarizer.pipeline import SummaryPipeline
from sembr.summarizer.templates import PROMPTS_DIR
from sembr.vector_store.intents import ALIAS_NAME as _INTENTS_ALIAS, ensure_intents_collection
from sembr.vector_store.news import ensure_news_collection
from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)


async def _dispatch_notification(
    conn: aiosqlite.Connection,
    email_ch: EmailChannel,
    result: SummaryResult,
) -> None:
    # Mirrors the never-raise contract of EmailChannel.send — DB errors here must not
    # abort the remaining groups in the same SummaryPipeline tick.
    try:
        intent = await get_intent(conn, result.intent_id)
        if intent is None:
            return
        for ch in intent.channels:
            if isinstance(ch, EmailChannelConfig):
                await email_ch.send(
                    result,
                    config=ch,
                    intent_name=intent.name,
                    intent_timezone=intent.timezone,
                )
    except Exception:
        logger.error(
            "dispatch_notification failed for intent_id=%d", result.intent_id, exc_info=True
        )


async def _get_intent_prompt_ctx(conn, intent_id: int) -> tuple[str, str, str, str]:
    intent = await get_intent(conn, intent_id)
    if intent is None:
        return "default", "default", "", "zh"
    return intent.system_template, intent.instruction_template, intent.text, intent.language


async def _dispatch_template_error(
    conn: aiosqlite.Connection,
    email_ch: EmailChannel,
    intent_id: int,
    kind: str,
    name: str,
    reason: str,
) -> None:
    try:
        intent = await get_intent(conn, intent_id)
        if intent is None:
            return
        for ch in intent.channels:
            if isinstance(ch, EmailChannelConfig):
                await email_ch.send_error(intent.name, kind, name, reason, config=ch)
    except Exception:
        logger.error(
            "dispatch_template_error failed for intent_id=%d template=%s/%s",
            intent_id,
            kind,
            name,
            exc_info=True,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    install_logbus(
        asyncio.get_running_loop(),
        buffer_per_tag=settings.dashboard_log_buffer_per_tag,
        default_level=getattr(logging, settings.dashboard_log_level),
    )
    if not settings.dashboard_token.get_secret_value():
        logger.error(
            "DASHBOARD_TOKEN is empty — dashboard and API are unauthenticated. "
            "This is OK for local development on 127.0.0.1, but unsafe for any "
            "host reachable beyond localhost. Set DASHBOARD_TOKEN in .env "
            "(e.g. `openssl rand -hex 32`) before exposing sembr to a LAN or "
            "the public internet. See docs/deployment/public.md."
        )
    # Validate embedder config before any I/O — raises ValueError if EMBEDDER_API_KEY unset.
    embedder = build_embedder(settings)
    conn = await init_sqlite(settings.sqlite_path)
    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_event_log_tables(conn)  # FK references feeds.id, so feeds first
    await init_intent_tables(conn)  # also chains init_intent_sub_texts_tables (FK CASCADE)
    await init_match_seen_tables(conn)  # after intents — FK dependency
    await init_event_buffer_tables(conn)  # after intents — FK dependency
    await seed_initial_feeds(conn)
    qdrant = QdrantHandle(settings.qdrant_url)
    await ensure_news_collection(qdrant.client, embedder)
    # intent-match-enhancement: pass conn so the migration step (SELECT id,text FROM intents)
    # can re-embed main vectors for the new named-vector layout.
    await ensure_intents_collection(qdrant.client, embedder, conn=conn)
    # Fail-fast assertion: after migration the alias-targeted collection must use
    # the named-vector dict layout with a "main" slot. If it doesn't, lifespan aborts
    # rather than starting in a degraded state where every matcher tick logs warnings.
    _intents_info = await qdrant.client.get_collection(_INTENTS_ALIAS)
    _vectors_cfg = getattr(_intents_info.config.params, "vectors", None)
    if not isinstance(_vectors_cfg, dict) or "main" not in _vectors_cfg:
        raise RuntimeError(
            f"intents collection {_INTENTS_ALIAS!r} is not in named-vector layout "
            f"(vectors_config={_vectors_cfg!r}); the named-vector migration "
            f"did not complete. "
            f"Recovery: inspect Qdrant for an `intents_<model>_mv` collection; "
            f"if absent or has wrong layout, restart the container to retry the "
            f"migration. If retry fails repeatedly, delete the partially-built "
            f'`*_mv` collection (`docker compose exec api python -c "from '
            f"qdrant_client import QdrantClient; QdrantClient(url='http://qdrant:6333')"
            f".delete_collection('<NAME>_mv')\"`) and restart so ensure_intents_collection "
            f"rebuilds it from scratch."
        )
    load_task = asyncio.create_task(embedder.load())  # background; /health probes status
    scheduler = make_scheduler()
    # Per-host concurrency limiter must exist before any feed job can fire so
    # the first tick already sees the cap. set_host_limiter is the module-level
    # handle collect_feed reads; app.state.host_limiter is the readable handle.
    host_limiter = HostLimiter(settings.proxy_hosts_set, max_per_host=2)
    set_host_limiter(host_limiter)
    app.state.host_limiter = host_limiter
    feeds = await list_feeds(conn)
    for feed in feeds:
        if feed.enabled:
            await add_feed_job(scheduler, feed)
    add_embedder_worker_job(scheduler, embedder, qdrant, app)
    add_log_retention_job(scheduler, settings)  # hourly log prune
    # Per-container metrics sampler. Registered before scheduler.start() so the
    # first tick is computed by the trigger (NOT by passing next_run_time=None
    # — that would silently pause the job).
    metrics_collector = SystemMetricsCollector(
        interval_seconds=settings.dashboard_poll_interval_seconds
    )
    add_system_metrics_job(
        scheduler,
        metrics_collector,
        interval_seconds=settings.dashboard_poll_interval_seconds,
    )
    # Three maintenance jobs share `maintenance_interval_hours` but stagger their
    # first fire by 5 / 15 / 25 min so they never hit Qdrant at the same instant.
    add_reconcile_job(scheduler, qdrant, settings)
    add_qdrant_ttl_job(scheduler, qdrant, settings)
    add_dead_ttl_job(scheduler, settings)
    # Sweep expired fire tasks every 5 minutes
    from apscheduler.triggers.interval import IntervalTrigger as _IT  # noqa: PLC0415

    scheduler.add_job(
        sweep_expired,
        trigger=_IT(minutes=5),
        id="fire-tasks-sweep",
        coalesce=True,
        replace_existing=True,
    )
    # Sweep expired feed fire tasks (symmetric with intent fire sweep)
    scheduler.add_job(
        feed_fire_sweep_expired,
        trigger=_IT(minutes=5),
        id="feed-fire-tasks-sweep",
        coalesce=True,
        replace_existing=True,
    )
    # ManualPruneTask in-memory store, swept every 5 min
    scheduler.add_job(
        manual_prune_sweep_expired,
        trigger=_IT(minutes=5),
        id="manual-prune-sweep",
        coalesce=True,
        replace_existing=True,
    )
    # Register per-intent jobs for all currently-enabled intents (restart recovery)
    enabled_intents = await list_intents(conn, enabled=True)
    await register_all_enabled(scheduler, enabled_intents, app, qdrant.client)
    # Load event-mode intent vectors into in-process cache (after register_all_enabled)
    event_intent_cache = EventIntentCache()
    await load_event_cache(event_intent_cache, qdrant, conn)

    # Sweeper flushes timed-out event buffers every 30s
    async def _event_y_sweeper() -> None:
        from sembr.db.sqlite import get_conn as _get_conn  # noqa: PLC0415

        await _event_sweep_timed_out(_get_conn(), app, app.state.event_intent_cache)

    scheduler.add_job(
        _event_y_sweeper,
        trigger=_IT(seconds=30),
        id="event-y-sweeper",
        coalesce=True,
        replace_existing=True,
    )
    # Assign on_match before scheduler.start() so first ticks always find a callback
    llm_backend = build_llm_backend(settings)
    email_ch = EmailChannel(settings)
    pipeline = SummaryPipeline(
        llm=llm_backend,
        get_intent_prompt_ctx=lambda iid: _get_intent_prompt_ctx(conn, iid),
        get_feed_names=lambda ids: get_feed_names(conn, ids),
        on_summary=lambda r: _dispatch_notification(conn, email_ch, r),
        on_template_error=lambda iid, k, n, r: _dispatch_template_error(
            conn, email_ch, iid, k, n, r
        ),
        prompts_dir=PROMPTS_DIR,
    )
    app.state.on_match = pipeline.handle
    # External fire endpoint reaches the pipeline through this handle; it must
    # be set in lifespan adjacent to on_match so both are wired before the
    # first request lands.
    app.state.summary_pipeline = pipeline
    app.state.qdrant = qdrant
    app.state.scheduler = scheduler
    app.state.settings = settings
    app.state.embedder = embedder
    app.state.event_intent_cache = event_intent_cache
    app.state.metrics_collector = metrics_collector
    # Translate endpoint reads the LLM backend from app.state. Wiring it here
    # keeps a single instance for summarizer + translator.
    app.state.llm_backend = llm_backend
    scheduler.start()
    # Log actual next_run_time for matcher jobs after scheduler.start() computes them.
    for job in scheduler.get_jobs():
        if job.id.startswith("matcher-intent-"):
            logger.info("matcher job %s next_run=%s", job.id, job.next_run_time)
    try:
        yield
    finally:

        async def _shutdown() -> None:
            # wait=False: for AsyncIOScheduler, wait=True only blocks on
            # ThreadPoolExecutor jobs — it does NOT await async coroutines like
            # embedder_worker. Blocking the event loop here risks hitting
            # Docker's 10s SIGKILL before aclose/close_sqlite complete.
            scheduler.shutdown(wait=False)
            # Yield one tick so collect_feed coros that already entered but
            # haven't yet read _LIMITER_REF can pick up the live limiter;
            # otherwise they'd silently bypass the per-host gate via the
            # contextlib.nullcontext fallback.
            await asyncio.sleep(0)
            set_host_limiter(None)
            load_task.cancel()
            try:
                await asyncio.wait_for(load_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            if hasattr(embedder, "aclose"):
                await embedder.aclose()
            # Close qdrant first so any in-flight matcher coroutines that
            # survived scheduler.shutdown(wait=False) hit a ClientClosed
            # before the LLM client disappears under them.
            await qdrant.close()
            if hasattr(llm_backend, "aclose"):
                await llm_backend.aclose()
            await close_sqlite()

        try:
            await asyncio.wait_for(_shutdown(), timeout=settings.lifespan_shutdown_timeout)
            logger.info("lifespan graceful shutdown complete")
        except asyncio.TimeoutError:
            logger.error(
                "lifespan graceful shutdown timed out after %ss; forcing exit",
                settings.lifespan_shutdown_timeout,
            )
        finally:
            # Only force-exit when a self-restart was requested.  Normal
            # `docker compose down` leaves _RESTART_REQUESTED=False so
            # uvicorn shuts down cleanly without os._exit.  TestClient paths
            # never send SIGTERM so the flag stays False — no risk of test
            # process being killed.
            if settings_restart.is_restart_requested():
                settings_restart._force_exit(0)


app = FastAPI(title="sembr", version="1.0.0", lifespan=lifespan)
# Auth gate sits in front of every /dashboard and /api/dashboard request.
# When DASHBOARD_TOKEN is empty (default), the middleware is a pass-through.
app.add_middleware(DashboardTokenMiddleware)
app.include_router(health_router)
app.include_router(feeds_router)
app.include_router(feeds_fire_router)
app.include_router(intents_router)
# /intents/translate is a stateless endpoint in its own router module so the
# translate-specific imports (LLMError) and the hardcoded prompt constant
# stay out of api/intents.py.
app.include_router(translate_router)
app.include_router(fire_router)
app.include_router(external_fire_router)
app.include_router(prompts_router)
app.include_router(settings_router)
app.include_router(dashboard_router)
app.include_router(restart_router)
app.include_router(maintenance_router)
app.include_router(logs_router)


class _NoCacheHTMLStaticFiles(StaticFiles):
    """StaticFiles wrapper that disables disk-caching for HTML responses.

    index.html is the entry point that references every JS/CSS via ?v=N
    cache busters. If the browser caches index.html itself, the cache
    busting collapses — the browser keeps reading the OLD `?v=oldN` URLs
    from disk and never sees server-side bumps. Force HTML to revalidate
    on every request (server returns 304 via etag when unchanged, so
    cost is minimal); leave JS/CSS at StaticFiles defaults — their URLs
    change on every content change so long cache is safe.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        ct = response.headers.get("content-type", "")
        if "text/html" in ct:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# Mount /dashboard only when the bundled UI exists. Missing bundle = JSON API still
# works (per AC#10) and startup logs an INFO line.
_dashboard_dir = Path(__file__).resolve().parent.parent / "web" / "static"
if (_dashboard_dir / "index.html").is_file():
    app.mount(
        "/dashboard",
        _NoCacheHTMLStaticFiles(directory=str(_dashboard_dir), html=True),
        name="dashboard",
    )
    logger.info("dashboard static mounted at /dashboard from %s", _dashboard_dir)
else:
    logger.info(
        "web/static/index.html not found at %s; dashboard UI disabled "
        "(JSON API at /api/dashboard/* remains available)",
        _dashboard_dir,
    )
