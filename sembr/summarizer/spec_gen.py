# SPDX-License-Identifier: Apache-2.0
"""Extraction-spec generation, validation, and atomic persistence (spec-autogen).

``spec.py`` stays read-only (load + compile + extract). This module owns the
*write* side that the spec-autogen dashboard feature needs:

- ``generate_spec`` — meta-LLM turns an intent's analysis template (+ the shared
  ``_base.md`` floor + optional recent digest) into a draft :data:`MetaSpecOut`,
  then guarantees the common-claim *floor* fields are present (design §4.0) so
  the base prompt never references a field absent from the schema.
- ``validate_spec_payload`` — the authoritative save-time check (12 hard rules +
  2 soft warnings, design §5); the frontend only does a parse+non-empty
  drift-guard.
- ``save_spec_atomic`` — double-file (.md + .json) tmp+fsync+os.replace write,
  mirroring ``templates.save_template_atomic`` (design D5).

Naming: each intent owns one spec named ``intent-{id}`` (design D3).
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
1. 每个字段（article_fields / common_claim_fields / 各 section.fields）都必须填 `role` 和 `label`：
   - `role`：事实主体 → `content`；溯源/性质（来源类型、归属、时间等）→ `meta`；布尔徽章（是否预测、是否单一来源等）→ `flag`。
   - `label`：简短中文显示名。
2. 你产出的 `extraction_prompt` 必须【原样继承】下方给出的通用基底规则（反幻觉铁律 + 归属 + 横切字段 + 输入说明），再在其上叠加该 intent 的特化（章节骨架 + 相关性闸门 + 每节字段说明）。不得丢弃基底铁律。
3. 每个 section 的 `key` 是机器名（snake_case，字母开头，仅字母数字下划线），`label` 是节标题；claim 的 section 值将 ∈ 这些 key。

【固定外壳，不要重复产出】：claim 的 `section`/`text`/`quote` 与 article 的 `no_relevant_content` 由下游写死——【不要】把它们列为字段。`source_org`、`thesis` 请放进 article_fields（带 role/label）。通用横切字段（source_type/is_projection/time_ref/single_source/attribution）系统会自动补齐进 common_claim_fields，你产出与否都可以。

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
    """Per-intent spec name (design D3): stable, id-derived, identifier-safe."""
    return f"intent-{intent_id}"


def load_base(prompts_dir: Path = PROMPTS_DIR) -> str:
    """Read the shared ``_base.md`` floor prompt.

    Raises ``SpecBaseMissingError`` (→ 500) when absent: it's an operator-deployed
    file, and silently generating without the anti-hallucination floor would
    violate the spec's contract.
    """
    path = _spec_path(prompts_dir, "_base", "md")
    if not path.is_file():
        raise SpecBaseMissingError(
            f"通用基底 prompts/extraction/_base.md 缺失（{path}）；请管理员部署。"
        )
    return path.read_text(encoding="utf-8")


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
    return text[:limit] + "\n\n（…digest 已截断，仅作 schema 校准用）"


def _inject_floor(common_claim_fields: list[dict]) -> list[dict]:
    """Guarantee every floor field is present (design §4.0). Existing same-named
    fields are kept as-is (trust the meta-LLM's role/label); missing ones get the
    canonical def so the base prompt never references an absent field."""
    present = {f.get("name") for f in common_claim_fields if isinstance(f, dict)}
    out = list(common_claim_fields)
    for fld in _FLOOR_FIELDS:
        if fld["name"] not in present:
            out.append(dict(fld))
    return out


def _strip_reserved(fields: list[dict]) -> list[dict]:
    """Drop fields whose name collides with the fixed shell (would be ignored by
    compile_validator and rejected by validate_spec_payload rule 10)."""
    return [f for f in fields if not (isinstance(f, dict) and f.get("name") in _RESERVED)]


async def generate_spec(
    *,
    system_tpl: str,
    instruction_tpl: str,
    base: str,
    digest: str | None,
    backend: BaseLLMBackend,
    model: str,
) -> tuple[str, dict]:
    """Meta-LLM → draft spec. Returns ``(extraction_prompt_md, json_obj)``.

    json_obj = ``{sections, article_fields, common_claim_fields}`` with the floor
    guaranteed and reserved-shell names stripped. The endpoint serializes it.
    Raises ``LLMError`` if the model can't produce a schema-valid spec after the
    structured() repair loop.
    """
    schema = json.dumps(MetaSpecOut.model_json_schema(), ensure_ascii=False)
    user = META_USER.format(
        base=base,
        system=system_tpl,
        instruction=instruction_tpl,
        digest=digest or _NO_DIGEST,
        schema=schema,
    )
    out = await backend.structured(user, MetaSpecOut, system=META_SYSTEM, model=model)
    json_obj = {
        "sections": [
            {**s.model_dump(), "fields": _strip_reserved([f.model_dump() for f in s.fields])}
            for s in out.sections
        ],
        "article_fields": _strip_reserved([f.model_dump() for f in out.article_fields]),
        "common_claim_fields": _inject_floor(
            _strip_reserved([f.model_dump() for f in out.common_claim_fields])
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
            issues.append(ValidationIssue(loc=loc, msg="字段必须是对象"))
            continue
        name = f.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(ValidationIssue(loc=f"{loc}.name", msg="字段缺 name"))
            name = None
        else:
            if name in seen:
                issues.append(ValidationIssue(loc=f"{loc}.name", msg=f"字段名重复: {name}"))
            seen.add(name)
            if name in _RESERVED:
                issues.append(
                    ValidationIssue(loc=f"{loc}.name", msg=f"字段名 {name} 是保留 shell 名，不可用")
                )
        label = f.get("label")
        if not isinstance(label, str) or not label.strip():
            issues.append(ValidationIssue(loc=f"{loc}.label", msg=f"字段 {name or '?'} 缺 label"))
        role = f.get("role", "content")
        if role not in _VALID_ROLES:
            issues.append(
                ValidationIssue(loc=f"{loc}.role", msg=f"role 非法: {role}（须 content/meta/flag）")
            )
        ftype = f.get("type", "string")
        if ftype not in _VALID_TYPES:
            issues.append(ValidationIssue(loc=f"{loc}.type", msg=f"type 非法: {ftype}"))
        if ftype == "enum" and not (isinstance(f.get("enum"), list) and f.get("enum")):
            issues.append(ValidationIssue(loc=f"{loc}.enum", msg="enum 类型须给取值列表"))


def validate_spec_payload(md: str, json_text: str) -> list[ValidationIssue]:
    """Authoritative save-time validation. Empty list = pass. Errors block the
    write; warnings (severity='warning') don't. Rules indexed to design §5."""
    issues: list[ValidationIssue] = []
    if not md.strip():  # rule 1
        issues.append(ValidationIssue(loc="extraction_prompt", msg="extraction_prompt 不能为空"))
    try:  # rule 2
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        issues.append(
            ValidationIssue(loc="json", msg=f"JSON 语法错误: 第 {exc.lineno} 行 {exc.msg}")
        )
        return issues  # structure checks impossible on broken JSON
    if not isinstance(data, dict):  # rule 3
        issues.append(ValidationIssue(loc="json", msg="顶层必须是 JSON 对象"))
        return issues
    for key in ("sections", "article_fields", "common_claim_fields"):
        if key in data and not isinstance(data[key], list):
            issues.append(ValidationIssue(loc=f"json.{key}", msg=f"{key} 必须是数组"))

    _check_fields(data.get("article_fields", []), "article_fields", issues)
    _check_fields(data.get("common_claim_fields", []), "common_claim_fields", issues)

    seen_keys: set[str] = set()  # rules 11/12
    sections = data.get("sections", [])
    for i, s in enumerate(sections if isinstance(sections, list) else []):
        loc = f"sections[{i}]"
        if not isinstance(s, dict):
            issues.append(ValidationIssue(loc=loc, msg="section 必须是对象"))
            continue
        key = s.get("key")
        if not isinstance(key, str) or not _SECTION_KEY_RE.match(key):
            issues.append(
                ValidationIssue(
                    loc=f"{loc}.key",
                    msg="section key 缺失或含非法字符（须字母开头，仅字母数字下划线）",
                )
            )
        else:
            if key in seen_keys:
                issues.append(ValidationIssue(loc=f"{loc}.key", msg=f"section key 重复: {key}"))
            seen_keys.add(key)
        _check_fields(s.get("fields", []), f"{loc}.fields", issues)

    # rule 13 (warning): reduce relies on source_org + thesis (design §8.5)
    art_names = {f.get("name") for f in data.get("article_fields", []) if isinstance(f, dict)}
    for req in ("source_org", "thesis"):
        if req not in art_names:
            issues.append(
                ValidationIssue(
                    loc="article_fields",
                    msg=f"建议 article_fields 含 {req}（reduce 组装简报需要）",
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
                    msg=f"{fname} 被 _base.md 引用，删除会致 base↔schema 失配",
                    severity="warning",
                )
            )
    return issues


def has_errors(issues: list[ValidationIssue]) -> bool:
    """True if any issue is severity='error' (warnings don't block save)."""
    return any(i.severity == "error" for i in issues)


# --------------------------------------------------------------------------- #
# Atomic double-file write (design D5)
# --------------------------------------------------------------------------- #
def save_spec_atomic(
    name: str,
    md_text: str,
    json_text: str,
    prompts_dir: Path = PROMPTS_DIR,
) -> None:
    """Write ``{name}.md`` + ``{name}.json`` under prompts/extraction/ atomically.

    Each ``os.replace`` is atomic; the gap *between* the two is the known
    double-file window (design D5/R4): a crash there can leave a mixed version,
    surfaced by ``load_spec``'s both-halves requirement and the enable endpoint's
    re-load — accepted for a single-operator tool. Tmp names start with '.' so
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
