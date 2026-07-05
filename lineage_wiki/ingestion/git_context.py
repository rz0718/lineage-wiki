"""Git context collection for update impact planning.

Answers "what actually happened in git since the last recorded run?" for
the target OKF repo and each configured local repo clone. Read-only:
``rev-parse``, ``status --porcelain``, ``log``, and ``diff --name-only``
against the baseline head recorded in the manifest. Git being absent (no
work tree, deleted baseline, no git binary) degrades to an unavailable
context — fingerprint diffing remains the source of truth for *whether*
something changed; git context explains *what*.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_MAX_COMMITS = 20


def _git(repo_dir: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


@dataclass
class GitContext:
    label: str
    available: bool = False
    head: str | None = None
    branch: str | None = None
    baseline: str | None = None
    commits_since: list[str] = field(default_factory=list)  # "abc1234 subject"
    changed_files: list[str] = field(default_factory=list)  # baseline..HEAD
    dirty_files: list[str] = field(default_factory=list)  # uncommitted

    def describe(self) -> str:
        if not self.available:
            return f"{self.label}: not a git work tree"
        head = (self.head or "?")[:12]
        parts = [f"{self.label}: {self.branch or 'detached'} @ {head}"]
        if self.baseline and self.baseline == self.head:
            parts.append("no commits since the last recorded run")
        elif self.commits_since:
            parts.append(
                f"{len(self.commits_since)} commit(s) since "
                f"{self.baseline[:12] if self.baseline else 'the last run'}"
            )
            if self.changed_files:
                shown = ", ".join(self.changed_files[:6])
                more = len(self.changed_files) - 6
                parts.append(
                    f"files touched: {shown}" + (f" (+{more} more)" if more > 0 else "")
                )
        elif self.baseline:
            parts.append(f"baseline {self.baseline[:12]} not comparable")
        if self.dirty_files:
            parts.append(f"{len(self.dirty_files)} uncommitted change(s)")
        return "; ".join(parts)


def collect_git_context(
    repo_dir: Path, *, label: str, baseline: str | None = None
) -> GitContext:
    ctx = GitContext(label=label, baseline=baseline)
    head = _git(repo_dir, "rev-parse", "HEAD")
    if head is None:
        return ctx
    ctx.available = True
    ctx.head = head.strip() or None
    branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    ctx.branch = branch.strip() if branch else None

    status = _git(repo_dir, "status", "--porcelain")
    if status:
        ctx.dirty_files = [
            line[3:].strip() for line in status.splitlines() if line.strip()
        ]

    if baseline and ctx.head and baseline != ctx.head:
        log = _git(
            repo_dir, "log", "--oneline", f"--max-count={_MAX_COMMITS}",
            f"{baseline}..HEAD",
        )
        if log is not None:
            ctx.commits_since = [l for l in log.splitlines() if l.strip()]
            diff = _git(repo_dir, "diff", "--name-only", f"{baseline}..HEAD")
            if diff is not None:
                ctx.changed_files = [l.strip() for l in diff.splitlines() if l.strip()]
    return ctx
