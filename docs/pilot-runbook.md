# Real-Chain Pilot Runbook

How to take lineage-wiki from fixture-tested to proven on one real data-product
chain, and what comes after. Agreed direction as of 2026-07-07.

## Why a pilot first

The deterministic core (generate / update / verify-bq / validate, LLM
grounding, scheduled GitHub Action) is built and fixture-tested but has never
run against a real chain. The open design questions — are Known Gaps legible
as questions? do divergences get acted on? — only answer themselves against a
real vertical. Run the pilot before building new features.

## Core thesis

People know more than they can write. The artifacts they produced for other
reasons — methodology docs, source code, BigQuery schemas, reports — encode
decisions never articulated. The wiki is not a place where knowledge is
written; it is a place where evidence is reconciled:

- artifacts are the durable memory;
- humans are the oracle, queried sparingly;
- every Known Gap and divergence is a well-formed question to route to a
  human, whose answer becomes fingerprinted, citable evidence.

## Stage 0 — pick the chain

Small but complete:

- one methodology doc (a rough export dumped to Markdown is fine);
- one repo with a local clone;
- 1–3 BigQuery tables;
- one report people actually read;
- an owner who will answer questions.

Prefer a chain with a **suspected doc-vs-code discrepancy** — a known-stale
methodology is the best possible test input: it exercises the divergence
machinery and lets you judge the result against known reality.

## Stage 1 — set up the target wiki repo

Keep it separate from this tool repo: a fresh **private** git repo, or the
existing OKF catalog repo. If that repo has hand-written pages, do the first
runs against a scratch clone to verify overwrite protection on real
hand-written content before trusting it.

```bash
uv run lineage-wiki init --root /path/to/wiki-repo
```

## Stage 2 — write the real chain config

Copy `chains/example.yml` to `chains/<real-chain>.yml`. Start minimal and
honest:

- the methodology file under `raw_files/<chain>/`;
- the repo `local_path` with a handful of `paths:` and the 2–3 `symbols:`
  that compute the core numbers;
- real table names under `sources.bigquery.tables`.

Set everything `required: false` initially so missing pieces become Known
Gaps instead of failures — the gap list is diagnostic output at this stage,
not noise.

## Stage 3 — dry-run until the evidence looks right

```bash
uv run lineage-wiki generate --config chains/<real-chain>.yml \
  --target-repo /path/to/wiki-repo --dry-run
```

Nothing is written. Check: does it find the configured evidence (symbols
located, doc loaded), and is the Known Gaps list *honest* — every gap either
genuinely unknown or a config mistake to fix? Iterate here; this loop is
free.

## Stage 4 — real deterministic generate, then read it

```bash
uv run lineage-wiki generate --config chains/<real-chain>.yml --root /path/to/wiki-repo
uv run lineage-wiki validate --root /path/to/wiki-repo
```

Commit, then evaluate as a human: open the framework page and walk
report → output → component → code link. First genuine product test —
scaffold quality on real content.

## Stage 5 — real BigQuery, escalating modes

```bash
uv sync --extra bigquery
gcloud auth application-default login
uv run lineage-wiki generate --config chains/<real-chain>.yml --root /path/to/wiki-repo  # real schemas
uv run lineage-wiki verify-bq --config chains/<real-chain>.yml --root /path/to/wiki-repo
```

Run `verify-bq` in `schema_only` mode first, then `profile`, then add **one**
`formula_check` for the chain's core formula. The formula check is the payoff
moment:

- `Matches` validates the pipeline end-to-end;
- `Source stale` / `Genuine conflict` means the tool found the discrepancy
  the chain was picked for.

Cost is bounded by `max_bytes_billed` and the date window. The exact SQL run
is in `.lineage-wiki/runs/<run-id>.json`.

## Stage 6 — test the maintenance claim

```bash
uv run lineage-wiki update --config chains/<real-chain>.yml --root /path/to/wiki-repo  # expect strict no-op
# then: edit the methodology doc, or pull a new commit in the source repo
uv run lineage-wiki update --config chains/<real-chain>.yml --root /path/to/wiki-repo --plan-only
```

