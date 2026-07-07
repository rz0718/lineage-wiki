import re

from lineage_wiki.config import ComponentInput
from lineage_wiki.constants import DIR_TO_TYPE, REQUIRED_SECTIONS
from lineage_wiki.okf.schemas import parse_page
from lineage_wiki.okf.templates import build_context, plan_chain_pages, render_component_page
from lineage_wiki.okf.validator import _placeholder_violations

from .conftest import FIXED_NOW

HEADINGS = re.compile(r"^## (.+?)\s*$", re.MULTILINE)

SNAPSHOT_TABLE = "example-project.analytics.example_daily_snapshot"


def component_cfg(example_cfg):
    """Example config extended with two configured components: one fully
    linked, one bare."""
    cfg = example_cfg.model_copy(deep=True)
    cfg.sources.components = [
        ComponentInput(
            name="Example Total Value",
            description="Total value per asset per day.",
            code_ref="example-pipeline",
            output_refs=[SNAPSHOT_TABLE],
        ),
        ComponentInput(name="Example Adjustment"),
    ]
    return cfg


def _assert_required_sections(page_type: str, body: str) -> None:
    headings = [h.lower() for h in HEADINGS.findall(body)]
    for required in REQUIRED_SECTIONS[page_type]:
        assert any(
            h.startswith(required.lower()) for h in headings
        ), f"{page_type} template missing section {required!r}"


def test_plan_covers_expected_pages(example_cfg, tmp_path):
    plan = plan_chain_pages(example_cfg, tmp_path, FIXED_NOW)
    paths = [p.rel_path for p in plan.pages]
    assert paths == [
        "okf/frameworks/example-chain.md",
        "okf/code-links/example-pipeline-engine.md",
        "okf/outputs/example-daily-snapshot.md",
        "okf/report-templates/example-daily-report.md",
        "okf/change-checks/example-chain-review-rules.md",
        "okf/metrics/example-metric.md",
    ]
    assert plan.gaps  # scaffold always records gaps


def test_every_generated_page_is_valid_okf(example_cfg, tmp_path):
    plan = plan_chain_pages(example_cfg, tmp_path, FIXED_NOW)
    for draft in plan.pages:
        parsed = parse_page(draft.content)
        assert parsed.fm_error is None, draft.rel_path
        assert parsed.frontmatter, draft.rel_path
        page_dir = draft.rel_path.split("/")[1]
        assert parsed.frontmatter["type"] == DIR_TO_TYPE[page_dir], draft.rel_path
        # unquoted timestamps parse as datetime, matching the catalog style
        assert str(parsed.frontmatter["timestamp"]).startswith(FIXED_NOW[:10])
        _assert_required_sections(parsed.frontmatter["type"], parsed.body)


def test_no_placeholders_outside_known_gaps(example_cfg, tmp_path):
    plan = plan_chain_pages(example_cfg, tmp_path, FIXED_NOW)
    for draft in plan.pages:
        parsed = parse_page(draft.content)
        assert _placeholder_violations(parsed.body) == [], draft.rel_path


def test_component_template(example_cfg, tmp_path):
    ctx = build_context(example_cfg, tmp_path, FIXED_NOW)
    content = render_component_page(
        ctx,
        title="Example Average Cost",
        description="Running average cost basis.",
        code_link_rel=ctx.code_links[0][1],
    )
    parsed = parse_page(content)
    assert parsed.frontmatter["type"] == "Component"
    assert parsed.frontmatter["framework_refs"] == ["../frameworks/example-chain.md"]
    assert parsed.frontmatter["code_refs"] == ["../code-links/example-pipeline-engine.md"]
    _assert_required_sections("Component", parsed.body)
    assert "| Component | What it represents | Driving factors | Code location |" in parsed.body


def test_framework_refs_point_at_planned_pages(example_cfg, tmp_path):
    plan = plan_chain_pages(example_cfg, tmp_path, FIXED_NOW)
    framework = next(p for p in plan.pages if "frameworks/" in p.rel_path)
    fm = parse_page(framework.content).frontmatter
    assert fm["implementation_refs"][0]["code_link"] == "../code-links/example-pipeline-engine.md"
    assert fm["output_refs"][0]["table"] == "example-project.analytics.example_daily_snapshot"
    assert fm["output_refs"][0]["output"] == "../outputs/example-daily-snapshot.md"
    assert fm["report_refs"] == ["../report-templates/example-daily-report.md"]
    assert fm["change_check"] == "../change-checks/example-chain-review-rules.md"
    # configured raw doc does not exist in tmp_path -> must NOT be a source_ref
    assert "source_refs" not in fm


def test_configured_components_are_planned_after_framework(example_cfg, tmp_path):
    plan = plan_chain_pages(component_cfg(example_cfg), tmp_path, FIXED_NOW)
    paths = [p.rel_path for p in plan.pages]
    assert paths[:3] == [
        "okf/frameworks/example-chain.md",
        "okf/components/example-total-value.md",
        "okf/components/example-adjustment.md",
    ]
    assert "No component pages exist yet" not in " ".join(plan.gaps)


def test_generated_component_pages_are_valid_okf(example_cfg, tmp_path):
    plan = plan_chain_pages(component_cfg(example_cfg), tmp_path, FIXED_NOW)
    for draft in plan.pages:
        parsed = parse_page(draft.content)
        assert parsed.fm_error is None, draft.rel_path
        page_dir = draft.rel_path.split("/")[1]
        assert parsed.frontmatter["type"] == DIR_TO_TYPE[page_dir], draft.rel_path
        _assert_required_sections(parsed.frontmatter["type"], parsed.body)
        assert _placeholder_violations(parsed.body) == [], draft.rel_path


