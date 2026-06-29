# SPDX-License-Identifier: Apache-2.0
"""P3 — KB ingest wiring + cold-start distill (design §2.1/§3.3 + F3).

Three layers:
- pipeline `_dispatch`: on_kb_ingest fires only on the persist path, after
  on_persist, isolated (never-raise) — so manual fire (persist=False) never
  mutates the KB and an ingest failure can't block notification.
- main `_kb_ingest`: early-returns when the intent has kb_enabled=0.
- distill: structured LLM events → canonical events.md.
"""

from __future__ import annotations

import aiosqlite

from sembr.db.intents import create_intent, init_intent_tables, update_intent
from sembr.db.sqlite import install_for_test
from sembr.kb import distill as D
from sembr.kb import merge as M
from sembr.kb.gitrepo import GitRepo
from sembr.kb.store import KbStore
from sembr.models import IntentCreate, IntentUpdate
from sembr.summarizer.models import SummaryResult
from sembr.summarizer.pipeline import SummaryPipeline


def _result() -> SummaryResult:
    return SummaryResult(intent_id=1, summary="## S\n- a\n- b\n- c\n")


def _pipeline(**cbs) -> SummaryPipeline:
    # llm is unused by _dispatch; pass a placeholder.
    return SummaryPipeline(llm=object(), **cbs)


# --------------------------------------------------------------------------- #
# pipeline _dispatch
# --------------------------------------------------------------------------- #


async def test_dispatch_kb_ingest_runs_on_persist() -> None:
    order: list[str] = []

    async def on_persist(r):
        order.append("persist")

    async def on_kb_ingest(r):
        order.append("kb")

    async def on_summary(r):
        order.append("summary")

    pipe = _pipeline(on_persist=on_persist, on_kb_ingest=on_kb_ingest, on_summary=on_summary)
    await pipe._dispatch(_result(), persist=True, intent_id=1)
    # Order: persist → kb ingest → summary (design §3.3).
    assert order == ["persist", "kb", "summary"]


async def test_dispatch_no_kb_ingest_when_not_persist() -> None:
    """F3: fire_handle (persist=False) must never run KB ingest."""
    called: list[str] = []

    async def on_kb_ingest(r):
        called.append("kb")

    async def on_summary(r):
        called.append("summary")

    pipe = _pipeline(on_kb_ingest=on_kb_ingest, on_summary=on_summary)
    await pipe._dispatch(_result(), persist=False, intent_id=1)
    assert called == ["summary"]  # kb ingest skipped on the non-persist path


async def test_dispatch_kb_ingest_failure_isolated() -> None:
    """An ingest failure is swallowed and does not block on_summary."""
    summary_called: list[str] = []

    async def bad_kb_ingest(r):
        raise RuntimeError("merge blew up")

    async def on_summary(r):
        summary_called.append("summary")

    pipe = _pipeline(on_kb_ingest=bad_kb_ingest, on_summary=on_summary)
    await pipe._dispatch(_result(), persist=True, intent_id=1)  # must not raise
    assert summary_called == ["summary"]


# --------------------------------------------------------------------------- #
# main._kb_ingest gating
# --------------------------------------------------------------------------- #


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def ingest(self, intent_id, run_at, digest_text, *, backend, merge_model):
        self.calls.append((intent_id, digest_text, merge_model))
        return M.MergeStats()


async def test_kb_ingest_skips_when_disabled() -> None:
    from sembr.main import _kb_ingest

    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        install_for_test(conn)
        intent = await create_intent(
            conn,
            IntentCreate.model_validate(
                {"name": "x", "text": "t", "channels": [{"type": "email", "to": ["a@b.c"]}]}
            ),
        )
        store = _RecordingStore()
        # kb_enabled defaults to 0 → ingest must be skipped.
        await _kb_ingest(
            SummaryResult(intent_id=intent.id, summary="d"),
            store=store,
            backend=object(),
            merge_model="m",
        )
        assert store.calls == []
        # Flip on → ingest is invoked.
        await update_intent(conn, intent.id, IntentUpdate(kb_enabled=True))
        await _kb_ingest(
            SummaryResult(intent_id=intent.id, summary="digest text"),
            store=store,
            backend=object(),
            merge_model="m",
        )
        assert len(store.calls) == 1
        assert store.calls[0][0] == intent.id and store.calls[0][2] == "m"
    finally:
        await conn.close()


