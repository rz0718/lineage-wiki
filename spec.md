# Project Spec: Lineage Wiki (`lineage-wiki`)

Recommended names:

```text
Product name: Lineage Wiki
Repo name: lineage-wiki
CLI name: lineage-wiki
Python package: lineage_wiki
```

## 1. Product Goal

Build **Lineage Wiki**, a Python CLI agent for generating and maintaining
Open Knowledge Format (OKF) data-product documentation.

Lineage Wiki is **OpenWiki for data products**, not a fork of OpenWiki's  
TypeScript implementation. [https://www.langchain.com/blog/introducing-openwiki-an-open-source-agent-for-repo-documentation](https://www.langchain.com/blog/introducing-openwiki-an-open-source-agent-for-repo-documentation) It should borrow OpenWiki's product mechanics:

- repo-local wiki output
- init/update run modes
- model/provider configuration
- strong prompting discipline
- git-aware update context
- no-op update detection
- agent instruction insertion into `AGENTS.md` / `CLAUDE.md`
- scheduled workflow that can open a documentation PR

The implementation should remain Python.

The tool should take a chain/data-product input package:

```text
chain or vertical name
BigQuery tables/views
GitHub repo links or local code paths
raw documentation files, optional
dashboard/report links, optional
business terms or metrics, optional
human notes, optional
```

Then it should generate or update a structured OKF knowledge bundle under
`okf/`, following the existing catalog in `llm-wiki-dataproducts`.

The output is Markdown files with YAML frontmatter, internal links, source
citations, gaps, divergences, and validation rules.

The tool is not meant to store live financial numbers. It documents:

```text
source methodology
  -> source code
  -> BigQuery outputs
  -> dashboard or report line
  -> business definition
  -> review/change rules
```

The same chain should work in reverse. A user or agent should be able to start
from a report line item and trace it back to the BigQuery column, component
formula, source code path, and source methodology.

---

## 2. Reference Inputs

### Knowledge Catalog Reference

Use `llm-wiki-dataproducts/` as the canonical OKF output reference.

Important files:

```text
llm-wiki-dataproducts/README.md
llm-wiki-dataproducts/OPERATION.md
llm-wiki-dataproducts/scripts/validate_okf.py
llm-wiki-dataproducts/okf/index.md
llm-wiki-dataproducts/okf/frameworks/gold-pnl.md
llm-wiki-dataproducts/okf/frameworks/gold-dynamic-spread.md
```

Reference output directories:

```text
okf/
  index.md
  frameworks/
  components/
  outputs/
  report-templates/
  code-links/
  change-checks/
  metrics/
raw_files/
```

Reference verticals:

- Gold PnL
- Gold Dynamic Spread

The generated files should look like the existing catalog, including
frontmatter key names, title-cased page types, index maintenance, verification
status sections, and doc-vs-code divergence tables.

### OpenWiki Reference

Use `openwiki/` as a product mechanics reference, not as an implementation
stack.

Borrow these mechanics:

- `init` creates first-pass docs and agent instructions.
- `update` reads previous run metadata and git context before editing.
- No-op updates should not churn metadata.
- Provider/model setup should be explicit and reusable.
- Prompting should be strict about source grounding and repository boundaries.
- Scheduled updates should be possible through GitHub Actions.

Do not copy these implementation choices:

- Node.js / TypeScript
- Ink terminal UI
- DeepAgents dependency as a required runtime
- OpenWiki's generic codebase-documentation page taxonomy

Lineage Wiki should use Python and the OKF page taxonomy.

---

## 3. MVP Scope

Build a CLI called:

```bash
lineage-wiki
```

It should support:

```bash
lineage-wiki init
lineage-wiki generate --config chains/gold-pnl.yml
lineage-wiki update --config chains/gold-pnl.yml
lineage-wiki validate
lineage-wiki inspect --chain gold-pnl
lineage-wiki configure
```

Optional one-shot mode, borrowed from OpenWiki:

```bash
lineage-wiki -p "inspect gold pnl lineage"
lineage-wiki --print --config chains/gold-pnl.yml "generate missing report mappings"
```

MVP should focus on one chain/data-product vertical at a time.

Example:

```bash
lineage-wiki generate --config chains/gold-pnl.yml
```

This should create or update:

```text
okf/
  index.md
  frameworks/<slug>.md
  components/<slug>-*.md
  outputs/<slug>-*.md
  code-links/<slug>-*.md
  report-templates/<slug>-*.md
  change-checks/<slug>-review-rules.md
  metrics/<metric>.md
  */index.md
raw_files/
  <chain>/
    ...
.lineage-wiki/
  manifest.yml
  runs/
    <run-id>.json
```

The generated structure must follow the current OKF catalog shape:
frameworks, components, outputs, report templates, code links, change checks,
and metrics.

---

## 4. Non-Goals

Do not build a web UI.

Do not store live metric values or row-level data.

Do not require a proprietary catalog database.

Do not replace the OKF Markdown repo as the source of truth.

Do not generate undocumented formulas, columns, code paths, dashboard
behavior, or business definitions.

Do not rewrite the entire OKF catalog during update runs. Updates should be
surgical.

Do not require BigQuery credentials for purely local/raw-doc generation.

---

## 5. Input Contract

Each chain should be described by a YAML config file.

### Example Config

```yaml
chain:
  id: gold_pnl
  slug: gold-pnl
  name: Gold PnL
  domain: financial_data_product
  owner: Treasury
  description: End-to-end PnL calculation framework for gold products.

sources:
  raw_docs:
    - path: raw_files/goldpnl/GoldPNLDoc.md
      type: methodology
      required: false

  repos:
    - name: gold-pnl
      host: github
      url: git@github.com:rz0718/gold-pnl.git
      branch: main
      local_path: ../gold-pnl
      paths:
        - main_pnl.py
        - gold_pnl_utils.py
        - futures_hedging_pnl_report.py
      symbols:
        - calculate_framework_pnl_wac
        - load_filtered_transactions_for_pnl
      required: true

  bigquery:
    project: bem---beli-emas-murni
    datasets:
      - treasury_da
      - gold_production
      - pluang_forex
    tables:
      - bem---beli-emas-murni.treasury_da.gold_pnl_daily_snapshot
      - bem---beli-emas-murni.treasury_da.gold_spread_revenue_spread_cost
    include_sample_rows: false
    required: true

  reports:
    - name: Gold Daily PnL Report
      type: slack_or_dashboard
      url: ""
      source_mapping_notes: ""
      required: false

  human_notes:
    - title: Review caveat
      content: "Do not treat USD equivalents as source of truth; IDR is canonical."

generation:
  output_dir: okf
  raw_files_dir: raw_files/goldpnl
  overwrite_policy: update_existing
  create_missing_metrics: true
  update_indexes: true
  require_citations: true
  mark_unknowns_as_gaps: true
  preserve_manual_sections: true

model:
  provider: openai
  model: "<configured-model-id>"
  temperature: 0

validation:
  require_frontmatter: true
  require_links_resolve: true
  require_frontmatter_refs_resolve: true
  require_source_citations: true
  fail_on_uncited_formula: true
  fail_on_placeholders_outside_known_gaps: true
```

### Source Behavior

Sources are normalized into evidence items. Each fact generated into OKF must
be traceable to at least one evidence item.

Implementation evidence priority:

1. Explicit raw methodology
2. Source code
3. BigQuery schema and SQL
4. Dashboard/report mapping
5. Human-supplied notes

When sources conflict, the generated OKF should prefer verified
implementation behavior for current-state documentation and record the
conflict in **Known Doc-vs-Code Divergences** or the relevant change-check
page.

---

## 6. OKF Output Contract

Every generated OKF file must be Markdown with YAML frontmatter.

Lineage Wiki must match the existing catalog's conventions. Page `type`
values are title-cased human labels, not lowercase enum values.

### Common Frontmatter

```yaml
---
type: Framework
title: Gold PnL Framework
description: End-to-end Gold PnL computation methodology, covering scope, formulas, components, implementation, and output lineage.
owner: Treasury
status: draft
tags:
  - gold
  - pnl
  - framework
  - methodology
timestamp: 2026-07-03T00:00:00Z
source_refs:
  - ../../raw_files/goldpnl/GoldPNLDoc.md
component_refs:
  - ../components/gold-wac.md
implementation_refs:
  - repo: gold-pnl
    primary: true
    ref: main
    path: /
    code_link: ../code-links/gold-pnl-engine.md
output_refs:
  - system: bigquery
    primary: true
    table: bem---beli-emas-murni.treasury_da.gold_pnl_daily_snapshot
    output: ../outputs/gold-pnl-daily-snapshot.md
report_refs:
  - ../report-templates/gold-daily-pnl-report.md
change_check: ../change-checks/gold-pnl-review-rules.md
approved_by:
approval_date:
review_cycle: on methodology change
---
```

### Page Types


| Directory                            | `type` value      | Purpose                                                    |
| ------------------------------------ | ----------------- | ---------------------------------------------------------- |
| `okf/index.md` and directory indexes | `Index`           | Navigation and retrieval routing                           |
| `okf/frameworks/`                    | `Framework`       | End-to-end methodology bundle                              |
| `okf/components/`                    | `Component`       | Formula or business-rule building block                    |
| `okf/outputs/`                       | `Output`          | BigQuery table/view/output documentation                   |
| `okf/report-templates/`              | `Report Template` | Dashboard, report, Slack, spreadsheet, or BI mapping       |
| `okf/code-links/`                    | `Code Link`       | Repo/path/symbol/runtime pointer                           |
| `okf/change-checks/`                 | `Change Check`    | Review rules triggered by source/code/table/report changes |
| `okf/metrics/`                       | `Metric`          | Standalone reusable definition or term registry entry      |


### Required Reference Fields

Use these frontmatter reference fields where relevant:

```yaml
source_refs:
framework_refs:
component_refs:
metric_refs:
implementation_refs:
code_refs:
output_refs:
report_refs:
change_check:
```

The validator must resolve relative Markdown references in these fields.

### Required File Types And Sections

#### `Framework`

Purpose: end-to-end methodology page.

Required sections:

```markdown
# <Framework Name>

## Scope
## Core Assumptions
## Core Formula
## Components
## Implementation
## Outputs
## Reports
## Verification Status
## Known Gaps
## Known Doc-vs-Code Divergences
## Source
```

Use the existing Gold PnL and Gold Dynamic Spread framework pages as the
reference style. If a section does not apply, keep the heading and state why.

#### `Component`

Purpose: formula-level or business-rule building block.

Required sections:

```markdown
# <Component Name>

## What It Represents
## Factors Table
## Formula / Logic
## Inputs
## Outputs
## Edge Cases
## Verification Status
## Implementation Backlink
```

Component pages should include a factors table:

```markdown
| Component | What it represents | Driving factors | Code location |
|---|---|---|---|
```

#### `Output`

Purpose: BigQuery table/view documentation.

Required sections:

```markdown
# <Output Name>

## Table
## Grain
## Column Definitions
## Key Formula Mapping
## Upstream Sources
## Downstream Consumers
## Verification Status
## Implementation
```

Every important calculated column should link to a component or framework.

#### `Code Link`

Purpose: repo/path/symbol/runtime pointer.

Required sections:

```markdown
# <Code Link Name>

## Repository
## Implementation Areas
## Input Tables Consumed
## Outputs
## Runtime Assumptions
## Linked OKF Pages
```

Code-link pages are load-bearing. A code diff should be resolvable from
changed paths/symbols to affected framework, component, output, report, and
change-check pages.

#### `Report Template`

Purpose: dashboard/report interpretation guide.

Required sections:

```markdown
# <Report Name>

## Purpose
## Audience
## Metrics Shown
## Source Mapping
## BigQuery Source Mapping
## Interpretation Rules
## Known Caveats
## Verification Status
```

Report pages should let someone start from a visible report number and trace
it back to output column, component formula, code path, and source document.

#### `Change Check`

Purpose: review rules triggered by source/code/table/report changes.

Required sections:

```markdown
# <Framework> Review Rules

## How to Trigger a Review
## Code Change Triggers
## Output Change Triggers
## Report Change Triggers
## Reference Document Change Triggers
## Required Agent Behavior
## Impacted Pages
```

The page should distinguish these outcomes:

1. Code matches OKF.
2. Code intentionally changes methodology.
3. Code conflicts with approved OKF.
4. OKF is incomplete.

#### `Metric`

Purpose: standalone reusable definition.

Required sections:

```markdown
# <Metric Name>

## Definition
## Business Meaning
## Calculation Logic
## Unit
## Grain
## Source References
## Used By
## Caveats
```

If a quantity is only meaningful inside a parent framework, it belongs in
`components/` and should be registered by alias in `okf/metrics/index.md`.

---

## 7. Index Maintenance Contract

After every successful generate/update run, Lineage Wiki must update:

```text
okf/index.md
okf/frameworks/index.md
okf/components/index.md
okf/outputs/index.md
okf/report-templates/index.md
okf/code-links/index.md
okf/change-checks/index.md
okf/metrics/index.md
```

Indexes should be useful entry points for humans and agents. They should not
duplicate full page content.

`okf/index.md` should include:

- repository-level OKF description
- navigation by question type
- traceability chain
- directory table
- vertical summaries
- bot consumption rules

`okf/metrics/index.md` should act as the term registry. It should register:

- standalone metric pages
- component terms that users may ask about by name
- framework names
- glossary aliases from source documents

---

## 8. Agent Behavior Rules

The generation agent must follow these rules:

```text
1. Never invent formulas, table columns, code paths, dashboard behavior, or business definitions.
2. If evidence is missing, create a Known Gap instead of guessing.
3. If raw documentation conflicts with code or BigQuery schema, create a Known Doc-vs-Code Divergence.
4. Prefer implementation evidence in this order:
   a. Explicit raw methodology
   b. Source code
   c. BigQuery schema and SQL
   d. Dashboard/report mapping
   e. Human-supplied notes
5. Every formula must cite or link to at least one source.
6. Every BigQuery output page must include table grain, important columns, and downstream consumers if discoverable.
7. Every framework must link to its components, outputs, code links, report templates, and review rules.
8. Every generated file must pass validation.
9. Every new or renamed page must be added to the relevant indexes.
10. During update runs, edit only pages affected by changed evidence unless the index needs a link refresh.
```

### Cross-Check Requirement

Lineage Wiki must not stop at raw source documents when code and outputs are
available.

For each vertical, it should cross-check:

- source methodology files under `raw_files/`
- source code paths/symbols
- BigQuery schemas
- BigQuery SQL or producer jobs, if available
- sample rows or verification queries, when explicitly enabled
- report/dashboard mappings, if available

Per important fact, the generated OKF should classify the result:


| Classification   | OKF behavior                                                                  |
| ---------------- | ----------------------------------------------------------------------------- |
| Matches          | State the verified behavior and cite/link to the source                       |
| Source stale     | Carry the verified implementation behavior and add a divergence row           |
| Genuine conflict | Keep the issue open in the framework/change-check page with owner/review need |
| Missing evidence | Add a Known Gap and do not infer the answer                                   |


The Gold Dynamic Spread vertical is the reference: source-doc gaps and stale
claims are resolved from code/BigQuery, while the 18% internal cap vs 10%
Trading Rules disclosure remains an open business conflict.

---

## 9. Internal Architecture

Implement in Python as layered modules:

```text
lineage_wiki/
  cli.py
  config.py
  constants.py
  credentials.py
  providers.py
  connectors/
    github_connector.py
    local_repo_connector.py
    bigquery_connector.py
    raw_doc_connector.py
    report_connector.py
  ingestion/
    source_loader.py
    code_indexer.py
    bq_schema_loader.py
    doc_chunker.py
    git_context.py
  agent/
    prompts.py
    planner.py
    extractor.py
    writer.py
    reviewer.py
    runner.py
  okf/
    schemas.py
    templates.py
    graph.py
    indexes.py
    validator.py
  storage/
    repo_writer.py
    manifest.py
    snapshots.py
    runs.py
tests/
```

### `connectors/`

Responsible for fetching source evidence.

MVP connectors:

- local raw Markdown/text connector
- local repo connector
- GitHub metadata/path connector
- BigQuery schema connector
- report/dashboard notes connector

The MVP can treat remote GitHub and dashboard links as references if network
or credentials are unavailable, but it must mark missing evidence as a gap.

### `ingestion/`

Responsible for turning sources into structured evidence.

Evidence model:

```python
class EvidenceItem(BaseModel):
    id: str
    source_type: Literal[
        "raw_doc",
        "github",
        "local_repo",
        "bigquery_schema",
        "bigquery_sql",
        "report",
        "human_note",
        "git_diff",
    ]
    source_uri: str
    title: str | None = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str | None = None
```

### `agent/`

Responsible for planning, extracting, writing, and reviewing.

Suggested pipeline:

```text
1. Load and validate config.
2. Load previous manifest and previous run metadata.
3. Collect git context for configured local repos and the OKF repo.
4. Load raw docs from local files if provided.
5. Load configured local/GitHub code files and symbols.
6. Load BigQuery table schemas for configured tables.
7. Normalize source materials into EvidenceItem objects.
8. Plan required OKF pages.
9. Extract formulas, business definitions, components, source tables,
   output tables, code paths, report mappings, gaps, and divergences.
10. Build traceability graph.
11. Generate or update OKF Markdown files.
12. Review generated files against evidence.
13. Update indexes.
14. Write files and manifest.
15. Run validation.
16. Print summary of created/updated files, gaps, divergences, and validation status.
```

The first milestone should implement deterministic scaffolding before adding
LLM extraction/writing.

### `okf/`

Responsible for OKF schema, templates, graph, index maintenance, and
validation.

OKF page model:

```python
class OkfPage(BaseModel):
    id: str
    slug: str
    type: Literal[
        "Index",
        "Framework",
        "Component",
        "Output",
        "Code Link",
        "Report Template",
        "Change Check",
        "Metric",
    ]
    title: str
    description: str
    owner: str | None = None
    status: Literal["draft", "reviewed", "approved", "deprecated"] = "draft"
    tags: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    body: str
    links: list[str] = Field(default_factory=list)
```

### `storage/`

Responsible for writing Markdown, preserving manual edits, maintaining
manifests, storing run metadata, and computing content snapshots.

Manifest example:

```yaml
version: 1
chain_id: gold_pnl
chain_slug: gold-pnl
output_dir: okf
generated_files:
  - okf/frameworks/gold-pnl.md
  - okf/components/gold-wac.md
  - okf/outputs/gold-pnl-daily-snapshot.md
managed_indexes:
  - okf/index.md
  - okf/frameworks/index.md
source_fingerprints:
  repos:
    gold-pnl:
      ref: main
      git_head: 0d0f748
      paths_hash: sha256:...
  bigquery:
    bem---beli-emas-murni.treasury_da.gold_pnl_daily_snapshot: sha256:...
  raw_docs:
    raw_files/goldpnl/GoldPNLDoc.md: sha256:...
last_run_at: 2026-07-03T12:00:00+08:00
last_content_snapshot: sha256:...
```

Run metadata example:

```json
{
  "updatedAt": "2026-07-03T12:00:00+08:00",
  "command": "update",
  "chainId": "gold_pnl",
  "model": "<configured-model-id>",
  "okfGitHead": "abc123",
  "contentChanged": true,
  "createdFiles": [],
  "updatedFiles": ["okf/outputs/gold-pnl-daily-snapshot.md"],
  "gaps": 1,
  "divergences": 2
}
```

---

## 10. CLI Behavior

### `lineage-wiki init`

Creates:

```text
.lineage-wiki/
  config.example.yml
  prompts/
    system.md
    page_planner.md
    extractor.md
    writer.md
    reviewer.md
chains/
  example.yml
```

If `okf/` does not exist, create the directory structure and index stubs.

Optionally append instructions to top-level:

```text
AGENTS.md
CLAUDE.md
```

Suggested instruction block:

```markdown
## OKF Wiki Context

This repository contains an Open Knowledge Format catalog under `okf/`.

Start here:
- [OKF index](okf/index.md)

When working on data products, formulas, BigQuery tables, dashboards, risk
definitions, PnL methodology, spread methodology, treasury definitions, or
liquidity definitions:

1. Start from `okf/index.md`.
2. Follow links to the relevant framework, component, output, code-link,
   metric, report-template, and change-check pages.
3. Do not change formulas, table mappings, or report behavior without
   updating the relevant OKF pages.
4. If code and OKF conflict, flag the divergence and update the relevant
   change-check page.
5. Do not invent formulas, code paths, output columns, or report behavior
   when the traceability chain is missing.
```

### `lineage-wiki configure`

Interactive or non-interactive setup for provider/model credentials.

Store local configuration outside the target repo, for example:

```text
~/.lineage-wiki/.env
```

Support at least:

- OpenAI-compatible provider
- Anthropic-compatible provider, optional
- model ID
- tracing settings, optional

Do not read or print secret values.

### `lineage-wiki generate --config chains/gold-pnl.yml`

Creates the first OKF vertical from configured sources.

If pages already exist, behavior depends on `overwrite_policy`:

- `fail_if_exists`: stop with a clear error.
- `update_existing`: preserve human-managed content where possible and print
a diff summary.

### `lineage-wiki update --config chains/gold-pnl.yml`

Updates existing OKF pages by comparing:

```text
previous manifest
current OKF git state
current source repo git commits/diffs
current BigQuery schema fingerprints
current raw doc hashes
current report mapping notes
```

Then only rewrites affected pages.

If the OKF content snapshot does not change, do not update run metadata except
for transient logs. This borrows OpenWiki's no-op update behavior.

Update impact rules:


| Changed evidence                  | Pages to consider                                                  |
| --------------------------------- | ------------------------------------------------------------------ |
| Code files/symbols                | code-links, affected components, affected framework, change-checks |
| BigQuery schema                   | outputs, affected framework, report-templates, change-checks       |
| Raw docs                          | framework, components, metrics, known gaps/divergences             |
| Report mapping                    | report-templates, outputs, framework, change-checks                |
| Index-affecting rename/add/remove | relevant directory index and `okf/index.md`                        |


Never overwrite human edits blindly. Preserve manually edited sections where
possible or emit a conflict/diff summary.

### `lineage-wiki validate`

Runs:

```text
frontmatter validation
frontmatter reference validation
required section validation
relative Markdown link validation
source citation validation
OKF graph validation
index membership validation
no unresolved placeholder validation
```

The current catalog already has `scripts/validate_okf.py`, which checks
frontmatter and relative Markdown links. The MVP can vendor, wrap, or extend
that behavior rather than replacing it.

### `lineage-wiki inspect --chain gold-pnl`

Prints a lineage summary:

```text
framework
components
outputs
reports
code-links
change-checks
known gaps
known divergences
validation status
last run metadata
```

This command should not call the LLM. It should read the OKF graph and
manifest.

---

## 11. Prompting And Run Discipline

Borrow these OpenWiki-style prompt rules and adapt them to OKF:

- Work only inside the target repository and configured source paths.
- Do not read secrets, `.env` files, private keys, tokens, or credential
files.
- Treat existing README/runbook/source methodology files as primary evidence,
but prefer current code/output behavior when docs are stale.
- Use git history to understand changes, not to create persistent commit lists
unless a specific ref is the verification baseline.
- During generate runs, create a focused first-pass vertical rather than many
thin pages.
- During update runs, build an impact plan before editing.
- Keep concepts canonical: detailed definition in one page, lightweight links
elsewhere.
- Do not make formatting-only changes during update runs.
- Preserve existing useful wording when it remains accurate.
- Always record gaps instead of inventing missing evidence.

Temporary planning files, if used, must be removed before a successful run
completes.

---

## 12. Validation Requirements

Validation should fail if:

1. A non-index OKF page has no YAML frontmatter.
2. Frontmatter has no non-empty `type`.
3. Frontmatter `type` is not one of the expected title-cased OKF labels.
4. A relative Markdown link does not resolve.
5. A frontmatter reference path does not resolve.
6. A generated page contains `TODO`, `TBD`, `<path-TBD>`, or `???` outside a
  `Known Gaps` or equivalent open-issues section.
7. A formula section exists without a source reference, code link, or explicit
  verification status.
8. A BigQuery output page has no grain or no column list.
9. A framework page does not link to code links and outputs when those sources
  exist.
10. A generated page is missing required sections for its type.
11. A generated non-index page is absent from the relevant directory index.
12. A term introduced as reusable is absent from `okf/metrics/index.md`.

Validation should warn, not fail, if optional evidence is unavailable and the
page records a clear Known Gap.

---

## 13. Tests

MVP tests should cover:

- config loading and validation
- source fingerprinting
- raw doc loading
- local repo file loading
- BigQuery schema parsing from mocked schema output
- OKF template rendering
- required section validation
- frontmatter reference validation
- Markdown link validation
- index update behavior
- manifest diffing
- no-op update behavior
- affected-page selection for code/schema/raw-doc/report changes
- preservation of manual sections or conflict reporting

Snapshot tests are useful for deterministic skeleton generation.

LLM-dependent tests should be behind an integration flag and should not be
required for normal unit test runs.

---

## 14. Acceptance Criteria

The MVP is complete when:

1. `lineage-wiki init` creates config examples, prompt files, OKF directories,
  and optional agent instruction sections.
2. `lineage-wiki generate --config chains/example.yml` creates valid OKF
  Markdown files using the catalog's page taxonomy and frontmatter style.
3. Generated files include YAML frontmatter, required sections, internal
  links, and source references or Known Gaps.
4. Directory indexes and `okf/index.md` are created or updated.
5. `lineage-wiki validate` passes on generated output.
6. `.lineage-wiki/manifest.yml` records generated files and source
  fingerprints.
7. Running `lineage-wiki update` with no source changes produces no OKF file
  changes and does not churn run metadata.
8. Running `lineage-wiki update` after changing a source file updates only
  affected pages and indexes.
9. `lineage-wiki inspect --chain <chain>` prints the OKF graph, gaps,
  divergences, and last run status without calling an LLM.
10. Tests cover config loading, template rendering, validation, index updates,
  manifest diffing, and update impact behavior.

---

## 15. Recommended Build Order

Start with a deterministic MVP. Do **not** build the autonomous LLM agent
first.

Recommended milestones:

```text
Milestone 1: CLI + config + deterministic OKF templates + validator
Milestone 2: index generation + metrics registry updates
Milestone 3: manifest + source fingerprinting + no-op update detection
Milestone 4: local raw docs + local repo ingestion
Milestone 5: BigQuery schema ingestion
Milestone 6: deterministic OKF skeleton generation for one vertical
Milestone 7: LLM extraction/writing behind an interface
Milestone 8: update mode using manifest diffs and git context
Milestone 9: provider/model configuration
Milestone 10: GitHub Action for scheduled update PRs
Milestone 11: AGENTS.md / CLAUDE.md integration hardening
```

The reason is simple: if the schema, file contracts, validator, indexes, and
manifest are stable, the LLM layer becomes replaceable. The tool can support
OpenAI, Anthropic, Gemini, local models, or a deterministic/manual mode later.

---

## 16. Goal Prompt For Coding Agent

Paste this as the main instruction to a coding agent.

```markdown
# Goal

Implement **Lineage Wiki**, exposed as the `lineage-wiki` CLI.

Lineage Wiki is a Python tool that generates and maintains Open Knowledge
Format documentation for data products. It should borrow OpenWiki's product
mechanics (init/update modes, git-aware updates, provider configuration,
agent instruction insertion, no-op metadata behavior, scheduled PR workflow)
but it must use Python and the OKF output structure from
`llm-wiki-dataproducts`.

The tool takes a chain-level YAML config containing BigQuery tables,
GitHub/local code references, optional raw documentation, optional report
links, and optional human notes. It generates a structured OKF Markdown
knowledge bundle under `okf/`.

The output must follow the existing repository model:

- `okf/index.md`
- `okf/frameworks/`
- `okf/components/`
- `okf/outputs/`
- `okf/report-templates/`
- `okf/code-links/`
- `okf/change-checks/`
- `okf/metrics/`
- `raw_files/`

Generated pages must use the existing catalog's title-cased `type` values:
`Index`, `Framework`, `Component`, `Output`, `Report Template`, `Code Link`,
`Change Check`, and `Metric`.

The tool must not invent formulas, columns, code paths, dashboard behavior, or
business definitions. Missing evidence should be represented as `Known Gaps`.
Conflicts between docs/code/BigQuery should be represented as `Known
Doc-vs-Code Divergences` or open issues in the change-check page.

# Non-goals

Do not build a web UI.
Do not store live metric values.
Do not require a proprietary catalog database.
Do not replace the OKF Markdown repo as the source of truth.
Do not generate undocumented claims without citations.

# MVP commands

Implement:

```bash
lineage-wiki init
lineage-wiki configure
lineage-wiki generate --config chains/<chain>.yml
lineage-wiki update --config chains/<chain>.yml
lineage-wiki validate
lineage-wiki inspect --chain <chain>
```

# Required implementation

Use Python.

Create package structure:

```text
lineage_wiki/
  cli.py
  config.py
  constants.py
  credentials.py
  providers.py
  connectors/
  ingestion/
  agent/
  okf/
  storage/
tests/
```

Use Typer or Click for CLI.
Use Pydantic for config/schema validation.
Use Jinja2 or plain templates for Markdown generation.
Use PyYAML for YAML.
Use the behavior from `llm-wiki-dataproducts/scripts/validate_okf.py` as the
baseline validator and extend it where needed.

# Required behavior

1. Generate OKF pages matching the existing `llm-wiki-dataproducts` style.
2. Maintain all relevant `index.md` files.
3. Store source fingerprints and generated-file metadata in
  `.lineage-wiki/manifest.yml`.
4. During updates, compare current evidence to the manifest and edit only
  affected pages.
5. If no OKF content changes, do not churn run metadata.
6. Preserve human edits where possible; otherwise print a conflict/diff
  summary.
7. Validate links, frontmatter references, required sections, unresolved
  placeholders, and index membership.
8. Keep deterministic scaffolding working without an LLM.
9. Add LLM extraction/writing behind an interface after the deterministic
  contracts are stable.

# Build order

Start with:

1. CLI
2. Config parser
3. OKF page schemas
4. Markdown templates
5. Validator
6. Index generator
7. Manifest writer
8. Source fingerprinting
9. Example chain config
10. Snapshot tests

Add source ingestion and LLM extraction only after those contracts pass tests.

```

---

## 17. One-Line Positioning

**OpenWiki for data products: a Python CLI agent that turns BigQuery schemas,
source code, raw methodology, and dashboard/report mappings into a
git-backed OKF knowledge graph.**
```

