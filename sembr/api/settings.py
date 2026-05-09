"""Settings editor router — /api/settings/*.

Reads / writes the host-side `.env` file (bind-mounted to /app/.env) and
orchestrates the matching container restart(s). All endpoints require an
``X-Dashboard-Token`` **header** (no cookie fallback) to defend against
CSRF — see design.md Decision #15.

Routes:

- ``GET  /api/settings/schema``         field metadata derived from sembr.config.Settings
- ``GET  /api/settings/values``         current values with sensitive fields masked
- ``POST /api/settings/save``           atomic write + restart trigger
"""
from __future__ import annotations

import logging
import os
import secrets
import typing as _t
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, SecretStr, ValidationError
from pydantic.fields import FieldInfo

from sembr.api.settings_envfile import KEY_PATTERN, EnvFile
from sembr.api.settings_restart import RestartController
from sembr.config import NEWSAPI_VALID_CATEGORIES, Settings, get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])

# Container-side `.env` path. Compose bind-mounts host `./.env` here.
ENV_FILE_PATH = Path("/app/.env")

# RSSHub passthrough whitelist (design.md Decision #5 / O5a).
# Why prefix-matching: lets RSSHub add new sources without sembr code
# changes, but the strict ALL_CAPS regex defends against unicode-homograph
# tricks ("ＴＷＩＴＴＥＲ_COOKIE" attempting to bypass the prefix check).
_RSSHUB_PASSTHROUGH_PREFIXES: tuple[str, ...] = (
    "TWITTER_",
    "TELEGRAM_",
    "GITHUB_",
    "RSSHUB_",
    "SOCIAL_",
    "OPENAI_",
)

# Mask shown in place of any SecretStr value. The exact string also doubles
# as the sentinel: clients that submit the mask back unmodified mean
# "leave the existing secret alone" (design.md Decision #6).
SENSITIVE_MASK = "••••••"

# Substrings that mark a passthrough variable as secret-ish when no Settings
# field declares it sensitive (e.g. TWITTER_AUTH_TOKEN, GITHUB_ACCESS_TOKEN).
# Used to mask values on read and to coalesce a submitted mask back to the
# stored value on write — keep both call sites pointed at this single tuple.
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = (
    "TOKEN", "COOKIE", "SECRET", "KEY", "PASSWORD", "SESSION",
)

# Well-known RSSHub passthrough variables shown in the UI as starter rows
# even when absent from `.env`. Lets the user fill them in without first
# clicking "+ Add". Each entry must match a passthrough prefix.
_RSSHUB_RECOMMENDED: tuple[dict[str, str], ...] = (
    {"key": "TWITTER_AUTH_TOKEN",
     "description": "Twitter/X auth_token cookie value (40-char hex) for user timelines and search. Comma-separate multiple accounts."},
    {"key": "TELEGRAM_TOKEN",
     "description": "Telegram bot token (BotFather) for public channel feeds."},
    {"key": "TELEGRAM_SESSION",
     "description": "Telegram user session string (Telethon/Pyrogram) for restricted channels."},
    {"key": "GITHUB_ACCESS_TOKEN",
     "description": "GitHub PAT — raises API rate limit from 60 to 5000 req/h."},
)

# Settings fields hidden from the UI: declared in Settings but the field is
# either a single-value Literal (no real choice) or otherwise not user-editable.
_HIDDEN_FROM_UI: frozenset[str] = frozenset({"EMBEDDER_BACKEND"})


# ── auth ──────────────────────────────────────────────────────────────────


