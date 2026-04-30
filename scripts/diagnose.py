"""
sembr runtime diagnostic — run directly on the server (no extra deps).

Checks:
  1. Docker container health (via `docker compose ps`)
  2. /health API endpoint
  3. SQLite article pipeline counts
  4. RSS feed freshness (flags stale > 2h)
  5. Per-feed 24h throughput vs lifetime daily average (flags drop > 50%)
  6. Per-feed publish→ingest delay snapshot (flags avg > 60m)
  7. Qdrant collection vector counts
  8. Recent match activity (match_seen)
  9. Stuck / dead articles

Usage (run from project root on Mac Mini):
    python scripts/diagnose.py
    python scripts/diagnose.py --db /app/data/sembr.db        # custom DB path
    python scripts/diagnose.py --api http://localhost:8000     # custom API base
    python scripts/diagnose.py --stale-hours 1                # tighter staleness threshold
    python scripts/diagnose.py --inside-container             # skip docker compose ps
"""

import argparse
import asyncio
import json
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


# ── colour helpers ────────────────────────────────────────────────────────────

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text

OK   = lambda t: _c("32", t)   # noqa: E731
WARN = lambda t: _c("33", t)   # noqa: E731
ERR  = lambda t: _c("31", t)   # noqa: E731
BOLD = lambda t: _c("1",  t)   # noqa: E731
DIM  = lambda t: _c("2",  t)   # noqa: E731


def section(title: str) -> None:
    print(f"\n{BOLD('── ' + title + ' ' + '─' * max(0, 60 - len(title)))}")


def row(label: str, value: str, status: str = "ok") -> None:
    icon = {"ok": OK("✓"), "warn": WARN("!"), "err": ERR("✗"), "info": DIM("·")}[status]
    print(f"  {icon}  {label:<36} {value}")


# ── 1. docker compose ps ──────────────────────────────────────────────────────

