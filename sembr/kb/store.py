# SPDX-License-Identifier: Apache-2.0
"""KB store: per-intent events.md read/write + incremental ingest (design §4/§5/§6).

Responsibilities:
- Resolve ``data/kb/intent-<id>/<kind>.md`` paths (kind-validated against KB_KINDS).
- Atomic writes (tmp + os.replace) committed to the nested KB git repo.
- Serialize the three writers (ingest / lint / dashboard PUT) with a per-intent
  ``asyncio.Lock`` — the scheduler is AsyncIOScheduler so all writers share one
  event loop; a plain asyncio lock is sufficient (design §3.4 / F2).
- Bootstrap guard: ingest into a not-yet-built KB is skipped, never auto-built,
  so the explicit "rebuild" (pro distill, O3) stays the only cold-start path (F1).
- Key-integrity warnings for hand-edited content (F9).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from sembr.kb import KB_KINDS
from sembr.kb import merge as _merge
from sembr.kb.gitrepo import GitRepo, GitUnavailableError
from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

DEFAULT_KB_ROOT = Path("/app/data/kb")
# KB-specific size cap. NOT MAX_TEMPLATE_BYTES (64KB) — CJK is ~3 bytes/char in
# UTF-8, and a busy macro intent's 30-day active+archive can approach 64KB (F4).
MAX_KB_BYTES = 256 * 1024

# Commit identities (design §5.3 table) — author == committer, supplied inline so
# no global git identity is required.
_INGEST_IDENTITY = ("sembr-kb", "kb@sembr.local")
_LINT_IDENTITY = ("sembr-kb", "kb@sembr.local")
DASHBOARD_IDENTITY = ("dashboard", "dashboard@sembr.local")
MANUAL_LINT_IDENTITY = ("manual lint", "kb@sembr.local")


class KbSizeError(ValueError):
    """Raised when a user-supplied KB write exceeds MAX_KB_BYTES (API → 413)."""


class KbStore:
    def __init__(self, root: Path | str = DEFAULT_KB_ROOT, git: GitRepo | None = None) -> None:
        self.root = Path(root)
        self.git = git or GitRepo(self.root)
        self._locks: dict[int, asyncio.Lock] = {}

    # -- paths ------------------------------------------------------------- #

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in KB_KINDS:
            raise ValueError(f"unknown KB kind {kind!r}; expected one of {list(KB_KINDS)}")

    def path(self, intent_id: int, kind: str = "events") -> Path:
        self._check_kind(kind)
        return self.root / f"intent-{intent_id}" / f"{kind}.md"

    def _lock(self, intent_id: int) -> asyncio.Lock:
        lock = self._locks.get(intent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[intent_id] = lock
        return lock

    # -- read -------------------------------------------------------------- #

    def read(self, intent_id: int, kind: str = "events") -> str | None:
        """Return events.md content, or None if this intent's KB isn't built yet."""
        p = self.path(intent_id, kind)
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8")

    def head_hash(self) -> str | None:
        """Current KB git HEAD short hash — the optimistic-lock token for PUT (F2)."""
        try:
            return self.git.head_hash()
        except GitUnavailableError:
            return None

    # -- write ------------------------------------------------------------- #

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)  # atomic on POSIX — no half-written events.md

    def _write_and_commit(
        self,
        intent_id: int,
        content: str,
        *,
        kind: str,
        identity: tuple[str, str],
        message: str,
        enforce_size: bool,
    ) -> str | None:
        """Atomic write + git commit (NO lock — caller holds the per-intent lock).

        ``enforce_size`` raises for oversize user input (PUT); ingest passes False
        and only warns so it never drops merged content (lint compacts later, F4).
        """
        n_bytes = len(content.encode("utf-8"))
        if n_bytes > MAX_KB_BYTES:
            if enforce_size:
                raise KbSizeError(f"KB content {n_bytes} bytes exceeds MAX_KB_BYTES={MAX_KB_BYTES}")
            logger.warning(
                "kb intent-%d %s is %d bytes (> MAX_KB_BYTES=%d) — weekly lint should "
                "compact the archive section",
                intent_id,
                kind,
                n_bytes,
                MAX_KB_BYTES,
            )
        path = self.path(intent_id, kind)
        self._atomic_write(path, content)
        try:
            name, email = identity
            return self.git.commit_all(message, name=name, email=email)
        except GitUnavailableError as exc:
            # R5: content is preserved; only the version-history commit is skipped.
            logger.warning("kb commit skipped (git unavailable): %s", exc)
            return None

    async def write(
        self,
        intent_id: int,
        content: str,
        *,
        kind: str = "events",
        identity: tuple[str, str] = DASHBOARD_IDENTITY,
        message: str,
    ) -> str | None:
        """Overwrite events.md (dashboard PUT path) under the per-intent lock."""
        self._check_kind(kind)
        async with self._lock(intent_id):
            return await asyncio.to_thread(
                self._write_and_commit,
                intent_id,
                content,
                kind=kind,
                identity=identity,
                message=message,
                enforce_size=True,
            )

    # -- ingest ------------------------------------------------------------ #

    async def ingest(
        self,
        intent_id: int,
        run_at: str,
        digest_text: str,
        *,
        backend: BaseLLMBackend,
        merge_model: str | None = None,
        kind: str = "events",
    ) -> _merge.MergeStats:
        """Incrementally merge today's digest into this intent's events.md.

        Bootstrap guard (F1): if the KB isn't built yet, skip + warn — do NOT
        auto-build (cold start is the explicit rebuild path, O3). Returns merge
        stats; a skip carries ``stats.skipped``.
        """
        self._check_kind(kind)
        async with self._lock(intent_id):
            existing = self.read(intent_id, kind)
            if existing is None:
                logger.warning(
                    "kb ingest skipped: intent-%d KB not bootstrapped — use rebuild first",
                    intent_id,
                )
                return _merge.MergeStats(skipped="not_bootstrapped")

            result = await _merge.merge_digest(existing, digest_text, run_at, backend, merge_model)
            if result.stats.skipped:
                return result.stats
            if result.content == existing:
                return result.stats  # no net change — nothing to write/commit

            msg = (
                f"ingest intent-{intent_id} {kind} @ {run_at} "
                f"(+{result.stats.new} new, ~{result.stats.updated} updated, "
                f"{result.stats.archived} archived)"
            )
            await asyncio.to_thread(
                self._write_and_commit,
                intent_id,
                result.content,
                kind=kind,
                identity=_INGEST_IDENTITY,
                message=msg,
                enforce_size=False,
            )
            return result.stats

    # -- validation -------------------------------------------------------- #

    @staticmethod
    def validate_key_integrity(content: str) -> list[str]:
        """Return warnings for event-looking lines that lost their ``<!--k:-->`` key.

        Hand-edits in the modal can delete the key comment; next ingest would then
        treat the line as a brand-new event (F9). We don't reject the write (users
        must be able to save) — we surface warnings so the UI can flag them, and
        weekly lint merges the resulting near-duplicate keys.
        """
        warnings: list[str] = []
        for i, line in enumerate(content.splitlines(), 1):
            if _merge._SECTION_RE.match(line):
                continue
            if _merge._BULLET_RE.match(line) and "<!--k:" not in line:
                warnings.append(f"line {i}: event bullet missing key anchor: {line.strip()[:80]}")
        return warnings
