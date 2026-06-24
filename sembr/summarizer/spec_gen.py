# SPDX-License-Identifier: Apache-2.0
"""Extraction-spec generation, validation, and atomic persistence (spec-autogen).

``spec.py`` stays read-only (load + compile + extract). This module owns the
*write* side that the spec-autogen dashboard feature needs:

- ``generate_spec`` — meta-LLM turns an intent's analysis template (+ the shared
  ``_base.md`` floor + optional recent digest) into a draft :data:`MetaSpecOut`,
  then guarantees the common-claim *floor* fields are present (design §4.0) so
  the base prompt never references a field absent from the schema.
- ``validate_spec_payload`` — the authoritative save-time check (13 hard rules +
  3 soft warnings; see §5); the frontend only does a parse+non-empty drift-guard.
- ``save_spec_atomic`` — double-file (.md + .json) tmp+fsync+os.replace write,
  mirroring ``templates.save_template_atomic``.

Naming: each intent owns one spec named ``intent-{id}``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from sembr.summarizer.llm.base import BaseLLMBackend
from sembr.summarizer.spec import _RESERVED, _spec_path
from sembr.summarizer.templates import PROMPTS_DIR

logger = logging.getLogger(__name__)


class SpecBaseMissingError(FileNotFoundError):
    """Raised when the shared ``_base.md`` floor prompt is absent (a deploy error)."""


# --------------------------------------------------------------------------- #
# Common floor — guaranteed in every spec's common_claim_fields, paired 1:1 with
# _base.md's cross-cutting section (design §4.0). generate_spec injects any that
# the meta-LLM omitted; editing _base.md's cross-cutting fields means editing
# this list too (one contract).
# --------------------------------------------------------------------------- #
_FLOOR_FIELDS: list[dict] = [
    {
        "name": "source_type",
        "type": "enum",
        "enum": ["primary", "secondary", "social_unverified"],
        "description": "一手 primary / 转述 secondary / 社媒未核实 social_unverified",
        "role": "meta",
        "label": "来源类型",
    },
    {
        "name": "attribution",
        "type": "string",
        "enum": [],
        "description": "该条事实的归属方，默认 = source_org，转引时填被引方",
        "role": "meta",
        "label": "归属",
    },
    {
        "name": "is_projection",
        "type": "bool",
        "enum": [],
        "description": "该条是预测/预期而非既成事实时置 true",
        "role": "flag",
        "label": "预测",
    },
    {
        "name": "single_source",
        "type": "bool",
        "enum": [],
        "description": "社媒等未核实的单一来源时置 true",
        "role": "flag",
        "label": "单一来源",
    },
    {
        "name": "time_ref",
        "type": "string",
        "enum": [],
        "description": "事件时间，尽量 MM/DD HH:MM TZ",
        "role": "meta",
        "label": "时间",
    },
]
_FLOOR_NAMES = frozenset(f["name"] for f in _FLOOR_FIELDS)


# --------------------------------------------------------------------------- #
# Meta-LLM output contract (mirrors GeneratedSpec's *generatable* part — name /
# schema_version are computed at load time, not generated).
# --------------------------------------------------------------------------- #
class _GenFieldDef(BaseModel):
    name: str
    type: str = "string"
    enum: list[str] = Field(default_factory=list)
    description: str = ""
    role: str = "content"
    label: str = ""


class _GenSectionDef(BaseModel):
    key: str
    label: str = ""
    fields: list[_GenFieldDef] = Field(default_factory=list)


class MetaSpecOut(BaseModel):
    extraction_prompt: str
    sections: list[_GenSectionDef] = Field(default_factory=list)
    article_fields: list[_GenFieldDef] = Field(default_factory=list)
    common_claim_fields: list[_GenFieldDef] = Field(default_factory=list)


META_SYSTEM = """你是「抽取规格设计师」。给你一份新闻简报的【分析模板】(system+instruction) 和一份【通用基底提取规则】，你要设计一套 per-article 抽取规格——让一个轻量 LLM 逐篇把文章抽成结构化事实，下游再用这些事实重建出该模板规定的简报。

【主依据是分析模板本身】：模板的输出结构（有哪些章节、每节写什么、引用/反幻觉/格式规则）就是规格的来源——这是新建 intent 时唯一可用的信息，必须只凭模板就能推出可用规格。若额外给了一篇真实简报样例，仅作补充校准（确认没漏维度），没有也要正常产出。

