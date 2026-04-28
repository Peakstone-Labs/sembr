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

    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
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
        description="Per-request HTTP timeout in seconds for embedding calls.",
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
    llm_grouping_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="difflib title similarity threshold for grouping articles into one event.",
    )

    smtp_host: str = Field(default="", description="SMTP server hostname. Empty = email disabled.")
    smtp_port: int = Field(default=587, description="SMTP server port.")
    smtp_username: str = Field(default="", description="SMTP login username.")
    smtp_password: SecretStr = Field(default="", description="SMTP login password.")
    smtp_from: str = Field(default="", description="From address. Falls back to smtp_username if empty.")
    smtp_use_starttls: bool = Field(default=True, description="Enable STARTTLS (port 587 style).")
    smtp_use_ssl: bool = Field(default=False, description="Use SMTP_SSL instead of SMTP+STARTTLS (port 465 style).")

    display_timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA timezone used to render published_at in notifications (e.g. Asia/Shanghai, UTC, America/New_York).",
    )

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
