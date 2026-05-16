# SPDX-License-Identifier: Apache-2.0
"""Application settings.

Priority chain (low → high) per CLAUDE.md "Configuration":
  defaults  <  sembr.yaml  <  .env  <  shell env vars  <  (runtime API override — out of scope here)
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


_YAML_PATH = Path("sembr.yaml")

# Single source of truth for the NEWSAPI_CATEGORIES candidate list.
# Both the Settings field validator (this module) and the dashboard schema
# (`api/settings._MULTISELECT_FIELDS`) reference this tuple; keep order
# stable so the saved CSV stays canonical across reloads.
NEWSAPI_VALID_CATEGORIES: tuple[str, ...] = (
    "Business",
    "Politics",
    "Technology",
    "Science",
    "Health",
    "Environment",
    "Sports",
    "Arts and Entertainment",
)


class _YamlSource(PydanticBaseSettingsSource):
    """Loads `sembr.yaml` from CWD; missing file → empty dict (yaml is optional)."""

    def get_field_value(self, field, field_name: str) -> tuple[Any, str, bool]:  # noqa: D401
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        if not _YAML_PATH.is_file():
            return {}
        with _YAML_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{_YAML_PATH} must parse to a mapping at top level")
        return data


class Settings(BaseSettings):
    """Application settings exposed to the dashboard and consumed by every subsystem.

    Field descriptions are user-facing — they render as the hint text under each
    setting row in the dashboard accordion, so keep them concise, English-only,
    and free of internal references (code paths, design-decision IDs, jargon).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    qdrant_url: str = Field(default="http://qdrant:6333")
    sqlite_path: str = Field(default="/app/data/sembr.db")
    embedder_backend: Literal["siliconflow"] = Field(
        default="siliconflow",
        description="Embedding backend. Only 'siliconflow' is supported in this release.",
    )
    embedder_api_base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="Base URL for the OpenAI-compatible /v1/embeddings endpoint.",
    )
    embedder_api_key: SecretStr = Field(
        default="",
        description="API key for the embeddings provider. Required to start.",
    )
    embedder_model: str = Field(
        default="BAAI/bge-m3",
        description="Model name passed to the embeddings endpoint.",
    )
    embedder_timeout_seconds: float = Field(
        default=30.0,
        description=(
            "HTTP timeout (seconds) for embedding requests. Batch calls scale "
            "the timeout up with payload size, so values below 30 mostly affect "
            "the startup probe."
        ),
    )

    llm_api_base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="Base URL for the OpenAI-compatible /v1/chat/completions endpoint.",
    )
    llm_api_key: SecretStr = Field(
        default="",
        description="API key for the LLM provider.",
    )
    llm_model: str = Field(
        default="deepseek-ai/DeepSeek-V4-Flash",
        description="Model name for LLM summarization.",
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        description="HTTP timeout (seconds) per summarization call.",
    )
    llm_max_prompt_chars: int = Field(
        default=1_500_000,
        ge=2_000,
        description=(
            "Character budget for the full LLM prompt. Tune to your model's "
            "context window — roughly 1.5M for 1M-token models, 200K for 128K, "
            "16K for 8K. Counts characters, not tokens."
        ),
    )

    smtp_host: str = Field(default="", description="SMTP server hostname. Empty = email disabled.")
    smtp_port: int = Field(default=587, description="SMTP server port.")
    smtp_username: str = Field(default="", description="SMTP login username.")
    smtp_password: SecretStr = Field(default="", description="SMTP login password.")
    smtp_from: str = Field(
        default="",
        description="From address for outgoing email. Falls back to the SMTP username if empty.",
    )
    smtp_use_starttls: bool = Field(default=True, description="Use STARTTLS (typical port 587).")
    smtp_use_ssl: bool = Field(
        default=False, description="Use SMTPS (typical port 465). Overrides STARTTLS when enabled."
    )

    display_timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA timezone for rendering timestamps in notifications (e.g. Asia/Shanghai, UTC, America/New_York).",
    )

    dashboard_token: SecretStr = Field(
        default="",
        description="Shared token required to access the dashboard. Empty = open access.",
    )
    dashboard_log_retention_days: int = Field(
        default=7,
        ge=1,
        le=90,
        description="How long (days) to keep feed-fetch and embedder-call history.",
    )
    dashboard_log_max_per_feed: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description="Max fetch records kept per feed (oldest pruned first).",
    )
    dashboard_poll_interval_seconds: int = Field(
        default=10,
        ge=2,
        le=120,
        description="How often (seconds) the dashboard refreshes its snapshot.",
    )
    dashboard_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Default log level on startup.",
    )
    dashboard_log_buffer_per_tag: int = Field(
        default=1000,
        ge=100,
        le=10000,
        description="Number of log lines kept in memory per category.",
    )

    qdrant_news_retention_days: int = Field(
        default=35,
        ge=30,
        le=365,
        description=(
            "How long (days) to keep ingested article vectors in Qdrant. "
            "Must be at least 30 to cover the maximum intent lookback window."
        ),
    )
    dead_articles_retention_days: int = Field(
        default=14,
        ge=1,
        le=180,
        description="How long (days) to keep dead-article records (failed-embedder rows kept for debugging).",
    )
    maintenance_interval_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="How often (hours) to run background cleanup jobs.",
    )

    sembr_bind_addr: str = Field(
        default="0.0.0.0",
        description=(
            "IP address the API container binds to. 0.0.0.0 = reachable from "
            "any device on the LAN; 127.0.0.1 = localhost only (use when "
            "behind a reverse proxy or for agent-only setups)."
        ),
    )
    sembr_host_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Host port mapped to the API container. Change if 8000 is already in use.",
    )

    lifespan_shutdown_timeout: float = Field(
        default=8.0,
        description="Max seconds for graceful shutdown before forcing exit. Keep below Docker's stop timeout (~10s).",
    )

    newsapi_api_key: SecretStr = Field(
        default="",
        description=(
            "NewsAPI.ai (eventregistry.org) API key. Empty = NewsAPI feeds "
            "disabled. Free tier ≈ 2000 tokens/month."
        ),
    )
    newsapi_poll_interval_minutes: int = Field(
        default=30,
        ge=5,
        le=1440,
        description=(
            "How often (minutes) to poll NewsAPI. One token per poll is "
            "shared across all NewsAPI feeds; lowering this burns the free "
            "quota faster."
        ),
    )
    newsapi_timeout_seconds: float = Field(
        default=30.0,
        description="HTTP timeout (seconds) for NewsAPI requests.",
    )
    newsapi_categories: str = Field(
        default="Business,Technology,Science,Politics",
        description=(
            "Comma-separated NewsAPI category whitelist. Valid: Business, "
            "Politics, Technology, Science, Health, Environment, Sports, "
            "Arts and Entertainment."
        ),
    )
    newsapi_max_pages: int = Field(
        default=10,
        ge=1,
        le=20,
        description=(
            "Max pages fetched per NewsAPI poll. Set to 1 to disable "
            "pagination; higher values raise the token cost of each poll."
        ),
    )
    newsapi_indexing_lag_hours: float = Field(
        default=2.0,
        ge=0.0,
        le=12.0,
        description=(
            "Watermark grace period (hours). NewsAPI indexes articles with "
            "delay (Reuters/USA Today: ~1-2h); pagination would otherwise "
            "stop at p1 before walking back far enough to catch articles "
            "freshly indexed below the cursor. Each extra hour of grace "
            "typically costs ~1 extra page (1 token) per master tick. "
            "Set to 0 to disable the grace (revert to publication-time "
            "watermark = cursor)."
        ),
    )

    @field_validator("newsapi_categories")
    @classmethod
    def _newsapi_categories_valid(cls, v: str) -> str:
        # Reject empty CSV and validate every entry is in the supported
        # 8-category set so a hand-edited .env can't silently produce
        # categoryUri=["news/FooBar"] and 0 results.
        items = [s.strip() for s in (v or "").split(",") if s.strip()]
        if not items:
            raise ValueError(
                "newsapi_categories must contain at least one category "
                "(e.g. 'Business,Technology'); empty CSV would unset the "
                "whitelist server-side"
            )
        invalid = [x for x in items if x not in NEWSAPI_VALID_CATEGORIES]
        if invalid:
            raise ValueError(
                f"newsapi_categories has invalid entries: {invalid}. "
                f"Valid: {sorted(NEWSAPI_VALID_CATEGORIES)}"
            )
        return v

    @property
    def newsapi_category_uris(self) -> list[str]:
        # CSV → ["news/Business", ...]; same csv-derived-property pattern
        # as proxy_hosts_set above.
        return [
            f"news/{name}"
            for name in (s.strip() for s in self.newsapi_categories.split(","))
            if name
        ]

    proxy_hosts: str = Field(
        default="rsshub:1200",
        description=(
            "Comma-separated host[:port] entries that fan out to many backends "
            "(e.g. RSSHub). Concurrency is tracked separately for each backend "
            "behind these proxies so they don't share a single rate limit."
        ),
    )

    @property
    def proxy_hosts_set(self) -> frozenset[str]:
        # Tolerate whitespace, trailing slashes, schemes typed by the user.
        out: set[str] = set()
        for raw in self.proxy_hosts.split(","):
            entry = raw.strip().lower()
            for prefix in ("http://", "https://"):
                if entry.startswith(prefix):
                    entry = entry[len(prefix) :]
            entry = entry.rstrip("/")
            if entry:
                out.add(entry)
        return frozenset(out)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Pydantic-settings invokes sources high-priority-first.
        # Order: shell env > .env > sembr.yaml > class defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
