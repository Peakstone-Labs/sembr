# SPDX-License-Identifier: Apache-2.0
"""Per-intent markdown knowledge base (delta-label-accuracy SF1).

This package maintains a git-versioned, human-readable/editable markdown KB per
intent. The only `kind` this round is the **event index** (`events.md`): a set of
coarse **tracked threads** (~10-20 per intent), each with a current-state line and
an append-only dated **timeline** showing how the event evolved — incrementally
merged from the intent's cron digests (one entry per thread per day).

Design notes live in the internal development docs (delta-label-accuracy / kb).
The KB lives under the gitignored `data/kb/` runtime tree (a nested independent
git repo), NOT in the public sembr code repo.
"""

from __future__ import annotations

# The kinds the KB supports. One entry this round; adding "playbook" later is a
# one-line change here + a merge-rule registration (design §7.3 zero-refactor).
KB_KINDS: tuple[str, ...] = ("events",)
