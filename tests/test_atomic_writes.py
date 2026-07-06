from __future__ import annotations

from pathlib import Path

import pytest

from lineage_wiki.agent.runner import GenerateError, run_generate, run_update
from lineage_wiki.config import MetricInput
from lineage_wiki.okf.validator import Issue, ValidationReport
from lineage_wiki.storage.manifest import load_manifest

from .conftest import FIXED_NOW

LATER = "2026-07-04T00:00:00Z"


def _tree_state(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _write_raw_doc(root: Path, content: str = "# Methodology\n\nUpdated.\n") -> None:
    doc = root / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(content, encoding="utf-8")


def test_hand_written_index_entries_survive_update(example_cfg, tmp_path):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    index = tmp_path / "okf" / "metrics" / "index.md"
    original = index.read_text(encoding="utf-8")
    edited = original.replace(
        "| Example Metric | [Example Metric](example-metric.md) |",
        "| Example Metric | [Example Metric](example-metric.md) |\n"
        "| Human Alias | [Example Metric](example-metric.md) |",
    )
    index.write_text(edited, encoding="utf-8")

    cfg2 = example_cfg.model_copy(deep=True)
    cfg2.sources.metrics.append(
        MetricInput(name="Second Metric", definition="Another value.", unit="IDR")
    )
    result = run_update(cfg2, tmp_path, now=LATER)

    assert "okf/metrics/index.md" in result.indexes_skipped
    assert index.read_text(encoding="utf-8") == edited
    assert result.report is not None and result.report.errors == []
    manifest = load_manifest(tmp_path)
    assert "okf/metrics/index.md" not in manifest.managed_indexes


def test_failed_staged_validation_rolls_back_all_writes(example_cfg, tmp_path, monkeypatch):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    _write_raw_doc(tmp_path)
    before = _tree_state(tmp_path)

    def fail_validation(root: Path, okf_dir: str = "okf") -> ValidationReport:
        return ValidationReport(
            issues=[
                Issue(
                    "error",
                    f"{okf_dir}/frameworks/example-chain.md",
                    "forced staged validation failure",
                )
            ]
        )

    monkeypatch.setattr("lineage_wiki.agent.runner.validate_tree", fail_validation)

    with pytest.raises(GenerateError, match="staged output failed validation"):
        run_update(example_cfg, tmp_path, now=LATER)

    assert _tree_state(tmp_path) == before


def test_manual_sections_embedded_in_generated_pages_survive_update(example_cfg, tmp_path):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    page = tmp_path / "okf" / "frameworks" / "example-chain.md"
    page.write_text(
        page.read_text(encoding="utf-8")
        + "\n## Human Runbook\n\nManual escalation path.\n",
        encoding="utf-8",
    )
    _write_raw_doc(tmp_path, "# Methodology Retitled\n\nUpdated source.\n")

    result = run_update(example_cfg, tmp_path, now=LATER)

    assert "okf/frameworks/example-chain.md" in result.updated
    text = page.read_text(encoding="utf-8")
    assert "## Human Runbook" in text
    assert "Manual escalation path." in text


def test_fingerprint_only_update_does_not_advance_manifest(example_cfg, tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    repo = tmp_path / "example-pipeline" / "pipeline"
    repo.mkdir(parents=True)
    (repo / "main.py").write_text(
        "def compute_daily_snapshot():\n    return 1\n", encoding="utf-8"
    )
    (repo / "utils.py").write_text("HELPER = True\n", encoding="utf-8")

    run_generate(example_cfg, root, now=FIXED_NOW)
    before = _tree_state(root)
    before_manifest = load_manifest(root)
    before_entry = before_manifest.chains[example_cfg.chain.id]
    (repo / "main.py").write_text(
        "def compute_daily_snapshot():\n    return 2\n", encoding="utf-8"
    )

    result = run_update(example_cfg, root, now=LATER)

    assert result.noop is False
    assert result.updated == result.created == result.indexes_written == []
    assert result.manifest_written is False
    assert result.run_file is None
    assert _tree_state(root) == before
    after_entry = load_manifest(root).chains[example_cfg.chain.id]
    assert after_entry.source_fingerprints == before_entry.source_fingerprints
    again = run_update(example_cfg, root, now=LATER)
    assert again.noop is False
    assert again.changes.repos == ["example-pipeline"]


def test_unsafe_output_dir_is_rejected_before_any_write(example_cfg, tmp_path):
    cfg = example_cfg.model_copy(deep=True)
    cfg.generation.output_dir = "../outside"
    before = _tree_state(tmp_path)

    with pytest.raises(GenerateError, match="unsafe output_dir"):
        run_generate(cfg, tmp_path, now=FIXED_NOW)

    assert _tree_state(tmp_path) == before

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "okf").symlink_to(outside, target_is_directory=True)
    before = _tree_state(tmp_path)

    with pytest.raises(GenerateError, match="unsafe output_dir"):
        run_generate(example_cfg, tmp_path, now=FIXED_NOW)

    assert _tree_state(tmp_path) == before
    assert list(outside.iterdir()) == []
