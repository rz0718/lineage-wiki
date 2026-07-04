# Lineage Wiki

**OpenWiki for data products**: a Python CLI that turns BigQuery schemas,
source code, raw methodology, and dashboard/report mappings into a
git-backed Open Knowledge Format (OKF) knowledge graph.

Milestones 1–4 ship the deterministic core — no LLM, no network. Local
evidence (raw methodology docs and local repo clones) is ingested into
normalized `EvidenceItem`s; configured symbols are located in the loaded
code; unavailable optional sources become Known Gaps and missing
`required: true` sources fail clearly.

Milestone 5 adds **schema-only BigQuery ingestion**: table metadata and
schemas (columns, types, descriptions, partitioning, clustering, table vs
view, view SQL, last-modified) are loaded for the tables configured under
`sources.bigquery.tables`, normalized into evidence, fingerprinted for
update diffing, and rendered into the output pages. The connector never
runs queries — no SELECT statements, no row reads, no live data values.

Commands:

- `lineage-wiki init` — scaffold config examples, prompt stubs, and the `okf/` structure
- `lineage-wiki generate --config chains/<chain>.yml` — deterministic OKF scaffold for one chain (pages, all eight indexes, `.lineage-wiki/manifest.yml`, run metadata under `.lineage-wiki/runs/`)
- `lineage-wiki update --config chains/<chain>.yml` — diffs source fingerprints (raw doc hashes, local repo git HEAD + path hashes, BigQuery schema hashes, report/config identity) against the manifest, prints an impact plan, and rewrites only affected tool-owned pages. With no source changes it is a strict no-op: no file writes, no manifest churn, no run metadata. BigQuery schema fingerprints ignore last-modified metadata, so plain data loads never trigger rewrites — only real schema changes do.
- `lineage-wiki validate` — frontmatter, page types, required sections, relative links, frontmatter refs, placeholder checks, and directory-index/metrics-registry membership

## Install

```bash
uv sync --extra dev            # or: pip install -e '.[dev]'
```

## Quick start

```bash
uv run lineage-wiki init --root /path/to/wiki-repo
uv run lineage-wiki generate --config chains/example.yml --root /path/to/wiki-repo
uv run lineage-wiki validate --root /path/to/wiki-repo
```

Validate an existing OKF catalog (e.g. the reference repo):

```bash
uv run lineage-wiki validate --root ../llm-wiki-dataproducts
```

## BigQuery schema ingestion

Schema only, always: the connector reads table metadata (`get_table`) and
never runs queries. `include_sample_rows` is ignored in this milestone.

**With mocked schemas** (no credentials needed) — point
`LINEAGE_WIKI_BQ_FIXTURES` at a YAML/JSON file mapping fully qualified
table names to schemas (see `tests/fixtures/bigquery_schemas.yml`):

```bash
LINEAGE_WIKI_BQ_FIXTURES=tests/fixtures/bigquery_schemas.yml \
  uv run lineage-wiki generate --config chains/example.yml --root /path/to/wiki-repo
```

The output pages then carry the loaded schema: table type, partitioning,
clustering, last-modified, a column table (name / type / mode / description),
and view SQL for views.

**Against real BigQuery** — install the optional extra and provide
application-default credentials:

```bash
uv sync --extra bigquery          # or: pip install -e '.[bigquery]'
gcloud auth application-default login
uv run lineage-wiki generate --config chains/<chain>.yml --root /path/to/wiki-repo
```

Tables are configured under `sources.bigquery.tables` as
`project.dataset.table` (or `dataset.table` plus `sources.bigquery.project`).

**When BigQuery is unavailable** (no extra installed, no credentials, or
`LINEAGE_WIKI_BQ_OFFLINE=1`): `required: false` records the missing schemas
as Known Gaps and the run succeeds; `required: true` fails the run with a
clear error. A missing individual table follows the same rule.

## Tests

```bash
uv run pytest
```

Snapshot tests compare generated output against `tests/snapshots/`. To
regenerate after an intentional template change:

```bash
LW_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_snapshots.py
```

## Layout

```text
lineage_wiki/
  cli.py            Typer CLI (init, generate, validate)
  config.py         Pydantic chain-config models
  constants.py      OKF taxonomy, required sections, validation rules
  okf/
    schemas.py      OkfPage model, frontmatter render/parse
    templates.py    deterministic Markdown templates for all page types
    indexes.py      generator for okf/index.md + the 7 directory indexes
    validator.py    baseline validator (extends the catalog's validate_okf.py)
  storage/
    manifest.py     .lineage-wiki/manifest.yml reader/writer
  agent/
    runner.py       deterministic init/generate/update runs
    planner.py      update impact planning
  connectors/
    raw_doc_connector.py     local raw Markdown/text docs
    local_repo_connector.py  configured paths from local clones
    bigquery_connector.py    schema-only BigQuery metadata (never queries rows)
  ingestion/
    evidence.py     EvidenceItem model
    source_loader.py connectors -> normalized EvidenceBundle
    code_indexer.py deterministic symbol location (def/class/assignment)
    fingerprints.py source fingerprints for the manifest
chains/example.yml  example chain config
tests/              unit + snapshot tests
```

See `spec.md` for the full product spec and milestone plan.
