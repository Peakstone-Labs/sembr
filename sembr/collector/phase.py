"""Hash-based deterministic phase + jitter for per-feed scheduling.

D3: phase derived from md5(feed.id) so distribution survives process restarts.
D11: jitter = max(60, period_seconds // 30), capped at 600s.
"""
from __future__ import annotations

import hashlib


def derive_phase_seconds(feed_id: int, period_seconds: int) -> int:
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    digest = hashlib.md5(f"feed-{feed_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % period_seconds


def derive_jitter_seconds(period_seconds: int) -> int:
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    return min(600, max(60, period_seconds // 30))