设计目标（硬标准）：只看抽取结果（不看原文），就能写出符合该模板的简报。多了浪费，少了下游得回去翻原文（幻觉复活）。

【硬要求，必须满足】：
1. 每个字段（article_fields / common_claim_fields / 各 section.fields）都必须填 `role` 和 `label`，并尽量给一句 `description`（说明该字段抽什么）：
   - `role`：事实主体 → `content`；溯源/性质（来源类型、归属、时间等）→ `meta`；布尔徽章（是否预测、是否单一来源等）→ `flag`。
   - `label`：简短显示名（用目标语言，见文末「目标语言」指令）。
   - `description`：一句话说明取值口径，供下游/编辑者看（留空也能跑，但尽量填）。
2. 你产出的 `extraction_prompt` 必须【原样继承】下方给出的通用基底规则（反幻觉铁律 + 归属 + 横切字段 + 输入说明），再在其上叠加该 intent 的特化（章节骨架 + 相关性闸门 + 每节字段说明）。不得丢弃基底铁律。
3. 每个 section 的 `key` 是机器名（snake_case，字母开头，仅字母数字下划线），`label` 是节标题；claim 的 section 值将 ∈ 这些 key。
4. 【逐节覆盖，绝不漏维度】逐一扫描分析模板的输出结构，模板里**每一个输出章节 / 信息维度**都要有对应的 section（或字段）承接，**不得遗漏**。两类最常被漏、务必覆盖：
   - **分析 / 推断节**（模板标注「推断」「不加 [N]」「判断表」的节，如「资产影响 / 市场判断」「反共识 / 尾部风险」）：该节由下游综合，但你**仍必须为它建 section + 字段**去抽它所依赖的【底层事实】——价格 / 点位数字、市场与资产反应、成交 / 费率 / 库存 / 产量数据、关键信号。否则下游无事实可锚定，该节会空或幻觉。这类「市场 / 价格 / 资产」节是市场类简报的核心，最不能漏。
   - **多子项的表态 / 各方节**（如「关键各方表态」含谈判 / 各施压方 / 各当事方）：用【一个 section + `actor` 字段】区分不同方，**不要按方拆成多个 section**（如 us_stance / iran_stance / israel_stance —— 这类拆分里弱势方常只有零星 1 条，全是冗余空节；section 的粒度对齐模板的章节标题，不对齐文章里的实体）。同样也别把内部结构压成一句话，靠字段承接。

【固定外壳，不要重复产出】：claim 的 `section`/`text`/`quote` 与 article 的 `no_relevant_content` 由下游写死——【不要】把它们列为字段。`source_org`、`thesis` 由系统自动补进 article_fields（你产出与否都可以，但**不要漏掉 thesis 这个维度的设计意图**）。通用横切字段（source_type/is_projection/time_ref/single_source/attribution）系统会自动补齐进 common_claim_fields，你产出与否都可以；若你产出，其类型/枚举以基底为准（系统会强制规范化）——别把 is_projection/single_source 之类改成 string。概览 / 叙事节（如「态势综述」）由 article 级 thesis 承接，不必单设 section。

你的自由度在 sections 枚举、各 section 专属字段、以及 article_fields 里 source_org/thesis 之外的补充。

只输出 JSON（MetaSpecOut）。"""

META_USER = """# 通用基底提取规则（必须原样继承进 extraction_prompt）
{base}

---

# 分析模板 — system
{system}

---

# 分析模板 — instruction
{instruction}

---

# （可选）最近 1 篇真实简报 — 仅作补充校准，无则忽略
{digest}

---

只输出 JSON，严格符合此结构（MetaSpecOut）：
{schema}

