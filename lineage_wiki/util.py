"""Small shared helpers."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lowercase, replace non-alphanumerics with hyphens, collapse runs."""
    return _SLUG_RE.sub("-", value.lower()).strip("-")


def humanize(slug: str) -> str:
    """Turn a slug or snake_case name into a title-ish label."""
    return " ".join(word.capitalize() for word in slugify(slug).split("-"))


def now_stamp() -> str:
    """Frontmatter timestamp for generated pages.

    Date-granular (midnight UTC) so re-running on the same day is
    byte-identical. Overridable via LINEAGE_WIKI_NOW for reproducible runs.
    """
    override = os.environ.get("LINEAGE_WIKI_NOW")
    if override:
        return override
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
