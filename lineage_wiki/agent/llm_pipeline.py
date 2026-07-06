"""LLM enrichment pipeline: planner → extractor → writer → reviewer.

Runs only when a generate is started with ``--use-llm``; the deterministic
scaffold path never imports a provider. The pipeline can only rewrite
``## `` sections of pages the deterministic plan already contains — it
cannot create files, touch non-planned pages, or edit anything outside the
selected sections, so there is no free-form repo editing surface.

All model output is JSON, parsed strictly, and filtered through the
deterministic checks in ``grounding.py``. Rejected formula claims turn into
Known Gap entries; accepted conflicts land in the framework page's
``## Known Doc-vs-Code Divergences`` section. The pipeline produces page
*drafts* — the normal write path (ownership, protection, manual-section
preservation, dry-run) applies unchanged on top.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ChainConfig
from ..constants import PRESERVED_SECTIONS
from ..okf.sections import (
    append_to_section,
    cited_evidence_ids,
    replace_section,
    split_sections,
)
from ..okf.templates import RAW_DOC_EXTRACTION_GAP, ChainPlan, bq_cross_check_gap
from ..providers import LLMProvider
from .grounding import Claim, Conflict, GroundingContext
from .prompts import (
    PromptSet,
    extractor_prompt,
    load_prompts,
    planner_prompt,
    reviewer_prompt,
    writer_prompt,
)


class LLMPipelineError(Exception):
    """A pipeline stage failed (bad JSON, provider error surfaced, …)."""


# Sections the pipeline must never write: verify-bq / divergence sections
# are owned by their own flows, and Known Gaps is computed deterministically.
_FORBIDDEN_SECTIONS = set(PRESERVED_SECTIONS) | {"Known Gaps"}

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse_json(stage: str, text: str) -> dict:
    cleaned = _FENCE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMPipelineError(
            f"{stage} stage did not return valid JSON: {exc}"
        ) from None
    if not isinstance(data, dict):
        raise LLMPipelineError(f"{stage} stage must return a JSON object")
    return data


def _headings(text: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"(?m)^## (.+?)\s*$", text)}


def _strip_gap_bullets(content: str, texts: list[str]) -> str:
    """Remove deterministic Known Gaps bullets this run actually resolved.

    Operates on the already-rendered page text (the deterministic scaffold
    has no memory of what the LLM pass accomplishes), matching by exact
    bullet text so it never touches gaps it didn't specifically resolve.
    """
    if not texts:
        return content
    _, sections = split_sections(content)
    block = next((b for h, b in sections if h == "Known Gaps"), None)
    if block is None:
        return content
    heading_line, *body_lines = block.splitlines()
    targets = {f"- {t}" for t in texts}
    kept = [line for line in body_lines if line.strip() not in targets]
    if kept == body_lines:
        return content
    body = "\n".join(kept).strip() or "None recorded."
    return replace_section(content, "Known Gaps", body)


@dataclass
class PageJob:
    rel_path: str
    sections: list[str]


@dataclass
class EnrichmentResult:
    drafts: dict[str, str] = field(default_factory=dict)  # rel -> new content
    sections_written: dict[str, list[str]] = field(default_factory=dict)
    gaps_added: list[str] = field(default_factory=list)
    gaps_resolved: list[str] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    divergence_evidence_ids: list[str] = field(default_factory=list)
    # evidence ids actually cited in a *written and accepted* section body,
    # keyed by "<rel_path>::<heading>" — used to gate gap resolution on
    # published citations, not merely on accepted-but-unwritten claims.
    section_evidence_ids: dict[str, list[str]] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)  # human-readable reasons
    reviewer_issues: list[str] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)
    transcript: dict = field(default_factory=dict)


def _complete(
    provider: LLMProvider, prompts: PromptSet, stage: str, prompt: str, temperature: float
) -> str:
    response = provider.complete(
        stage=stage, system=prompts.system, prompt=prompt, temperature=temperature
    )
    return response.text


def _run_planner(
    provider: LLMProvider,
    prompts: PromptSet,
    cfg: ChainConfig,
    plan: ChainPlan,
    temperature: float,
) -> list[PageJob]:
    drafts = {d.rel_path: d.content for d in plan.pages}
    items = plan.bundle.all_items()
    raw = _complete(
        provider,
        prompts,
        "page_planner",
        planner_prompt(prompts, cfg, sorted(drafts), items),
        temperature,
    )
    data = _parse_json("page_planner", raw)
    jobs: list[PageJob] = []
    for entry in data.get("pages", []):
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("rel_path", ""))
        if rel not in drafts:
            continue  # the model may not invent pages
        available = _headings(drafts[rel]) - _FORBIDDEN_SECTIONS
        sections = [
            str(s) for s in entry.get("sections", []) if str(s) in available
        ]
        if sections:
            jobs.append(PageJob(rel_path=rel, sections=sections))
    return jobs


def _run_extractor(
    provider: LLMProvider,
    prompts: PromptSet,
    plan: ChainPlan,
    ctx: GroundingContext,
    result: EnrichmentResult,
    temperature: float,
) -> list[Claim]:
    items = plan.bundle.all_items()
    raw = _complete(
        provider, prompts, "extractor", extractor_prompt(prompts, items), temperature
    )
    data = _parse_json("extractor", raw)

    accepted: list[Claim] = []
    for entry in data.get("claims", []):
        if not isinstance(entry, dict):
            continue
        claim = Claim(
            id=str(entry.get("id", "")) or f"c{len(accepted) + 1}",
            kind=str(entry.get("kind", "fact")),
            text=str(entry.get("text", "")),
            evidence_ids=[str(e) for e in entry.get("evidence_ids", [])],
            quote=str(entry.get("quote", "")),
        )
        decision = ctx.check_claim(claim)
        if decision.accepted:
            accepted.append(claim)
            continue
        result.rejected.append(f"claim {claim.id} ({claim.kind}): {decision.reason}")
        if claim.kind == "formula":
            result.gaps_added.append(
                f"A proposed formula ({claim.text.strip()}) was rejected — "
                f"{decision.reason}; it stays a Known Gap instead of being "
                "published."
            )

    for entry in data.get("conflicts", []):
        if not isinstance(entry, dict):
            continue
        conflict = Conflict(
            topic=str(entry.get("topic", "")),
            detail=str(entry.get("detail", "")),
            evidence_ids=[str(e) for e in entry.get("evidence_ids", [])],
            quotes=[str(q) for q in entry.get("quotes", [])],
        )
        decision = ctx.check_conflict(conflict)
        if decision.accepted:
            cited = ", ".join(f"`{e}`" for e in conflict.evidence_ids)
            result.divergences.append(
                f"- **{conflict.topic.strip()}** — {conflict.detail.strip()} "
                f"(evidence: {cited})"
            )
            result.divergence_evidence_ids.extend(conflict.evidence_ids)
        else:
            result.rejected.append(f"conflict {conflict.topic!r}: {decision.reason}")
    return accepted


def _run_writer_and_reviewer(
    provider: LLMProvider,
    prompts: PromptSet,
    job: PageJob,
    draft: str,
    claims: list[Claim],
    ctx: GroundingContext,
    result: EnrichmentResult,
    temperature: float,
) -> dict[str, str]:
    """Returns accepted {heading: body} for one page."""
    claims_payload = [c.payload() for c in claims]
    raw = _complete(
        provider,
        prompts,
        "writer",
        writer_prompt(prompts, job.rel_path, draft, job.sections, claims_payload),
        temperature,
    )
    data = _parse_json("writer", raw)

    proposed: dict[str, str] = {}
    for entry in data.get("sections", []):
        if not isinstance(entry, dict):
            continue
        heading = str(entry.get("heading", ""))
        body = str(entry.get("body", ""))
        if heading not in job.sections:
            result.rejected.append(
                f"{job.rel_path}: section {heading!r} was not in the allowed list"
            )
            continue
        decision = ctx.check_section_body(body, claims)
        if not decision.accepted:
            result.rejected.append(
                f"{job.rel_path}: section {heading!r} rejected — {decision.reason}"
            )
            continue
        proposed[heading] = body

    if not proposed:
        return {}

    sections_payload = [
        {"heading": h, "body": b} for h, b in sorted(proposed.items())
    ]
    raw = _complete(
        provider,
        prompts,
        "reviewer",
        reviewer_prompt(prompts, job.rel_path, sections_payload, claims_payload),
        temperature,
    )
    review = _parse_json("reviewer", raw)
    for issue in review.get("issues", []):
        result.reviewer_issues.append(f"{job.rel_path}: {issue}")
    verdict = str(review.get("verdict", "revise")).lower()
    rejected_sections = {str(s) for s in review.get("rejected_sections", [])}
    if verdict != "approve" and not rejected_sections:
        rejected_sections = set(proposed)  # revise with no detail drops all
    for heading in rejected_sections & set(proposed):
        result.rejected.append(
            f"{job.rel_path}: section {heading!r} rejected by reviewer"
        )
        proposed.pop(heading)
    return proposed


def run_llm_enrichment(
    cfg: ChainConfig,
    root: Path,
    plan: ChainPlan,
    provider: LLMProvider,
) -> EnrichmentResult:
    """Enrich the deterministic drafts with grounded, cited section content."""
    prompts = load_prompts(root)
    temperature = cfg.model.temperature
    ctx = GroundingContext(plan.bundle)
    result = EnrichmentResult()

    jobs = _run_planner(provider, prompts, cfg, plan, temperature)
    claims = _run_extractor(provider, prompts, plan, ctx, result, temperature)

    drafts = {d.rel_path: d.content for d in plan.pages}
    for job in jobs:
        if not claims:
            break  # nothing grounded to write from
        accepted = _run_writer_and_reviewer(
            provider, prompts, job, drafts[job.rel_path], claims, ctx, result,
            temperature,
        )
        if not accepted:
            continue
        content = drafts[job.rel_path]
        for heading, body in accepted.items():
            content = replace_section(content, heading, body)
            result.section_evidence_ids[f"{job.rel_path}::{heading}"] = (
                cited_evidence_ids(body)
            )
        drafts[job.rel_path] = content
        result.drafts[job.rel_path] = content
        result.sections_written[job.rel_path] = sorted(accepted)

    okf = cfg.generation.output_dir
    framework_rel = f"{okf}/frameworks/{cfg.chain.slug}.md"
    if framework_rel in drafts:
        content = drafts[framework_rel]

        resolved: list[str] = []
        core_formula_ids = result.section_evidence_ids.get(
            f"{framework_rel}::Core Formula", []
        )
        if any(ctx.types.get(e) == "raw_doc" for e in core_formula_ids):
            resolved.append(RAW_DOC_EXTRACTION_GAP)

        # Only tables a *published* citation (a written section, not merely
        # an accepted-but-unwritten claim) or a recorded divergence actually
        # cross-checked — an accepted claim that never made it into a
        # section must not silently resolve the gap.
        written_ids = [e for ids in result.section_evidence_ids.values() for e in ids]
        bq_tables_addressed = {
            e.split(":", 1)[1]
            for e in written_ids + result.divergence_evidence_ids
            if e.startswith("bq-schema:")
        }
        resolved.extend(bq_cross_check_gap(t) for t in sorted(bq_tables_addressed))
        if resolved:
            new_content = _strip_gap_bullets(content, resolved)
            if new_content != content:
                result.gaps_resolved.extend(resolved)
                content = new_content

        if result.gaps_added and cfg.generation.mark_unknowns_as_gaps:
            # The (llm) prefix marks these bullets so later deterministic
            # rewrites carry them over (see sections.LLM_GAP_PREFIX).
            content = append_to_section(
                content, "Known Gaps", [f"- (llm) {gap}" for gap in result.gaps_added]
            )
        if result.divergences:
            content = replace_section(
                content,
                "Known Doc-vs-Code Divergences",
                "Recorded by the LLM extraction run (each entry cites its "
                "evidence):\n\n" + "\n".join(result.divergences),
            )
        if content != drafts[framework_rel]:
            result.drafts[framework_rel] = content

    n_accepted = len(claims)
    result.summary = [
        f"claims accepted: {n_accepted}, rejected: "
        f"{sum(1 for r in result.rejected if r.startswith('claim '))}",
        f"pages enriched: {len(result.sections_written)}"
        + (
            " (" + ", ".join(sorted(result.sections_written)) + ")"
            if result.sections_written
            else ""
        ),
        f"divergences recorded: {len(result.divergences)}",
        f"gaps added from rejected formulas: {len(result.gaps_added)}",
        f"gaps resolved: {len(result.gaps_resolved)}",
    ]
    if result.reviewer_issues:
        result.summary.append(
            f"reviewer issues: {len(result.reviewer_issues)}"
        )

    result.transcript = {
        "provider": provider.name,
        "jobs": [{"rel_path": j.rel_path, "sections": j.sections} for j in jobs],
        "claims_accepted": [c.payload() for c in claims],
        "rejected": list(result.rejected),
        "reviewer_issues": list(result.reviewer_issues),
        "divergences": list(result.divergences),
        "gaps_added": list(result.gaps_added),
        "gaps_resolved": list(result.gaps_resolved),
        "sections_written": dict(result.sections_written),
    }
    return result
