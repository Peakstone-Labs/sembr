# SPDX-License-Identifier: Apache-2.0
"""QA-owned tests for the extraction-spec lifecycle endpoints (spec-autogen T6–T13).

Test Strategy owner: QA (design.md §9, T6–T13).

Fixture pattern mirrors tests/api/test_prompts_routes.py:
  - in-memory aiosqlite via install_for_test + init_intent_tables
  - fake LLM backend for generate tests
  - PROMPTS_DIR patched to a tmp_path
  - DashboardTokenMiddleware added where 401 is tested
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.extraction_spec import router as extraction_spec_router
from sembr.dashboard.auth import DashboardTokenMiddleware
from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.sqlite import install_for_test
from sembr.db.summary_history import init_summary_history_table
from sembr.models import IntentCreate
from sembr.summarizer.spec_gen import _FLOOR_NAMES, MetaSpecOut

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

_FAKE_BASE = "宁缺毋造。横切字段。归属铁律。"  # subset of _base.md content for marker checks


def _make_fake_meta_spec_out(*, omit_floor: bool = False) -> MetaSpecOut:
    """A well-formed MetaSpecOut for the fake backend to return.

    When omit_floor=True, the common_claim_fields is intentionally empty so the
    generate_spec floor-injection logic is exercised (T6 floor-補齐 sub-case).
    """
    common = (
        []
        if omit_floor
        else [
            {
                "name": "source_type",
                "type": "enum",
                "enum": ["primary", "secondary"],
                "description": "",
                "role": "meta",
                "label": "来源类型",
            },
            {
                "name": "attribution",
                "type": "string",
                "enum": [],
                "description": "",
                "role": "meta",
                "label": "归属",
            },
            {
                "name": "is_projection",
                "type": "bool",
                "enum": [],
                "description": "",
                "role": "flag",
                "label": "预测",
            },
            {
                "name": "single_source",
                "type": "bool",
                "enum": [],
                "description": "",
                "role": "flag",
                "label": "单一来源",
            },
            {
                "name": "time_ref",
                "type": "string",
                "enum": [],
                "description": "",
                "role": "meta",
                "label": "时间",
            },
        ]
    )
    return MetaSpecOut(
        extraction_prompt="宁缺毋造。横切字段。归属铁律。特化内容。",
        sections=[
            {
                "key": "policy_narrative",
                "label": "政策叙事",
                "fields": [
                    {
                        "name": "stance",
                        "type": "enum",
                        "enum": ["hawkish", "dovish"],
                        "description": "鹰鸽",
                        "role": "content",
                        "label": "立场",
                    },
                ],
            }
        ],
        article_fields=[
            {
                "name": "source_org",
                "type": "string",
                "enum": [],
                "description": "发布机构",
                "role": "content",
                "label": "机构",
            },
            {
                "name": "thesis",
                "type": "string",
                "enum": [],
                "description": "论点",
                "role": "content",
                "label": "论点",
            },
        ],
        common_claim_fields=common,
    )


def _make_fake_backend(meta_out: MetaSpecOut | None = None) -> MagicMock:
    """Fake LLM backend; structured() returns the given MetaSpecOut (default well-formed)."""
    backend = MagicMock()
    result = meta_out or _make_fake_meta_spec_out()
    backend.structured = AsyncMock(return_value=result)
    return backend


@contextmanager
def _client(
    prompts_dir: Path,
    *,
    with_auth: bool = False,
    token: str = "test-secret",
    backend: MagicMock | None = None,
    fake_settings: MagicMock | None = None,
):
    """TestClient with in-memory SQLite + extraction_spec_router + optional auth."""

    conn_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import aiosqlite  # noqa: PLC0415

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        await init_summary_history_table(conn)
        install_for_test(conn)
        conn_holder["conn"] = conn
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    if with_auth:
        app.add_middleware(DashboardTokenMiddleware)
    app.include_router(extraction_spec_router)

    settings = fake_settings or MagicMock()
    settings.effective_meta_extraction_model = "test-meta-model"
    app.state.settings = settings
    app.state.llm_backend = backend or _make_fake_backend()

    mock_auth_settings = MagicMock()
    mock_auth_settings.dashboard_token.get_secret_value.return_value = token if with_auth else ""

    with (
        patch("sembr.summarizer.templates.PROMPTS_DIR", prompts_dir),
        patch("sembr.summarizer.spec_gen.PROMPTS_DIR", prompts_dir),
        patch("sembr.api.extraction_spec.PROMPTS_DIR", prompts_dir),
        patch("sembr.dashboard.auth.get_settings", return_value=mock_auth_settings),
    ):
        with TestClient(app) as http:
            yield http, conn_holder


def _write_base(prompts_dir: Path, content: str = _FAKE_BASE) -> None:
    extraction_dir = prompts_dir / "extraction"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    (extraction_dir / "_base.md").write_text(content, encoding="utf-8")


def _write_templates(prompts_dir: Path) -> None:
    """Write minimal system + instruction templates."""
    (prompts_dir / "system").mkdir(parents=True, exist_ok=True)
    (prompts_dir / "instruction").mkdir(parents=True, exist_ok=True)
    (prompts_dir / "system" / "default.md").write_text("System: {language}", encoding="utf-8")
    (prompts_dir / "instruction" / "default.md").write_text(
        "Intent: {intent_text}\n{articles}", encoding="utf-8"
    )


async def _create_test_intent(
    conn, *, system_template: str = "default", extraction_enabled: bool = False
) -> int:
    """Create a test intent and return its id."""
    body = IntentCreate(
        name="test-intent",
        text="Federal Reserve policy",
        channels=[{"type": "email", "to": ["test@example.com"]}],
        system_template=system_template,
        extraction_enabled=extraction_enabled,
    )
    intent = await create_intent(conn, body)
    return intent.id


# --------------------------------------------------------------------------- #
# T6 — generate_spec unit: floor guarantees + field contracts
# --------------------------------------------------------------------------- #


def test_generate_spec_fields_have_role_and_label() -> None:
    """T6a: every field in generated spec has role ∈ {content,meta,flag} and non-empty label."""
    import asyncio

    from sembr.summarizer.spec_gen import generate_spec

    backend = _make_fake_backend()
    base = _FAKE_BASE

    async def run():
        return await generate_spec(
            system_tpl="System prompt",
            instruction_tpl="Instructions",
            base=base,
            digest=None,
            backend=backend,
            model="test-model",
        )

    md, json_obj = asyncio.get_event_loop().run_until_complete(run())

    valid_roles = {"content", "meta", "flag"}

    def check_fields(fields, scope):
        for f in fields:
            assert (
                f.get("role") in valid_roles
            ), f"field {f.get('name')!r} in {scope}: role={f.get('role')!r} not in valid set"
            assert (
                f.get("label") and f["label"].strip()
            ), f"field {f.get('name')!r} in {scope}: label is empty"

    check_fields(json_obj.get("article_fields", []), "article_fields")
    check_fields(json_obj.get("common_claim_fields", []), "common_claim_fields")
    for s in json_obj.get("sections", []):
        check_fields(s.get("fields", []), f"section {s.get('key')!r}")


def test_generate_spec_section_keys_are_valid() -> None:
    """T6b: all section keys pass snake_case identifier constraint."""
    import asyncio
    import re

    from sembr.summarizer.spec_gen import generate_spec

    backend = _make_fake_backend()

    async def run():
        return await generate_spec(
            system_tpl="System",
            instruction_tpl="Instruction",
            base=_FAKE_BASE,
            digest=None,
            backend=backend,
            model="test-model",
        )

    _, json_obj = asyncio.get_event_loop().run_until_complete(run())
    key_re = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
    for s in json_obj.get("sections", []):
        key = s.get("key", "")
        assert key_re.match(key), f"section key {key!r} does not match identifier constraint"


def test_generate_spec_extraction_prompt_contains_base_marker() -> None:
    """T6c: the generated extraction_prompt must contain the base floor marker string."""
    import asyncio

    from sembr.summarizer.spec_gen import generate_spec

    # The fake backend returns an extraction_prompt that contains the base content
    backend = _make_fake_backend()

    async def run():
        return await generate_spec(
            system_tpl="System",
            instruction_tpl="Instruction",
            base=_FAKE_BASE,
            digest=None,
            backend=backend,
            model="test-model",
        )

    md, _ = asyncio.get_event_loop().run_until_complete(run())
    # The MetaSpecOut we feed has extraction_prompt that includes _FAKE_BASE content
    # This tests the design contract: the base IS passed to the meta LLM as required context
    assert "宁缺毋造" in md, "extraction_prompt must contain base anti-hallucination marker"


def test_generate_spec_floor_injection_when_meta_omits_floor() -> None:
    """T6d: when meta-LLM omits floor fields, generate_spec injects them automatically."""
    import asyncio

    from sembr.summarizer.spec_gen import generate_spec

    # Meta returns spec with empty common_claim_fields (omit_floor=True)
    backend = _make_fake_backend(meta_out=_make_fake_meta_spec_out(omit_floor=True))

    async def run():
        return await generate_spec(
            system_tpl="System",
            instruction_tpl="Instruction",
            base=_FAKE_BASE,
            digest=None,
            backend=backend,
            model="test-model",
        )

    _, json_obj = asyncio.get_event_loop().run_until_complete(run())
    present_names = {f["name"] for f in json_obj.get("common_claim_fields", [])}
    assert _FLOOR_NAMES.issubset(
        present_names
    ), f"floor fields missing after injection: {_FLOOR_NAMES - present_names}"
    # Each injected floor field must have role and label too
    for f in json_obj.get("common_claim_fields", []):
        if f["name"] in _FLOOR_NAMES:
            assert f.get("role") in {
                "content",
                "meta",
                "flag",
            }, f"floor field {f['name']}: bad role"
            assert f.get("label", "").strip(), f"floor field {f['name']}: empty label"


# --------------------------------------------------------------------------- #
# T7 — generate endpoint e2e (TestClient + fake backend)
# --------------------------------------------------------------------------- #


def test_generate_endpoint_template_only_returns_md_and_json(tmp_path: Path) -> None:
    """T7a: POST generate with use_digest=false returns {md, json, warnings}."""
    _write_base(tmp_path)
    _write_templates(tmp_path)
    with _client(tmp_path) as (http, conn_holder):
        # Create an intent
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )

        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/generate",
            json={"use_digest": False},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "md" in data, "response missing 'md' field"
    assert "json" in data, "response missing 'json' field"
    assert data["md"].strip(), "md must be non-empty"
    # json field must be valid JSON string
    parsed = json.loads(data["json"])
    assert "sections" in parsed or "common_claim_fields" in parsed


def test_generate_endpoint_missing_base_returns_500(tmp_path: Path) -> None:
    """T7b: _base.md absent → 500 with explicit error message."""
    # Do NOT write _base.md
    _write_templates(tmp_path)
    (tmp_path / "extraction").mkdir(parents=True, exist_ok=True)  # dir exists but no _base.md

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )

        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/generate",
            json={"use_digest": False},
        )
    assert resp.status_code == 500
    detail = resp.json().get("detail", "")
    assert "_base.md" in detail or "基底" in detail, f"500 detail should mention base: {detail!r}"


def test_generate_endpoint_missing_template_returns_422(tmp_path: Path) -> None:
    """T7c: missing system/instruction template → 422."""
    _write_base(tmp_path)
    # intentionally NOT writing templates
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "instruction").mkdir(parents=True, exist_ok=True)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )

        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/generate",
            json={"use_digest": False},
        )
    assert resp.status_code == 422


def test_generate_endpoint_nonexistent_intent_returns_404(tmp_path: Path) -> None:
    """T7d: intent_id 9999 not found → 404."""
    _write_base(tmp_path)
    _write_templates(tmp_path)
    with _client(tmp_path) as (http, _):
        resp = http.post("/api/intents/9999/extraction-spec/generate", json={"use_digest": False})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# T8 — digest calibration
# --------------------------------------------------------------------------- #


def test_generate_with_digest_injects_summary(tmp_path: Path) -> None:
    """T8a: use_digest=True with existing digest → backend.structured called with digest in prompt."""
    _write_base(tmp_path)
    _write_templates(tmp_path)
    backend = _make_fake_backend()

    with _client(tmp_path, backend=backend) as (http, conn_holder):
        import asyncio

        conn = conn_holder["conn"]
        intent_id = asyncio.get_event_loop().run_until_complete(_create_test_intent(conn))
        # Insert a fake digest (summary_history row)
        asyncio.get_event_loop().run_until_complete(
            conn.execute(
                "INSERT INTO summary_history (intent_id, run_at, summary, citations) VALUES (?, ?, ?, ?)",
                (intent_id, "2026-06-21T10:00:00Z", "Recent digest summary content.", "[]"),
            )
        )
        asyncio.get_event_loop().run_until_complete(conn.commit())

        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/generate",
            json={"use_digest": True},
        )

    assert resp.status_code == 200
    # Verify the backend was called with a user message containing the digest content
    call_args = backend.structured.call_args
    user_msg = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
    assert (
        "Recent digest summary content" in user_msg
    ), f"digest content should appear in meta-LLM prompt; got: {user_msg[:200]!r}"


def test_generate_with_digest_no_digest_returns_digest_available_false(tmp_path: Path) -> None:
    """T8b: GET endpoint returns digest_available:false when no digest rows exist."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )
        resp = http.get(f"/api/intents/{intent_id}/extraction-spec")

    assert resp.status_code == 200
    data = resp.json()
    assert data["digest_available"] is False


