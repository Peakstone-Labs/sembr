# SPDX-License-Identifier: Apache-2.0
"""KB lint tests (delta-label/kb SF1, design §7.2).

Lint is deterministic low-risk cleanup: dedup keys, archive stale, mark (not
delete) malformed, drop empty sections. Marking-not-deleting is the safety rule.
"""

from __future__ import annotations

from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sembr.kb import lint as L
from sembr.kb.gitrepo import GitRepo
from sembr.kb.store import KbStore


def test_dedup_keys_keeps_latest() -> None:
    content = (
        "## S\n"
        "- <!--k:repo--> **逆回购**（首见 2026-06-01，最新 2026-06-10）：旧状态\n"
        "- <!--k:repo--> **逆回购**（首见 2026-06-01，最新 2026-06-20）：新状态\n"
        "- <!--k:mlf--> **MLF**（首见 2026-06-02，最新 2026-06-18）：续作\n"
    )
    out, n = L.dedup_keys(content)
    assert n == 1
    repo_lines = [ln for ln in out.splitlines() if "<!--k:repo-->" in ln]
    assert len(repo_lines) == 1
    assert "新状态" in repo_lines[0]  # kept the latest-dated one
    assert "<!--k:mlf-->" in out  # unrelated key untouched


def test_mark_malformed_marks_not_deletes() -> None:
    content = (
        "## S\n"
        "- <!--k:ok--> **t**（首见 2026-06-01，最新 2026-06-01）：s\n"
        "- <!--k:broken--> this line has a key but is not canonical\n"
    )
    out, n = L.mark_malformed(content)
    assert n == 1
    assert L._MALFORMED_MARK in out
    # nothing deleted — both lines still present.
    assert out.count("<!--k:") == 2
    # idempotent: a second pass doesn't double-mark.
    out2, n2 = L.mark_malformed(out)
    assert n2 == 0


def test_remove_empty_sections() -> None:
    content = (
        "## 有内容\n"
        "- <!--k:a--> **t**（首见 2026-06-01，最新 2026-06-01）：s\n"
        "## 空节\n"
        "## 末尾空节\n"
    )
    out, n = L.remove_empty_sections(content)
    assert n == 2
    assert "## 空节" not in out
    assert "## 末尾空节" not in out
    assert "## 有内容" in out


def test_lint_content_combines() -> None:
    content = (
        "## 货币\n"
        "- <!--k:repo--> **逆回购**（首见 2026-05-01，最新 2026-05-02）：很旧\n"
        "- <!--k:repo--> **逆回购**（首见 2026-05-01，最新 2026-05-03）：旧重复\n"
        "## 空\n"
    )
    out, stats = L.lint_content(content, "2026-07-01")
    assert stats.merged_dups == 1
    assert stats.archived == 1  # the surviving repo line is >30d stale → archived
    # both 货币 (emptied by the archive move) and 空 are now empty → removed.
    assert stats.empty_sections == 2
    assert stats.changed >= 3
    # the event is not lost — it lives in the archive section.
    assert "## 已归档" in out and "<!--k:repo-->" in out


async def test_run_for_intent_skips_when_unbuilt(tmp_path) -> None:
    store = KbStore(root=tmp_path, git=GitRepo(tmp_path))
    stats = await L.run_for_intent(store, 9)
    assert stats.skipped == "not_bootstrapped"


async def test_run_for_intent_commits_changes(tmp_path) -> None:
    store = KbStore(root=tmp_path, git=GitRepo(tmp_path))
    content = (
        "## S\n"
        "- <!--k:repo--> **逆回购**（首见 2026-06-01，最新 2026-06-10）：旧\n"
        "- <!--k:repo--> **逆回购**（首见 2026-06-01，最新 2026-06-20）：新\n"
    )
    await store.write(1, content, message="seed")
    anchor = datetime(2026, 6, 25, tzinfo=UTC)
    stats = await L.run_for_intent(store, 1, now=anchor)
    assert stats.merged_dups == 1
    assert store.read(1).count("<!--k:repo-->") == 1
    # a no-op lint run leaves things unchanged.
    stats2 = await L.run_for_intent(store, 1, now=anchor)
    assert stats2.changed == 0


def test_add_kb_lint_job_idempotent_registration(tmp_path) -> None:
    sched = AsyncIOScheduler()
    store = KbStore(root=tmp_path, git=GitRepo(tmp_path))
    L.add_kb_lint_job(sched, store)
    L.add_kb_lint_job(sched, store)  # replace_existing=True → no raise
    job = sched.get_job("weekly-kb-lint")
    assert job is not None
    # not paused (APScheduler discipline: next_run_time is not None when scheduler
    # has been configured) — job exists with a cron trigger.
    assert "cron" in str(job.trigger).lower()
