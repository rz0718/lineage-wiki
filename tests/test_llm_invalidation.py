"""Stale-citation invalidation: LLM-written sections whose cited evidence
changed must not survive an update run under valid-looking citations."""

from __future__ import annotations

from lineage_wiki.agent.runner import _stale_evidence_ids, run_generate, run_update
from lineage_wiki.config import load_config
from lineage_wiki.okf.sections import (
    cited_evidence_ids,
    merge_manual_sections,
)
from lineage_wiki.okf.templates import (
    RAW_DOC_EXTRACTION_GAP,
    bq_cross_check_gap,
    repo_cross_check_gap,
    repo_cross_check_repo,
)
from lineage_wiki.storage.manifest import SourceChanges

from .conftest import EXAMPLE_CONFIG, FIXED_NOW
from .test_llm import RAW_DOC_ID, _setup_target

LATER = "2026-07-07T00:00:00Z"

TABLE = "proj.ds.tbl"
GAP_PAGE_DRAFT = (
    "---\ntype: Framework\n---\n# Page\n\n"
    "## Core Formula\n\nScaffold formula.\n\n"
    "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
    "## Known Gaps\n\n"
    f"- {RAW_DOC_EXTRACTION_GAP}\n"
    f"- {bq_cross_check_gap(TABLE)}\n"
    "- Some unrelated gap.\n"
)

EXISTING_PAGE = (
    "---\ntype: Framework\n---\n# Page\n\n"
    "## Scope\n\nLLM text about scope. [src: raw-doc:docs/a.md]\n\n"
    "## Core Formula\n\nx = y [src: local-repo:pipeline:main.py]\n\n"
    "## Components\n\nPlain scaffold text.\n"
)
DRAFT_PAGE = (
    "---\ntype: Framework\n---\n# Page\n\n"
    "## Scope\n\nScaffold scope.\n\n"
    "## Core Formula\n\nScaffold formula.\n\n"
    "## Components\n\nPlain scaffold text.\n"
)


# --- unit: cited-id extraction and stale-aware merge ------------------------------


def test_cited_evidence_ids_extraction():
    assert cited_evidence_ids(EXISTING_PAGE) == [
        "raw-doc:docs/a.md",
        "local-repo:pipeline:main.py",
    ]
    assert cited_evidence_ids("no markers") == []


def test_merge_keeps_cited_sections_when_evidence_unchanged():
    merged = merge_manual_sections(EXISTING_PAGE, DRAFT_PAGE)
    assert "LLM text about scope." in merged
    assert "x = y [src: local-repo:pipeline:main.py]" in merged


def test_merge_invalidates_sections_citing_stale_evidence():
    invalidated: list[tuple[str, list[str]]] = []
    merged = merge_manual_sections(
        EXISTING_PAGE,
        DRAFT_PAGE,
        stale_evidence=frozenset({"raw-doc:docs/a.md"}),
        invalidated=invalidated,
    )
    # The section citing the changed doc reverts to scaffold + a visible note.
    assert "LLM text about scope." not in merged
    assert "Scaffold scope." in merged
    assert "invalidated because" in merged and "`raw-doc:docs/a.md`" in merged
    # The section citing untouched evidence survives.
    assert "x = y [src: local-repo:pipeline:main.py]" in merged
    assert invalidated == [("Scope", ["raw-doc:docs/a.md"])]


def test_merge_prefix_matching_for_repo_evidence():
    invalidated: list[tuple[str, list[str]]] = []
    merged = merge_manual_sections(
        EXISTING_PAGE,
        DRAFT_PAGE,
        stale_evidence=frozenset({"local-repo:pipeline:"}),
        invalidated=invalidated,
    )
    assert "x = y" not in merged
    assert "LLM text about scope." in merged  # raw doc untouched
    assert invalidated == [("Core Formula", ["local-repo:pipeline:main.py"])]


# --- unit: Known Gaps reconciliation against the page's own merged content -------