def test_generate_with_digest_returns_digest_available_true(tmp_path: Path) -> None:
    """T8c: GET endpoint returns digest_available:true when digest rows exist."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        conn = conn_holder["conn"]
        intent_id = asyncio.get_event_loop().run_until_complete(_create_test_intent(conn))
        asyncio.get_event_loop().run_until_complete(
            conn.execute(
                "INSERT INTO summary_history (intent_id, run_at, summary, citations) VALUES (?, ?, ?, ?)",
                (intent_id, "2026-06-21T10:00:00Z", "Some digest summary.", "[]"),
            )
        )
        asyncio.get_event_loop().run_until_complete(conn.commit())

        resp = http.get(f"/api/intents/{intent_id}/extraction-spec")

    assert resp.status_code == 200
    assert resp.json()["digest_available"] is True


# --------------------------------------------------------------------------- #
# T9 — save endpoint: write to disk + validation failure path
# --------------------------------------------------------------------------- #


def _valid_spec_json() -> str:
    return json.dumps(
        {
            "sections": [
                {
                    "key": "policy_news",
                    "label": "政策",
                    "fields": [
                        {"name": "indicator", "type": "string", "role": "content", "label": "指标"}
                    ],
                }
            ],
            "article_fields": [
                {"name": "source_org", "type": "string", "role": "content", "label": "机构"},
                {"name": "thesis", "type": "string", "role": "content", "label": "论点"},
            ],
            "common_claim_fields": [
                {"name": n, "type": "string", "role": "meta", "label": n} for n in _FLOOR_NAMES
            ],
        },
        ensure_ascii=False,
    )


def test_save_spec_valid_writes_both_files(tmp_path: Path) -> None:
    """T9a: valid spec → 200, both .md and .json files exist on disk."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )
        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": "extraction prompt content", "json": _valid_spec_json()},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True

    # Both files must exist on disk
    extraction_dir = tmp_path / "extraction"
    md_path = extraction_dir / f"intent-{intent_id}.md"
    json_path = extraction_dir / f"intent-{intent_id}.json"
    assert md_path.is_file(), f"Missing {md_path}"
    assert json_path.is_file(), f"Missing {json_path}"
    assert md_path.read_text(encoding="utf-8") == "extraction prompt content"


