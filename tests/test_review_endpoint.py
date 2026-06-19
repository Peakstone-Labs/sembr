# SPDX-License-Identifier: Apache-2.0
"""Tests for review-endpoint feature (manual review of persisted history rows).

Dev-owned tests (from design.md Test Strategy table):
  T1, T2, T3, T5, T23, T8-T10, T14, T18

QA-owned tests added by QA subagent (Loop 1):
  T4, T7, T11-T13, T15-T17, T19-T22

T6 (update_summary_happy_path), T8-T10, T14 are dev-owned HTTP endpoint
tests deferred by dev progress.md.  They are NOT re-implemented here to
avoid duplication — the dev is responsible for them.
T24 is manual-only (scripts/qa_review_endpoint_golden.sh on Mac mini).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from sembr.summarizer.review import (
    build_articles_text_from_citations,
    run_review_gate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm(responses: list[str]) -> AsyncMock:
    llm = AsyncMock()
    llm.summarize = AsyncMock(side_effect=list(responses))
    llm.max_prompt_chars = 2_000_000
    return llm


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    (d / "system").mkdir(parents=True)
    (d / "instruction").mkdir(parents=True)
    (d / "system" / "review.md").write_text("Review system: {language}", encoding="utf-8")
    (d / "instruction" / "review.md").write_text(
        "Digest:\n{intent_text}\n\nArticles:\n{articles}", encoding="utf-8"
    )
    return d


# ---------------------------------------------------------------------------
# T1: D1 signature change — run_review_gate returns tuple
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_review_gate_returns_tuple_success(prompts_dir):
    """Success path returns (str, list) tuple."""
    llm = _make_llm([
        '{"corrections":[{"quote":"bad","replacement":"good","cited":[1]}]}'
    ])
    result = await run_review_gate(
        llm, intent_id=1, summary_raw="bad text",
        articles_text="[1] Article\nBody\nSource: News (url)",
        language="zh", run_at="2026-06-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert isinstance(result, tuple)
    assert len(result) == 2
    corrected, corrections = result
    assert isinstance(corrected, str)
    assert isinstance(corrections, list)
    assert corrected == "good text"
    assert len(corrections) == 1
    assert corrections[0]["matched"] is True


@pytest.mark.asyncio
async def test_run_review_gate_returns_tuple_failopen(prompts_dir):
    """Fail-open path (LLM error) returns (str, []) — NOT a bare str."""
    llm = _make_llm([])
    llm.summarize = AsyncMock(side_effect=RuntimeError("LLM down"))
    result = await run_review_gate(
        llm, intent_id=1, summary_raw="original text",
        articles_text="[1] Art\nBody\nSource: url",
        language="zh", run_at="2026-06-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert isinstance(result, tuple)
    corrected, corrections = result
    assert corrected == "original text"  # fail-open: returns original
    assert corrections == []             # no corrections


@pytest.mark.asyncio
async def test_run_review_gate_zero_corrections_returns_tuple(prompts_dir):
    """0 corrections → (summary_raw, []) tuple (not bare str)."""
    llm = _make_llm(['{"corrections":[]}'])
    result = await run_review_gate(
        llm, intent_id=1, summary_raw="clean digest",
        articles_text="[1] Art\nBody\nSource: url",
        language="zh", run_at="2026-06-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert isinstance(result, tuple)
    corrected, corrections = result
    assert corrected == "clean digest"
    assert corrections == []


@pytest.mark.asyncio
async def test_run_review_gate_bad_json_returns_tuple(prompts_dir):
    """Bad JSON → fail-open returns (str, []) tuple."""
    llm = _make_llm(["not valid json at all"])
    result = await run_review_gate(
        llm, intent_id=1, summary_raw="digest",
        articles_text="[1] Art\nBody\nSource: url",
        language="zh", run_at="2026-06-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert isinstance(result, tuple)
    corrected, corrections = result
    assert corrected == "digest"
    assert corrections == []


# ---------------------------------------------------------------------------
# T2: build_articles_text_from_citations — all bodies present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_articles_text_from_citations_all_present():
    citations = [
        {"article_id": "550e8400-e29b-41d4-a716-446655440000", "title": "Article One",
         "url": "https://example.com/1", "source": 7},
        {"article_id": "ba2e8400-e29b-41d4-a716-446655440001", "title": "Article Two",
         "url": "https://example.com/2", "source": 8},
    ]
    feed_name_map = {7: "Feed Seven", 8: "Feed Eight"}

    async def body_fetcher(md5_hex: str) -> str:
        return f"Body for {md5_hex}"

    result = await build_articles_text_from_citations(citations, body_fetcher, feed_name_map)
    assert result is not None
    assert "[1] Article One" in result
    assert "Body for 550e8400e29b41d4a716446655440000" in result
    assert "Source: Feed Seven (https://example.com/1)" in result
    assert "[2] Article Two" in result
    assert "Source: Feed Eight (https://example.com/2)" in result
    assert "\n\n" in result  # D6: entry separator


# ---------------------------------------------------------------------------
# T3: build_articles_text_from_citations — strict mode (body missing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_articles_text_from_citations_missing_body():
    citations = [
        {"article_id": "a" * 32, "title": "Article", "url": "https://x.com", "source": 1},
        {"article_id": "b" * 32, "title": "Missing", "url": "", "source": 2},
    ]
    feed_name_map = {1: "Feed A", 2: "Feed B"}
    call_order: list[str] = []

    async def body_fetcher(md5_hex: str) -> str | None:
        call_order.append(md5_hex)
        if md5_hex == "a" * 32:
            return "body A"
        return None

    result = await build_articles_text_from_citations(citations, body_fetcher, feed_name_map)
    assert result is None
    assert len(call_order) == 2  # both attempted


# ---------------------------------------------------------------------------
# T5: body_fetcher receives stripped md5 (no dashes) — D14
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_articles_text_body_fetcher_stripped_md5():
    citations = [
        {"article_id": "550e8400-e29b-41d4-a716-446655440000", "title": "Test",
         "url": "https://x.com", "source": 1},
    ]
    feed_name_map = {1: "Feed One"}
    received: list[str] = []

    async def body_fetcher(md5_hex: str) -> str:
        received.append(md5_hex)
        return "body"

    result = await build_articles_text_from_citations(citations, body_fetcher, feed_name_map)
    assert result is not None
    assert len(received) == 1
    assert received[0] == "550e8400e29b41d4a716446655440000"
    assert "-" not in received[0]


# ---------------------------------------------------------------------------
# T23: empty body string != expired (D6-c)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_articles_text_empty_body_not_expired():
    citations = [
        {"article_id": "c" * 32, "title": "Empty body article",
         "url": "https://x.com", "source": 1},
    ]
    feed_name_map = {1: "Feed"}

    async def body_fetcher(md5_hex: str) -> str | None:
        return ""

    result = await build_articles_text_from_citations(citations, body_fetcher, feed_name_map)
    assert result is not None
    assert "[1] Empty body article" in result


# ---------------------------------------------------------------------------
# D6 source line fallback rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_articles_d6_fallback_no_feed_name():
    """D6(a): feed_name not in map → Source: {url}"""
    citations = [{"article_id": "d" * 32, "title": "T", "url": "https://x.com", "source": 99}]
    feed_name_map = {}  # feed 99 not in map

    async def body_fetcher(md5_hex: str) -> str:
        return "body"

    result = await build_articles_text_from_citations(citations, body_fetcher, feed_name_map)
    assert result is not None
    assert "Source: https://x.com" in result


@pytest.mark.asyncio
async def test_build_articles_d6_fallback_no_url():
    """D6(b): url empty but feed_name present → Source: {feed_name}"""
    citations = [{"article_id": "e" * 32, "title": "T", "url": "", "source": 1}]
    feed_name_map = {1: "My Feed"}

    async def body_fetcher(md5_hex: str) -> str:
        return "body"

    result = await build_articles_text_from_citations(citations, body_fetcher, feed_name_map)
    assert result is not None
    assert "Source: My Feed" in result


@pytest.mark.asyncio
async def test_build_articles_d6_fallback_unknown():
    """D6(c): both feed_name and url missing → Source: (unknown)"""
    citations = [{"article_id": "f" * 32, "title": "T", "url": "", "source": 99}]
    feed_name_map = {}

    async def body_fetcher(md5_hex: str) -> str:
        return "body"

    result = await build_articles_text_from_citations(citations, body_fetcher, feed_name_map)
    assert result is not None
    assert "Source: (unknown)" in result


# ===================================================================
# QA-OWNED TESTS (T4, T7, T11-T13, T15-T17, T19-T22)
# ===================================================================

# ---------------------------------------------------------------------------
# T4: build_articles_text_from_citations — deleted feed (feed_id absent from map)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_articles_text_deleted_feed():
    """T4: feed_id in citation but absent from feed_name_map, url present → Source: {url}."""
    citations = [
        {"article_id": "g" * 32, "title": "Deleted Feed Article",
         "url": "https://deleted.example.com", "source": 999},
    ]
    feed_name_map = {}  # feed 999 was deleted, not in map

    async def body_fetcher(md5_hex: str) -> str:
        return "body text"

    result = await build_articles_text_from_citations(citations, body_fetcher, feed_name_map)
    assert result is not None
    assert "Source: https://deleted.example.com" in result
    assert "Deleted Feed Article" in result


# ---------------------------------------------------------------------------
# T7: update_summary — row not found cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_summary_row_not_found():
    """T7: update_summary returns False when no row exists for the given id."""
    conn = await aiosqlite.connect(":memory:")
    try:
        from sembr.db.sqlite import install_for_test

        install_for_test(conn)
        conn.row_factory = aiosqlite.Row  # set row factory for init to work
        from sembr.db.summary_history import init_summary_history_table, update_summary

        await init_summary_history_table(conn)
        result = await update_summary(conn, intent_id=1, row_id=999, new_summary="new text")
        assert result is False
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_update_summary_wrong_intent():
    """T7 variant: update_summary returns False when row belongs to different intent."""
    conn = await aiosqlite.connect(":memory:")
    try:
        from sembr.db.sqlite import install_for_test

        install_for_test(conn)
        conn.row_factory = aiosqlite.Row
        from sembr.db.summary_history import (
            init_summary_history_table,
            save_summary,
            update_summary,
        )
        from sembr.summarizer.models import Citation, SummaryResult

        await init_summary_history_table(conn)
        # Insert a row for intent 1
        result = SummaryResult(
            intent_id=1,
            summary="test summary",
            citations=[Citation(
                article_id="abc123", title="Test", url="https://x.com",
                source=1, published_at="2026-06-01T00:00:00Z",
            )],
        )
        row_id = await save_summary(conn, result, run_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))

        # Try to update with intent_id=2 (wrong intent)
        updated = await update_summary(conn, intent_id=2, row_id=row_id, new_summary="new")
        assert updated is False
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# HTTP endpoint helpers (follow test_history_api.py pattern)
# ---------------------------------------------------------------------------


def _cron_intent(intent_id: int = 1):
    from sembr.models import CronSchedule, Intent

    return Intent(
        id=intent_id,
        name="review-test-intent",
        text="review endpoint test",
        threshold=0.75,
        enabled=True,
        channels=[],
        tags=[],
        schedule=CronSchedule(preset="daily", hour=9, minute=0, lookback_seconds=86400),
        feed_filter=None,
        timezone="UTC",
        language="zh",
        review_gate=False,
        system_template="default",
        instruction_template="default",
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


def _make_app():
    from fastapi import FastAPI

    from sembr.api.history import router

    app = FastAPI()
    app.include_router(router)
    app.state.qdrant = MagicMock()
    app.state.qdrant.client = MagicMock()
    app.state.llm_backend = MagicMock()
    app.state.llm_backend.max_prompt_chars = 2_000_000
    app.state.scheduler = MagicMock()
    app.state.summary_pipeline = MagicMock()
    return app


def _target_row(summary: str = "test summary", citations: list[dict] | None = None):
    if citations is None:
        citations = [
            {"article_id": "a" * 32, "title": "Article A", "url": "https://a.com", "source": 1}
        ]
    return {
        "id": 1,
        "intent_id": 1,
        "run_at": "2026-06-01T00:00:00Z",
        "summary": summary,
        "citations": citations,
    }


# ---------------------------------------------------------------------------
# T11: POST review — history row not found
# ---------------------------------------------------------------------------


def test_review_endpoint_row_not_found():
    """T11: POST /review on non-existent history row → 404."""
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.get_summary_by_id", new=AsyncMock(return_value=None)),
    ):
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/history/999/review")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# T12: POST review — intent not found
# ---------------------------------------------------------------------------


def test_review_endpoint_intent_not_found():
    """T12: POST /review on non-existent intent → 404."""
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=None)),
    ):
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/999/history/1/review")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# T13: POST review — budget exceeded → 422 digest_too_long
# ---------------------------------------------------------------------------


def test_review_endpoint_budget_exceeded():
    """T13: digest too long → 422 with code=digest_too_long."""
    app = _make_app()
    app.state.llm_backend.max_prompt_chars = 10  # very low → budget exceeded

    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.get_summary_by_id", new=AsyncMock(return_value=_target_row())),
        patch("sembr.api.history.db_get_feed_names", new=AsyncMock(return_value={1: "Feed"})),
        patch(
            "sembr.summarizer.review.build_articles_text_from_citations",
            new=AsyncMock(return_value="articles for review"),
        ),
        patch("sembr.summarizer.templates.render_system", return_value="system " * 100),
        patch("sembr.summarizer.templates.load_template", return_value="instruction template"),
        patch(
            "sembr.summarizer.templates.render_instruction_from_raw",
            return_value="user prompt " * 100,
        ),
    ):
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/history/1/review")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "digest_too_long"


# ---------------------------------------------------------------------------
# T15: PATCH history — row not found
# ---------------------------------------------------------------------------


def test_patch_history_row_not_found():
    """T15: PATCH non-existent history row → 404."""
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.update_summary", new=AsyncMock(return_value=False)),
    ):
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.patch("/intents/1/history/999", json={"summary": "new text"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# T16: PATCH history — wrong intent (update_summary scoped to intent_id)
# ---------------------------------------------------------------------------


def test_patch_history_row_wrong_intent():
    """T16: PATCH history row belonging to different intent → 404."""
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent(intent_id=2))),
        patch("sembr.api.history.update_summary", new=AsyncMock(return_value=False)),
    ):
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        # Intent exists (id=2), but history row belongs to intent 1 → update_summary returns False
        resp = client.patch("/intents/2/history/1", json={"summary": "new text"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# T17: review idempotent — same row reviewed twice
# ---------------------------------------------------------------------------


def test_review_endpoint_idempotent():
    """T17: reviewing same row twice yields same run_review_gate call signature.

    The first review calls run_review_gate with the original summary.
    The second review (simulating a non-PATCH'd scenario) also calls
    with the same original because get_summary_by_id returns unchanged data.
    """
    app = _make_app()
    run_review_mock = AsyncMock(return_value=("corrected summary", [{"error_class": "factual",
                                                                      "before": "bad", "after": "good",
                                                                      "matched": True}]))
    row = _target_row(summary="original digest text")

    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.get_summary_by_id", new=AsyncMock(return_value=row)),
        patch("sembr.api.history.db_get_feed_names", new=AsyncMock(return_value={1: "Feed"})),
        patch(
            "sembr.summarizer.review.build_articles_text_from_citations",
            new=AsyncMock(return_value="articles for review"),
        ),
        patch("sembr.summarizer.review.run_review_gate", new=run_review_mock),
        patch("sembr.summarizer.templates.render_system", return_value="review system prompt"),
        patch("sembr.summarizer.templates.load_template", return_value="instruction template"),
        patch(
            "sembr.summarizer.templates.render_instruction_from_raw",
            return_value="review user prompt",
        ),
    ):
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)

        # First review
        resp1 = client.post("/intents/1/history/1/review")
        assert resp1.status_code == 200
        assert run_review_mock.call_count == 1
        call_args1 = run_review_mock.await_args
        assert call_args1 is not None
        _, kwargs1 = call_args1
        # run_review_gate receives summary_raw as positional or keyword?
        # Signature: async def run_review_gate(llm, intent_id, summary_raw, articles_text, language, run_at, prompts_dir=None)
        # The endpoint calls: await run_review_gate(llm, intent_id, summary_raw, articles_text, intent.language, run_at)
        # These are positional args
        assert call_args1[0][0] is app.state.llm_backend  # llm
        assert call_args1[0][2] == "original digest text"  # summary_raw

        # Second review (get_summary_by_id still returns same row)
        resp2 = client.post("/intents/1/history/1/review")
        assert resp2.status_code == 200
        assert run_review_mock.call_count == 2
        call_args2 = run_review_mock.await_args_list[1]
        assert call_args2[0][2] == "original digest text"  # same summary_raw


# ===================================================================
# UI STATIC TESTS (T19-T22)
# ===================================================================

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INDEX_HTML = _REPO_ROOT / "web" / "static" / "index.html"
_INTENTS_JS = _REPO_ROOT / "web" / "static" / "intents.js"


@pytest.fixture(scope="module")
def index_html() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def intents_js() -> str:
    return _INTENTS_JS.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T19: Review button positioned between View and Delete
# ---------------------------------------------------------------------------


def test_review_button_position(index_html: str):
    """T19: Review button must appear between View and Delete in the history actions column.

    Expected order: View → Review → Delete (all in the same <td>).
    """
    td_start = index_html.find('<button @click="openReviewConfirm')
    assert td_start > 0, "Review button not found in index.html"

    # Find the section containing history action buttons
    cron_marker = "── Cron sub-tab ──"
    cron_start = index_html.find(cron_marker)
    assert cron_start > 0

    # The history actions are in the expanded history table
    # Check View button exists before Review in the same scope
    view_pos = index_html.find('<button @click="openHistoryView', cron_start)
    review_pos = index_html.find('<button @click="openReviewConfirm', cron_start)
    delete_pos = index_html.find('class="danger" @click="confirmDeleteHistory', cron_start)
    assert view_pos > 0, "View button not found"
    assert review_pos > 0, "Review button not found"
    assert delete_pos > 0, "Delete button not found"
    assert view_pos < review_pos < delete_pos, (
        f"Expected View < Review < Delete; got View={view_pos}, Review={review_pos}, Delete={delete_pos}"
    )


# ---------------------------------------------------------------------------
# T20: 0 corrections — toast only, no modal
# ---------------------------------------------------------------------------


def test_review_zero_corrections_toast(intents_js: str):
    """T20: 0 corrections triggers toast 'Review passed — no issues found' only."""
    assert "showToast('Review passed — no issues found', 'info')" in intents_js, (
        "Missing zero-corrections toast in runReview"
    )
    # Verify it's inside the corrections.length === 0 branch
    assert "if (!data.corrections || data.corrections.length === 0)" in intents_js, (
        "Zero-corrections branch not found in runReview"
    )


# ---------------------------------------------------------------------------
# T21: Comparison modal — left/right columns + corrections detail table
# ---------------------------------------------------------------------------


def test_review_comparison_modal_structure(index_html: str):
    """T21: Review comparison modal must have:
    - Left column (Original) and right column (Corrected) with rendered markdown
    - Corrections detail table with Error class / Before / After columns
    - Apply button
    """
    # Find the comparison modal section — search within the modal div
    modal_start = index_html.find("Review comparison modal")
    assert modal_start > 0, "Comparison modal marker not found"
    # The full modal is ~5500 chars; use generous buffer
    section = index_html[modal_start : modal_start + 6000]

    # Left column (Original)
    assert "Original" in section, "Missing 'Original' column header"
    # Right column (Corrected)
    assert "Corrected" in section, "Missing 'Corrected' column header"
    # Grid layout
    assert "grid-template-columns:1fr 1fr" in section, "Missing two-column grid layout"
    # Corrections detail table
    assert "Error class" in section, "Missing Error class column"
    assert "Before" in section, "Missing Before column"
    assert "After" in section, "Missing After column"
    # Apply button
    assert "Apply corrections" in section or "applyReview" in section, (
        "Missing Apply corrections button"
    )


def test_review_comparison_modal_markdown_rendering(intents_js: str):
    """T21 (cont): Comparison modal must render markdown via _renderMarkdown for both columns."""
    assert "this._renderMarkdown(data.original)" in intents_js, (
        "Missing originalHtml markdown rendering"
    )
    assert "this._renderMarkdown(data.corrected)" in intents_js, (
        "Missing correctedHtml markdown rendering"
    )
    assert "reviewCompare" in intents_js, "Missing reviewCompare state"


# ---------------------------------------------------------------------------
# T22: Apply corrections — history list refresh
# ---------------------------------------------------------------------------


def test_apply_refreshes_history_list(intents_js: str):
    """T22: Apply corrections (PATCH) must trigger history list refresh via _loadHistoryPage."""
    apply_section_start = intents_js.find("async applyReview()")
    assert apply_section_start > 0, "applyReview function not found"
    section = intents_js[apply_section_start : apply_section_start + 2000]

    # Must call PATCH endpoint (JS uses single quotes: 'PATCH')
    assert "'PATCH'" in section, "Missing PATCH verb in applyReview"
    # Must refresh history list
    assert "_loadHistoryPage(intentId)" in section, (
        "Missing history list refresh after PATCH"
    )
    # Must reset the expanded state before refresh
    assert "expanded.offset = 0" in section, "Missing offset reset"
    assert "expanded.rows = []" in section, "Missing rows reset"
    assert "expanded.loading = true" in section, "Missing loading state"
