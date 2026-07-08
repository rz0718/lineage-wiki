"""BigQuery Verification Mode phase 1: schema_only + profile via verify-bq.

Every test uses mocked BigQuery responses (fixture schema/profile clients);
conftest forces offline mode so nothing here can reach real BigQuery. Real
BigQuery coverage is integration-only: run
`LINEAGE_WIKI_BQ_INTEGRATION=1 pytest -m bq_integration` with credentials.
"""

import json
import os
from pathlib import Path

import pytest

from lineage_wiki.agent.runner import run_generate
from lineage_wiki.config import (
    ConfigError,
    ProfilingSpec,
    VerificationTableSpec,
    load_config,
)
from lineage_wiki.connectors.bigquery_connector import (
    FixtureBigQueryClient,
    parse_table_name,
)
from lineage_wiki.ingestion.bq_profiler import (
    FixtureProfileClient,
    build_profile_plan,
    pick_date_column,
    run_profile_plan,
)
from lineage_wiki.okf.verifier import VerificationError, run_verify_bq

from .conftest import EXAMPLE_CONFIG, FIXED_NOW, REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bigquery_schemas.yml"
SNAPSHOT_TABLE = "example-project.analytics.example_daily_snapshot"


@pytest.fixture
def wiki_root(tmp_path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


@pytest.fixture
def schema_client() -> FixtureBigQueryClient:
    return FixtureBigQueryClient.from_file(FIXTURES)


@pytest.fixture
def profile_client() -> FixtureProfileClient:
    return FixtureProfileClient.from_file(FIXTURES)


@pytest.fixture
def snapshot_schema(schema_client):
    return schema_client.get_table_schema(parse_table_name(SNAPSHOT_TABLE))


@pytest.fixture
def verify_cfg(example_cfg):
    """Example config with fixture-friendly verification settings."""
    cfg = example_cfg.model_copy(deep=True)
    cfg.bigquery_verification.enabled = True
    return cfg


def _verify(cfg, root, **kw):
    kw.setdefault("now", FIXED_NOW)
    return run_verify_bq(cfg, root, **kw)


# --- config -----------------------------------------------------------------------


def test_verification_config_defaults(tmp_path):
    cfg_file = tmp_path / "chain.yml"
    cfg_file.write_text("chain: {id: c, name: C}\n")
    spec = load_config(cfg_file).bigquery_verification
    assert spec.enabled is False
    assert spec.mode == "schema_only"
    assert spec.max_bytes_billed == 1_000_000_000
    assert spec.profiling.date_window_days == 90
    assert spec.store_results.okf_pages == "summary_only"
    assert spec.store_results.run_metadata == "detailed"


def test_example_config_parses_verification_block(example_cfg):
    spec = example_cfg.bigquery_verification
    assert spec.enabled is True
    assert spec.mode == "profile"
    table_spec = spec.table_spec(SNAPSHOT_TABLE)
    assert table_spec.expect_columns == ["snapshot_date", "asset_id", "total_value"]
    assert table_spec.date_column == "snapshot_date"


def test_unknown_verification_keys_rejected(tmp_path):
    cfg_file = tmp_path / "chain.yml"
    cfg_file.write_text(
        "chain: {id: c, name: C}\nbigquery_verification: {run_sql: true}\n"
    )
    with pytest.raises(ConfigError):
        load_config(cfg_file)


# --- profiling query plans -----------------------------------------------------------


def test_profile_plan_is_safe_aggregate_sql(snapshot_schema):
    plan = build_profile_plan(snapshot_schema, ProfilingSpec())
    assert "SELECT *" not in plan.sql
    assert "COUNT(*) AS row_count" in plan.sql
    assert f"FROM `{SNAPSHOT_TABLE}`" in plan.sql
    assert (
        "WHERE `snapshot_date` >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)"
        in plan.sql
    )
    assert "COUNTIF(`price` IS NULL) AS null__price" in plan.sql
    assert "APPROX_COUNT_DISTINCT(`asset_id`) AS distinct__asset_id" in plan.sql
    assert "MIN(`quantity`) AS min__quantity" in plan.sql
    assert "MAX(`quantity`) AS max__quantity" in plan.sql
    # Aggregates only — no bare column selection anywhere.
    select_body = plan.sql.split("FROM")[0]
    for line in select_body.splitlines()[1:]:
        assert line.lstrip().startswith(("COUNT", "COUNTIF", "APPROX_COUNT_DISTINCT", "MIN", "MAX"))


def test_profile_plan_respects_include_flags(snapshot_schema):
    profiling = ProfilingSpec(
        include_row_count=False,
        include_null_counts=False,
        include_distinct_counts=False,
        include_min_max=False,
    )
    plan = build_profile_plan(snapshot_schema, profiling)
    # Only the date-coverage aggregates remain.
    kinds = {m.kind for m in plan.metrics}
    assert kinds == {"date_min", "date_max"}


def test_profile_plan_uses_configured_columns(snapshot_schema):
    table_spec = VerificationTableSpec(
        table=SNAPSHOT_TABLE,
        null_columns=["price"],
        dimension_columns=["asset_id"],
        numeric_columns=["total_value"],
    )
    plan = build_profile_plan(snapshot_schema, ProfilingSpec(), table_spec)
    null_cols = [m.column for m in plan.metrics if m.kind == "null_count"]
    num_cols = sorted({m.column for m in plan.metrics if m.kind in ("min", "max")})
    assert null_cols == ["price"]
    assert num_cols == ["total_value"]


def test_date_column_detection_order(snapshot_schema):
    # Config wins, then partition field, then first date-typed column.
    spec = VerificationTableSpec(table=SNAPSHOT_TABLE, date_column="snapshot_date")
    assert pick_date_column(snapshot_schema, spec).name == "snapshot_date"
    assert pick_date_column(snapshot_schema, None).name == "snapshot_date"  # partition
    unpartitioned = snapshot_schema.model_copy(update={"partitioning": None})
    assert pick_date_column(unpartitioned, None).name == "snapshot_date"  # first DATE


def test_profile_plan_window_predicate_matches_column_type(snapshot_schema):
    ts_schema = snapshot_schema.model_copy(deep=True)
    ts_schema.partitioning = None
    ts_schema.columns[0].type = "TIMESTAMP"
    plan = build_profile_plan(ts_schema, ProfilingSpec(date_window_days=30))
    assert "TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)" in plan.sql


def test_profile_plan_skips_nested_and_repeated_columns(snapshot_schema):
    schema = snapshot_schema.model_copy(deep=True)
    schema.columns[2].name = "record.child"  # flattened RECORD child
    schema.columns[3].mode = "REPEATED"
    plan = build_profile_plan(schema, ProfilingSpec())
    profiled = {m.column for m in plan.metrics if m.column}
    assert "record.child" not in profiled
    assert schema.columns[3].name not in profiled


def test_fixture_profile_client_answers_plan(snapshot_schema, profile_client):
    plan = build_profile_plan(snapshot_schema, ProfilingSpec())
    result = run_profile_plan(plan, profile_client, max_bytes_billed=10**9)
    assert result.row_count == 187
    assert result.date_min == "2026-04-05"
    assert result.null_counts["price"] == 2
    assert result.distinct_counts["asset_id"] == 3
    assert result.min_max["quantity"] == {"min": 0, "max": 250}


# --- verify-bq: schema_only ----------------------------------------------------------


def test_schema_only_verification_passes(verify_cfg, wiki_root, schema_client):
    verify_cfg.bigquery_verification.mode = "schema_only"
    result = _verify(verify_cfg, wiki_root, schema_client=schema_client)
    assert result.ok is True
    (tv,) = result.tables
    assert tv.exists is True
    assert tv.fingerprint.startswith("sha256:")
    assert tv.missing_columns == []
    assert any("expected columns are present" in c for c in tv.conclusions)
    assert any("Time-partitioned (DAY) on `snapshot_date`" in c for c in tv.conclusions)
    assert tv.profile is None  # schema_only never queries


def test_schema_only_fails_on_missing_expected_column(verify_cfg, wiki_root, schema_client):
    verify_cfg.bigquery_verification.mode = "schema_only"
    verify_cfg.bigquery_verification.tables[0].expect_columns.append("ghost_col")
    result = _verify(verify_cfg, wiki_root, schema_client=schema_client)
    assert result.ok is False
    assert result.tables[0].missing_columns == ["ghost_col"]


def test_schema_only_fails_on_missing_table(verify_cfg, wiki_root, schema_client):
    verify_cfg.sources.bigquery.tables = ["example-project.analytics.ghost"]
    verify_cfg.bigquery_verification.mode = "schema_only"
    result = _verify(verify_cfg, wiki_root, schema_client=schema_client)
    assert result.ok is False
    assert result.tables[0].exists is False


# --- verify-bq: profile ----------------------------------------------------------------


def test_profile_verification_records_details_in_run_only(
    verify_cfg, wiki_root, schema_client, profile_client, monkeypatch
):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    run_generate(verify_cfg, wiki_root, now=FIXED_NOW)

    result = _verify(
        verify_cfg, wiki_root, schema_client=schema_client, profile_client=profile_client
    )
    assert result.ok is True
    (tv,) = result.tables
    assert tv.profile.row_count == 187

    # Detailed values (SQL + query results) live in the run file...
    run_file = wiki_root / result.run_file
    detail = json.loads(run_file.read_text())
    table_detail = detail["tables"][0]
    assert table_detail["profile"]["row_count"] == 187
    assert table_detail["profile"]["null_counts"]["price"] == 2
    assert "SELECT" in table_detail["profile"]["sql"]
    assert detail["max_bytes_billed"] == 1_000_000_000

    # ...while the OKF page gets conclusions only — no live values.
    page = (wiki_root / "okf" / "outputs" / "example-daily-snapshot.md").read_text()
    assert "okf/outputs/example-daily-snapshot.md" in result.pages_updated
    assert "Verified from BigQuery schema metadata and safe aggregate profiling." in page
    assert "Rows are present in the profiled window." in page
    assert "NULLs present in: `price`" in page
    assert result.run_file in page
    section = page.split("## Verification Status")[1].split("##")[0]
    # The schema-fingerprint hex may legitimately contain digit runs; every
    # other line must be free of profiled values.
    value_lines = "\n".join(
        line for line in section.splitlines() if "fingerprinted" not in line
    )
    for live_value in ("187", "2026-04-05", "1250000", "250"):
        assert live_value not in value_lines


def test_verify_bq_normalizes_redundant_output_dir(
    verify_cfg, wiki_root, schema_client, profile_client, monkeypatch
):
    """generate accepts an output_dir that redundantly includes the root
    (e.g. ``../wiki/okf``) and records manifest ownership as ``okf/...``;
    verify-bq must normalize the same way or it skips its own pages as
    not tool-generated."""
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    verify_cfg.generation.output_dir = f"../{wiki_root.name}/okf"
    run_generate(verify_cfg, wiki_root, now=FIXED_NOW)

    result = _verify(
        verify_cfg, wiki_root, schema_client=schema_client, profile_client=profile_client
    )
    assert "okf/outputs/example-daily-snapshot.md" in result.pages_updated
    assert not result.pages_skipped
    page = (wiki_root / "okf" / "outputs" / "example-daily-snapshot.md").read_text()
    assert "Verified from BigQuery schema metadata and safe aggregate profiling." in page


def test_verify_bq_rejects_unsafe_output_dir(verify_cfg, wiki_root, schema_client):
    verify_cfg.generation.output_dir = "../elsewhere/okf"
    with pytest.raises(VerificationError, match="unsafe output_dir"):
        _verify(verify_cfg, wiki_root, schema_client=schema_client)


def test_profile_zero_rows_is_an_issue(
    verify_cfg, wiki_root, schema_client, snapshot_schema
):
    empty_client = FixtureProfileClient(
        {SNAPSHOT_TABLE: {"row_count": 0}}, origin="<memory>"
    )
    result = _verify(
        verify_cfg, wiki_root, schema_client=schema_client, profile_client=empty_client
    )
    assert result.ok is False
    assert "no rows in the profiled window" in result.tables[0].issues


def test_profile_mode_with_profiling_disabled_runs_schema_checks(
    verify_cfg, wiki_root, schema_client
):
    verify_cfg.bigquery_verification.profiling.enabled = False
    result = _verify(verify_cfg, wiki_root, schema_client=schema_client)
    assert result.ok is True
    assert result.tables[0].profile is None
    assert any("profiling.enabled is false" in n for n in result.notes)


def test_store_results_none_skips_page_updates(
    verify_cfg, wiki_root, schema_client, profile_client, monkeypatch
):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    run_generate(verify_cfg, wiki_root, now=FIXED_NOW)
    verify_cfg.bigquery_verification.store_results.okf_pages = "none"
    result = _verify(
        verify_cfg, wiki_root, schema_client=schema_client, profile_client=profile_client
    )
    assert result.pages_updated == []


def test_pages_not_generated_yet_are_skipped(verify_cfg, wiki_root, schema_client, profile_client):
    result = _verify(
        verify_cfg, wiki_root, schema_client=schema_client, profile_client=profile_client
    )
    assert result.pages_updated == []
    assert any("run generate first" in s for s in result.pages_skipped)


# --- verify-bq: guard rails --------------------------------------------------------------


def test_disabled_verification_fails_clearly(example_cfg, wiki_root):
    cfg = example_cfg.model_copy(deep=True)
    cfg.bigquery_verification.enabled = False
    with pytest.raises(VerificationError, match="enabled is false"):
        _verify(cfg, wiki_root)


def test_unimplemented_mode_fails_clearly(verify_cfg, wiki_root):
    verify_cfg.bigquery_verification.mode = "full_verification"
    with pytest.raises(VerificationError, match="not implemented"):
        _verify(verify_cfg, wiki_root)


def test_unavailable_bigquery_fails_clearly(verify_cfg, wiki_root):
    # Offline mode (conftest) and no injected clients.
    with pytest.raises(VerificationError, match="BigQuery is unavailable"):
        _verify(verify_cfg, wiki_root)


def test_sample_rows_are_never_read(verify_cfg, wiki_root, schema_client, profile_client):
    verify_cfg.bigquery_verification.sample_rows.enabled = True
    result = _verify(
        verify_cfg, wiki_root, schema_client=schema_client, profile_client=profile_client
    )
    assert any("sample rows are not read in phase 1" in n for n in result.notes)


# --- CLI ------------------------------------------------------------------------------


def test_cli_verify_bq_with_fixtures(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lineage_wiki.cli import app

    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    monkeypatch.setenv("LINEAGE_WIKI_NOW", FIXED_NOW)
    root = tmp_path / "wiki"
    root.mkdir()
    runner = CliRunner()

    gen = runner.invoke(
        app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root)]
    )
    assert gen.exit_code == 0, gen.output

    result = runner.invoke(
        app, ["verify-bq", "--config", str(EXAMPLE_CONFIG), "--root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "mode      profile" in result.output
    assert SNAPSHOT_TABLE in result.output
    assert "verification sections updated" in result.output
    assert "verify-bq OK." in result.output


def test_cli_verify_bq_unavailable_exits_1(tmp_path):
    from typer.testing import CliRunner

    from lineage_wiki.cli import app

    root = tmp_path / "wiki"
    root.mkdir()
    result = CliRunner().invoke(
        app, ["verify-bq", "--config", str(EXAMPLE_CONFIG), "--root", str(root)]
    )
    assert result.exit_code == 1
    assert "unavailable" in result.output


# --- real BigQuery (integration-only, never in the normal unit run) ---------------------


@pytest.mark.bq_integration
@pytest.mark.skipif(
    not os.environ.get("LINEAGE_WIKI_BQ_INTEGRATION"),
    reason="integration-only: set LINEAGE_WIKI_BQ_INTEGRATION=1 with real credentials",
)
def test_real_bigquery_schema_only(monkeypatch, tmp_path):
    """Schema-only smoke test against a real table; opt-in via env var.
    Configure the target with LINEAGE_WIKI_BQ_INTEGRATION_TABLE."""
    monkeypatch.delenv("LINEAGE_WIKI_BQ_OFFLINE", raising=False)
    monkeypatch.delenv("LINEAGE_WIKI_BQ_FIXTURES", raising=False)
    table = os.environ.get(
        "LINEAGE_WIKI_BQ_INTEGRATION_TABLE", "bigquery-public-data.samples.shakespeare"
    )
    from lineage_wiki.config import BigQuerySource
    from lineage_wiki.connectors.bigquery_connector import load_bigquery_schemas

    load = load_bigquery_schemas(BigQuerySource(tables=[table], required=True))
    assert load.available is True
    schema = load.schemas[table]
    assert schema.columns
    assert schema.fingerprint().startswith("sha256:")
