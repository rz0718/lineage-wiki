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

from ..constants import (
    GROUNDING_STATUS_MARK,
    PRESERVED_SECTIONS,
    SCAFFOLD_STATUS_MARK,
)
from .templates import (
    RAW_DOC_EXTRACTION_GAP,
    bq_cross_check_table,
    repo_cross_check_repo,
)

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
    ``:`` are prefixes (e.g. ``local-repo:example-revenue:``), others exact ids."""
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


def _mentions_evidence(text: str, evidence_id: str) -> bool:
    # Divergences cite evidence as backtick-wrapped ids ("evidence: `id`"),
    # not the `[src: id]` marker prose sections use — check both forms.
    return f"`{evidence_id}`" in text or f"[src: {evidence_id}]" in text


def _status_refreshable(old: str, draft_block: str) -> bool:
    """Whether a ``Verification Status`` body may refresh from the draft.

    Pure scaffold text always tracks the current run's evidence state. An
    LLM-grounding note (``GROUNDING_STATUS_MARK``) may be replaced by a newer
    grounding note — a fresh ``--use-llm`` run recomputed it — but not by
    plain scaffold text: a deterministic rerun must not silently discard
    grounding results. verify-bq results and human-edited bodies carry
    neither mark and are never refreshed here.
    """
    if SCAFFOLD_STATUS_MARK in old:
        return True
    if GROUNDING_STATUS_MARK in old:
        return GROUNDING_STATUS_MARK in draft_block
    return False


def _reconcile_gap_bullets(
    merged: dict[str, str], stale_evidence: frozenset[str] | set[str] = frozenset()
) -> str:
    """Drop deterministic Known Gaps bullets that the page's own merged
    content shows as already resolved this run — a grounded Core Formula
    citation, a recorded divergence citing a table's BigQuery schema, or a
    published citation of a repo's loaded code (the "cross-checking lands
    in a later milestone" bullet) — so the page never simultaneously claims
    "not extracted yet" next to an extracted, cited Core Formula. Bullets
    reappear automatically once the citation they depend on is gone (e.g.
    the section was invalidated and reverted to scaffold), since that's
    re-derived from the merged content every time, not tracked as separate
    state.

    ``Known Doc-vs-Code Divergences`` is a preserved section: it survives a
    scaffold rewrite verbatim, with no staleness check of its own (unlike
    citation-bearing prose sections). So a divergence recorded before this
    run can go on citing a table's schema evidence even after that evidence
    changed. ``stale_evidence`` — this run's changed-evidence ids — is
    checked explicitly here so a stale citation can never be read as proof
    a gap is still resolved.
    """
    block = merged.get("Known Gaps", "")
    lines = block.splitlines()
    if not lines:
        return block
    heading_line, *body_lines = lines

    stale_prefixes = tuple(e for e in stale_evidence if e.endswith(":"))
    stale_exact = {e for e in stale_evidence if not e.endswith(":")}

    def _fresh(evidence_id: str) -> bool:
        return evidence_id not in stale_exact and not evidence_id.startswith(
            stale_prefixes or ("\0",)
        )

    # Only a formula grounded in fresh (non-stale) raw-doc evidence resolves
    # the "not extracted from raw docs" bullet — a formula grounded purely
    # in code or a human note doesn't speak to raw-doc extraction at all.
    formula_grounded = any(
        e.startswith("raw-doc:") and _fresh(e)
        for e in cited_evidence_ids(merged.get("Core Formula", ""))
    )
    evidence_text = merged.get("Core Formula", "") + merged.get(
        "Known Doc-vs-Code Divergences", ""
    )
    # Repo cross-check bullets resolve on any published citation of that
    # repo's files, wherever on the page it landed (code claims are not
    # confined to Core Formula the way formula claims are).
    page_cited = [
        cited
        for heading, body in merged.items()
        if heading != "Known Gaps"
        for cited in cited_evidence_ids(body)
    ]

    kept = []
    for line in body_lines:
        text = line.strip()
        if text.startswith("- "):
            text = text[2:]
        if formula_grounded and text == RAW_DOC_EXTRACTION_GAP:
            continue
        table = bq_cross_check_table(text)
        if table is not None:
            evidence_id = f"bq-schema:{table}"
            if _fresh(evidence_id) and _mentions_evidence(
                evidence_text, evidence_id
            ):
                continue
        repo = repo_cross_check_repo(text)
        if repo is not None:
            prefix = f"local-repo:{repo}:"
            # Divergences cite evidence as backtick-wrapped ids, not [src:].
            backticked = re.findall(
                rf"`({re.escape(prefix)}[^`]+)`",
                merged.get("Known Doc-vs-Code Divergences", ""),
            )
            if any(
                cited.startswith(prefix) and _fresh(cited)
                for cited in page_cited + backticked
            ):
                continue
        kept.append(line)
    if kept == body_lines:
        return block
    body = "\n".join(kept).strip() or "None recorded."
    return f"{heading_line}\n\n{body}\n"


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
    allow_status_refresh: bool = False,
) -> str:
    """Render ``draft`` while keeping manual content from ``existing``:

    - a section named in ``force`` always takes the draft body (the current
      run intentionally rewrote it, e.g. the LLM pipeline);
    - a section named in ``preserved`` keeps its existing body — except a
      ``Verification Status`` body that is still tool-authored (pure
      scaffold text, or an LLM-grounding note being replaced by a newer
      grounding note — see ``_status_refreshable``), which refreshes from
      the draft only when ``allow_status_refresh`` proves the whole page
      still matches its manifest snapshot; a grounding note whose page just
      had grounded sections invalidated likewise reverts to scaffold only
      with that ownership proof;
    - a section whose existing body carries ``[src: …]`` citations is
      evidence-written (LLM run) and survives a scaffold rewrite — *unless*
      one of its cited ids is in ``stale_evidence`` (that evidence changed
      this run), in which case the section reverts to the draft body plus a
      visible invalidation note, and ``(heading, stale_ids)`` is appended to
      ``invalidated`` when provided;
    - ``Known Gaps`` keeps ``- (llm)`` bullets from previous LLM runs, and
      drops the deterministic bullets that the page's own merged content
      (e.g. a grounded Core Formula citation) shows as already resolved;
    - sections present only in ``existing`` are retained (appended after
      the draft's sections, in their original order).
    """
    _, existing_sections = split_sections(existing)
    draft_prelude, draft_sections = split_sections(draft)

    existing_by_heading: dict[str, str] = {}
    for heading, block in existing_sections:
        existing_by_heading.setdefault(heading, block)
    draft_headings = {heading for heading, _ in draft_sections}

    order: list[str] = []
    merged: dict[str, str] = {}
    invalidated_here: list[tuple[str, list[str]]] = []
    for heading, block in draft_sections:
        old = existing_by_heading.get(heading)
        if heading in force or old is None:
            body = block
        elif heading in preserved:
            if (
                heading == "Verification Status"
                and allow_status_refresh
                and _status_refreshable(old, block)
            ):
                body = block
            else:
                body = old
        elif heading == "Known Gaps":
            body = _merge_gap_block(old, block)
        elif CITATION_MARK in old and CITATION_MARK not in block:
            stale_ids = _stale_cited(old, stale_evidence)
            if stale_ids:
                # The evidence this content cites changed: stale LLM prose
                # must not survive under a valid-looking citation.
                body = block.rstrip("\n") + "\n\n" + _invalidation_note(stale_ids) + "\n"
                invalidated_here.append((heading, stale_ids))
            else:
                body = old
        else:
            body = block
        order.append(heading)
        merged[heading] = body
    if invalidated is not None:
        invalidated.extend(invalidated_here)

    if (
        allow_status_refresh
        and invalidated_here
        and GROUNDING_STATUS_MARK in merged.get("Verification Status", "")
    ):
        # Grounded sections on this page were just reverted; a grounding
        # status describing them must not survive them. Revert it to the
        # scaffold draft (a fresh grounding note would have refreshed in
        # the loop above and needs no revert).
        draft_status = next(
            (b for h, b in draft_sections if h == "Verification Status"), None
        )
        if draft_status is not None and GROUNDING_STATUS_MARK not in draft_status:
            merged["Verification Status"] = draft_status

    if "Known Gaps" in merged:
        merged["Known Gaps"] = _reconcile_gap_bullets(merged, stale_evidence)

    parts = [draft_prelude] + [merged[heading] for heading in order]
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