sections 必须覆盖模板规定的全部章节与信息维度；各 section 的专属字段嵌在该 section 的 fields 里。"""

_NO_DIGEST = "（本 intent 尚无历史简报 —— 仅凭模板推导，这是新建 cron 的常态）"

# digest is plain summary markdown; cap to keep the meta-LLM context bounded.
_MAX_DIGEST_CHARS = 12_000


def derive_spec_name(intent_id: int) -> str:
    """Per-intent spec name: stable, id-derived, identifier-safe (``intent-{id}``)."""
    return f"intent-{intent_id}"


def load_base(prompts_dir: Path = PROMPTS_DIR, language: str = "zh") -> tuple[str, bool]:
    """Read the floor prompt for *language*; return ``(text, is_native)``.

    Prefers a hand-authored ``_base_{language}.md`` (returned verbatim, so the
    anti-hallucination floor is guaranteed intact — ``is_native=True``). Falls
    back to the default Chinese ``_base.md`` when no per-language file exists;
    then ``is_native=False`` and the caller asks the meta-LLM to translate the
    floor into *language* (acceptable until a ``_base_{language}.md`` is added).
    ``zh`` always maps to ``_base.md`` and is native.

    Raises ``SpecBaseMissingError`` (→ 500) when even the default is absent.
    """
    lang = (language or "zh").lower()
    if lang != "zh":
        try:
            cand = _spec_path(prompts_dir, f"_base_{lang}", "md")
        except ValueError:
            cand = None  # malformed language code → fall back to the default
        if cand is not None and cand.is_file():
            return cand.read_text(encoding="utf-8"), True
    path = _spec_path(prompts_dir, "_base", "md")
    if not path.is_file():
        raise SpecBaseMissingError(
            f"Shared base prompt prompts/extraction/_base.md is missing ({path}); "
            "an administrator must deploy it."
        )
    return path.read_text(encoding="utf-8"), (lang == "zh")


# Display names for the target-language directive; unknown codes pass through.
_LANG_NAMES = {
    "zh": "中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "pt": "Português",
    "ru": "Русский",
}


def _lang_directive(language: str, base_is_native: bool) -> str:
    """Target-language directive appended to the meta prompt.

    ``zh`` + native base reproduces today's behavior (Chinese spec, floor inherited
    verbatim). A non-native base asks the meta-LLM to translate the floor into the
    target language, preserving every rule (used until a ``_base_{lang}.md`` exists).
    The directive itself is written in Chinese (the meta model is bilingual); only
    the *generated* spec follows the target language.
    """
    lang = (language or "zh").lower()
    name = _LANG_NAMES.get(lang, language or "中文")
    base_clause = (
        f"基底规则（上文）已是{name}，按原文逐字继承，不要改写或省略。"
        if base_is_native
        else f"上文基底规则不是{name}：请将其忠实译为{name}后并入——每条反幻觉铁律、"
        "归属与字段规则都必须保留，不得删减或弱化。"
    )
    return (
        f"\n\n---\n# 目标语言：{name}\n"
        f"- 生成的 extraction_prompt 整体，以及所有 section 与字段的 label / description，都用{name}。\n"
        f"- {base_clause}\n"
        f"- 在 extraction_prompt 里指示抽取器：text / thesis / 各字段值用{name}输出；"
        f"`quote` 保持文章源语言、逐字照抄、绝不翻译。"
    )


def read_spec_raw(name: str, prompts_dir: Path = PROMPTS_DIR) -> tuple[str, str] | None:
    """Return ``(md_text, json_text)`` raw from disk, or None if either half is
    missing. Bypasses ``load_spec`` parsing on purpose: the GET-to-edit path must
    surface a *malformed* spec so the user can fix it in the editor. Raises
    ``ValueError`` on a bad name."""
    md_path = _spec_path(prompts_dir, name, "md")
    json_path = _spec_path(prompts_dir, name, "json")
    if not md_path.is_file() or not json_path.is_file():
        return None
    return md_path.read_text(encoding="utf-8"), json_path.read_text(encoding="utf-8")


def truncate_digest(text: str, limit: int = _MAX_DIGEST_CHARS) -> str:
    """Keep the head (lead + section skeleton), drop the tail, mark truncation."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n(…digest truncated; calibration context only)"


