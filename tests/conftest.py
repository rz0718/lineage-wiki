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


@pytest.fixture
def example_cfg() -> ChainConfig:
    return load_config(EXAMPLE_CONFIG)