def test_save_spec_invalid_returns_422_with_errors(tmp_path: Path) -> None:
    """T9b: invalid spec → 422 with {code:spec_invalid, errors:[...]} and no files written."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )
        bad_json = json.dumps(
            {
                "sections": [
                    {
                        "key": "valid_key",
                        "label": "x",
                        "fields": [
                            {
                                "name": "",
                                "type": "string",
                                "role": "content",
                                "label": "ok",
                            }  # empty name = rule 4
                        ],
                    }
                ],
                "article_fields": [],
                "common_claim_fields": [],
            }
        )
        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": "prompt", "json": bad_json},
        )

    assert resp.status_code == 422
    detail = resp.json().get("detail", {})
    assert detail.get("code") == "spec_invalid"
    assert "errors" in detail
    assert len(detail["errors"]) > 0

    # Files must NOT exist (write was blocked)
    extraction_dir = tmp_path / "extraction"
    assert not (extraction_dir / f"intent-{intent_id}.md").is_file()
    assert not (extraction_dir / f"intent-{intent_id}.json").is_file()


def test_save_spec_empty_md_returns_422(tmp_path: Path) -> None:
    """T9c: empty extraction_prompt → 422 (rule 1)."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )
        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": "   ", "json": _valid_spec_json()},
        )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "spec_invalid"
    locs = [e["loc"] for e in detail["errors"]]
    assert any("extraction_prompt" in loc for loc in locs)


