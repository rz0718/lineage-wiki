"""Source fingerprinting (Milestone 3) — local and deterministic, no network.

Fingerprints answer one question for the update command: "did this evidence
source change since the last run?"

- raw docs: sha256 of file content ("missing" when the file is absent, so
  creating the file later registers as a change).
- repos: branch ref + git HEAD of the local clone (when available) + sha256
  over the configured paths' contents. Remote-only repos fingerprint their
  config identity; content-level fingerprints for them land with the GitHub
  connector milestone.
- bigquery: hash of the configured table identity. Schema-level fingerprints
  land with the BigQuery connector milestone.
- reports: hash of the configured report mapping (name/type/url/notes).
- config: hash of the scaffold-relevant chain config (everything except
  ``model`` and ``validation``, which do not affect deterministic output).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml

from ..config import ChainConfig, RepoSource
from ..storage.manifest import RepoFingerprint, SourceFingerprints

MISSING = "missing"


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


def compute_fingerprints(cfg: ChainConfig, root: str | Path) -> SourceFingerprints:
    root = Path(root)
    bigquery = {}
    if cfg.sources.bigquery:
        bigquery = {
            table: _sha_obj(f"config:{table}") for table in cfg.sources.bigquery.tables
        }
    return SourceFingerprints(
        repos={repo.name: fingerprint_repo(root, repo) for repo in cfg.sources.repos},
        bigquery=bigquery,
        raw_docs={doc.path: fingerprint_raw_doc(root, doc.path) for doc in cfg.sources.raw_docs},
        reports={r.name: _sha_obj(r.model_dump()) for r in cfg.sources.reports},
        config=_sha_obj(cfg.model_dump(exclude={"model", "validation"})),
    )
