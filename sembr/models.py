"""Domain models."""
from __future__ import annotations

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
