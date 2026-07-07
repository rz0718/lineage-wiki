"""Evidence model (spec section 9): every fact generated into OKF must be
traceable to at least one EvidenceItem."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SourceType = Literal[
    "raw_doc",
    "github",
    "local_repo",
    "bigquery_schema",
    "bigquery_sql",
    "report",
    "slack_message",
    "human_note",
    "git_diff",
]


class EvidenceItem(BaseModel):
    id: str
    source_type: SourceType
    source_uri: str
    title: str | None = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str | None = None
