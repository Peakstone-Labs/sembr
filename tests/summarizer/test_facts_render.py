# SPDX-License-Identifier: Apache-2.0
"""T1 (design §6): render_facts — VQP form renders quote, [N] aligns with recall
order, claims bucket by spec section, preamble prepended, no-content articles stay
in the article list, and the de-quote fallback swaps the preamble consistently.
"""

from __future__ import annotations

from sembr.summarizer.facts_render import (
    PREAMBLE_V2,
    PREAMBLE_V2_NOQUOTE,
    render_facts,
)
from sembr.summarizer.spec import GeneratedSpec

_SPEC = GeneratedSpec(
    name="intent-1",
    extraction_prompt="x",
    sections=[{"key": "facts", "label": "事实"}, {"key": "impact", "label": "影响"}],
    schema_version="v1",
)


def _records() -> list[dict]:
    # Given out of index order on purpose — render must sort by index.
    return [
        {
            "index": 2,
            "source_org": "Reuters",
            "source_name": "reuters-feed",
            "published_at": "2026-06-02",
            "thesis": "second thesis",
            "claims": [
                {"section": "impact", "text": "yields rose", "quote": "yields rose 10bp"},
            ],
        },
        {
            "index": 1,
            "source_org": "Fed",
            "published_at": "2026-06-01",
            "thesis": "first thesis",
            "claims": [
                {
                    "section": "facts",
                    "text": "held rates",
                    "quote": "held at 5.25%",
                    "is_projection": True,
                },
            ],
        },
        # no-content / failed article: listed in the article list, no facts/thesis.
        {
            "index": 3,
            "source_name": "x-feed",
            "published_at": "2026-06-03",
            "no_relevant_content": True,
            "claims": [],
        },
    ]


def test_vqp_renders_quote_and_preamble() -> None:
    out = render_facts(_records(), _SPEC)
    assert out.startswith(PREAMBLE_V2)
    assert '〔原文: "held at 5.25%"〕' in out
    assert '〔原文: "yields rose 10bp"〕' in out
    # claim extras (spec fields not in the reserved shell) render as a tag
    assert "is_projection=True" in out


def test_index_aligns_with_recall_order() -> None:
    out = render_facts(_records(), _SPEC)
    # article list sorted by index ascending → [1] before [2] before [3]
    assert out.index("[1] Fed") < out.index("[2] Reuters") < out.index("[3] x-feed")
    # claims carry their own [N]
    assert "- [1] " in out
    assert "- [2] " in out


def test_sections_bucketed_by_spec_label() -> None:
    out = render_facts(_records(), _SPEC)
    assert "### 事实" in out  # facts section label
    assert "### 影响" in out  # impact section label
    # facts section comes before impact (spec section order)
    assert out.index("### 事实") < out.index("### 影响")


def test_no_content_article_listed_but_no_claims() -> None:
    out = render_facts(_records(), _SPEC)
    assert "[3] x-feed · 2026-06-03" in out  # in the article list
    assert "- [3] " not in out  # but contributes no claim line


def test_thesis_block_present_for_articles_with_thesis() -> None:
    out = render_facts(_records(), _SPEC)
    assert "[1] Fed: first thesis" in out
    assert "[2] Reuters: second thesis" in out


def test_dequote_fallback_drops_quote_and_swaps_preamble() -> None:
    out = render_facts(_records(), _SPEC, include_quote=False, preamble=PREAMBLE_V2_NOQUOTE)
    assert "〔原文:" not in out
    assert out.startswith(PREAMBLE_V2_NOQUOTE)
    # the language guard (quote-only) is gone, but the delta-label guard stays
    assert "语言纪律" not in out
    assert "增量标签以 history 为准" in out
    # claims still render (just without the quote line)
    assert "- [1] " in out
    assert "held rates" in out


def test_source_name_fallback_when_no_source_org() -> None:
    # article 3 has only source_name → article list uses it as the org fallback
    out = render_facts(_records(), _SPEC)
    assert "[3] x-feed" in out


def test_numeric_zero_claim_field_is_not_dropped() -> None:
    """🟡-2: a numeric 0/0.0 extra must render (not eaten by 0 == False)."""
    records = [
        {
            "index": 1,
            "source_org": "Fed",
            "published_at": "2026-06-01",
            "claims": [
                {"section": "facts", "text": "no change", "bps_change": 0, "pct": 0.0},
            ],
        }
    ]
    out = render_facts(records, _SPEC)
    assert "bps_change=0" in out
    assert "pct=0.0" in out


def test_false_flag_and_empty_values_still_dropped() -> None:
    """_is_empty still omits None / '' / [] / {} and a bool False flag."""
    records = [
        {
            "index": 1,
            "source_org": "Fed",
            "published_at": "2026-06-01",
            "claims": [
                {
                    "section": "facts",
                    "text": "t",
                    "flag": False,
                    "blank": "",
                    "none_v": None,
                    "empty_list": [],
                    "kept": "yes",
                },
            ],
        }
    ]
    out = render_facts(records, _SPEC)
    assert "flag=" not in out
    assert "blank=" not in out
    assert "none_v=" not in out
    assert "empty_list=" not in out
    assert "kept=yes" in out
