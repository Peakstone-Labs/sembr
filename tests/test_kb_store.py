# SPDX-License-Identifier: Apache-2.0
"""KB storage-layer tests (delta-label/kb SF1, design §4/§5 + §13 review hardening).

Deterministic merge logic is unit-tested directly (no LLM). The single LLM step
(key assignment) is driven by a fake backend returning canned assignments, so the
merge/apply/archive guarantees are tested without network.
"""

from __future__ import annotations

import asyncio

import pytest

from sembr.kb import merge as M
from sembr.kb.gitrepo import GitRepo
from sembr.kb.store import KbSizeError, KbStore

# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #

EVENTS_MD = (
    "## 货币政策\n"
    "- <!--k:reverse-repo--> **7天逆回购利率**（首见 2026-06-01，最新 2026-06-20）：维持1.50%\n"
    "- <!--k:mlf--> **MLF**（首见 2026-06-02，最新 2026-06-18）：等量续作\n"
    "\n## 增长与数据\n"
    "- <!--k:social-finance--> **社融**（首见 2026-06-05，最新 2026-06-19）：同比多增\n"
)


class FakeBackend:
    """Minimal stand-in: ``structured`` returns prebuilt assignments."""

    def __init__(self, assignments: list[dict]) -> None:
        self._assignments = assignments

    async def structured(self, prompt, schema, *, system=None, model=None, repair_attempts=2):
        return schema(assignments=self._assignments)


# --------------------------------------------------------------------------- #
# Deterministic merge helpers
# --------------------------------------------------------------------------- #


def test_chunk_digest_strips_labels_and_tracks_section() -> None:
    digest = (
        "## 货币政策\n"
        "- [新增] 7天逆回购下调10bp至1.40%\n"
        "- 【持续】MLF 等量续作\n"
        "## 数据\n"
        "* 社融同比多增\n"
        "普通段落不是候选\n"
    )
    cands = M.chunk_digest(digest)
    assert [c.text for c in cands] == [
        "7天逆回购下调10bp至1.40%",
        "MLF 等量续作",
        "社融同比多增",
    ]
    assert cands[0].section == "货币政策"
    assert cands[2].section == "数据"


def test_slugify_canonicalizes() -> None:
    assert M.slugify("7Day Reverse Repo") == "7day-reverse-repo"
    assert M.slugify("  Foo--Bar  ") == "foo-bar"
    assert M.slugify("逆回购") == ""  # non-ascii collapses → caller falls back


def test_parse_events_roundtrip() -> None:
    events = M.parse_events(EVENTS_MD)
    assert set(events) == {"reverse-repo", "mlf", "social-finance"}
    assert events["reverse-repo"].first == "2026-06-01"
    assert events["reverse-repo"].last == "2026-06-20"


def test_apply_merge_existing_key_updates_single_line() -> None:
    cands = [M.Candidate(text="逆回购下调", section="货币政策")]
    assigns = [
        M._Assignment(
            candidate_index=0,
            key="reverse-repo",
            is_new=False,
            title="ignored",
            section="货币政策",
            state="下调10bp至1.40%",
        )
    ]
    out, stats = M.apply_merge(EVENTS_MD, cands, assigns, "2026-06-27")
    assert stats.updated == 1 and stats.new == 0
    line = next(ln for ln in out.splitlines() if "<!--k:reverse-repo-->" in ln)
    # first-seen + title preserved; latest date + state updated.
    assert "首见 2026-06-01" in line
    assert "最新 2026-06-27" in line
    assert "下调10bp至1.40%" in line
    assert "**7天逆回购利率**" in line
    # Sibling lines byte-preserved (no drop).
    assert "<!--k:mlf-->" in out and "<!--k:social-finance-->" in out


def test_apply_merge_new_key_appended_to_section() -> None:
    cands = [M.Candidate(text="降准", section="货币政策")]
    assigns = [
        M._Assignment(
            candidate_index=0,
            key="rrr-cut",
            is_new=True,
            title="降准",
            section="货币政策",
            state="预期7月落地",
        )
    ]
    out, stats = M.apply_merge(EVENTS_MD, cands, assigns, "2026-06-27")
    assert stats.new == 1
    line = next(ln for ln in out.splitlines() if "<!--k:rrr-cut-->" in ln)
    assert "首见 2026-06-27" in line and "最新 2026-06-27" in line
    assert M.parse_events(out).keys() >= {"reverse-repo", "mlf", "social-finance", "rrr-cut"}


