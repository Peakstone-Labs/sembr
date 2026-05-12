# SPDX-License-Identifier: Apache-2.0
"""
RSSHub full-text conversion probe -- Phase 2 pre-flight.

For feeds suspected to be truncated, this script compares the original RSS
against a known RSSHub route and reports whether RSSHub provides more content.

Usage:
    pip install feedparser httpx
    python scripts/probe_rsshub.py
    python scripts/probe_rsshub.py --rsshub-base https://rsshub.app
    python scripts/probe_rsshub.py --rsshub-base http://localhost:1200  # self-hosted
"""

import argparse
import asyncio
import html
import re
import time
from dataclasses import dataclass
from typing import Optional

import feedparser
import httpx

# ---------------------------------------------------------------------------
# Mapping: (name, original_url, rsshub_path)
# rsshub_path is appended to the RSSHub base URL.
# None = no known RSSHub route yet (mark for investigation).
# ---------------------------------------------------------------------------

RSSHUB_CANDIDATES = [
    # General news -- typically have paywalls or summary-only RSS
    (
        "Reuters Tech",
        "https://www.reutersagency.com/feed/?best-topics=tech",
        "/reuters/category/tech",
    ),
    ("BBC News", "http://feeds.bbci.co.uk/news/rss.xml", "/bbc/news"),
    ("The Guardian", "https://www.theguardian.com/uk/rss", "/theguardian/uk"),
    ("CNN Edition", "http://rss.cnn.com/rss/edition.rss", "/cnn/edition"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "/aljazeera/feed"),
    ("SCMP", "https://www.scmp.com/rss/91/feed", "/scmp/91"),
    # Finance -- most have heavy paywalls, full-text less likely via RSSHub
    ("Financial Times", "https://www.ft.com/?format=rss", "/ft/myft/daily-digest"),  # may 403
    ("The Economist", "https://www.economist.com/latest/rss.xml", "/economist/latest"),
    ("Forbes", "https://www.forbes.com/real-time/feed/", "/forbes/latest"),
    ("MarketWatch", "https://www.marketwatch.com/rss/topstories", "/marketwatch/articles"),
    ("Seeking Alpha", "https://seekingalpha.com/feed.xml", "/seekingalpha/news"),
]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class CompareResult:
    name: str
    orig_url: str
    rsshub_url: str
    orig_avg_len: int = 0
    rsshub_avg_len: int = 0
    orig_status: Optional[int] = None
    rsshub_status: Optional[int] = None
    orig_error: Optional[str] = None
    rsshub_error: Optional[str] = None
    improvement: float = 0.0  # rsshub_avg / orig_avg
    recommendation: str = ""


# ---------------------------------------------------------------------------
# Helpers (duplicated from probe_rss for standalone use)
# ---------------------------------------------------------------------------


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


