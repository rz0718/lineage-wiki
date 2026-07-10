"""Shared constants: OKF page taxonomy, required sections, validation rules."""

from __future__ import annotations

# --- OKF page taxonomy -------------------------------------------------------

# Title-cased page `type` labels, exactly as used in the OKF catalog.
PAGE_TYPES = (
    "Index",
    "Framework",
    "Component",
    "Output",
    "Code Link",
    "Report Template",
    "Change Check",
    "Metric",
)

# okf/ subdirectory -> page type of the pages that live there.
DIR_TO_TYPE = {
    "frameworks": "Framework",
    "components": "Component",
    "outputs": "Output",
    "report-templates": "Report Template",
    "code-links": "Code Link",
    "change-checks": "Change Check",
    "metrics": "Metric",
}

TYPE_TO_DIR = {v: k for k, v in DIR_TO_TYPE.items()}

OKF_SUBDIRS = tuple(DIR_TO_TYPE)

# The eight index files maintained after every run, relative to the okf dir.
INDEX_FILES = ("index.md",) + tuple(f"{d}/index.md" for d in OKF_SUBDIRS)

# --- Frontmatter -------------------------------------------------------------

# Frontmatter fields that carry relative Markdown references. The validator
# resolves every `.md` path found (at any nesting depth) under these keys.
REF_KEYS = (
    "source_refs",
    "framework_refs",
    "component_refs",
    "metric_refs",
    "implementation_refs",
    "code_refs",
    "output_refs",
    "report_refs",
    "change_check",
)

PAGE_STATUSES = ("draft", "reviewed", "approved", "deprecated")

# --- Required sections per page type (spec section 6) ------------------------

REQUIRED_SECTIONS: dict[str, tuple[str, ...]] = {
    "Index": (),
    "Framework": (
        "Scope",
        "Core Assumptions",
        "Core Formula",
        "Components",
        "Implementation",
        "Outputs",
        "Reports",
        "Verification Status",
        "Known Gaps",
        "Known Doc-vs-Code Divergences",
        "Source",
    ),
    "Component": (
        "What It Represents",
        "Factors Table",
        "Formula / Logic",
        "Inputs",
        "Outputs",
        "Edge Cases",
        "Verification Status",
        "Implementation Backlink",
    ),
    "Output": (
        "Table",
        "Grain",
        "Column Definitions",
        "Key Formula Mapping",
        "Upstream Sources",
        "Downstream Consumers",
        "Verification Status",
        "Implementation",
    ),
    "Code Link": (
        "Repository",
        "Implementation Areas",
        "Input Tables Consumed",
        "Outputs",
        "Runtime Assumptions",
        "Linked OKF Pages",
    ),
    "Report Template": (
        "Purpose",
        "Audience",
        "Metrics Shown",
        "Source Mapping",
        "BigQuery Source Mapping",
        "Interpretation Rules",
        "Known Caveats",
        "Verification Status",
    ),
    "Change Check": (
        "How to Trigger a Review",
        "Code Change Triggers",
        "Output Change Triggers",
        "Report Change Triggers",
        "Reference Document Change Triggers",
        "Required Agent Behavior",
        "Impacted Pages",
    ),
    "Metric": (
        "Definition",
        "Business Meaning",
        "Calculation Logic",
        "Unit",
        "Grain",
        "Source References",
        "Used By",
        "Caveats",
    ),
}

# --- Placeholder validation ---------------------------------------------------

# Tokens that mark unfinished content. They fail validation unless they appear
# inside a Known Gaps / open-issues style section, or inside an inline code
# span (documentation *about* placeholders, e.g. `<path-TBD>`).
PLACEHOLDER_PATTERN = r"\bTODO\b|\bTBD\b|\?{3}"

# H2 section headings under which placeholder tokens are allowed.
PLACEHOLDER_ALLOWED_SECTION_PATTERN = r"known gaps|open issues|gap|divergence"

# Marker embedded in tool-rendered index files. An existing index without
# this marker (and not listed in the manifest's managed_indexes) is treated
# as hand-written and is never overwritten.
GENERATED_MARKER = "<!-- generated-by: lineage-wiki -->"

# H2 headings whose existing content survives tool rewrites of an owned
# page: they are filled by `verify-bq` or reviewed by humans, never by the
# scaffold templates.
PRESERVED_SECTIONS = ("Verification Status", "Known Doc-vs-Code Divergences")

# Every scaffold-written Verification Status body starts with this phrase
# (see templates._scaffold_note). A Verification Status section that still
# contains it holds no verify-bq results or human notes, so a rewrite may
# refresh it instead of preserving stale evidence state.
SCAFFOLD_STATUS_MARK = "Unverified scaffold"

# Every Verification Status body reconciled from an LLM grounding run leads
# with this phrase (see agent/llm_pipeline). Like SCAFFOLD_STATUS_MARK it
# identifies a tool-authored body holding no verify-bq results or human
# notes: the section merge lets a newer grounding note replace it, and
# reverts it to scaffold text when the page's grounded sections are
# invalidated — verify-bq and human-written bodies carry neither mark and
# are always preserved. Deliberately distinct wording from credentialed
# BigQuery verification ("Verified from BigQuery schema metadata …").
GROUNDING_STATUS_MARK = "LLM claim grounding"