# --------------------------------------------------------------------------- #
# distill
# --------------------------------------------------------------------------- #


class _FakeDistillBackend:
    def __init__(self, threads: list[dict]) -> None:
        self._threads = threads

    async def structured(self, prompt, schema, *, system=None, model=None, repair_attempts=2):
        return schema(threads=self._threads)


def _thread(title, section, first, current, timeline):
    return {
        "title": title,
        "section": section,
        "first_seen": first,
        "current_state": current,
        "timeline": [{"date": d, "entry": e} for d, e in timeline],
    }


def test_render_threads_canonical_with_timeline() -> None:
    threads = [
        D._DistillThread(
            **_thread(
                "逆回购利率",
                "货币政策",
                "2026-06-01",
                "维持1.50%",
                [("2026-06-01", "招标1.50%"), ("2026-06-20", "维持")],
            )
        ),
        D._DistillThread(
            **_thread("MLF", "货币政策", "bad-date", "等量续作", [("2026-06-18", "等量续作")])
        ),
    ]
    md = D.render_threads(threads, "2026-06-25")
    threads_out = M.parse_doc(md)[0]
    assert len(threads_out) == 2
    # pure-Chinese title → opaque content-hash key (invisible HTML comment); MLF has
    # ASCII so it slugs to "mlf". Look threads up by title, not key.
    repo = next(t for t in threads_out if t.title == "逆回购利率")
    assert len(repo.entries) == 2 and repo.current == "维持1.50%"
    mlf = next(t for t in threads_out if t.title == "MLF")
    assert mlf.first == "2026-06-25"  # bad first_seen → fallback to now_date


def test_render_threads_uses_llm_ascii_key() -> None:
    """R1b: prefer the LLM's ASCII key; empty key → title slug → hash fallback."""
    t = D._DistillThread(
        key="oil-price",
        title="油价",
        section="石油市场",
        first_seen="2026-06-01",
        current_state="跌",
        timeline=[D._TimelineEntry(date="2026-06-01", entry="x")],
    )
    assert "<!--k:oil-price-->" in D.render_threads([t], "2026-06-25")  # ascii key, not a hash
    t2 = D._DistillThread(**_thread("逆回购", "S", "2026-06-01", "x", [("2026-06-01", "y")]))
    key2 = next(iter(M.parse_events(D.render_threads([t2], "2026-06-25"))))
    assert key2.startswith("event-")  # no LLM key + Chinese title → content-hash fallback


def test_render_threads_chinese_titles_distinct_stable_keys() -> None:
    """Review 🔴-1: all-Chinese titles get distinct, stable content-hash keys."""
    ts = [
        D._DistillThread(**_thread("中国宏观甲", "S", "2026-06-01", "a", [("2026-06-01", "x")])),
        D._DistillThread(**_thread("中国宏观乙", "S", "2026-06-01", "b", [("2026-06-01", "y")])),
    ]
    keys = set(M.parse_events(D.render_threads(ts, "2026-06-25")))
    assert len(keys) == 2
    assert set(M.parse_events(D.render_threads(ts, "2026-06-26"))) == keys


async def test_distill_events_produces_mergeable_index() -> None:
    backend = _FakeDistillBackend(
        [
            _thread("逆回购利率", "货币政策", "2026-06-01", "维持1.50%", [("2026-06-20", "维持")]),
            _thread("社融", "增长与数据", "2026-06-05", "同比多增", [("2026-06-19", "多增")]),
        ]
    )
    md = await D.distill_events("=== 2026-06-20 ===\n逆回购维持...\n", backend, "pro", "2026-06-25")
    assert len(M.parse_events(md)) == 2  # canonical, parseable by the merge layer


async def test_bootstrap_intent_writes_events(tmp_path) -> None:
    store = KbStore(root=tmp_path, git=GitRepo(tmp_path))
    backend = _FakeDistillBackend(
        [
            _thread("逆回购利率", "货币政策", "2026-06-01", "维持1.50%", [("2026-06-20", "维持")]),
            _thread("降准", "货币政策", "2026-06-10", "预期升温", [("2026-06-22", "升温")]),
        ]
    )
    content = await D.bootstrap_intent(store, 5, "history prose", backend, model="pro")
    assert store.read(5) == content
    assert len(M.parse_events(content)) == 2
