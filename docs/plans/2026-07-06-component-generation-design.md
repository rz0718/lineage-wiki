# Component Generation Design

## Goal

Generate OKF component pages automatically while preserving the current safety
model: page identity is deterministic, and LLM output can only enrich
pre-planned pages with cited, grounded evidence.

Components are the important formula and business-rule building blocks under a
framework. They sit between the framework-level methodology and the concrete
code, BigQuery outputs, and report lines that implement or display the result.

## Recommended Approach

Add explicit, manually maintained component entries to the chain config:

```yaml
sources:
  components:
    - name: Settled Revenue
      description: Closed-position value movement.
      code_ref: example-revenue-engine
      output_refs:
        - project.dataset.example_revenue_daily
    - name: Projected Value
      description: Mark-to-market valuation for open positions.
```

Deterministic generation creates one `components/*.md` page per configured
component. The LLM may enrich those pages only when `--use-llm` is enabled, and
only because the deterministic plan already contains them.

The model must not invent new component pages in this milestone.

## Config Semantics

Each component entry should include:

- `name`: required display name and slug source.
- `description`: optional short business definition.
- `code_ref`: optional configured repo name or code-link identifier.
- `output_refs`: optional BigQuery table names already configured under
  `sources.bigquery.tables`.

Unmatched `code_ref` or `output_refs` should be reported as Known Gaps rather
than silently ignored.

## Generated Pages

`plan_chain_pages()` should include component drafts after the framework page
and before dependent pages that reference them. Each generated component page
should:

- use `type: Component`;
- link back to the framework through `framework_refs`;
- include `code_refs` when a configured component code reference resolves;
- include `output_refs` when configured output references resolve;
- carry scaffold sections for representation, factors, formula, inputs,
  outputs, edge cases, verification status, and implementation backlink.

The framework page's `## Components` section should list configured component
pages instead of the current placeholder text.

## LLM Enrichment

With `--use-llm`, component pages become eligible for the existing planner,
extractor, writer, and reviewer flow.

The LLM may use available evidence from raw docs, local repo files, BigQuery
schemas, report mapping notes, and human notes to enrich component sections.
The existing grounding rules still apply:

- every claim cites known evidence;
- formulas, definitions, mappings, and facts need verbatim supporting quotes;
- column claims cite ingested BigQuery schema evidence;
- code path claims cite loaded repository evidence;
- written sections cite accepted evidence with `[src: ...]` markers;
- SQL remains disallowed in LLM-written sections.

Rejected formulas remain Known Gaps. Accepted component prose should follow the
same stale-citation invalidation behavior as other LLM-written sections.

## Indexes And Cross-References

Existing index generation already scans `type: Component` pages. Once component
pages are generated, the following should update naturally:

- `components/index.md` lists generated component pages;
- `metrics/index.md` can point framework terms to component pages;
- `okf/index.md` includes components in the framework block;
- impact planning can include linked component pages for raw doc and code
  changes.

Where output pages or reports can safely reference configured components, those
links can be added in a later pass. V1 only needs framework-to-component and
component-to-framework links plus optional code/output frontmatter refs.

## Update Behavior

Component pages should be tool-owned like other generated pages. A config change
that adds a component creates a new page. A config change that edits a component
updates the scaffold while preserving manual and LLM-managed sections under the
existing preservation rules.

Removing a component from config should follow the existing generated-file
ownership behavior for pages no longer planned. If the current system does not
delete stale generated pages, this feature should not introduce deletion as a
side effect.

Raw doc, code, BigQuery, and report changes should affect component pages when
they are linked through frontmatter or framework membership.

## Compatibility And Safety

This design keeps deterministic and LLM responsibilities separate:

- config controls which component pages exist;
- deterministic templates create valid scaffold pages;
- LLM mode enriches sections but cannot create files;
- validation and grounding remain responsible for rejecting unsupported claims.

Existing hand-written component pages remain supported because indexes scan the
OKF tree. A generated component page must not overwrite a hand-written page
unless it is already owned by the current chain manifest entry.

## Test Plan

Add and update tests for:

- config parsing for `sources.components`;
- deterministic planning includes component pages when configured;
- generated component pages have valid frontmatter and required sections;
- framework pages list configured components;
- component indexes include generated component pages;
- invalid `code_ref` or `output_refs` become Known Gaps;
- LLM planner can select component pages but still cannot invent unplanned
  pages;
- update impact includes linked component pages for relevant source changes;
- existing no-component configs keep the current placeholder behavior.