def test_apply_merge_hallucinated_section_falls_back() -> None:
    cands = [M.Candidate(text="x", section="不存在的节")]
    assigns = [
        M._Assignment(
            candidate_index=0,
            key="brand-new",
            is_new=True,
            title="新事件",
            section="一个从未出现过的节",
            state="状态",
        )
    ]
    out, _ = M.apply_merge(EVENTS_MD, cands, assigns, "2026-06-27")
    assert f"## {M.FALLBACK_SECTION}" in out  # not the hallucinated section name
    assert "一个从未出现过的节" not in out


def test_apply_merge_no_false_key_match_keeps_distinct_event() -> None:
    """T3: a conservative assigner flags a semantically-distinct event is_new →
    it gets its own line; the similar existing event's line is untouched."""
    cands = [M.Candidate(text="某完全不同的事件", section="货币政策")]
    assigns = [
        M._Assignment(
            candidate_index=0,
            key="distinct-event",
            is_new=True,
            title="不同事件",
            section="货币政策",
            state="新状态",
        )
    ]
    out, stats = M.apply_merge(EVENTS_MD, cands, assigns, "2026-06-27")
    assert stats.new == 1 and stats.updated == 0
    # original reverse-repo state must not have been clobbered.
    rr = next(ln for ln in out.splitlines() if "<!--k:reverse-repo-->" in ln)
    assert "维持1.50%" in rr
    assert "<!--k:distinct-event-->" in out


def test_archive_expired_moves_not_deletes() -> None:
    out, n = M.archive_expired(EVENTS_MD, "2026-07-25")  # >30d after 2026-06-x
    assert n == 3
    assert f"## {M.ARCHIVE_SECTION}" in out
    # all keys still present (archived, not dropped) — C1.
    assert M.parse_events(out).keys() == {"reverse-repo", "mlf", "social-finance"}


def test_archive_expired_keeps_recent() -> None:
    out, n = M.archive_expired(EVENTS_MD, "2026-06-25")  # within 30d
    assert n == 0
    assert out == EVENTS_MD


async def test_merge_digest_end_to_end_with_fake_backend() -> None:
    digest = "## 货币政策\n- [新增] 逆回购下调10bp\n- 降准预期升温\n## 数据\n- 社融同比多增\n"
    backend = FakeBackend(
        [
            {
                "candidate_index": 0,
                "key": "reverse-repo",
                "is_new": False,
                "title": "7天逆回购利率",
                "section": "货币政策",
                "state": "下调10bp至1.40%",
            },
            {
                "candidate_index": 1,
                "key": "rrr-cut",
                "is_new": True,
                "title": "降准",
                "section": "货币政策",
                "state": "预期升温",
            },
            {
                "candidate_index": 2,
                "key": "social-finance",
                "is_new": False,
                "title": "社融",
                "section": "数据",
                "state": "同比多增扩大",
            },
        ]
    )
    res = await M.merge_digest(EVENTS_MD, digest, "2026-06-27T09:00:00Z", backend, "fake-model")
    assert res.stats.skipped is None
    assert res.stats.updated == 2 and res.stats.new == 1
    assert "<!--k:rrr-cut-->" in res.content


async def test_merge_digest_low_candidates_skips() -> None:
    backend = FakeBackend([])  # never called
    res = await M.merge_digest(
        EVENTS_MD, "- only one bullet\n", "2026-06-27T09:00:00Z", backend, None
    )
    assert res.stats.skipped == "low_candidates"
    assert res.content == EVENTS_MD


# --------------------------------------------------------------------------- #
# GitRepo
# --------------------------------------------------------------------------- #


def test_gitrepo_tmp_path_real_commit(tmp_path) -> None:
    repo = GitRepo(tmp_path)
    repo.ensure_init()
    repo.ensure_init()  # idempotent — second init must not raise
    (tmp_path / "events.md").write_text("hello\n", encoding="utf-8")
    h = repo.commit_all("first", name="sembr-kb", email="kb@sembr.local")
    assert h is not None  # real commit, inline identity (no global git config needed)
    # nothing changed → no new commit
    assert repo.commit_all("noop", name="sembr-kb", email="kb@sembr.local") is None


