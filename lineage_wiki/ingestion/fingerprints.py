"""Source fingerprinting (Milestone 3) — local and deterministic, no network.

Fingerprints answer one question for the update command: "did this evidence
source change since the last run?"

- raw docs: sha256 of file content ("missing" when the file is absent, so
  creating the file later registers as a change).
- repos: branch ref + git HEAD of the local clone (when available) + sha256
  over the configured paths' contents. Remote-only repos fingerprint their
  config identity; content-level fingerprints for them land with the GitHub
  connector milestone.
- bigquery: hash of the loaded table schema (columns, types, partitioning,
  clustering, view SQL — everything except volatile last-modified metadata).
  When BigQuery is unavailable the fingerprint falls back to the configured
  table identity, so unavailable runs stay no-op-stable and schemas register
  as a change the first time they load.
- reports: hash of the configured report mapping (name/type/url/notes).
- config: hash of the scaffold-relevant chain config (everything except
  ``model`` and ``validation``, which do not affect deterministic output).
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config import ChainConfig, RepoSource
from ..storage.manifest import RepoFingerprint, SourceFingerprints

MISSING = "missing"


@dataclass
class FingerprintComputation:
    fingerprints: SourceFingerprints
    warnings: list[str] = field(default_factory=list)


def sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha_obj(obj) -> str:
    return sha_bytes(yaml.safe_dump(obj, sort_keys=True).encode("utf-8"))


def fingerprint_raw_doc(root: Path, path: str) -> str:
    file = root / path
    if not file.exists():
        return MISSING
    return sha_bytes(file.read_bytes())


def git_head(repo_dir: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def fingerprint_repo(root: Path, repo: RepoSource) -> RepoFingerprint:
    fingerprint = RepoFingerprint(ref=repo.branch)
    local = (root / repo.local_path).resolve() if repo.local_path else None
    if local and local.exists():
        fingerprint.git_head = git_head(local)
        digest = hashlib.sha256()
        for rel in sorted(repo.paths):
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            file = local / rel
            digest.update(file.read_bytes() if file.exists() else MISSING.encode("utf-8"))
            digest.update(b"\0")
        fingerprint.paths_hash = "sha256:" + digest.hexdigest()
    else:
        # No local clone to hash — fingerprint the configured identity so at
        # least config-level changes are detected.
        fingerprint.paths_hash = _sha_obj(repo.model_dump())
    return fingerprint


def fingerprint_bigquery(
    source, previous: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    # Imported here: the connector imports config/evidence, and connectors
    # already import this module for sha_bytes/git_head.
    from ..connectors.bigquery_connector import load_bigquery_schemas

    fingerprints = {table: _sha_obj(f"config:{table}") for table in source.tables}
    load = load_bigquery_schemas(source, enforce_required=False)
    if not load.available:
        preserved = []
        if previous:
            for table in source.tables:
                if table in previous:
                    fingerprints[table] = previous[table]
                    preserved.append(table)
        warning = (
            "BigQuery unavailable"
            + (f" ({load.unavailable_reason})" if load.unavailable_reason else "")
            + "; preserved prior schema fingerprints for "
            + (
                ", ".join(f"`{table}`" for table in preserved)
                if preserved
                else "0 table(s)"
            )
        )
        return fingerprints, [warning]
    for table, schema in load.schemas.items():
        fingerprints[table] = schema.fingerprint()
    return fingerprints, []


def compute_fingerprint_result(
    cfg: ChainConfig,
    root: str | Path,
    previous: SourceFingerprints | None = None,
) -> FingerprintComputation:
    root = Path(root)
    bigquery = {}
    warnings = []
    if cfg.sources.bigquery and cfg.sources.bigquery.tables:
        bigquery, warnings = fingerprint_bigquery(
            cfg.sources.bigquery,
            previous.bigquery if previous is not None else None,
        )
    return FingerprintComputation(SourceFingerprints(
        repos={repo.name: fingerprint_repo(root, repo) for repo in cfg.sources.repos},
        bigquery=bigquery,
        raw_docs={doc.path: fingerprint_raw_doc(root, doc.path) for doc in cfg.sources.raw_docs},
        reports={r.name: _sha_obj(r.model_dump()) for r in cfg.sources.reports},
        # bigquery_verification is excluded too: verify-bq settings do not
        # affect deterministic page output, so tuning them must not register
        # as a chain-config change.
        config=_sha_obj(
            cfg.model_dump(exclude={"model", "validation", "bigquery_verification"})
        ),
    ), warnings)


def compute_fingerprints(
    cfg: ChainConfig,
    root: str | Path,
    previous: SourceFingerprints | None = None,
) -> SourceFingerprints:
    return compute_fingerprint_result(cfg, root, previous).fingerprints
