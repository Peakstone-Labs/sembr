"""
RSS feed probe script -- Phase 2 pre-flight.

Tests each feed for:
  1. HTTP reachability + status code
  2. feedparser parse success
  3. Entry count
  4. Content completeness: full text vs. truncated summary
  5. Flags feeds that likely need RSSHub full-text conversion

Chinese RSSHub routes default to rsshub.app; point --rsshub-base to a local
instance (http://localhost:1200) for reliable results.

Usage:
    pip install feedparser httpx
    python scripts/probe_rss.py
    python scripts/probe_rss.py --samples
    python scripts/probe_rss.py --rsshub-base http://localhost:1200
    python scripts/probe_rss.py --timeout 15 --min-content-len 800
"""

import argparse
import asyncio
import html
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import feedparser
import httpx

# ---------------------------------------------------------------------------
# Feed catalogue
# Each entry: (display_name, url_or_rsshub_path, is_rsshub_route)
# ---------------------------------------------------------------------------

# -- International general news (native RSS) ---------------------------------
GENERAL_INTL = [
    # Reuters: feeds.reuters.com TLS dead; RSSHub routes all 503 -- dropped
    ("BBC News",            "http://feeds.bbci.co.uk/news/rss.xml",                 False),
    ("The Guardian",        "https://www.theguardian.com/world/rss",                False),
    ("CNN Edition",         "http://rss.cnn.com/rss/edition.rss",                   False),
    ("AP News",             "/apnews/topics/apf-topnews",                           True),   # via RSSHub
    ("Al Jazeera",          "https://www.aljazeera.com/xml/rss/all.xml",            False),
    ("SCMP",                "https://www.scmp.com/rss/91/feed",                     False),
    ("NYT World",           "https://rss.nytimes.com/services/xml/rss/nyt/World.xml", False),
    ("Washington Post",     "https://feeds.washingtonpost.com/rss/world",           False),
    ("NPR News",            "https://feeds.npr.org/1001/rss.xml",                   False),
]

# -- International finance (native RSS) --------------------------------------
FINANCE_INTL = [
    # Reuters Business: same TLS issue + RSSHub 503 -- dropped
    ("Financial Times",     "https://www.ft.com/?format=rss",                      False),
    ("The Economist",       "https://www.economist.com/latest/rss.xml",            False),
    ("Bloomberg Markets",   "https://feeds.bloomberg.com/markets/news.rss",        False),
    ("WSJ World",           "https://feeds.a.dj.com/rss/RSSWorldNews.xml",         False),
    ("Nikkei Asia",         "https://asia.nikkei.com/rss/feed/nar",                False),
    ("MarketWatch",         "https://www.marketwatch.com/rss/topstories",          False),
    ("Investing.com",       "https://www.investing.com/rss/news.rss",              False),
    ("Seeking Alpha",       "https://seekingalpha.com/feed.xml",                   False),
    ("Forbes",              "https://www.forbes.com/real-time/feed/",              False),
]

# -- Chinese finance (RSSHub routes) -----------------------------------------
FINANCE_CN = [
    ("华尔街见闻",           "/wallstreetcn/news/global",                            True),
    ("财联社电报",           "/cls/telegraph",                                       True),
    ("东方财富",             "/eastmoney/news/cjyw",                                 True),  # was cjxw -> 503, try cjyw
    ("第一财经",             "/yicai/news",                                          True),
    ("36氪",                "/36kr/news/latest",                                    True),
    ("虎嗅",                "/huxiu/article",                                       True),
    ("钛媒体",              "/tmtpost/article",                                     True),  # was latest -> 503, try article
]

# -- Chinese general news (native RSS + RSSHub) ------------------------------
GENERAL_CN = [
    ("新华网",              "https://xinhuarss.xinhua.org/zh_news_2016010601/rss.xml", False),  # old URL 404, try new
    ("澎湃新闻",            "/thepaper/featured",                                   True),
    ("界面新闻",            "/jiemian/news/latest",                                 True),
    ("南方周末",            "/infzm/latest",                                        True),  # was /infzm/news/latest -> 503
]