def test_merge_drops_raw_doc_gap_when_formula_grounded_in_raw_doc():
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\n`x = y` [src: raw-doc:docs/a.md]\n\n"
        "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
        "## Known Gaps\n\n"
        f"- {RAW_DOC_EXTRACTION_GAP}\n"
        f"- {bq_cross_check_gap(TABLE)}\n"
        "- Some unrelated gap.\n"
    )
    merged = merge_manual_sections(existing, GAP_PAGE_DRAFT)
    assert RAW_DOC_EXTRACTION_GAP not in merged
    assert "Some unrelated gap." in merged  # untouched gaps survive


def test_merge_keeps_raw_doc_gap_when_formula_grounded_in_code_not_raw_doc():
    """A formula claim may cite local_repo or human_note evidence — that
    doesn't resolve "not extracted from raw docs" (the exact false-positive
    a review pass on this logic flagged)."""
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\n`x = y` [src: local-repo:pipeline:main.py]\n\n"
        "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
        "## Known Gaps\n\n"
        f"- {RAW_DOC_EXTRACTION_GAP}\n"
        "- Some unrelated gap.\n"
    )
    draft = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\nScaffold formula.\n\n"
        "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
        "## Known Gaps\n\n"
        f"- {RAW_DOC_EXTRACTION_GAP}\n"
        "- Some unrelated gap.\n"
    )
    merged = merge_manual_sections(existing, draft)
    assert RAW_DOC_EXTRACTION_GAP in merged


def test_merge_drops_bq_gap_when_divergence_cites_table_schema():
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\nScaffold formula.\n\n"
        "## Known Doc-vs-Code Divergences\n\n"
        f"- **Formula wording** — mismatch (evidence: `bq-schema:{TABLE}`)\n\n"
        "## Known Gaps\n\n"
        f"- {RAW_DOC_EXTRACTION_GAP}\n"
        f"- {bq_cross_check_gap(TABLE)}\n"
    )
    merged = merge_manual_sections(existing, GAP_PAGE_DRAFT)
    assert bq_cross_check_gap(TABLE) not in merged
    assert RAW_DOC_EXTRACTION_GAP in merged  # unrelated gap untouched


def test_merge_gap_reappears_once_grounding_is_invalidated():
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\n`x = y` [src: raw-doc:docs/a.md]\n\n"
        "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
        "## Known Gaps\n\n"
        f"- {RAW_DOC_EXTRACTION_GAP}\n"
    )
    draft = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\nScaffold formula.\n\n"
        "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
        "## Known Gaps\n\n"
        f"- {RAW_DOC_EXTRACTION_GAP}\n"
    )
    # While the citation is fresh, the gap stays resolved.
    assert RAW_DOC_EXTRACTION_GAP not in merge_manual_sections(existing, draft)
    # Once its evidence goes stale, Core Formula reverts to scaffold and the
    # gap bullet must come back — the page must never claim resolution for a
    # citation it just discarded.
    merged = merge_manual_sections(
        existing, draft, stale_evidence=frozenset({"raw-doc:docs/a.md"})
    )
    assert RAW_DOC_EXTRACTION_GAP in merged


def test_merge_bq_gap_reappears_when_preserved_divergence_evidence_goes_stale():
    """`Known Doc-vs-Code Divergences` is a preserved section: it survives a
    scaffold rewrite verbatim with no staleness check of its own, unlike
    Core Formula. A stale-but-still-preserved divergence citing a table's
    schema must not be read as proof that table is still cross-checked."""
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\nScaffold formula.\n\n"
        "## Known Doc-vs-Code Divergences\n\n"
        f"- **Formula wording** — mismatch (evidence: `bq-schema:{TABLE}`)\n\n"
        "## Known Gaps\n\n"
        f"- {bq_cross_check_gap(TABLE)}\n"
    )
    draft = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\nScaffold formula.\n\n"
        "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
        "## Known Gaps\n\n"
        f"- {bq_cross_check_gap(TABLE)}\n"
    )
    # No schema change this run: the preserved divergence is still valid.
    fresh = merge_manual_sections(existing, draft)
    assert bq_cross_check_gap(TABLE) not in fresh

    # The table's schema changed this run: that citation is now stale, so
    # the preserved (unchanged) divergence text must not resolve the gap.
    stale = merge_manual_sections(
        existing, draft, stale_evidence=frozenset({f"bq-schema:{TABLE}"})
    )
    assert bq_cross_check_gap(TABLE) in stale


