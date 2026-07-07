"""Dry-run workflow, overwrite protection, and manual-section preservation."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lineage_wiki.agent.runner import run_generate, run_update
from lineage_wiki.cli import app
from lineage_wiki.config import load_config
from lineage_wiki.constants import GENERATED_MARKER
from lineage_wiki.okf.sections import diff_summary, merge_manual_sections

from .conftest import FIXED_NOW, REPO_ROOT

runner = CliRunner()

EXAMPLE_CONFIG = REPO_ROOT / "chains" / "example.yml"

HAND_PAGE = (
    "---\ntype: Framework\ntitle: Mine\ndescription: Hand-written.\n---\n"
    "# Mine\n\n## Scope\n\nHand-written scope.\n"
)
HAND_INDEX = (
    "---\ntype: Index\ntitle: Hand Index\ndescription: Hand-written index.\n---\n"
    "# Hand Index\n\nCarefully curated by a human.\n"
)


def _tree_state(root: Path) -> dict[str, str]:
    """Content hash of every file under root (symlinks by target path)."""
    state: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            state[rel] = f"link:{path.readlink()}"
        elif path.is_file():
            state[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return state


# --- dry-run: no writes, accurate preview ---------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    before = _tree_state(tmp_path)
    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert _tree_state(tmp_path) == before == {}
    assert "DRY RUN — no files were written" in result.output
    assert "create    okf/frameworks/example-chain.md" in result.output
    assert "index     okf/index.md" in result.output
    assert "manifest  would be written" in result.output
    assert "run       not recorded (dry run)" in result.output
    assert "evidence:" in result.output
    assert "bigquery verification:" in result.output
    assert "validation (as if the run had been applied):" in result.output


def test_dry_run_reports_evidence_gaps_and_verification(tmp_path):
    doc = tmp_path / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Example Methodology\n\nBody.\n", encoding="utf-8")
    result = run_generate(load_config(EXAMPLE_CONFIG), tmp_path, FIXED_NOW, dry_run=True)
    assert result.dry_run

    evidence = "\n".join(result.evidence)
    assert "raw doc `raw_files/example/methodology.md` — loaded" in evidence
    assert "repo `example-pipeline`" in evidence
    assert "bigquery — unavailable" in evidence

    assert result.gaps  # missing evidence surfaces as Known Gaps
    verification = "\n".join(result.verification)
    assert "would verify `example-project.analytics.example_daily_snapshot`" in verification
    assert "generate never queries BigQuery" in verification


def test_dry_run_predicts_real_run(tmp_path):
    cfg = load_config(EXAMPLE_CONFIG)
    dry = run_generate(cfg, tmp_path, FIXED_NOW, dry_run=True)
    assert not (tmp_path / "okf").exists()
    assert not (tmp_path / ".lineage-wiki").exists()

    real = run_generate(cfg, tmp_path, FIXED_NOW)
    assert sorted(dry.created) == sorted(real.created)
    assert sorted(dry.indexes_written) == sorted(real.indexes_written)
    assert dry.manifest_written == real.manifest_written is True
    # The shadow-applied preview and the real run agree on validation.
    assert [str(i) for i in dry.report.errors] == [str(i) for i in real.report.errors]

    # Contents predicted by the dry run are what the real run wrote.
    for rel, content in dry.pending.items():
        assert (tmp_path / rel).read_text(encoding="utf-8") == content


def test_target_repo_flag_is_root_alias(tmp_path):
    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--target-repo", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "okf" / "frameworks" / "example-chain.md").exists()


# --- protection: hand-written pages and indexes ----------------------------------


def test_dry_run_then_real_run_protect_hand_written_pages(tmp_path):
    page = tmp_path / "okf" / "frameworks" / "example-chain.md"
    page.parent.mkdir(parents=True)
    page.write_text(HAND_PAGE, encoding="utf-8")
    index = tmp_path / "okf" / "index.md"
    index.write_text(HAND_INDEX, encoding="utf-8")

    dry = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path), "--dry-run"],
    )
    assert dry.exit_code == 0, dry.output
    assert "protected okf/frameworks/example-chain.md" in dry.output
    assert "protected okf/index.md (existing index is not tool-generated" in dry.output
    assert page.read_text(encoding="utf-8") == HAND_PAGE
    assert index.read_text(encoding="utf-8") == HAND_INDEX

    real = runner.invoke(
        app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)]
    )
    assert real.exit_code == 0, real.output
    assert "protected okf/frameworks/example-chain.md" in real.output
    assert "protected okf/index.md" in real.output
    assert page.read_text(encoding="utf-8") == HAND_PAGE
    assert index.read_text(encoding="utf-8") == HAND_INDEX


def test_hand_written_index_never_enters_manifest(tmp_path, example_cfg):
    index = tmp_path / "okf" / "index.md"
    index.parent.mkdir(parents=True)
    index.write_text(HAND_INDEX, encoding="utf-8")

    result = run_generate(example_cfg, tmp_path, FIXED_NOW)
    assert "okf/index.md" in result.indexes_skipped

    import yaml

    manifest = yaml.safe_load(
        (tmp_path / ".lineage-wiki" / "manifest.yml").read_text(encoding="utf-8")
    )
    entry = manifest["chains"][example_cfg.chain.id]
    assert "okf/index.md" not in entry["managed_indexes"]
    assert "okf/frameworks/index.md" in entry["managed_indexes"]

    # And the next run still refuses to touch it.
    again = run_generate(example_cfg, tmp_path, FIXED_NOW)
    assert "okf/index.md" in again.indexes_skipped
    assert index.read_text(encoding="utf-8") == HAND_INDEX


def test_init_then_generate_owns_init_indexes(tmp_path, example_cfg):
    from lineage_wiki.agent.runner import run_init

    run_init(tmp_path, now=FIXED_NOW)
    root_index = (tmp_path / "okf" / "index.md").read_text(encoding="utf-8")
    assert GENERATED_MARKER in root_index

    result = run_generate(example_cfg, tmp_path, FIXED_NOW)
    assert not result.indexes_skipped
    assert "okf/index.md" in result.indexes_written  # regenerated with new pages


def test_generated_indexes_carry_marker(tmp_path, example_cfg):
    run_generate(example_cfg, tmp_path, FIXED_NOW)
    for rel in ["okf/index.md", "okf/frameworks/index.md", "okf/metrics/index.md"]:
        assert GENERATED_MARKER in (tmp_path / rel).read_text(encoding="utf-8")


# --- manual-section preservation --------------------------------------------------


def _replace_section(path: Path, heading: str, body: str) -> None:
    import re

    text = path.read_text(encoding="utf-8")
    pattern = rf"(?ms)^(## {re.escape(heading)}\n).*?(?=^## |\Z)"
    updated = re.sub(pattern, rf"\g<1>\n{body}\n\n", text, count=1)
    assert updated != text
    path.write_text(updated, encoding="utf-8")


def test_generate_preserves_verification_and_manual_sections(tmp_path, example_cfg):
    run_generate(example_cfg, tmp_path, FIXED_NOW)
    output_page = tmp_path / "okf" / "outputs" / "example-daily-snapshot.md"

    _replace_section(
        output_page, "Verification Status", "Verified by verify-bq on 2026-07-03."
    )
    with output_page.open("a", encoding="utf-8") as fh:
        fh.write("\n## Operational Notes\n\nManual runbook link: see wiki.\n")
    edited = output_page.read_text(encoding="utf-8")

    second = run_generate(example_cfg, tmp_path, FIXED_NOW)
    kept = output_page.read_text(encoding="utf-8")
    assert "Verified by verify-bq on 2026-07-03." in kept
    assert "## Operational Notes" in kept
    assert "Manual runbook link: see wiki." in kept
    # Nothing else about the page changed, so the run classified it unchanged.
    assert "okf/outputs/example-daily-snapshot.md" in second.unchanged
    assert kept == edited

    # A later update run is still a strict no-op.
    update = run_update(example_cfg, tmp_path, FIXED_NOW)
    assert update.noop


def test_merge_manual_sections_semantics():
    existing = (
        "---\ntype: Output\n---\n# Page\n\nIntro old.\n\n"
        "## Columns\n\nOld column text.\n\n"
        "## Verification Status\n\nVerified: all good.\n\n"
        "## Runbook\n\nManual content.\n"
    )
    draft = (
        "---\ntype: Output\n---\n# Page\n\nIntro new.\n\n"
        "## Columns\n\nNew column text.\n\n"
        "## Verification Status\n\nNot verified yet (scaffold).\n"
    )
    merged = merge_manual_sections(existing, draft)
    assert "Intro new." in merged  # prelude comes from the draft
    assert "New column text." in merged  # template-owned sections refresh
    assert "Verified: all good." in merged  # preserved section survives
    assert "Not verified yet (scaffold)." not in merged
    assert merged.rstrip().endswith("Manual content.")  # manual section retained


def test_diff_summary_reports_lines_and_sections():
    old = "# T\n\n## A\n\none\n\n## B\n\ntwo\n"
    new = "# T\n\n## A\n\none\nplus\n\n## B\n\nchanged\n"
    summary = diff_summary(old, new)
    assert summary.startswith("+2 -1 line(s)")
    assert "A" in summary and "B" in summary


def test_update_run_reports_diff_summary(tmp_path, example_cfg):
    doc = tmp_path / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Methodology\n\nversion one\n", encoding="utf-8")
    run_generate(example_cfg, tmp_path, FIXED_NOW)

    # Retitling the doc changes the rendered evidence tables, so the
    # affected pages are really rewritten (not just re-fingerprinted).
    doc.write_text("# Methodology v2\n\nversion two\n\nwith more lines\n", encoding="utf-8")
    result = run_update(example_cfg, tmp_path, "2026-07-04T00:00:00Z")
    assert not result.noop
    assert result.updated
    rel = result.updated[0]
    assert "line(s); sections:" in result.diffs[rel]
