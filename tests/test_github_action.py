"""`init --github-action` workflow generation."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import yaml
from typer.testing import CliRunner

from lineage_wiki.agent.runner import run_init
from lineage_wiki.cli import app
from lineage_wiki.github_action import WORKFLOW_REL_PATH, render_workflow

runner = CliRunner()

SNAPSHOT = Path(__file__).parent / "snapshots" / "github" / "lineage-wiki-update.yml"


def test_workflow_matches_snapshot():
    rendered = render_workflow()
    if os.environ.get("LW_UPDATE_SNAPSHOTS"):
        if SNAPSHOT.parent.exists():
            shutil.rmtree(SNAPSHOT.parent)
        SNAPSHOT.parent.mkdir(parents=True)
        SNAPSHOT.write_text(rendered, encoding="utf-8")
    assert SNAPSHOT.exists(), "snapshot missing — run with LW_UPDATE_SNAPSHOTS=1"
    assert rendered == SNAPSHOT.read_text(encoding="utf-8"), (
        "workflow snapshot drift — regenerate with LW_UPDATE_SNAPSHOTS=1 and review"
    )


def test_workflow_is_valid_yaml_with_required_steps():
    data = yaml.safe_load(render_workflow())
    assert data["name"] == "Lineage Wiki Update"
    triggers = data[True] if True in data else data["on"]  # YAML parses `on:` as True
    assert "schedule" in triggers and "workflow_dispatch" in triggers

    job = data["jobs"]["update"]
    assert job["runs-on"] == "ubuntu-latest"
    steps = job["steps"]
    by_name = {step["name"]: step for step in steps}

    assert by_name["Install Python"]["uses"].startswith("actions/setup-python@")
    assert 'pip install "lineage-wiki[bigquery]"' in by_name["Install lineage-wiki"]["run"]
    assert job["env"]["LINEAGE_WIKI_CONFIG"] == "chains/example.yml"
    update_run = by_name["Update OKF pages from configured sources"]["run"]
    assert "chains/*.yml" not in update_run and "for config in" not in update_run
    assert 'lineage-wiki update --config "$LINEAGE_WIKI_CONFIG"' in update_run
    assert by_name["Validate the knowledge graph"]["run"] == "lineage-wiki validate"

    pr = by_name["Open a pull request with the updates"]
    assert pr["if"] == "steps.changes.outputs.changed == 'true'"
    assert pr["with"]["token"] == "${{ secrets.GITHUB_TOKEN }}"
    # Only OKF content is ever staged into the PR.
    assert set(pr["with"]["add-paths"].split()) == {"okf/", ".lineage-wiki/"}


def test_workflow_references_secrets_without_values():
    text = render_workflow()
    for secret in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GCP_SERVICE_ACCOUNT_JSON", "GITHUB_TOKEN"):
        assert f"secrets.{secret}" in text
    # Secrets only appear as ${{ secrets.* }} expressions — no literal values.
    for line in text.splitlines():
        if "secrets." in line:
            assert re.search(r"\$\{\{\s*secrets\.[A-Z_]+\s*\}\}", line), line
    # Credentials are written outside the checkout, and never committed.
    assert "$RUNNER_TEMP/gcp-service-account.json" in text


def test_init_flag_writes_workflow_idempotently(tmp_path):
    result = runner.invoke(app, ["init", "--root", str(tmp_path), "--github-action"])
    assert result.exit_code == 0, result.output
    assert f"created   {WORKFLOW_REL_PATH}" in result.output
    workflow = tmp_path / WORKFLOW_REL_PATH
    assert workflow.read_text(encoding="utf-8") == render_workflow()

    again = run_init(tmp_path, github_action=True)
    assert WORKFLOW_REL_PATH in again.skipped
    assert workflow.read_text(encoding="utf-8") == render_workflow()


def test_workflow_not_written_without_flag(tmp_path):
    run_init(tmp_path)
    assert not (tmp_path / ".github").exists()
