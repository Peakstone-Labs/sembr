"""Event grouping: cluster match articles by title similarity.

Uses difflib.SequenceMatcher (stdlib) with union-find merging to handle
transitive similarity (A~B and B~C → all three in one group).

Cost: O(n²) ratio() calls per intent per tick. Worst case bounded by
matcher's _SEARCH_LIMIT=100 ⇒ ≤4950 comparisons (<10ms in practice).
Design.md target was n≤20; n=100 is the safety ceiling.
"""
from __future__ import annotations

import re
import string
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sembr.matcher.callback import Match


def _normalize(title: str) -> str:
    """Lowercase + strip punctuation for stable ratio comparison."""
    title = title.lower()
    title = title.translate(str.maketrans("", "", string.punctuation))
    return re.sub(r"\s+", " ", title).strip()


class _UnionFind:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


class GroupingStep:
    def __init__(self, threshold: float = 0.85) -> None:
        self.threshold = threshold

    def group(self, matches: list[Match]) -> list[list[Match]]:
        n = len(matches)
        if n == 0:
            return []

        normalized = [_normalize(m.payload.get("title", "")) for m in matches]
        uf = _UnionFind(n)

        for i in range(n):
            for j in range(i + 1, n):
                ratio = SequenceMatcher(None, normalized[i], normalized[j]).ratio()
                if ratio >= self.threshold:
                    uf.union(i, j)

        groups: dict[int, list[Match]] = {}
        for i, m in enumerate(matches):
            root = uf.find(i)
            groups.setdefault(root, []).append(m)

        return list(groups.values())