# Per-page-type H2 headings the LLM pipeline must never enrich, on top of
# PRESERVED_SECTIONS / Known Gaps (llm_pipeline._FORBIDDEN_SECTIONS). These
# sections are template-owned: procedural change-check instructions must not
# become an LLM write surface (and could never satisfy the citation rules),
# and the output-page schema table is rendered verbatim from the loaded
# BigQuery schema — LLM-derived column meaning goes to `Column Meanings`.
ENRICHMENT_DENYLIST: dict[str, tuple[str, ...]] = {
    "Change Check": ("How to Trigger a Review", "Required Agent Behavior"),
    "Output": ("Column Definitions",),
}


def enrichment_denied_sections(rel_path: str) -> tuple[str, ...]:
    """Deny-listed (template-owned, never LLM-enrichable) section headings
    for the page at ``rel_path``, keyed by its okf subdirectory."""
    parts = rel_path.split("/")
    page_type = DIR_TO_TYPE.get(parts[-2]) if len(parts) >= 2 else None
    return ENRICHMENT_DENYLIST.get(page_type, ()) if page_type else ()

# --- init scaffolding ---------------------------------------------------------

MANIFEST_DIR = ".lineage-wiki"
MANIFEST_FILE = f"{MANIFEST_DIR}/manifest.yml"

# Legacy heading marker: blocks written before the start/end comment
# markers existed are recognized (and migrated) by this heading.
AGENT_INSTRUCTIONS_MARKER = "## OKF Wiki Context"

# Managed-block delimiters in AGENTS.md / CLAUDE.md. Everything between
# them is owned by `lineage-wiki init --agents` and is replaced on update;
# everything outside them is never touched.
AGENT_BLOCK_START = "<!-- okf-wiki-context:start -->"
AGENT_BLOCK_END = "<!-- okf-wiki-context:end -->"

AGENT_INSTRUCTIONS_BLOCK = f"""\
{AGENT_BLOCK_START}
## OKF Wiki Context

This repository contains an Open Knowledge Format catalog under `okf/`.

Start here:
- [OKF index](okf/index.md)

When working on data products, formulas, BigQuery tables, dashboards, risk
definitions, revenue methodology, spread methodology, data definitions, or
liquidity definitions:

1. Start from `okf/index.md`.
2. Follow links to the relevant framework, component, output, code-link,
   report-template, metric, and change-check pages.
3. Do not change formulas, table mappings, or report behavior without
   updating the relevant OKF pages.
4. If code and OKF conflict, record the divergence (the framework page's
   `## Known Doc-vs-Code Divergences` section and the relevant
   change-check page) instead of silently rewriting either side.
5. Do not invent missing lineage: no formulas, code paths, output columns,
   or report behavior without evidence — record a Known Gap instead.
{AGENT_BLOCK_END}
"""

# Prompt stubs written by `lineage-wiki init`. The LLM pipeline that consumes
# them lands in a later milestone; the files exist so runs are configurable
# from day one.
PROMPT_STUBS: dict[str, str] = {
    "system.md": """\
# Lineage Wiki — System Prompt

You maintain an Open Knowledge Format (OKF) catalog for data products.

Rules:

1. Never invent formulas, table columns, code paths, dashboard behavior, or
   business definitions.
2. If evidence is missing, create a Known Gap instead of guessing.
3. If raw documentation conflicts with code or BigQuery schema, record a
   Known Doc-vs-Code Divergence.
4. Prefer implementation evidence in this order: raw methodology, source
   code, BigQuery schema/SQL, report mapping, human notes.
5. Every formula must cite or link to at least one source.
6. Work only inside the target repository and configured source paths.
7. Do not read secrets, `.env` files, private keys, tokens, or credential
   files.
""",
    "page_planner.md": """\
# Page Planner Prompt

Given normalized evidence for one chain, plan the OKF pages to create or
update: framework, components, outputs, code links, report templates,
change checks, and metrics. Plan surgical edits during update runs — touch
only pages affected by changed evidence.
""",
    "extractor.md": """\
# Extractor Prompt

Extract formulas, business definitions, components, source tables, output
tables, code paths, report mappings, gaps, and divergences from the supplied
evidence items. Every extracted fact must cite at least one evidence item.
""",
    "writer.md": """\
# Writer Prompt

Write OKF Markdown pages using the catalog's frontmatter style, title-cased
page types, and required sections for each type. Keep concepts canonical:
detailed definition in one page, lightweight links elsewhere. Preserve
existing useful wording when it remains accurate. Write readable prose:
never paste raw evidence formatting (table rows, code fragments,
docstrings, or table-of-contents links) into a section body.
""",
    "reviewer.md": """\
# Reviewer Prompt

Review the proposed sections against the accepted claims. Reject any
formula, column, code path, or report behavior that lacks a citation or is
not supported by a claim. Judge only what you are shown — deterministic
validation checks links, indexes, and required sections elsewhere.
""",
}
