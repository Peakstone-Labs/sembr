# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sembr.summarizer.spec — load_spec / compile_validator / extract_one.

Loads the real hand-written ``fed_watch`` spec from the repo ``prompts/`` dir so a
drift between the shipped spec files and the compiler is caught here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sembr.summarizer.llm.base import BaseLLMBackend
from sembr.summarizer.spec import (
    SpecError,
    SpecNotFoundError,
    build_extract_prompt,
    compile_validator,
    extract_one,
    load_spec,
)

_REPO_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


def _legal_extraction() -> dict:
    return {
        "source_org": "德意志银行",
        "thesis": "认为市场加息定价过度",
        "claims": [
            {
                "section": "data_release",
                "text": "5月核心CPI同比2.85%",
                "quote": "核心CPI同比升至2.85%",
                "indicator": "CPI",
                "direction": "beat",
                "stance": "hawkish",
                "source_type": "secondary_cn",
                "is_projection": False,
                "metrics": [{"name": "CPI", "value": "2.85%"}],
                "regime_signal": {"growth": "na", "inflation": "up"},
            }
        ],
    }


# --------------------------------------------------------------------------- #
# load_spec — against the shipped fed_watch files
# --------------------------------------------------------------------------- #
def test_load_fed_watch_spec():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    assert spec.name == "fed_watch"
    assert "抽取" in spec.extraction_prompt  # md content loaded
    assert {s.key for s in spec.sections} == {
        "policy_narrative",
        "official_remark",
        "policy_signal",
        "data_release",
        "financial_condition",
        "global_cb",
    }
    assert len(spec.schema_version) == 16
    assert all(c in "0123456789abcdef" for c in spec.schema_version)


def test_load_spec_schema_version_deterministic_and_content_sensitive(tmp_path: Path):
    d = tmp_path / "extraction"
    d.mkdir()
    (d / "x.md").write_text("prompt A", encoding="utf-8")
    (d / "x.json").write_text('{"sections": []}', encoding="utf-8")
    v1 = load_spec("x", prompts_dir=tmp_path).schema_version
    v1b = load_spec("x", prompts_dir=tmp_path).schema_version
    assert v1 == v1b  # deterministic
    (d / "x.md").write_text("prompt B", encoding="utf-8")  # md edit
    v2 = load_spec("x", prompts_dir=tmp_path).schema_version
    assert v2 != v1  # md change → version drift
    (d / "x.md").write_text("prompt B", encoding="utf-8")
    (d / "x.json").write_text('{"sections": [{"key": "s"}]}', encoding="utf-8")  # json edit
    v3 = load_spec("x", prompts_dir=tmp_path).schema_version
    assert v3 != v2  # json change → version drift


def test_load_spec_missing_half_raises(tmp_path: Path):
    d = tmp_path / "extraction"
    d.mkdir()
    (d / "only.md").write_text("p", encoding="utf-8")  # no .json
    with pytest.raises(SpecNotFoundError):
        load_spec("only", prompts_dir=tmp_path)


