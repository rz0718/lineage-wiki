"""Deterministic update impact planning (spec section 10 impact rules).

Maps changed evidence to the OKF pages that must be considered:

| Changed evidence   | Pages to consider                                        |
|--------------------|----------------------------------------------------------|
| Code files/symbols | code-links, linked components, framework, change-checks  |
| BigQuery schema    | outputs, framework, report-templates, change-checks      |
| Raw docs           | framework, components, metrics                           |
| Report mapping     | report-templates, outputs, framework, change-checks      |
| Chain config       | every page planned for the chain                         |

The plan includes hand-written pages linked to the chain's framework (found
by scanning frontmatter refs); the deterministic writer only rewrites
tool-owned pages, but the plan is printed so humans/agents can review the
rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import ChainConfig
from ..okf.indexes import scan_pages
from ..okf.templates import PageDraft, build_context
from ..storage.manifest import SourceChanges


@dataclass
class ImpactPlan:
    """Pages to consider, repo-root-relative, with human-readable reasons."""

    pages: dict[str, list[str]] = field(default_factory=dict)

    def add(self, rel_path: str, reason: str) -> None:
        reasons = self.pages.setdefault(rel_path, [])
        if reason not in reasons:
            reasons.append(reason)

    def sorted_items(self) -> list[tuple[str, list[str]]]:
        return sorted(self.pages.items())


def build_impact_plan(
    cfg: ChainConfig,
    root: Path,
    now: str,
    changes: SourceChanges,
    planned: list[PageDraft],
) -> ImpactPlan:
    ctx = build_context(cfg, root, now)
    okf = ctx.okf
    plan = ImpactPlan()

    def add(rel_in_okf: str, reason: str) -> None:
        plan.add(f"{okf}/{rel_in_okf}", reason)

    scanned = scan_pages(root / okf)
    fw = ctx.framework_rel
    chain_components = [p for p in scanned if p.type == "Component" and fw in p.framework_refs]
    chain_metrics = [p for p in scanned if p.type == "Metric" and fw in p.framework_refs]

    if changes.config:
        for draft in planned:
            plan.add(draft.rel_path, "chain config changed")

    for path in changes.raw_docs:
        reason = f"raw doc `{path}` changed"
        add(fw, reason)
        for page in chain_components:
            add(page.rel, reason)
        for page in chain_metrics:
            add(page.rel, reason)

    for name in changes.repos:
        reason = f"repo `{name}` changed"
        code_link = next((rel for r, rel, _ in ctx.code_links if r.name == name), None)
        if code_link:
            add(code_link, reason)
            for page in chain_components:
                if code_link in page.code_refs:
                    add(page.rel, reason)
        add(fw, reason)
        add(ctx.change_check_rel, reason)

    for table in changes.bigquery:
        reason = f"bigquery table `{table}` changed"
        output = next((rel for t, rel, _ in ctx.outputs if t == table), None)
        if output:
            add(output, reason)
        add(fw, reason)
        for _, report_rel, _ in ctx.reports:
            add(report_rel, reason)
        add(ctx.change_check_rel, reason)

    for name in changes.reports:
        reason = f"report `{name}` mapping changed"
        report = next((rel for r, rel, _ in ctx.reports if r.name == name), None)
        if report:
            add(report, reason)
        for _, output_rel, _ in ctx.outputs:
            add(output_rel, reason)
        add(fw, reason)
        add(ctx.change_check_rel, reason)

    # Planned pages that do not exist on disk yet are always in scope — the
    # traceability chain is incomplete without them.
    for draft in planned:
        if not (root / draft.rel_path).exists():
            plan.add(draft.rel_path, "page missing from the OKF tree")

    return plan
