"""Index generation for the OKF tree.

Builds all eight index files (`okf/index.md` plus one per directory) by
scanning page frontmatter, grouping pages by the framework they reference.
Index content is derived entirely from the pages on disk, so indexes stay
correct across chains and across human-added pages.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from ..constants import DIR_TO_TYPE, GENERATED_MARKER, OKF_SUBDIRS
from ..util import humanize
from .schemas import parse_page, render_frontmatter
from .templates import PageDraft


@dataclass
class PageInfo:
    """Frontmatter summary of one non-index page, rel path within okf/."""

    rel: str
    type: str
    title: str
    description: str
    status: str
    framework_refs: list[str] = field(default_factory=list)
    code_refs: list[str] = field(default_factory=list)
    output_refs: list[str] = field(default_factory=list)


def _resolved_refs(fm: dict, key: str, page_dir: str) -> list[str]:
    """Page paths referenced under ``key``, resolved relative to the page.
    Entries may be plain rel-path strings or the structured dict form other
    templates emit (e.g. ``{system, table, output: ../outputs/x.md}``)."""
    refs = []
    raw_refs = fm.get(key) or []
    if isinstance(raw_refs, list):
        for ref in raw_refs:
            values = ref.values() if isinstance(ref, dict) else [ref]
            for value in values:
                if isinstance(value, str) and value.endswith(".md"):
                    refs.append(posixpath.normpath(posixpath.join(page_dir, value)))
    return refs


def scan_pages(okf_dir: Path) -> list[PageInfo]:
    """Parse every non-index page under the okf dir into PageInfo entries."""
    pages: list[PageInfo] = []
    if not okf_dir.exists():
        return pages
    for path in sorted(okf_dir.rglob("*.md")):
        if path.name == "index.md":
            continue
        rel = path.relative_to(okf_dir).as_posix()
        parsed = parse_page(path.read_text(encoding="utf-8"))
        fm = parsed.frontmatter
        if not fm or not isinstance(fm.get("type"), str):
            continue  # the validator reports these separately
        page_dir = posixpath.dirname(rel)
        pages.append(
            PageInfo(
                rel=rel,
                type=fm["type"],
                title=" ".join(str(fm.get("title") or humanize(path.stem)).split()),
                description=" ".join(str(fm.get("description") or "").split()),
                status=str(fm.get("status") or "draft"),
                framework_refs=_resolved_refs(fm, "framework_refs", page_dir),
                code_refs=_resolved_refs(fm, "code_refs", page_dir),
                output_refs=_resolved_refs(fm, "output_refs", page_dir),
            )
        )
    return pages


def _cell(text: str) -> str:
    """Escape a value for use inside a Markdown table cell."""
    return text.replace("|", "\\|")


# --- Helpers -----------------------------------------------------------------


def _index_fm(title: str, description: str, tag: str, now: str) -> dict:
    return {
        "type": "Index",
        "title": title,
        "description": description,
        "status": "draft",
        "tags": ["okf", tag, "index"],
        "timestamp": now,
    }


def _by_type(pages: list[PageInfo], type_: str) -> list[PageInfo]:
    return sorted((p for p in pages if p.type == type_), key=lambda p: p.title)


def _grouped(pages: list[PageInfo], frameworks: list[PageInfo]) -> list[tuple[PageInfo | None, list[PageInfo]]]:
    """Group pages under the first framework they reference."""
    fw_by_rel = {fw.rel: fw for fw in frameworks}
    groups: dict[str | None, list[PageInfo]] = {}
    for page in pages:
        key = next((r for r in page.framework_refs if r in fw_by_rel), None)
        groups.setdefault(key, []).append(page)
    ordered: list[tuple[PageInfo | None, list[PageInfo]]] = []
    for fw in frameworks:
        if fw.rel in groups:
            ordered.append((fw, groups.pop(fw.rel)))
    leftovers = [page for members in groups.values() for page in members]
    if leftovers:
        ordered.append((None, sorted(leftovers, key=lambda p: p.title)))
    return ordered


def _link_from_dir(src_dir: str, target_rel: str) -> str:
    return posixpath.relpath(target_rel, start=src_dir)


def _page_text(fm: dict, body: str) -> str:
    # The marker is how later runs recognize an index they own; existing
    # indexes without it are hand-written and stay protected.
    return render_frontmatter(fm) + "\n" + GENERATED_MARKER + "\n\n" + body.strip() + "\n"


def _grouped_tables(
    pages: list[PageInfo],
    frameworks: list[PageInfo],
    src_dir: str,
    noun: str,
    empty: str,
) -> str:
    if not pages:
        return empty
    blocks = []
    for fw, members in _grouped(sorted(pages, key=lambda p: p.title), frameworks):
        heading = f"{fw.title.removesuffix(' Framework')} {noun}" if fw else f"Other {noun}"
        rows = "\n".join(
            f"| [{_cell(p.title)}]({_link_from_dir(src_dir, p.rel)}) | {_cell(p.description)} |"
            for p in members
        )
        blocks.append(f"## {heading}\n\n| {noun[:-1] if noun.endswith('s') else noun} | Description |\n|---|---|\n{rows}")
    return "\n\n".join(blocks)


# --- Directory indexes ---------------------------------------------------------

_DIR_INDEX_META = {
    "frameworks": (
        "OKF Frameworks",
        "Index of methodology bundles — one page per vertical.",
        "Framework pages document end-to-end computation methodologies. Each "
        "framework page carries `implementation_refs` pointing to the code "
        "repo and `output_refs` pointing to BigQuery tables, making the "
        "chain traceable.",
        "Frameworks",
    ),
    "components": (
        "OKF Components",
        "Index of formula and business-rule pages for individual framework components.",
        'Component pages answer "How is this component calculated?" and carry '
        "a factors table documenting what drives each component and where the "
        "code lives.",
        "Components",
    ),
    "outputs": (
        "OKF Outputs",
        "Index of generated BigQuery tables and views produced by OKF frameworks.",
        "Output pages document generated BigQuery tables and views: table "
        "identity, grain, columns, component links, and the reports that "
        "display them.",
        "Outputs",
    ),
    "report-templates": (
        "OKF Report Templates",
        "Index of report and dashboard template pages — interpretation guides for each published report.",
        'Report template pages answer "Where does the user see the number, '
        'and how should it be interpreted?" Each page documents line items, '
        "source columns, and known caveats.",
        "Reports",
    ),
    "code-links": (
        "OKF Code Links",
        "Index of implementation pointers — repo identity, file paths, symbols, and runtime assumptions.",
        "Code link pages are load-bearing — they carry repo identity so a "
        "diff in an external repo can be resolved back to the wiki page it "
        "backs. Each entry records repo + path + symbol + ref.",
        "Code Links",
    ),
    "change-checks": (
        "OKF Change Checks",
        "Index of agent review rules triggered by code, output, report, or reference document changes.",
        "Change check pages tell an agent what to inspect when code, outputs, "
        "reports, or reference documents change: read the diff, resolve the "
        "changed symbol through `code-links/`, and land on the affected page.",
        "Change Checks",
    ),
}


def _render_dir_index(subdir: str, pages: list[PageInfo], frameworks: list[PageInfo], now: str) -> str:
    title, description, intro, noun = _DIR_INDEX_META[subdir]
    type_ = DIR_TO_TYPE[subdir]
    members = _by_type(pages, type_)

    if subdir == "frameworks":
        if members:
            rows = "\n".join(
                f"| [{_cell(p.title)}]({_link_from_dir(subdir, p.rel)}) | {_cell(p.description)} | {p.status} |"
                for p in members
            )
            table = f"| Framework | Description | Status |\n|---|---|---|\n{rows}"
        else:
            table = "No framework pages yet."
        body = f"# {title}\n\n{intro}\n\n{table}"
    else:
        table = _grouped_tables(
            members, frameworks, subdir, noun, f"No {noun.lower()} pages yet."
        )
        body = f"# {title}\n\n{intro}\n\n{table}"

    return _page_text(_index_fm(title, description, subdir, now), body)


# --- Metrics registry -----------------------------------------------------------


def _render_metrics_index(pages: list[PageInfo], frameworks: list[PageInfo], now: str) -> str:
    metrics = _by_type(pages, "Metric")
    components = _by_type(pages, "Component")

    if metrics:
        rows = "\n".join(
            f"| {_cell(m.title)} | [{_cell(m.title)}]({_link_from_dir('metrics', m.rel)}) |"
            for m in metrics
        )
        standalone = f"| Term | Definition lives at |\n|---|---|\n{rows}"
    else:
        standalone = "None registered yet."

    fw_blocks = []
    for fw in frameworks:
        rows = [
            f"| {_cell(fw.title.removesuffix(' Framework'))} | Framework | "
            f"[{_cell(fw.title)}]({_link_from_dir('metrics', fw.rel)}) |"
        ]
        for comp in components:
            if fw.rel in comp.framework_refs:
                rows.append(
                    f"| {_cell(comp.title)} | Component | "
                    f"[{_cell(comp.title)}]({_link_from_dir('metrics', comp.rel)}) |"
                )
        fw_blocks.append(
            f"## {fw.title.removesuffix(' Framework')} Terms\n\n"
            "| Term | Type | Definition lives at |\n|---|---|---|\n" + "\n".join(rows)
        )
    fw_section = "\n\n".join(fw_blocks) if fw_blocks else "## Framework Terms\n\nNone registered yet."

    body = f"""\
