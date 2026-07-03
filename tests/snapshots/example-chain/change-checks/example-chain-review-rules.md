---
type: Change Check
title: Example Chain Review Rules
description: Review rules for Example Chain code, output, report, and reference document changes.
owner: Data Team
status: draft
tags:
  - example
  - chain
  - review
  - change-check
timestamp: 2026-07-03T00:00:00Z
framework_refs:
  - ../frameworks/example-chain.md
code_refs:
  - ../code-links/example-pipeline-engine.md
---

# Example Chain Review Rules

## How to Trigger a Review

1. Read the diff in the changed source (code repo, BigQuery table, report,
   or reference document).
2. Resolve changed code paths and symbols through the chain's code link
   pages.
3. Land on the affected framework, component, or output page and compare
   the new behavior with the documented methodology.
4. Classify the outcome using **Required Agent Behavior** below.

Code links for this chain:

- [Example Pipeline Engine](../code-links/example-pipeline-engine.md)

## Code Change Triggers

If `pipeline/main.py`, `pipeline/utils.py`, `compute_daily_snapshot` changes in `example-pipeline` (branch `main`), resolve the change through [Example Pipeline Engine](../code-links/example-pipeline-engine.md) and review the framework methodology.

## Output Change Triggers

If the schema or the producing job of any of these tables changes, review the affected output page:

| Table | Output page |
|---|---|
| `example-project.analytics.example_daily_snapshot` | [Example Daily Snapshot](../outputs/example-daily-snapshot.md) |

## Report Change Triggers

- [Example Daily Report](../report-templates/example-daily-report.md) — review if line items, layout, or the underlying query change.

## Reference Document Change Triggers

- `raw_files/example/methodology.md` — review the framework page if this document changes.

## Required Agent Behavior

When a trigger fires, classify the change as exactly one of:

1. **Code matches OKF.** No documentation change is needed; record that the
   check happened.
2. **Code intentionally changes methodology.** Update the affected
   framework, component, and output pages, and record the change here.
3. **Code conflicts with approved OKF.** Do not silently rewrite the OKF —
   flag the conflict to the owner and keep it open on this page.
4. **OKF is incomplete.** Add the missing link in the traceability chain and
   register any new page in the relevant indexes.

The agent must never invent formulas, columns, code paths, report behavior,
or business definitions; missing evidence becomes a Known Gap on the
framework page.

## Impacted Pages

- [Example Chain Framework](../frameworks/example-chain.md)
- [Example Pipeline Engine](../code-links/example-pipeline-engine.md)
- [Example Daily Snapshot](../outputs/example-daily-snapshot.md)
- [Example Daily Report](../report-templates/example-daily-report.md)
- [Example Metric](../metrics/example-metric.md)
