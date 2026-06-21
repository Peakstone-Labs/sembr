# SPDX-License-Identifier: Apache-2.0
"""Extraction-spec loading, dynamic validator compilation, and per-article map.

An extraction *spec* is a hand-written (later meta-LLM-generated) pair of files
under ``prompts/extraction/``:

- ``{name}.md``   — the extraction prompt (system message; reads naturally as
  markdown, no JSON escaping of the instruction).
- ``{name}.json`` — the *structure*: ``sections`` (1:1 with the analysis
  template's chapters, each with section-specific ``fields``), ``article_fields``
  (carried for the renderer / future spec-gen), and ``common_claim_fields``
  (cross-cutting fields every claim may carry).

``load_spec`` merges the two into a :class:`GeneratedSpec` and stamps a
``schema_version`` = ``sha256(md_bytes + json_bytes)[:16]`` so any edit to either
file invalidates the cache keyed on it. ``compile_validator`` turns
the spec into a dynamic Pydantic model (no per-intent code — mirrors the probe's
``compile_run.compile_models``); ``extract_one`` runs one article through the
backend's structured() with the spec prompt + the model's JSON schema embedded.

The Article shell is fixed (``no_relevant_content`` / ``source_org`` / ``thesis``
/ ``claims[]``); ``article_fields`` are not folded into the validator — the fixed
shell is what the downstream renderer/reduce relies on.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import html2text as _h2t
from pydantic import BaseModel, Field, create_model

from sembr.summarizer.llm.base import BaseLLMBackend
from sembr.summarizer.templates import PROMPTS_DIR, _validate_name

logger = logging.getLogger(__name__)

# Spec files live alongside system/instruction templates under prompts_dir.
_KIND = "extraction"

# Claim keys the compiler owns (fixed shell + index/source metadata the runtime
# attaches) — spec fields colliding with these are ignored, never override them.
_RESERVED = frozenset(
    {"section", "text", "quote", "article_idx", "index", "source_name", "published_at"}
)

# Generous safety bound so a pathologically long body can't blow the provider's
# context; the reduce model's window is large, so normal articles never hit it.
_MAX_EXTRACT_BODY_CHARS = 40_000
# Titles are short in practice; cap anyway so a giant title can't bypass the body cap.
_MAX_TITLE_CHARS = 500

# Version of the code-built extraction prompt scaffolding (`build_extract_prompt`).
# It is NOT part of the spec files, so it is folded into schema_version separately:
# changing the prompt wording here would otherwise leave the cache serving stale
# extractions under an unchanged hash. **Bump this whenever build_extract_prompt's
# assembled wording changes** so existing caches re-extract.
#   1 → original (topic + title + body + schema)
#   2 → + published_at line (anchor relative time_ref to an absolute date)
#   3 → + source hint (url / feed name) for source_org when the body is unsigned
#   4 → social post = handle owner; in-body data-compiler/link brands ≠ publisher
_EXTRACT_PROMPT_VERSION = "4"

# URLs are short; cap defensively so a pathological one can't bloat the prompt.
_MAX_URL_CHARS = 300

_h2t_converter = _h2t.HTML2Text()
_h2t_converter.ignore_links = True
_h2t_converter.ignore_images = True
_h2t_converter.ignore_emphasis = False
_h2t_converter.body_width = 0  # no line wrapping


class SpecError(ValueError):
    """Raised when a spec file is present but malformed (bad JSON / wrong shape)."""


class SpecNotFoundError(FileNotFoundError):
    """Raised when either half (.md / .json) of a spec is missing."""


# --------------------------------------------------------------------------- #
# Spec model
# --------------------------------------------------------------------------- #
def _humanize_field(name: str) -> str:
    """Default display label: drop a leading ``is_``, underscores → spaces, cap first.

    ``is_projection`` → "Projection", ``single_source`` → "Single source".
    """
    s = name[3:] if name.startswith("is_") else name
    s = s.replace("_", " ").strip()
    return (s[:1].upper() + s[1:]) if s else name


class FieldDef(BaseModel):
    name: str
    type: str = "string"  # string / enum / bool / number / array / object
    enum: list[str] = Field(default_factory=list)
    description: str = ""
    # Display-only metadata (does not affect extraction): the dashboard renders
    # generically from these so no field name is hard-coded in the UI.
    #   role  — "content" (what the fact is about; default) / "meta" (provenance,
    #           shown subordinate) / "flag" (boolean surfaced as a badge).
    #   label — display label; falls back to a humanized field name.
    role: str = "content"
    label: str = ""


class SectionDef(BaseModel):
    key: str
    label: str = ""
    fields: list[FieldDef] = Field(default_factory=list)


class GeneratedSpec(BaseModel):
    name: str
    extraction_prompt: str
    sections: list[SectionDef] = Field(default_factory=list)
    article_fields: list[FieldDef] = Field(default_factory=list)
    common_claim_fields: list[FieldDef] = Field(default_factory=list)
    schema_version: str

    def claim_field_display(self) -> dict[str, dict[str, str]]:
        """Claim-field display map: ``name → {role, label, type}``.

        Drives the dashboard's generic renderer end-to-end so the UI hard-codes
        no field names — swapping the spec (different template) automatically
        re-buckets fields and re-labels badges. The fixed claim shell
        (section/text/quote/article_idx) is excluded; the renderer handles those
        structurally. Article-level source_org/thesis are excluded too (rendered
        as the header). First definition of a name wins.
        """
        out: dict[str, dict[str, str]] = {}
        defs: list[FieldDef] = list(self.common_claim_fields)
        for s in self.sections:
            defs.extend(s.fields)
        for f in defs:
            if not f.name or f.name in _RESERVED or f.name in out:
                continue
            out[f.name] = {
                "role": f.role or "content",
                "label": f.label or _humanize_field(f.name),
                "type": f.type or "string",
            }
        return out


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _spec_path(prompts_dir: Path, name: str, ext: str) -> Path:
    """Resolve+validate ``prompts_dir/extraction/{name}.{ext}``; reject path escape.

    Mirrors ``templates.template_path`` so spec names share the identifier rules
    (no '/', '\\', '..', no leading dot) and can't be steered outside prompts_dir.
    """
    _validate_name(name)
    candidate = (prompts_dir / _KIND / f"{name}.{ext}").resolve()
    if not candidate.is_relative_to(prompts_dir.resolve()):
        raise ValueError(f"Spec path {candidate} escapes prompts_dir {prompts_dir}")
    return candidate


def load_spec(name: str, prompts_dir: Path = PROMPTS_DIR) -> GeneratedSpec:
    """Load the ``{name}.md`` + ``{name}.json`` pair into a :class:`GeneratedSpec`.

    Raises:
        ValueError: name fails identifier validation.
        SpecNotFoundError: either file is missing.
        SpecError: the .json is not valid JSON or not an object.
    """
    md_path = _spec_path(prompts_dir, name, "md")
    json_path = _spec_path(prompts_dir, name, "json")
    if not md_path.is_file() or not json_path.is_file():
        raise SpecNotFoundError(
            f"extraction spec '{name}' is incomplete: need both {md_path.name} and "
            f"{json_path.name} under {md_path.parent}"
        )
    md_bytes = md_path.read_bytes()
    json_bytes = json_path.read_bytes()
    try:
        data = json.loads(json_bytes)
    except json.JSONDecodeError as exc:
        raise SpecError(f"spec '{name}' .json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"spec '{name}' .json must be a JSON object at top level")

    # Fold the prompt-scaffold version in so a code-side prompt change (which the
    # spec files don't capture) still invalidates the cache. See _EXTRACT_PROMPT_VERSION.
    schema_version = hashlib.sha256(
        md_bytes + json_bytes + _EXTRACT_PROMPT_VERSION.encode("utf-8")
    ).hexdigest()[:16]
    try:
        return GeneratedSpec(
            name=name,
            extraction_prompt=md_bytes.decode("utf-8"),
            sections=data.get("sections", []),
            article_fields=data.get("article_fields", []),
            common_claim_fields=data.get("common_claim_fields", []),
            schema_version=schema_version,
        )
    except (ValueError, TypeError) as exc:  # pydantic ValidationError ⊂ ValueError
        raise SpecError(f"spec '{name}' .json has an invalid field/section shape: {exc}") from exc


# --------------------------------------------------------------------------- #
# Compilation: spec → dynamic Pydantic validator
# --------------------------------------------------------------------------- #
def _pytype(t: str):
    """Map a spec field type onto a lenient Optional Python type.

    Everything is Optional so the extractor can omit a field it has no value for
    (宁缺毋造). enum collapses to str — the prompt enforces the value set; loose
    str keeps a slightly-off label from failing the whole article's validation.
    """
    t = (t or "string").lower()
    if t.startswith("bool"):
        return bool | None
    if t.startswith(("number", "int", "float")):
        return float | None
    if t.startswith(("array", "list")):
        return list | None
    if t.startswith(("object", "dict")):
        return dict | None
    return str | None  # string / enum / anything else → lenient str


def compile_validator(spec: GeneratedSpec) -> type[BaseModel]:
    """Build the per-article ``CompiledArticle`` Pydantic model from *spec*.

    Claim = {section, text required; quote optional} + each section's fields +
    common_claim_fields (all optional). Article = fixed shell {no_relevant_content,
    source_org, thesis, claims[]}. ``article_fields`` are intentionally not added
    to the shell — the downstream renderer relies on the fixed shell only.
    """
    claim_fields: dict[str, tuple] = {
        "section": (str, ...),
        "text": (str, ...),
        "quote": (str | None, None),
    }
    defs: list[FieldDef] = list(spec.common_claim_fields)
    for s in spec.sections:
        defs.extend(s.fields)
    for f in defs:
        if not f.name or f.name in _RESERVED or f.name in claim_fields:
            continue
        claim_fields[f.name] = (_pytype(f.type), None)

    claim_model = create_model("CompiledClaim", **claim_fields)
    return create_model(
        "CompiledArticle",
        no_relevant_content=(bool, False),
        source_org=(str | None, None),
        thesis=(str | None, None),
        claims=(list[claim_model], Field(default_factory=list)),  # type: ignore[valid-type]
    )


# --------------------------------------------------------------------------- #
# Per-article extraction (map)
# --------------------------------------------------------------------------- #
def _to_plain_text(raw: str) -> str:
    if "<" in raw and ">" in raw:
        return _h2t_converter.handle(raw).strip()
    return raw.strip()


def build_extract_prompt(
    validator: type[BaseModel],
    *,
    title: str,
    body: str,
    intent_text: str = "",
    published_at: str | None = None,
    url: str | None = None,
    source_name: str | None = None,
) -> str:
    """Assemble the user message: topic + article + the JSON schema to fill.

    Two pieces of article metadata are injected when known:
    - *published_at* — anchor a relative ``time_ref`` ("本周"/"昨日") to an absolute date.
    - *url* / *source_name* — a publisher fallback for ``source_org`` when the body
      and title carry no attribution (e.g. a bare tweet whose only publisher signal
      is the handle in ``x.com/<handle>/…``). Framed as a fallback, not an override,
      with an explicit guard against treating a generic feed label as the publisher.

    Wording changes here must bump ``_EXTRACT_PROMPT_VERSION``.
    """
    schema = json.dumps(validator.model_json_schema(), ensure_ascii=False)
    plain = _to_plain_text(body)[:_MAX_EXTRACT_BODY_CHARS]
    # Cap the title too so a pathologically long one can't dodge the body cap.
    safe_title = (title or "")[:_MAX_TITLE_CHARS]
    topic = f"用户追踪的主题：\n> {intent_text}\n\n" if intent_text.strip() else ""
    pub = ""
    if published_at and str(published_at).strip():
        pub = (
            f"本文发布时间：{str(published_at).strip()}\n"
            "（正文中的相对时间如“本周/昨日/上月”，请据此换算为绝对日期填入 time_ref；"
            "无法确定就照抄原文相对词，不要臆造。）\n\n"
        )
    src = ""
    url_s = (url or "").strip()[:_MAX_URL_CHARS]
    sn_s = (source_name or "").strip()
    if url_s or sn_s:
        lines = []
        if url_s:
            lines.append(f"- URL：{url_s}")
        if sn_s:
            lines.append(f"- 渠道：{sn_s}")
        src = (
            "来源信息（仅供正文/标题无署名时参考，不得凌驾于正文）：\n"
            + "\n".join(lines)
            + "\n（优先从正文/标题挖真实发布机构填 source_org；正文确无署名时，可据 URL 的"
            "域名/账号推断，如 x.com/elerianm→Mohamed El-Erian、wsj.com→WSJ。"
            "社媒帖（x.com/<handle> 等）的发布者【就是该 handle 账号主本人】——正文里以“据 X”"
            "“X 编制/数据”形式出现、或仅被转引/链接的品牌，是数据或转引来源，【不是】本帖发布者，"
            "不要据此填 source_org（例：x.com/C_Barraud 帖里提到“data compiled by Bloomberg”，"
            "发布者是 Barraud，不是 Bloomberg）。把泛化的渠道名（如“外资研报”“Twitter·宏观市场”）"
            "当机构名也不行——以上情况都宁可留 null。）\n\n"
        )
    return (
        f"{topic}请从下面这【一篇】文章中，按上述章节需求抽取结构化事实。\n\n"
        f"标题：{safe_title}\n\n{src}{pub}正文：\n{plain}\n\n"
        f"输出 JSON，严格符合此结构：\n{schema}"
    )


async def extract_one(
    llm: BaseLLMBackend,
    spec: GeneratedSpec,
    validator: type[BaseModel],
    *,
    title: str,
    body: str,
    model: str,
    intent_text: str = "",
    published_at: str | None = None,
    url: str | None = None,
    source_name: str | None = None,
) -> BaseModel:
    """Map one article → validated extraction. Raises ``LLMError`` on failure.

    Validator is passed in (compiled once per batch by the caller) so a digest's
    worth of articles share one model build. The structured() repair loop handles
    transient schema misses; a hard failure propagates for the caller to record
    in the per-article errors list.
    """
    prompt = build_extract_prompt(
        validator,
        title=title,
        body=body,
        intent_text=intent_text,
        published_at=published_at,
        url=url,
        source_name=source_name,
    )
    return await llm.structured(prompt, validator, system=spec.extraction_prompt, model=model)
