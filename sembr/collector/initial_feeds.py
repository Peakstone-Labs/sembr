"""Initial seed feeds loaded on first startup.

RSSHub routes use the internal docker-compose service name ``rsshub:1200``.
For local dev outside Docker, replace with ``localhost:1200``.
"""
from __future__ import annotations

INITIAL_FEEDS: list[dict] = [
    # --- International General ---
    # Reuters: feeds.reuters.com TLS dead, all RSSHub /reuters/* routes 503 — dropped
    {"name": "AP News",            "url": "http://rsshub:1200/apnews/topics/apf-topnews",             "poll_interval_minutes": 30},
    {"name": "The Guardian World", "url": "https://www.theguardian.com/world/rss",                    "poll_interval_minutes": 30},
    {"name": "SCMP",               "url": "https://www.scmp.com/rss/91/feed",                         "poll_interval_minutes": 30},
    {"name": "NYT World",          "url": "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",   "poll_interval_minutes": 60},
    {"name": "BBC News",           "url": "http://feeds.bbci.co.uk/news/rss.xml",                     "poll_interval_minutes": 30},
    {"name": "CNN Edition",        "url": "http://rss.cnn.com/rss/edition.rss",                       "poll_interval_minutes": 30},
    {"name": "Al Jazeera",         "url": "https://www.aljazeera.com/xml/rss/all.xml",                "poll_interval_minutes": 60},
    {"name": "NPR News",           "url": "https://feeds.npr.org/1001/rss.xml",                       "poll_interval_minutes": 30},
    {"name": "Washington Post",    "url": "https://feeds.washingtonpost.com/rss/world",               "poll_interval_minutes": 60},
    # --- International Finance (native RSS, no RSSHub needed) ---
    {"name": "Bloomberg Markets",  "url": "https://feeds.bloomberg.com/markets/news.rss",             "poll_interval_minutes": 60},
    {"name": "Financial Times",    "url": "https://www.ft.com/?format=rss",                           "poll_interval_minutes": 60},
    {"name": "The Economist",      "url": "https://www.economist.com/latest/rss.xml",                 "poll_interval_minutes": 1440},
    {"name": "WSJ World",          "url": "https://feeds.a.dj.com/rss/RSSWorldNews.xml",              "poll_interval_minutes": 60},
    {"name": "Nikkei Asia",        "url": "https://asia.nikkei.com/rss/feed/nar",                     "poll_interval_minutes": 60},
    {"name": "MarketWatch",        "url": "https://www.marketwatch.com/rss/topstories",               "poll_interval_minutes": 30},
    {"name": "Seeking Alpha",      "url": "https://seekingalpha.com/feed.xml",                        "poll_interval_minutes": 30},
    {"name": "Investing.com",      "url": "https://www.investing.com/rss/news.rss",                   "poll_interval_minutes": 15},
    # --- Chinese Finance (requires RSSHub sidecar at http://rsshub:1200) ---
    {"name": "华尔街见闻",           "url": "http://rsshub:1200/wallstreetcn/news/global",              "poll_interval_minutes": 30},
    {"name": "财联社电报",           "url": "http://rsshub:1200/cls/telegraph",                         "poll_interval_minutes": 30},
    {"name": "第一财经",             "url": "http://rsshub:1200/yicai/news",                            "poll_interval_minutes": 60},
    {"name": "36氪",                "url": "http://rsshub:1200/36kr/news/latest",                      "poll_interval_minutes": 30},
    {"name": "虎嗅",                "url": "http://rsshub:1200/huxiu/article",                         "poll_interval_minutes": 30},
    # --- Chinese General (requires RSSHub sidecar) ---
    {"name": "澎湃新闻",             "url": "http://rsshub:1200/thepaper/featured",                     "poll_interval_minutes": 30},
]