def require_header_token(
    x_dashboard_token: Annotated[str | None, Header(alias="X-Dashboard-Token")] = None,
) -> None:
    """Header-only auth dependency for /api/settings/*.

    Why a separate dependency rather than reusing DashboardTokenMiddleware:
    the middleware accepts cookies, which makes settings POSTs trivially
    CSRF-able from any logged-in browser tab (design.md Decision #15). This
    dep enforces an explicit ``X-Dashboard-Token`` header — a value an
    attacker on a different origin cannot inject from a cross-site form/fetch.
    """
    expected = get_settings().dashboard_token.get_secret_value()
    if not expected:
        # No token configured → middleware also treats every request as
        # public; mirror that contract here so dev mode stays usable.
        return
    if not x_dashboard_token or not secrets.compare_digest(x_dashboard_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


# ── schema introspection ─────────────────────────────────────────────────


class SembrFieldMeta(BaseModel):
    key: str
    type: Literal["str", "int", "float", "bool", "secret", "enum", "multiselect", "path"]
    sensitive: bool
    description: str = ""
    enum: list[str] | None = None
    ge: float | None = None
    le: float | None = None
    default: Any | None = None


# D13/O3-A: fields rendered as multi-select checkboxes in the dashboard. Key
# is the lower-case Settings field name; value is the candidate list. The
# field is still stored as a CSV string in .env (see Settings.newsapi_categories
# / proxy_hosts pattern), and the frontend joins selections with ',' on submit.
# Source of truth for the candidate list lives in `sembr.config` so both this
# module and the Settings field validator stay aligned (review-loop1 🟡-2).
_MULTISELECT_FIELDS: dict[str, list[str]] = {
    "newsapi_categories": list(NEWSAPI_VALID_CATEGORIES),
}


class PassthroughRecommended(BaseModel):
    key: str
    description: str = ""


class SchemaResponse(BaseModel):
    sembr_fields: list[SembrFieldMeta]
    passthrough_prefixes: list[str]
    passthrough_recommended: list[PassthroughRecommended]


def _is_sensitive(field_info: FieldInfo) -> bool:
    """Return True if the field's annotation is (Optional[]) SecretStr."""
    annotation = field_info.annotation
    if annotation is SecretStr:
        return True
    # Handle Optional[SecretStr] / Union[SecretStr, None]
    args = _t.get_args(annotation)
    return SecretStr in args if args else False


def _enum_values(field_info: FieldInfo) -> list[str] | None:
    annotation = field_info.annotation
    origin = _t.get_origin(annotation)
    if origin is Literal:
        return [str(v) for v in _t.get_args(annotation)]
    return None


def _field_type(field_info: FieldInfo) -> str:
    if _is_sensitive(field_info):
        return "secret"
    if _enum_values(field_info) is not None:
        return "enum"
    annotation = field_info.annotation
    if annotation is bool:
        return "bool"
    if annotation is int:
        return "int"
    if annotation is float:
        return "float"
    if annotation is Path:
        return "path"
    return "str"


def _field_constraints(field_info: FieldInfo) -> tuple[float | None, float | None]:
    """Pull ge/le constraints out of pydantic v2 metadata."""
    ge: float | None = None
    le: float | None = None
    for m in field_info.metadata:
        if hasattr(m, "ge") and m.ge is not None:
            ge = float(m.ge)
        if hasattr(m, "le") and m.le is not None:
            le = float(m.le)
    return ge, le


def _build_field_meta(name: str, field_info: FieldInfo) -> SembrFieldMeta:
    ge, le = _field_constraints(field_info)
    default = field_info.default
    # Don't leak SecretStr default ("" usually but defensive)
    if _is_sensitive(field_info):
        default = ""
    elif isinstance(default, Path):
        default = str(default)
    field_type: str = _field_type(field_info)
    enum_values = _enum_values(field_info)
    # D13: multiselect overrides the inferred 'str' type. Sourced from a
    # backend-side dict so the candidate list stays in one place — the
    # frontend renders checkboxes from `enum`, posts back a CSV.
    if name in _MULTISELECT_FIELDS:
        field_type = "multiselect"
        enum_values = list(_MULTISELECT_FIELDS[name])
    return SembrFieldMeta(
        key=name.upper(),
        type=field_type,  # type: ignore[arg-type]
        sensitive=_is_sensitive(field_info),
        description=field_info.description or "",
        enum=enum_values,
        ge=ge,
        le=le,
        default=default,
    )


def _sembr_field_metas() -> list[SembrFieldMeta]:
    out: list[SembrFieldMeta] = []
    for name, fi in Settings.model_fields.items():
        if name.upper() in _HIDDEN_FROM_UI:
            continue
        out.append(_build_field_meta(name, fi))
    return out


@router.get("/schema", response_model=SchemaResponse, dependencies=[Depends(require_header_token)])
async def get_schema() -> SchemaResponse:
    return SchemaResponse(
        sembr_fields=_sembr_field_metas(),
        passthrough_prefixes=list(_RSSHUB_PASSTHROUGH_PREFIXES),
        passthrough_recommended=[PassthroughRecommended(**r) for r in _RSSHUB_RECOMMENDED],
    )


# ── values ────────────────────────────────────────────────────────────────


class UnknownKey(BaseModel):
    key: str
    value: str


class ValuesResponse(BaseModel):
    values: dict[str, str]
    overridden_by_shell_env: list[str]
    unknown_keys: list[UnknownKey]


def _is_sembr_key(key: str) -> bool:
    return key.lower() in Settings.model_fields


def _is_passthrough_key(key: str) -> bool:
    if not KEY_PATTERN.match(key):
        return False
    return any(key.startswith(p) for p in _RSSHUB_PASSTHROUGH_PREFIXES)


def _detect_shell_overrides(env_keys: list[str], envfile_values: dict[str, str]) -> list[str]:
    """Keys whose live ``os.environ`` value diverges from the `.env` value.

    docker compose's ``env_file: .env`` injects EVERY key from `.env` into the
    container's process environment, so mere presence in ``os.environ`` is not
    an override — the env_file pass-through case sees the same value on both
    sides. A real shell-time override requires the user to ``export KEY=...``
    in the shell *before* ``docker compose up``; pydantic-settings then sees
    that exported value (which differs from the disk value) and prefers it.

    pydantic-settings is case-insensitive (Settings.model_config has
    case_sensitive=False), so check both the upper and lower forms of each key.
    """
    out: list[str] = []
    for key in env_keys:
        if not _is_sembr_key(key):
            continue
        for case_key in (key.upper(), key.lower()):
            if case_key not in os.environ:
                continue
            runtime_value = os.environ[case_key]
            file_value = envfile_values.get(key, envfile_values.get(key.upper(), ""))
            if runtime_value != file_value:
                out.append(key.upper())
            break
    return out


def _envfile() -> EnvFile:
    return EnvFile.load(ENV_FILE_PATH)


@router.get("/values", response_model=ValuesResponse, dependencies=[Depends(require_header_token)])
async def get_values() -> ValuesResponse:
    try:
        ef = _envfile()
    except IsADirectoryError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="cp .env.example .env on the host then rebuild: /app/.env is a directory",
        ) from exc
    raw = ef.values()

    out_values: dict[str, str] = {}
    unknown: list[UnknownKey] = []
    sembr_keys_present: list[str] = []

    sensitive_keys = {
        name.upper() for name, fi in Settings.model_fields.items() if _is_sensitive(fi)
    }

    for key, value in raw.items():
        upper = key.upper()
        if upper in _HIDDEN_FROM_UI:
            # Hidden fields stay on disk untouched but never reach the UI —
            # otherwise the frontend (which trusts schema for sembr-key
            # identity) would mis-classify them as passthrough additions.
            continue
        if _is_sembr_key(key):
            sembr_keys_present.append(upper)
            out_values[upper] = SENSITIVE_MASK if upper in sensitive_keys and value else value
        elif _is_passthrough_key(upper):
            # Passthrough fields are also masked when they look secret-ish.
            # Without a Settings field declaring them sensitive, infer by name.
            if any(s in upper for s in _SENSITIVE_SUBSTRINGS):
                out_values[upper] = SENSITIVE_MASK if value else ""
            else:
                out_values[upper] = value
        else:
            unknown.append(UnknownKey(key=upper, value=value))

    return ValuesResponse(
        values=out_values,
        overridden_by_shell_env=_detect_shell_overrides(sembr_keys_present, raw),
        unknown_keys=unknown,
    )