def _fetch_avg_len(url: str, timeout: int) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """Returns (http_status, avg_body_len, error_str)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; sembr-probe/0.1; +https://github.com/Peakstone-Labs/sembr)"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        status = resp.status_code
        if status >= 400:
            return status, 0, f"HTTP {status}"
        feed = feedparser.parse(resp.text)
        entries = feed.get("entries", [])[:20]
        if not entries:
            return status, 0, "0 entries"
        lengths = [len(_best_body(e)) for e in entries]
        return status, int(sum(lengths) / len(lengths)), None
    except httpx.TimeoutException:
        return None, 0, "TIMEOUT"
    except Exception as exc:
        return None, 0, str(exc)[:80]


def _compare_one(
    name: str, orig_url: str, rsshub_path: str, rsshub_base: str, timeout: int
) -> CompareResult:
    rsshub_url = rsshub_base.rstrip("/") + rsshub_path
    r = CompareResult(name=name, orig_url=orig_url, rsshub_url=rsshub_url)

    r.orig_status, r.orig_avg_len, r.orig_error = _fetch_avg_len(orig_url, timeout)
    r.rsshub_status, r.rsshub_avg_len, r.rsshub_error = _fetch_avg_len(rsshub_url, timeout)

    orig = r.orig_avg_len or 1
    rsshub = r.rsshub_avg_len or 0
    r.improvement = rsshub / orig

    if r.rsshub_error:
        r.recommendation = "RSSHub route unavailable -- keep original"
    elif r.improvement >= 3.0:
        r.recommendation = "USE_RSSHUB (>=3x improvement)"
    elif r.improvement >= 1.5:
        r.recommendation = "CONSIDER_RSSHUB (1.5-3x improvement)"
    elif rsshub == 0 and r.orig_avg_len > 0:
        r.recommendation = "KEEP_ORIGINAL (RSSHub returned nothing)"
    else:
        r.recommendation = "KEEP_ORIGINAL (marginal or no improvement)"

    return r


# ---------------------------------------------------------------------------
# Async runner
# ---------------------------------------------------------------------------


async def _run_all(candidates: list[tuple], rsshub_base: str, timeout: int) -> list[CompareResult]:
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, _compare_one, name, orig, path, rsshub_base, timeout)
        for name, orig, path in candidates
    ]
    return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _print_report(results: list[CompareResult]) -> None:
    print(f"\n{'=' * 80}")
    print("  RSSHub vs Original -- full-text conversion comparison")
    print(f"{'=' * 80}")
    col = "{:<22} {:>8} {:>8} {:>6}  {}"
    print(col.format("Feed", "Orig avg", "RSSHub avg", "Ratio", "Recommendation"))
    print("-" * 80)

    for r in results:
        ratio = f"{r.improvement:.1f}x" if not r.rsshub_error else "--"
        print(
            col.format(
                r.name[:22],
                r.orig_avg_len,
                r.rsshub_avg_len if not r.rsshub_error else f"ERR",
                ratio,
                r.recommendation[:50],
            )
        )

    # Group recommendations
    use_rsshub = [r for r in results if "USE_RSSHUB" in r.recommendation]
    consider = [r for r in results if "CONSIDER_RSSHUB" in r.recommendation]
    keep = [r for r in results if "KEEP_ORIGINAL" in r.recommendation]

    print(f"\n  USE_RSSHUB={len(use_rsshub)}  CONSIDER={len(consider)}  KEEP_ORIGINAL={len(keep)}")

    if use_rsshub:
        print("\n  Feeds to route through RSSHub:")
        for r in use_rsshub:
            print(f"    - {r.name}: {r.rsshub_url}")

    if consider:
        print("\n  Feeds worth testing with self-hosted RSSHub:")
        for r in consider:
            print(f"    - {r.name} ({r.improvement:.1f}x): {r.rsshub_url}")

    # Error details
    errors = [r for r in results if r.rsshub_error or r.orig_error]
    if errors:
        print("\n  Fetch errors:")
        for r in errors:
            if r.orig_error:
                print(f"    - {r.name} ORIGINAL: {r.orig_error}")
            if r.rsshub_error:
                print(f"    - {r.name} RSSHUB: {r.rsshub_error}")

    print(f"\n  Note: RSSHub public instance (rsshub.app) may rate-limit or block")
    print(f"  For production, self-host: docker run -p 1200:1200 diygod/rsshub")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="RSSHub full-text conversion probe")
    parser.add_argument(
        "--rsshub-base",
        default="https://rsshub.app",
        help="RSSHub instance base URL (default: https://rsshub.app)",
    )
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout (default 15s)")
    args = parser.parse_args()

    print(f"\nsembr RSSHub probe  |  base={args.rsshub_base}  timeout={args.timeout}s")
    print(f"Comparing {len(RSSHUB_CANDIDATES)} feeds ...\n")

    t0 = time.perf_counter()
    results = asyncio.run(_run_all(RSSHUB_CANDIDATES, args.rsshub_base, args.timeout))
    elapsed = time.perf_counter() - t0

    _print_report(results)
    print(f"\nTotal probe time: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