# --------------------------------------------------------------------------- #
# KbStore
# --------------------------------------------------------------------------- #


def _store(tmp_path) -> KbStore:
    return KbStore(root=tmp_path, git=GitRepo(tmp_path))


async def test_store_atomic_write_and_read(tmp_path) -> None:
    store = _store(tmp_path)
    h = await store.write(1, EVENTS_MD, message="edit intent-1 events via dashboard")
    assert h is not None
    assert store.read(1) == EVENTS_MD
    assert store.read(2) is None  # unbuilt intent


async def test_store_oversize_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    big = "x" * (256 * 1024 + 1)
    with pytest.raises(KbSizeError):
        await store.write(1, big, message="too big")


async def test_store_ingest_skips_when_not_bootstrapped(tmp_path) -> None:
    """T1/F1: ingest into an unbuilt KB skips + does not create the file."""
    store = _store(tmp_path)
    backend = FakeBackend([])  # must not be called
    stats = await store.ingest(7, "2026-06-27T09:00:00Z", "## S\n- a\n- b\n- c\n", backend=backend)
    assert stats.skipped == "not_bootstrapped"
    assert not store.path(7).exists()


async def test_store_ingest_low_candidates_skips(tmp_path) -> None:
    store = _store(tmp_path)
    await store.write(1, EVENTS_MD, message="seed")
    backend = FakeBackend([])
    stats = await store.ingest(1, "2026-06-27T09:00:00Z", "- single bullet only\n", backend=backend)
    assert stats.skipped == "low_candidates"


async def test_store_ingest_merges_and_commits(tmp_path) -> None:
    store = _store(tmp_path)
    await store.write(1, EVENTS_MD, message="seed")
    backend = FakeBackend(
        [
            {
                "candidate_index": 0,
                "key": "reverse-repo",
                "is_new": False,
                "title": "7天逆回购利率",
                "section": "货币政策",
                "state": "下调至1.40%",
            },
            {
                "candidate_index": 1,
                "key": "rrr-cut",
                "is_new": True,
                "title": "降准",
                "section": "货币政策",
                "state": "预期升温",
            },
            {
                "candidate_index": 2,
                "key": "pmi",
                "is_new": True,
                "title": "PMI",
                "section": "增长与数据",
                "state": "回升至50.1",
            },
        ]
    )
    digest = "## 货币政策\n- 逆回购下调\n- 降准预期\n## 增长与数据\n- PMI回升\n"
    stats = await store.ingest(1, "2026-06-27T09:00:00Z", digest, backend=backend)
    assert stats.skipped is None and stats.new == 2 and stats.updated == 1
    assert "<!--k:rrr-cut-->" in store.read(1)


async def test_store_key_integrity_warnings() -> None:
    bad = (
        "## S\n"
        "- <!--k:ok--> **t**（首见 2026-06-01，最新 2026-06-01）：s\n"
        "- 这一行丢了键注释\n"
    )
    warns = KbStore.validate_key_integrity(bad)
    assert len(warns) == 1
    assert "missing key anchor" in warns[0]


async def test_store_lock_is_per_intent(tmp_path) -> None:
    store = _store(tmp_path)
    assert store._lock(1) is store._lock(1)
    assert store._lock(1) is not store._lock(2)


async def test_store_concurrent_writers_no_corruption(tmp_path) -> None:
    """T2/F2: concurrent writes to one intent serialize → file is exactly one
    writer's content (never an interleaved mix), and every write commits."""
    store = _store(tmp_path)
    contents = [
        f"## S\n- <!--k:k{i}--> **t{i}**（首见 2026-06-01，最新 2026-06-01）：s{i}\n"
        for i in range(5)
    ]
    await asyncio.gather(*[store.write(1, c, message=f"w{i}") for i, c in enumerate(contents)])
    final = store.read(1)
    assert final in contents  # intact, not a mix
    # 5 commits recorded in the KB git history.
    log = GitRepo(tmp_path)._run("rev-list", "--count", "HEAD").stdout.strip()
    assert int(log) == 5
