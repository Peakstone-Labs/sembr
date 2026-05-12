"""Initial seed feeds loaded on first startup.

RSSHub routes use the internal docker-compose service name ``rsshub:1200``.
For local dev outside Docker, replace with ``localhost:1200``.

Selection criteria: feeds either deliver substantive body text OR carry
information-dense headlines (newsflash style where the title is the fact).

Each entry may optionally carry ``source_type`` (default ``"rss"``). The
30 NewsAPI.ai sources from ``collector.newsapi.RECOMMENDED_SOURCES`` are
appended via ``_newsapi_initial_feeds()`` so the canonical source list
stays single-source-of-truth (only the title/uri pair lives in newsapi.py).
"""

from __future__ import annotations


def _newsapi_initial_feeds() -> list[dict]:
    """Materialize the 30 RECOMMENDED_SOURCES as INITIAL_FEEDS dicts.

    poll_interval_minutes is set to 30 for cosmetic alignment with the master
    job's default cadence; the master tick reads ``Settings.newsapi_poll_interval_minutes``
    directly so the column is purely display.
    """
    from sembr.collector.newsapi import RECOMMENDED_SOURCES  # noqa: PLC0415

    return [
        {
            "name": s["title"],
            "url": s["uri"],
            "source_type": "newsapi",
            "poll_interval_minutes": 30,
        }
        for s in RECOMMENDED_SOURCES
    ]


INITIAL_FEEDS: list[dict] = [
    # --- International General ---
    {
        "name": "The Guardian World",
        "url": "https://www.theguardian.com/world/rss",
        "poll_interval_minutes": 30,
    },
    {"name": "SCMP", "url": "https://www.scmp.com/rss/91/feed", "poll_interval_minutes": 30},
    {"name": "NPR News", "url": "https://feeds.npr.org/1001/rss.xml", "poll_interval_minutes": 30},
    {
        "name": "Washington Post",
        "url": "https://feeds.washingtonpost.com/rss/world",
        "poll_interval_minutes": 60,
    },
    {
        "name": "New Yorker",
        "url": "http://rsshub:1200/newyorker/latest",
        "poll_interval_minutes": 360,
    },
    # --- International Finance ---
    {
        "name": "Bloomberg Markets",
        "url": "https://feeds.bloomberg.com/markets/news.rss",
        "poll_interval_minutes": 60,
    },
    # --- Chinese Finance (long-form) ---
    {
        "name": "华尔街见闻",
        "url": "http://rsshub:1200/wallstreetcn/news/global",
        "poll_interval_minutes": 30,
    },
    {"name": "第一财经", "url": "http://rsshub:1200/yicai/news", "poll_interval_minutes": 60},
    {
        "name": "第一财经-头条",
        "url": "http://rsshub:1200/yicai/headline",
        "poll_interval_minutes": 60,
    },
    {"name": "36氪", "url": "http://rsshub:1200/36kr/news/latest", "poll_interval_minutes": 30},
    {"name": "虎嗅", "url": "http://rsshub:1200/huxiu/article", "poll_interval_minutes": 30},
    {
        "name": "格隆汇热门文章",
        "url": "http://rsshub:1200/gelonghui/hot-article",
        "poll_interval_minutes": 60,
    },
    {
        "name": "东财-策略报告",
        "url": "http://rsshub:1200/eastmoney/report/strategyreport",
        "poll_interval_minutes": 360,
    },
    # --- Chinese Finance (newsflash; title carries the fact) ---
    {"name": "财联社电报", "url": "http://rsshub:1200/cls/telegraph", "poll_interval_minutes": 30},
    {"name": "格隆汇快讯", "url": "http://rsshub:1200/gelonghui/live", "poll_interval_minutes": 30},
    {"name": "金十-快讯", "url": "http://rsshub:1200/jin10", "poll_interval_minutes": 30},
    # --- Chinese General ---
    {
        "name": "澎湃新闻",
        "url": "http://rsshub:1200/thepaper/featured",
        "poll_interval_minutes": 30,
    },
    # --- Government / Statistics ---
    {
        "name": "国家统计局",
        "url": "http://rsshub:1200/gov/stats/sj/zxfb",
        "poll_interval_minutes": 1440,
    },
    # --- Academic ---
    {
        "name": "Nature",
        "url": "http://rsshub:1200/nature/research/nature",
        "poll_interval_minutes": 1440,
    },
    {
        "name": "Nature Biotechnology",
        "url": "http://rsshub:1200/nature/research/nbt",
        "poll_interval_minutes": 1440,
    },
    {
        "name": "Nature Neuroscience",
        "url": "http://rsshub:1200/nature/research/neuro",
        "poll_interval_minutes": 1440,
    },
    # --- Tools / Open Source ---
    {
        "name": "HelloGitHub",
        "url": "http://rsshub:1200/hellogithub/home/all",
        "poll_interval_minutes": 1440,
    },
    # --- Twitter ---
    {
        "name": "Elon Musk",
        "url": "http://rsshub:1200/twitter/user/elonmusk",
        "poll_interval_minutes": 60,
    },
]

# Append all 30 NewsAPI.ai sources. Done at module-import time so existing
# `seeded_feeds` machinery + tests treat them as ordinary INITIAL_FEEDS rows.
INITIAL_FEEDS.extend(_newsapi_initial_feeds())
