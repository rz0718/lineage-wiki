from pathlib import Path

from lineage_wiki.okf.validator import validate_tree
from lineage_wiki.storage.manifest import Manifest, save_manifest

VALID_METRIC = """\
---
type: Metric
title: Example
description: Example metric.
status: draft
tags:
  - example
timestamp: 2026-07-03T00:00:00Z
---

# Example

## Definition

A thing.

## Business Meaning

Meaningful.

## Calculation Logic

Documented elsewhere.

## Unit

IDR

## Grain

daily

## Source References

None.

## Used By

Nobody.

## Caveats

None.
"""


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_valid_page_passes(tmp_path):
    _write(tmp_path, "okf/metrics/example.md", VALID_METRIC)
    report = validate_tree(tmp_path)
    assert report.errors == []


def test_missing_frontmatter_fails(tmp_path):
    _write(tmp_path, "okf/metrics/bad.md", "# No frontmatter\n")
    report = validate_tree(tmp_path)
    assert any("missing YAML frontmatter" in i.message for i in report.errors)


def test_empty_type_fails(tmp_path):
    _write(tmp_path, "okf/metrics/bad.md", "---\ntitle: X\n---\n# X\n")
    report = validate_tree(tmp_path)
    assert any("no non-empty `type`" in i.message for i in report.errors)


def test_invalid_type_fails(tmp_path):
    _write(tmp_path, "okf/metrics/bad.md", "---\ntype: metric\n---\n# X\n")
    report = validate_tree(tmp_path)
    assert any("invalid page type 'metric'" in i.message for i in report.errors)


def test_broken_body_link_fails(tmp_path):
    content = VALID_METRIC.replace("A thing.", "See [other](../frameworks/missing.md).")
    _write(tmp_path, "okf/metrics/example.md", content)
    report = validate_tree(tmp_path)
    assert any("broken link -> ../frameworks/missing.md" in i.message for i in report.errors)


def test_broken_frontmatter_ref_fails(tmp_path):
    content = VALID_METRIC.replace(
        "timestamp: 2026-07-03T00:00:00Z",
        "timestamp: 2026-07-03T00:00:00Z\nframework_refs:\n  - ../frameworks/missing.md",
    )
    _write(tmp_path, "okf/metrics/example.md", content)
    report = validate_tree(tmp_path)
    assert any(
        "broken frontmatter ref (framework_refs) -> ../frameworks/missing.md" in i.message
        for i in report.errors
    )


def test_nested_frontmatter_ref_resolves(tmp_path):
    _write(tmp_path, "okf/metrics/example.md", VALID_METRIC)
    content = VALID_METRIC.replace(
        "timestamp: 2026-07-03T00:00:00Z",
        "timestamp: 2026-07-03T00:00:00Z\n"
        "implementation_refs:\n  - repo: x\n    code_link: ../code-links/missing.md",
    )
    _write(tmp_path, "okf/metrics/other.md", content)
    report = validate_tree(tmp_path)
    assert any(
        "broken frontmatter ref (implementation_refs) -> ../code-links/missing.md" in i.message
        for i in report.errors
    )


def test_placeholder_outside_known_gaps_fails(tmp_path):
    content = VALID_METRIC.replace("A thing.", "TODO write this.")
    _write(tmp_path, "okf/metrics/example.md", content)
    report = validate_tree(tmp_path)
    assert any("placeholder 'TODO'" in i.message for i in report.errors)


def test_path_tbd_token_fails(tmp_path):
    content = VALID_METRIC.replace("A thing.", "Code lives at <path-TBD> for now.")
    _write(tmp_path, "okf/metrics/example.md", content)
    report = validate_tree(tmp_path)
    assert any("placeholder 'TBD'" in i.message for i in report.errors)


def test_placeholder_inside_known_gaps_passes(tmp_path):
    content = VALID_METRIC.replace(
        "## Caveats\n\nNone.",
        "## Caveats\n\nNone.\n\n## Known Gaps\n\n- TBD: confirm the unit.",
    )
    _write(tmp_path, "okf/metrics/example.md", content)
    report = validate_tree(tmp_path)
    assert report.errors == []