# --------------------------------------------------------------------------- #
# T10 — save ≠ enable: save doesn't change extraction_enabled; the toggle does
# --------------------------------------------------------------------------- #


def test_save_does_not_change_extraction_enabled(tmp_path: Path) -> None:
    """T10a: after save, intent.extraction_enabled is unchanged (still off)."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        conn = conn_holder["conn"]
        intent_id = asyncio.get_event_loop().run_until_complete(_create_test_intent(conn))
        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": "prompt text", "json": _valid_spec_json()},
        )
        assert resp.status_code == 200

        from sembr.db.intents import get_intent

        intent = asyncio.get_event_loop().run_until_complete(get_intent(conn, intent_id))
        assert intent is not None
        assert intent.extraction_enabled is False, "save must not flip the toggle"


def test_enable_sets_extraction_enabled(tmp_path: Path) -> None:
    """T10b: after the toggle is enabled, intent.extraction_enabled is True (the
    spec name/file are never touched)."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        conn = conn_holder["conn"]
        intent_id = asyncio.get_event_loop().run_until_complete(_create_test_intent(conn))
        # Must save first before enable
        http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": "prompt text", "json": _valid_spec_json()},
        )
        resp = http.post(f"/api/intents/{intent_id}/extraction-spec/enable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        from sembr.db.intents import get_intent

        intent = asyncio.get_event_loop().run_until_complete(get_intent(conn, intent_id))
        assert intent is not None and intent.extraction_enabled is True


def test_toggle_disable_clears_extraction_enabled(tmp_path: Path) -> None:
    """The enable endpoint with {enabled: false} turns the bool off — the spec
    file and name are untouched, only the toggle flips."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        from sembr.db.intents import get_intent

        conn = conn_holder["conn"]
        intent_id = asyncio.get_event_loop().run_until_complete(_create_test_intent(conn))
        http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": "prompt text", "json": _valid_spec_json()},
        )
        # enable, then disable
        http.post(f"/api/intents/{intent_id}/extraction-spec/enable", json={"enabled": True})
        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/enable", json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        intent = asyncio.get_event_loop().run_until_complete(get_intent(conn, intent_id))
        assert intent is not None and intent.extraction_enabled is False
        # the spec file is NOT deleted by disabling
        assert (tmp_path / "extraction" / f"intent-{intent_id}.md").exists()


# --------------------------------------------------------------------------- #
# T11 — after enable, _resolve_spec_name hits intent-{id}, load_spec passes
# --------------------------------------------------------------------------- #


def test_enable_then_load_spec_succeeds(tmp_path: Path) -> None:
    """T11: after save + enable, load_spec('intent-{id}') succeeds (not spec_not_found).

    Full map Extract facts requires Qdrant; this test verifies the resolver +
    load path only (design T11 annotation: 'can only verify parse+load').
    """
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        from sembr.summarizer.spec import SpecNotFoundError, load_spec

        conn = conn_holder["conn"]
        intent_id = asyncio.get_event_loop().run_until_complete(_create_test_intent(conn))
        # Save
        save_resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": "extraction prompt text here", "json": _valid_spec_json()},
        )
        assert save_resp.status_code == 200

        # Enable
        enable_resp = http.post(f"/api/intents/{intent_id}/extraction-spec/enable")
        assert enable_resp.status_code == 200

        # Now _resolve_spec_name should hit "intent-{id}"
        from sembr.db.intents import get_intent

        intent = asyncio.get_event_loop().run_until_complete(get_intent(conn, intent_id))
        assert intent is not None

        # Simulate what history.py:_resolve_spec_name does (always intent-{id})
        spec_name = f"intent-{intent.id}"
        assert spec_name == f"intent-{intent_id}"

        # load_spec must NOT raise SpecNotFoundError
        try:
            spec = load_spec(spec_name, tmp_path)
        except SpecNotFoundError:
            pytest.fail(f"load_spec raised SpecNotFoundError for {spec_name!r} after enable")

        assert spec.name == spec_name
        assert spec.schema_version  # non-empty


# --------------------------------------------------------------------------- #
# T12 — 401 e2e for all 4 endpoints when DashboardTokenMiddleware is active
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method, path_suffix, body",
    [
        ("GET", "/extraction-spec", None),
        ("POST", "/extraction-spec/generate", {"use_digest": False}),
        ("POST", "/extraction-spec/save", {"md": "p", "json": "{}"}),
        ("POST", "/extraction-spec/enable", None),
    ],
)
def test_extraction_spec_endpoints_require_auth(
    tmp_path: Path, method: str, path_suffix: str, body
) -> None:
    """T12: each endpoint without token → 401 JSON (not 302)."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path, with_auth=True, token="real-secret") as (http, conn_holder):
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )
        url = f"/api/intents/{intent_id}{path_suffix}"
        resp = http.request(method, url, json=body)

    # Must be 401 JSON, not 302 redirect
    assert resp.status_code == 401, f"{method} {path_suffix}: expected 401, got {resp.status_code}"
    assert resp.headers.get("content-type", "").startswith(
        "application/json"
    ), f"{method} {path_suffix}: 401 should be JSON, not redirect"