def test_load_spec_bad_json_raises(tmp_path: Path):
    d = tmp_path / "extraction"
    d.mkdir()
    (d / "b.md").write_text("p", encoding="utf-8")
    (d / "b.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SpecError):
        load_spec("b", prompts_dir=tmp_path)


def test_load_spec_rejects_path_escape(tmp_path: Path):
    with pytest.raises(ValueError):
        load_spec("../escape", prompts_dir=tmp_path)


# --------------------------------------------------------------------------- #
# compile_validator
# --------------------------------------------------------------------------- #
def test_compile_validator_field_surface():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    assert set(Article.model_fields) == {"no_relevant_content", "source_org", "thesis", "claims"}
    Claim = Article.model_fields["claims"].annotation.__args__[0]
    cf = set(Claim.model_fields)
    # fixed shell
    assert {"section", "text", "quote"} <= cf
    # section-specific fields folded in
    assert {"speaker", "indicator", "direction", "channel", "cb", "signal_kind"} <= cf
    # common claim fields folded in
    assert {"stance", "regime_signal", "metrics", "is_projection"} <= cf


def test_compile_validator_accepts_legal_extraction():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    art = Article.model_validate(_legal_extraction())
    assert art.source_org == "德意志银行"
    assert art.claims[0].metrics == [{"name": "CPI", "value": "2.85%"}]  # array stays list[dict]
    assert art.claims[0].regime_signal == {"growth": "na", "inflation": "up"}  # object stays dict


def test_compile_validator_requires_section_and_text():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    with pytest.raises(ValidationError):
        Article.model_validate({"claims": [{"section": "data_release"}]})  # text missing


def test_claim_field_display_roles_and_labels():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    fm = spec.claim_field_display()
    # shell fields are excluded (renderer handles them structurally)
    assert "section" not in fm and "text" not in fm and "quote" not in fm
    # flag with explicit label
    assert fm["is_projection"] == {"role": "flag", "label": "Projection", "type": "bool"}
    assert fm["single_source"]["role"] == "flag"
    # provenance → meta
    for k in ("source_type", "attribution", "time_ref", "original_en"):
        assert fm[k]["role"] == "meta", f"{k} should be meta"
    # content default + humanized label fallback
    assert fm["indicator"]["role"] == "content"
    assert fm["regime_signal"]["role"] == "content"
    assert fm["market_interpretation"]["label"] == "Market interpretation"  # humanized


def test_humanize_field():
    from sembr.summarizer.spec import _humanize_field

    assert _humanize_field("is_projection") == "Projection"
    assert _humanize_field("single_source") == "Single source"
    assert _humanize_field("cb") == "Cb"


def test_compile_validator_lenient_on_offspec_enum():
    # enum collapses to str so a slightly-off label doesn't fail the whole article.
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    art = Article.model_validate(
        {"claims": [{"section": "x", "text": "t", "stance": "VERY_hawkish_typo"}]}
    )
    assert art.claims[0].stance == "VERY_hawkish_typo"


# --------------------------------------------------------------------------- #
# build_extract_prompt
# --------------------------------------------------------------------------- #
def test_build_extract_prompt_strips_html_and_embeds_schema():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    p = build_extract_prompt(
        Article, title="T", body="<p>Hello <b>world</b></p>", intent_text="Fed 追踪"
    )
    assert "标题：T" in p
    assert "Fed 追踪" in p  # topic block present
    assert "Hello" in p and "world" in p  # HTML tags stripped (emphasis kept as markdown)
    assert "<p>" not in p and "<b>" not in p
    assert '"properties"' in p  # JSON schema embedded


def test_build_extract_prompt_truncates_long_body():
    from sembr.summarizer.spec import _MAX_EXTRACT_BODY_CHARS

    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    body = "字" * (_MAX_EXTRACT_BODY_CHARS + 5_000)
    p = build_extract_prompt(Article, title="T", body=body)
    assert "字" * _MAX_EXTRACT_BODY_CHARS in p
    assert "字" * (_MAX_EXTRACT_BODY_CHARS + 1) not in p  # capped
    assert "用户追踪的主题" not in p  # no topic block when intent_text empty


def test_build_extract_prompt_caps_title():
    from sembr.summarizer.spec import _MAX_TITLE_CHARS

    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    p = build_extract_prompt(Article, title="T" * (_MAX_TITLE_CHARS + 100), body="b")
    assert "T" * _MAX_TITLE_CHARS in p
    assert "T" * (_MAX_TITLE_CHARS + 1) not in p  # title capped too


def test_build_extract_prompt_injects_published_at():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    # present → date line + relative-time instruction injected before the body
    p = build_extract_prompt(Article, title="T", body="b", published_at="2026-06-13")
    assert "本文发布时间：2026-06-13" in p
    assert "time_ref" in p
    assert p.index("本文发布时间") < p.index("正文：")  # appears before the body
    # absent → no date line, no spurious instruction
    p2 = build_extract_prompt(Article, title="T", body="b")
    assert "本文发布时间" not in p2


def test_build_extract_prompt_injects_source_hint():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    p = build_extract_prompt(
        Article,
        title="T",
        body="b",
        url="https://x.com/elerianm/status/123",
        source_name="Twitter·宏观市场",
    )
    assert "https://x.com/elerianm/status/123" in p  # url is the strong signal
    assert "Twitter·宏观市场" in p
    assert "source_org" in p and "宁可留 null" in p  # framed as fallback w/ generic-name guard
    # social post publisher = handle owner; in-body data/link brand ≠ publisher
    assert "账号主" in p and "转引来源" in p
    assert p.index("来源信息") < p.index("正文：")  # before the body
    # absent → no source block
    p2 = build_extract_prompt(Article, title="T", body="b")
    assert "来源信息" not in p2


def test_schema_version_folds_prompt_scaffold_version(monkeypatch):
    # A code-side prompt change (bumping _EXTRACT_PROMPT_VERSION) must change
    # schema_version even though the spec files are untouched — otherwise the
    # cache serves stale extractions under an unchanged hash.
    v1 = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS).schema_version
    monkeypatch.setattr("sembr.summarizer.spec._EXTRACT_PROMPT_VERSION", "999")
    v2 = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS).schema_version
    assert v1 != v2


# --------------------------------------------------------------------------- #
# extract_one — full path through real structured() over a fake chat
# --------------------------------------------------------------------------- #
class _FakeBackend(BaseLLMBackend):
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.last_system: str | None = None
        self.last_model: str | None = None

    @property
    def max_prompt_chars(self) -> int:
        return 10_000

    async def summarize(self, prompt, *, system=None):  # pragma: no cover
        raise NotImplementedError

    async def chat(self, prompt, *, system=None, model=None, json_mode=False):
        self.last_system = system
        self.last_model = model
        return self._reply

    async def health(self):  # pragma: no cover
        return True


async def test_extract_one_returns_validated_and_passes_system_model():
    spec = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
    Article = compile_validator(spec)
    backend = _FakeBackend(json.dumps(_legal_extraction(), ensure_ascii=False))
    out = await extract_one(
        backend, spec, Article, title="T", body="b", model="reduce-x", intent_text="Fed"
    )
    assert out.source_org == "德意志银行"
    assert backend.last_system == spec.extraction_prompt  # spec prompt used as system
    assert backend.last_model == "reduce-x"  # reduce model threaded through