# ── save ──────────────────────────────────────────────────────────────────


class SaveRequest(BaseModel):
    changes: dict[str, str] = Field(default_factory=dict)
    additions: dict[str, str] = Field(default_factory=dict)
    deletions: list[str] = Field(default_factory=list)
    confirmed: bool


class SaveResponse(BaseModel):
    saved_keys: list[str]
    deleted_keys: list[str]
    restart_targets: list[Literal["api", "rsshub"]]
    rsshub_restart_failed: bool = False
    rsshub_error: str | None = None


def _classify_key(key: str) -> Literal["sembr", "passthrough", "invalid"]:
    if _is_sembr_key(key):
        return "sembr"
    if _is_passthrough_key(key):
        return "passthrough"
    return "invalid"


def _coalesce_value(key: str, submitted: str, current_raw: str) -> str:
    """If user submitted the mask sentinel for a sensitive field, keep the
    original disk value untouched (design.md Decision #6 / AC4)."""
    sensitive = (
        (key.lower() in Settings.model_fields and _is_sensitive(Settings.model_fields[key.lower()]))
        or any(s in key for s in _SENSITIVE_SUBSTRINGS)
    )
    if sensitive and submitted == SENSITIVE_MASK:
        return current_raw
    return submitted


def _validate_proposed_settings(ef: EnvFile) -> None:
    """Dry-run ``Settings(**proposed)`` against the in-memory EnvFile state
    so a save that would crash the next force-recreate is rejected with
    422 instead of being persisted.

    Why: ``/api/settings/save`` writes ``.env`` then triggers a force-recreate.
    pydantic-settings validators (e.g. ``newsapi_categories`` enum membership,
    ``newsapi_poll_interval_minutes`` ge/le) only fire at ``Settings()``
    construction time, which is during the NEW container's lifespan startup.
    A bad value would land on disk → new container fails to boot →
    ``restart: unless-stopped`` loops. Catch it here, before persist.

    Sembr-class keys from the proposed envfile go in as ``init_settings``
    kwargs (highest priority in the 5-level chain), so they override any
    stale value still in ``os.environ``. Passthrough keys are not
    ``Settings`` fields and are excluded. Sensitive fields receive the
    coalesced disk value (real secret, not the SENSITIVE_MASK sentinel),
    so SecretStr validators see the real input.
    """
    proposed = ef.values()
    sembr_kwargs: dict[str, Any] = {}
    for upper_key, value in proposed.items():
        lower = upper_key.lower()
        if lower in Settings.model_fields:
            sembr_kwargs[lower] = value
    try:
        Settings(**sembr_kwargs)
    except ValidationError as exc:
        # Surface offending fields + per-field messages. Don't pass
        # exc.errors() raw — pydantic's `ctx` field can contain
        # non-JSON-serializable ValueError instances; build a flat
        # JSON-safe summary instead.
        bad_fields: list[str] = []
        messages: list[dict[str, str]] = []
        for err in exc.errors():
            loc = err.get("loc") or ()
            field = str(loc[0]).upper() if loc else ""
            if field and field not in bad_fields:
                bad_fields.append(field)
            messages.append({"field": field, "msg": str(err.get("msg", ""))})
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "Settings validation failed; .env not written",
                "rejected_fields": bad_fields,
                "errors": messages,
            },
        ) from exc


