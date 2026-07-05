"""AGENTS.md / CLAUDE.md managed OKF Wiki Context block."""

from __future__ import annotations

from typer.testing import CliRunner

from lineage_wiki.agent.runner import run_init, upsert_agent_context
from lineage_wiki.cli import app
from lineage_wiki.constants import (
    AGENT_BLOCK_END,
    AGENT_BLOCK_START,
    AGENT_INSTRUCTIONS_BLOCK,
)

runner = CliRunner()

REQUIRED_PHRASES = (
    "okf/index.md",
    "framework, component, output, code-link,\n   report-template, metric, and change-check",
    "Do not change formulas, table mappings, or report behavior",
    "record the divergence",
    "Do not invent missing lineage",
)


def test_block_contains_all_required_guidance():
    for phrase in REQUIRED_PHRASES:
        assert phrase in AGENT_INSTRUCTIONS_BLOCK, phrase
    assert AGENT_INSTRUCTIONS_BLOCK.startswith(AGENT_BLOCK_START)
    assert AGENT_INSTRUCTIONS_BLOCK.rstrip().endswith(AGENT_BLOCK_END)


def test_file_missing_creates_both_files(tmp_path):
    result = run_init(tmp_path, agents=True)
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert name in result.created
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert text == AGENT_INSTRUCTIONS_BLOCK
        assert text.count(AGENT_BLOCK_START) == 1


def test_existing_file_without_block_is_appended_and_preserved(tmp_path):
    original = "# My Project\n\nBuild with make. Do not touch prod.\n"
    (tmp_path / "AGENTS.md").write_text(original, encoding="utf-8")

    result = run_init(tmp_path, agents=True)
    assert "AGENTS.md" in result.updated

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text.startswith("# My Project\n\nBuild with make. Do not touch prod.")
    assert text.count(AGENT_BLOCK_START) == 1
    assert text.index(AGENT_BLOCK_START) > text.index("Do not touch prod.")


def test_legacy_heading_block_is_upgraded_without_duplication(tmp_path):
    legacy = (
        "# My Project\n\nIntro.\n\n"
        "## OKF Wiki Context\n\n"
        "Old outdated instructions from a previous version.\n\n"
        "- stale bullet\n\n"
        "## Deployment\n\nShip on Fridays only.\n"
    )
    (tmp_path / "CLAUDE.md").write_text(legacy, encoding="utf-8")

    result = run_init(tmp_path, agents=True)
    assert "CLAUDE.md" in result.updated

    text = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert text.count("## OKF Wiki Context") == 1
    assert text.count(AGENT_BLOCK_START) == 1
    assert "Old outdated instructions" not in text
    assert "stale bullet" not in text
    # Unrelated content on both sides survives.
    assert text.startswith("# My Project\n\nIntro.")
    assert "## Deployment\n\nShip on Fridays only." in text
    assert "Do not invent missing lineage" in text


def test_managed_block_is_updated_in_place(tmp_path):
    stale_block = (
        f"{AGENT_BLOCK_START}\n## OKF Wiki Context\n\nstale managed body\n{AGENT_BLOCK_END}"
    )
    (tmp_path / "AGENTS.md").write_text(
        f"# Front matter\n\n{stale_block}\n\n## After\n\nKeep me.\n", encoding="utf-8"
    )

    result = run_init(tmp_path, agents=True)
    assert "AGENTS.md" in result.updated

    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "stale managed body" not in text
    assert "Do not invent missing lineage" in text
    assert text.startswith("# Front matter")
    assert "## After\n\nKeep me." in text
    assert text.count(AGENT_BLOCK_START) == 1
    assert text.count(AGENT_BLOCK_END) == 1


def test_repeated_init_is_idempotent_no_duplicates(tmp_path):
    run_init(tmp_path, agents=True)
    first = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")

    second = run_init(tmp_path, agents=True)
    assert "AGENTS.md" in second.skipped and "CLAUDE.md" in second.skipped
    assert not second.updated
    text = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert text == first
    assert text.count(AGENT_BLOCK_START) == 1

    third = run_init(tmp_path, agents=True)
    assert "AGENTS.md" in third.skipped
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == first


def test_upsert_is_pure_and_stable():
    created, action = upsert_agent_context("")
    assert action == "created"
    same, action = upsert_agent_context(created)
    assert action == "unchanged" and same == created

    appended, action = upsert_agent_context("# Other\n\nstuff\n")
    assert action == "appended"
    stable, action = upsert_agent_context(appended)
    assert action == "unchanged" and stable == appended


def test_cli_reports_updated_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
    result = runner.invoke(app, ["init", "--root", str(tmp_path), "--agents"])
    assert result.exit_code == 0, result.output
    assert "updated   AGENTS.md" in result.output
    assert "created   CLAUDE.md" in result.output
