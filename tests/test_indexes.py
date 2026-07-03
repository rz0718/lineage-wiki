from lineage_wiki.agent.runner import run_generate
from lineage_wiki.constants import INDEX_FILES
from lineage_wiki.okf.indexes import build_all_indexes, scan_pages
from lineage_wiki.okf.schemas import parse_page

from .conftest import FIXED_NOW


def test_all_eight_indexes_generated(example_cfg, tmp_path):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    for rel in INDEX_FILES:
        path = tmp_path / "okf" / rel
        assert path.exists(), rel
        fm = parse_page(path.read_text()).frontmatter
        assert fm["type"] == "Index", rel


def test_indexes_register_generated_pages(example_cfg, tmp_path):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    okf = tmp_path / "okf"

    root_index = (okf / "index.md").read_text()
    assert "[Example Chain Framework](frameworks/example-chain.md)" in root_index
    assert "(outputs/example-daily-snapshot.md)" in root_index
    assert "(code-links/example-pipeline-engine.md)" in root_index
    assert "(report-templates/example-daily-report.md)" in root_index
    assert "(change-checks/example-chain-review-rules.md)" in root_index
    assert "(metrics/example-metric.md)" in root_index

    frameworks_index = (okf / "frameworks" / "index.md").read_text()
    assert "[Example Chain Framework](example-chain.md)" in frameworks_index

    outputs_index = (okf / "outputs" / "index.md").read_text()
    assert "## Example Chain Outputs" in outputs_index
    assert "(example-daily-snapshot.md)" in outputs_index

    metrics_index = (okf / "metrics" / "index.md").read_text()
    assert "| Example Metric | [Example Metric](example-metric.md) |" in metrics_index
    assert "| Example Chain | Framework | [Example Chain Framework](../frameworks/example-chain.md) |" in metrics_index


def test_empty_tree_produces_stub_indexes(tmp_path):
    okf = tmp_path / "okf"
    okf.mkdir()
    drafts = build_all_indexes(okf, FIXED_NOW)
    assert len(drafts) == len(INDEX_FILES)
    by_path = {d.rel_path: d.content for d in drafts}
    assert "No framework pages yet." in by_path["okf/frameworks/index.md"]
    assert "None registered yet." in by_path["okf/metrics/index.md"]


def test_index_generation_is_idempotent(example_cfg, tmp_path):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    first = {d.rel_path: d.content for d in build_all_indexes(tmp_path / "okf", FIXED_NOW)}
    # writing indexes and rescanning must not change them
    second = {d.rel_path: d.content for d in build_all_indexes(tmp_path / "okf", FIXED_NOW)}
    assert first == second


def test_generated_tree_has_full_index_membership(example_cfg, tmp_path):
    from lineage_wiki.okf.validator import validate_tree

    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    report = validate_tree(tmp_path)
    assert not any("not listed" in i.message for i in report.issues)


def test_table_cells_escape_pipes(example_cfg, tmp_path):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    (tmp_path / "okf" / "components" / "weird.md").write_text(
        "---\n"
        "type: Component\n"
        "title: Weird Component\n"
        "description: Uses A | B logic\n"
        "framework_refs:\n"
        "  - ../frameworks/example-chain.md\n"
        "---\n\n# Weird Component\n"
    )
    drafts = {d.rel_path: d.content for d in build_all_indexes(tmp_path / "okf", FIXED_NOW)}
    assert "Uses A \\| B logic" in drafts["okf/components/index.md"]


def test_human_metric_page_gets_registered(example_cfg, tmp_path):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    (tmp_path / "okf" / "metrics" / "hand-metric.md").write_text(
        "---\n"
        "type: Metric\n"
        "title: Hand Metric\n"
        "description: Written by a human.\n"
        "framework_refs:\n"
        "  - ../frameworks/example-chain.md\n"
        "---\n\n# Hand Metric\n"
    )
    drafts = {d.rel_path: d.content for d in build_all_indexes(tmp_path / "okf", FIXED_NOW)}
    registry = drafts["okf/metrics/index.md"]
    assert "| Hand Metric | [Hand Metric](hand-metric.md) |" in registry


def test_scan_skips_index_and_broken_pages(example_cfg, tmp_path):
    run_generate(example_cfg, tmp_path, now=FIXED_NOW)
    (tmp_path / "okf" / "metrics" / "broken.md").write_text("no frontmatter\n")
    pages = scan_pages(tmp_path / "okf")
    rels = [p.rel for p in pages]
    assert "index.md" not in rels
    assert "metrics/broken.md" not in rels
    assert "frameworks/example-chain.md" in rels
