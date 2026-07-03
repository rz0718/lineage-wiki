"""Content-snapshot helpers for no-op detection."""

from __future__ import annotations

import re

_TIMESTAMP_LINE = re.compile(r"^timestamp: .*$", re.MULTILINE)


def materially_equal(old: str, new: str) -> bool:
    """True when two page renderings differ only by the frontmatter timestamp.

    Re-rendering on a later day refreshes the ``timestamp:`` field; that alone
    is a formatting-only change and must not churn files during update runs.
    """
    if old == new:
        return True
    return _TIMESTAMP_LINE.sub("timestamp:", old) == _TIMESTAMP_LINE.sub("timestamp:", new)