SECTIONS = [
    ("International General News",  GENERAL_INTL),
    ("International Finance",       FINANCE_INTL),
    ("Chinese Finance (RSSHub)",    FINANCE_CN),
    ("Chinese General (RSSHub)",    GENERAL_CN),
]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    name: str
    url: str
    http_status: Optional[int] = None
    http_error: Optional[str] = None
    latency_ms: Optional[float] = None
    parse_ok: bool = False
    entry_count: int = 0
    has_content_tag: bool = False
    avg_body_len: int = 0
    max_body_len: int = 0
    sample_title: str = ""
    verdict: str = ""           # FULL | SUMMARY | STUB | EMPTY | ERROR
    needs_rsshub: bool = False
    notes: list[str] = field(default_factory=list)
    # publication cadence
    pub_span_hours: Optional[float] = None   # time between oldest and newest item
    pub_rate_per_day: Optional[float] = None # estimated articles published per day
    safe_poll_minutes: Optional[int] = None  # interval to not miss any item


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pub_cadence(entries) -> tuple[Optional[float], Optional[float], Optional[int]]:
    """Return (span_hours, rate_per_day, safe_poll_minutes) from entry timestamps."""
    import calendar
    timestamps = []
    for e in entries:
        t = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if t:
            try:
                timestamps.append(float(calendar.timegm(t)))
            except Exception:
                pass
    if len(timestamps) < 2:
        return None, None, None
    timestamps.sort()
    span_s = timestamps[-1] - timestamps[0]
    span_h = span_s / 3600
    if span_h < 0.1:
        return span_h, None, None
    rate_per_day = len(timestamps) / (span_h / 24)
    # safe interval = window_size / rate, with 20% safety margin, capped 15-1440 min
    safe_min = int((len(timestamps) / rate_per_day) * 24 * 60 * 0.8)
    safe_min = max(15, min(safe_min, 1440))
    return round(span_h, 1), round(rate_per_day, 1), safe_min


def _strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _best_body(entry) -> str:
    candidates = []
    for c in getattr(entry, "content", []):
        candidates.append(c.get("value", ""))
    candidates.append(getattr(entry, "summary", "") or "")
    stripped = [_strip_html(c) for c in candidates]
    return max(stripped, key=len) if stripped else ""


def _analyse_entries(entries) -> tuple[bool, int, int]:
    has_content = any(bool(getattr(e, "content", None)) for e in entries)
    lengths = [len(_best_body(e)) for e in entries] if entries else [0]
    avg = int(sum(lengths) / len(lengths)) if lengths else 0
    mx = max(lengths) if lengths else 0
    return has_content, avg, mx


# ---------------------------------------------------------------------------
# Core probe
# ---------------------------------------------------------------------------

