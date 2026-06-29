# SPDX-License-Identifier: Apache-2.0
"""Per-intent markdown knowledge base (delta-label-accuracy SF1).

This package maintains a git-versioned, human-readable/editable markdown KB per
intent. The only `kind` this round is the **event index** (`events.md`): an
incrementally-merged table of `event-key -> first-seen / latest date -> latest
state`, distilled from the intent's cron digests.

Design notes live in the internal development docs (delta-label-accuracy / kb).
The KB lives under the gitignored `data/kb/` runtime tree (a nested independent
git repo), NOT in the public sembr code repo.
"""

from __future__ import annotations

# The kinds the KB supports. One entry this round; adding "playbook" later is a
# one-line change here + a merge-rule registration (design §7.3 zero-refactor).
KB_KINDS: tuple[str, ...] = ("events",)
