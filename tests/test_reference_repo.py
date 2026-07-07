"""Acceptance check against the sibling OKF reference catalog.

The hand-written reference pages predate the strict per-type section
contract, so section issues surface as warnings there — but frontmatter,
types, links, refs, and placeholders must all be clean.
"""

from pathlib import Path

import pytest

from lineage_wiki.okf.validator import validate_tree

REFERENCE = Path(__file__).resolve().parents[2] / "example-okf-catalog"


@pytest.mark.skipif(not REFERENCE.exists(), reason="reference repo not checked out")
def test_reference_catalog_has_no_errors():
    report = validate_tree(REFERENCE)
    assert report.n_pages > 0
    assert report.errors == [], [str(i) for i in report.errors]
