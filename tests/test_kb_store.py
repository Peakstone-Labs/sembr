# SPDX-License-Identifier: Apache-2.0
"""KB storage-layer tests — v2 tracked-thread + timeline model.

Deterministic merge logic is unit-tested directly (no LLM); the single LLM step
(thread assignment) is driven by a fake backend returning canned thread updates.
"""

from __future__ import annotations

import asyncio

import pytest

from sembr.kb import merge as M
from sembr.kb.gitrepo import GitRepo
from sembr.kb.store import KbSizeError, KbStore

# A canonical v2 events.md: two sections, three tracked threads with timelines.
EVENTS_MD = (
    "## 货币政策\n"
    "\n"
    "### 逆回购利率 <!--k:reverse-repo-->\n"
    "首见 2026-06-01 · 最新 2026-06-20 · 当前：维持1.50%\n"
    "- 2026-06-01 招标维持1.50%\n"
    "- 2026-06-20 继续维持1.50%\n"
    "\n"
    "### MLF <!--k:mlf-->\n"
    "首见 2026-06-02 · 最新 2026-06-18 · 当前：等量续作\n"
    "- 2026-06-18 等量续作\n"
    "\n"
    "## 增长与数据\n"
    "\n"
    "### 社融 <!--k:social-finance-->\n"
    "首见 2026-06-05 · 最新 2026-06-19 · 当前：同比多增\n"
    "- 2026-06-19 同比多增\n"
)


class FakeBackend:
    """structured() returns prebuilt thread updates (the assign step)."""

    def __init__(self, updates: list[dict]) -> None:
        self._updates = updates

    async def structured(self, prompt, schema, *, system=None, model=None, repair_attempts=2):
        return schema(updates=self._updates)


# --------------------------------------------------------------------------- #
# parse / render
# --------------------------------------------------------------------------- #


def test_parse_doc_roundtrip_is_stable() -> None:
    threads, leading = M.parse_doc(EVENTS_MD)
    assert [t.key for t in threads] == ["reverse-repo", "mlf", "social-finance"]
    rr = next(t for t in threads if t.key == "reverse-repo")
    assert rr.section == "货币政策" and rr.first == "2026-06-01" and rr.last == "2026-06-20"
    assert rr.entries == [("2026-06-01", "招标维持1.50%"), ("2026-06-20", "继续维持1.50%")]
    # render(parse(x)) is byte-stable for canonical docs (idempotent lint).
    assert M.render_doc(threads, leading) == EVENTS_MD


def test_parse_events_counts_threads() -> None:
    assert set(M.parse_events(EVENTS_MD)) == {"reverse-repo", "mlf", "social-finance"}


def test_chunk_digest_strips_labels_and_tracks_section() -> None:
    digest = (
        "## 货币政策\n- [新增] 逆回购下调10bp\n- 【持续】MLF 续作\n## 数据\n* 社融多增\n普通段落\n"
    )
    cands = M.chunk_digest(digest)
    assert [c.text for c in cands] == ["逆回购下调10bp", "MLF 续作", "社融多增"]
    assert cands[0].section == "货币政策" and cands[2].section == "数据"


def test_slugify_and_fallback() -> None:
    assert M.slugify("7Day Reverse Repo") == "7day-reverse-repo"
    assert M.slugify("逆回购") == ""
    assert M.stable_fallback_key("事件A") == M.stable_fallback_key("事件A")
    assert M.stable_fallback_key("事件A") != M.stable_fallback_key("事件B")


def test_meta_empty_current_roundtrip_stable() -> None:
    """Review 🟡-2: an empty 当前： must still parse, so render→parse is stable and
    the meta line doesn't get duplicated (unbounded growth) each pass."""
    md = "## S\n\n### t <!--k:t-->\n首见 2026-06-01 · 最新 2026-06-01 · 当前：\n- 2026-06-01 e\n"
    threads, leading = M.parse_doc(md)
    assert len(threads) == 1 and threads[0].current == ""
    rendered = M.render_doc(threads, leading)
    # exactly one meta line; idempotent round-trip (no duplicate 首见 lines).
    assert rendered.count("首见 ") == 1
    assert M.render_doc(*M.parse_doc(rendered)).count("首见 ") == 1


def test_meta_separator_variants_parse() -> None:
    """💡-1: tolerate hand-edit separator variants (· ｜ | , ，) in the meta line."""
    for sep in ["·", "|", "｜", ",", "，"]:
        md = (
            f"## S\n\n### t <!--k:t-->\n首见 2026-06-01 {sep} 最新 2026-06-02 {sep} 当前：x\n"
            "- 2026-06-01 a\n"
        )
        threads = M.parse_doc(md)[0]
        assert len(threads) == 1
        assert threads[0].last == "2026-06-02" and threads[0].current == "x"


