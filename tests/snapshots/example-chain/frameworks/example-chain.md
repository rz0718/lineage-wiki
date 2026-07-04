---
type: Framework
title: Example Chain Framework
description: Example end-to-end data product chain used to exercise the lineage-wiki scaffold.
owner: Data Team
status: draft
tags:
  - example
  - chain
  - framework
  - methodology
timestamp: 2026-07-03T00:00:00Z
implementation_refs:
  - repo: example-pipeline
    primary: true
    ref: main
    path: /
    code_link: ../code-links/example-pipeline-engine.md
output_refs:
  - system: bigquery
    primary: true
    table: example-project.analytics.example_daily_snapshot
    output: ../outputs/example-daily-snapshot.md
report_refs:
  - ../report-templates/example-daily-report.md
metric_refs:
  - ../metrics/example-metric.md
change_check: ../change-checks/example-chain-review-rules.md
approved_by:
approval_date:
review_cycle: on methodology change
---

# Example Chain Framework

## Scope

Example end-to-end data product chain used to exercise the lineage-wiki scaffold.

Domain: `financial_data_product`.

This page is a deterministic scaffold created by `lineage-wiki` from
`chains/example-chain.yml`-style config. Scope details beyond the configured
description have not been ingested — see [Known Gaps](#known-gaps).

## Core Assumptions

No verified assumptions have been documented yet — see
[Known Gaps](#known-gaps).

Human-supplied notes (unverified evidence, lowest priority in the evidence order):

- **Review caveat:** Scaffold example only; replace with a real chain.

## Core Formula

No formula has been documented for this chain yet. `lineage-wiki` does not
invent formulas; the core formula will be added once methodology or code
evidence is ingested — see [Known Gaps](#known-gaps).

## Components

No component pages have been generated for this framework yet. Components
are added once formula-level evidence is ingested — see
[Known Gaps](#known-gaps).

## Implementation

| Repo | Branch | Host | Code Link |
|---|---|---|---|
| `example-pipeline` | `main` | github | [Example Pipeline Engine](../code-links/example-pipeline-engine.md) |

## Outputs

| Table | Output Page |
|---|---|
| `example-project.analytics.example_daily_snapshot` | [Example Daily Snapshot](../outputs/example-daily-snapshot.md) |

## Reports

- [Example Daily Report](../report-templates/example-daily-report.md)

## Verification Status

| Evidence | Status |
|---|---|
| Raw methodology | Not ingested (scaffold) |
| Source code | Not loaded — no local clone available |
| BigQuery schemas | Not loaded — BigQuery unavailable |
| Report mappings | Not ingested (scaffold) |

Unverified scaffold framework page generated deterministically by `lineage-wiki` from the chain config and locally loaded evidence. No fact on this page has been cross-checked against that evidence yet.

## Known Gaps

- Source methodology has not been ingested; framework scope details, assumptions, and formulas are undocumented.
- No component pages exist yet; the component inventory is unknown until formula-level evidence is ingested.
- Configured raw doc `raw_files/example/methodology.md` was not found at generate time.
- Repo `example-pipeline` has no local clone available; configured paths and symbols are unverified references (remote GitHub ingestion lands in a later milestone).
- BigQuery is unavailable (offline mode via LINEAGE_WIKI_BQ_OFFLINE); configured table schemas cannot be ingested.
- BigQuery schema for `example-project.analytics.example_daily_snapshot` has not been ingested; grain and column definitions are undocumented.
- Report `Example Daily Report` has no verified source mapping.

## Known Doc-vs-Code Divergences

None recorded yet. Divergences are only recorded once documentation, code,
and output evidence have been ingested and cross-checked.

## Source

- Configured raw doc `raw_files/example/methodology.md` was not found at generate time — see [Known Gaps](#known-gaps).

Review rules: [Example Chain Review Rules](../change-checks/example-chain-review-rules.md)
