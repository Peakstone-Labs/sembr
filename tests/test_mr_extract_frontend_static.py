# SPDX-License-Identifier: Apache-2.0
"""Static assertions for the map sub-feature frontend (intents.js + index.html).

File-content greps — fast, deterministic, browser-free. Guards the wiring that
the source-extraction button, in-place expand (tbody-per-citation), and the
async gen-guard depend on.
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


# --------------------------------------------------------------------------- #
# New methods exist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method",
    [
        "startExtractSources",
        "_pollExtractSources",
        "loadCitationExtraction",
        "toggleCitationExpand",
        "extractionSections",
        "claimContentTags",
        "claimMetaTags",
        "claimFlags",
        "extractionHeader",
    ],
)
def test_methods_present(intents_js: str, method: str) -> None:
    assert re.search(rf"\b{re.escape(method)}\s*\(", intents_js), f"missing {method!r}"


def test_render_is_spec_driven_not_fed_watch_coupled(intents_js: str) -> None:
    """The renderer must hard-code NO fed_watch field names — roles/labels come
    from the spec (field_meta). This is the whole point of the dynamic-spec design."""
    for hardcoded in (
        "is_projection",
        "source_type",
        "attribution",
        "single_source",
        "regime_signal",
        "original_en",
    ):
        assert (
            hardcoded not in intents_js
        ), f"{hardcoded!r} is hard-coded in intents.js — must be driven by spec field_meta"
    # roles/labels are consulted from the spec map served by the endpoint
    assert "field_meta" in intents_js
    assert "fieldMeta" in intents_js
    assert "_roleOf(" in intents_js and "_labelOf(" in intents_js


def test_content_meta_split_and_flag_badges(index_html: str) -> None:
    # content and provenance render as two separate lines (provenance subordinate)
    assert "claimContentTags(claim)" in index_html
    assert "claimMetaTags(claim)" in index_html
    # flags surfaced as badges, label comes from the spec (x-text, not a literal)
    assert "claimFlags(claim)" in index_html
    assert 'x-text="flag"' in index_html


def test_metrics_render_handles_bare_string(intents_js: str) -> None:
    # Metrics can arrive as a bare string instead of {name,value}; the shape-based
    # formatter must handle both so a string metric isn't silently dropped.
    start = intents_js.find("_formatField(k, v)")
    blob = intents_js[start : start + 700]
    assert "typeof m === 'string'" in blob

    # regime_signal-style object fields render generically by shape, no ↑ glyph
    assert "↑" not in intents_js


def test_na_sentinel_filtered(intents_js: str) -> None:
    # The "na" not-applicable sentinel must be treated as empty so "Stance: na"
    # noise doesn't render (consulted via _blank in the content/meta loops).
    assert "_blank(" in intents_js
    assert "v === 'na'" in intents_js


def test_tag_value_dup_of_quote_suppressed(intents_js: str) -> None:
    # A tag field whose value just repeats the quote (e.g. original_en on an
    # English source) must be suppressed so the same sentence isn't shown twice.
    assert "_dupOfQuote(" in intents_js
    assert intents_js.count("_dupOfQuote(v, quote)") >= 2  # used in content + meta loops


# --------------------------------------------------------------------------- #
# Endpoint URLs wired (all under /api/* for the 401 contract)
# --------------------------------------------------------------------------- #
def test_endpoint_urls_wired(intents_js: str) -> None:
    assert "/history/${rowId}/extract-sources?override=" in intents_js
    assert "/extract-sources/${taskId}" in intents_js
    assert "/extractions/${encodeURIComponent(c.article_id" in intents_js
    # all three live under /api/intents/ (auth → 401 not 302)
    assert intents_js.count("/api/intents/") >= 3


def test_override_flag_threaded(intents_js: str) -> None:
    assert "ex.override ? 'true' : 'false'" in intents_js


# --------------------------------------------------------------------------- #
# Async gen-guard (Alpine-modal stale-result drop)
# --------------------------------------------------------------------------- #
def test_gen_guard_present(intents_js: str) -> None:
    assert "_hvGen" in intents_js
    # bumped on open and close
    assert intents_js.count("this._hvGen++") >= 2
    # guarded after awaits
    assert intents_js.count("gen !== this._hvGen") >= 4


def test_poll_uses_setTimeout_not_setInterval(intents_js: str) -> None:
    # Re-arm via setTimeout so polls never overlap (no setInterval in poll).
    poll_start = intents_js.find("_pollExtractSources")
    assert poll_start > 0
    poll_blob = intents_js[poll_start : poll_start + 1600]
    assert "setTimeout(" in poll_blob
    assert "setInterval(" not in poll_blob  # the call, not the word in a comment


# --------------------------------------------------------------------------- #
# index.html: control bar + in-place expand (tbody per citation)
# --------------------------------------------------------------------------- #
def test_extract_button_and_override_checkbox(index_html: str) -> None:
    assert "startExtractSources()" in index_html
    assert 'x-model="historyView.extract.override"' in index_html
    assert "Extract facts" in index_html  # button label (English UI)


def test_extract_ui_strings_are_english(index_html: str, intents_js: str) -> None:
    """No CJK in the map-feature UI chrome — the dashboard is an English UI.

    Scoped to the sources-extraction block + the methods that set user-facing
    strings, so the pre-existing aggregate-prompt CJK defaults aren't flagged.
    """
    import re

    start = index_html.find('x-model="historyView.extract.override"')
    end = index_html.find("<!-- ── Backfill modal")
    block = index_html[start:end]
    assert not re.search(r"[一-鿿]", block), "sources-extraction HTML must be English-only"
    for needle in (
        "Not extracted — use Extract facts above",
        "Source expired",
        "An extraction is already running",
        "(Unknown publisher)",
    ):
        assert needle in intents_js, f"expected English string {needle!r} in intents.js"


def test_inplace_expand_tbody_per_citation(index_html: str) -> None:
    """The citations x-for must wrap each row in its own <tbody> with the
    expand <tr> as a sibling (so it opens directly under the row, not at the
    list bottom). Mirrors the feeds-tab tbody-per-iteration pattern."""
    # Slice the historyView citations block.
    start = index_html.find('x-for="(c, idx) in historyView.citations"')
    assert start > 0, "citations x-for not found"
    blob = index_html[start : start + 1200]
    # template → tbody → article tr → expand tr(x-show=expandOpen), all in order
    i_tbody = blob.find("<tbody>")
    i_expand = blob.find('x-show="c.expandOpen"')
    assert 0 < i_tbody < i_expand, "expand row must be a sibling tr inside the per-citation tbody"
    assert "toggleCitationExpand(c)" in blob


def test_old_bottom_stacked_body_list_removed(index_html: str, intents_js: str) -> None:
    # The previous design stacked bodies below the table via :key="'body-' + idx"
    # and toggled c.bodyOpen — both must be gone after the in-place refactor.
    assert "'body-' + idx" not in index_html
    assert "bodyOpen" not in index_html
    assert "bodyOpen" not in intents_js


def test_cache_buster_bumped(index_html: str) -> None:
    m = re.search(r"intents\.js\?v=(\d+)", index_html)
    assert m is not None
    assert int(m.group(1)) >= 17, f"expected intents.js?v>=17; got v={m.group(1)}"


# --------------------------------------------------------------------------- #
# settings.js: LLM section rename + reduce/meta/concurrency grouped under it
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def settings_js() -> str:
    return (_REPO_ROOT / "web" / "static" / "settings.js").read_text(encoding="utf-8")


def test_llm_section_renamed_and_groups_reduce_fields(settings_js: str) -> None:
    assert "'LLM Summarizer'" not in settings_js  # renamed
    assert "'LLM Settings'" in settings_js
    # the three map-reduce fields are pulled into the LLM section via exact match
    for key in ("REDUCE_MODEL", "META_EXTRACTION_MODEL", "REDUCE_CONCURRENCY"):
        assert key in settings_js, f"{key} must be listed in settings.js LLM section exact[]"


def test_settings_js_cache_buster_bumped(index_html: str) -> None:
    m = re.search(r"settings\.js\?v=(\d+)", index_html)
    assert m is not None
    assert int(m.group(1)) >= 13, f"expected settings.js?v>=13; got v={m.group(1)}"


# --------------------------------------------------------------------------- #
# spec-autogen (Advanced extraction-spec panel) — T14/T15/T16
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method",
    [
        "openAdvanced",
        "closeAdvanced",
        "generateSpec",
        "saveSpec",
        "enableSpec",
        "prettifyJson",
        "highlightJson",
        "_validateJsonLocal",
        "_escapeHtml",
    ],
)
def test_advanced_methods_present(intents_js: str, method: str) -> None:
    assert re.search(rf"\b{re.escape(method)}\s*\(", intents_js), f"missing {method!r}"


def test_advanced_button_and_modal_wired(index_html: str) -> None:
    assert "openAdvanced(intent)" in index_html  # entry point on the cron row
    assert 'x-show="advanced.open"' in index_html  # modal present
    # separate save / enable actions (not one combined button)
    assert "saveSpec()" in index_html and "enableSpec()" in index_html


def test_highlight_escapes_before_tokenizing(intents_js: str) -> None:
    # XSS guard: highlightJson must escape the whole text first, then tokenize on
    # the escaped quote delimiter (&quot;). Asserts the escape-first contract.
    m = re.search(r"highlightJson\([^)]*\)\s*\{(.*?)\n    \},", intents_js, re.S)
    assert m, "highlightJson body not found"
    body = m.group(1)
    assert "_escapeHtml(text)" in body, "highlightJson must escape the raw text first"
    assert "&quot;" in body, "tokenizer must match on the escaped quote delimiter"


def test_intents_js_cache_buster_bumped_for_spec_autogen(index_html: str) -> None:
    m = re.search(r"intents\.js\?v=(\d+)", index_html)
    assert m is not None and int(m.group(1)) >= 23, f"expected intents.js?v>=23; got v={m.group(1)}"


def test_style_css_cache_buster_bumped(index_html: str) -> None:
    m = re.search(r"style\.css\?v=(\d+)", index_html)
    assert m is not None and int(m.group(1)) >= 13, f"expected style.css?v>=13; got v={m.group(1)}"


def test_frontend_json_validation_is_drift_guard_only(intents_js: str) -> None:
    # The frontend must NOT mirror the backend's 12-rule set (drift-guard): the
    # authoritative validator lives server-side. Backend-only rule messages must
    # not appear in intents.js.
    for backend_only in ("保留 shell 名", "role 非法", "section key 缺失", "enum 类型须给取值"):
        assert (
            backend_only not in intents_js
        ), f"{backend_only!r} duplicates a backend rule in intents.js — drift risk"
