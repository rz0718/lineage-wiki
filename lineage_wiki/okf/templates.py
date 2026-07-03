"""Deterministic Markdown templates for every OKF page type.

Renders scaffold pages from a chain config alone — no LLM, no ingestion.
The templates never invent formulas, columns, code paths, or report
behavior: anything unknown is stated as such and enumerated under the
framework's Known Gaps section, matching the OKF agent behavior rules.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ChainConfig, MetricInput, RepoSource, ReportSource
from ..connectors.local_repo_connector import RepoLoadResult
from ..ingestion.code_indexer import CodeIndex, index_repo
from ..ingestion.evidence import EvidenceItem
from ..ingestion.source_loader import EvidenceBundle, load_sources
from ..util import humanize, slugify
from .schemas import render_frontmatter


@dataclass
class PageDraft:
    """One rendered page, path relative to the repo root."""

    rel_path: str
    content: str


@dataclass
class ChainPlan:
    """All non-index pages planned for one chain, plus the gap register."""

    pages: list[PageDraft]
    gaps: list[str]


# --- Planning context -----------------------------------------------------------


@dataclass
class _Ctx:
    cfg: ChainConfig
    now: str
    okf: str
    framework_rel: str = ""
    framework_title: str = ""
    # (source object, rel path within okf dir, page title)
    code_links: list[tuple[RepoSource, str, str]] = field(default_factory=list)
    outputs: list[tuple[str, str, str]] = field(default_factory=list)
    reports: list[tuple[ReportSource, str, str]] = field(default_factory=list)
    metrics: list[tuple[MetricInput, str]] = field(default_factory=list)
    change_check_rel: str = ""
    change_check_title: str = ""
    evidence: EvidenceBundle = field(default_factory=EvidenceBundle)
    raw_docs_found: list[EvidenceItem] = field(default_factory=list)
    raw_docs_missing: list[str] = field(default_factory=list)
    repo_loads: dict[str, RepoLoadResult] = field(default_factory=dict)
    code_indexes: dict[str, CodeIndex] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    def link_from(self, src_dir: str, target_rel: str) -> str:
        """Relative link from a page in okf/<src_dir>/ to another okf page."""
        return posixpath.relpath(target_rel, start=src_dir)

    def raw_doc_ref(self, src_dir: str, raw_path: str) -> str:
        """Relative link from a page in okf/<src_dir>/ to a repo-root path."""
        return posixpath.relpath(raw_path, start=posixpath.join(self.okf, src_dir))


def _build_ctx(cfg: ChainConfig, root: Path, now: str) -> _Ctx:
    chain = cfg.chain
    ctx = _Ctx(cfg=cfg, now=now, okf=cfg.generation.output_dir)

    ctx.framework_rel = f"frameworks/{chain.slug}.md"
    ctx.framework_title = f"{chain.name} Framework"
    ctx.change_check_rel = f"change-checks/{chain.slug}-review-rules.md"
    ctx.change_check_title = f"{chain.name} Review Rules"

    for repo in cfg.sources.repos:
        repo_slug = slugify(repo.name)
        ctx.code_links.append(
            (repo, f"code-links/{repo_slug}-engine.md", f"{humanize(repo.name)} Engine")
        )

    if cfg.sources.bigquery:
        for table in cfg.sources.bigquery.tables:
            leaf = slugify(table.split(".")[-1])
            ctx.outputs.append((table, f"outputs/{leaf}.md", humanize(leaf)))

    for report in cfg.sources.reports:
        ctx.reports.append((report, f"report-templates/{slugify(report.name)}.md", report.name))

    if cfg.generation.create_missing_metrics:
        for metric in cfg.sources.metrics:
            ctx.metrics.append((metric, f"metrics/{slugify(metric.name)}.md"))

    # Load local evidence (raises SourceUnavailableError for missing
    # required sources).
    bundle = load_sources(cfg, root)
    ctx.evidence = bundle
    ctx.raw_docs_found = bundle.raw_docs
    ctx.raw_docs_missing = bundle.missing_raw_docs
    ctx.repo_loads = {load.repo.name: load for load in bundle.repos}
    ctx.code_indexes = {load.repo.name: index_repo(load) for load in bundle.repos}

    # Deterministic gap register — one entry per fact the scaffold cannot know.
    gaps = ctx.gaps
    if ctx.raw_docs_found:
        gaps.append(
            "Formulas and business definitions have not been extracted from "
            "the ingested raw docs (LLM extraction lands in a later milestone)."
        )
    else:
        gaps.append(
            "Source methodology has not been ingested; framework scope "
            "details, assumptions, and formulas are undocumented."
        )
    gaps.append(
        "No component pages exist yet; the component inventory is unknown "
        "until formula-level evidence is ingested."
    )
    for path in ctx.raw_docs_missing:
        gaps.append(f"Configured raw doc `{path}` was not found at generate time.")
    for repo, _, _ in ctx.code_links:
        load = ctx.repo_loads[repo.name]
        if not load.available:
            gaps.append(
                f"Repo `{repo.name}` has no local clone available; configured "
                "paths and symbols are unverified references (remote GitHub "
                "ingestion lands in a later milestone)."
            )
            continue
        for path in load.missing_paths:
            gaps.append(f"Configured path `{path}` was not found in repo `{repo.name}`.")
        for symbol in ctx.code_indexes[repo.name].missing_symbols:
            gaps.append(
                f"Configured symbol `{symbol}` was not found in the loaded "
                f"files of repo `{repo.name}`."
            )
        gaps.append(
            f"Code loaded from repo `{repo.name}` has not been cross-checked "
            "against the methodology (cross-checking lands in a later milestone)."
        )
    for table, _, _ in ctx.outputs:
        gaps.append(
            f"BigQuery schema for `{table}` has not been ingested; grain and "
            "column definitions are undocumented."
        )
    for report, _, _ in ctx.reports:
        if not report.source_mapping_notes:
            gaps.append(f"Report `{report.name}` has no verified source mapping.")
    for metric, _ in ctx.metrics:
        if not metric.definition:
            gaps.append(f"Metric `{metric.name}` has no configured definition.")
    return ctx


# --- Shared rendering helpers ---------------------------------------------------


def _page(fm: dict[str, Any], body: str) -> str:
    return render_frontmatter(fm) + "\n" + body.strip() + "\n"


def _base_fm(ctx: _Ctx, *, type_: str, title: str, description: str, tags: list[str]) -> dict:
    return {
        "type": type_,
        "title": title,
        "description": description,
        "owner": ctx.cfg.chain.owner,
        "status": "draft",
        "tags": tags,
        "timestamp": ctx.now,
    }


def _tags(ctx: _Ctx, *extra: str) -> list[str]:
    return ctx.cfg.chain.slug.split("-") + list(extra)


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"- {item}" for item in items) if items else empty


def _scaffold_note(ctx: _Ctx, page_kind: str) -> str:
    # Deliberately date-free: the frontmatter `timestamp` carries the run
    # date, and a date in the body would defeat no-op detection across days.
    return (
        f"Unverified scaffold {page_kind} generated deterministically by "
        "`lineage-wiki` from the chain config and locally loaded evidence. "
        "No fact on this page has been cross-checked against that evidence yet."
    )


# --- Framework -------------------------------------------------------------------


def render_framework_page(ctx: _Ctx) -> str:
    cfg = ctx.cfg
    chain = cfg.chain
    d = "frameworks"

    fm = _base_fm(
        ctx,
        type_="Framework",
        title=ctx.framework_title,
        description=chain.description
        or f"End-to-end methodology bundle for the {chain.name} chain.",
        tags=_tags(ctx, "framework", "methodology"),
    )
    if ctx.raw_docs_found:
        fm["source_refs"] = [ctx.raw_doc_ref(d, doc.source_uri) for doc in ctx.raw_docs_found]
    if ctx.code_links:
        fm["implementation_refs"] = [
            {
                "repo": repo.name,
                "primary": i == 0,
                "ref": repo.branch,
                "path": "/",
                "code_link": ctx.link_from(d, rel),
            }
            for i, (repo, rel, _) in enumerate(ctx.code_links)
        ]
    if ctx.outputs:
        fm["output_refs"] = [
            {
                "system": "bigquery",
                "primary": i == 0,
                "table": table,
                "output": ctx.link_from(d, rel),
            }
            for i, (table, rel, _) in enumerate(ctx.outputs)
        ]
    if ctx.reports:
        fm["report_refs"] = [ctx.link_from(d, rel) for _, rel, _ in ctx.reports]
    if ctx.metrics:
        fm["metric_refs"] = [ctx.link_from(d, rel) for _, rel in ctx.metrics]
    fm["change_check"] = ctx.link_from(d, ctx.change_check_rel)
    fm["approved_by"] = None
    fm["approval_date"] = None
    fm["review_cycle"] = "on methodology change"

    scope = chain.description or "No chain description was configured."
    if chain.domain:
        scope += f"\n\nDomain: `{chain.domain}`."

    notes = ""
    if cfg.sources.human_notes:
        note_lines = "\n".join(
            f"- **{note.title}:** {note.content}" for note in cfg.sources.human_notes
        )
        notes = (
            "\n\nHuman-supplied notes (unverified evidence, lowest priority in "
            "the evidence order):\n\n" + note_lines
        )

    if ctx.code_links:
        impl_rows = "\n".join(
            f"| `{repo.name}` | `{repo.branch}` | {repo.host} | "
            f"[{title}]({ctx.link_from(d, rel)}) |"
            for repo, rel, title in ctx.code_links
        )
        implementation = (
            "| Repo | Branch | Host | Code Link |\n|---|---|---|---|\n" + impl_rows
        )
    else:
        implementation = "No code repositories are configured for this chain."

    if ctx.outputs:
        output_rows = "\n".join(
            f"| `{table}` | [{title}]({ctx.link_from(d, rel)}) |"
            for table, rel, title in ctx.outputs
        )
        outputs = "| Table | Output Page |\n|---|---|\n" + output_rows
    else:
        outputs = "No BigQuery outputs are configured for this chain."

    reports = _bullets(
        [f"[{title}]({ctx.link_from(d, rel)})" for _, rel, title in ctx.reports],
        "No reports are configured for this chain.",
    )

    sources: list[str] = []
    for doc in ctx.raw_docs_found:
        sources.append(
            f"[{posixpath.basename(doc.source_uri)}]({ctx.raw_doc_ref(d, doc.source_uri)}) "
            f"— {doc.metadata['doc_type']}, ingested "
            f"({doc.metadata['lines']} lines; title: {doc.title})"
        )
    for path in ctx.raw_docs_missing:
        sources.append(
            f"Configured raw doc `{path}` was not found at generate time — "
            "see [Known Gaps](#known-gaps)."
        )
    source_section = _bullets(sources, "No raw source documents are configured for this chain.")

    loaded_repos = [load for load in ctx.repo_loads.values() if load.available]
    n_code_files = sum(len(load.files) for load in loaded_repos)
    if ctx.raw_docs_found:
        raw_status = (
            f"Ingested — {len(ctx.raw_docs_found)} doc(s) loaded; extraction pending"
        )
    else:
        raw_status = "Not ingested (scaffold)"
    if loaded_repos:
        code_status = (
            f"Loaded — {n_code_files} file(s) from {len(loaded_repos)} repo(s); "
            "cross-check pending"
        )
    elif ctx.code_links:
        code_status = "Not loaded — no local clone available"
    else:
        code_status = "Not ingested (scaffold)"

    body = f"""\
