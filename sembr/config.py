"""Application settings.

Priority chain (low → high) per CLAUDE.md "Configuration":
  defaults  <  sembr.yaml  <  .env  <  shell env vars  <  (runtime API override — out of scope here)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


_YAML_PATH = Path("sembr.yaml")


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
    """Minimal Feature-1 settings surface (设计决策 #9).

    Real-feature config (RSS cadence, LLM backend, channel tokens, ...) is intentionally
    not declared here yet — extending later won't break this module's contract.
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
        description="API key for the embeddings endpoint. Required; startup fails if empty.",
    )
    embedder_model: str = Field(
        default="BAAI/bge-m3",
        description="Model name passed to the embeddings endpoint.",
    )
    embedder_timeout_seconds: float = Field(
        default=30.0,
        description=(
            "HTTP timeout for the startup probe and as the httpx client default. "
            "Note: batch embed calls compute a dynamic timeout = max(30s, total_chars/1500) "
            "in scheduler.py, so values below 30 do not tighten the batch path."
        ),
    )

    llm_api_base_url: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="Base URL for the OpenAI-compatible /v1/chat/completions endpoint.",
    )
    llm_api_key: SecretStr = Field(
        default="",
        description="API key for the LLM endpoint. Shares the SiliconFlow key by default.",
    )
    llm_model: str = Field(
        default="deepseek-ai/DeepSeek-V4-Flash",
        description="Model name passed to the LLM chat/completions endpoint.",
    )
    llm_timeout_seconds: float = Field(
        default=60.0,
        description="Per-request HTTP timeout in seconds for LLM summarization calls.",
    )
    llm_max_prompt_chars: int = Field(
        default=2_000_000,
        ge=2_000,
        description=(
            "Total prompt-side character budget for the LLM backend (system + "
            "instruction + assembled articles). The pipeline reserves ~15% for the "
            "LLM response and instruction overhead, then water-fills article bodies "
            "into the remainder — short articles stay whole, only the longest get "
            "truncated. Tune to your model's context window: 2_000_000 is generous "
            "for DeepSeek-V4-Flash (1M token ctx ≈ 2M Chinese chars / 4M English "
            "chars); drop to ~16_000 for an 8K-token local model. Characters "
            "(not tokens) so the pipeline can operate on strings; set "
            "conservatively for non-English content."
        ),
    )

    smtp_host: str = Field(default="", description="SMTP server hostname. Empty = email disabled.")
    smtp_port: int = Field(default=587, description="SMTP server port.")
    smtp_username: str = Field(default="", description="SMTP login username.")
    smtp_password: SecretStr = Field(default="", description="SMTP login password.")
    smtp_from: str = Field(default="", description="From address. Falls back to smtp_username if empty.")
    smtp_use_starttls: bool = Field(default=True, description="Enable STARTTLS (port 587 style).")
    smtp_use_ssl: bool = Field(default=False, description="Use SMTP_SSL instead of SMTP+STARTTLS (port 465 style).")

    prompts_dir: Path = Field(
        default=Path("/app/prompts"),
        description="Root directory for prompt templates. Subdirs: system/ and instruction/. Override via SEMBR_PROMPTS_DIR.",
    )

    display_timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA timezone used to render published_at in notifications (e.g. Asia/Shanghai, UTC, America/New_York).",
    )

    dashboard_token: SecretStr = Field(
        default="",
        description="Optional shared token gating /dashboard and /api/dashboard. Empty = no auth.",
    )
    dashboard_log_retention_days: int = Field(
        default=7, ge=1, le=90,
        description="Maximum age (days) of rows kept in feed_fetch_log / embed_call_log.",
    )
    dashboard_log_max_per_feed: int = Field(
        default=1000, ge=10, le=100000,
        description="Per-feed cap on retained feed_fetch_log rows; older rows pruned in FIFO order.",
    )
    dashboard_poll_interval_seconds: int = Field(
        default=10, ge=2, le=120,
        description="Frontend polling cadence; surfaced via /api/dashboard/config to the bundled JS.",
    )
    dashboard_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Default level applied to all LogBus tags on startup.",
    )
    dashboard_log_buffer_per_tag: int = Field(
        default=1000, ge=100, le=10000,
        description="Ring buffer capacity per log tag (number of log entries retained in memory).",
    )

    lifespan_shutdown_timeout: float = Field(
        default=8.0,
        description=(
            "Maximum seconds allowed for graceful lifespan shutdown before forcing exit. "
            "Set below docker stop's SIGKILL deadline (default 10s). Only applies during "
            "self-restart; normal `docker compose down` is not affected."
        ),
    )

    proxy_hosts: str = Field(
        default="rsshub:1200",
        description=(
            "Comma-separated host[:port] entries that front many backends "
            "(e.g. an RSSHub instance). For these hosts, the per-host concurrency "
            "limiter additionally segments by the first URL path segment so backends "
            "behind one proxy don't share a single semaphore. Default mirrors the "
            "docker-compose RSSHub service."
        ),
    )

    @property
    def proxy_hosts_set(self) -> frozenset[str]:
        # R7: tolerate whitespace, trailing slashes, schemes typed by the user.
        out: set[str] = set()
        for raw in self.proxy_hosts.split(","):
            entry = raw.strip().lower()
            for prefix in ("http://", "https://"):
                if entry.startswith(prefix):
                    entry = entry[len(prefix):]
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
