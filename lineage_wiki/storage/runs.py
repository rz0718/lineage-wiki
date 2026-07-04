"""Run metadata under .lineage-wiki/runs/<run-id>.json."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..constants import MANIFEST_DIR

RUNS_DIRNAME = f"{MANIFEST_DIR}/runs"


class RunRecord(BaseModel):
    """One generate/update run (field names follow the spec example)."""

    model_config = ConfigDict(extra="ignore")

    updatedAt: str
    command: str
    chainId: str
    model: str = ""
    okfGitHead: str | None = None
    contentChanged: bool = False
    createdFiles: list[str] = Field(default_factory=list)
    updatedFiles: list[str] = Field(default_factory=list)
    gaps: int = 0
    divergences: int = 0


def runs_dir(root: str | Path) -> Path:
    return Path(root) / RUNS_DIRNAME


def okf_git_head(root: str | Path) -> str | None:
    """HEAD of the OKF repo, if the root is inside a git work tree."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _next_run_path(root: str | Path, updated_at: str, command: str) -> Path:
    """Run ids are timestamp + command, deduped."""
    directory = runs_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9TZ]", "", updated_at) or "run"
    base = f"{stamp}-{command}"
    path = directory / f"{base}.json"
    counter = 2
    while path.exists():
        path = directory / f"{base}-{counter}.json"
        counter += 1
    return path


def write_run(root: str | Path, record: RunRecord) -> Path:
    """Write one generate/update run record."""
    path = _next_run_path(root, record.updatedAt, record.command)
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def write_json_run(root: str | Path, updated_at: str, command: str, payload: dict) -> Path:
    """Write a free-form run payload (e.g. detailed verify-bq results —
    query outputs belong here, never on OKF pages)."""
    path = _next_run_path(root, updated_at, command)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def list_runs(root: str | Path) -> list[Path]:
    directory = runs_dir(root)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))