def test_component_page_carries_configured_refs(example_cfg, tmp_path):
    plan = plan_chain_pages(component_cfg(example_cfg), tmp_path, FIXED_NOW)
    linked = next(p for p in plan.pages if "example-total-value" in p.rel_path)
    fm = parse_page(linked.content).frontmatter
    assert fm["framework_refs"] == ["../frameworks/example-chain.md"]
    assert fm["code_refs"] == ["../code-links/example-pipeline-engine.md"]
    assert fm["output_refs"] == ["../outputs/example-daily-snapshot.md"]
    assert "Total value per asset per day." in linked.content
    assert f"`{SNAPSHOT_TABLE}`" in linked.content

    bare = next(p for p in plan.pages if "example-adjustment" in p.rel_path)
    bare_fm = parse_page(bare.content).frontmatter
    assert "code_refs" not in bare_fm and "output_refs" not in bare_fm
    assert "No code link is associated with this component yet" in bare.content


def test_framework_page_lists_configured_components(example_cfg, tmp_path):
    plan = plan_chain_pages(component_cfg(example_cfg), tmp_path, FIXED_NOW)
    framework = next(p for p in plan.pages if "frameworks/" in p.rel_path)
    fm = parse_page(framework.content).frontmatter
    assert fm["component_refs"] == [
        "../components/example-total-value.md",
        "../components/example-adjustment.md",
    ]
    components = framework.content.split("## Components")[1].split("## Implementation")[0]
    assert "[Example Total Value](../components/example-total-value.md)" in components
    assert "— Total value per asset per day." in components
    assert "[Example Adjustment](../components/example-adjustment.md)" in components
    assert "No component pages have been generated" not in components


def test_no_component_config_keeps_placeholder(example_cfg, tmp_path):
    plan = plan_chain_pages(example_cfg, tmp_path, FIXED_NOW)
    framework = next(p for p in plan.pages if "frameworks/" in p.rel_path)
    assert "No component pages have been generated for this framework yet." in framework.content
    assert "component_refs" not in parse_page(framework.content).frontmatter
    assert any("No component pages exist yet" in gap for gap in plan.gaps)


def test_unmatched_component_refs_become_known_gaps(example_cfg, tmp_path):
    cfg = component_cfg(example_cfg)
    cfg.sources.components[0].code_ref = "no-such-repo"
    cfg.sources.components[0].output_refs = ["example-project.analytics.no_such_table"]
    plan = plan_chain_pages(cfg, tmp_path, FIXED_NOW)

    gaps = " ".join(plan.gaps)
    assert "code_ref `no-such-repo`" in gaps
    assert "`example-project.analytics.no_such_table`" in gaps
    assert "Component `Example Adjustment` has no configured description." in plan.gaps

    # unresolved refs never land in frontmatter
    page = next(p for p in plan.pages if "example-total-value" in p.rel_path)
    fm = parse_page(page.content).frontmatter
    assert "code_refs" not in fm and "output_refs" not in fm


def test_component_output_refs_match_backticked_table_spellings(example_cfg, tmp_path):
    """A supported backticked/padded table spelling on either side of the
    config must still resolve to the same output page."""
    cfg = component_cfg(example_cfg)
    cfg.sources.bigquery.tables = [f"`{SNAPSHOT_TABLE}`"]
    cfg.sources.components[0].output_refs = [f" {SNAPSHOT_TABLE} "]
    plan = plan_chain_pages(cfg, tmp_path, FIXED_NOW)

    page = next(p for p in plan.pages if "example-total-value" in p.rel_path)
    fm = parse_page(page.content).frontmatter
    assert fm["output_refs"] == ["../outputs/example-daily-snapshot.md"]
    assert not any("references output" in gap for gap in plan.gaps)


def test_component_output_refs_match_default_project_spellings(example_cfg, tmp_path):
    """With sources.bigquery.project set, a two-part `dataset.table` and its
    fully-qualified equivalent must resolve to the same output page."""
    cfg = component_cfg(example_cfg)
    assert cfg.sources.bigquery.project == "example-project"
    cfg.sources.bigquery.tables = ["analytics.example_daily_snapshot"]
    cfg.sources.components[0].output_refs = [SNAPSHOT_TABLE]  # fully qualified
    plan = plan_chain_pages(cfg, tmp_path, FIXED_NOW)

    page = next(p for p in plan.pages if "example-total-value" in p.rel_path)
    fm = parse_page(page.content).frontmatter
    assert fm["output_refs"] == ["../outputs/example-daily-snapshot.md"]
    assert not any("references output" in gap for gap in plan.gaps)

    # and the reverse: two-part component ref against a fully-qualified table
    cfg.sources.bigquery.tables = [SNAPSHOT_TABLE]
    cfg.sources.components[0].output_refs = ["analytics.example_daily_snapshot"]
    plan = plan_chain_pages(cfg, tmp_path, FIXED_NOW)
    page = next(p for p in plan.pages if "example-total-value" in p.rel_path)
    assert parse_page(page.content).frontmatter["output_refs"] == [
        "../outputs/example-daily-snapshot.md"
    ]


def test_change_check_impacted_pages_include_components(example_cfg, tmp_path):
    plan = plan_chain_pages(component_cfg(example_cfg), tmp_path, FIXED_NOW)
    check = next(p for p in plan.pages if "change-checks/" in p.rel_path)
    assert "[Example Total Value](../components/example-total-value.md)" in check.content


def test_existing_raw_doc_becomes_source_ref(example_cfg, tmp_path):
    doc = tmp_path / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Methodology\n")
    plan = plan_chain_pages(example_cfg, tmp_path, FIXED_NOW)
    framework = next(p for p in plan.pages if "frameworks/" in p.rel_path)
    fm = parse_page(framework.content).frontmatter
    assert fm["source_refs"] == ["../../raw_files/example/methodology.md"]
