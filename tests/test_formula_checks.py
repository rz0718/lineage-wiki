"""BigQuery Verification phase 2: deterministic formula checks.

All tests use mocked query results — conftest forces offline mode, and the
fixture clients answer from tests/fixtures/bigquery_schemas.yml or inline
dicts. SQL rendering is snapshot-tested as exact strings.
"""

import json
from pathlib import Path

import pytest

from lineage_wiki.agent.runner import run_generate
from lineage_wiki.config import (
    ConfigError,
    FormulaCheck,
    FormulaChecksSpec,
    load_config,
)
from lineage_wiki.connectors.bigquery_connector import (
    FixtureBigQueryClient,
    parse_table_name,
)
from lineage_wiki.ingestion.bq_formula_verifier import (
    GENUINE_CONFLICT,
    MATCHES,
    MISSING_EVIDENCE,
    SOURCE_STALE,
    build_formula_plan,
    classify,
    extract_identifiers,
    run_formula_check,
)
from lineage_wiki.ingestion.bq_profiler import FixtureProfileClient
from lineage_wiki.okf.verifier import VerificationError, run_verify_bq

from .conftest import FIXED_NOW, REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bigquery_schemas.yml"
SNAPSHOT_TABLE = "example-project.analytics.example_daily_snapshot"
CHECK_NAME = "example_total_value_formula"


@pytest.fixture
def wiki_root(tmp_path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


@pytest.fixture
def schema_client() -> FixtureBigQueryClient:
    return FixtureBigQueryClient.from_file(FIXTURES)


@pytest.fixture
def snapshot_schema(schema_client):
    return schema_client.get_table_schema(parse_table_name(SNAPSHOT_TABLE))


@pytest.fixture
def formula_cfg(example_cfg):
    cfg = example_cfg.model_copy(deep=True)
    cfg.bigquery_verification.enabled = True
    cfg.bigquery_verification.mode = "formula_check"
    return cfg


def _formula_client(checked: int, mismatched: int) -> FixtureProfileClient:
    return FixtureProfileClient(
        {}, formulas={CHECK_NAME: {"checked_rows": checked, "mismatch_rows": mismatched}}
    )


def _check(**overrides) -> FormulaCheck:
    base = dict(
        name=CHECK_NAME,
        table=SNAPSHOT_TABLE,
        expression="total_value",
        expected_expression="quantity * price",
        tolerance_absolute=0.01,
        date_column="snapshot_date",
    )
    base.update(overrides)
    return FormulaCheck(**base)


def _verify(cfg, root, **kw):
    kw.setdefault("now", FIXED_NOW)
    return run_verify_bq(cfg, root, **kw)


# --- config safety -----------------------------------------------------------------


def test_example_config_parses_formula_checks(example_cfg):
    spec = example_cfg.bigquery_verification.formula_checks
    assert spec.enabled is True
    (check,) = spec.checks
    assert check.name == CHECK_NAME
    assert check.expected_expression == "quantity * price"
    assert check.tolerance_absolute == 0.01
    assert spec.tolerance.absolute == 0.01
    assert spec.tolerance.relative == 0.0001


@pytest.mark.parametrize(
    "bad",
    [
        "total_value; DROP TABLE x",
        "total_value -- comment",
        "(SELECT 1)",
        "a UNION b",
        "col /* hidden */",
        "",
    ],
)
def test_unsafe_expressions_rejected(bad):
    with pytest.raises(ValueError):
        _check(expression=bad)


def test_unsafe_expression_rejected_at_config_load(tmp_path):
    cfg_file = tmp_path / "chain.yml"
    cfg_file.write_text(
        "chain: {id: c, name: C}\n"
        "bigquery_verification:\n"
        "  formula_checks:\n"
        "    checks:\n"
        "      - name: bad\n"
        "        table: p.d.t\n"
        "        expression: 'x; DROP TABLE y'\n"
        "        expected_expression: a + b\n"
    )
    with pytest.raises(ConfigError):
        load_config(cfg_file)


def test_extract_identifiers_skips_functions():
    ids = extract_identifiers("ABS(realized_pnl + unrealized_mtm) * 2 + ROUND(x, 2)")
    assert ids == {"realized_pnl", "unrealized_mtm", "x"}


# --- SQL rendering (snapshot tests) ---------------------------------------------------


def test_spec_example_sql_snapshot(snapshot_schema):
    """The rendered SQL for the spec's own example shape, byte for byte."""
    schema = snapshot_schema.model_copy(update={"table_id": SNAPSHOT_TABLE})
    check = _check(tolerance_relative=0.0)
    plan = build_formula_plan(check, FormulaChecksSpec(date_window_days=90), schema)
    assert plan.sql == (
        "SELECT\n"
        "  COUNT(*) AS checked_rows,\n"
        "  COUNTIF(ABS((total_value) - (quantity * price)) > 0.01) AS mismatch_rows\n"
        f"FROM `{SNAPSHOT_TABLE}`\n"
        "WHERE `snapshot_date` >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)"
    )


def test_relative_tolerance_sql_snapshot(snapshot_schema):
    check = _check(tolerance_absolute=0.01, tolerance_relative=0.0001)
    plan = build_formula_plan(check, FormulaChecksSpec(), snapshot_schema)
    assert plan.sql == (
        "SELECT\n"
        "  COUNT(*) AS checked_rows,\n"
        "  COUNTIF(ABS((total_value) - (quantity * price)) > 0.01 + 0.0001 * "
        "ABS(quantity * price)) AS mismatch_rows\n"
        f"FROM `{SNAPSHOT_TABLE}`\n"
        "WHERE `snapshot_date` >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)"
    )


def test_sql_without_date_column_has_no_where(snapshot_schema):
    plan = build_formula_plan(
        _check(date_column=None), FormulaChecksSpec(), snapshot_schema
    )
    assert "WHERE" not in plan.sql
    assert plan.window_days is None


def test_sql_never_selects_star(snapshot_schema):
    plan = build_formula_plan(_check(), FormulaChecksSpec(), snapshot_schema)
    assert "SELECT *" not in plan.sql
    assert plan.sql.startswith("SELECT\n  COUNT(*) AS checked_rows")


def test_check_tolerance_falls_back_to_spec_defaults(snapshot_schema):
    check = _check(tolerance_absolute=None, tolerance_relative=None)
    plan = build_formula_plan(check, FormulaChecksSpec(), snapshot_schema)
    assert plan.tolerance_absolute == 0.01
    assert plan.tolerance_relative == 0.0001


# --- classification ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "checked,mismatched,expected",
    [
        (100, 0, MATCHES),
        (100, None, MATCHES),
        (100, 100, SOURCE_STALE),
        (100, 150, SOURCE_STALE),  # defensive: never more mismatches than rows
        (100, 1, GENUINE_CONFLICT),
        (100, 99, GENUINE_CONFLICT),
        (0, 0, MISSING_EVIDENCE),
        (None, None, MISSING_EVIDENCE),
    ],
)
def test_classification_rules(checked, mismatched, expected):
    assert classify(checked, mismatched) == expected


