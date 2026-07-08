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
    prompts: PromptSet,
    cfg: ChainConfig,
    pages: dict[str, list[str]],
    items: list[EvidenceItem],
) -> str:
    schema = {
        "pages": [
            {
                "rel_path": "<one of the plannable pages>",
                "sections": ["<a section heading exactly as listed, without '## '>"],
                "evidence_ids": ["<relevant evidence ids>"],
            }
        ]
    }
    page_lines: list[str] = []
    for rel in sorted(pages):
        page_lines.append(f"- {rel}")
        page_lines.extend(f"  - {heading}" for heading in pages[rel])
    return (
        f"{prompts.page_planner}\n\n"
        f"Chain: {cfg.chain.id} — {cfg.chain.name}\n"
        f"Description: {cfg.chain.description}\n\n"
        "Plannable pages, each with the section headings you may enrich "
        "(you may only select from this list; copy headings verbatim):\n"
        + "\n".join(page_lines)
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
        f"{prompts.extractor}\n\n"
        "`text` must be a clean, self-contained sentence (or a short "
        "formula in backticks) that can be pasted into documentation "
        "prose as-is — never a raw table row, pipe-delimited fragment, "
        "multi-line code, docstring, or table-of-contents entry. Raw "
        "evidence wording belongs in `quote`, which must be verbatim.\n\n"
        "Evidence items:\n\n"
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
                "body": "<Markdown body; cite sources with [src: <evidence-id>]>",
            }
        ]
    }
    return (
        f"{prompts.writer}\n\nPage: {rel_path}\n"
        "Allowed sections (write only these):\n"
        + "\n".join(f"- {s}" for s in sections)
        + "\n\nEvery statement must be supported by one of the accepted "
        "claims below, and every section body must cite its sources with "
        "[src: <id>] markers, where <id> is a value from the supporting "
        "claim's `evidence_ids` (never the claim's own `id`). For each "
        "source you cite, the body must include the supporting claim's "
        "`text` verbatim — copy it exactly; do not paraphrase, reword, or "
        "break it up with Markdown emphasis, or the section will be "
        "rejected by the deterministic grounding check. Prefer `text` "
        "over `quote`: fall back to the `quote` only when the claim has "
        "no usable `text`. Write readable prose — never paste raw "
        "evidence formatting into a body: no pipe-delimited table "
        "fragments, no multi-line code or docstrings, no anchor or "
        "table-of-contents links copied from evidence (they do not "
        "resolve on these pages). Do not repeat the same claim in more "
        "than one section of the page. Do not write SQL. If the claims "
        "do not cover a section, omit that section.\n\n"
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
        f"{prompts.reviewer}\n\n"
        "Judge ONLY the proposed section bodies against the accepted "
        "claims: grounding, citation correctness, and internal "
        "consistency. Do not raise issues about link resolvability, "
        "index membership, required sections, or anything outside the "
        "sections shown — deterministic validation covers those "
        "elsewhere, and you cannot see the rest of the page. Reject a "
        "section only for a grounding problem (an unsupported statement, "
        "a wrong or missing citation, or content that contradicts the "
        "claims), not for style.\n\n"
        f"Page: {rel_path}\n\n"
        f"Proposed sections:\n{json.dumps(sections_payload, indent=2)}\n\n"
        f"Accepted claims they must be grounded in:\n"
        f"{json.dumps(claims_payload, indent=2)}\n\n"
        f"JSON schema:\n{json.dumps(schema, indent=2)}\n\n{_JSON_ONLY}"
    )
