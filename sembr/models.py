"""Domain models."""
from __future__ import annotations

import re
from typing import Annotated, Literal, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sembr.notifier.email import EmailChannelConfig

_NEWSAPI_HOSTNAME_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$"
)


def _normalize_newsapi_url(s: str) -> str:
    # D11/O2-A: same algorithm as collector.newsapi.normalize_source_uri.
    # Defined here too so models.py stays import-cycle-free (collector
    # depends on models, not the other way around).
    out = s.strip().lower()
    for prefix in ("https://", "http://"):
        if out.startswith(prefix):
            out = out[len(prefix):]
    if out.startswith("www."):
        out = out[4:]
    return out.rstrip("/")

# Discriminated union of all known channel configs, keyed by `type`.
# Single-element today; when a second channel ships, wrap with
# `Annotated[Union[EmailChannelConfig, TelegramChannelConfig], Field(discriminator="type")]`.
ChannelConfig = EmailChannelConfig


class CronSchedule(BaseModel):
    mode: Literal["cron"] = "cron"
    preset: Literal["daily", "weekly", "hourly"]
    hour: int = Field(ge=0, le=23, default=0)
    minute: int = Field(ge=0, le=59, default=0)
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | None = None
    lookback_seconds: int = Field(default=86400, ge=300, le=2592000)
    skip_seen: bool = True

    @model_validator(mode="after")
    def _weekday_constraint(self) -> "CronSchedule":
        if self.preset == "weekly" and self.weekday is None:
            raise ValueError("weekday is required when preset='weekly'")
        if self.preset != "weekly" and self.weekday is not None:
            raise ValueError("weekday must be None when preset is not 'weekly'")
        if self.preset == "hourly" and self.hour != 0:
            raise ValueError("hour is ignored for preset='hourly'; set preset='daily' to schedule a specific hour")
        return self


class EventSchedule(BaseModel):
    mode: Literal["event"] = "event"
    trigger_count: int = Field(default=3, ge=1, le=10)
    max_wait_seconds: int = Field(default=1800, ge=60, le=86400)


Schedule = Annotated[Union[CronSchedule, EventSchedule], Field(discriminator="mode")]


class FeedFilter(BaseModel):
    ids: list[int] | None = None  # None=全扫，[]=空集，[1,3]=子集


_FEED_TAG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def _normalize_feed_tags(v: list[str]) -> list[str]:
    """D10: lowercase + kebab-case, dedup, max 10. Reused by FeedCreate / FeedTagsUpdate."""
    norm: list[str] = []
    seen: set[str] = set()
    for t in v:
        t2 = t.strip().lower()
        if not _FEED_TAG_RE.fullmatch(t2):
            raise ValueError(f"invalid tag {t!r}: must match ^[a-z0-9][a-z0-9-]{{0,31}}$")
        if t2 in seen:
            continue
        seen.add(t2)
        norm.append(t2)
    return norm


class FeedCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    source_type: str = Field(default="rss")
    config: dict = Field(default_factory=dict)
    poll_interval_minutes: int = Field(default=30, ge=5, le=1440)
    tags: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _validate_url_per_source_type(self) -> "FeedCreate":
        # D11: source_type='newsapi' uses bare hostnames (matches NewsAPI.ai
        # source.uri format) and requires normalize-on-write so feeds.url's
        # UNIQUE constraint catches case/scheme/www. duplicates client-side.
        # source_type='rss' keeps the historical http(s)://-required scheme.
        if self.source_type == "newsapi":
            normalized = _normalize_newsapi_url(self.url)
            if not _NEWSAPI_HOSTNAME_RE.match(normalized):
                raise ValueError(
                    "newsapi feed url must be a hostname (e.g. 'reuters.com'); "
                    f"got {self.url!r} after normalization → {normalized!r}"
                )
            self.url = normalized
            # R6: front-end disables the poll_interval input for newsapi feeds,
            # but a stale form field or direct API caller may still send a
            # value that differs from settings. Coerce silently so feeds.url
            # row matches the global interval. Master tick reads the setting
            # directly, never this column — coercion is purely cosmetic for
            # the dashboard list. Imported lazily to keep models.py free of
            # import-time side effects (Settings reads .env on first call).
            from sembr.config import get_settings  # noqa: PLC0415
            self.poll_interval_minutes = get_settings().newsapi_poll_interval_minutes
        else:
            if not self.url.lower().startswith(("http://", "https://")):
                raise ValueError("url must start with http:// or https://")
        return self

    @field_validator("tags")
    @classmethod
    def _tag_syntax(cls, v: list[str]) -> list[str]:
        return _normalize_feed_tags(v)


