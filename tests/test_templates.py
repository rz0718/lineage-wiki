import re

from lineage_wiki.constants import DIR_TO_TYPE, REQUIRED_SECTIONS
from lineage_wiki.okf.schemas import parse_page
from lineage_wiki.okf.templates import build_context, plan_chain_pages, render_component_page
from lineage_wiki.okf.validator import _placeholder_violations

from .conftest import FIXED_NOW

HEADINGS = re.compile(r"^## (.+?)\s*$", re.MULTILINE)


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
        title="Example WAC",
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


def test_existing_raw_doc_becomes_source_ref(example_cfg, tmp_path):
    doc = tmp_path / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Methodology\n")
    plan = plan_chain_pages(example_cfg, tmp_path, FIXED_NOW)
    framework = next(p for p in plan.pages if "frameworks/" in p.rel_path)
    fm = parse_page(framework.content).frontmatter
    assert fm["source_refs"] == ["../../raw_files/example/methodology.md"]