REPO_GAP = repo_cross_check_gap("pipeline")
REPO_GAP_DRAFT = (
    "---\ntype: Framework\n---\n# Page\n\n"
    "## Core Formula\n\nScaffold formula.\n\n"
    "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
    "## Known Gaps\n\n"
    f"- {REPO_GAP}\n"
    "- Some unrelated gap.\n"
)


def test_repo_cross_check_gap_roundtrip():
    assert repo_cross_check_repo(repo_cross_check_gap("engine")) == "engine"
    assert repo_cross_check_repo("Some other bullet.") is None
    assert "later milestone" in repo_cross_check_gap("engine")


def test_merge_drops_repo_gap_when_published_citation_covers_repo():
    """A published citation of the repo's loaded files supersedes the
    "cross-checking lands in a later milestone" bullet — wherever on the
    page the citation landed."""
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\n`x = y` [src: local-repo:pipeline:main.py]\n\n"
        "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
        "## Known Gaps\n\n"
        f"- {REPO_GAP}\n"
        "- Some unrelated gap.\n"
    )
    merged = merge_manual_sections(existing, REPO_GAP_DRAFT)
    assert REPO_GAP not in merged
    assert "Some unrelated gap." in merged
    # A citation of some *other* repo does not resolve this repo's bullet.
    other = existing.replace("local-repo:pipeline:main.py", "local-repo:other:main.py")
    assert REPO_GAP in merge_manual_sections(other, REPO_GAP_DRAFT)


def test_merge_repo_gap_reappears_when_repo_evidence_goes_stale():
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\n`x = y` [src: local-repo:pipeline:main.py]\n\n"
        "## Known Doc-vs-Code Divergences\n\nNone recorded yet.\n\n"
        "## Known Gaps\n\n"
        f"- {REPO_GAP}\n"
    )
    merged = merge_manual_sections(
        existing, REPO_GAP_DRAFT, stale_evidence=frozenset({"local-repo:pipeline:"})
    )
    assert REPO_GAP in merged


def test_merge_drops_repo_gap_when_divergence_cites_repo_file():
    """Divergences cite evidence as backtick-wrapped ids; a preserved
    divergence citing the repo's code also counts as cross-checking."""
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Core Formula\n\nScaffold formula.\n\n"
        "## Known Doc-vs-Code Divergences\n\n"
        "- **Rounding** — doc vs code mismatch "
        "(evidence: `raw-doc:docs/a.md`, `local-repo:pipeline:main.py`)\n\n"
        "## Known Gaps\n\n"
        f"- {REPO_GAP}\n"
    )
    assert REPO_GAP not in merge_manual_sections(existing, REPO_GAP_DRAFT)
    stale = merge_manual_sections(
        existing, REPO_GAP_DRAFT, stale_evidence=frozenset({"local-repo:pipeline:"})
    )
    assert REPO_GAP in stale


def test_update_reverts_grounding_status_when_cited_doc_changes(tmp_path, monkeypatch):
    """The grounding status must not outlive the grounded sections it
    describes: once the update run invalidates them, the status reverts to
    scaffold wording (and the next --use-llm run can refresh it again)."""
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    run_generate(cfg, root, FIXED_NOW, use_llm=True)

    framework = root / "okf" / "frameworks" / "example-chain.md"
    status_before = framework.read_text(encoding="utf-8").split(
        "## Verification Status"
    )[1].split("\n## ")[0]
    assert "LLM claim grounding has run" in status_before

    (root / "raw_files" / "example" / "methodology.md").write_text(
        "# Example Methodology v2\n\nEverything changed.\n", encoding="utf-8"
    )
    run_update(cfg, root, LATER)

    status_after = framework.read_text(encoding="utf-8").split(
        "## Verification Status"
    )[1].split("\n## ")[0]
    assert "LLM claim grounding" not in status_after
    assert "Unverified scaffold" in status_after