Verify the no-op is truly silent (no writes, no manifest churn) and that
after a real source change the impact plan names only the pages that should
be affected. If the plan looks right, apply the update for real.

## Stage 7 — LLM enrichment, reviewed

Run `generate --use-llm --dry-run` first, then for real. Read the stage
transcript in `.lineage-wiki/runs/<run-id>-generate-llm.json` — accepted vs
rejected claims with reasons. On real methodology text, the rejection rate
and reasons tell you whether the grounding rules are calibrated.

## Stage 8 — turn on the loop

`init --github-action`, add `GCP_SERVICE_ACCOUNT_JSON` as a repo secret, let
the daily run open its first PR after a real upstream change. Then send that
PR to the chain owner and watch whether the Known Gaps read as answerable
questions to someone who did not build the tool. Their reaction is the
requirements document for the gap-resolution loop — build it after this
observation, not before.

## Cautions

- The wiki repo will contain internal methodology and schema names — keep it
  private; check raw docs before committing them.
- Pages never receive row-level data or profiled values; those go to
  `.lineage-wiki/runs/`, which the Action stages — confirm you are
  comfortable committing run JSONs with profiled aggregates, or gitignore
  `runs/`.

## Pilot success criteria (in order)

1. The formula check classifies correctly against known reality.
2. Update runs are surgical; no-ops are silent.
3. The chain owner can read the gap list and answer at least one gap unaided.

---

# After the pilot: agreed direction

## 1. Gap-resolution loop (designed, not implemented)

Turns Known Gaps from warnings into an interview channel:

- **Gaps get identity** — `plan.gaps: list[str]` becomes structured objects:
  stable id (hash of chain + kind + subject), kind
  (`missing-methodology`, `unmapped-report-line`,
  `divergence-adjudication`, …), a question phrased for a domain owner, and
  what evidence would resolve it. Register persisted in the manifest.
- **Questions reach humans in the Action PR** — for each open gap the run
  scaffolds an answer stub `raw_files/<chain>/answers/<gap-id>.md` with
  frontmatter (`answers: <gap-id>`, `author:`) plus the question and a blank
  section. Answering = filling a blank in the GitHub UI and merging.
- **Answers become evidence via existing machinery** — the raw_doc connector
  ingests the answer file; it gets a fingerprint and becomes an
  `EvidenceItem` (`source_type: human_note`, metadata linking the gap id).
  The next update run maps the answered gap to its owning page; prose
  extraction from answers goes through the existing `--use-llm` grounding
  rules (verbatim-quote citations against the answer file).
- **Answers expire like everything else** — record which evidence state the
  gap was computed from; when that state changes materially, re-open the gap
  with a note ("previously answered by <author> on <date>; underlying
  evidence has changed"). Mirrors the existing LLM section
  invalidation-on-fingerprint-change. Without this, answer files become
  relocated tribal lore.
- **Divergences use the same loop** — a divergence answer is an adjudication
  from a closed set: *doc stale* (implementation wins), *code bug*
  (divergence stays open with ticket ref), *both intentional* (the
  explanation becomes the documentation).

Implementation delta: structure gaps in `agent/planner.py`, persist the
register in the manifest, teach the Action to scaffold answer stubs, teach
`connectors/raw_doc_connector.py` to recognize `answers/` frontmatter, extend
the update planner with "answered gap → owning page" and "answer's underlying
evidence changed → re-open". Fingerprinting, impact planning, grounding, and
invalidation are already built and reused.

## 2. `inspect` command (spec'd in spec.md §10, unbuilt)

The reverse traversal — report line → output column → component formula →
code path → methodology → human note — as one command and one agent-facing
tool. This is the consumption half of "traceable for agent and human";
unused wikis rot regardless of tooling.

## 3. Report connector

Currently a stub (name, URL, empty notes), yet reports are where humans start
their questions. Even a manual-but-structured mapping format
(report line item → `table.column`) makes the reverse chain real.

## System health metric

Open-gap count and answer latency per chain — not page count — tell you
whether the wiki is alive.