def check_containers(inside: bool) -> None:
    section("Container status")
    if inside:
        row("(running inside container)", "skipped", "info")
        return
    try:
        out = subprocess.check_output(
            ["docker", "compose", "ps", "--format", "json"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # docker compose ps --format json outputs one JSON object per line
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        if not lines:
            row("docker compose ps", "no containers found", "warn")
            return
        for ln in lines:
            svc = json.loads(ln)
            name   = svc.get("Service") or svc.get("Name", "?")
            state  = svc.get("State", "?")
            health = svc.get("Health", "")
            label  = f"{name} ({health})" if health else name
            if state == "running":
                row(label, state, "ok")
            else:
                row(label, state, "err")
    except (subprocess.CalledProcessError, FileNotFoundError):
        row("docker compose", "command failed — run from project root", "warn")


# ── 2. /health API ────────────────────────────────────────────────────────────

def check_api(base: str) -> None:
    section("API /health")
    url = f"{base.rstrip('/')}/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            body = json.loads(resp.read())
            overall = body.get("status", "?")
            status = "ok" if overall == "ok" else "warn"
            row("overall", overall, status)
            for comp, info in body.get("components", {}).items():
                s = info.get("status", "?") if isinstance(info, dict) else str(info)
                row(comp, s, "ok" if s == "ok" else "warn")
    except Exception as exc:
        row(url, f"UNREACHABLE — {exc}", "err")


# ── 3-7. SQLite checks ────────────────────────────────────────────────────────

def check_sqlite(db_path: str, stale_hours: float) -> None:
    if not Path(db_path).exists():
        section("SQLite")
        row(db_path, "file not found", "err")
        return

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    # ── pipeline counts ──
    section("Article pipeline (SQLite)")
    counts = {
        "feeds":            "SELECT COUNT(*) FROM feeds",
        "feed_items (dedup fingerprints)": "SELECT COUNT(*) FROM feed_items",
        "pending_articles": "SELECT COUNT(*) FROM pending_articles",
        "dead_articles":    "SELECT COUNT(*) FROM dead_articles",
        "intents":          "SELECT COUNT(*) FROM intents",
        "match_seen":       "SELECT COUNT(*) FROM match_seen",
    }
    for label, sql in counts.items():
        try:
            n = con.execute(sql).fetchone()[0]
            status = "err" if (label == "dead_articles" and n > 0) else "ok"
            row(label, str(n), status)
        except sqlite3.OperationalError as exc:
            row(label, f"query failed: {exc}", "warn")

    # ── WAL mode ──
    section("SQLite pragmas")
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    row("journal_mode", mode, "ok" if mode == "wal" else "warn")

    # ── feed freshness ──
    section(f"RSS feed freshness (stale > {stale_hours}h)")
    try:
        feeds = con.execute(
            "SELECT name, url, last_collected_at FROM feeds ORDER BY last_collected_at ASC"
        ).fetchall()
        now = datetime.now(timezone.utc)
        for f in feeds:
            name, url, last = f["name"], f["url"], f["last_collected_at"]
            if last:
                # SQLite stores as text; strip timezone if present
                ts_str = last.replace("Z", "+00:00")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    hours_ago = (now - ts).total_seconds() / 3600
                    label = f"{name[:38]}"
                    val   = f"{hours_ago:.1f}h ago  ({last[:19]})"
                    status = "err" if hours_ago > stale_hours else "ok"
                    row(label, val, status)
                except ValueError:
                    row(name[:38], f"unparseable timestamp: {last}", "warn")
            else:
                row(f["name"][:38], "never collected", "warn")
    except sqlite3.OperationalError as exc:
        row("feeds", f"query failed: {exc}", "warn")

    # ── per-feed 24h throughput vs lifetime daily average ──
    section("Per-feed throughput (last 24h vs lifetime avg/day)")
    try:
        rows = con.execute(
            """
            SELECT f.name,
                   SUM(CASE WHEN fi.collected_at > datetime('now','-24 hours')
                            THEN 1 ELSE 0 END) AS last_24h,
                   COUNT(fi.md5)               AS total,
                   COALESCE(MIN(fi.collected_at), '')  AS first_seen
              FROM feeds f
              LEFT JOIN feed_items fi ON fi.feed_id = f.id
             GROUP BY f.id
             ORDER BY last_24h ASC
            """
        ).fetchall()
        for r in rows:
            name, last_24h, total, first_seen = r["name"], r["last_24h"], r["total"], r["first_seen"]
            if total == 0 or not first_seen:
                row(name[:38], "no items collected yet", "warn")
                continue
            try:
                first_ts = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                if first_ts.tzinfo is None:
                    first_ts = first_ts.replace(tzinfo=timezone.utc)
                age_days = max((datetime.now(timezone.utc) - first_ts).total_seconds() / 86400, 1/24)
                avg_per_day = total / age_days
            except ValueError:
                avg_per_day = 0.0
            # half of expected throughput in last 24h triggers a warning
            ratio = last_24h / avg_per_day if avg_per_day > 0 else 1.0
            status = "err" if (avg_per_day >= 1 and ratio < 0.5) else "ok"
            val = f"{last_24h} / 24h    (avg {avg_per_day:.1f}/day, total {total})"
            row(name[:38], val, status)
    except sqlite3.OperationalError as exc:
        row("feed_items", f"query failed: {exc}", "warn")

    # ── per-feed end-to-end delay snapshot (currently-pending articles) ──
    section("Per-feed publish→ingest delay (currently pending)")
    try:
        rows = con.execute(
            """
            SELECT f.name,
                   COUNT(p.md5) AS n,
                   AVG((julianday(p.created_at) - julianday(p.published_at)) * 1440) AS avg_min,
                   MAX((julianday(p.created_at) - julianday(p.published_at)) * 1440) AS max_min
              FROM feeds f
              JOIN pending_articles p ON p.feed_id = f.id
             WHERE p.published_at IS NOT NULL
             GROUP BY f.id
             ORDER BY avg_min DESC
            """
        ).fetchall()
        if not rows:
            row("delay snapshot", "no pending articles with published_at", "info")
        else:
            for r in rows:
                name, n, avg_min, max_min = r["name"], r["n"], r["avg_min"], r["max_min"]
                if avg_min is None:
                    continue
                # > 60 min average suggests poll interval too long or upstream lag
                status = "warn" if avg_min > 60 else "ok"
                val = f"{n} pending  avg={avg_min:.0f}m  max={max_min:.0f}m"
                row(name[:38], val, status)
    except sqlite3.OperationalError as exc:
        row("pending_articles delay", f"query failed: {exc}", "warn")

    # ── stuck pending articles (retry > 0) ──
    section("Stuck pending articles (retry_count > 0)")
    try:
        stuck = con.execute(
            "SELECT feed_id, title, retry_count, created_at "
            "FROM pending_articles WHERE retry_count > 0 "
            "ORDER BY retry_count DESC LIMIT 10"
        ).fetchall()
        if not stuck:
            row("stuck articles", "none", "ok")
        else:
            for s in stuck:
                title = (s["title"] or "")[:40]
                row(title, f"retry={s['retry_count']}  created={s['created_at'][:19]}", "warn")
    except sqlite3.OperationalError as exc:
        row("pending_articles", f"query failed: {exc}", "warn")

    # ── recent matches ──
    section("Recent matches (match_seen, last 5)")
    try:
        matches = con.execute(
            "SELECT intent_id, article_id, first_matched_at "
            "FROM match_seen ORDER BY first_matched_at DESC LIMIT 5"
        ).fetchall()
        if not matches:
            row("match_seen", "no matches yet", "info")
        else:
            for m in matches:
                row(
                    f"intent={m['intent_id'][:8]}…",
                    f"article={m['article_id'][:8]}…  at {m['first_matched_at'][:19]}",
                    "ok",
                )
    except sqlite3.OperationalError as exc:
        row("match_seen", f"query failed: {exc}", "warn")

    con.close()


# ── 5. Qdrant vector counts ───────────────────────────────────────────────────

async def check_qdrant_async() -> list[tuple[str, str, str]]:
    try:
        from qdrant_client import AsyncQdrantClient  # type: ignore
    except ImportError:
        return [("qdrant_client", "not importable (run inside container)", "warn")]

    results: list[tuple[str, str, str]] = []
    try:
        q = AsyncQdrantClient(host="qdrant", port=6333)
        colls = await q.get_collections()
        if not colls.collections:
            results.append(("collections", "none found", "warn"))
        for c in colls.collections:
            info = await q.get_collection(c.name)
            n = info.points_count or 0
            results.append((c.name, f"{n:,} vectors", "ok" if n > 0 else "warn"))
        await q.close()
    except Exception as exc:
        results.append(("qdrant", f"connection failed — {exc}", "err"))
    return results


def check_qdrant() -> None:
    section("Qdrant vector counts")
    results = asyncio.run(check_qdrant_async())
    for label, val, status in results:
        row(label, val, status)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="sembr runtime diagnostic")
    parser.add_argument("--db",               default="/app/data/sembr.db",
                        help="Path to sembr.db (default: /app/data/sembr.db)")
    parser.add_argument("--api",              default="http://localhost:8000",
                        help="API base URL (default: http://localhost:8000)")
    parser.add_argument("--stale-hours",      type=float, default=2.0,
                        help="Hours without collection before marking a feed stale (default: 2)")
    parser.add_argument("--inside-container", action="store_true",
                        help="Skip 'docker compose ps' (run from inside the api container)")
    args = parser.parse_args()

    print(BOLD(f"\nsembr diagnostic — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"))
    print(DIM(f"  db={args.db}  api={args.api}"))

    check_containers(args.inside_container)
    check_api(args.api)
    check_sqlite(args.db, args.stale_hours)
    check_qdrant()

    print()


if __name__ == "__main__":
    main()
