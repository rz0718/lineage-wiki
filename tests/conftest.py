from pathlib import Path

import pytest

from lineage_wiki.config import ChainConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "chains" / "example.yml"

FIXED_NOW = "2026-07-03T00:00:00Z"


@pytest.fixture
def example_cfg() -> ChainConfig:
    return load_config(EXAMPLE_CONFIG)
