"""Snapshot tests: deterministic generation must be byte-stable.

Regenerate after an intentional template change with:

    LW_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_snapshots.py
"""

import os
import shutil
from pathlib import Path

from lineage_wiki.agent.runner import run_generate

from .conftest import FIXED_NOW

SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "example-chain"


def _generate_tree(cfg, root: Path) -> dict[str, str]:
    run_generate(cfg, root, now=FIXED_NOW)
    okf = root / "okf"
    return {
        p.relative_to(okf).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(okf.rglob("*.md"))
    }


def test_generated_tree_matches_snapshots(example_cfg, tmp_path):
    generated = _generate_tree(example_cfg, tmp_path)

    if os.environ.get("LW_UPDATE_SNAPSHOTS"):
        if SNAPSHOT_DIR.exists():
            shutil.rmtree(SNAPSHOT_DIR)
        for rel, content in generated.items():
            target = SNAPSHOT_DIR / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    assert SNAPSHOT_DIR.exists(), "snapshots missing — run with LW_UPDATE_SNAPSHOTS=1"
    expected = {
        p.relative_to(SNAPSHOT_DIR).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(SNAPSHOT_DIR.rglob("*.md"))
    }
    assert sorted(generated) == sorted(expected)
    for rel in expected:
        assert generated[rel] == expected[rel], f"snapshot drift in {rel}"


def test_generation_is_deterministic(example_cfg, tmp_path):
    first = _generate_tree(example_cfg, tmp_path / "a")
    second = _generate_tree(example_cfg, tmp_path / "b")
    assert first == second
