# SPDX-License-Identifier: Apache-2.0
"""Unit tests for spec-autogen: semantic projection, validation, floor, atomic write.

Dev-owned slice of the Test Strategy (T1–T5). Endpoint e2e / generate-with-fake-
backend (T6–T13) are QA-owned and live in tests/api/.
"""

from __future__ import annotations

import json

import pytest

from sembr.config import Settings
from sembr.summarizer.spec import _semantic_projection, load_spec
from sembr.summarizer.spec_gen import (
    _FLOOR_NAMES,
    _inject_article_floor,
    _inject_floor,
    _lang_directive,
    _normalize_fields,
    _normalize_type,
    _strip_reserved,
    derive_spec_name,
    has_errors,
    load_base,
    save_spec_atomic,
    validate_spec_payload,
)


# --------------------------------------------------------------------------- #
# T1 — _semantic_projection: display-only edits don't change the hash basis
# --------------------------------------------------------------------------- #
def _spec_dict(*, field_type="string", field_role="meta", field_label="X", sec_label="L"):
    return {
        "sections": [
            {
                "key": "s1",
                "label": sec_label,
                "fields": [
                    {"name": "a", "type": field_type, "role": field_role, "label": field_label}
                ],
            }
        ],
        "article_fields": [
            {"name": "source_org", "type": "string", "role": "content", "label": "y"}
        ],
        "common_claim_fields": [],
    }


def test_projection_ignores_role_label_enum_and_keyorder() -> None:
    base = _spec_dict()
    # reorder keys + whitespace + change role/label/section.label → same projection
    reordered = json.loads(json.dumps(base))
    reordered["sections"][0]["fields"][0] = {
        "label": "DIFFERENT",
        "role": "flag",
        "name": "a",
        "type": "string",
    }
    reordered["sections"][0]["label"] = "OTHER LABEL"
    reordered["article_fields"][0]["role"] = "meta"
    assert _semantic_projection(base) == _semantic_projection(reordered)


def test_projection_changes_on_type_name_section_key() -> None:
    base = _spec_dict()
    assert _semantic_projection(base) != _semantic_projection(_spec_dict(field_type="number"))
    renamed = json.loads(json.dumps(base))
    renamed["sections"][0]["fields"][0]["name"] = "b"
    assert _semantic_projection(base) != _semantic_projection(renamed)
    rekey = json.loads(json.dumps(base))
    rekey["sections"][0]["key"] = "s2"
    assert _semantic_projection(base) != _semantic_projection(rekey)


def test_projection_robust_on_malformed() -> None:
    # Non-dict / wrong-shape entries must not raise (load_spec raises the proper
    # SpecError afterwards; the projection just shouldn't crash).
    assert _semantic_projection({"sections": "nope", "article_fields": [42, {"name": "x"}]})


# --------------------------------------------------------------------------- #
# T2 — load_spec: schema_version stable + display-only edits don't bump it
# --------------------------------------------------------------------------- #
def _write_spec(prompts_dir, name, md, json_obj):
    d = prompts_dir / "extraction"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(md, encoding="utf-8")
    (d / f"{name}.json").write_text(json.dumps(json_obj, ensure_ascii=False), encoding="utf-8")


def test_load_spec_version_invariant_to_role_label(tmp_path) -> None:
    md = "prompt body"
    v1 = _spec_dict(field_role="meta", field_label="A")
    _write_spec(tmp_path, "s", md, v1)
    ver1 = load_spec("s", tmp_path).schema_version
    # same semantic content, different role/label → same version
    _write_spec(tmp_path, "s", md, _spec_dict(field_role="flag", field_label="B"))
    assert load_spec("s", tmp_path).schema_version == ver1
    # change the field type (semantic) → version moves
    _write_spec(tmp_path, "s", md, _spec_dict(field_type="number"))
    assert load_spec("s", tmp_path).schema_version != ver1


# --------------------------------------------------------------------------- #
# T3 — save_spec_atomic: writes both halves, no tmp leftover, rejects bad name
# --------------------------------------------------------------------------- #
def test_save_spec_atomic_writes_both(tmp_path) -> None:
    save_spec_atomic("intent-7", "MD BODY", '{"sections":[]}', tmp_path)
    d = tmp_path / "extraction"
    assert (d / "intent-7.md").read_text(encoding="utf-8") == "MD BODY"
    assert (d / "intent-7.json").read_text(encoding="utf-8") == '{"sections":[]}'
    # no leftover tmp (hidden) files
    assert not [p for p in d.iterdir() if p.name.startswith(".")]


def test_save_spec_atomic_rejects_path_escape(tmp_path) -> None:
    with pytest.raises(ValueError):
        save_spec_atomic("../evil", "x", "{}", tmp_path)


