---
type: Index
title: OKF Knowledge Bundle
description: Entry point for the git-backed data-product knowledge base. Organized for traceability from report line items back to code and source documents.
status: draft
tags:
  - okf
  - okf
  - index
timestamp: 2026-07-03T00:00:00Z
---

<!-- generated-by: lineage-wiki -->

# OKF Knowledge Bundle

This bundle is the source of truth for data-product methodology used by
agents and humans. Git is authoritative.

## How to Navigate

Start here for a term or number you want to trace:

1. **I have a term name** → [Metrics Registry](metrics/index.md)
2. **I have a report line** → [Report Templates](report-templates/index.md) → output column → component → code
3. **I have an output column** → [Outputs](outputs/index.md) → component formula → code
4. **I have a framework or methodology question** → [Frameworks](frameworks/index.md)
5. **Code changed and I need to check methodology** → [Change Checks](change-checks/index.md)

## Question-Type Routing

| Question type | Traversal |
|---|---|
| "What is the breakdown of X?" | [Metrics Registry](metrics/index.md) → component → optionally report template |
| "What are the inputs to X?" | component → [Code Links](code-links/index.md) |
| "What happens if table Y changes?" | code-links input table → component → [Outputs](outputs/index.md) → [Report Templates](report-templates/index.md) |
| "How do I read this report line?" | [Report Templates](report-templates/index.md) → [Outputs](outputs/index.md) → component |

## Traceability Chain

```text
report line item
  → output column          okf/outputs/
  → component formula      okf/components/
  → framework methodology  okf/frameworks/
  → code implementation    okf/code-links/
  → source document        raw_files/
```

## Directories

| Directory | Purpose |
|---|---|
| [frameworks/](frameworks/index.md) | End-to-end methodology bundles — top of the traceability chain |
| [components/](components/index.md) | Formula pages for each framework component, with factors tables |
| [outputs/](outputs/index.md) | Generated BigQuery tables and columns, with component links |
| [report-templates/](report-templates/index.md) | Report interpretation guides, line-item to column mapping |
| [code-links/](code-links/index.md) | Repo + path + symbol pointers — load-bearing for change tracing |
| [change-checks/](change-checks/index.md) | Agent review rules triggered by code, output, or source changes |
| [metrics/](metrics/index.md) | Registry of terms keyed by name; atomic metric definitions |

## Example Chain

**Framework:** [Example Chain Framework](frameworks/example-chain.md)

**Components:**
- None yet.

**Outputs:**
- [Example Daily Snapshot](outputs/example-daily-snapshot.md)

**Reports:**
- [Example Daily Report](report-templates/example-daily-report.md)

**Implementation:**
- [Example Pipeline Engine](code-links/example-pipeline-engine.md)
- [Example Chain Review Rules](change-checks/example-chain-review-rules.md)

**Metrics:**
- [Example Metric](metrics/example-metric.md)

## Bot Consumption Rules

1. Start retrieval from this index unless the user provides a specific OKF page.
2. Prefer `status: approved` over `status: draft`.
3. Select the traversal path from **Question-Type Routing** before opening detailed pages.
4. Follow links: report → output → component → framework → code → source.
5. Cite the OKF files used to answer the question.
6. Do not invent formulas, definitions, code paths, table columns, or report behavior.
7. If code and OKF conflict, report the conflict explicitly.
8. If OKF is missing a link in the traceability chain, say which link is missing.
9. If source documents changed but OKF was not updated, flag the affected pages.