def _probe_one(name: str, url: str, timeout: int, min_full_len: int) -> ProbeResult:
    result = ProbeResult(name=name, url=url)
    t0 = time.perf_counter()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; sembr-probe/0.1; "
            "+https://github.com/Peakstone-Labs/sembr)"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        result.http_status = resp.status_code
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        raw = resp.text
    except httpx.TimeoutException:
        result.http_error = "TIMEOUT"
        result.verdict = "ERROR"
        return result
    except Exception as exc:
        result.http_error = str(exc)[:120]
        result.verdict = "ERROR"
        return result

    if resp.status_code >= 400:
        result.verdict = "ERROR"
        result.notes.append(f"HTTP {resp.status_code}")
        return result

    feed = feedparser.parse(raw)
    entries = feed.get("entries", [])
    result.parse_ok = not feed.get("bozo", False) or bool(entries)
    result.entry_count = len(entries)

    if not result.parse_ok and result.entry_count == 0:
        result.verdict = "ERROR"
        result.notes.append(
            f"parse failed bozo_exc={feed.get('bozo_exception', '')}"
        )
        return result

    if result.entry_count == 0:
        result.verdict = "EMPTY"
        result.notes.append("0 entries")
        return result

    result.sample_title = getattr(entries[0], "title", "")[:80]
    has_content, avg, mx = _analyse_entries(entries[:20])
    result.has_content_tag = has_content
    result.avg_body_len = avg
    result.max_body_len = mx

    span_h, rate, safe_min = _pub_cadence(entries)
    result.pub_span_hours = span_h
    result.pub_rate_per_day = rate
    result.safe_poll_minutes = safe_min

    if avg >= min_full_len:
        result.verdict = "FULL"
    elif avg >= min_full_len // 3:
        result.verdict = "SUMMARY"
        result.needs_rsshub = True
        result.notes.append(f"avg {avg}ch -- truncated summary")
    else:
        result.verdict = "STUB"
        result.needs_rsshub = True
        result.notes.append(f"avg {avg}ch -- headline/stub only")

    if not has_content:
        result.notes.append("no <content:encoded>")

    return result


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------

async def _run_section(
    entries: list[tuple[str, str, bool]],
    rsshub_base: str,
    timeout: int,
    min_full_len: int,
) -> list[ProbeResult]:
    loop = asyncio.get_event_loop()
    tasks = []
    for name, url_or_path, is_rsshub in entries:
        url = (rsshub_base.rstrip("/") + url_or_path) if is_rsshub else url_or_path
        tasks.append(loop.run_in_executor(None, _probe_one, name, url, timeout, min_full_len))
    return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

VERDICT_LABEL = {
    "FULL":    "[OK]   ",
    "SUMMARY": "[~]    ",
    "STUB":    "[STUB] ",
    "EMPTY":   "[EMPTY]",
    "ERROR":   "[ERR]  ",
}


def _print_section(results: list[ProbeResult], title: str) -> None:
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")
    col = "{:<24} {:<6} {:<9} {:>5} {:>7} {:>7}  {}"
    print(col.format("Name", "HTTP", "Verdict", "Items", "AvgLen", "MaxLen", "Notes"))
    print("-" * 80)

    for r in results:
        label = VERDICT_LABEL.get(r.verdict, "?      ")
        http = str(r.http_status) if r.http_status else (r.http_error or "--")
        notes = "; ".join(r.notes)
        print(col.format(
            r.name[:24], http[:6], label,
            r.entry_count, r.avg_body_len, r.max_body_len,
            notes[:55],
        ))

    full  = sum(1 for r in results if r.verdict == "FULL")
    stub  = sum(1 for r in results if r.verdict in ("STUB", "SUMMARY"))
    err   = sum(1 for r in results if r.verdict == "ERROR")
    print(f"\n  FULL={full}  STUB/SUMMARY={stub}  ERROR={err}")


def _print_cadence(all_results: list[list[ProbeResult]], section_names: list[str]) -> None:
    """Polling cadence table: items, span, rate/day, recommended interval."""
    print(f"\n{'='*80}")
    print("  Publication cadence & recommended poll interval")
    print(f"{'='*80}")
    col = "{:<24} {:>6} {:>9} {:>10} {:>12}  {}"
    print(col.format("Name", "Items", "Span(h)", "Rate/day", "SafePoll(m)", "Verdict"))
    print("-" * 80)
    for results in all_results:
        for r in results:
            if r.verdict == "ERROR":
                continue
            span  = f"{r.pub_span_hours:.0f}h"  if r.pub_span_hours  is not None else "--"
            rate  = f"{r.pub_rate_per_day:.1f}"  if r.pub_rate_per_day is not None else "--"
            poll  = f"{r.safe_poll_minutes}m"    if r.safe_poll_minutes is not None else "--"
            # flag feeds where default 30-min poll may miss items
            warn = " <-- increase!" if (r.safe_poll_minutes is not None and r.safe_poll_minutes < 30) else ""
            print(col.format(r.name[:24], r.entry_count, span, rate, poll, r.verdict + warn))

    print("\n  Note: SafePoll = (window_size / rate_per_day) * 0.8, capped 15-1440 min")
    print("  If SafePoll < your configured interval, you WILL miss articles.")


