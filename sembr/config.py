"""Application settings.

Priority chain (low → high) per CLAUDE.md "Configuration":
  defaults  <  sembr.yaml  <  .env  <  shell env vars  <  (runtime API override — out of scope here)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field
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
    embedder_backend: Literal["docker_cpu", "host_mlx"] = Field(default="docker_cpu")

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