# {ctx.framework_title}

## Scope

{scope}

This page is a deterministic scaffold created by `lineage-wiki` from
`chains/{chain.slug}.yml`-style config. Scope details beyond the configured
description have not been ingested — see [Known Gaps](#known-gaps).

## Core Assumptions

No verified assumptions have been documented yet — see
[Known Gaps](#known-gaps).{notes}

## Core Formula

No formula has been documented for this chain yet. `lineage-wiki` does not
invent formulas; the core formula will be added once methodology or code
evidence is ingested — see [Known Gaps](#known-gaps).

## Components

No component pages have been generated for this framework yet. Components
are added once formula-level evidence is ingested — see
[Known Gaps](#known-gaps).

## Implementation

{implementation}

## Outputs

{outputs}

## Reports

{reports}

## Verification Status

| Evidence | Status |
|---|---|
| Raw methodology | {raw_status} |
| Source code | {code_status} |
| BigQuery schemas | Not ingested (scaffold) |
| Report mappings | Not ingested (scaffold) |

{_scaffold_note(ctx, "framework page")}

## Known Gaps

{_bullets(ctx.gaps, "None recorded.")}

## Known Doc-vs-Code Divergences

None recorded yet. Divergences are only recorded once documentation, code,
and output evidence have been ingested and cross-checked.

## Source

{source_section}

Review rules: [{ctx.change_check_title}]({ctx.link_from(d, ctx.change_check_rel)})
"""
    return _page(fm, body)


# --- Component ---------------------------------------------------------------------


def render_component_page(
    ctx: _Ctx,
    *,
    title: str,
    description: str = "",
    code_link_rel: str | None = None,
) -> str:
    """Component scaffold. Not emitted by the deterministic generate run (no
    component evidence exists in a chain config), but part of the template
    contract for later milestones."""
    d = "components"
    fm = _base_fm(
        ctx,
        type_="Component",
        title=title,
        description=description or f"Component building block of the {ctx.framework_title}.",
        tags=_tags(ctx, "component"),
    )
    fm["framework_refs"] = [ctx.link_from(d, ctx.framework_rel)]
    if code_link_rel:
        fm["code_refs"] = [ctx.link_from(d, code_link_rel)]

    if code_link_rel:
        code_cell = f"[code link]({ctx.link_from(d, code_link_rel)})"
        backlink = f"- {code_cell}"
    else:
        code_cell = "Not yet documented"
        backlink = (
            "No code link is associated with this component yet — see the "
            f"framework page's [Known Gaps]({ctx.link_from(d, ctx.framework_rel)}#known-gaps)."
        )

    body = f"""\
# {title}

## What It Represents

{description or "Not yet documented — no evidence has been ingested for this component."}

## Factors Table

| Component | What it represents | Driving factors | Code location |
|---|---|---|---|
| {title} | Not yet documented | Not yet documented | {code_cell} |

## Formula / Logic

No formula evidence has been ingested yet. `lineage-wiki` does not invent
formulas — see the framework page's
[Known Gaps]({ctx.link_from(d, ctx.framework_rel)}#known-gaps).

## Inputs

Not yet documented.

## Outputs

Not yet documented.

## Edge Cases

Not yet documented.

## Verification Status

{_scaffold_note(ctx, "component page")}

## Implementation Backlink

{backlink}

Framework: [{ctx.framework_title}]({ctx.link_from(d, ctx.framework_rel)})
"""
    return _page(fm, body)


# --- Output ---------------------------------------------------------------------


def render_output_page(ctx: _Ctx, table: str, rel: str, title: str) -> str:
    d = "outputs"
    fm = _base_fm(
        ctx,
        type_="Output",
        title=title,
        description=f"Scaffold documentation for BigQuery table {table}.",
        tags=_tags(ctx, "bigquery", "output"),
    )
    fm["framework_refs"] = [ctx.link_from(d, ctx.framework_rel)]
    if ctx.code_links:
        fm["code_refs"] = [ctx.link_from(d, cl_rel) for _, cl_rel, _ in ctx.code_links]
    if ctx.reports:
        fm["report_refs"] = [ctx.link_from(d, r_rel) for _, r_rel, _ in ctx.reports]

    framework_link = ctx.link_from(d, ctx.framework_rel)
    consumers = _bullets(
        [f"[{r_title}]({ctx.link_from(d, r_rel)})" for _, r_rel, r_title in ctx.reports],
        "None recorded yet.",
    )
    implementation = _bullets(
        [f"[{cl_title}]({ctx.link_from(d, cl_rel)})" for _, cl_rel, cl_title in ctx.code_links],
        "No code repositories are configured for this chain.",
    )

    body = f"""\
# {title}

## Table

`{table}`

## Grain

Not yet documented — the table grain will be recorded once BigQuery schema
evidence is ingested (tracked in the framework page's
[Known Gaps]({framework_link}#known-gaps)).

## Column Definitions

Not yet documented — no schema evidence has been ingested for this table.

## Key Formula Mapping

No formula mappings exist yet. Calculated columns will be linked to
component pages once formula evidence is ingested.

## Upstream Sources

Not yet documented — upstream tables will be recorded once code or SQL
evidence is ingested.

## Downstream Consumers

{consumers}

## Verification Status

{_scaffold_note(ctx, "output page")} The table identity comes from the chain
config; nothing else has been verified.

## Implementation

{implementation}

Framework: [{ctx.framework_title}]({framework_link})
"""
    return _page(fm, body)


# --- Code Link --------------------------------------------------------------------


def render_code_link_page(ctx: _Ctx, repo: RepoSource, rel: str, title: str) -> str:
    d = "code-links"
    fm = _base_fm(
        ctx,
        type_="Code Link",
        title=title,
        description=f"Implementation pointer for repo {repo.name} — paths, symbols, and outputs.",
        tags=_tags(ctx, "code"),
    )
    fm["framework_refs"] = [ctx.link_from(d, ctx.framework_rel)]
    if ctx.outputs:
        fm["output_refs"] = [ctx.link_from(d, o_rel) for _, o_rel, _ in ctx.outputs]

    load = ctx.repo_loads.get(repo.name) or RepoLoadResult(repo=repo, available=False)
    index = ctx.code_indexes.get(repo.name) or CodeIndex(repo_name=repo.name)

    repo_rows = [f"| Repo | `{repo.name}` |", f"| Host | {repo.host} |"]
    if repo.url:
        repo_rows.append(f"| URL | `{repo.url}` |")
    repo_rows.append(f"| Branch / ref | `{repo.branch}` |")
    if repo.local_path:
        repo_rows.append(f"| Local path | `{repo.local_path}` |")
    if load.available and load.git_head:
        repo_rows.append(f"| Verified head | `{load.git_head}` |")
    repository = "| Field | Value |\n|---|---|\n" + "\n".join(repo_rows)
    if load.available:
        repository += (
            "\n\nPaths and symbols below were loaded from the local clone at "
            "generate time."
        )
    else:
        repository += (
            "\n\nNo local clone was available at generate time; the repo "
            "identity above is config-supplied and unverified — see the "
            "framework page's "
            f"[Known Gaps]({ctx.link_from(d, ctx.framework_rel)}#known-gaps)."
        )

    if not repo.paths and not repo.symbols:
        areas = "No paths or symbols are configured for this repo."
    elif load.available:
        files_by_path = {item.metadata["path"]: item for item in load.files}
        blocks: list[str] = []
        if repo.paths:
            path_rows = []
            for path in repo.paths:
                item = files_by_path.get(path)
                if item:
                    path_rows.append(f"| `{path}` | Loaded | {item.metadata['lines']} |")
                else:
                    path_rows.append(f"| `{path}` | Not found | — |")
            blocks.append("| Path | Status | Lines |\n|---|---|---|\n" + "\n".join(path_rows))
        if repo.symbols:
            symbol_rows = []
            for symbol in repo.symbols:
                hit = index.best_hit(symbol)
                if hit:
                    symbol_rows.append(f"| `{symbol}` | `{hit.location}` | {hit.kind} |")
                else:
                    symbol_rows.append(f"| `{symbol}` | Not found in loaded paths | — |")
            blocks.append(
                "| Symbol | Located at | Kind |\n|---|---|---|\n" + "\n".join(symbol_rows)
            )
        areas = "\n\n".join(blocks)
    else:
        area_rows = [f"| Path | `{p}` |" for p in repo.paths] + [
            f"| Symbol | `{s}` |" for s in repo.symbols
        ]
        areas = (
            "Configured paths and symbols (from the chain config; unverified "
            "— no local clone was available):\n\n| Kind | Value |\n|---|---|\n"
            + "\n".join(area_rows)
        )

    outputs = _bullets(
        [f"[{o_title}]({ctx.link_from(d, o_rel)})" for _, o_rel, o_title in ctx.outputs],
        "No BigQuery outputs are configured for this chain.",
    )

    body = f"""\
# {title}

## Repository

{repository}

## Implementation Areas

{areas}

## Input Tables Consumed

Not yet documented — input tables are not extracted by the deterministic
scaffold; they will be recorded once code evidence is cross-checked
(tracked in the framework page's
[Known Gaps]({ctx.link_from(d, ctx.framework_rel)}#known-gaps)).

## Outputs

{outputs}

## Runtime Assumptions

Not yet documented — no runtime evidence has been ingested.

## Linked OKF Pages

- Framework: [{ctx.framework_title}]({ctx.link_from(d, ctx.framework_rel)})
- Change check: [{ctx.change_check_title}]({ctx.link_from(d, ctx.change_check_rel)})
"""
    return _page(fm, body)


# --- Report Template ----------------------------------------------------------------


def render_report_page(ctx: _Ctx, report: ReportSource, rel: str, title: str) -> str:
    d = "report-templates"
    fm = _base_fm(
        ctx,
        type_="Report Template",
        title=title,
        description=f"Interpretation guide scaffold for the {report.name}.",
        tags=_tags(ctx, "report"),
    )
    fm["framework_refs"] = [ctx.link_from(d, ctx.framework_rel)]
    if ctx.outputs:
        fm["output_refs"] = [ctx.link_from(d, o_rel) for _, o_rel, _ in ctx.outputs]

    framework_link = ctx.link_from(d, ctx.framework_rel)
    surface = f"Configured surface type: `{report.type}`."
    if report.url:
        surface += f" URL: {report.url}"

    if report.source_mapping_notes:
        mapping = f"Configured mapping notes (unverified):\n\n> {report.source_mapping_notes}"
    else:
        mapping = (
            "No verified source mapping yet — recorded in the framework "
            f"page's [Known Gaps]({framework_link}#known-gaps)."
        )

    bq_mapping = _bullets(
        [
            f"[{o_title}]({ctx.link_from(d, o_rel)}) — `{table}`"
            for table, o_rel, o_title in ctx.outputs
        ],
        "No BigQuery outputs are configured for this chain.",
    )

    body = f"""\
# {title}

## Purpose

{surface}

The purpose of this report has not been documented yet — see the framework
page's [Known Gaps]({framework_link}#known-gaps).

## Audience

Not yet documented.

## Metrics Shown

Not yet documented — line items will be recorded once report evidence is
ingested.

## Source Mapping

{mapping}

## BigQuery Source Mapping

The chain's configured BigQuery outputs (line-item to column mapping not yet
verified):

{bq_mapping}

## Interpretation Rules

Not yet documented.

## Known Caveats

None recorded yet.

## Verification Status

{_scaffold_note(ctx, "report template")}

Framework: [{ctx.framework_title}]({framework_link})
"""
    return _page(fm, body)


# --- Change Check ---------------------------------------------------------------------


def render_change_check_page(ctx: _Ctx) -> str:
    d = "change-checks"
    fm = _base_fm(
        ctx,
        type_="Change Check",
        title=ctx.change_check_title,
        description=(
            f"Review rules for {ctx.cfg.chain.name} code, output, report, and "
            "reference document changes."
        ),
        tags=_tags(ctx, "review", "change-check"),
    )
    fm["framework_refs"] = [ctx.link_from(d, ctx.framework_rel)]
    if ctx.code_links:
        fm["code_refs"] = [ctx.link_from(d, cl_rel) for _, cl_rel, _ in ctx.code_links]

    code_link_list = _bullets(
        [f"[{title}]({ctx.link_from(d, rel)})" for _, rel, title in ctx.code_links],
        "No code links exist for this chain yet.",
    )

    if ctx.code_links:
        code_trigger_blocks = []
        for repo, rel, title in ctx.code_links:
            watched = [f"`{p}`" for p in repo.paths] + [f"`{s}`" for s in repo.symbols]
            watched_text = ", ".join(watched) if watched else "any file"
            code_trigger_blocks.append(
                f"If {watched_text} changes in `{repo.name}` (branch `{repo.branch}`), "
                f"resolve the change through [{title}]({ctx.link_from(d, rel)}) and "
                "review the framework methodology."
            )
        code_triggers = "\n\n".join(code_trigger_blocks)
    else:
        code_triggers = "No code repositories are configured for this chain."

    if ctx.outputs:
        output_rows = "\n".join(
            f"| `{table}` | [{title}]({ctx.link_from(d, rel)}) |"
            for table, rel, title in ctx.outputs
        )
        output_triggers = (
            "If the schema or the producing job of any of these tables "
            "changes, review the affected output page:\n\n"
            "| Table | Output page |\n|---|---|\n" + output_rows
        )
    else:
        output_triggers = "No BigQuery outputs are configured for this chain."

    report_triggers = _bullets(
        [
            f"[{title}]({ctx.link_from(d, rel)}) — review if line items, layout, "
            "or the underlying query change."
            for _, rel, title in ctx.reports
        ],
        "No reports are configured for this chain.",
    )

    ref_docs = [doc.source_uri for doc in ctx.raw_docs_found] + ctx.raw_docs_missing
    ref_triggers = _bullets(
        [f"`{path}` — review the framework page if this document changes." for path in ref_docs],
        "No reference documents are configured for this chain.",
    )

    impacted = [f"[{ctx.framework_title}]({ctx.link_from(d, ctx.framework_rel)})"]
    impacted += [f"[{t}]({ctx.link_from(d, r)})" for _, r, t in ctx.code_links]
    impacted += [f"[{t}]({ctx.link_from(d, r)})" for _, r, t in ctx.outputs]
    impacted += [f"[{t}]({ctx.link_from(d, r)})" for _, r, t in ctx.reports]
    impacted += [f"[{m.name}]({ctx.link_from(d, r)})" for m, r in ctx.metrics]

    body = f"""\
# {ctx.change_check_title}

## How to Trigger a Review

1. Read the diff in the changed source (code repo, BigQuery table, report,
   or reference document).
2. Resolve changed code paths and symbols through the chain's code link
   pages.
3. Land on the affected framework, component, or output page and compare
   the new behavior with the documented methodology.
4. Classify the outcome using **Required Agent Behavior** below.

Code links for this chain:

{code_link_list}

## Code Change Triggers

{code_triggers}

## Output Change Triggers

{output_triggers}

## Report Change Triggers

{report_triggers}

## Reference Document Change Triggers

{ref_triggers}

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

{_bullets(impacted, "None.")}
"""
    return _page(fm, body)


# --- Metric -----------------------------------------------------------------------


def render_metric_page(ctx: _Ctx, metric: MetricInput, rel: str) -> str:
    d = "metrics"
    fm = _base_fm(
        ctx,
        type_="Metric",
        title=metric.name,
        description=metric.definition or f"Registry entry scaffold for the term {metric.name}.",
        tags=_tags(ctx, "metric"),
    )
    fm["framework_refs"] = [ctx.link_from(d, ctx.framework_rel)]

    framework_link = ctx.link_from(d, ctx.framework_rel)
    definition = metric.definition or (
        "Not yet documented — recorded in the framework page's "
        f"[Known Gaps]({framework_link}#known-gaps)."
    )
    unit = f"`{metric.unit}`" if metric.unit else "Not yet documented."
    grain = f"{metric.grain}" if metric.grain else "Not yet documented."

    body = f"""\
# {metric.name}

## Definition

{definition}

## Business Meaning

Not yet documented — no business-definition evidence has been ingested.

## Calculation Logic

No formula evidence has been ingested yet. `lineage-wiki` does not invent
calculation logic — see the framework page's
[Known Gaps]({framework_link}#known-gaps).

## Unit

{unit}

## Grain

{grain}

## Source References

- Framework: [{ctx.framework_title}]({framework_link})

## Used By

- [{ctx.framework_title}]({framework_link})

## Caveats

{_scaffold_note(ctx, "metric page")}
"""
    return _page(fm, body)


# --- Chain planning entry point ------------------------------------------------------


def plan_chain_pages(cfg: ChainConfig, root: Path, now: str) -> ChainPlan:
    """Plan every non-index page for one chain, deterministically."""
    ctx = _build_ctx(cfg, root, now)
    okf = ctx.okf
    pages = [PageDraft(f"{okf}/{ctx.framework_rel}", render_framework_page(ctx))]
    for repo, rel, title in ctx.code_links:
        pages.append(PageDraft(f"{okf}/{rel}", render_code_link_page(ctx, repo, rel, title)))
    for table, rel, title in ctx.outputs:
        pages.append(PageDraft(f"{okf}/{rel}", render_output_page(ctx, table, rel, title)))
    for report, rel, title in ctx.reports:
        pages.append(PageDraft(f"{okf}/{rel}", render_report_page(ctx, report, rel, title)))
    pages.append(PageDraft(f"{okf}/{ctx.change_check_rel}", render_change_check_page(ctx)))
    for metric, rel in ctx.metrics:
        pages.append(PageDraft(f"{okf}/{rel}", render_metric_page(ctx, metric, rel)))
    return ChainPlan(pages=pages, gaps=list(ctx.gaps))


def build_context(cfg: ChainConfig, root: Path, now: str) -> _Ctx:
    """Public accessor for the planning context (used by tests and later
    milestones that render individual pages)."""
    return _build_ctx(cfg, root, now)
