# SPDX-License-Identifier: Apache-2.0
"""Git wrapper for the KB runtime data tree (design §5.3 / F7).

The KB lives in a *nested independent* git repo under the gitignored ``data/kb/``
tree, so its day-to-day churn never touches the public sembr code repo. This
module is a thin subprocess wrapper — sync by design; async callers wrap it in
``asyncio.to_thread`` so the (small, infrequent) git calls don't block the loop.

F7 hardening:
- ``repo_path`` is configurable (default ``/app/data/kb``) so unit tests inject a
  ``tmp_path`` and exercise real ``git init``/commit instead of mocking.
- Commits never rely on a global ``user.name``/``user.email`` — each commit
  passes ``-c user.*`` inline, so it works in a fresh container / CI with no
  global git identity.
- Every call passes ``-c safe.directory=<repo>`` so a root-owned bind-mount
  doesn't trip git's dubious-ownership guard, without polluting global config.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Default container path: the bind-mounted ./data:/app/data tree (design §5.3).
DEFAULT_KB_REPO = Path("/app/data/kb")


class GitUnavailableError(RuntimeError):
    """Raised when the ``git`` binary is missing or a git call fails.

    Store callers catch this and degrade gracefully: the events.md write still
    lands (content preserved), only the version-history commit is skipped + logged
    (design R5).
    """


class GitRepo:
    def __init__(self, repo_path: Path | str = DEFAULT_KB_REPO) -> None:
        self.repo_path = Path(repo_path)

    def _base_args(self) -> list[str]:
        # safe.directory inline (not --global) handles root-owned bind-mounts
        # without mutating the host's global gitconfig.
        return ["git", "-c", f"safe.directory={self.repo_path}", "-C", str(self.repo_path)]

    def _run(
        self, *args: str, identity: tuple[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        cmd = self._base_args()
        if identity is not None:
            name, email = identity
            cmd += ["-c", f"user.name={name}", "-c", f"user.email={email}"]
        cmd += list(args)
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True)
        except FileNotFoundError as exc:  # git binary absent
            raise GitUnavailableError("git binary not found on PATH") from exc
        except subprocess.CalledProcessError as exc:
            raise GitUnavailableError(
                f"git {' '.join(args)} failed (rc={exc.returncode}): {exc.stderr.strip()[:300]}"
            ) from exc

    def ensure_init(self) -> None:
        """Create the repo dir and ``git init`` it if absent. Idempotent."""
        self.repo_path.mkdir(parents=True, exist_ok=True)
        if (self.repo_path / ".git").exists():
            return
        # init without identity; identity is supplied per-commit.
        self._run("init", "-q")

    def commit_all(self, message: str, *, name: str, email: str) -> str | None:
        """Stage everything and commit with the given identity.

        Returns the new HEAD hash, or ``None`` if there was nothing to commit
        (so an ingest that produced no net change doesn't error). Raises
        ``GitUnavailableError`` if git itself is unusable.
        """
        self.ensure_init()
        self._run("add", "-A")
        # Nothing staged → skip the commit (git would exit non-zero otherwise).
        status = self._run("status", "--porcelain")
        if not status.stdout.strip():
            return None
        self._run("commit", "-q", "-m", message, identity=(name, email))
        return self.head_hash()

    def head_hash(self) -> str | None:
        """Current HEAD short hash, or ``None`` on an empty repo (no commits yet)."""
        try:
            return self._run("rev-parse", "--short", "HEAD").stdout.strip()
        except GitUnavailableError:
            return None  # unborn HEAD (no commits) — expected before first commit