def _canonicalize_floor(fields: list[dict], floor: list[dict]) -> list[dict]:
    """Force every floor field to its canonical type/enum/description/role,
    overriding whatever the meta-LLM emitted, then append any it omitted.

    The floor is a fixed contract paired 1:1 with ``_base.md``; the meta-LLM
    non-deterministically *degrades* it when it chooses to emit the floor fields
    itself — ``is_projection`` bool→string, ``source_type``'s enum dropped,
    descriptions emptied — which silently breaks the base↔schema contract (e.g.
    a string-typed ``is_projection`` makes ``compile_validator`` extract
    "true"/"false" text instead of a bool). So we don't trust the meta-LLM's
    version: only its display ``label`` is kept (when non-empty); everything else
    is the canonical def. Non-floor fields pass through in place; missing floor
    fields are appended in canonical order."""
    canon = {f["name"]: f for f in floor}
    out: list[dict] = []
    seen: set[str] = set()
    for f in fields:
        if isinstance(f, dict) and f.get("name") in canon:
            c = dict(canon[f["name"]])
            if isinstance(f.get("label"), str) and f["label"].strip():
                c["label"] = f["label"]
            out.append(c)
            seen.add(f["name"])
        else:
            out.append(f)
    for name, fld in canon.items():
        if name not in seen:
            out.append(dict(fld))
    return out


def _inject_floor(common_claim_fields: list[dict]) -> list[dict]:
    """Guarantee every common-claim floor field is present *and canonical*
    (so ``_base.md`` never references an absent or degraded field)."""
    return _canonicalize_floor(common_claim_fields, _FLOOR_FIELDS)


# Article-level floor — source_org + thesis must always be present (reduce uses
# thesis for the TL;DR; the meta-LLM sometimes drops it). Mirrors _inject_floor.
_ARTICLE_FLOOR = [
    {
        "name": "source_org",
        "type": "string",
        "enum": [],
        "description": "本篇的真实发布机构",
        "role": "meta",
        "label": "来源机构",
    },
    {
        "name": "thesis",
        "type": "string",
        "enum": [],
        "description": "本篇核心论点（一句话，供下游构建 TL;DR）",
        "role": "content",
        "label": "核心论点",
    },
]


def _inject_article_floor(article_fields: list[dict]) -> list[dict]:
    """Guarantee source_org + thesis are present *and canonical* (reduce relies
    on thesis for the TL;DR; the meta-LLM sometimes drops or retypes it)."""
    return _canonicalize_floor(article_fields, _ARTICLE_FLOOR)


def _strip_reserved(fields: list[dict]) -> list[dict]:
    """Drop fields whose name collides with the fixed shell (would be ignored by
    compile_validator and rejected by validate_spec_payload rule 10)."""
    return [f for f in fields if not (isinstance(f, dict) and f.get("name") in _RESERVED)]


# meta-LLMs lean on JSON-Schema type names (boolean/integer/...); map them to
# sembr's set so a `boolean` field isn't flagged "invalid type". `_pytype`
# already tolerates these (it's startswith-based), so this only aligns the
# validator + keeps the persisted spec canonical.
_TYPE_ALIASES = {
    "str": "string",
    "string": "string",
    "text": "string",
    "enum": "enum",
    "bool": "bool",
    "boolean": "bool",
    "number": "number",
    "num": "number",
    "int": "number",
    "integer": "number",
    "float": "number",
    "double": "number",
    "array": "array",
    "list": "array",
    "object": "object",
    "dict": "object",
    "map": "object",
}


def _normalize_type(t: object) -> str:
    """Canonical sembr type for a (possibly JSON-Schema-style) type name.
    Unknown values pass through unchanged so validation still flags real typos."""
    raw = t if isinstance(t, str) and t.strip() else "string"
    return _TYPE_ALIASES.get(raw.strip().lower(), raw)


def _normalize_fields(fields: list[dict]) -> list[dict]:
    for f in fields:
        if isinstance(f, dict) and "type" in f:
            f["type"] = _normalize_type(f.get("type"))
    return fields


