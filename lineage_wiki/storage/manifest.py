"""Manifest reading/writing and fingerprint diffing under .lineage-wiki/."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..constants import MANIFEST_FILE


class RepoFingerprint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str | None = None
    git_head: str | None = None
    paths_hash: str | None = None


class SourceFingerprints(BaseModel):
    model_config = ConfigDict(extra="ignore")

    repos: dict[str, RepoFingerprint] = Field(default_factory=dict)
    bigquery: dict[str, str] = Field(default_factory=dict)
    raw_docs: dict[str, str] = Field(default_factory=dict)
    # Extensions beyond the spec example, needed for deterministic updates:
    # report-mapping fingerprints and a hash of the scaffold-relevant config.
    reports: dict[str, str] = Field(default_factory=dict)
    config: str = ""


class Manifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = 1
    chain_id: str
    chain_slug: str
    output_dir: str = "okf"
    generated_files: list[str] = Field(default_factory=list)
    managed_indexes: list[str] = Field(default_factory=list)
    file_snapshots: dict[str, str] = Field(default_factory=dict)
    source_fingerprints: SourceFingerprints = Field(default_factory=SourceFingerprints)
    last_run_at: str = ""
    last_content_snapshot: str = ""
    # Baseline for git context on later update runs ("what happened in the
    # OKF repo since the last content-changing run?"). Informational only —
    # excluded from equality so a plain commit never churns the manifest.
    okf_git_head: str | None = None


def manifest_path(root: str | Path) -> Path:
    return Path(root) / MANIFEST_FILE


def load_manifest(root: str | Path) -> Manifest | None:
    path = manifest_path(root)
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    return Manifest.model_validate(data)


def save_manifest(root: str | Path, manifest: Manifest) -> Path:
    path = manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(manifest.model_dump(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def compute_snapshot(contents: dict[str, str]) -> str:
    """Deterministic content snapshot over {rel_path: file_content}."""
    digest = hashlib.sha256()
    for rel in sorted(contents):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(contents[rel].encode("utf-8"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def compute_file_snapshots(contents: dict[str, str]) -> dict[str, str]:
    """Per-file content hashes for ownership drift checks."""
    return {rel: compute_snapshot({rel: text}) for rel, text in sorted(contents.items())}


def manifests_equal_ignoring_run_time(a: Manifest, b: Manifest) -> bool:
    exclude = {"last_run_at", "okf_git_head"}
    return a.model_dump(exclude=exclude) == b.model_dump(exclude=exclude)


# --- Fingerprint diffing ---------------------------------------------------------


@dataclass
class SourceChanges:
    """Evidence sources whose fingerprints differ between two manifests."""

    raw_docs: list[str] = field(default_factory=list)
    repos: list[str] = field(default_factory=list)
    bigquery: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    config: bool = False

    def any(self) -> bool:
        return bool(
            self.raw_docs or self.repos or self.bigquery or self.reports or self.config
        )

    def describe(self) -> list[str]:
        lines = []
        if self.config:
            lines.append("chain config changed")
        lines.extend(f"raw doc `{p}` changed" for p in self.raw_docs)
        lines.extend(f"repo `{r}` changed" for r in self.repos)
        lines.extend(f"bigquery table `{t}` changed" for t in self.bigquery)
        lines.extend(f"report `{r}` mapping changed" for r in self.reports)
        return lines


def _changed_keys(old: dict, new: dict) -> list[str]:
    """Keys added, removed, or with a different value."""
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def diff_fingerprints(old: SourceFingerprints, new: SourceFingerprints) -> SourceChanges:
    return SourceChanges(
        raw_docs=_changed_keys(old.raw_docs, new.raw_docs),
        repos=_changed_keys(
            {k: v.model_dump() for k, v in old.repos.items()},
            {k: v.model_dump() for k, v in new.repos.items()},
        ),
        bigquery=_changed_keys(old.bigquery, new.bigquery),
        reports=_changed_keys(old.reports, new.reports),
        config=old.config != new.config,
    )
