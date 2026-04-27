"""Collector abstractions."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass
class RawArticle:
    url: str
    title: str
    body: str
    content_quality: Literal["full", "summary", "stub", "title_only"]
    published_at: datetime | None
    feed_md5: str  # MD5(url + title), computed by the source


class BaseSource(ABC):
    @abstractmethod
    async def fetch(self, since: datetime | None = None) -> list[RawArticle]: ...

    @abstractmethod
    async def health(self) -> bool: ...

    @classmethod
    @abstractmethod
    def config_schema(cls) -> dict: ...
