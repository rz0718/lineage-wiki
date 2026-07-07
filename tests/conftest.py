from pathlib import Path

import pytest

from lineage_wiki.config import ChainConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "chains" / "example.yml"

FIXED_NOW = "2026-07-03T00:00:00Z"


@pytest.fixture(autouse=True)
def _bigquery_offline(monkeypatch):
    """Unit tests never touch real BigQuery — even on machines with the
    `bigquery` extra and live credentials installed. Tests that want mocked
    schemas set LINEAGE_WIKI_BQ_FIXTURES, which takes precedence."""
    monkeypatch.delenv("LINEAGE_WIKI_BQ_FIXTURES", raising=False)
    monkeypatch.setenv("LINEAGE_WIKI_BQ_OFFLINE", "1")


@pytest.fixture(autouse=True)
def _slack_offline(monkeypatch):
    """Unit tests never touch the real Slack API. Tests that want mocked
    messages set LINEAGE_WIKI_SLACK_FIXTURES, which takes precedence."""
    monkeypatch.delenv("LINEAGE_WIKI_SLACK_FIXTURES", raising=False)
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_OFFLINE", "1")


@pytest.fixture(autouse=True)
def _llm_isolated(monkeypatch, tmp_path_factory):
    """Unit tests never call a live model and never read the developer's
    real ~/.lineage-wiki config. Tests that want mocked responses set
    LINEAGE_WIKI_LLM_FIXTURES or pass a provider explicitly."""
    monkeypatch.delenv("LINEAGE_WIKI_LLM_FIXTURES", raising=False)
    monkeypatch.setenv(
        "LINEAGE_WIKI_HOME", str(tmp_path_factory.mktemp("lineage-wiki-home"))
    )


@pytest.fixture
def example_cfg() -> ChainConfig:
    return load_config(EXAMPLE_CONFIG)
