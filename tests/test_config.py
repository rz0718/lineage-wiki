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
    cfg = ChainConfig.model_validate({"chain": {"id": "example_revenue", "name": "Example Revenue"}})
    assert cfg.chain.slug == "example-revenue"  # derived from id
    assert cfg.generation.output_dir == "okf"
    assert cfg.generation.overwrite_policy == "update_existing"
    assert cfg.sources.repos == []
    assert cfg.model.temperature == 0.0


def test_components_config_parses():
    cfg = ChainConfig.model_validate(
        {
            "chain": {"id": "example_revenue", "name": "Example Revenue"},
            "sources": {
                "components": [
                    {
                        "name": "Settled Revenue",
                        "description": "Closed-position value movement.",
                        "code_ref": "example-revenue-engine",
                        "output_refs": ["project.dataset.example_revenue_daily"],
                    },
                    {"name": "Projected Value"},
                ]
            },
        }
    )
    first, second = cfg.sources.components
    assert first.name == "Settled Revenue"
    assert first.code_ref == "example-revenue-engine"
    assert first.output_refs == ["project.dataset.example_revenue_daily"]
    assert second.description == "" and second.code_ref is None
    assert second.output_refs == []


def test_components_default_to_empty(example_cfg):
    assert example_cfg.sources.components == []


def test_component_requires_non_empty_name(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text(
        "chain:\n  id: x\n  name: X\n"
        "sources:\n  components:\n    - name: '  '\n"
    )
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(path)


def test_component_unknown_key_fails(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text(
        "chain:\n  id: x\n  name: X\n"
        "sources:\n  components:\n    - name: A\n      formula: invented\n"
    )
    with pytest.raises(ConfigError, match="formula"):
        load_config(path)


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