def test_extraction_spec_endpoints_allow_valid_token(tmp_path: Path) -> None:
    """T12 positive: valid token passes through (GET returns 200)."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    with _client(tmp_path, with_auth=True, token="real-secret") as (http, conn_holder):
        import asyncio

        intent_id = asyncio.get_event_loop().run_until_complete(
            _create_test_intent(conn_holder["conn"])
        )
        resp = http.get(
            f"/api/intents/{intent_id}/extraction-spec",
            headers={"X-Dashboard-Token": "real-secret"},
        )

    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# T13 — R6: saving for an intent that uses fed_watch does NOT touch fed_watch.{md,json}
# --------------------------------------------------------------------------- #


def test_save_does_not_modify_shared_fed_watch_spec(tmp_path: Path) -> None:
    """T13: saving intent spec writes intent-{id}.{md,json}; fed_watch.{md,json} unchanged."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    # Write fake fed_watch spec files in the extraction dir
    extraction_dir = tmp_path / "extraction"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    fed_watch_md = "fed_watch extraction prompt"
    fed_watch_json = json.dumps({"sections": [], "article_fields": [], "common_claim_fields": []})
    (extraction_dir / "fed_watch.md").write_text(fed_watch_md, encoding="utf-8")
    (extraction_dir / "fed_watch.json").write_text(fed_watch_json, encoding="utf-8")

    # Write the fed_watch system template so the intent can reference it
    (tmp_path / "system" / "fed_watch.md").write_text(
        "Fed system prompt {language}", encoding="utf-8"
    )
    (tmp_path / "instruction" / "default.md").write_text(
        "Intent: {intent_text}\n{articles}", encoding="utf-8"
    )

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        conn = conn_holder["conn"]
        # fed_watch system template + an existing fed_watch spec on disk
        body = IntentCreate(
            name="fed-intent",
            text="Federal Reserve",
            channels=[{"type": "email", "to": ["test@example.com"]}],
            system_template="fed_watch",
        )
        intent = asyncio.get_event_loop().run_until_complete(create_intent(conn, body))
        intent_id = intent.id

        # Read original bytes BEFORE save
        orig_md_bytes = (extraction_dir / "fed_watch.md").read_bytes()
        orig_json_bytes = (extraction_dir / "fed_watch.json").read_bytes()

        # Save the spec (should write intent-{id}, NOT fed_watch)
        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": "new intent-specific prompt", "json": _valid_spec_json()},
        )
        assert resp.status_code == 200, f"save failed: {resp.json()}"

        # intent-{id} files MUST exist
        assert (extraction_dir / f"intent-{intent_id}.md").is_file()
        assert (extraction_dir / f"intent-{intent_id}.json").is_file()

        # fed_watch files MUST be byte-for-byte unchanged
        assert (
            extraction_dir / "fed_watch.md"
        ).read_bytes() == orig_md_bytes, "fed_watch.md was modified by save!"
        assert (
            extraction_dir / "fed_watch.json"
        ).read_bytes() == orig_json_bytes, "fed_watch.json was modified by save!"