def _print_samples(all_results: list[list[ProbeResult]], section_names: list[str]) -> None:
    print(f"\n{'='*80}")
    print("  Sample titles")
    print(f"{'='*80}")
    for results, name in zip(all_results, section_names):
        ok = [r for r in results if r.sample_title]
        if not ok:
            continue
        print(f"\n  [{name}]")
        for r in ok:
            print(f"    {r.name[:22]:<22}  {r.sample_title[:60]}")


def _print_summary(all_results: list[list[ProbeResult]]) -> None:
    flat = [r for section in all_results for r in section]
    full  = [r for r in flat if r.verdict == "FULL"]
    stub  = [r for r in flat if r.verdict in ("STUB", "SUMMARY")]
    err   = [r for r in flat if r.verdict == "ERROR"]

    print(f"\n{'='*80}")
    print(f"  OVERALL: {len(flat)} feeds -- FULL={len(full)}  STUB/SUMMARY={len(stub)}  ERROR={len(err)}")
    print(f"{'='*80}")

    if full:
        print("\n  Ready to use (FULL content):")
        for r in full:
            print(f"    {r.name}: {r.url}")

    if stub:
        print("\n  Need RSSHub or replacement (STUB/SUMMARY):")
        for r in stub:
            print(f"    {r.name} ({r.avg_body_len}ch): {r.url}")

    if err:
        print("\n  Unreachable / failed:")
        for r in err:
            detail = r.http_error or f"HTTP {r.http_status}"
            print(f"    {r.name} [{detail}]: {r.url}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="RSS feed probe -- sembr Phase 2")
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--min-content-len", type=int, default=600,
                        help="Avg char length threshold for FULL verdict (default 600)")
    parser.add_argument("--rsshub-base", default="https://rsshub.app",
                        help="RSSHub base URL (default rsshub.app; use http://localhost:1200 for self-hosted)")
    parser.add_argument("--samples", action="store_true", help="Print sample titles")
    parser.add_argument("--section", nargs="+",
                        choices=["intl-general", "intl-finance", "cn-finance", "cn-general"],
                        help="Probe one or more sections (default: all)")
    args = parser.parse_args()

    section_map = {
        "intl-general": SECTIONS[0],
        "intl-finance":  SECTIONS[1],
        "cn-finance":    SECTIONS[2],
        "cn-general":    SECTIONS[3],
    }
    active_sections = [section_map[s] for s in args.section] if args.section else SECTIONS
    total = sum(len(s[1]) for s in active_sections)

    print(f"\nsembr RSS probe  |  timeout={args.timeout}s  min_full={args.min_content_len}  rsshub={args.rsshub_base}")
    print(f"Probing {total} feeds across {len(active_sections)} section(s) ...\n")

    t0 = time.perf_counter()

    async def _run_all():
        return await asyncio.gather(*[
            _run_section(entries, args.rsshub_base, args.timeout, args.min_content_len)
            for _, entries in active_sections
        ])

    all_results = asyncio.run(_run_all())
    elapsed = time.perf_counter() - t0

    names = [name for name, _ in active_sections]
    for results, name in zip(all_results, names):
        _print_section(results, name)

    _print_cadence(all_results, names)
    _print_summary(all_results)

    if args.samples:
        _print_samples(all_results, names)

    print(f"\nTotal probe time: {elapsed:.1f}s")

    if any(r.verdict == "ERROR" for section in all_results for r in section):
        sys.exit(1)


if __name__ == "__main__":
    main()
