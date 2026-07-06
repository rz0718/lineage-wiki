"""LLM layer: providers, configure, grounding rules, and the mocked pipeline.

Every test runs against canned responses — no live model, ever. The
deterministic default path must keep working with no provider configured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lineage_wiki.agent.grounding import Claim, Conflict, GroundingContext
from lineage_wiki.agent.runner import run_generate, run_update
from lineage_wiki.cli import app
from lineage_wiki.config import BigQuerySource, load_config
from lineage_wiki.connectors.bigquery_connector import (
    BigQueryLoadResult,
    ColumnSchema,
    TableSchema,
)
from lineage_wiki.credentials import (
    config_path,
    load_local_config,
    resolve_llm_provider,
    save_local_config,
    LocalModelConfig,
)
from lineage_wiki.ingestion.evidence import EvidenceItem
from lineage_wiki.ingestion.source_loader import EvidenceBundle
from lineage_wiki.providers import (
    MockProvider,
    ProviderError,
    build_provider,
    validate_api_key_env,
)

from .conftest import FIXED_NOW, REPO_ROOT

runner = CliRunner()

EXAMPLE_CONFIG = REPO_ROOT / "chains" / "example.yml"
LLM_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "llm_responses.yml"
BQ_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bigquery_schemas.yml"

SNAPSHOT_TABLE = "example-project.analytics.example_daily_snapshot"
RAW_DOC_ID = "raw-doc:raw_files/example/methodology.md"
SCHEMA_ID = f"bq-schema:{SNAPSHOT_TABLE}"


# --- providers ---------------------------------------------------------------


def test_mock_provider_string_list_and_dict_entries(tmp_path):
    provider = MockProvider(
        responses={
            "extractor": '{"claims": []}',
            "writer": {"page-a": "A", "page-b": "B"},
            "reviewer": ["first", "second"],
        }
    )
    assert provider.complete(stage="extractor", system="", prompt="x").text == '{"claims": []}'
    assert provider.complete(stage="writer", system="", prompt="... page-b ...").text == "B"
    assert provider.complete(stage="reviewer", system="", prompt="").text == "first"
    assert provider.complete(stage="reviewer", system="", prompt="").text == "second"
    assert provider.complete(stage="reviewer", system="", prompt="").text == "second"
    with pytest.raises(ProviderError, match="no response for stage"):
        provider.complete(stage="page_planner", system="", prompt="")


def test_openai_provider_requires_env_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = build_provider(provider="openai", model="gpt-x")
    with pytest.raises(ProviderError, match=r"\$OPENAI_API_KEY"):
        provider.complete(stage="extractor", system="s", prompt="p")


def test_anthropic_provider_requires_env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = build_provider(provider="anthropic", model="claude-x")
    with pytest.raises(ProviderError, match=r"\$ANTHROPIC_API_KEY"):
        provider.complete(stage="extractor", system="s", prompt="p")


def test_build_provider_rejects_unknown():
    with pytest.raises(ProviderError, match="unknown LLM provider"):
        build_provider(provider="bard", model="x")


def test_api_key_env_must_be_a_name_not_a_value():
    assert validate_api_key_env("OPENAI_API_KEY") == "OPENAI_API_KEY"
    for secret_looking in ("sk-abc123", "Bearer xyz", "my key"):
        with pytest.raises(ProviderError, match="never stores API keys"):
            validate_api_key_env(secret_looking)


def test_resolution_order(monkeypatch):
    # 1. fixtures env wins
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", str(LLM_FIXTURES))
    assert resolve_llm_provider("openai", "gpt-x").name == "mock"
    # 2. local config
    monkeypatch.delenv("LINEAGE_WIKI_LLM_FIXTURES")
    save_local_config(LocalModelConfig(provider="anthropic", model="claude-x"))
    assert resolve_llm_provider("openai", "gpt-x").name == "anthropic"
    # 3. chain config fallback
    config_path().unlink()
    assert resolve_llm_provider("openai", "gpt-x").name == "openai"
    # 4. nothing configured -> clear error
    with pytest.raises(ProviderError, match="lineage-wiki configure"):
        resolve_llm_provider("", "")


# --- configure ---------------------------------------------------------------


def test_configure_writes_local_config_without_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    result = runner.invoke(
        app,
        [
            "configure",
            "--provider", "openai",
            "--model", "gpt-x",
            "--api-key-env", "OPENAI_API_KEY",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no secrets stored" in result.output
    assert "sk-super-secret-value" not in result.output

    path = config_path()
    text = path.read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in text
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_local_config()
    assert loaded.provider == "openai" and loaded.model == "gpt-x"

    shown = runner.invoke(app, ["configure", "--show"])
    assert shown.exit_code == 0
    assert "currently set" in shown.output
    assert "sk-super-secret-value" not in shown.output


def test_configure_rejects_key_material():
    result = runner.invoke(
        app,
        ["configure", "--provider", "openai", "--model", "m", "--api-key-env", "sk-oops"],
    )
    assert result.exit_code == 1
    assert "never stores API keys" in result.output


def test_configure_show_before_any_config():
    result = runner.invoke(app, ["configure", "--show"])
    assert result.exit_code == 0
    assert "no local model config yet" in result.output


# --- grounding rules -----------------------------------------------------------


def _bundle() -> EvidenceBundle:
    doc = EvidenceItem(
        id=RAW_DOC_ID,
        source_type="raw_doc",
        source_uri="raw_files/example/methodology.md",
        title="Methodology",
        content=(
            "# Methodology\n\n"
            "Total value = quantity * price\n"
            "The daily snapshot pipeline is covered by this method.\n"
        ),
    )
    bundle = EvidenceBundle(raw_docs=[doc])
    schema = TableSchema(
        table_id=SNAPSHOT_TABLE,
        columns=[ColumnSchema(name="snapshot_date", type="DATE"),
                 ColumnSchema(name="total_value", type="NUMERIC")],
    )
    bundle.bigquery = BigQueryLoadResult(
        source=BigQuerySource(tables=[SNAPSHOT_TABLE]),
        available=True,
        client_kind="fixtures",
        schemas={SNAPSHOT_TABLE: schema},
        items=[schema.to_evidence()],
    )
    return bundle


def test_claims_must_cite_known_evidence():
    ctx = GroundingContext(_bundle())
    ok = ctx.check_claim(
        Claim(
            id="c",
            kind="fact",
            text="The daily snapshot pipeline is covered by this method.",
            evidence_ids=[RAW_DOC_ID],
            quote="daily snapshot pipeline is covered",
        )
    )
    assert ok.accepted
    bad = ctx.check_claim(Claim(id="c", kind="fact", text="x", evidence_ids=["nope"]))
    assert not bad.accepted and "unknown evidence ids" in bad.reason
    none = ctx.check_claim(Claim(id="c", kind="fact", text="x"))
    assert not none.accepted


def test_natural_language_claims_require_verbatim_quote_in_cited_source():
    ctx = GroundingContext(_bundle())
    missing_quote = ctx.check_claim(
        Claim(id="f1", kind="fact", text="Covered by the method.", evidence_ids=[RAW_DOC_ID])
    )
    assert not missing_quote.accepted and "no supporting quote" in missing_quote.reason

    fabricated = ctx.check_claim(
        Claim(
            id="f2",
            kind="fact",
            text="Unsupported fact with a valid-looking citation.",
            evidence_ids=[RAW_DOC_ID],
            quote="this quote does not appear",
        )
    )
    assert not fabricated.accepted and "quote not found" in fabricated.reason


def test_formula_requires_verbatim_quote_in_cited_source():
    ctx = GroundingContext(_bundle())
    ok = ctx.check_claim(
        Claim(
            id="f1", kind="formula", text="total_value = quantity * price",
            evidence_ids=[RAW_DOC_ID],
            quote="total   VALUE =\nquantity * price",  # whitespace/case-insensitive
        )
    )
    assert ok.accepted
    missing_quote = ctx.check_claim(
        Claim(id="f2", kind="formula", text="x = y", evidence_ids=[RAW_DOC_ID])
    )
    assert not missing_quote.accepted and "no supporting quote" in missing_quote.reason
    fabricated = ctx.check_claim(
        Claim(id="f3", kind="formula", text="x = y", evidence_ids=[RAW_DOC_ID],
              quote="not in the doc at all")
    )
    assert not fabricated.accepted and "quote not found" in fabricated.reason
    wrong_source = ctx.check_claim(
        Claim(id="f4", kind="formula", text="x = y", evidence_ids=[SCHEMA_ID],
              quote="total value")
    )
    assert not wrong_source.accepted and "methodology/code/note" in wrong_source.reason


def test_column_claims_require_bq_schema_evidence():
    ctx = GroundingContext(_bundle())
    ok = ctx.check_claim(
        Claim(id="c1", kind="column", text="Column total_value holds the product.",
              evidence_ids=[SCHEMA_ID])
    )
    assert ok.accepted
    no_schema = ctx.check_claim(
        Claim(id="c2", kind="column", text="Column total_value ...",
              evidence_ids=[RAW_DOC_ID])
    )
    assert not no_schema.accepted and "no ingested BigQuery schema" in no_schema.reason
    bogus_column = ctx.check_claim(
        Claim(id="c3", kind="column", text="Column made_up_col exists.",
              evidence_ids=[SCHEMA_ID])
    )
    assert not bogus_column.accepted and "names no column" in bogus_column.reason


def test_code_path_claims_require_repo_evidence():
    ctx = GroundingContext(_bundle())
    bad = ctx.check_claim(
        Claim(id="p1", kind="code_path", text="Computed in main.py",
              evidence_ids=[RAW_DOC_ID])
    )
    assert not bad.accepted and "repository file evidence" in bad.reason


def test_conflicts_require_known_evidence():
    ctx = GroundingContext(_bundle())
    ok = ctx.check_conflict(
        Conflict(
            topic="Formula wording",
            detail="doc vs schema",
            evidence_ids=[RAW_DOC_ID, SCHEMA_ID],
            quotes=["Total value = quantity * price", "total_value"],
        )
    )
    assert ok.accepted
    bad = ctx.check_conflict(Conflict(topic="x", detail="y", evidence_ids=["nope"]))
    assert not bad.accepted
    unquoted = ctx.check_conflict(
        Conflict(topic="x", detail="y", evidence_ids=[RAW_DOC_ID, SCHEMA_ID])
    )
    assert not unquoted.accepted and "supporting quotes" in unquoted.reason
    fabricated = ctx.check_conflict(
        Conflict(
            topic="x",
            detail="y",
            evidence_ids=[RAW_DOC_ID, SCHEMA_ID],
            quotes=["not in any cited evidence"],
        )
    )
    assert not fabricated.accepted and "quote not found" in fabricated.reason


def test_section_bodies_must_cite_and_must_not_contain_sql():
    ctx = GroundingContext(_bundle())
    accepted_claims = [
        Claim(
            id="f1",
            kind="formula",
            text="total_value = quantity * price",
            evidence_ids=[RAW_DOC_ID],
            quote="Total value = quantity * price",
        ),
        Claim(
            id="c1",
            kind="column",
            text="Column total_value holds the product.",
            evidence_ids=[SCHEMA_ID],
        ),
    ]
    ok = ctx.check_section_body(
        f"`total_value = quantity * price` [src: {RAW_DOC_ID}]", accepted_claims
    )
    assert ok.accepted
    uncited = ctx.check_section_body("Plain assertion.", accepted_claims)
    assert not uncited.accepted and "citation markers" in uncited.reason
    foreign = ctx.check_section_body("Text. [src: other-id]", accepted_claims)
    assert not foreign.accepted and "outside the accepted claims" in foreign.reason
    unsupported = ctx.check_section_body(
        f"Valid-looking but unrelated assertion. [src: {RAW_DOC_ID}]",
        accepted_claims,
    )
    assert not unsupported.accepted and "not traceable" in unsupported.reason
    sql_fence = ctx.check_section_body(
        f"```sql\nSELECT 1\n```\n[src: {RAW_DOC_ID}]", accepted_claims
    )
    assert not sql_fence.accepted and "SQL is not allowed" in sql_fence.reason
    sql_inline = ctx.check_section_body(
        f"Run select x from t daily. [src: {RAW_DOC_ID}]", accepted_claims
    )
    assert not sql_inline.accepted


# --- end-to-end pipeline with mocked responses -------------------------------------


def _setup_target(tmp_path, monkeypatch) -> Path:
    doc = tmp_path / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        (
            "# Example Methodology\n\n"
            "This methodology covers the daily example metric snapshot pipeline.\n\n"
            "Total value = quantity * price\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(BQ_FIXTURES))
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", str(LLM_FIXTURES))
    return tmp_path


def test_generate_use_llm_end_to_end(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root), "--use-llm"],
    )
    assert result.exit_code == 0, result.output
    assert "llm       claims accepted: 3, rejected: 3" in result.output

    framework = (root / "okf" / "frameworks" / "example-chain.md").read_text(
        encoding="utf-8"
    )
    # Accepted, cited sections were written.
    assert "daily example metric snapshot pipeline. [src: " in framework
    assert "`total_value = quantity * price` [src: " in framework
    # The rejected formula became a Known Gap instead of being published.
    assert "- (llm) A proposed formula (profit = revenue - cost) was rejected" in framework
    assert "profit = revenue - cost\n" not in framework.split("## Known Gaps")[0]
    # The accepted conflict became a Known Doc-vs-Code Divergence.
    assert "**Formula wording**" in framework
    # The model's attempt to write Known Gaps directly was blocked.
    assert "The model must not write this section." not in framework

    # SQL-bearing section was rejected: the output page keeps its scaffold.
    output_page = (root / "okf" / "outputs" / "example-daily-snapshot.md").read_text(
        encoding="utf-8"
    )
    assert "SELECT * FROM example_daily_snapshot" not in output_page
    assert "```sql" not in output_page

    # Detailed transcript lands in run metadata, not in pages.
    run_files = sorted((root / ".lineage-wiki" / "runs").glob("*generate-llm.json"))
    assert run_files, "generate-llm transcript missing"
    transcript = json.loads(run_files[-1].read_text(encoding="utf-8"))
    assert len(transcript["claims_accepted"]) == 3
    assert any("SQL is not allowed" in r for r in transcript["rejected"])
    assert any("not in the allowed list" in r for r in transcript["rejected"])


def test_llm_sections_survive_deterministic_rerun(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    first = run_generate(cfg, root, FIXED_NOW, use_llm=True)
    assert first.llm

    framework_rel = "okf/frameworks/example-chain.md"
    before = (root / framework_rel).read_text(encoding="utf-8")

    second = run_generate(cfg, root, FIXED_NOW)  # deterministic, no LLM
    after = (root / framework_rel).read_text(encoding="utf-8")
    assert framework_rel in second.unchanged
    assert after == before  # cited sections, gap bullets, divergences all kept

    update = run_update(cfg, root, FIXED_NOW)
    assert update.noop


def test_dry_run_with_llm_writes_nothing(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root),
            "--use-llm", "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "llm       claims accepted: 3" in result.output
    assert not (root / "okf").exists()
    assert not (root / ".lineage-wiki").exists()


def test_default_generate_never_touches_a_provider(tmp_path, monkeypatch):
    # A broken fixtures path would explode if the deterministic path ever
    # resolved a provider.
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", "/does/not/exist.yml")
    result = runner.invoke(
        app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output


def test_use_llm_without_any_provider_fails_clearly(tmp_path):
    cfg_text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    data = yaml.safe_load(cfg_text)
    data["model"] = {"provider": "", "model": ""}
    config = tmp_path / "chain.yml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = runner.invoke(
        app, ["generate", "--config", str(config), "--root", str(tmp_path), "--use-llm"]
    )
    assert result.exit_code == 1
    assert "no LLM provider configured" in result.output


def test_bad_json_from_model_fails_the_stage_clearly(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    broken = tmp_path / "broken.yml"
    broken.write_text("responses:\n  page_planner: 'not json'\n", encoding="utf-8")
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", str(broken))
    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root), "--use-llm"],
    )
    assert result.exit_code == 1
    assert "page_planner stage did not return valid JSON" in result.output


def test_prompt_stub_overrides_are_loaded(tmp_path):
    from lineage_wiki.agent.prompts import load_prompts

    defaults = load_prompts(tmp_path)
    assert "Never invent formulas" in defaults.system

    override = tmp_path / ".lineage-wiki" / "prompts" / "system.md"
    override.parent.mkdir(parents=True)
    override.write_text("# Custom system prompt\n", encoding="utf-8")
    loaded = load_prompts(tmp_path)
    assert loaded.system == "# Custom system prompt\n"
    assert loaded.extractor == defaults.extractor
