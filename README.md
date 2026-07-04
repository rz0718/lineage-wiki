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
- `lineage-wiki generate --config chains/<chain>.yml` — deterministic OKF scaffold for one chain (pages, all eight indexes, `.lineage-wiki/manifest.yml`, run metadata under `.lineage-wiki/runs/`). `--target-repo <path>` is an alias for `--root`; `--dry-run` previews the full run (writes, protections, evidence, gaps, verification plan, post-run validation) without writing a single file
- `lineage-wiki update --config chains/<chain>.yml` — diffs source fingerprints (raw doc hashes, local repo git HEAD + path hashes, BigQuery schema hashes, report/config identity) against the manifest, prints an impact plan, and rewrites only affected tool-owned pages. With no source changes it is a strict no-op: no file writes, no manifest churn, no run metadata. BigQuery schema fingerprints ignore last-modified metadata, so plain data loads never trigger rewrites — only real schema changes do.
- `lineage-wiki verify-bq --config chains/<chain>.yml` — optional, credentialed BigQuery verification (`bigquery_verification` config block). `schema_only` mode checks that configured tables and expected columns exist and fingerprints/capture partitioning, clustering, and view SQL; `profile` mode adds safe aggregate queries (row count, date coverage, null counts, distinct counts, min/max). Detailed results — including SQL and profiled values — go to `.lineage-wiki/runs/<run-id>.json`; OKF output pages receive only summary conclusions under `## Verification Status`.
- `lineage-wiki validate` — frontmatter, page types, required sections, relative links, frontmatter refs, placeholder checks, and directory-index/metrics-registry membership. Offline only — never calls BigQuery or an LLM.

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

## Dry-run workflow against an existing OKF repo

`chains/gold-pnl.yml` is a real chain config for the Gold PnL vertical of
the reference catalog. Preview everything without touching the repo:

```bash
uv run lineage-wiki generate --config chains/gold-pnl.yml \
  --target-repo ../llm-wiki-dataproducts --dry-run

# Same, with mocked BigQuery schema evidence (no credentials needed):
LINEAGE_WIKI_BQ_FIXTURES=tests/fixtures/gold_pnl_schemas.yml \
  uv run lineage-wiki generate --config chains/gold-pnl.yml \
  --target-repo ../llm-wiki-dataproducts --dry-run
```

The dry run prints the pages that would be created or updated (with diff
summaries), the pages and indexes that are protected, the evidence found
per source, the Known Gaps, what `verify-bq` would check, and the
validation status *as if the run had been applied* — computed against a
temporary shadow copy, so indexes and validation reflect the pending pages.
Nothing is written: no pages, no indexes, no manifest, no run metadata.
The real OKF repo is only modified when `--dry-run` is omitted explicitly.

### Overwrite protection

Runs (dry or real) never destroy human work:

- An existing page not recorded in `.lineage-wiki/manifest.yml` as
  tool-generated is **never overwritten**, under every `overwrite_policy` —
  `update_existing` only refreshes tool-owned pages, and
  `fail_if_exists` aborts on any collision.
- Index files carry a `<!-- generated-by: lineage-wiki -->` marker; an
  existing index without the marker (and not in the manifest) is
  hand-written and stays protected. A tool-created page missing from a
  hand-written index is reported as a warning to fix manually, not an
  error.
- With `generation.preserve_manual_sections: true` (default), rewriting a
  tool-owned page keeps the existing `## Verification Status` and
  `## Known Doc-vs-Code Divergences` bodies (so `verify-bq` results survive
  regeneration) and retains any extra `## ` section a human added.

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

## BigQuery verification (`verify-bq`)

Controlled by the `bigquery_verification` block in the chain config (see
`chains/example.yml`). Phases 1–2 implement `schema_only`, `profile`, and
`formula_check`; `full_verification` fails with a clear "not implemented".

