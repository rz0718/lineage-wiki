"""Source loading orchestration: connectors -> normalized EvidenceBundle."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import ChainConfig
from ..connectors.bigquery_connector import BigQueryLoadResult, load_bigquery_schemas
from ..connectors.local_repo_connector import RepoLoadResult, load_local_repo
from ..connectors.raw_doc_connector import load_raw_docs
from ..connectors.slack_connector import SlackLoadResult, load_slack_sources
from ..util import slugify
from .evidence import EvidenceItem
from .fingerprints import sha_bytes


@dataclass
class EvidenceBundle:
    """All evidence loadable for one chain, normalized to EvidenceItems."""

    raw_docs: list[EvidenceItem] = field(default_factory=list)
    missing_raw_docs: list[str] = field(default_factory=list)
    repos: list[RepoLoadResult] = field(default_factory=list)
    bigquery: BigQueryLoadResult | None = None
    human_notes: list[EvidenceItem] = field(default_factory=list)
    reports: list[EvidenceItem] = field(default_factory=list)
    slack: list[SlackLoadResult] = field(default_factory=list)

    def repo_load(self, name: str) -> RepoLoadResult | None:
        return next((r for r in self.repos if r.repo.name == name), None)

    def all_items(self) -> list[EvidenceItem]:
        items = list(self.raw_docs)
        for repo in self.repos:
            items.extend(repo.files)
        if self.bigquery is not None:
            items.extend(self.bigquery.items)
        items.extend(self.human_notes)
        items.extend(self.reports)
        items.extend(load.item for load in self.slack if load.item is not None)
        return items


def load_sources(cfg: ChainConfig, root: str | Path) -> EvidenceBundle:
    """Load every configured source. Raises SourceUnavailableError when a
    ``required: true`` source cannot be loaded."""
    root = Path(root)
    raw = load_raw_docs(cfg.sources.raw_docs, root)
    bundle = EvidenceBundle(raw_docs=raw.items, missing_raw_docs=raw.missing)
    bundle.repos = [load_local_repo(repo, root) for repo in cfg.sources.repos]
    if cfg.sources.bigquery and cfg.sources.bigquery.tables:
        bundle.bigquery = load_bigquery_schemas(cfg.sources.bigquery)
    if cfg.sources.slack:
        bundle.slack = load_slack_sources(cfg.sources.slack)

    for note in cfg.sources.human_notes:
        bundle.human_notes.append(
            EvidenceItem(
                id=f"human-note:{slugify(note.title)}",
                source_type="human_note",
                source_uri="config:human_notes",
                title=note.title,
                content=note.content,
                fingerprint=sha_bytes(note.content.encode("utf-8")),
            )
        )

    for report in cfg.sources.reports:
        bundle.reports.append(
            EvidenceItem(
                id=f"report:{slugify(report.name)}",
                source_type="report",
                source_uri=report.url or "config:reports",
                title=report.name,
                content=report.source_mapping_notes,
                metadata={"report_type": report.type},
                fingerprint=sha_bytes(report.source_mapping_notes.encode("utf-8")),
            )
        )
    return bundle
