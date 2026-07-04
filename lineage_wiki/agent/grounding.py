"""Deterministic grounding enforcement for LLM output.

The model proposes; this module disposes. Nothing the model says reaches an
OKF page unless it passes these checks, which are plain Python over the
loaded evidence bundle — the model is never trusted to verify itself:

- every claim must cite at least one known EvidenceItem id;
- ``formula`` claims must cite doc/code/note evidence and carry a verbatim
  quote found in the cited evidence — otherwise they become Known Gaps;
- ``column`` claims must cite BigQuery schema evidence and name a real
  column from the cited schema;
- ``code_path`` claims must cite loaded repo-file evidence;
- conflicts must cite known evidence and become Known Doc-vs-Code
  Divergences;
- written sections must cite accepted evidence via ``[src: <id>]`` markers
  and must not contain SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..ingestion.source_loader import EvidenceBundle

CLAIM_KINDS = ("formula", "definition", "column", "code_path", "mapping", "fact")

# Evidence types acceptable as the source of a formula.
_FORMULA_SOURCES = ("raw_doc", "local_repo", "human_note")

_SRC_MARKER = re.compile(r"\[src:\s*([^\]\s]+)\s*\]")
_SQL_FENCE = re.compile(r"(?is)```sql")
_SQL_STATEMENT = re.compile(r"(?is)\bselect\b.{0,400}?\bfrom\b")
_WS = re.compile(r"\s+")


def _normalized(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


@dataclass
class Claim:
    id: str
    kind: str
    text: str
    evidence_ids: list[str] = field(default_factory=list)
    quote: str = ""

    def payload(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass
class Conflict:
    topic: str
    detail: str
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class Decision:
    accepted: bool
    reason: str = ""


class GroundingContext:
    """Evidence lookup tables the checks run against."""

    def __init__(self, bundle: EvidenceBundle):
        items = bundle.all_items()
        self.known_ids = {item.id for item in items}
        self.types = {item.id: item.source_type for item in items}
        self.contents = {item.id: _normalized(item.content) for item in items}
        self.bq_columns: dict[str, list[str]] = {}
        if bundle.bigquery is not None:
            for schema in bundle.bigquery.schemas.values():
                evidence_id = f"bq-schema:{schema.table_id}"
                self.bq_columns[evidence_id] = [c.name for c in schema.columns]

    # --- claims -----------------------------------------------------------

    def check_claim(self, claim: Claim) -> Decision:
        if claim.kind not in CLAIM_KINDS:
            return Decision(False, f"unknown claim kind {claim.kind!r}")
        if not claim.text.strip():
            return Decision(False, "empty claim text")
        unknown = [e for e in claim.evidence_ids if e not in self.known_ids]
        if unknown or not claim.evidence_ids:
            missing = ", ".join(unknown) or "none cited"
            return Decision(False, f"cites unknown evidence ids ({missing})")
        if claim.kind == "formula":
            return self._check_formula(claim)
        if claim.kind == "column":
            return self._check_column(claim)
        if claim.kind == "code_path":
            return self._check_code_path(claim)
        return Decision(True)

    def _check_formula(self, claim: Claim) -> Decision:
        sources = [e for e in claim.evidence_ids if self.types[e] in _FORMULA_SOURCES]
        if not sources:
            return Decision(
                False,
                "formula cites no methodology/code/note evidence "
                f"(needs one of: {', '.join(_FORMULA_SOURCES)})",
            )
        quote = _normalized(claim.quote)
        if not quote:
            return Decision(False, "formula has no supporting quote")
        if not any(quote in self.contents[e] for e in sources):
            return Decision(False, "formula quote not found in the cited evidence")
        return Decision(True)

    def _check_column(self, claim: Claim) -> Decision:
        schema_ids = [e for e in claim.evidence_ids if e in self.bq_columns]
        if not schema_ids:
            return Decision(
                False, "column claim cites no ingested BigQuery schema evidence"
            )
        text = claim.text
        cited_columns = [c for e in schema_ids for c in self.bq_columns[e]]
        if not any(column in text for column in cited_columns):
            return Decision(
                False, "column claim names no column present in the cited schema"
            )
        return Decision(True)

    def _check_code_path(self, claim: Claim) -> Decision:
        if not any(self.types[e] == "local_repo" for e in claim.evidence_ids):
            return Decision(
                False, "code path claim cites no loaded repository file evidence"
            )
        return Decision(True)

    # --- conflicts ----------------------------------------------------------

    def check_conflict(self, conflict: Conflict) -> Decision:
        if not conflict.topic.strip() or not conflict.detail.strip():
            return Decision(False, "conflict missing topic or detail")
        if not conflict.evidence_ids or any(
            e not in self.known_ids for e in conflict.evidence_ids
        ):
            return Decision(False, "conflict cites unknown or no evidence ids")
        return Decision(True)

    # --- written sections -----------------------------------------------------

    def check_section_body(
        self, body: str, accepted_evidence_ids: set[str]
    ) -> Decision:
        if not body.strip():
            return Decision(False, "empty section body")
        if _SQL_FENCE.search(body) or _SQL_STATEMENT.search(body):
            return Decision(
                False, "SQL is not allowed in LLM-written sections"
            )
        cited = _SRC_MARKER.findall(body)
        if not cited:
            return Decision(False, "no [src: <evidence-id>] citation markers")
        bad = [c for c in cited if c not in accepted_evidence_ids]
        if bad:
            return Decision(
                False,
                "cites evidence outside the accepted claims: " + ", ".join(bad),
            )
        return Decision(True)
