"""Git-aware update impact planning and `update --plan-only`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lineage_wiki.agent.runner import run_generate, run_update
from lineage_wiki.cli import app
from lineage_wiki.config import ChainConfig
from lineage_wiki.ingestion.git_context import collect_git_context
from lineage_wiki.storage.manifest import load_manifest

from .conftest import EXAMPLE_CONFIG, FIXED_NOW

runner = CliRunner()
LATER = "2026-07-05T00:00:00Z"


@pytest.fixture
def wiki_root(tmp_path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


def _tree_state(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _write_raw_doc(root: Path, content: str = "# Methodology\n") -> Path:
    doc = root / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(content, encoding="utf-8")
    return doc


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    return path


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _example_cfg(**overrides) -> ChainConfig:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        data[key] = value
    return ChainConfig.model_validate(data)


# --- git context collection ---------------------------------------------------


def test_collect_git_context_reports_commits_and_files(tmp_path):
    repo = _git_repo(tmp_path / "clone")
    (repo / "pipeline.py").write_text("v1\n", encoding="utf-8")
    baseline = _commit_all(repo, "first")
    (repo / "pipeline.py").write_text("v2\n", encoding="utf-8")
    (repo / "other.py").write_text("x\n", encoding="utf-8")
    head = _commit_all(repo, "second")

    ctx = collect_git_context(repo, label="repo `x`", baseline=baseline)
    assert ctx.available and ctx.head == head and ctx.baseline == baseline
    assert len(ctx.commits_since) == 1 and "second" in ctx.commits_since[0]
    assert sorted(ctx.changed_files) == ["other.py", "pipeline.py"]
    assert "1 commit(s) since" in ctx.describe()

    same = collect_git_context(repo, label="repo `x`", baseline=head)
    assert "no commits since the last recorded run" in same.describe()


def test_collect_git_context_outside_git(tmp_path):
    ctx = collect_git_context(tmp_path, label="okf repo")
    assert not ctx.available
    assert "not a git work tree" in ctx.describe()


# --- plan-only ---------------------------------------------------------------


def test_plan_only_writes_nothing_and_predicts_the_real_update(wiki_root):
    _write_raw_doc(wiki_root, "# Methodology\n\nv1\n")
    cfg = _example_cfg()
    run_generate(cfg, wiki_root, FIXED_NOW)
    _write_raw_doc(wiki_root, "# Methodology Retitled\n\nv2, more lines\n")

    before = _tree_state(wiki_root)
    result = runner.invoke(
        app,
        ["update", "--config", str(EXAMPLE_CONFIG), "--root", str(wiki_root), "--plan-only"],
    )
    assert result.exit_code == 0, result.output
    assert _tree_state(wiki_root) == before  # not a byte changed anywhere

    out = result.output
    assert "git context:" in out
    assert "raw doc `raw_files/example/methodology.md` changed" in out
    assert "impact plan (pages to consider):" in out
    assert "indexes affected:" in out
    assert "okf/frameworks/index.md" in out
    assert "proposed actions:" in out
    assert "update    okf/frameworks/example-chain.md" in out
    assert "line(s); sections:" in out  # diff summary attached to the action
    assert "validation risks:" in out
    assert "plan-only: no files were written" in out

    # The real run does exactly what the plan proposed.
    applied = run_update(_example_cfg(), wiki_root, LATER)
    assert "okf/frameworks/example-chain.md" in applied.updated


def test_plan_only_noop_prints_and_writes_nothing(wiki_root, example_cfg):
    _write_raw_doc(wiki_root)
    run_generate(example_cfg, wiki_root, FIXED_NOW)
    before = _tree_state(wiki_root)
    result = runner.invoke(
        app,
        ["update", "--config", str(EXAMPLE_CONFIG), "--root", str(wiki_root), "--plan-only"],
    )
    assert result.exit_code == 0, result.output
    assert "no-op: no source changes detected" in result.output
    assert _tree_state(wiki_root) == before


def test_plan_only_flags_protected_pages_as_risks(wiki_root, example_cfg):
    hand = wiki_root / "okf" / "frameworks" / "example-chain.md"
    hand.parent.mkdir(parents=True)
    hand.write_text(
        "---\ntype: Framework\ntitle: Mine\ndescription: Hand.\n---\n# Mine\n",
        encoding="utf-8",
    )
    _write_raw_doc(wiki_root, "# Methodology\n\nv1\n")
    run_generate(example_cfg, wiki_root, FIXED_NOW)
    _write_raw_doc(wiki_root, "# Methodology Two\n\nv2\n")

    result = run_update(_example_cfg(), wiki_root, LATER, plan_only=True)
    assert result.plan_only and not result.noop
    protected = [a for a in result.actions if a.startswith("protected")]
    assert any("okf/frameworks/example-chain.md" in a for a in protected)
    assert any("not tool-owned" in r for r in result.risks)
    assert hand.read_text(encoding="utf-8").startswith("---\ntype: Framework\ntitle: Mine")


# --- git-aware impact reasons ----------------------------------------------------


def _cfg_with_local_clone(wiki_root: Path) -> ChainConfig:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    data["sources"]["repos"] = [
        {
            "name": "example-pipeline",
            "host": "github",
            "branch": "main",
            "local_path": "clone",
            "paths": ["pipeline.py"],
            "symbols": ["compute_daily_snapshot"],
            "required": False,
        }
    ]
    return ChainConfig.model_validate(data)


def test_repo_change_reason_includes_commits_and_touched_paths(wiki_root):
    clone = _git_repo(wiki_root / "clone")
    (clone / "pipeline.py").write_text(
        "def compute_daily_snapshot():\n    return 1\n", encoding="utf-8"
    )
    _commit_all(clone, "initial pipeline")

    _write_raw_doc(wiki_root)
    cfg = _cfg_with_local_clone(wiki_root)
    run_generate(cfg, wiki_root, FIXED_NOW)
    baseline = load_manifest(wiki_root).source_fingerprints.repos[
        "example-pipeline"
    ].git_head
    assert baseline

    (clone / "pipeline.py").write_text(
        "def compute_daily_snapshot():\n    return 2\n", encoding="utf-8"
    )
    _commit_all(clone, "tweak formula")

    result = run_update(_cfg_with_local_clone(wiki_root), wiki_root, LATER, plan_only=True)
    reasons = result.impact["okf/code-links/example-pipeline-engine.md"]
    joined = "; ".join(reasons)
    assert f"1 commit(s) since {baseline[:12]}" in joined
    assert "configured paths touched: pipeline.py" in joined
    # Framework and change-check pages ride the same reason.
    assert "okf/frameworks/example-chain.md" in result.impact
    assert "okf/change-checks/example-chain-review-rules.md" in result.impact
    # Git context lines cover both the okf repo and the clone.
    assert any(line.startswith("repo `example-pipeline`") for line in result.git_context)
    assert any(line.startswith("okf repo") for line in result.git_context)


def test_okf_repo_baseline_recorded_and_compared(wiki_root, example_cfg):
    _git_repo(wiki_root)
    _write_raw_doc(wiki_root)
    (wiki_root / "seed.txt").write_text("s\n", encoding="utf-8")
    first = _commit_all(wiki_root, "seed")

    run_generate(example_cfg, wiki_root, FIXED_NOW)
    manifest = load_manifest(wiki_root)
    assert manifest.okf_git_head == first

    _commit_all(wiki_root, "commit generated pages")
    _write_raw_doc(wiki_root, "# Methodology Two\n")
    result = run_update(_example_cfg(), wiki_root, LATER, plan_only=True)
    okf_line = next(l for l in result.git_context if l.startswith("okf repo"))
    assert f"commit(s) since {first[:12]}" in okf_line


def test_plain_okf_commit_does_not_churn_the_manifest(wiki_root, example_cfg):
    """okf_git_head is informational: a commit with no source change keeps
    generate/update strict no-ops and leaves the manifest untouched."""
    _git_repo(wiki_root)
    _write_raw_doc(wiki_root)
    (wiki_root / "seed.txt").write_text("s\n", encoding="utf-8")
    _commit_all(wiki_root, "seed")
    run_generate(example_cfg, wiki_root, FIXED_NOW)
    manifest_bytes = (wiki_root / ".lineage-wiki" / "manifest.yml").read_bytes()

    _commit_all(wiki_root, "commit generated pages")  # git moves, sources do not

    update = run_update(_example_cfg(), wiki_root, LATER)
    assert update.noop
    regen = run_generate(_example_cfg(), wiki_root, LATER)
    assert not regen.manifest_written
    assert (wiki_root / ".lineage-wiki" / "manifest.yml").read_bytes() == manifest_bytes


# --- affected indexes -------------------------------------------------------------


def test_affected_indexes_follow_affected_pages(wiki_root, example_cfg):
    _write_raw_doc(wiki_root, "# Methodology\n\nv1\n")
    run_generate(example_cfg, wiki_root, FIXED_NOW)
    _write_raw_doc(wiki_root, "# Methodology Two\n\nv2\n")

    result = run_update(_example_cfg(), wiki_root, LATER, plan_only=True)
    assert "okf/index.md" in result.indexes_affected
    assert "okf/frameworks/index.md" in result.indexes_affected
    # Raw-doc changes do not touch report templates, so that index isn't listed.
    assert "okf/report-templates/index.md" not in result.indexes_affected