# OKF Metrics Registry

This registry is the front door for any term queried by name. Each entry is
one line pointing to where the authoritative definition lives — it does not
restate the definition.

- A standalone quantity (defined without a parent framework) → its
  `metrics/` page.
- A framework intermediate that people also ask about by name → its
  `components/` page.

## Standalone Metrics

{standalone}

{fw_section}
"""
    fm = _index_fm(
        "OKF Metrics Registry",
        "Registry keyed by term name. Front door for any quantity queried by "
        "name — points to where its definition lives.",
        "metrics",
        now,
    )
    return _page_text(fm, body)


# --- Root index ---------------------------------------------------------------


def _render_root_index(pages: list[PageInfo], frameworks: list[PageInfo], now: str) -> str:
    def section_list(fw: PageInfo, type_: str) -> str:
        members = [
            p for p in _by_type(pages, type_) if fw.rel in p.framework_refs
        ]
        if not members:
            return "- None yet."
        return "\n".join(f"- [{p.title}]({p.rel})" for p in members)

    vertical_blocks = []
    for fw in frameworks:
        vertical_blocks.append(
            f"""\
## {fw.title.removesuffix(' Framework')}

**Framework:** [{fw.title}]({fw.rel})

**Components:**
{section_list(fw, 'Component')}

**Outputs:**
{section_list(fw, 'Output')}

