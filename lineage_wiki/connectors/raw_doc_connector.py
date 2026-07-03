"""Local raw Markdown/text document connector."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import RawDocSource
from ..ingestion.evidence import EvidenceItem
from ..ingestion.fingerprints import sha_bytes
from . import SourceUnavailableError

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class RawDocLoadResult:
    items: list[EvidenceItem] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # configured paths not found


def _doc_title(text: str, fallback: str) -> str:
    match = _H1_RE.search(text)
    return match.group(1) if match else fallback


def load_raw_docs(docs: list[RawDocSource], root: Path) -> RawDocLoadResult:
    """Load configured raw docs from disk.

    Optional docs that are absent are reported as missing (the caller turns
    them into Known Gaps); a missing ``required: true`` doc fails clearly.
    """
    result = RawDocLoadResult()
    for doc in docs:
        file = root / doc.path
        if not file.exists():
            if doc.required:
                raise SourceUnavailableError(
                    f"required raw doc not found: {doc.path} (resolved to {file})"
                )
            result.missing.append(doc.path)
            continue
        data = file.read_bytes()
        text = data.decode("utf-8", errors="replace")
        result.items.append(
            EvidenceItem(
                id=f"raw-doc:{doc.path}",
                source_type="raw_doc",
                source_uri=doc.path,
                title=_doc_title(text, file.name),
                content=text,
                metadata={"doc_type": doc.type, "lines": len(text.splitlines())},
                fingerprint=sha_bytes(data),
            )
        )
    return result
