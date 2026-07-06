"""Stale-citation invalidation: LLM-written sections whose cited evidence
changed must not survive an update run under valid-looking citations."""

from __future__ import annotations

from lineage_wiki.agent.runner import _stale_evidence_ids, run_generate, run_update
from lineage_wiki.config import load_config
from lineage_wiki.okf.sections import (
    cited_evidence_ids,
    merge_manual_sections,
)
from lineage_wiki.storage.manifest import SourceChanges

from .conftest import EXAMPLE_CONFIG, FIXED_NOW
from .test_llm import RAW_DOC_ID, _setup_target

LATER = "2026-07-07T00:00:00Z"

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