# --------------------------------------------------------------------------- #
# T4 — validate_spec_payload: each rule has a passing + failing case
# --------------------------------------------------------------------------- #
def _valid_json() -> str:
    return json.dumps(
        {
            "sections": [
                {
                    "key": "sec_a",
                    "label": "A",
                    "fields": [
                        {"name": "speaker", "type": "string", "role": "meta", "label": "发言人"}
                    ],
                }
            ],
            "article_fields": [
                {"name": "source_org", "type": "string", "role": "meta", "label": "来源"},
                {"name": "thesis", "type": "string", "role": "content", "label": "论点"},
            ],
            "common_claim_fields": [
                {"name": n, "type": "string", "role": "meta", "label": n} for n in _FLOOR_NAMES
            ],
        },
        ensure_ascii=False,
    )


# helpers that mutate the first article field / first section (defined before the
# parametrized test below — decorator args evaluate at import time)
def _drop_field_key(d, key):
    d["article_fields"][0].pop(key, None)
    return d


def _set_field_key(d, key, val):
    d["article_fields"][0][key] = val
    return d


def _dup_name(d):
    d["article_fields"].append(dict(d["article_fields"][0]))
    return d


def _set_section_key(d, val):
    d["sections"][0]["key"] = val
    return d


def _dup_section_key(d):
    d["sections"].append(json.loads(json.dumps(d["sections"][0])))
    return d


def test_validate_clean_spec_passes() -> None:
    # prompt mentions the section field (speaker) so rule 15 stays quiet too
    issues = validate_spec_payload("prompt that mentions speaker", _valid_json())
    assert not has_errors(issues)
    assert not issues  # clean spec → no errors AND no warnings


def test_validate_rule15_prompt_schema_consistency() -> None:
    # section field absent from the prompt → warning (not an error)
    issues = validate_spec_payload("prompt without that word", _valid_json())
    assert not has_errors(issues)
    assert any(
        i.severity == "warning" and "speaker" in i.msg and "extraction_prompt" in i.msg
        for i in issues
    )
    # once the prompt mentions it → no such warning
    ok = validate_spec_payload("we extract the speaker's stance", _valid_json())
    assert not any("isn't mentioned in extraction_prompt" in i.msg for i in ok)


@pytest.mark.parametrize(
    "md, mutate, expect_loc_substr",
    [
        ("", lambda d: d, "extraction_prompt"),  # rule 1
        ("p", "NOT JSON", "json"),  # rule 2 (mutate=string → replace json_text)
        ("p", lambda d: {**d, "sections": "x"}, "json.sections"),  # rule 3
        ("p", lambda d: _drop_field_key(d, "name"), ".name"),  # rule 4
        ("p", lambda d: _drop_field_key(d, "label"), ".label"),  # rule 5
        ("p", lambda d: _set_field_key(d, "role", "zzz"), ".role"),  # rule 6
        ("p", lambda d: _set_field_key(d, "type", "weird"), ".type"),  # rule 7
        (
            "p",
            lambda d: _set_field_key(d, "type", "enum"),
            ".enum",
        ),  # rule 8 (enum type, no values)
        ("p", _dup_name, ".name"),  # rule 9
        ("p", lambda d: _set_field_key(d, "name", "quote"), ".name"),  # rule 10 reserved
        ("p", lambda d: _set_section_key(d, "1bad"), "sections[0].key"),  # rule 11
        ("p", _dup_section_key, ".key"),  # rule 12
    ],
)
def test_validate_each_error_rule(md, mutate, expect_loc_substr) -> None:
    if mutate == "NOT JSON":
        issues = validate_spec_payload(md, "{ not json ]")
    else:
        data = json.loads(_valid_json())
        data = mutate(data) if callable(mutate) else data
        issues = validate_spec_payload(md, json.dumps(data, ensure_ascii=False))
    assert has_errors(issues)
    assert any(expect_loc_substr in i.loc for i in issues if i.severity == "error"), (
        f"no error at {expect_loc_substr!r}: {[(i.loc, i.msg) for i in issues]}"
    )


def test_validate_floor_and_source_org_warnings() -> None:
    data = json.loads(_valid_json())
    data["common_claim_fields"] = []  # drop the floor
    data["article_fields"] = []  # drop source_org/thesis
    issues = validate_spec_payload("p", json.dumps(data, ensure_ascii=False))
    assert not has_errors(issues)  # all warnings, no errors
    warn_msgs = " ".join(i.msg for i in issues if i.severity == "warning")
    assert "source_org" in warn_msgs and "thesis" in warn_msgs
    for fname in _FLOOR_NAMES:
        assert fname in warn_msgs


# --------------------------------------------------------------------------- #
# floor / strip / naming units (support generate_spec; T6 e2e is QA-owned)
# --------------------------------------------------------------------------- #
def test_inject_floor_adds_missing_keeps_existing() -> None:
    out = _inject_floor([{"name": "stance", "type": "enum", "role": "flag", "label": "s"}])
    names = [f["name"] for f in out]
    assert names[0] == "stance"  # existing kept, in place
    assert _FLOOR_NAMES.issubset(set(names))  # all floor present


