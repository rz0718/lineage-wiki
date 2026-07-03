---
type: Index
title: OKF Metrics Registry
description: Registry keyed by term name. Front door for any quantity queried by name — points to where its definition lives.
status: draft
tags:
  - okf
  - metrics
  - index
timestamp: 2026-07-03T00:00:00Z
---

# OKF Metrics Registry

This registry is the front door for any term queried by name. Each entry is
one line pointing to where the authoritative definition lives — it does not
restate the definition.

- A standalone quantity (defined without a parent framework) → its
  `metrics/` page.
- A framework intermediate that people also ask about by name → its
  `components/` page.

## Standalone Metrics

| Term | Definition lives at |
|---|---|
| Example Metric | [Example Metric](example-metric.md) |

## Example Chain Terms

| Term | Type | Definition lives at |
|---|---|---|
| Example Chain | Framework | [Example Chain Framework](../frameworks/example-chain.md) |