def test_missing_table_is_missing_evidence():
    result = run_formula_check(
        _check(), FormulaChecksSpec(), schema=None, client=None, max_bytes_billed=1
    )
    assert result.classification == MISSING_EVIDENCE
    assert result.sql is None  # no query was built, let alone run
    assert "was not found" in result.notes[0]


def test_missing_columns_are_missing_evidence(snapshot_schema):
    check = _check(expected_expression="quantity * ghost_price")
    result = run_formula_check(
        check, FormulaChecksSpec(), snapshot_schema, client=None, max_bytes_billed=1
    )
    assert result.classification == MISSING_EVIDENCE
    assert "`ghost_price`" in result.notes[0]
    assert result.sql is None


def test_mocked_query_result_flows_into_result(snapshot_schema):
    client = _formula_client(checked=187, mismatched=3)
    result = run_formula_check(
        _check(), FormulaChecksSpec(), snapshot_schema, client, max_bytes_billed=10**9
    )
    assert result.checked_rows == 187
    assert result.mismatch_rows == 3
    assert result.classification == GENUINE_CONFLICT
    assert result.ok is False


# --- verify-bq integration ------------------------------------------------------------


def test_formula_check_mode_passes_and_updates_pages(formula_cfg, wiki_root, schema_client, monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    run_generate(formula_cfg, wiki_root, now=FIXED_NOW)

    result = _verify(
        formula_cfg,
        wiki_root,
        schema_client=schema_client,
        profile_client=_formula_client(187, 0),
    )
    assert result.ok is True
    (fr,) = result.formula_checks
    assert fr.classification == MATCHES

    # Run JSON holds the detailed counts and SQL.
    detail = json.loads((wiki_root / result.run_file).read_text())
    (fc,) = detail["formula_checks"]
    assert fc["checked_rows"] == 187
    assert fc["mismatch_rows"] == 0
    assert "COUNTIF" in fc["sql"]

    # The output page gets the conclusion, not the counts.
    page = (wiki_root / "okf" / "outputs" / "example-daily-snapshot.md").read_text()
    assert "deterministic formula checks" in page
    assert f"Formula check `{CHECK_NAME}` passed" in page
    assert "187" not in page.split("## Verification Status")[1].split("##")[0]

    # No conflicts -> framework divergences section says none recorded.
    framework = (wiki_root / "okf" / "frameworks" / "example-chain.md").read_text()
    assert "None recorded by the latest formula verification run" in framework


def test_genuine_conflict_writes_framework_divergence(formula_cfg, wiki_root, schema_client, monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    run_generate(formula_cfg, wiki_root, now=FIXED_NOW)

    result = _verify(
        formula_cfg,
        wiki_root,
        schema_client=schema_client,
        profile_client=_formula_client(187, 12),
    )
    assert result.ok is False
    assert result.formula_checks[0].classification == GENUINE_CONFLICT

    framework_rel = "okf/frameworks/example-chain.md"
    assert framework_rel in result.pages_updated
    section = (
        (wiki_root / framework_rel)
        .read_text()
        .split("## Known Doc-vs-Code Divergences")[1]
        .split("##")[0]
    )
    assert "Genuine conflict" in section
    assert CHECK_NAME in section
    assert "owner review required" in section
    # Counts stay in run metadata.
    assert "12" not in section and "187" not in section


def test_source_stale_classification_end_to_end(formula_cfg, wiki_root, schema_client):
    result = _verify(
        formula_cfg,
        wiki_root,
        schema_client=schema_client,
        profile_client=_formula_client(187, 187),
    )
    (fr,) = result.formula_checks
    assert fr.classification == SOURCE_STALE
    assert "documented formula appears outdated" in fr.conclusion()
    assert result.ok is False


def test_check_on_unconfigured_table_fetches_schema(formula_cfg, wiki_root, schema_client):
    view_check = FormulaCheck(
        name="view_check",
        table="example-project.analytics.example_daily_view",
        expression="total_value",
        expected_expression="total_value",
    )
    formula_cfg.bigquery_verification.formula_checks.checks = [view_check]
    result = _verify(
        formula_cfg,
        wiki_root,
        schema_client=schema_client,
        profile_client=FixtureProfileClient(
            {}, formulas={"view_check": {"checked_rows": 10, "mismatch_rows": 0}}
        ),
    )
    (fr,) = result.formula_checks
    assert fr.classification == MATCHES


def test_formula_mode_without_checks_fails_clearly(formula_cfg, wiki_root, schema_client):
    formula_cfg.bigquery_verification.formula_checks.checks = []
    with pytest.raises(VerificationError, match="no checks are configured"):
        _verify(formula_cfg, wiki_root, schema_client=schema_client)


def test_formula_mode_with_formulas_disabled_is_schema_only(
    formula_cfg, wiki_root, schema_client
):
    formula_cfg.bigquery_verification.formula_checks.enabled = False
    result = _verify(formula_cfg, wiki_root, schema_client=schema_client)
    assert result.formula_checks == []
    assert any("formula_checks.enabled is false" in n for n in result.notes)
    assert result.ok is True


def test_cli_formula_check_mode(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lineage_wiki.cli import app
    from .conftest import EXAMPLE_CONFIG

    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    monkeypatch.setenv("LINEAGE_WIKI_NOW", FIXED_NOW)
    root = tmp_path / "wiki"
    root.mkdir()

    # Example config ships mode: profile — flip to formula_check for the run.
    cfg_file = tmp_path / "chain.yml"
    cfg_file.write_text(
        EXAMPLE_CONFIG.read_text().replace("mode: profile", "mode: formula_check")
    )
    runner = CliRunner()
    assert runner.invoke(
        app, ["generate", "--config", str(cfg_file), "--root", str(root)]
    ).exit_code == 0

    result = runner.invoke(
        app, ["verify-bq", "--config", str(cfg_file), "--root", str(root)]
    )
    assert result.exit_code == 0, result.output
    assert "mode      formula_check" in result.output
    assert f"formula {CHECK_NAME} — Matches" in result.output
    assert "verify-bq OK." in result.output
