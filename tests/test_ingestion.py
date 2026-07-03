"""Milestone 4: raw-doc and local-repo ingestion with temporary fixtures."""

from pathlib import Path

import pytest

from lineage_wiki.agent.runner import run_generate
from lineage_wiki.config import RawDocSource, RepoSource
from lineage_wiki.connectors import SourceUnavailableError
from lineage_wiki.connectors.local_repo_connector import load_local_repo
from lineage_wiki.connectors.raw_doc_connector import load_raw_docs
from lineage_wiki.ingestion.code_indexer import index_repo
from lineage_wiki.ingestion.source_loader import load_sources

from .conftest import FIXED_NOW

PIPELINE_CODE = '''\
"""Example pipeline."""

SNAPSHOT_TABLE = "analytics.example_daily_snapshot"


def compute_daily_snapshot(rows):
    return len(rows)
'''


@pytest.fixture
def wiki_root(tmp_path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


@pytest.fixture
def fixture_repo(tmp_path) -> Path:
    """Temporary local clone next to the wiki root (matches ../example-pipeline)."""
    repo = tmp_path / "example-pipeline"
    (repo / "pipeline").mkdir(parents=True)
    (repo / "pipeline" / "main.py").write_text(PIPELINE_CODE)
    (repo / "pipeline" / "utils.py").write_text("HELPER = True\n")
    return repo


@pytest.fixture
def fixture_doc(wiki_root) -> Path:
    doc = wiki_root / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Example Methodology\n\nValue = quantity x price.\n")
    return doc


# --- raw_doc_connector ---------------------------------------------------------


def test_raw_doc_loading(wiki_root, fixture_doc):
    result = load_raw_docs(
        [RawDocSource(path="raw_files/example/methodology.md")], wiki_root
    )
    assert result.missing == []
    (item,) = result.items
    assert item.id == "raw-doc:raw_files/example/methodology.md"
    assert item.source_type == "raw_doc"
    assert item.title == "Example Methodology"
    assert "quantity x price" in item.content
    assert item.metadata["doc_type"] == "methodology"
    assert item.fingerprint.startswith("sha256:")


def test_missing_optional_raw_doc_is_reported(wiki_root):
    result = load_raw_docs([RawDocSource(path="raw_files/nope.md")], wiki_root)
    assert result.items == []
    assert result.missing == ["raw_files/nope.md"]


def test_missing_required_raw_doc_fails(wiki_root):
    with pytest.raises(SourceUnavailableError, match="raw_files/nope.md"):
        load_raw_docs([RawDocSource(path="raw_files/nope.md", required=True)], wiki_root)


# --- local_repo_connector -------------------------------------------------------


def _repo_source(**overrides) -> RepoSource:
    base = dict(
        name="example-pipeline",
        local_path="../example-pipeline",
        paths=["pipeline/main.py", "pipeline/missing.py"],
        symbols=["compute_daily_snapshot", "SNAPSHOT_TABLE", "ghost_symbol"],
        required=False,
    )
    base.update(overrides)
    return RepoSource(**base)


def test_local_repo_loading(wiki_root, fixture_repo):
    result = load_local_repo(_repo_source(), wiki_root)
    assert result.available is True
    assert result.local_dir == fixture_repo.resolve()
    (item,) = result.files
    assert item.id == "local-repo:example-pipeline:pipeline/main.py"
    assert item.source_type == "local_repo"
    assert item.metadata["lines"] == len(PIPELINE_CODE.splitlines())
    assert item.fingerprint.startswith("sha256:")
    assert result.missing_paths == ["pipeline/missing.py"]


def test_repo_without_local_path_is_reference_only(wiki_root):
    result = load_local_repo(_repo_source(local_path=None), wiki_root)
    assert result.available is False
    assert result.files == []


def test_missing_optional_repo_is_unavailable(wiki_root):
    result = load_local_repo(_repo_source(), wiki_root)  # no fixture_repo
    assert result.available is False


def test_missing_required_repo_fails(wiki_root):
    with pytest.raises(SourceUnavailableError, match="example-pipeline"):
        load_local_repo(_repo_source(required=True), wiki_root)


# --- code_indexer ----------------------------------------------------------------


def test_code_indexer_locates_symbols(wiki_root, fixture_repo):
    load = load_local_repo(_repo_source(), wiki_root)
    index = index_repo(load)

    hit = index.best_hit("compute_daily_snapshot")
    assert hit.kind == "def"
    assert hit.location == "pipeline/main.py:6"

    hit = index.best_hit("SNAPSHOT_TABLE")
    assert hit.kind == "assignment"
    assert hit.line == 3

    assert index.missing_symbols == ["ghost_symbol"]
    assert index.best_hit("ghost_symbol") is None


# --- source_loader ------------------------------------------------------------------


def test_source_loader_builds_bundle(example_cfg, wiki_root, fixture_repo, fixture_doc):
    bundle = load_sources(example_cfg, wiki_root)
    assert [d.source_uri for d in bundle.raw_docs] == ["raw_files/example/methodology.md"]
    assert bundle.missing_raw_docs == []
    assert bundle.repo_load("example-pipeline").available is True

    items = bundle.all_items()
    types = {i.source_type for i in items}
    assert types == {"raw_doc", "local_repo", "human_note", "report"}
    assert all(i.fingerprint for i in items)
    note = next(i for i in items if i.source_type == "human_note")
    assert note.title == "Review caveat"


# --- evidence-aware skeleton generation ------------------------------------------------


def test_generated_pages_reflect_loaded_evidence(example_cfg, wiki_root, fixture_repo, fixture_doc):
    result = run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    assert result.report.errors == []

    framework = (wiki_root / "okf" / "frameworks" / "example-chain.md").read_text()
    assert "source_refs:\n  - ../../raw_files/example/methodology.md" in framework
    assert "methodology, ingested (3 lines; title: Example Methodology)" in framework
    assert "| Raw methodology | Ingested — 1 doc(s) loaded; extraction pending |" in framework
    assert "| Source code | Loaded — 2 file(s) from 1 repo(s); cross-check pending |" in framework
    assert "Source methodology has not been ingested" not in framework
    assert "have not been extracted from the ingested raw docs" in framework

    code_link = (wiki_root / "okf" / "code-links" / "example-pipeline-engine.md").read_text()
    assert "| `pipeline/main.py` | Loaded | 7 |" in code_link
    assert "| `compute_daily_snapshot` | `pipeline/main.py:6` | def |" in code_link
    assert "loaded from the local clone" in code_link


def test_unavailable_optional_sources_become_known_gaps(example_cfg, wiki_root):
    result = run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    assert result.report.errors == []

    gaps = "\n".join(result.gaps)
    assert "Configured raw doc `raw_files/example/methodology.md` was not found" in gaps
    assert "Repo `example-pipeline` has no local clone available" in gaps

    framework = (wiki_root / "okf" / "frameworks" / "example-chain.md").read_text()
    assert "| Source code | Not loaded — no local clone available |" in framework

    code_link = (wiki_root / "okf" / "code-links" / "example-pipeline-engine.md").read_text()
    assert "No local clone was available at generate time" in code_link
    assert "unverified" in code_link


def test_missing_path_and_symbol_become_known_gaps(example_cfg, wiki_root, fixture_repo):
    cfg = example_cfg.model_copy(deep=True)
    cfg.sources.repos[0].paths.append("pipeline/missing.py")
    cfg.sources.repos[0].symbols.append("ghost_symbol")

    result = run_generate(cfg, wiki_root, now=FIXED_NOW)
    gaps = "\n".join(result.gaps)
    assert "Configured path `pipeline/missing.py` was not found in repo `example-pipeline`" in gaps
    assert "Configured symbol `ghost_symbol` was not found" in gaps

    code_link = (wiki_root / "okf" / "code-links" / "example-pipeline-engine.md").read_text()
    assert "| `pipeline/missing.py` | Not found | — |" in code_link
    assert "| `ghost_symbol` | Not found in loaded paths | — |" in code_link


def test_generate_fails_clearly_on_required_missing_repo(example_cfg, wiki_root):
    cfg = example_cfg.model_copy(deep=True)
    cfg.sources.repos[0].required = True
    with pytest.raises(SourceUnavailableError, match="no local clone at ../example-pipeline"):
        run_generate(cfg, wiki_root, now=FIXED_NOW)


def test_git_head_recorded_when_clone_is_a_git_repo(example_cfg, wiki_root, fixture_repo):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=fixture_repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=fixture_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=fixture_repo,
        check=True,
    )
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    code_link = (wiki_root / "okf" / "code-links" / "example-pipeline-engine.md").read_text()
    assert "| Verified head | `" in code_link
