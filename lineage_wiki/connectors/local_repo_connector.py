"""Local repository connector: reads configured paths from a local clone.

No cloning or fetching — the clone must already exist at
``sources.repos[].local_path`` (resolved relative to the target repo root).
Repos without a usable local clone are treated as unverified references and
surface as Known Gaps (the remote GitHub connector lands in a later
milestone); a ``required: true`` repo whose configured clone is absent fails
clearly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import RepoSource
from ..ingestion.evidence import EvidenceItem
from ..ingestion.fingerprints import git_head, sha_bytes
from . import SourceUnavailableError


@dataclass
class RepoLoadResult:
    repo: RepoSource
    available: bool
    local_dir: Path | None = None
    git_head: str | None = None
    files: list[EvidenceItem] = field(default_factory=list)  # one per loaded path
    missing_paths: list[str] = field(default_factory=list)


def load_local_repo(repo: RepoSource, root: Path) -> RepoLoadResult:
    if not repo.local_path:
        # Reference-only repo: nothing to read without a remote connector.
        return RepoLoadResult(repo=repo, available=False)

    local = (root / repo.local_path).resolve()
    if not local.is_dir():
        if repo.required:
            raise SourceUnavailableError(
                f"required repo `{repo.name}` has no local clone at "
                f"{repo.local_path} (resolved to {local})"
            )
        return RepoLoadResult(repo=repo, available=False)

    result = RepoLoadResult(
        repo=repo, available=True, local_dir=local, git_head=git_head(local)
    )
    for rel in repo.paths:
        file = local / rel
        if not file.is_file():
            result.missing_paths.append(rel)
            continue
        data = file.read_bytes()
        text = data.decode("utf-8", errors="replace")
        result.files.append(
            EvidenceItem(
                id=f"local-repo:{repo.name}:{rel}",
                source_type="local_repo",
                source_uri=f"{repo.name}:{rel}",
                title=rel,
                content=text,
                metadata={
                    "repo": repo.name,
                    "path": rel,
                    "lines": len(text.splitlines()),
                },
                fingerprint=sha_bytes(data),
            )
        )
    return result