class FeedTagsUpdate(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("tags")
    @classmethod
    def _tag_syntax(cls, v: list[str]) -> list[str]:
        return _normalize_feed_tags(v)


class Feed(FeedCreate):
    id: int
    enabled: bool = True
    last_collected_at: str | None
    created_at: str


class FeedUpdate(BaseModel):
    """Partial update body for PATCH /feeds/{id}.

    Only editable fields; url and source_type are intentionally absent so
    extra="forbid" causes a 422 if the client tries to send them.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=10)
    poll_interval_minutes: int | None = Field(default=None, ge=5, le=1440)
    config: dict | None = None
    enabled: bool | None = None

    @field_validator("tags")
    @classmethod
    def _tag_syntax(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return _normalize_feed_tags(v)


_TEMPLATE_IDENT_RE = re.compile(r"^(?!\.)(?!.*\.\.)[^/\\]{1,100}$")


def _validate_template_name(v: str) -> str:
    if not _TEMPLATE_IDENT_RE.match(v):
        raise ValueError(
            f"Invalid template name {v!r}: must not start with '.', "
            "must not contain '/', '\\', or '..', length 1–100."
        )
    return v


class IntentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    threshold: float = Field(default=0.75, ge=0.60, le=0.95)
    enabled: bool = True
    channels: list[ChannelConfig] = Field(min_length=1, max_length=10)
    tags: list[str] = Field(default_factory=list, max_length=10)
    schedule: Schedule = Field(default_factory=lambda: CronSchedule(preset="daily"))
    system_template: str = "default"
    instruction_template: str = "default"
    feed_filter: FeedFilter | None = None
    timezone: str = "UTC"
    language: str = "zh"

    @field_validator("tags")
    @classmethod
    def _tag_lengths(cls, v: list[str]) -> list[str]:
        for t in v:
            if not (1 <= len(t) <= 50):
                raise ValueError("tag length must be 1..50")
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"unknown timezone: {v!r}")
        return v

    @field_validator("language")
    @classmethod
    def _language_safe(cls, v: str) -> str:
        if not v:
            raise ValueError("language must not be empty")
        if len(v) > 32:
            raise ValueError("language must be ≤ 32 chars")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\- ]*", v):
            raise ValueError("language must start with a letter and contain only letters, digits, hyphens, underscores, or spaces")
        return v

    @field_validator("system_template", "instruction_template")
    @classmethod
    def _template_name_syntax(cls, v: str) -> str:
        return _validate_template_name(v)


class IntentUpdate(BaseModel):
    """All fields optional — partial update; all defaults = no-op (200 + current state)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    threshold: float | None = Field(default=None, ge=0.60, le=0.95)
    enabled: bool | None = None
    channels: list[ChannelConfig] | None = Field(default=None, min_length=1, max_length=10)
    tags: list[str] | None = Field(default=None, max_length=10)
    schedule: Schedule | None = None
    system_template: str | None = None
    instruction_template: str | None = None
    feed_filter: FeedFilter | None = None
    timezone: str | None = None
    language: str | None = None

    @field_validator("tags")
    @classmethod
    def _tag_lengths(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for t in v:
            if not (1 <= len(t) <= 50):
                raise ValueError("tag length must be 1..50")
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, KeyError):
            raise ValueError(f"unknown timezone: {v!r}")
        return v

    @field_validator("language")
    @classmethod
    def _language_safe(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v:
            raise ValueError("language must not be empty")
        if len(v) > 32:
            raise ValueError("language must be ≤ 32 chars")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\- ]*", v):
            raise ValueError("language must start with a letter and contain only letters, digits, hyphens, underscores, or spaces")
        return v

    @field_validator("system_template", "instruction_template")
    @classmethod
    def _template_name_syntax(cls, v: str | None) -> str | None:
        if v is None:
            return v
        return _validate_template_name(v)


class Intent(BaseModel):
    """Response model — server-generated fields first, then user fields, timestamps last.

    Intentionally not inheriting IntentCreate: response models don't need input validators,
    and standalone definition gives full control over JSON schema field ordering (M6).
    """

    id: int
    name: str
    text: str
    threshold: float
    enabled: bool
    channels: list[ChannelConfig]
    tags: list[str]
    schedule: Schedule
    system_template: str
    instruction_template: str
    feed_filter: FeedFilter | None
    timezone: str
    language: str
    created_at: str
    updated_at: str