def test_headless_thread_not_merged_into_previous() -> None:
    """Review 🟡-1: a headless '### ' block (key deleted) must not have its timeline
    entries absorbed into the previous thread."""
    md = (
        "## S\n\n"
        "### 正常 <!--k:k1-->\n首见 2026-06-01 · 最新 2026-06-01 · 当前：x\n- 2026-06-01 正常进展\n"
        "\n### 缺键的块\n- 2026-06-02 手写进展\n"
    )
    threads, _ = M.parse_doc(md)
    k1 = next(t for t in threads if t.key == "k1")
    assert ("2026-06-02", "手写进展") not in k1.entries  # not polluted into k1
    assert all("手写进展" not in x for _, x in k1.entries)
    # the headless content is preserved somewhere in the rendered output (not lost).
    assert "手写进展" in M.render_doc(threads, M.parse_doc(md)[1])


# --------------------------------------------------------------------------- #
# apply_updates
# --------------------------------------------------------------------------- #


def _apply(md, updates, today):
    threads, _ = M.parse_doc(md)
    threads, stats = M.apply_updates(threads, [M._ThreadUpdate(**u) for u in updates], today)
    return M.render_doc(threads), stats, threads


def test_apply_appends_timeline_entry_to_existing_thread() -> None:
    out, stats, _ = _apply(
        EVENTS_MD,
        [
            {
                "key": "reverse-repo",
                "is_new": False,
                "title": "逆回购利率",
                "section": "货币政策",
                "entry": "下调10bp至1.40%",
                "current_state": "下调至1.40%",
            }
        ],
        "2026-06-27",
    )
    assert stats.updated == 1 and stats.new == 0
    rr = M.parse_events(out)["reverse-repo"]
    # timeline grew (history kept), latest date + current updated, first-seen kept.
    assert ("2026-06-27", "下调10bp至1.40%") in rr.entries
    assert ("2026-06-01", "招标维持1.50%") in rr.entries  # old entries not lost
    assert rr.last == "2026-06-27" and rr.first == "2026-06-01"
    assert rr.current == "下调至1.40%"


def test_apply_new_thread_created() -> None:
    out, stats, _ = _apply(
        EVENTS_MD,
        [
            {
                "key": "rrr-cut",
                "is_new": True,
                "title": "降准",
                "section": "货币政策",
                "entry": "预期7月落地",
                "current_state": "预期升温",
            }
        ],
        "2026-06-27",
    )
    assert stats.new == 1
    t = M.parse_events(out)["rrr-cut"]
    assert t.first == "2026-06-27" and t.entries == [("2026-06-27", "预期7月落地")]


def test_apply_is_new_with_existing_key_updates_not_duplicates() -> None:
    """The v1 bug: is_new=True on an existing key spawned a duplicate. v2 updates."""
    _out, stats, threads = _apply(
        EVENTS_MD,
        [
            {
                "key": "reverse-repo",
                "is_new": True,
                "title": "逆回购利率",
                "section": "货币政策",
                "entry": "今日更新",
                "current_state": "更新",
            }
        ],
        "2026-06-27",
    )
    assert stats.new == 0 and stats.updated == 1
    assert [t.key for t in threads].count("reverse-repo") == 1  # no duplicate thread


def test_apply_one_entry_per_day() -> None:
    upd = {
        "key": "reverse-repo",
        "is_new": False,
        "title": "逆回购利率",
        "section": "货币政策",
        "entry": "first take",
        "current_state": "x",
    }
    threads, _ = M.parse_doc(EVENTS_MD)
    M.apply_updates(threads, [M._ThreadUpdate(**upd)], "2026-06-27")
    upd2 = {**upd, "entry": "revised take", "current_state": "y"}
    threads, _ = M.apply_updates(threads, [M._ThreadUpdate(**upd2)], "2026-06-27")
    rr = {t.key: t for t in threads}["reverse-repo"]
    same_day = [e for e in rr.entries if e[0] == "2026-06-27"]
    assert same_day == [("2026-06-27", "revised take")]  # replaced, not appended twice


def test_archive_expired_moves_whole_thread() -> None:
    threads, _ = M.parse_doc(EVENTS_MD)
    n = M.archive_expired(threads, "2026-07-25")  # all >30d stale
    assert n == 3
    assert all(t.section == M.ARCHIVE_SECTION for t in threads)
    # keys preserved (archived, not dropped)
    assert set(M.parse_events(M.render_doc(threads))) == {"reverse-repo", "mlf", "social-finance"}


def test_archive_keeps_recent() -> None:
    threads, _ = M.parse_doc(EVENTS_MD)
    assert M.archive_expired(threads, "2026-06-25") == 0


async def test_merge_digest_end_to_end() -> None:
    digest = "## 货币政策\n- 逆回购下调\n- 降准预期\n## 增长与数据\n- 社融多增\n"
    backend = FakeBackend(
        [
            {
                "key": "reverse-repo",
                "is_new": False,
                "title": "逆回购利率",
                "section": "货币政策",
                "entry": "下调10bp",
                "current_state": "下调至1.40%",
            },
            {
                "key": "rrr-cut",
                "is_new": True,
                "title": "降准",
                "section": "货币政策",
                "entry": "预期升温",
                "current_state": "预期升温",
            },
        ]
    )
    res = await M.merge_digest(EVENTS_MD, digest, "2026-06-27T09:00:00Z", backend, "m")
    assert res.stats.skipped is None and res.stats.updated == 1 and res.stats.new == 1
    assert "<!--k:rrr-cut-->" in res.content
    assert "- 2026-06-27 下调10bp" in res.content  # timeline entry appended


