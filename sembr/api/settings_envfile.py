# SPDX-License-Identifier: Apache-2.0
"""Line-level `.env` parser/writer.

Preserves comments, blank lines, and group ordering across read/modify/write
cycles. Backed by atomic tmp+rename and a single-generation `.env.bak` taken
before each write.

Why hand-rolled (not python-dotenv): `dotenv.set_key` rewrites the whole file
and drops grouping comments; `.env.example` ships with section headers
(`# ── API server ──`) that users rely on for navigation, so preserving line
order verbatim is a hard requirement (design.md O4a).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# A *strict* validation regex applied only when accepting **new** keys from
# user input (POST /save additions). Existing lines are accepted as-is even
# when the key uses lowercase or non-standard punctuation, so a hand-edited
# `.env` is never rejected on read.
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Greedy KEY=VALUE matcher. Allows leading whitespace ("export KEY=..." is
# *not* supported — sembr's compose-managed .env never uses it; reject loudly
# rather than silently mis-parsing).
_KV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

USER_ADDITIONS_HEADER = (
    "# ── User additions ──────────────────────────────────────────────────────────"
)


@dataclass(frozen=True)
class EnvLine:
    """One physical line of the `.env` file.

    Either:
      - a KV line: ``key`` set, ``raw`` is the full original text (preserved
        verbatim if untouched on write).
      - a comment / blank line: ``key is None``, ``raw`` is the original text.
    """

    raw: str
    key: str | None
    value: str | None

    @property
    def is_kv(self) -> bool:
        return self.key is not None


def _strip_inline_comment_and_quotes(raw_value: str) -> str:
    """Decode a `.env` RHS into the actual string value.

    Handles ``KEY="quoted value"`` (preserves spaces, allows `#`),
    ``KEY='single'`` (no escape processing), and unquoted ``KEY=val # comment``
    (strips trailing ``# comment``). Mirrors python-dotenv's behavior closely
    enough for the fields sembr cares about; this is parsing, not eval.
    """
    s = raw_value.strip()
    if not s:
        return ""
    if s[0] == '"':
        # Find matching closing quote (respect backslash-escape).
        out = []
        i = 1
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            if c == '"':
                return "".join(out)
            out.append(c)
            i += 1
        # Unterminated quote — fall through to raw return.
        return s
    if s[0] == "'":
        end = s.find("'", 1)
        if end == -1:
            return s
        return s[1:end]
    # Unquoted: strip first ` #` (space + hash) inline comment.
    hash_idx = s.find(" #")
    if hash_idx >= 0:
        s = s[:hash_idx].rstrip()
    # Hash-at-start-of-token treated as inline comment too.
    if "\t#" in s:
        s = s.split("\t#", 1)[0].rstrip()
    return s


def _quote_for_write(value: str) -> str:
    """Encode a string back to a `.env` RHS.

    Conservative: quote whenever the value contains anything that could be
    misparsed (whitespace, ``#``, quotes, escapes, leading/trailing spaces).
    Empty strings are written as ``KEY=`` (matches `.env.example` style).
    """
    if value == "":
        return ""
    needs_quote = any(c in value for c in (" ", "\t", "#", '"', "'", "\\", "\n", "\r"))
    if not needs_quote and not value.startswith((" ", "\t")):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


class EnvFile:
    """Read/write helper.

    Usage:
      ef = EnvFile.load(Path("/app/.env"))
      ef.upsert("KEY", "value")
      ef.delete("OBSOLETE")
      ef.save()                       # creates .env.bak then atomic rename
    """

    def __init__(self, path: Path, lines: list[EnvLine]) -> None:
        self.path = path
        self._lines = lines

    # ── construction ──────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "EnvFile":
        if not path.exists():
            return cls(path, [])
        if path.is_dir():
            # `.env` mounted as directory means the host file was missing when
            # docker compose started — surfaces here rather than as a cryptic
            # `IsADirectoryError` deeper in the stack (design.md Risk row 2).
            raise IsADirectoryError(
                f"{path} is a directory, not a file. "
                "Run `cp .env.example .env` on the host before `docker compose up`."
            )
        text = path.read_text(encoding="utf-8")
        return cls(path, cls._parse(text))

    @staticmethod
    def _parse(text: str) -> list[EnvLine]:
        out: list[EnvLine] = []
        # `splitlines(keepends=False)` drops trailing newline distinction,
        # but for write we always re-append `\n` — fine for `.env` files
        # that never have meaningful trailing whitespace runs.
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                out.append(EnvLine(raw=raw, key=None, value=None))
                continue
            m = _KV_LINE.match(raw)
            if not m:
                # Unparseable non-comment line: keep verbatim so we never
                # silently drop user content.
                out.append(EnvLine(raw=raw, key=None, value=None))
                continue
            key = m.group(1)
            value = _strip_inline_comment_and_quotes(m.group(2))
            out.append(EnvLine(raw=raw, key=key, value=value))
        return out

    # ── read API ──────────────────────────────────────────────────────────

    def values(self) -> dict[str, str]:
        """Last-wins map of all KV lines (matches shell semantics)."""
        out: dict[str, str] = {}
        for line in self._lines:
            if line.is_kv:
                out[line.key] = line.value or ""
        return out

    def keys(self) -> list[str]:
        return [ln.key for ln in self._lines if ln.is_kv and ln.key is not None]

    def has_key(self, key: str) -> bool:
        return any(ln.is_kv and ln.key == key for ln in self._lines)

    # ── mutation API ──────────────────────────────────────────────────────

    def upsert(self, key: str, value: str) -> None:
        """Update existing line in-place; otherwise append under user-additions."""
        for i, line in enumerate(self._lines):
            if line.is_kv and line.key == key:
                new_raw = f"{key}={_quote_for_write(value)}"
                self._lines[i] = EnvLine(raw=new_raw, key=key, value=value)
                return
        self._append_to_user_additions(key, value)

    def delete(self, key: str) -> bool:
        """Remove all lines for ``key``. Returns True iff any line was removed."""
        before = len(self._lines)
        self._lines = [ln for ln in self._lines if not (ln.is_kv and ln.key == key)]
        return len(self._lines) != before

    def _append_to_user_additions(self, key: str, value: str) -> None:
        # Locate (or create) the user-additions section header.
        header_idx = next(
            (
                i
                for i, ln in enumerate(self._lines)
                if ln.raw.strip() == USER_ADDITIONS_HEADER.strip()
            ),
            None,
        )
        new_kv = EnvLine(raw=f"{key}={_quote_for_write(value)}", key=key, value=value)
        if header_idx is None:
            # Trim trailing blank lines so the new header doesn't accumulate
            # blank padding on every additions cycle.
            while self._lines and self._lines[-1].raw.strip() == "":
                self._lines.pop()
            if self._lines:
                self._lines.append(EnvLine(raw="", key=None, value=None))
            self._lines.append(EnvLine(raw=USER_ADDITIONS_HEADER, key=None, value=None))
            self._lines.append(new_kv)
            return
        self._lines.append(new_kv)

    # ── persistence ───────────────────────────────────────────────────────

    def render(self) -> str:
        return "\n".join(ln.raw for ln in self._lines) + ("\n" if self._lines else "")

    def save(self) -> None:
        """Write with single-gen `.env.bak` fallback.

        Sequence:
          1. backup current file → ``.env.bak`` (if file exists)
          2. write new content directly to target with fsync

        Why not tmp+rename: Docker Desktop on macOS (VirtioFS/osxfs) returns
        EBUSY when ``os.replace(tmp, target)`` is called on a bind-mounted
        file because the mount-point inode is held busy by the hypervisor.
        Writing directly to the same path avoids the rename entirely; the
        backup provides recovery if the write is interrupted.
        """
        target = self.path
        directory = target.parent
        if not directory.exists():
            raise FileNotFoundError(
                f"parent directory {directory} does not exist; cannot write {target}"
            )

        if target.exists() and target.is_file():
            backup_path = (
                target.with_suffix(target.suffix + ".bak")
                if target.suffix
                else target.with_name(target.name + ".bak")
            )
            backup_path.write_bytes(target.read_bytes())

        payload = self.render().encode("utf-8")
        with open(target, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