def test_placeholder_in_inline_code_passes(tmp_path):
    content = VALID_METRIC.replace(
        "A thing.", "Pages previously marked `<path-TBD>` are resolved."
    )
    _write(tmp_path, "okf/metrics/example.md", content)
    report = validate_tree(tmp_path)
    assert report.errors == []


def test_missing_section_is_warning_for_unmanaged_page(tmp_path):
    content = VALID_METRIC.replace("## Caveats\n\nNone.\n", "")
    _write(tmp_path, "okf/metrics/example.md", content)
    report = validate_tree(tmp_path)
    assert report.errors == []
    assert any("missing required `## Caveats`" in i.message for i in report.warnings)
    assert report.failed(strict=True)
    assert not report.failed(strict=False)


def test_missing_section_is_error_for_managed_page(tmp_path):
    content = VALID_METRIC.replace("## Caveats\n\nNone.\n", "")
    _write(tmp_path, "okf/metrics/example.md", content)
    save_manifest(
        tmp_path,
        Manifest(
            chain_id="x",
            chain_slug="x",
            generated_files=["okf/metrics/example.md"],
        ),
    )
    report = validate_tree(tmp_path)
    assert any("missing required `## Caveats`" in i.message for i in report.errors)


def test_empty_tree_fails(tmp_path):
    report = validate_tree(tmp_path)
    assert report.errors


METRICS_INDEX_WITH_LINK = """\
---
type: Index
title: OKF Metrics Registry
---

# Registry

| Term | Definition lives at |
|---|---|
| Example | [Example](example.md) |
"""


def test_index_membership_warning_for_unmanaged_page(tmp_path):
    _write(tmp_path, "okf/metrics/example.md", VALID_METRIC)
    _write(
        tmp_path,
        "okf/metrics/index.md",
        "---\ntype: Index\ntitle: Registry\n---\n\n# Registry\n\nNothing here.\n",
    )
    report = validate_tree(tmp_path)
    assert report.errors == []
    assert any(
        "not listed in the metrics registry" in i.message for i in report.warnings
    )


def test_index_membership_error_for_managed_page(tmp_path):
    _write(tmp_path, "okf/metrics/example.md", VALID_METRIC)
    _write(
        tmp_path,
        "okf/metrics/index.md",
        "---\ntype: Index\ntitle: Registry\n---\n\n# Registry\n\nNothing here.\n",
    )
    save_manifest(
        tmp_path,
        Manifest(
            chain_id="x",
            chain_slug="x",
            generated_files=["okf/metrics/example.md"],
            managed_indexes=["okf/metrics/index.md"],
        ),
    )
    report = validate_tree(tmp_path)
    assert any("not listed in the metrics registry" in i.message for i in report.errors)


def test_index_membership_warning_when_index_is_hand_written(tmp_path):
    """A tool-managed page missing from a hand-written index is a human
    follow-up (the tool never edits protected indexes), not an error."""
    _write(tmp_path, "okf/metrics/example.md", VALID_METRIC)
    _write(
        tmp_path,
        "okf/metrics/index.md",
        "---\ntype: Index\ntitle: Registry\n---\n\n# Registry\n\nNothing here.\n",
    )
    save_manifest(
        tmp_path,
        Manifest(chain_id="x", chain_slug="x", generated_files=["okf/metrics/example.md"]),
    )
    report = validate_tree(tmp_path)
    assert report.errors == []
    assert any(
        "add the entry manually" in i.message for i in report.warnings
    )


def test_index_membership_satisfied_by_link(tmp_path):
    _write(tmp_path, "okf/metrics/example.md", VALID_METRIC)
    _write(tmp_path, "okf/metrics/index.md", METRICS_INDEX_WITH_LINK)
    report = validate_tree(tmp_path)
    assert report.errors == []
    assert not any("not listed" in i.message for i in report.issues)