def _build_passthrough_error_detail(bad_keys: list[str]) -> dict[str, Any]:
    return {
        "error": "key not in passthrough whitelist",
        "rejected_keys": bad_keys,
        "allowed_prefixes": list(_RSSHUB_PASSTHROUGH_PREFIXES),
        "hint": "new keys must match ^[A-Z][A-Z0-9_]*$ and begin with one of the allowed prefixes",
    }


@router.post(
    "/save",
    response_model=SaveResponse,
    dependencies=[Depends(require_header_token)],
)
async def save_settings(body: SaveRequest) -> SaveResponse:
    if not body.confirmed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="confirmed must be true",
        )

    # ── validate ──────────────────────────────────────────────────────────
    invalid_changes = [
        k.upper() for k in body.changes if _classify_key(k.upper()) == "invalid"
    ]
    invalid_additions = [
        k.upper() for k in body.additions if _classify_key(k.upper()) == "invalid"
    ]
    if invalid_changes or invalid_additions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_build_passthrough_error_detail(invalid_changes + invalid_additions),
        )

    # ── load + mutate ─────────────────────────────────────────────────────
    try:
        ef = _envfile()
    except IsADirectoryError as exc:
        logger.error("envfile is a directory: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="cp .env.example .env on the host then rebuild: /app/.env is a directory",
        ) from exc
    except Exception as exc:
        logger.error("envfile load failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to read .env: {exc}",
        ) from exc
    raw_values = ef.values()
    saved: list[str] = []
    deleted: list[str] = []
    touched_classes: set[str] = set()

    for key, val in body.changes.items():
        upper = key.upper()
        cls = _classify_key(upper)
        coalesced = _coalesce_value(upper, val, raw_values.get(upper, ""))
        if coalesced == raw_values.get(upper, "") and ef.has_key(upper):
            # Nothing actually changed (sensitive submitted as mask) — skip
            # both write and restart to avoid pointless container churn.
            continue
        ef.upsert(upper, coalesced)
        saved.append(upper)
        touched_classes.add(cls)

    for key, val in body.additions.items():
        upper = key.upper()
        # 🔴-1: writing the mask sentinel literal into a fresh KV would let a
        # garbage value reach RSSHub. Reject explicitly — the sentinel is a
        # display artifact, never a real value.
        if val == SENSITIVE_MASK:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"addition {upper}: mask sentinel '{SENSITIVE_MASK}' is not a valid value",
            )
        cls = _classify_key(upper)
        ef.upsert(upper, val)
        saved.append(upper)
        touched_classes.add(cls)

    for key in body.deletions:
        upper = key.upper()
        if ef.delete(upper):
            deleted.append(upper)
            touched_classes.add(_classify_key(upper))

    if not saved and not deleted:
        # Nothing to do — return without writing or restarting.
        return SaveResponse(saved_keys=[], deleted_keys=[], restart_targets=[])

    # ── validate proposed state (before persist) ─────────────────────────
    # Catches "user typed an invalid value in the UI" before .env hits disk;
    # otherwise the bad value would only surface during the next
    # force-recreate's Settings() construction → uvicorn fails to boot →
    # restart loop. See _validate_proposed_settings docstring.
    if "sembr" in touched_classes:
        _validate_proposed_settings(ef)

    # ── persist ───────────────────────────────────────────────────────────
    try:
        ef.save()
    except Exception as exc:
        logger.error("envfile save failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to persist .env",
        ) from exc

    # ── decide restart targets ────────────────────────────────────────────
    restart_targets: list[Literal["api", "rsshub"]] = []
    if "sembr" in touched_classes:
        restart_targets.append("api")
    if "passthrough" in touched_classes:
        # RSSHub bind-mounts the same .env, so any passthrough change requires
        # restarting it. The api container also re-reads the env on its own
        # restart (it shares the same .env file via env_file:), so a
        # passthrough-only edit *also* implies an api restart whenever the
        # value should be observable to sembr code.
        restart_targets.append("rsshub")
        if "api" not in restart_targets:
            restart_targets.append("api")

    # ── trigger restart ───────────────────────────────────────────────────
    # Ordering rationale (Loop 2 🟡-1 + Loop 2 🟡-A):
    #   1. await rsshub restart first — docker SDK call typically takes 5–15s.
    #   2. THEN schedule the api self-restart. SIGTERM fires 1.5s after the
    #      schedule call, so it must come *after* awaiting rsshub: scheduling
    #      it earlier would let SIGTERM trigger uvicorn graceful shutdown
    #      while the response is still being assembled, dropping the
    #      connection before the client receives the JSON body.
    #   3. rsshub failure is downgraded to a 200 warning (rsshub_restart_failed
    #      flag) so the api self-restart still happens — disk + process state
    #      converge regardless of rsshub state. (Loop 2 🟡-1 contract.)
    rc = RestartController()
    rsshub_restart_failed = False
    rsshub_error: str | None = None
    if "rsshub" in restart_targets:
        try:
            await rc.restart_rsshub()
        except Exception as exc:
            logger.error("rsshub restart failed (continuing): %s", exc, exc_info=True)
            rsshub_restart_failed = True
            rsshub_error = str(exc)

    if "api" in restart_targets:
        rc.schedule_self_restart()

    return SaveResponse(
        saved_keys=saved,
        deleted_keys=deleted,
        restart_targets=restart_targets,
        rsshub_restart_failed=rsshub_restart_failed,
        rsshub_error=rsshub_error,
    )
