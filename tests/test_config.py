import pytest

from lineage_wiki.config import ChainConfig, ConfigError, load_config
from lineage_wiki.examples import EXAMPLE_CHAIN_YAML

from .conftest import EXAMPLE_CONFIG


def test_example_config_loads(example_cfg):
    assert example_cfg.chain.id == "example_chain"
    assert example_cfg.chain.slug == "example-chain"
    assert example_cfg.chain.name == "Example Chain"
    assert example_cfg.sources.bigquery.tables == [
        "example-project.analytics.example_daily_snapshot"
    ]
    assert example_cfg.sources.repos[0].symbols == ["compute_daily_snapshot"]
    assert example_cfg.generation.overwrite_policy == "update_existing"
    assert example_cfg.validation.fail_on_placeholders_outside_known_gaps is True


def test_repo_example_matches_packaged_example():
    assert EXAMPLE_CONFIG.read_text(encoding="utf-8") == EXAMPLE_CHAIN_YAML


def test_defaults_are_applied():
    cfg = ChainConfig.model_validate({"chain": {"id": "gold_pnl", "name": "Gold PnL"}})
    assert cfg.chain.slug == "gold-pnl"  # derived from id
    assert cfg.generation.output_dir == "okf"
    assert cfg.generation.overwrite_policy == "update_existing"
    assert cfg.sources.repos == []
    assert cfg.model.temperature == 0.0


def test_missing_chain_fails(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("sources: {}\n")
    with pytest.raises(ConfigError, match="chain"):
        load_config(path)


def test_unknown_key_fails(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("chain:\n  id: x\n  name: X\n  unexpected_key: 1\n")
    with pytest.raises(ConfigError, match="unexpected_key"):
        load_config(path)


def test_invalid_overwrite_policy_fails(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text(
        "chain:\n  id: x\n  name: X\ngeneration:\n  overwrite_policy: clobber\n"
    )
    with pytest.raises(ConfigError, match="overwrite_policy"):
        load_config(path)


def test_missing_file_fails(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yml")
