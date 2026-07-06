"""Section-level page merging and diff summaries.

Tool-owned pages are rewritten from templates, but two kinds of content must
survive a rewrite:

- preserved sections (see ``PRESERVED_SECTIONS``) — filled by ``verify-bq``
  or reviewed by humans, never by the scaffold templates;
- manual sections — any ``## `` section a human added that the template
  does not render.

``merge_manual_sections`` folds both from the existing page into the new
draft so generate/update runs never destroy them.
"""

from __future__ import annotations

import difflib
import re

from ..constants import PRESERVED_SECTIONS

_SECTION_HEAD = re.compile(r"(?m)^## (.+?)\s*$")


def split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a page into its prelude (frontmatter, H1, intro) and a list of
    ``(heading, block)`` pairs, where each block starts at its ``## `` line
    and runs to the next section heading (or end of text)."""
    matches = list(_SECTION_HEAD.finditer(text))
    if not matches:
        return text, []
    prelude = text[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((match.group(1), text[match.start() : end]))
    return prelude, sections


def _joined(parts: list[str]) -> str:
    normalized = [p.rstrip("\n") for p in parts if p.strip()]
    return "\n\n".join(normalized) + "\n"


# Marker that LLM-written bodies must carry (one per citation); its
# presence identifies evidence-written content that scaffold rewrites keep.
CITATION_MARK = "[src:"

_SRC_ID = re.compile(r"\[src:\s*([^\]\s]+)\s*\]")


def cited_evidence_ids(text: str) -> list[str]:
    """Evidence ids cited by ``[src: <id>]`` markers, in order, deduped."""
    seen: list[str] = []
    for match in _SRC_ID.finditer(text):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def _stale_cited(block: str, stale_evidence: frozenset[str] | set[str]) -> list[str]:
    """Cited ids in ``block`` matching the stale set. Entries ending in
    ``:`` are prefixes (e.g. ``local-repo:gold-pnl:``), others exact ids."""
    prefixes = tuple(e for e in stale_evidence if e.endswith(":"))
    exact = {e for e in stale_evidence if not e.endswith(":")}
    return [
        cited
        for cited in cited_evidence_ids(block)
        if cited in exact or cited.startswith(prefixes or ("\0",))
    ]

# Prefix for Known Gaps bullets added by the LLM pipeline; deterministic
# rewrites carry them over instead of reverting them to scaffold gaps.
LLM_GAP_PREFIX = "- (llm)"


def _merge_gap_block(existing_block: str, draft_block: str) -> str:
    carried = [
        line
        for line in existing_block.splitlines()
        if line.startswith(LLM_GAP_PREFIX) and line not in draft_block
    ]
    if not carried:
        return draft_block
    return draft_block.rstrip("\n") + "\n" + "\n".join(carried) + "\n"


def _invalidation_note(stale_ids: list[str]) -> str:
    cited = ", ".join(f"`{e}`" for e in stale_ids)
    return (
        f"> (llm) Previous LLM-written content here was invalidated because "
        f"its cited evidence changed ({cited}); re-run "
        f"`lineage-wiki generate --use-llm` to re-extract it."
    )


def merge_manual_sections(
    existing: str,
    draft: str,
    preserved: tuple[str, ...] = PRESERVED_SECTIONS,
    force: tuple[str, ...] = (),
    stale_evidence: frozenset[str] | set[str] = frozenset(),
    invalidated: list[tuple[str, list[str]]] | None = None,
) -> str:
    """Render ``draft`` while keeping manual content from ``existing``:

    - a section named in ``force`` always takes the draft body (the current
      run intentionally rewrote it, e.g. the LLM pipeline);
    - a section named in ``preserved`` keeps its existing body;
    - a section whose existing body carries ``[src: …]`` citations is
      evidence-written (LLM run) and survives a scaffold rewrite — *unless*
      one of its cited ids is in ``stale_evidence`` (that evidence changed
      this run), in which case the section reverts to the draft body plus a
      visible invalidation note, and ``(heading, stale_ids)`` is appended to
      ``invalidated`` when provided;
    - ``Known Gaps`` keeps ``- (llm)`` bullets from previous LLM runs;
    - sections present only in ``existing`` are retained (appended after
      the draft's sections, in their original order).
    """
    _, existing_sections = split_sections(existing)
    draft_prelude, draft_sections = split_sections(draft)

    existing_by_heading: dict[str, str] = {}
    for heading, block in existing_sections:
        existing_by_heading.setdefault(heading, block)
    draft_headings = {heading for heading, _ in draft_sections}

    parts = [draft_prelude]
    for heading, block in draft_sections:
        old = existing_by_heading.get(heading)
        if heading in force or old is None:
            parts.append(block)
        elif heading in preserved:
            parts.append(old)
        elif heading == "Known Gaps":
            parts.append(_merge_gap_block(old, block))
        elif CITATION_MARK in old and CITATION_MARK not in block:
            stale_ids = _stale_cited(old, stale_evidence)
            if stale_ids:
                # The evidence this content cites changed: stale LLM prose
                # must not survive under a valid-looking citation.
                parts.append(
                    block.rstrip("\n") + "\n\n" + _invalidation_note(stale_ids) + "\n"
                )
                if invalidated is not None:
                    invalidated.append((heading, stale_ids))
            else:
                parts.append(old)
        else:
            parts.append(block)
    for heading, block in existing_sections:
        if heading not in draft_headings:
            parts.append(block)
    return _joined(parts)


def replace_section(text: str, heading: str, body: str) -> str:
    """Replace the body of ``## heading`` (keeping the heading line).
    Returns ``text`` unchanged when the section is absent."""
    prelude, sections = split_sections(text)
    if all(h != heading for h, _ in sections):
        return text
    parts = [prelude]
    for h, block in sections:
        if h == heading:
            parts.append(f"## {heading}\n\n{body.strip()}\n")
        else:
            parts.append(block)
    return _joined(parts)


def append_to_section(text: str, heading: str, lines: list[str]) -> str:
    """Append lines to the end of ``## heading``'s body. Returns ``text``
    unchanged when the section is absent or ``lines`` is empty."""
    if not lines:
        return text
    prelude, sections = split_sections(text)
    if all(h != heading for h, _ in sections):
        return text
    parts = [prelude]
    for h, block in sections:
        if h == heading:
            parts.append(block.rstrip("\n") + "\n" + "\n".join(lines) + "\n")
        else:
            parts.append(block)
    return _joined(parts)


def _line_sections(lines: list[str]) -> list[str | None]:
    current: str | None = None
    out: list[str | None] = []
    for line in lines:
        match = _SECTION_HEAD.match(line)
        if match:
            current = match.group(1)
        out.append(current)
    return out


def diff_summary(old: str, new: str) -> str:
    """One-line human summary of a page rewrite: net line churn plus which
    ``## `` sections the changes fall under."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    old_secs, new_secs = _line_sections(old_lines), _line_sections(new_lines)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    added = removed = 0
    sections: list[str] = []

    def note(section: str | None) -> None:
        label = section or "(preamble)"
        if label not in sections:
            sections.append(label)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed += i2 - i1
        added += j2 - j1
        for i in range(i1, i2):
            note(old_secs[i])
        for j in range(j1, j2):
            note(new_secs[j])
    return f"+{added} -{removed} line(s); sections: {', '.join(sections) or 'none'}"