def test_inject_floor_idempotent_when_present() -> None:
    seed = [{"name": n, "type": "string", "role": "meta", "label": n} for n in _FLOOR_NAMES]
    assert len(_inject_floor(seed)) == len(seed)  # nothing appended


def test_inject_floor_canonicalizes_degraded_floor() -> None:
    # The meta-LLM sometimes emits floor fields with a degraded type (bool→string)
    # and dropped enum/description; the floor contract with _base.md must win.
    out = _inject_floor(
        [
            {"name": "is_projection", "type": "string", "role": "flag", "label": "我的预测"},
            {"name": "source_type", "type": "string", "enum": [], "role": "meta", "label": "类型"},
        ]
    )
    by_name = {f["name"]: f for f in out}
    assert by_name["is_projection"]["type"] == "bool"  # canonical type restored
    assert by_name["is_projection"]["label"] == "我的预测"  # meta's label preserved
    assert by_name["source_type"]["type"] == "enum" and by_name["source_type"]["enum"]
    assert by_name["source_type"]["description"]  # canonical description restored


def _seed_base(prompts_dir, name: str, text: str) -> None:
    d = prompts_dir / "extraction"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text, encoding="utf-8")


def test_load_base_zh_is_native_default(tmp_path) -> None:
    _seed_base(tmp_path, "_base", "中文基底")
    text, native = load_base(tmp_path, "zh")
    assert text == "中文基底" and native is True


def test_load_base_non_native_falls_back_to_default(tmp_path) -> None:
    # No _base_en.md → fall back to _base.md, flagged non-native (meta translates).
    _seed_base(tmp_path, "_base", "中文基底")
    text, native = load_base(tmp_path, "en")
    assert text == "中文基底" and native is False


def test_load_base_prefers_language_file_verbatim(tmp_path) -> None:
    _seed_base(tmp_path, "_base", "中文基底")
    _seed_base(tmp_path, "_base_en", "English floor")
    text, native = load_base(tmp_path, "en")
    assert text == "English floor" and native is True


def test_lang_directive_zh_keeps_verbatim_chinese() -> None:
    d = _lang_directive("zh", base_is_native=True)
    assert "目标语言：中文" in d and "逐字继承" in d and "绝不翻译" in d


def test_lang_directive_non_native_asks_for_translation() -> None:
    d = _lang_directive("en", base_is_native=False)
    assert "English" in d and "译为" in d  # translate the floor into the target language


def test_strip_reserved_drops_shell_names() -> None:
    out = _strip_reserved([{"name": "quote"}, {"name": "speaker"}, {"name": "section"}])
    assert [f["name"] for f in out] == ["speaker"]


def test_inject_article_floor_guarantees_source_org_and_thesis() -> None:
    # meta dropping thesis (the geo_hormuz failure) → floor puts it back
    out = _inject_article_floor(
        [{"name": "source_org", "type": "string", "role": "meta", "label": "x"}]
    )
    names = [f["name"] for f in out]
    assert "source_org" in names and "thesis" in names
    # idempotent when both present
    seed = [{"name": "source_org"}, {"name": "thesis"}]
    assert len(_inject_article_floor(seed)) == 2


def test_derive_spec_name() -> None:
    assert derive_spec_name(30) == "intent-30"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("boolean", "bool"),
        ("integer", "number"),
        ("float", "number"),
        ("list", "array"),
        ("dict", "object"),
        ("str", "string"),
        ("BOOLEAN", "bool"),  # case-insensitive
        ("string", "string"),
        ("strign", "strign"),  # real typo passes through (validation still flags it)
    ],
)
def test_normalize_type(raw, expected) -> None:
    assert _normalize_type(raw) == expected


def test_normalize_fields_rewrites_json_schema_types() -> None:
    fields = [{"name": "f", "type": "boolean", "role": "flag", "label": "F"}]
    _normalize_fields(fields)
    assert fields[0]["type"] == "bool"


def test_validate_accepts_json_schema_type_aliases() -> None:
    # meta-LLM-style `boolean` must NOT be flagged invalid (the bug the user hit).
    data = json.loads(_valid_json())
    data["article_fields"].append({"name": "is_x", "type": "boolean", "role": "flag", "label": "X"})
    issues = validate_spec_payload("p", json.dumps(data, ensure_ascii=False))
    assert not any("invalid type" in i.msg for i in issues if i.severity == "error")


# --------------------------------------------------------------------------- #
# T5 — effective_meta_extraction_model fallback
# --------------------------------------------------------------------------- #
def test_effective_meta_extraction_model_fallback() -> None:
    s = Settings.model_construct(meta_extraction_model="", llm_model="base-model")
    assert s.effective_meta_extraction_model == "base-model"
    s2 = Settings.model_construct(meta_extraction_model="meta-x", llm_model="base-model")
    assert s2.effective_meta_extraction_model == "meta-x"