async def test_merge_digest_low_candidates_skips() -> None:
    res = await M.merge_digest(
        EVENTS_MD, "- one bullet\n", "2026-06-27T09:00:00Z", FakeBackend([]), None
    )
    assert res.stats.skipped == "low_candidates" and res.content == EVENTS_MD


# --------------------------------------------------------------------------- #
# GitRepo
# --------------------------------------------------------------------------- #


def test_gitrepo_tmp_path_real_commit(tmp_path) -> None:
    repo = GitRepo(tmp_path)
    repo.ensure_init()
    repo.ensure_init()
    (tmp_path / "events.md").write_text("hi\n", encoding="utf-8")
    assert repo.commit_all("first", name="sembr-kb", email="kb@sembr.local") is not None
    assert repo.commit_all("noop", name="sembr-kb", email="kb@sembr.local") is None


# --------------------------------------------------------------------------- #
# KbStore
# --------------------------------------------------------------------------- #


def _store(tmp_path) -> KbStore:
    return KbStore(root=tmp_path, git=GitRepo(tmp_path))


async def test_store_atomic_write_and_read(tmp_path) -> None:
    store = _store(tmp_path)
    assert await store.write(1, EVENTS_MD, message="edit via dashboard") is not None
    assert store.read(1) == EVENTS_MD
    assert store.read(2) is None


async def test_store_oversize_rejected(tmp_path) -> None:
    with pytest.raises(KbSizeError):
        await _store(tmp_path).write(1, "x" * (256 * 1024 + 1), message="big")


async def test_store_ingest_skips_when_not_bootstrapped(tmp_path) -> None:
    store = _store(tmp_path)
    stats = await store.ingest(
        7, "2026-06-27T09:00:00Z", "## S\n- a\n- b\n- c\n", backend=FakeBackend([])
    )
    assert stats.skipped == "not_bootstrapped" and not store.path(7).exists()


async def test_store_ingest_merges_and_commits(tmp_path) -> None:
    store = _store(tmp_path)
    await store.write(1, EVENTS_MD, message="seed")
    backend = FakeBackend(
        [
            {
                "key": "reverse-repo",
                "is_new": False,
                "title": "逆回购利率",
                "section": "货币政策",
                "entry": "下调",
                "current_state": "下调至1.40%",
            },
            {
                "key": "pmi",
                "is_new": True,
                "title": "PMI",
                "section": "增长与数据",
                "entry": "回升至50.1",
                "current_state": "回升",
            },
        ]
    )
    stats = await store.ingest(
        1, "2026-06-27T09:00:00Z", "## 货币政策\n- a\n- b\n- c\n", backend=backend
    )
    assert stats.new == 1 and stats.updated == 1
    assert "<!--k:pmi-->" in store.read(1)


async def test_store_key_integrity_warns_on_headless_thread() -> None:
    bad = "## S\n\n### 缺键的线索\n首见 2026-06-01 · 最新 2026-06-01 · 当前：x\n- 2026-06-01 y\n"
    warns = KbStore.validate_key_integrity(bad)
    assert len(warns) == 1 and "missing key anchor" in warns[0]


async def test_store_lock_is_per_intent(tmp_path) -> None:
    store = _store(tmp_path)
    assert store._lock(1) is store._lock(1)
    assert store._lock(1) is not store._lock(2)


def test_store_rebuild_inflight_guard(tmp_path) -> None:
    store = _store(tmp_path)
    assert store.try_begin_rebuild(1) is True
    assert store.try_begin_rebuild(1) is False
    store.end_rebuild(1)
    assert store.try_begin_rebuild(1) is True


def test_store_forget_intent_clears_state(tmp_path) -> None:
    store = _store(tmp_path)
    lock = store._lock(5)
    store.try_begin_rebuild(5)
    store.forget_intent(5)
    assert 5 not in store._rebuilding and store._lock(5) is not lock


async def test_store_concurrent_writers_no_corruption(tmp_path) -> None:
    store = _store(tmp_path)
    contents = [
        f"## S\n\n### t{i} <!--k:k{i}-->\n首见 2026-06-01 · 最新 2026-06-01 · 当前：s{i}\n- 2026-06-01 e{i}\n"
        for i in range(5)
    ]
    await asyncio.gather(*[store.write(1, c, message=f"w{i}") for i, c in enumerate(contents)])
    assert store.read(1) in contents  # intact, not interleaved
    assert int(GitRepo(tmp_path)._run("rev-list", "--count", "HEAD").stdout.strip()) == 5
