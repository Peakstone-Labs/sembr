# SPDX-License-Identifier: Apache-2.0
"""Shared HTML→plain-text flattening for article bodies.

Both the reduce pipeline (raw-article path) and the extraction-spec layer (map
path) reduce possibly-HTML article bodies to plain text with the same rules, so
the two paths cannot drift. Kept in its own module to avoid a pipeline↔spec
import cycle (pipeline imports spec, so spec must not import pipeline).
"""

from __future__ import annotations

import html2text as _h2t

_h2t_converter = _h2t.HTML2Text()
_h2t_converter.ignore_links = True
_h2t_converter.ignore_images = True
_h2t_converter.ignore_emphasis = False
_h2t_converter.body_width = 0  # no line wrapping


def to_plain_text(raw: str) -> str:
    """Flatten HTML to plain text; pass through text that isn't already HTML."""
    if "<" in raw and ">" in raw:
        return _h2t_converter.handle(raw).strip()
    return raw.strip()
