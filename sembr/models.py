"""Domain models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class FeedCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    source_type: str = Field(default="rss")
    config: dict = Field(default_factory=dict)
    poll_interval_minutes: int = Field(default=30, ge=5, le=1440)

    @field_validator("url")
    @classmethod
    def _scheme_ok(cls, v: str) -> str:
        if not v.lower().startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class Feed(FeedCreate):
    id: int
    last_collected_at: str | None
    created_at: str


class IntentChannel(BaseModel):
    type: Literal["telegram", "email"]
    config: dict = Field(default_factory=dict)


class IntentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=2000)
    threshold: float = Field(default=0.75, ge=0.60, le=0.95)
    enabled: bool = True
    channels: list[IntentChannel] = Field(min_length=1, max_length=10)
    tags: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("tags")
    @classmethod
    def _tag_lengths(cls, v: list[str]) -> list[str]:
        for t in v:
            if not (1 <= len(t) <= 50):
                raise ValueError("tag length must be 1..50")
        return v


class IntentUpdate(BaseModel):
    """All fields optional — partial update; all defaults = no-op (200 + current state)."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    threshold: float | None = Field(default=None, ge=0.60, le=0.95)
    enabled: bool | None = None
    channels: list[IntentChannel] | None = Field(default=None, min_length=1, max_length=10)
    tags: list[str] | None = Field(default=None, max_length=10)

    @field_validator("tags")
    @classmethod
    def _tag_lengths(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        for t in v:
            if not (1 <= len(t) <= 50):
                raise ValueError("tag length must be 1..50")
        return v


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
    channels: list[IntentChannel]
    tags: list[str]
    created_at: str
    updated_at: str