def test_update_preserves_human_note_appended_to_grounding_status(
    tmp_path, monkeypatch
):
    from lineage_wiki.okf.sections import replace_section

    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    run_generate(cfg, root, FIXED_NOW, use_llm=True)

    framework = root / "okf" / "frameworks" / "example-chain.md"
    page = framework.read_text(encoding="utf-8")
    status = page.split("## Verification Status", 1)[1].split("\n## ", 1)[0]
    human_note = "Owner note: investigate the invalidated evidence manually."
    edited = replace_section(
        page,
        "Verification Status",
        f"{status.strip()}\n\n{human_note}",
    )
    framework.write_text(edited, encoding="utf-8")

    (root / "raw_files" / "example" / "methodology.md").write_text(
        "# Example Methodology v2\n\nEverything changed.\n", encoding="utf-8"
    )
    run_update(cfg, root, LATER)

    status_after = framework.read_text(encoding="utf-8").split(
        "## Verification Status", 1
    )[1].split("\n## ", 1)[0]
    assert "LLM claim grounding has run" in status_after
    assert human_note in status_after


def test_stale_evidence_ids_mapping():
    cfg = load_config(EXAMPLE_CONFIG)
    changes = SourceChanges(
        raw_docs=["raw_files/example/methodology.md"],
        repos=["example-pipeline"],
        bigquery=["example-project.analytics.example_daily_snapshot"],
        reports=["Example Daily Report"],
        config=True,
    )
    stale = _stale_evidence_ids(cfg, changes)
    assert "raw-doc:raw_files/example/methodology.md" in stale
    assert "local-repo:example-pipeline:" in stale
    assert "bq-schema:example-project.analytics.example_daily_snapshot" in stale
    assert "report:example-daily-report" in stale
    assert "human-note:" in stale


# --- end-to-end: update after generate --use-llm ----------------------------------


def test_update_invalidates_llm_sections_when_cited_doc_changes(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    first = run_generate(cfg, root, FIXED_NOW, use_llm=True)
    assert first.llm

    framework = root / "okf" / "frameworks" / "example-chain.md"
    enriched = framework.read_text(encoding="utf-8")
    assert f"[src: {RAW_DOC_ID}]" in enriched

    # The cited methodology changes.
    (root / "raw_files" / "example" / "methodology.md").write_text(
        (
            "# Example Methodology v2\n\n"
            "The pipeline scope was widened beyond daily snapshots.\n\n"
            "Total value = quantity * price * fx\n"
        ),
        encoding="utf-8",
    )

    # Plan-only predicts the revert as a risk, without writing.
    planned = run_update(cfg, root, LATER, plan_only=True)
    assert any("will be reverted" in r for r in planned.risks)
    assert f"[src: {RAW_DOC_ID}]" in framework.read_text(encoding="utf-8")

    applied = run_update(cfg, root, LATER)
    rel = "okf/frameworks/example-chain.md"
    assert rel in applied.updated
    headings = [h for h, _ in applied.invalidated[rel]]
    assert "Scope" in headings and "Core Formula" in headings

    after = framework.read_text(encoding="utf-8")
    # Stale LLM prose is gone; the invalidation note explains why.
    assert "covers the daily example metric snapshot pipeline." not in after
    assert "`total_value = quantity * price`" not in after
    assert "invalidated because its cited evidence changed" in after
    assert RAW_DOC_ID in after
    # The Known Gaps bullet this formula had resolved must come back — the
    # page can't claim raw-doc extraction is done next to a reverted,
    # no-longer-cited Core Formula section.
    assert "have not been extracted from" in after

    # The run settles: a follow-up update is a strict no-op.
    again = run_update(cfg, root, LATER)
    assert again.noop


def test_update_keeps_llm_sections_when_other_evidence_changes(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    run_generate(cfg, root, FIXED_NOW, use_llm=True)

    # Only the report mapping changes; the cited raw doc is untouched.
    cfg2 = load_config(EXAMPLE_CONFIG)
    cfg2.sources.reports[0].source_mapping_notes = "Rewired to the new dashboard."
    result = run_update(cfg2, root, LATER)
    assert not result.noop
    assert not result.invalidated

    after = (tmp_path / "okf" / "frameworks" / "example-chain.md").read_text(
        encoding="utf-8"
    )
    assert f"[src: {RAW_DOC_ID}]" in after  # LLM sections preserved
