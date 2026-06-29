# SPDX-License-Identifier: Apache-2.0
"""KB lint tests — v2 thread model.

Deterministic cleanup: merge duplicate-key threads (+ collapse same-day entries),
archive whole stale threads, mark (not delete) malformed headings.
"""

from __future__ import annotations

from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sembr.kb import lint as L
from sembr.kb import merge as M
from sembr.kb.gitrepo import GitRepo
from sembr.kb.store import KbStore


def test_dedup_threads_merges_same_key_and_unions_timeline() -> None:
    content = (
        "## S\n\n"
        "### 逆回购 <!--k:repo-->\n"
        "首见 2026-06-01 · 最新 2026-06-10 · 当前：旧\n"
        "- 2026-06-01 a\n"
        "- 2026-06-10 b\n"
        "\n"
        "### 逆回购(重复块) <!--k:repo-->\n"
        "首见 2026-06-05 · 最新 2026-06-20 · 当前：新\n"
        "- 2026-06-20 c\n"
    )
    out, n = L.dedup_threads(content)
    assert n == 1
    threads = M.parse_events(out)
    assert len(threads) == 1
    repo = threads["repo"]
    # timelines unioned, latest current/last kept, earliest first kept.
    assert {d for d, _ in repo.entries} == {"2026-06-01", "2026-06-10", "2026-06-20"}
    assert repo.current == "新" and repo.last == "2026-06-20" and repo.first == "2026-06-01"


def test_dedup_collapses_same_day_entries() -> None:
    content = (
        "## S\n\n### t <!--k:t-->\n首见 2026-06-01 · 最新 2026-06-01 · 当前：x\n"
        "- 2026-06-01 first\n- 2026-06-01 second\n"
    )
    out, _ = L.dedup_threads(content)
    entries = M.parse_events(out)["t"].entries
    assert entries == [("2026-06-01", "second")]  # one per day, last wins


def test_archive_stale_moves_thread() -> None:
    content = "## 货币\n\n### t <!--k:t-->\n首见 2026-05-01 · 最新 2026-05-02 · 当前：旧\n- 2026-05-02 x\n"
    out, n = L.archive_stale(content, "2026-07-01")
    assert n == 1 and f"## {M.ARCHIVE_SECTION}" in out and "<!--k:t-->" in out


def test_mark_malformed_marks_headless_thread_not_delete() -> None:
    content = "## S\n\n### 正常 <!--k:ok-->\n首见 2026-06-01 · 最新 2026-06-01 · 当前：x\n- 2026-06-01 y\n\n### 缺键的线索\n"
    out, n = L.mark_malformed(content)
    assert n == 1 and L._MALFORMED_MARK in out
    assert "### 缺键的线索" in out  # not deleted
    out2, n2 = L.mark_malformed(out)
    assert n2 == 0  # idempotent


def test_lint_content_combines() -> None:
    content = (
        "## 货币\n\n"
        "### 逆回购 <!--k:repo-->\n首见 2026-05-01 · 最新 2026-05-02 · 当前：旧\n- 2026-05-02 a\n"
        "\n### 逆回购重复 <!--k:repo-->\n首见 2026-05-01 · 最新 2026-05-03 · 当前：旧2\n- 2026-05-03 b\n"
    )
    out, stats = L.lint_content(content, "2026-07-01")
    assert stats.merged_dups == 1
    assert stats.archived == 1  # surviving repo thread is >30d stale → archived
    assert M.parse_events(out)["repo"].section == M.ARCHIVE_SECTION


def test_lint_content_noop_on_canonical_doc() -> None:
    from tests.test_kb_store import EVENTS_MD

    out, stats = L.lint_content(EVENTS_MD, "2026-06-25")  # within 30d → no archive
    assert stats.changed == 0 and out == EVENTS_MD  # idempotent on clean canonical doc


async def test_run_for_intent_skips_when_unbuilt(tmp_path) -> None:
    store = KbStore(root=tmp_path, git=GitRepo(tmp_path))
    assert (await L.run_for_intent(store, 9)).skipped == "not_bootstrapped"


async def test_run_for_intent_commits_changes(tmp_path) -> None:
    store = KbStore(root=tmp_path, git=GitRepo(tmp_path))
    content = (
        "## S\n\n### 逆回购 <!--k:repo-->\n首见 2026-06-01 · 最新 2026-06-10 · 当前：旧\n- 2026-06-10 a\n"
        "\n### 逆回购2 <!--k:repo-->\n首见 2026-06-01 · 最新 2026-06-20 · 当前：新\n- 2026-06-20 b\n"
    )
    await store.write(1, content, message="seed")
    anchor = datetime(2026, 6, 25, tzinfo=UTC)
    stats = await L.run_for_intent(store, 1, now=anchor)
    assert stats.merged_dups == 1
    assert len(M.parse_events(store.read(1))) == 1
    assert (await L.run_for_intent(store, 1, now=anchor)).changed == 0  # idempotent


def test_add_kb_lint_job_idempotent_registration(tmp_path) -> None:
    sched = AsyncIOScheduler()
    store = KbStore(root=tmp_path, git=GitRepo(tmp_path))
    L.add_kb_lint_job(sched, store)
    L.add_kb_lint_job(sched, store)
    job = sched.get_job("weekly-kb-lint")
    assert job is not None and "cron" in str(job.trigger).lower()
