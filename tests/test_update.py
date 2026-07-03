import json
from pathlib import Path

import pytest

from lineage_wiki.agent.runner import GenerateError, run_generate, run_update
from lineage_wiki.config import MetricInput
from lineage_wiki.storage.manifest import load_manifest
from lineage_wiki.storage.runs import list_runs

from .conftest import FIXED_NOW

LATER = "2026-07-04T00:00:00Z"


@pytest.fixture
def wiki_root(tmp_path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


def _tree_state(root: Path) -> dict[str, bytes]:
    """Byte-level snapshot of everything a run could touch."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _write_raw_doc(root: Path, content: str = "# Methodology\n") -> Path:
    doc = root / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(content)
    return doc


# --- no-op behavior (key acceptance) --------------------------------------------


def test_noop_update_touches_nothing(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    before = _tree_state(wiki_root)

    # even on a later day: no source changes -> nothing written at all
    result = run_update(example_cfg, wiki_root, now=LATER)

    assert result.noop is True
    assert result.created == result.updated == result.indexes_written == []
    assert result.run_file is None
    assert result.manifest_written is False
    assert _tree_state(wiki_root) == before


def test_generate_rerun_on_later_day_is_noop(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    before = _tree_state(wiki_root)

    result = run_generate(example_cfg, wiki_root, now=LATER)

    assert result.created == [] and result.updated == []
    assert result.indexes_written == []
    assert result.manifest_written is False
    assert result.run_file is None
    assert _tree_state(wiki_root) == before


def test_update_requires_manifest(example_cfg, wiki_root):
    with pytest.raises(GenerateError, match="generate"):
        run_update(example_cfg, wiki_root, now=FIXED_NOW)


def test_update_rejects_other_chain_manifest(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    other = example_cfg.model_copy(deep=True)
    other.chain.id = "other_chain"
    with pytest.raises(GenerateError, match="other_chain"):
        run_update(other, wiki_root, now=FIXED_NOW)


# --- changed-source impact planning ----------------------------------------------


def test_raw_doc_change_updates_framework_only(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    _write_raw_doc(wiki_root)

    result = run_update(example_cfg, wiki_root, now=LATER)

    assert result.noop is False
    assert result.changes.raw_docs == ["raw_files/example/methodology.md"]
    assert result.changes.repos == [] and result.changes.config is False

    framework = "okf/frameworks/example-chain.md"
    assert framework in result.impact
    assert "raw doc `raw_files/example/methodology.md` changed" in result.impact[framework]
    assert result.updated == [framework]

    # the framework now cites the raw doc instead of gap-flagging its absence
    content = (wiki_root / framework).read_text()
    assert "../../raw_files/example/methodology.md" in content
    assert "was not found at generate time" not in content

    # untouched evidence -> untouched pages
    assert "okf/outputs/example-daily-snapshot.md" not in result.updated
    assert "okf/outputs/example-daily-snapshot.md" not in result.impact

    # fingerprints recorded; a second update is a strict no-op
    manifest = load_manifest(wiki_root)
    assert manifest.source_fingerprints.raw_docs[
        "raw_files/example/methodology.md"
    ].startswith("sha256:")
    assert run_update(example_cfg, wiki_root, now=LATER).noop is True


def test_repo_content_change_plans_code_impact_without_churn(example_cfg, tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    repo = tmp_path / "example-pipeline" / "pipeline"
    repo.mkdir(parents=True)
    (repo / "main.py").write_text("def compute_daily_snapshot():\n    return 1\n")
    (repo / "utils.py").write_text("HELPER = True\n")

    run_generate(example_cfg, root, now=FIXED_NOW)
    before = _tree_state(root)
    (repo / "main.py").write_text("def compute_daily_snapshot():\n    return 2\n")

    result = run_update(example_cfg, root, now=LATER)

    assert result.changes.repos == ["example-pipeline"]
    expected_impact = {
        "okf/code-links/example-pipeline-engine.md",
        "okf/frameworks/example-chain.md",
        "okf/change-checks/example-chain-review-rules.md",
    }
    assert set(result.impact) == expected_impact
    for reasons in result.impact.values():
        assert "repo `example-pipeline` changed" in reasons

    # deterministic templates do not embed code content: pages considered but
    # not rewritten, and no run metadata is churned
    assert result.updated == [] and result.created == []
    assert result.run_file is None
    assert result.manifest_written is True  # new fingerprints recorded

    after = _tree_state(root)
    changed = {rel for rel in after if after[rel] != before.get(rel)}
    assert changed == {".lineage-wiki/manifest.yml"}
    assert run_update(example_cfg, root, now=LATER).noop is True


def test_bigquery_table_addition_impacts_outputs(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    cfg2 = example_cfg.model_copy(deep=True)
    cfg2.sources.bigquery.tables.append("example-project.analytics.example_intraday")

    result = run_update(cfg2, wiki_root, now=LATER)

    assert "example-project.analytics.example_intraday" in result.changes.bigquery
    assert result.changes.config is True  # table list lives in the config
    new_output = "okf/outputs/example-intraday.md"
    assert new_output in result.created
    reasons = result.impact[new_output]
    assert "bigquery table `example-project.analytics.example_intraday` changed" in reasons
    # report templates are in scope when an output table changes
    assert "okf/report-templates/example-daily-report.md" in result.impact
    assert result.report is not None and result.report.errors == []


def test_report_mapping_change_impacts_report_chain(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    cfg2 = example_cfg.model_copy(deep=True)
    cfg2.sources.reports[0].source_mapping_notes = "Line 1 maps to total_value."

    result = run_update(cfg2, wiki_root, now=LATER)

    assert result.changes.reports == ["Example Daily Report"]
    report_page = "okf/report-templates/example-daily-report.md"
    assert report_page in result.impact
    assert "report `Example Daily Report` mapping changed" in result.impact[report_page]
    assert "okf/outputs/example-daily-snapshot.md" in result.impact
    assert report_page in result.updated
    assert "Line 1 maps to total_value." in (wiki_root / report_page).read_text()


# --- metrics registry updates ------------------------------------------------------


def test_new_metric_is_created_and_registered(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    cfg2 = example_cfg.model_copy(deep=True)
    cfg2.sources.metrics.append(
        MetricInput(name="Second Metric", definition="Another value.", unit="IDR")
    )

    result = run_update(cfg2, wiki_root, now=LATER)

    assert result.changes.config is True
    assert "okf/metrics/second-metric.md" in result.created
    registry = (wiki_root / "okf" / "metrics" / "index.md").read_text()
    assert "| Second Metric | [Second Metric](second-metric.md) |" in registry
    assert "okf/metrics/index.md" in result.indexes_written
    assert result.report is not None and result.report.errors == []

    manifest = load_manifest(wiki_root)
    assert "okf/metrics/second-metric.md" in manifest.generated_files


# --- run metadata ----------------------------------------------------------------


def test_run_metadata_written_only_on_content_change(example_cfg, wiki_root):
    result = run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    runs = list_runs(wiki_root)
    assert len(runs) == 1
    record = json.loads(runs[0].read_text())
    assert record["command"] == "generate"
    assert record["chainId"] == "example_chain"
    assert record["contentChanged"] is True
    assert "okf/frameworks/example-chain.md" in record["createdFiles"]
    assert record["gaps"] == len(result.gaps)

    # no-op update -> still exactly one run file
    run_update(example_cfg, wiki_root, now=LATER)
    assert len(list_runs(wiki_root)) == 1

    # content-changing update -> a second run file
    _write_raw_doc(wiki_root)
    update_result = run_update(example_cfg, wiki_root, now=LATER)
    runs = list_runs(wiki_root)
    assert len(runs) == 2
    record = json.loads(runs[-1].read_text())
    assert record["command"] == "update"
    assert record["contentChanged"] is True
    assert record["updatedFiles"] == update_result.updated + update_result.indexes_written


def test_human_created_page_is_never_overwritten_by_update(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    # replace a tool page with a human one by removing it from the manifest
    manifest = load_manifest(wiki_root)
    manifest.generated_files = [
        f for f in manifest.generated_files if "report-templates" not in f
    ]
    from lineage_wiki.storage.manifest import save_manifest

    save_manifest(wiki_root, manifest)
    page = wiki_root / "okf" / "report-templates" / "example-daily-report.md"
    page.write_text("---\ntype: Report Template\ntitle: Mine\n---\n# Mine\n")

    cfg2 = example_cfg.model_copy(deep=True)
    cfg2.sources.reports[0].source_mapping_notes = "New notes."
    result = run_update(cfg2, wiki_root, now=LATER)

    assert "okf/report-templates/example-daily-report.md" in result.skipped
    assert page.read_text().startswith("---\ntype: Report Template\ntitle: Mine")
