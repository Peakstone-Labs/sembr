# SPDX-License-Identifier: Apache-2.0
"""Hash-based deterministic phase + jitter for per-feed scheduling.

Phase is derived from md5(feed.id) so the first-tick distribution survives
process restarts. Per-fire jitter is ``max(60, period_seconds // 30)``, capped
at 600 s, so concurrent ticks across feeds stay desynchronised.
"""

from __future__ import annotations

import hashlib


def derive_phase_seconds(feed_id: int, period_seconds: int) -> int:
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    # MD5 is a deterministic-spread function here, not a security primitive.
    # `usedforsecurity=False` keeps this importable on FIPS-mode systems.
    digest = hashlib.md5(f"feed-{feed_id}".encode("utf-8"), usedforsecurity=False).hexdigest()
    return int(digest[:8], 16) % period_seconds


def derive_jitter_seconds(period_seconds: int) -> int:
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    return min(600, max(60, period_seconds // 30))