async def generate_spec(
    *,
    system_tpl: str,
    instruction_tpl: str,
    base: str,
    digest: str | None,
    backend: BaseLLMBackend,
    model: str,
    language: str = "zh",
    base_is_native: bool = True,
) -> tuple[str, dict]:
    """Meta-LLM → draft spec. Returns ``(extraction_prompt_md, json_obj)``.

    json_obj = ``{sections, article_fields, common_claim_fields}`` with the floor
    guaranteed and reserved-shell names stripped. The endpoint serializes it.
    ``language`` (the intent's cron language) drives the generated spec's output
    language; ``base_is_native`` (from ``load_base``) decides whether the floor is
    inherited verbatim or translated. Raises ``LLMError`` if the model can't
    produce a schema-valid spec after the structured() repair loop.
    """
    schema = json.dumps(MetaSpecOut.model_json_schema(), ensure_ascii=False)
    user = META_USER.format(
        base=base,
        system=system_tpl,
        instruction=instruction_tpl,
        digest=digest or _NO_DIGEST,
        schema=schema,
    ) + _lang_directive(language, base_is_native)
    out = await backend.structured(user, MetaSpecOut, system=META_SYSTEM, model=model)
    json_obj = {
        "sections": [
            {
                **s.model_dump(),
                "fields": _normalize_fields(_strip_reserved([f.model_dump() for f in s.fields])),
            }
            for s in out.sections
        ],
        "article_fields": _inject_article_floor(
            _normalize_fields(_strip_reserved([f.model_dump() for f in out.article_fields]))
        ),
        "common_claim_fields": _inject_floor(
            _normalize_fields(_strip_reserved([f.model_dump() for f in out.common_claim_fields]))
        ),
    }
    return out.extraction_prompt, json_obj


# --------------------------------------------------------------------------- #
# Validation (authoritative; frontend only parse+non-empty — drift-guard)
# --------------------------------------------------------------------------- #
class ValidationIssue(BaseModel):
    loc: str
    msg: str
    severity: str = "error"  # "error" | "warning"


_VALID_ROLES = frozenset({"content", "meta", "flag"})
_VALID_TYPES = frozenset({"string", "enum", "bool", "number", "array", "object"})
_SECTION_KEY_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")


def _check_fields(items: object, scope_loc: str, issues: list[ValidationIssue]) -> None:
    """Rules 4–10, scoped (name uniqueness is per-scope)."""
    seen: set[str] = set()
    for i, f in enumerate(items if isinstance(items, list) else []):
        loc = f"{scope_loc}[{i}]"
        if not isinstance(f, dict):
            issues.append(ValidationIssue(loc=loc, msg="field must be an object"))
            continue
        name = f.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(ValidationIssue(loc=f"{loc}.name", msg="field is missing name"))
            name = None
        else:
            if name in seen:
                issues.append(
                    ValidationIssue(loc=f"{loc}.name", msg=f"duplicate field name: {name}")
                )
            seen.add(name)
            if name in _RESERVED:
                issues.append(
                    ValidationIssue(
                        loc=f"{loc}.name", msg=f"field name {name} is a reserved shell name"
                    )
                )
        label = f.get("label")
        if not isinstance(label, str) or not label.strip():
            issues.append(
                ValidationIssue(loc=f"{loc}.label", msg=f"field {name or '?'} is missing label")
            )
        role = f.get("role", "content")
        if role not in _VALID_ROLES:
            issues.append(
                ValidationIssue(
                    loc=f"{loc}.role", msg=f"invalid role: {role} (must be content/meta/flag)"
                )
            )
        ftype = _normalize_type(f.get("type", "string"))
        if ftype not in _VALID_TYPES:
            issues.append(ValidationIssue(loc=f"{loc}.type", msg=f"invalid type: {f.get('type')}"))
        if ftype == "enum" and not (isinstance(f.get("enum"), list) and f.get("enum")):
            issues.append(
                ValidationIssue(loc=f"{loc}.enum", msg="enum type requires a non-empty values list")
            )


