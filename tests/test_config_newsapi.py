"""D12 / D25: Settings.newsapi_categories validator + category_uris property."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from sembr.config import Settings


def test_newsapi_categories_default_yields_4_uris() -> None:
    s = Settings()
    assert s.newsapi_category_uris == [
        "news/Business",
        "news/Technology",
        "news/Science",
        "news/Politics",
    ]


def test_newsapi_categories_csv_with_spaces_trimmed() -> None:
    s = Settings(newsapi_categories=" Business , Technology ")
    assert s.newsapi_category_uris == ["news/Business", "news/Technology"]


def test_newsapi_categories_empty_csv_rejected() -> None:
    """D25: empty CSV would unset categoryUri server-side → 422."""
    with pytest.raises(ValidationError):
        Settings(newsapi_categories="")


def test_newsapi_categories_whitespace_only_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(newsapi_categories="   ")


def test_newsapi_categories_multi_value() -> None:
    s = Settings(newsapi_categories="Business,Sports,Health")
    assert s.newsapi_category_uris == ["news/Business", "news/Sports", "news/Health"]


def test_newsapi_categories_with_compound_name() -> None:
    """'Arts and Entertainment' must map to a single URI without splitting."""
    s = Settings(newsapi_categories="Business,Arts and Entertainment")
    assert s.newsapi_category_uris == [
        "news/Business",
        "news/Arts and Entertainment",
    ]