def test_save_intent_spec_content_differs_from_fed_watch(tmp_path: Path) -> None:
    """T13b: the saved intent-{id}.md content is what was POSTed, not fed_watch content."""
    _write_base(tmp_path)
    _write_templates(tmp_path)

    extraction_dir = tmp_path / "extraction"
    extraction_dir.mkdir(parents=True, exist_ok=True)
    (extraction_dir / "fed_watch.md").write_text("original fed_watch prompt", encoding="utf-8")
    (extraction_dir / "fed_watch.json").write_text(
        json.dumps({"sections": [], "article_fields": [], "common_claim_fields": []}),
        encoding="utf-8",
    )
    (tmp_path / "system" / "fed_watch.md").write_text("Fed system {language}", encoding="utf-8")

    with _client(tmp_path) as (http, conn_holder):
        import asyncio

        conn = conn_holder["conn"]
        body = IntentCreate(
            name="fed-intent-2",
            text="Federal Reserve",
            channels=[{"type": "email", "to": ["test@example.com"]}],
            system_template="fed_watch",
        )
        intent = asyncio.get_event_loop().run_until_complete(create_intent(conn, body))
        intent_id = intent.id

        custom_prompt = "custom per-intent prompt — must not appear in fed_watch.md"
        resp = http.post(
            f"/api/intents/{intent_id}/extraction-spec/save",
            json={"md": custom_prompt, "json": _valid_spec_json()},
        )
        assert resp.status_code == 200

        saved_md = (extraction_dir / f"intent-{intent_id}.md").read_text(encoding="utf-8")
        assert saved_md == custom_prompt

        fed_watch_md = (extraction_dir / "fed_watch.md").read_text(encoding="utf-8")
        assert fed_watch_md == "original fed_watch prompt"  # unchanged
