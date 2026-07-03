from lineage_wiki.okf.schemas import (
    OkfPage,
    parse_page,
    render_frontmatter,
    split_frontmatter,
)


def test_render_and_parse_round_trip():
    fm = {
        "type": "Framework",
        "title": "Gold PnL Framework",
        "description": "End-to-end Gold PnL computation methodology.",
        "owner": "Treasury",
        "status": "draft",
        "tags": ["gold", "pnl"],
        "timestamp": "2026-07-03T00:00:00Z",
        "implementation_refs": [
            {
                "repo": "gold-pnl",
                "primary": True,
                "ref": "main",
                "path": "/",
                "code_link": "../code-links/gold-pnl-engine.md",
            }
        ],
        "change_check": "../change-checks/gold-pnl-review-rules.md",
        "approved_by": None,
    }
    text = render_frontmatter(fm) + "\n# Body\n"
    parsed = parse_page(text)
    assert parsed.fm_error is None
    assert parsed.frontmatter["type"] == "Framework"
    assert parsed.frontmatter["tags"] == ["gold", "pnl"]
    assert parsed.frontmatter["implementation_refs"][0]["primary"] is True
    assert (
        parsed.frontmatter["implementation_refs"][0]["code_link"]
        == "../code-links/gold-pnl-engine.md"
    )
    assert parsed.frontmatter["approved_by"] is None
    assert parsed.body.strip() == "# Body"


def test_scalar_quoting_survives_yaml():
    fm = {
        "type": "Metric",
        "title": "GTV (Gross Transaction Value)",
        "description": "Total value: buys and sells, before fees. 100% #verified",
    }
    parsed = parse_page(render_frontmatter(fm) + "\nbody\n")
    assert parsed.fm_error is None
    assert parsed.frontmatter["title"] == "GTV (Gross Transaction Value)"
    assert (
        parsed.frontmatter["description"]
        == "Total value: buys and sells, before fees. 100% #verified"
    )


def test_split_frontmatter_absent():
    fm_text, body = split_frontmatter("# Just a body\n")
    assert fm_text is None
    assert body == "# Just a body\n"


def test_parse_page_reports_bad_yaml():
    parsed = parse_page("---\ntype: [unclosed\n---\nbody\n")
    assert parsed.frontmatter is None
    assert parsed.fm_error


def test_okf_page_model_defaults():
    page = OkfPage(
        id="gold-wac",
        slug="gold-wac",
        type="Component",
        title="Gold WAC",
        description="Weighted average cost.",
        body="# Gold WAC\n",
    )
    assert page.status == "draft"
    assert page.tags == []
    assert page.source_refs == []