def validate_spec_payload(md: str, json_text: str) -> list[ValidationIssue]:
    """Authoritative save-time validation. Empty list = pass. Errors block the
    write; warnings (severity='warning') don't. Rules indexed to design §5."""
    issues: list[ValidationIssue] = []
    if not md.strip():  # rule 1
        issues.append(
            ValidationIssue(loc="extraction_prompt", msg="extraction_prompt must not be empty")
        )
    try:  # rule 2
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        issues.append(
            ValidationIssue(loc="json", msg=f"JSON syntax error: line {exc.lineno} {exc.msg}")
        )
        return issues  # structure checks impossible on broken JSON
    if not isinstance(data, dict):  # rule 3
        issues.append(ValidationIssue(loc="json", msg="top level must be a JSON object"))
        return issues
    for key in ("sections", "article_fields", "common_claim_fields"):
        if key in data and not isinstance(data[key], list):
            issues.append(ValidationIssue(loc=f"json.{key}", msg=f"{key} must be an array"))

    _check_fields(data.get("article_fields", []), "article_fields", issues)
    _check_fields(data.get("common_claim_fields", []), "common_claim_fields", issues)

    seen_keys: set[str] = set()  # rules 11/12
    sections = data.get("sections", [])
    for i, s in enumerate(sections if isinstance(sections, list) else []):
        loc = f"sections[{i}]"
        if not isinstance(s, dict):
            issues.append(ValidationIssue(loc=loc, msg="section must be an object"))
            continue
        key = s.get("key")
        if not isinstance(key, str) or not _SECTION_KEY_RE.match(key):
            issues.append(
                ValidationIssue(
                    loc=f"{loc}.key",
                    msg="section key is missing or has invalid chars "
                    "(letter-led, alphanumeric/underscore only)",
                )
            )
        else:
            if key in seen_keys:
                issues.append(
                    ValidationIssue(loc=f"{loc}.key", msg=f"duplicate section key: {key}")
                )
            seen_keys.add(key)
        _check_fields(s.get("fields", []), f"{loc}.fields", issues)

    # rule 13 (warning): reduce relies on source_org + thesis
    art_names = {f.get("name") for f in data.get("article_fields", []) if isinstance(f, dict)}
    for req in ("source_org", "thesis"):
        if req not in art_names:
            issues.append(
                ValidationIssue(
                    loc="article_fields",
                    msg=f"article_fields should include {req} (needed by reduce)",
                    severity="warning",
                )
            )
    # rule 14 (warning): floor pairs with _base.md
    common_names = {
        f.get("name") for f in data.get("common_claim_fields", []) if isinstance(f, dict)
    }
    for fname in _FLOOR_NAMES:
        if fname not in common_names:
            issues.append(
                ValidationIssue(
                    loc="common_claim_fields",
                    msg=f"{fname} is referenced by _base.md; removing it desyncs base/schema",
                    severity="warning",
                )
            )
    # rule 15 (warning): prompt↔schema consistency — a section's own fields should
    # be described in the extraction_prompt. Scoped to section fields (floor lives
    # in _base.md, article shell is conventional); skip ≤2-char names to avoid
    # false positives on common short words. Substring match, warning only.
    for i, s in enumerate(sections if isinstance(sections, list) else []):
        if not isinstance(s, dict):
            continue
        for j, f in enumerate(s.get("fields", []) if isinstance(s.get("fields"), list) else []):
            name = f.get("name") if isinstance(f, dict) else None
            if isinstance(name, str) and len(name) > 2 and name not in _RESERVED and name not in md:
                issues.append(
                    ValidationIssue(
                        loc=f"sections[{i}].fields[{j}].name",
                        msg=f"field '{name}' isn't mentioned in extraction_prompt "
                        "(prompt/schema may be inconsistent)",
                        severity="warning",
                    )
                )
    return issues


def has_errors(issues: list[ValidationIssue]) -> bool:
    """True if any issue is severity='error' (warnings don't block save)."""
    return any(i.severity == "error" for i in issues)


# --------------------------------------------------------------------------- #
# Atomic double-file write (.md + .json together, same pattern as save_template_atomic)
# --------------------------------------------------------------------------- #
def save_spec_atomic(
    name: str,
    md_text: str,
    json_text: str,
    prompts_dir: Path = PROMPTS_DIR,
) -> None:
    """Write ``{name}.md`` + ``{name}.json`` under prompts/extraction/ atomically.

    Each ``os.replace`` is atomic; the gap *between* the two is the known
    double-file window: a crash there can leave a mixed version, surfaced by
    ``load_spec``'s both-halves requirement and the enable endpoint's re-load —
    accepted for a single-operator tool. Tmp names start with '.' so
    directory listings/globs skip them. Raises ``OSError`` on filesystem failure,
    ``ValueError`` on a bad name.
    """
    md_path = _spec_path(prompts_dir, name, "md")
    json_path = _spec_path(prompts_dir, name, "json")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"{os.getpid()}.{time.monotonic_ns()}"
    tmp_md = md_path.parent / f".{name}.md.tmp.{suffix}"
    tmp_json = json_path.parent / f".{name}.json.tmp.{suffix}"
    try:
        for tmp, text in ((tmp_md, md_text), (tmp_json, json_text)):
            with open(tmp, "wb") as fh:
                fh.write(text.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp_md, md_path)
        os.replace(tmp_json, json_path)
    except BaseException:
        for tmp in (tmp_md, tmp_json):
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        raise