**Reports:**
{section_list(fw, 'Report Template')}

**Implementation:**
{section_list(fw, 'Code Link')}
{section_list(fw, 'Change Check')}

**Metrics:**
{section_list(fw, 'Metric')}"""
        )
    verticals = "\n\n".join(vertical_blocks) if vertical_blocks else (
        "## Verticals\n\nNo framework pages yet — run `lineage-wiki generate "
        "--config chains/<chain>.yml` to create the first vertical."
    )

    body = f"""\
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

{verticals}

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
"""
    fm = _index_fm(
        "OKF Knowledge Bundle",
        "Entry point for the git-backed data-product knowledge base. "
        "Organized for traceability from report line items back to code and "
        "source documents.",
        "okf",
        now,
    )
    return _page_text(fm, body)


# --- Entry point -----------------------------------------------------------------


def build_all_indexes(okf_dir: Path, now: str, okf_dir_name: str = "okf") -> list[PageDraft]:
    """Render all eight index files from the pages currently on disk.

    Returned rel paths are relative to the repo root (prefixed with
    ``okf_dir_name``).
    """
    pages = scan_pages(okf_dir)
    frameworks = _by_type(pages, "Framework")
    drafts = [PageDraft(f"{okf_dir_name}/index.md", _render_root_index(pages, frameworks, now))]
    for subdir in OKF_SUBDIRS:
        if subdir == "metrics":
            content = _render_metrics_index(pages, frameworks, now)
        else:
            content = _render_dir_index(subdir, pages, frameworks, now)
        drafts.append(PageDraft(f"{okf_dir_name}/{subdir}/index.md", content))
    return drafts