```bash
# Mocked run (schemas + profile results from the fixture file's
# `tables:` and `profiles:` sections — no credentials needed):
LINEAGE_WIKI_BQ_FIXTURES=tests/fixtures/bigquery_schemas.yml \
  uv run lineage-wiki verify-bq --config chains/example.yml --root /path/to/wiki-repo

# Real run (after `uv sync --extra bigquery` and
# `gcloud auth application-default login`):
uv run lineage-wiki verify-bq --config chains/<chain>.yml --root /path/to/wiki-repo
```

Safety rules, enforced by construction:

- Profiling queries come from deterministic templates over schema-derived
  column names — never `SELECT *`, never natural-language SQL.
- Aggregates only (`COUNT`, `COUNTIF(... IS NULL)`, `APPROX_COUNT_DISTINCT`,
  `MIN`, `MAX`); no row-level data leaves BigQuery.
- Every real query sets `maximum_bytes_billed` from `max_bytes_billed`.
- When a date column is configured, is the partition field, or is detected
  from the schema, scans are limited to `profiling.date_window_days`.
- Profiled values (row counts, date bounds, min/max, …) are written to
  `.lineage-wiki/runs/` only; OKF pages get conclusions like "Rows are
  present in the profiled window", never live values.
- Sample rows are never read in phase 1, even if `sample_rows.enabled` is
  set.

Per-table expectations live under `bigquery_verification.tables`:
`expect_columns` (existence-checked in both modes), `date_column`,
`dimension_columns`, `numeric_columns`, `null_columns` (profiling column
selection; anything unset is detected from the loaded schema).

### Formula checks (`mode: formula_check`)

Explicitly configured, deterministic formula checks — never LLM-generated,
never natural-language-to-SQL. Expressions are validated at config load to
be arithmetic over column names (SQL keywords, `;`, and comment tokens are
rejected). Each check renders one fixed query:

```sql
SELECT
  COUNT(*) AS checked_rows,
  COUNTIF(ABS((total_pnl) - (realized_pnl + unrealized_mtm + hedge_pnl)) > 0.01) AS mismatch_rows
FROM `<table>`
WHERE `snapshot_date` >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
```

Tolerance follows the numpy `isclose` convention (`atol + rtol * ABS(expected)`);
per-check `tolerance_absolute`/`tolerance_relative` override the block-level
`tolerance` defaults. Results classify deterministically:

- **Matches** — rows were checked, none mismatched.
- **Source stale** — every checked row mismatches: the implementation is
  consistent but disagrees with the documented formula (docs look outdated).
- **Genuine conflict** — partial mismatches; owner review required.
- **Missing evidence** — table or referenced columns missing, or no rows in
  the window (no query is run when schema evidence is missing).

Checked/mismatch counts go to `.lineage-wiki/runs/` only. Output pages get
the conclusion line under `## Verification Status`; Source stale / Genuine
conflict results are also written to the framework page's
`## Known Doc-vs-Code Divergences` section. Any non-Matches classification
makes `verify-bq` exit 1. Mocked results come from the fixture file's
`formula_checks:` section (keyed by check name).

Unit tests use mocked responses only. A real-BigQuery smoke test is
integration-only: `LINEAGE_WIKI_BQ_INTEGRATION=1 uv run pytest -m bq_integration`
(target table via `LINEAGE_WIKI_BQ_INTEGRATION_TABLE`).

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
    sections.py     manual-section preservation + page diff summaries
    validator.py    baseline validator (extends the catalog's validate_okf.py)
    verifier.py     verify-bq runner (schema checks + profiling conclusions)
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
    bq_profiler.py  safe aggregate profiling (query templates + clients)
    bq_formula_verifier.py deterministic formula checks + classification
chains/example.yml  example chain config
chains/gold-pnl.yml real Gold PnL chain config for ../llm-wiki-dataproducts
tests/              unit + snapshot tests
```

See `spec.md` for the full product spec and milestone plan.
