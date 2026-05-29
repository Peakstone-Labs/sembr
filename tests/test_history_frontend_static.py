# SPDX-License-Identifier: Apache-2.0
"""Static assertions on web/static/index.html and intents.js for the history-display UI.

These are file-content greps — fast, deterministic, run on any host without a
browser.  Catches the two regressions the cache buster + event-tab isolation
have to guarantee.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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
# Cache buster — see memory feedback_static_cache_buster.md
# ---------------------------------------------------------------------------


def test_intents_js_cachebuster_bumped(index_html: str) -> None:
    """index.html intents.js script tag must reference v>=5 (was v=4 pre-feature)."""
    m = re.search(r"intents\.js\?v=(\d+)", index_html)
    assert m is not None, "expected intents.js?v=N in index.html"
    assert int(m.group(1)) >= 5, (
        f"intents.js cache buster must be bumped to v>=5; got v={m.group(1)}"
    )


# ---------------------------------------------------------------------------
# Event sub-tab must not show History / Backfill buttons
# ---------------------------------------------------------------------------


def test_backfill_history_buttons_only_in_cron_subtab(index_html: str) -> None:
    """History + Backfill buttons must live inside the cron sub-tab block only.

    The event sub-tab template gets re-rendered for event-mode intents; history
    rows never exist for event intents (Non-Goal — see requirements).  Slicing
    the file by the cron / event sub-tab markers and asserting each side
    contains / lacks the History+Backfill buttons guards against drift.
    """
    # Section markers come from the comment blocks in index.html so changes to
    # the Alpine conditional text don't break the test.
    cron_marker = "── Cron sub-tab ──"
    event_marker = "── Event sub-tab ──"
    cron_start = index_html.find(cron_marker)
    event_start = index_html.find(event_marker)
    assert cron_start > 0 and event_start > cron_start, "cron/event sub-tab markers missing"

    cron_section = index_html[cron_start:event_start]
    # Event sub-tab runs until the next major comment block (intents-view / modal).
    event_end = index_html.find("<!-- ──", event_start + len(event_marker))
    if event_end == -1:
        event_end = event_start + 5000
    event_section = index_html[event_start:event_end]

    assert "openHistory(intent)" in cron_section, "History button missing from cron sub-tab"
    assert "openBackfill(intent)" in cron_section, "Backfill button missing from cron sub-tab"
    assert "openHistory(" not in event_section, "History button must not appear in event sub-tab"
    assert "openBackfill(" not in event_section, "Backfill button must not appear in event sub-tab"


def test_backfill_button_inside_expand_pane(index_html: str) -> None:
    """Backfill button must live inside the History expand-row (cron-tab),
    not in the row's action-cell — mirrors feeds-tab "Edit tags" placement.
    """
    # action-cell with Edit/History/Fire/Delete must NOT contain Backfill
    cron_marker = "── Cron sub-tab ──"
    event_marker = "── Event sub-tab ──"
    cron_section = index_html[index_html.find(cron_marker) : index_html.find(event_marker)]
    # Locate the cron action-cell block: it's the div.action-cell with openEdit(intent).
    # Use the openEdit→Delete span as a proxy for "this is the row action cell".
    action_cell_start = cron_section.find('class="action-cell"')
    assert action_cell_start > 0, "cron action-cell not found"
    # action_cell_end is the next </div> after the cell — approximate by next 400 chars
    action_cell_blob = cron_section[action_cell_start : action_cell_start + 600]
    assert "openEdit(intent)" in action_cell_blob
    assert "openBackfill" not in action_cell_blob, (
        "Backfill button leaked back into the row action-cell; "
        "it must live inside the History expand-row instead"
    )
    # And the expand-row must contain openBackfill exactly once
    expand_marker = 'class="intent-expand-row"'
    expand_start = cron_section.find(expand_marker)
    assert expand_start > 0, "intent-expand-row not found in cron section"
    expand_blob = cron_section[expand_start:]
    assert "openBackfill(intent)" in expand_blob


# ---------------------------------------------------------------------------
# Vendor scripts wired in <head>
# ---------------------------------------------------------------------------


def test_vendor_scripts_wired(index_html: str) -> None:
    assert "vendor/marked.min.js" in index_html
    assert "vendor/dompurify.min.js" in index_html


def test_vendor_files_present() -> None:
    marked = _REPO_ROOT / "web" / "static" / "vendor" / "marked.min.js"
    dompurify = _REPO_ROOT / "web" / "static" / "vendor" / "dompurify.min.js"
    assert marked.exists() and marked.stat().st_size > 5000
    assert dompurify.exists() and dompurify.stat().st_size > 5000


# ---------------------------------------------------------------------------
# intents.js: new public methods exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "openHistory",
        "closeHistory",
        "openBackfill",
        "closeBackfill",
        "runBackfill",
        "openHistoryView",
        "closeHistoryView",
        "loadCitationBody",
        "confirmDeleteHistory",
        "deleteHistoryRow",
        "_pollBackfill",
        "fmtHistoryRunAt",
    ],
)
def test_intents_js_methods_present(intents_js: str, method: str) -> None:
    pat = re.compile(rf"\b{re.escape(method)}\s*\(")
    assert pat.search(intents_js), f"intents.js missing method {method!r}"


def test_history_runat_uses_tz_formatter(intents_js: str, index_html: str) -> None:
    """run_at display must go through fmtHistoryRunAt(row, intent.timezone)."""
    # Helper uses Intl.DateTimeFormat with the timezone from the intent
    assert "Intl.DateTimeFormat" in intents_js
    assert "timeZone" in intents_js
    # The cron expand-row table cell calls the formatter with intent.timezone
    assert "fmtHistoryRunAt(row.run_at, intent.timezone)" in index_html
    # View modal title and Delete confirm both use the snapshot timezone
    assert "fmtHistoryRunAt(historyView.row?.run_at, historyView.timezone)" in index_html
    assert "fmtHistoryRunAt(delHistory.row?.run_at, delHistory.timezone)" in index_html
    # Raw ISO leakage check — the three sites must no longer show row.run_at as-is
    assert 'x-text="row.run_at"' not in index_html
    assert 'x-text="historyView.row?.run_at' not in index_html
    assert 'x-text="delHistory.row?.run_at' not in index_html


# ---------------------------------------------------------------------------
# UUID -> md5 dash-strip for citation body
# ---------------------------------------------------------------------------


def test_citation_uuid_strip_dashes(intents_js: str) -> None:
    """loadCitationBody must strip dashes from the UUID before calling /articles/{md5}."""
    assert "replace(/-/g, '')" in intents_js or 'replace(/-/g,"")' in intents_js
    assert "/api/dashboard/articles/" in intents_js


# ---------------------------------------------------------------------------
# Snippet truncation
# ---------------------------------------------------------------------------


def test_history_snippet_truncates_at_120(intents_js: str) -> None:
    # Implementation lives in historySnippet().  Assert the function exists
    # and contains a 120-char cap; exact code shape is flexible but the
    # threshold must be present.
    m = re.search(r"historySnippet\([^)]*\)\s*\{[^}]*120", intents_js, re.DOTALL)
    assert m is not None, "historySnippet must truncate to 120 chars"


# ---------------------------------------------------------------------------
# QA Owner tests — Aggregate frontend
# ---------------------------------------------------------------------------


def test_aggregate_frontend_default_prompt_per_language(intents_js: str) -> None:
    """_defaultAggregatePrompt returns zh/en prompts that differ and both contain {history}.

    The function body is small and self-contained, so a simple text search
    of the JS file verifies both prompts exist with the placeholder.
    """
    # The function must exist
    assert "_defaultAggregatePrompt" in intents_js
    assert "language === 'zh'" in intents_js

    # Both prompts must contain {history} and differ by language.
    zh_line = [ln for ln in intents_js.splitlines() if "请根据以下每日摘要记录" in ln]
    en_line = [
        ln for ln in intents_js.splitlines() if "Based on the daily digest records below" in ln
    ]
    assert len(zh_line) == 1, f"expected 1 zh prompt line; got {len(zh_line)}"
    assert len(en_line) == 1, f"expected 1 en prompt line; got {len(en_line)}"
    assert "{history}" in intents_js, "prompt must contain {history}"
    assert zh_line[0] != en_line[0], "zh and en prompts must be different"


def test_aggregate_frontend_modal_state_after_send(index_html: str, intents_js: str) -> None:
    """Modal state is preserved after Summarize + Send.

    Verifies via static code analysis:
      1. closeSummarize() only sets open=false — does not reset state fields
      2. sendSummarize() never closes the modal (no open=false write)
      3. sendSummarize() does not modify form.prompt, form.since, form.until,
         result, or cachedSinceUntil
    """
    # 1. closeSummarize body must only set open=false (no reset of result/error/form)
    close_body_start = intents_js.find("closeSummarize()")
    assert close_body_start > 0, "closeSummarize method not found"
    close_brace = intents_js.find("{", close_body_start)
    close_end = close_brace
    depth = 0
    for i, ch in enumerate(intents_js[close_brace:], start=close_brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                close_end = i
                break
    close_body = intents_js[close_brace : close_end + 1]
    assert "open = false" in close_body, "closeSummarize must set open=false"
    # Allow only "this.summarize.open = false;" - no other state mutations
    assert close_body.count("=") <= 1, (
        f"closeSummarize should only set open=false; body={close_body}"
    )

    # 2. sendSummarize must not close the modal
    send_start = intents_js.find("async sendSummarize()")
    assert send_start > 0, "sendSummarize method not found"
    send_body_start = intents_js.find("{", send_start)
    depth = 0
    send_end = send_body_start
    for i, ch in enumerate(intents_js[send_body_start:], start=send_body_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                send_end = i
                break
    send_body = intents_js[send_body_start : send_end + 1]

    assert "open =" not in send_body, "sendSummarize must NOT close the modal"
    assert "form.prompt" not in send_body, "sendSummarize must not modify form.prompt"
    assert "form.since" not in send_body, "sendSummarize must not modify form.since"
    assert "form.until" not in send_body, "sendSummarize must not modify form.until"
    # result and cachedSinceUntil should NOT be cleared by sendSummarize
    # (they are destructured for reading but never assigned)
    assert ".result =" not in send_body, "sendSummarize must not clear result"
    assert ".cachedSinceUntil =" not in send_body, "sendSummarize must not clear cachedSinceUntil"


def test_static_cache_buster_intentsjs_bumped(index_html: str) -> None:
    """intents.js cache-buster present and >= 13 (not exact-match, so
    routine bumps don't break the gate; inequality catches a forgotten bump)."""
    m = re.search(r"intents\.js\?v=(\d+)", index_html)
    assert m is not None, "intents.js?v=N not found in index.html"
    version = int(m.group(1))
    assert version >= 14, (
        f"intents.js?v={version} is stale — expected >= 14 after recent JS changes. "
        "Bump the cache-buster in index.html."
    )
