"""Pydantic v2 response models for /api/dashboard endpoints (D5 / D6)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ComponentStatus = Literal["ok", "down"]
EmbedderStatus = Literal["ok", "loading", "error"]
LastOutcome = Literal["ok", "fail", "never"]
ArticleBucket = Literal["pending", "dead", "qdrant"]


class ComponentsBlock(BaseModel):
    qdrant: ComponentStatus
    sqlite: ComponentStatus
    embedder: EmbedderStatus


class Fetch24hBlock(BaseModel):
    total: int
    ok: int
    fail: int
    last_outcome: LastOutcome
    last_error_message: str | None = None
    consecutive_failures: int
    sparkline_buckets: list[int] = Field(default_factory=list)


class FeedRow(BaseModel):
    id: int
    name: str
    url: str
    poll_interval_minutes: int
    last_collected_at: str | None = None
    fetch_24h: Fetch24hBlock


class EmbedderCalls24h(BaseModel):
    total: int
    ok: int
    fail: int
    avg_total_elapsed_ms: int
    p95_total_elapsed_ms: int
    sparkline_latency_ms: list[int] = Field(default_factory=list)


class EmbedderBlock(BaseModel):
    status: EmbedderStatus
    model_version: str | None = None
    calls_24h: EmbedderCalls24h


class ArticlesBlock(BaseModel):
    pending_count: int
    dead_count: int
    qdrant_count: int


class SnapshotResponse(BaseModel):
    schema_version: int = 1
    generated_at: str
    components: ComponentsBlock
    feeds: list[FeedRow]
    embedder: EmbedderBlock
    articles: ArticlesBlock


class FeedFetchEvent(BaseModel):
    id: int
    started_at: str
    elapsed_ms: int
    ok: bool
    items_seen: int
    items_new: int
    error_class: str | None = None
    error_message: str | None = None


class EmbedCallEvent(BaseModel):
    id: int
    started_at: str
    elapsed_ms: int
    ok: bool
    batch_size: int
    total_chars: int
    timeout_seconds: float
    error_class: str | None = None
    error_message: str | None = None


class ArticleListItem(BaseModel):
    md5: str
    feed_id: int | None = None
    title: str
    url: str
    published_at: str | None = None
    bucket: ArticleBucket
    # bucket-specific fields:
    retry_count: int | None = None
    error_message: str | None = None
    failed_at: str | None = None
    ingested_at_ts: int | None = None


class ArticleDetail(ArticleListItem):
    body: str


class ConfigResponse(BaseModel):
    poll_interval_seconds: int
    auth_required: bool


class FeedRowExtended(FeedRow):
    """Feeds-tab row: FeedRow + tags + per-feed grouping/scheduling metadata.

    Backward-compatible: existing snapshot route still returns plain FeedRow.
    """

    source_type: str = "rss"
    config: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    group_key: str
    next_run_iso: str | None = None
    created_at: str | None = None


class FeedListResponse(BaseModel):
    items: list[FeedRowExtended]
    total: int


class SourceSchemaResponse(BaseModel):
    schemas: dict[str, dict]
