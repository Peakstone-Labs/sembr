# SPDX-License-Identifier: Apache-2.0
"""Map runner: extract every cited article of one digest into the cache.

Spawned by ``POST /api/intents/{id}/history/{row_id}/extract-sources``. The
endpoint resolves + loads the spec synchronously (so a missing/broken spec is a
clean 4xx/5xx, not a task failure the user has to poll for) and hands the
compiled validator + schema_version in here; this function only does the I/O fan
-out: per citation → Qdrant body → ``extract_one`` → ``put_extraction``.

Concurrency: ``asyncio.Semaphore`` caps simultaneous provider calls (mirrors the
probe). Each article fails independently — an expired body or a stubborn schema
miss lands in ``task.errors`` and never aborts the batch. The whole-task wrapper
catches ``BaseException`` so a shutdown-time cancel still flips the task to error
and releases the lock before re-raising.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sembr.dashboard.read_model import get_article_detail
from sembr.db.mr_cache import extraction_exists, get_extraction, put_extraction
from sembr.db.sqlite import get_conn
from sembr.summarizer.mr_extract_tasks import ExtractTask, release_row
from sembr.summarizer.spec import GeneratedSpec, extract_one

logger = logging.getLogger(__name__)

# Fallback parallelism when the app has no settings wired (e.g. unit tests that
# pass a bare fake app). Production reads ``settings.reduce_concurrency`` so the
# fan-out is tunable live from the dashboard.
_DEFAULT_CONCURRENCY = 16


async def _extract_citation(
    citation: dict,
    *,
    spec: GeneratedSpec,
    validator: type,
    schema_version: str,
    intent_id: int,
    intent_text: str,
    override: bool,
    app,
    task: ExtractTask,
    sem: asyncio.Semaphore,
) -> None:
    """Map one citation; record outcome on the task. Never raises (caught here)."""
    conn = get_conn()
    article_id = citation.get("article_id")
    if not article_id:
        task.progress.errors += 1
        task.errors.append({"article_id": None, "reason": "citation missing article_id"})
        return

    # Cheap skip check stays outside the semaphore so a fully-cached digest
    # returns instantly without occupying provider slots.
    if not override and await extraction_exists(conn, article_id, intent_id, schema_version):
        task.progress.skipped += 1
        return

    async with sem:
        try:
            # article_id is the Qdrant point UUID; get_article_detail keys on the
            # dash-less md5 hex (it rebuilds the UUID internally).
            md5 = str(article_id).replace("-", "")
            detail = await get_article_detail(conn, app.state.qdrant.client, md5, "qdrant")
            if detail is None or not (detail.body or "").strip():
                task.progress.errors += 1
                task.errors.append(
                    {"article_id": article_id, "reason": "article expired or missing in Qdrant"}
                )
                return
            extraction = await extract_one(
                app.state.llm_backend,
                spec,
                validator,
                title=detail.title or citation.get("title") or "",
                body=detail.body,
                model=app.state.settings.effective_reduce_model,
                intent_text=intent_text,
                # Anchor relative time_ref to the article's actual date.
                published_at=detail.published_at or citation.get("published_at"),
                # Publisher fallback when the body/title carry no attribution
                # (e.g. a bare tweet — the handle lives only in the URL).
                url=detail.url or citation.get("url"),
                source_name=citation.get("source_name"),
            )
            await put_extraction(
                conn,
                article_id=article_id,
                intent_id=intent_id,
                schema_version=schema_version,
                extraction=extraction.model_dump(),
                title=detail.title or citation.get("title"),
                source_name=citation.get("source_name"),
                published_at=detail.published_at or citation.get("published_at"),
            )
            task.progress.done += 1
        except Exception as exc:
            # Per-article failure (LLM 5xx after retries, schema miss after
            # repair, Qdrant hiccup). Record and move on — one bad article must
            # not sink the digest.
            task.progress.errors += 1
            task.errors.append({"article_id": article_id, "reason": str(exc)[:200]})


async def run_extract_sources(
    *,
    intent_id: int,
    row_id: int,
    override: bool,
    citations: list[dict],
    spec: GeneratedSpec,
    validator: type,
    schema_version: str,
    intent_text: str,
    app,
    task: ExtractTask,
) -> None:
    """Background orchestrator for one digest's extraction.

    **LOCK OWNERSHIP CONTRACT** — identical posture to ``run_backfill``:
    the caller (``api.history.post_extract_sources``) MUST have already taken
    ``try_acquire_row(row_id)``; this function never acquires it and ALWAYS
    releases it in ``finally`` (even on cancel). Callers must not release it
    once spawn succeeded.
    """
    settings = getattr(app.state, "settings", None)
    concurrency = getattr(settings, "reduce_concurrency", _DEFAULT_CONCURRENCY)
    sem = asyncio.Semaphore(max(1, concurrency))
    try:
        await asyncio.gather(
            *(
                _extract_citation(
                    c,
                    spec=spec,
                    validator=validator,
                    schema_version=schema_version,
                    intent_id=intent_id,
                    intent_text=intent_text,
                    override=override,
                    app=app,
                    task=task,
                    sem=sem,
                )
                for c in citations
            )
        )
        if task.status == "running":
            task.status = "done"
            task.finished_at = datetime.now(UTC)
    except BaseException as exc:
        if task.status == "running":
            task.status = "error"
            task.error_reason = f"unexpected: {type(exc).__name__}"
            task.finished_at = datetime.now(UTC)
        logger.exception("extract-sources: unhandled error for intent=%d row=%d", intent_id, row_id)
        raise
    finally:
        try:
            release_row(row_id)
        except Exception as exc:
            logger.warning(
                "extract-sources: release_row(%d) raised %s during cleanup; ignoring",
                row_id,
                type(exc).__name__,
            )


async def map_for_reduce(
    matches: list,
    *,
    intent_id: int,
    intent_text: str,
    spec: GeneratedSpec,
    validator: type,
    schema_version: str,
    llm,
    model: str,
    concurrency: int,
    feed_name_map: dict[int, str] | None = None,
) -> tuple[list[dict], int]:
    """Map recalled articles → render-ready records for reduce (design §2/§5).

    Returns ``(records, n_failed)``. One record per match, in input order, each
    stamped ``index`` = 1-based recall position so the rendered ``[N]`` aligns
    with ``summary_history.citations``. Cache hit → reuse; miss → ``extract_one``
    (D4 map-on-recall) → ``put_extraction``. Per-article failure or empty body →
    record marked ``no_relevant_content`` (never raised) and counted in
    ``n_failed``; one bad article never sinks the run (D2). ``n_failed`` drives
    the ``facts`` vs ``facts_partial`` reduce_mode in the caller.

    Bodies come from the match payload (recall already carries them) — no Qdrant
    round-trip, unlike the digest-citation extract path (``run_extract_sources``).
    Distinct from that path: no ``ExtractTask`` / per-row lock (there is no
    history row yet — the digest is mid-computation).
    """
    conn = get_conn()
    feed_name_map = feed_name_map or {}
    sem = asyncio.Semaphore(max(1, concurrency))
    records: list[dict | None] = [None] * len(matches)
    n_failed = 0

    async def _one(i: int, match) -> None:
        nonlocal n_failed
        payload = getattr(match, "payload", None) or {}
        source_name = feed_name_map.get(payload.get("feed_id", 0))
        published_at = payload.get("published_at")
        # index/source_name/published_at are attached at runtime (not part of the
        # extraction shell); render_facts reads them for the article list + [N].
        base = {"index": i + 1, "source_name": source_name, "published_at": published_at}
        article_id = getattr(match, "article_id", None)

        # ONE try wraps the WHOLE article — cache read, dict unpack, AND extract.
        # Any single-article failure (DB hiccup on the shared conn, a dirty cache
        # row missing/!= dict "extraction", an LLM 5xx) must degrade to an empty
        # record, never escape through asyncio.gather and sink the run. The outer
        # _build_facts_articles_text deliberately does NOT guard map_for_reduce,
        # so this per-article isolation is what upholds the "failure doesn't sink
        # the run" + D2 fail-open guarantee.
        try:
            # Cheap cache read stays outside the semaphore so a fully-cached recall
            # never occupies provider slots (mirrors _extract_citation's skip check).
            cached = (
                await get_extraction(conn, article_id, intent_id, schema_version)
                if article_id
                else None
            )
            if cached is not None and isinstance(cached.get("extraction"), dict):
                records[i] = {
                    **cached["extraction"],
                    **base,
                    # cache row's stored published_at (set at extract time) wins,
                    # else the recall payload's; the extraction JSON carries no
                    # published_at field of its own.
                    "published_at": cached.get("published_at") or published_at,
                }
                return

            title = payload.get("title") or ""
            body = payload.get("body") or ""
            if not article_id or not body.strip():
                # Empty/missing body: can't map → empty record (no LLM call),
                # counted so the caller can flag facts_partial.
                n_failed += 1
                records[i] = {**base, "no_relevant_content": True, "claims": []}
                return

            async with sem:
                result = await extract_one(
                    llm,
                    spec,
                    validator,
                    title=title,
                    body=body,
                    model=model,
                    intent_text=intent_text,
                    published_at=published_at,
                    url=payload.get("url"),
                    source_name=source_name,
                )
                extraction = result.model_dump()
                await put_extraction(
                    conn,
                    article_id=article_id,
                    intent_id=intent_id,
                    schema_version=schema_version,
                    extraction=extraction,
                    title=title,
                    source_name=source_name,
                    published_at=published_at,
                )
                records[i] = {**extraction, **base}
        except Exception as exc:
            # Any per-article failure (DB error, dirty cache row, LLM 5xx after
            # retries, schema miss after repair) → empty record + count; never
            # sink the run.
            n_failed += 1
            records[i] = {**base, "no_relevant_content": True, "claims": []}
            logger.warning(
                "map_for_reduce: map failed for article=%s intent=%d: %s",
                article_id,
                intent_id,
                str(exc)[:200],
            )

    await asyncio.gather(*(_one(i, m) for i, m in enumerate(matches)))
    return [r for r in records if r is not None], n_failed
