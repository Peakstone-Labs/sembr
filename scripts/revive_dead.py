# SPDX-License-Identifier: Apache-2.0
"""Revive dead_articles back to pending_articles for re-embedding.

Use after fixing a transient downstream issue (e.g. SiliconFlow timeout) that
caused articles to be wrongly demoted to dead. Resets retry_count to 0 so the
embedder gives them a fresh shot.

Usage (run inside api container):
    docker compose exec -T api python scripts/revive_dead.py --dry-run
    docker compose exec -T api python scripts/revive_dead.py --confirm
    docker compose exec -T api python scripts/revive_dead.py --confirm --since "2026-04-30 03:00:00"
"""

import argparse
import sqlite3
import sys


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="/app/data/sembr.db")
    p.add_argument(
        "--since",
        default=None,
        help="Only revive rows with failed_at >= this timestamp (ISO format)",
    )
    p.add_argument("--confirm", action="store_true", help="Apply changes (default is dry-run)")
    p.add_argument("--dry-run", action="store_true", help="Explicit dry-run flag")
    args = p.parse_args()

    if args.confirm and args.dry_run:
        print("ERROR: --confirm and --dry-run are mutually exclusive", file=sys.stderr)
        sys.exit(2)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    where = ""
    params: tuple = ()
    if args.since:
        where = "WHERE failed_at >= ?"
        params = (args.since,)

    rows = con.execute(
        f"SELECT md5, feed_id, url, title, body, published_at, failed_at, "
        f"       substr(error_message, 1, 60) AS err "
        f"FROM dead_articles {where} ORDER BY failed_at DESC",
        params,
    ).fetchall()

    if not rows:
        print("no dead_articles match filter")
        return

    print(f"found {len(rows)} dead_articles to revive:")
    for r in rows[:10]:
        print(
            f"  {r['failed_at'][:19]}  feed={r['feed_id']:>3}  body={len(r['body']):>6}  "
            f"err={r['err']!r}  | {r['title'][:50]}"
        )
    if len(rows) > 10:
        print(f"  ... and {len(rows) - 10} more")

    if not args.confirm:
        print("\n(dry-run) pass --confirm to actually move these rows back to pending")
        return

    cur = con.cursor()
    revived = 0
    for r in rows:
        try:
            cur.execute(
                "INSERT OR IGNORE INTO pending_articles "
                "(md5, feed_id, url, title, body, published_at, retry_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, datetime('now'))",
                (r["md5"], r["feed_id"], r["url"], r["title"], r["body"], r["published_at"]),
            )
            if cur.rowcount > 0:
                cur.execute("DELETE FROM dead_articles WHERE md5 = ?", (r["md5"],))
                revived += cur.rowcount
        except sqlite3.IntegrityError as exc:
            print(f"  SKIP {r['md5'][:8]}: {exc}")

    con.commit()
    con.close()
    print(
        f"\nrevived {revived} rows from dead_articles → pending_articles (retry_count reset to 0)"
    )


if __name__ == "__main__":
    main()
