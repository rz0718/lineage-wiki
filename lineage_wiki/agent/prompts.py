"""Prompt assembly for the four LLM pipeline stages.

Base prompt texts come from the stubs `lineage-wiki init` writes under
``.lineage-wiki/prompts/`` (editable per target repo); when a stub file is
absent the built-in default from ``constants.PROMPT_STUBS`` is used. Every
stage instructs the model to answer with a single JSON object — anything
else fails the stage cleanly.

Evidence content is embedded verbatim (truncated deterministically for very
large documents). Prompts never include secrets and never ask for SQL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import ChainConfig
from ..constants import MANIFEST_DIR, PROMPT_STUBS
from ..ingestion.evidence import EvidenceItem

# Deterministic truncation bound per evidence item, in characters.
MAX_EVIDENCE_CHARS = 12_000

_STAGE_FILES = {
    "system": "system.md",
    "page_planner": "page_planner.md",
    "extractor": "extractor.md",
    "writer": "writer.md",
    "reviewer": "reviewer.md",
}


@dataclass
class PromptSet:
    system: str
    page_planner: str
    extractor: str
    writer: str
    reviewer: str


def load_prompts(root: Path) -> PromptSet:
    """Load prompt texts, preferring the target repo's editable stubs."""
    texts: dict[str, str] = {}
    for key, filename in _STAGE_FILES.items():
        override = root / MANIFEST_DIR / "prompts" / filename
        if override.exists():
            texts[key] = override.read_text(encoding="utf-8")
        else:
            texts[key] = PROMPT_STUBS[filename]
    return PromptSet(**texts)


def _truncated(content: str) -> str:
    if len(content) <= MAX_EVIDENCE_CHARS:
        return content
    head = content[: MAX_EVIDENCE_CHARS // 2]
    tail = content[-MAX_EVIDENCE_CHARS // 2 :]
    return f"{head}\n\n[... truncated by lineage-wiki ...]\n\n{tail}"


def _evidence_block(items: list[EvidenceItem], *, with_content: bool) -> str:
    parts = []
    for item in items:
        header = f"evidence id: {item.id}\ntype: {item.source_type}\ntitle: {item.title}"
        if with_content:
            parts.append(f"{header}\ncontent:\n{_truncated(item.content)}")
        else:
            parts.append(header)
    return "\n\n---\n\n".join(parts) if parts else "(no evidence items)"


_JSON_ONLY = (
    "Reply with a single JSON object and nothing else — no prose, no "
    "Markdown fences."
)


def planner_prompt(
    prompts: PromptSet, cfg: ChainConfig, page_rels: list[str], items: list[EvidenceItem]
) -> str:
    schema = {
        "pages": [
            {
                "rel_path": "<one of the plannable pages>",
                "sections": ["<## section headings to enrich>"],
                "evidence_ids": ["<relevant evidence ids>"],
            }
        ]
    }
    return (
        f"{prompts.page_planner}\n\n"
        f"Chain: {cfg.chain.id} — {cfg.chain.name}\n"
        f"Description: {cfg.chain.description}\n\n"
        "Plannable pages (you may only select from this list):\n"
        + "\n".join(f"- {rel}" for rel in page_rels)
        + "\n\nEvidence catalog:\n\n"
        + _evidence_block(items, with_content=False)
        + f"\n\nJSON schema:\n{json.dumps(schema, indent=2)}\n\n{_JSON_ONLY}"
    )


def extractor_prompt(prompts: PromptSet, items: list[EvidenceItem]) -> str:
    schema = {
        "claims": [
            {
                "id": "<short unique id, e.g. c1>",
                "kind": "formula | definition | column | code_path | mapping | fact",
                "text": "<the claim, one sentence or formula>",
                "evidence_ids": ["<ids of evidence items supporting it>"],
                "quote": "<verbatim supporting excerpt from the evidence>",
            }
        ],
        "conflicts": [
            {
                "topic": "<what disagrees>",
                "detail": "<doc says X, code/schema says Y>",
                "evidence_ids": ["<ids on both sides>"],
                "quotes": ["<verbatim supporting excerpts from the cited evidence>"],
            }
        ],
    }
    return (
        f"{prompts.extractor}\n\nEvidence items:\n\n"
        + _evidence_block(items, with_content=True)
        + f"\n\nJSON schema:\n{json.dumps(schema, indent=2)}\n\n{_JSON_ONLY}"
    )


def writer_prompt(
    prompts: PromptSet,
    rel_path: str,
    draft: str,
    sections: list[str],
    claims_payload: list[dict],
) -> str:
    schema = {
        "sections": [
            {
                "heading": "<one of the allowed section headings>",
                "body": "<Markdown body; cite claims with [src: <evidence-id>]>",
            }
        ]
    }
    return (
        f"{prompts.writer}\n\nPage: {rel_path}\n"
        "Allowed sections (write only these):\n"
        + "\n".join(f"- {s}" for s in sections)
        + "\n\nEvery statement must be supported by one of the accepted "
        "claims below, and every section body must cite its evidence ids "
        "with [src: <evidence-id>] markers. Do not write SQL. If the "
        "claims do not cover a section, omit that section.\n\n"
        f"Accepted claims:\n{json.dumps(claims_payload, indent=2)}\n\n"
        f"Current page draft:\n\n{draft}\n\n"
        f"JSON schema:\n{json.dumps(schema, indent=2)}\n\n{_JSON_ONLY}"
    )


def reviewer_prompt(
    prompts: PromptSet,
    rel_path: str,
    sections_payload: list[dict],
    claims_payload: list[dict],
) -> str:
    schema = {
        "verdict": "approve | revise",
        "rejected_sections": ["<headings that must not be published>"],
        "issues": ["<one line per problem found>"],
    }
    return (
        f"{prompts.reviewer}\n\nPage: {rel_path}\n\n"
        f"Proposed sections:\n{json.dumps(sections_payload, indent=2)}\n\n"
        f"Accepted claims they must be grounded in:\n"
        f"{json.dumps(claims_payload, indent=2)}\n\n"
        f"JSON schema:\n{json.dumps(schema, indent=2)}\n\n{_JSON_ONLY}"
    )
