"""Milestone 5: schema-only BigQuery ingestion with mocked schema fixtures.

No test here touches real BigQuery — conftest forces offline mode, and
mocked schemas come from tests/fixtures/bigquery_schemas.yml.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from lineage_wiki.agent.runner import run_generate, run_update
from lineage_wiki.config import BigQuerySource, ConfigError, load_config
from lineage_wiki.connectors import SourceUnavailableError
from lineage_wiki.connectors.bigquery_connector import (
    FixtureBigQueryClient,
    TableSchema,
    load_bigquery_schemas,
    parse_table_name,
    resolve_bigquery_client,
)
from lineage_wiki.ingestion.fingerprints import compute_fingerprints
from lineage_wiki.ingestion.source_loader import load_sources
from lineage_wiki.storage.manifest import load_manifest

from .conftest import FIXED_NOW, REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bigquery_schemas.yml"
SNAPSHOT_TABLE = "example-project.analytics.example_daily_snapshot"
VIEW_TABLE = "example-project.analytics.example_daily_view"


@pytest.fixture
def wiki_root(tmp_path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


@pytest.fixture
def fixture_client() -> FixtureBigQueryClient:
    return FixtureBigQueryClient.from_file(FIXTURES)


def _source(**overrides) -> BigQuerySource:
    base = dict(project="example-project", tables=[SNAPSHOT_TABLE], required=False)
    base.update(overrides)
    return BigQuerySource(**base)


# --- table name parsing ---------------------------------------------------------


def test_parse_fully_qualified_table_name():
    name = parse_table_name("proj.ds.table")
    assert (name.project, name.dataset, name.table) == ("proj", "ds", "table")
    assert name.fqtn == "proj.ds.table"


def test_parse_strips_backticks_and_uses_default_project():
    assert parse_table_name("`proj.ds.table`").fqtn == "proj.ds.table"
    assert parse_table_name("ds.table", default_project="proj").fqtn == "proj.ds.table"


@pytest.mark.parametrize("bad", ["table", "a.b.c.d", "a..c", ""])
def test_parse_rejects_malformed_names(bad):
    with pytest.raises(ValueError):
        parse_table_name(bad, default_project="proj")


def test_parse_rejects_two_part_name_without_project():
    with pytest.raises(ValueError, match="no project part"):
        parse_table_name("ds.table")


def test_config_validates_table_names(tmp_path):
    cfg_file = tmp_path / "bad.yml"
    cfg_file.write_text(
        "chain: {id: c, name: C}\n"
        "sources:\n  bigquery:\n    tables: [not-a-table]\n"
    )
    with pytest.raises(ConfigError, match="invalid BigQuery table name"):
        load_config(cfg_file)


def test_config_requires_project_for_two_part_names(tmp_path):
    cfg_file = tmp_path / "bad.yml"
    cfg_file.write_text(
        "chain: {id: c, name: C}\n"
        "sources:\n  bigquery:\n    tables: [ds.table]\n"
    )
    with pytest.raises(ConfigError, match="no project part"):
        load_config(cfg_file)


# --- fixture client and schema capture --------------------------------------------


def test_fixture_client_loads_table_schema(fixture_client):
    schema = fixture_client.get_table_schema(parse_table_name(SNAPSHOT_TABLE))
    assert schema.table_id == SNAPSHOT_TABLE
    assert schema.table_type == "TABLE"
    assert [c.name for c in schema.columns] == [
        "snapshot_date", "asset_id", "quantity", "price", "total_value"
    ]
    assert schema.columns[0].type == "DATE"
    assert schema.columns[0].mode == "REQUIRED"
    assert schema.columns[0].description == "Snapshot date (daily grain)."
    assert schema.partitioning == {"kind": "time", "type": "DAY", "field": "snapshot_date"}
    assert schema.clustering == ["asset_id"]
    assert schema.last_modified == "2026-07-01T00:00:00+00:00"
    assert schema.view_sql is None


def test_fixture_client_loads_view_schema(fixture_client):
    schema = fixture_client.get_table_schema(parse_table_name(VIEW_TABLE))
    assert schema.table_type == "VIEW"
    assert "GROUP BY snapshot_date" in schema.view_sql


def test_fixture_client_returns_none_for_unknown_table(fixture_client):
    assert fixture_client.get_table_schema(parse_table_name("p.d.nope")) is None


def test_schema_clients_expose_no_query_surface(fixture_client):
    # Structural safety guarantee: the client contract is metadata-only.
    assert not hasattr(fixture_client, "query")
    assert not hasattr(fixture_client, "list_rows")


# --- fingerprints ------------------------------------------------------------------


def test_schema_fingerprint_ignores_last_modified(fixture_client):
    schema = fixture_client.get_table_schema(parse_table_name(SNAPSHOT_TABLE))
    moved = schema.model_copy(update={"last_modified": "2026-07-02T00:00:00+00:00"})
    assert schema.fingerprint() == moved.fingerprint()


def test_schema_fingerprint_changes_on_schema_change(fixture_client):
    schema = fixture_client.get_table_schema(parse_table_name(SNAPSHOT_TABLE))
    grown = schema.model_copy(deep=True)
    grown.columns[0].description = "changed"
    assert schema.fingerprint() != grown.fingerprint()


# --- load_bigquery_schemas ----------------------------------------------------------


def test_load_normalizes_schema_into_evidence(fixture_client):
    result = load_bigquery_schemas(_source(), client=fixture_client)
    assert result.available is True
    assert result.missing_tables == []
    (item,) = result.items
    assert item.id == f"bq-schema:{SNAPSHOT_TABLE}"
    assert item.source_type == "bigquery_schema"
    assert item.source_uri == f"bigquery:{SNAPSHOT_TABLE}"
    assert item.title == SNAPSHOT_TABLE
    assert "snapshot_date" in item.content
    assert item.metadata["table_type"] == "TABLE"
    assert item.metadata["n_columns"] == 5
    assert item.fingerprint.startswith("sha256:")
    assert item.fingerprint == result.schemas[SNAPSHOT_TABLE].fingerprint()


def test_load_missing_optional_table_is_reported(fixture_client):
    result = load_bigquery_schemas(
        _source(tables=[SNAPSHOT_TABLE, "example-project.analytics.nope"]),
        client=fixture_client,
    )
    assert result.missing_tables == ["example-project.analytics.nope"]
    assert list(result.schemas) == [SNAPSHOT_TABLE]


def test_load_missing_required_table_fails(fixture_client):
    with pytest.raises(SourceUnavailableError, match="analytics.nope"):
        load_bigquery_schemas(
            _source(tables=["example-project.analytics.nope"], required=True),
            client=fixture_client,
        )


def test_load_unavailable_optional_records_reason():
    # conftest forces offline mode, so client resolution yields nothing.
    result = load_bigquery_schemas(_source())
    assert result.available is False
    assert "offline mode" in result.unavailable_reason


def test_load_unavailable_required_fails():
    with pytest.raises(SourceUnavailableError, match="required BigQuery source"):
        load_bigquery_schemas(_source(required=True))


# --- client resolution ---------------------------------------------------------------


def test_resolve_prefers_fixture_env(monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    resolved = resolve_bigquery_client()
    assert resolved.kind == "fixtures"
    assert resolved.client.get_table_schema(parse_table_name(SNAPSHOT_TABLE))


def test_resolve_fails_clearly_on_missing_fixture_file(monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", "does/not/exist.yml")
    with pytest.raises(SourceUnavailableError, match="fixture file not found"):
        resolve_bigquery_client()


# --- manifest fingerprints -----------------------------------------------------------


def test_compute_fingerprints_uses_schema_hash_with_fixtures(
    example_cfg, wiki_root, fixture_client, monkeypatch
):
    offline = compute_fingerprints(example_cfg, wiki_root)
    # Offline falls back to the config-identity hash, stable across runs.
    assert offline.bigquery == compute_fingerprints(example_cfg, wiki_root).bigquery

    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    online = compute_fingerprints(example_cfg, wiki_root)

    schema = fixture_client.get_table_schema(parse_table_name(SNAPSHOT_TABLE))
    assert online.bigquery[SNAPSHOT_TABLE] == schema.fingerprint()
    assert offline.bigquery[SNAPSHOT_TABLE] != online.bigquery[SNAPSHOT_TABLE]


# --- generate integration ------------------------------------------------------------


def test_generate_with_mocked_schemas(example_cfg, wiki_root, monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    result = run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    assert result.report.errors == []

    output = (wiki_root / "okf" / "outputs" / "example-daily-snapshot.md").read_text()
    assert f"| Table | `{SNAPSHOT_TABLE}` |" in output
    assert "| Type | TABLE |" in output
    assert "| Partitioning | Time-partitioned (DAY) on `snapshot_date` |" in output
    assert "| Clustering | `asset_id` |" in output
    assert "| Last modified | 2026-07-01T00:00:00+00:00 |" in output
    assert "| `snapshot_date` | DATE | REQUIRED | Snapshot date (daily grain). |" in output
    assert "| `total_value` | NUMERIC | NULLABLE | quantity x price. |" in output
    assert "Verified from BigQuery schema metadata" in output
    assert "no rows were queried" in output

    framework = (wiki_root / "okf" / "frameworks" / "example-chain.md").read_text()
    assert "| BigQuery schemas | Ingested — 1 of 1 schema(s) loaded (fixtures); cross-check pending |" in framework
    gaps = "\n".join(result.gaps)
    assert f"Schema for `{SNAPSHOT_TABLE}` is ingested but has not been" in gaps
    assert f"BigQuery schema for `{SNAPSHOT_TABLE}` has not been ingested" not in gaps


def test_generate_renders_view_sql(example_cfg, wiki_root, monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    cfg = example_cfg.model_copy(deep=True)
    cfg.sources.bigquery.tables.append(VIEW_TABLE)
    result = run_generate(cfg, wiki_root, now=FIXED_NOW)
    assert result.report.errors == []

    view = (wiki_root / "okf" / "outputs" / "example-daily-view.md").read_text()
    assert "| Type | VIEW |" in view
    assert "```sql" in view
    assert "GROUP BY snapshot_date" in view
    assert "defines no partitioning for this view" in view


def test_generate_without_bigquery_records_known_gap(example_cfg, wiki_root):
    result = run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    assert result.report.errors == []

    gaps = "\n".join(result.gaps)
    assert "BigQuery is unavailable" in gaps
    assert f"BigQuery schema for `{SNAPSHOT_TABLE}` has not been ingested" in gaps

    framework = (wiki_root / "okf" / "frameworks" / "example-chain.md").read_text()
    assert "| BigQuery schemas | Not loaded — BigQuery unavailable |" in framework
    output = (wiki_root / "okf" / "outputs" / "example-daily-snapshot.md").read_text()
    assert "no schema evidence has been ingested" in output


def test_generate_fails_clearly_when_bigquery_required(example_cfg, wiki_root):
    cfg = example_cfg.model_copy(deep=True)
    cfg.sources.bigquery.required = True
    with pytest.raises(SourceUnavailableError, match="required BigQuery source"):
        run_generate(cfg, wiki_root, now=FIXED_NOW)


def test_missing_optional_table_is_a_known_gap(example_cfg, wiki_root, monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    cfg = example_cfg.model_copy(deep=True)
    cfg.sources.bigquery.tables.append("example-project.analytics.ghost")
    result = run_generate(cfg, wiki_root, now=FIXED_NOW)
    gaps = "\n".join(result.gaps)
    assert "BigQuery table `example-project.analytics.ghost` was not found" in gaps


def test_source_loader_includes_bigquery_items(example_cfg, wiki_root, monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    bundle = load_sources(example_cfg, wiki_root)
    assert bundle.bigquery.available is True
    types = {i.source_type for i in bundle.all_items()}
    assert "bigquery_schema" in types


# --- update integration ---------------------------------------------------------------


def test_update_reacts_to_schema_change_and_noops_otherwise(
    example_cfg, wiki_root, tmp_path, monkeypatch
):
    fixtures = tmp_path / "schemas.yml"
    shutil.copy(FIXTURES, fixtures)
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(fixtures))

    run_generate(example_cfg, wiki_root, now=FIXED_NOW)

    # Unchanged schemas -> strict no-op.
    result = run_update(example_cfg, wiki_root, now=FIXED_NOW)
    assert result.noop is True

    # Only last_modified moved -> still a no-op (data load, not schema change).
    data = yaml.safe_load(fixtures.read_text())
    data["tables"][SNAPSHOT_TABLE]["last_modified"] = "2026-07-03T00:00:00+00:00"
    fixtures.write_text(yaml.safe_dump(data))
    result = run_update(example_cfg, wiki_root, now=FIXED_NOW)
    assert result.noop is True

    # A real schema change -> the output page (and framework) get rewritten.
    data["tables"][SNAPSHOT_TABLE]["columns"].append(
        {"name": "hedge_value", "type": "NUMERIC", "description": "Hedged value."}
    )
    fixtures.write_text(yaml.safe_dump(data))
    result = run_update(example_cfg, wiki_root, now=FIXED_NOW)
    assert result.noop is False
    assert SNAPSHOT_TABLE in result.changes.bigquery
    assert "okf/outputs/example-daily-snapshot.md" in result.updated
    output = (wiki_root / "okf" / "outputs" / "example-daily-snapshot.md").read_text()
    assert "| `hedge_value` | NUMERIC | NULLABLE | Hedged value. |" in output


def test_update_preserves_schema_docs_when_optional_bigquery_is_unavailable(
    example_cfg, wiki_root, monkeypatch
):
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(FIXTURES))
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)

    output = wiki_root / "okf" / "outputs" / "example-daily-snapshot.md"
    before_output = output.read_text(encoding="utf-8")
    before_manifest = load_manifest(wiki_root)
    assert before_manifest is not None
    before_entry = before_manifest.chains[example_cfg.chain.id]
    before_fingerprints = dict(before_entry.source_fingerprints.bigquery)
    assert before_fingerprints[SNAPSHOT_TABLE].startswith("sha256:")

    monkeypatch.delenv("LINEAGE_WIKI_BQ_FIXTURES")
    result = run_update(example_cfg, wiki_root, now=FIXED_NOW)

    assert result.noop is True
    assert result.changes.bigquery == []
    assert result.warnings
    assert "BigQuery unavailable" in result.warnings[0]
    assert output.read_text(encoding="utf-8") == before_output
    after_manifest = load_manifest(wiki_root)
    assert after_manifest is not None
    after_entry = after_manifest.chains[example_cfg.chain.id]
    assert after_entry.source_fingerprints.bigquery == before_fingerprints
