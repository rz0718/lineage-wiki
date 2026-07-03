import shutil

import pytest
import yaml
from typer.testing import CliRunner

from lineage_wiki.cli import app
from lineage_wiki.constants import INDEX_FILES

from .conftest import EXAMPLE_CONFIG, FIXED_NOW

runner = CliRunner()


@pytest.fixture(autouse=True)
def fixed_now(monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_NOW", FIXED_NOW)


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "lineage-wiki" in result.output


def test_init_scaffolds_repo(tmp_path):
    result = runner.invoke(app, ["init", "--root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".lineage-wiki" / "config.example.yml").exists()
    assert (tmp_path / ".lineage-wiki" / "prompts" / "system.md").exists()
    assert (tmp_path / "chains" / "example.yml").exists()
    for rel in INDEX_FILES:
        assert (tmp_path / "okf" / rel).exists(), rel
    # no agent files unless requested
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_agents_flag_is_idempotent(tmp_path):
    runner.invoke(app, ["init", "--root", str(tmp_path), "--agents"])
    first = (tmp_path / "AGENTS.md").read_text()
    assert "## OKF Wiki Context" in first
    assert (tmp_path / "CLAUDE.md").exists()
    runner.invoke(app, ["init", "--root", str(tmp_path), "--agents"])
    assert (tmp_path / "AGENTS.md").read_text() == first


def test_generate_creates_valid_tree(tmp_path):
    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "okf" / "frameworks" / "example-chain.md").exists()
    assert (tmp_path / ".lineage-wiki" / "manifest.yml").exists()
    assert "known gaps recorded:" in result.output
    assert "OK — knowledge graph is clean" in result.output

    validated = runner.invoke(app, ["validate", "--root", str(tmp_path)])
    assert validated.exit_code == 0, validated.output


def test_second_generate_is_noop(tmp_path):
    runner.invoke(app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)])
    manifest_before = (tmp_path / ".lineage-wiki" / "manifest.yml").read_text()

    result = runner.invoke(
        app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "created" not in result.output.replace("known gaps recorded", "")
    assert "unchanged (no-op run)" in result.output
    assert (tmp_path / ".lineage-wiki" / "manifest.yml").read_text() == manifest_before


def test_generate_preserves_human_created_pages(tmp_path):
    human_page = tmp_path / "okf" / "frameworks" / "example-chain.md"
    human_page.parent.mkdir(parents=True)
    human_page.write_text("---\ntype: Framework\ntitle: Mine\n---\n# Mine\n")

    result = runner.invoke(
        app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)]
    )
    assert "skipped   okf/frameworks/example-chain.md" in result.output
    assert human_page.read_text().startswith("---\ntype: Framework\ntitle: Mine")


def test_fail_if_exists_policy(tmp_path):
    runner.invoke(app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)])

    cfg = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    cfg["generation"]["overwrite_policy"] = "fail_if_exists"
    strict_config = tmp_path / "strict.yml"
    strict_config.write_text(yaml.safe_dump(cfg))

    result = runner.invoke(
        app, ["generate", "--config", str(strict_config), "--root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "fail_if_exists" in result.output


def test_validate_fails_on_broken_tree(tmp_path):
    runner.invoke(app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)])
    (tmp_path / "okf" / "metrics" / "rogue.md").write_text("# no frontmatter, TODO\n")
    result = runner.invoke(app, ["validate", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "missing YAML frontmatter" in result.output
    assert "placeholder 'TODO'" in result.output


def test_validate_strict_escalates_warnings(tmp_path):
    runner.invoke(app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)])
    # a hand-written page missing required sections -> warning normally
    (tmp_path / "okf" / "metrics" / "hand.md").write_text(
        "---\ntype: Metric\ntitle: Hand\ndescription: Hand-written.\n---\n# Hand\n\n## Definition\n\nYes.\n"
    )
    ok = runner.invoke(app, ["validate", "--root", str(tmp_path)])
    assert ok.exit_code == 0, ok.output
    strict = runner.invoke(app, ["validate", "--root", str(tmp_path), "--strict"])
    assert strict.exit_code == 1


def test_update_noop_cli(tmp_path):
    runner.invoke(app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)])
    result = runner.invoke(
        app, ["update", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "no-op: no source changes detected" in result.output


def test_update_prints_impact_plan(tmp_path):
    runner.invoke(app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)])
    doc = tmp_path / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("# Methodology\n")

    result = runner.invoke(
        app, ["update", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "evidence changes:" in result.output
    assert "raw doc `raw_files/example/methodology.md` changed" in result.output
    assert "impact plan (pages to consider):" in result.output
    assert "updated   okf/frameworks/example-chain.md" in result.output


def test_update_without_manifest_fails(tmp_path):
    result = runner.invoke(
        app, ["update", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "generate" in result.output


def test_generate_bad_config_path(tmp_path):
    result = runner.invoke(
        app, ["generate", "--config", str(tmp_path / "nope.yml"), "--root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "not found" in result.output
