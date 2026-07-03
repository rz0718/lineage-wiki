"""Deterministic init/generate/update runs (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import ChainConfig
from ..constants import (
    AGENT_INSTRUCTIONS_BLOCK,
    AGENT_INSTRUCTIONS_MARKER,
    MANIFEST_DIR,
    OKF_SUBDIRS,
    PROMPT_STUBS,
)
from ..examples import EXAMPLE_CHAIN_YAML
from ..ingestion.fingerprints import compute_fingerprints
from ..okf.indexes import build_all_indexes
from ..okf.templates import ChainPlan, plan_chain_pages
from ..okf.validator import ValidationReport, validate_tree
from ..storage.manifest import (
    Manifest,
    SourceChanges,
    compute_snapshot,
    diff_fingerprints,
    load_manifest,
    manifests_equal_ignoring_run_time,
    save_manifest,
)
from ..storage.runs import RunRecord, okf_git_head, write_run
from ..storage.snapshots import materially_equal
from ..util import now_stamp
from .planner import build_impact_plan


class GenerateError(Exception):
    """Raised when a generate/update run must stop."""


# --- init ---------------------------------------------------------------------


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def run_init(root: str | Path, *, agents: bool = False, now: str | None = None) -> InitResult:
    """Scaffold a target repo: config examples, prompt stubs, okf/ structure."""
    root = Path(root).resolve()
    now = now or now_stamp()
    result = InitResult()

    def write_if_missing(rel: str, content: str) -> None:
        path = root / rel
        if path.exists():
            result.skipped.append(rel)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        result.created.append(rel)

    write_if_missing(f"{MANIFEST_DIR}/config.example.yml", EXAMPLE_CHAIN_YAML)
    for name, content in PROMPT_STUBS.items():
        write_if_missing(f"{MANIFEST_DIR}/prompts/{name}", content)
    write_if_missing("chains/example.yml", EXAMPLE_CHAIN_YAML)

    okf_dir = root / "okf"
    okf_missing = not (okf_dir / "index.md").exists()
    for subdir in OKF_SUBDIRS:
        (okf_dir / subdir).mkdir(parents=True, exist_ok=True)
    (root / "raw_files").mkdir(exist_ok=True)
    if okf_missing:
        for draft in build_all_indexes(okf_dir, now):
            write_if_missing(draft.rel_path, draft.content)

    if agents:
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = root / name
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            if AGENT_INSTRUCTIONS_MARKER in existing:
                result.skipped.append(name)
                continue
            joined = existing.rstrip() + "\n\n" if existing.strip() else ""
            path.write_text(joined + AGENT_INSTRUCTIONS_BLOCK, encoding="utf-8")
            result.created.append(name)

    return result


# --- shared write machinery -------------------------------------------------------


@dataclass
class WriteOutcome:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # existing, not tool-owned
    indexes_written: list[str] = field(default_factory=list)

    def content_changed(self) -> bool:
        return bool(self.created or self.updated or self.indexes_written)


def _write_page(root: Path, rel: str, content: str, owned: set[str], out: WriteOutcome) -> None:
    target = root / rel
    if target.exists():
        if rel not in owned:
            # Never blindly overwrite a page the tool did not generate.
            out.skipped.append(rel)
            return
        if materially_equal(target.read_text(encoding="utf-8"), content):
            out.unchanged.append(rel)
            return
        target.write_text(content, encoding="utf-8")
        out.updated.append(rel)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        out.created.append(rel)


def _write_indexes(cfg: ChainConfig, root: Path, now: str, out: WriteOutcome) -> None:
    if not cfg.generation.update_indexes:
        return
    okf_name = cfg.generation.output_dir
    for draft in build_all_indexes(root / okf_name, now, okf_name):
        target = root / draft.rel_path
        if target.exists() and materially_equal(
            target.read_text(encoding="utf-8"), draft.content
        ):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(draft.content, encoding="utf-8")
        out.indexes_written.append(draft.rel_path)


def _build_manifest(
    cfg: ChainConfig,
    root: Path,
    now: str,
    plan: ChainPlan,
    out: WriteOutcome,
    previous: Manifest | None,
) -> Manifest:
    okf_name = cfg.generation.output_dir
    owned = set(previous.generated_files) if previous else set()
    generated_files = sorted(
        (owned | {d.rel_path for d in plan.pages}) - set(out.skipped)
    )
    managed_indexes = (
        [f"{okf_name}/index.md"] + [f"{okf_name}/{sub}/index.md" for sub in OKF_SUBDIRS]
        if cfg.generation.update_indexes
        else []
    )
    contents: dict[str, str] = {}
    for rel in generated_files + managed_indexes:
        path = root / rel
        if path.exists():
            contents[rel] = path.read_text(encoding="utf-8")
    return Manifest(
        chain_id=cfg.chain.id,
        chain_slug=cfg.chain.slug,
        output_dir=okf_name,
        generated_files=generated_files,
        managed_indexes=managed_indexes,
        source_fingerprints=compute_fingerprints(cfg, root),
        last_run_at=now,
        last_content_snapshot=compute_snapshot(contents),
    )


def _record_run(
    cfg: ChainConfig, root: Path, now: str, command: str, plan: ChainPlan, out: WriteOutcome
) -> str | None:
    """Write run metadata — but never churn it on no-op runs."""
    if not out.content_changed():
        return None
    path = write_run(
        root,
        RunRecord(
            updatedAt=now,
            command=command,
            chainId=cfg.chain.id,
            model=cfg.model.model,
            okfGitHead=okf_git_head(root),
            contentChanged=True,
            createdFiles=sorted(out.created),
            updatedFiles=sorted(out.updated + out.indexes_written),
            gaps=len(plan.gaps),
            divergences=0,  # deterministic runs perform no cross-checking
        ),
    )
    return path.relative_to(root).as_posix()


# --- generate ------------------------------------------------------------------


@dataclass
class GenerateResult(WriteOutcome):
    gaps: list[str] = field(default_factory=list)
    manifest_written: bool = False
    run_file: str | None = None
    report: ValidationReport = field(default_factory=ValidationReport)


def run_generate(cfg: ChainConfig, root: str | Path, now: str | None = None) -> GenerateResult:
    """Deterministically scaffold one chain's OKF pages, indexes, and manifest."""
    root = Path(root).resolve()
    now = now or now_stamp()

    plan = plan_chain_pages(cfg, root, now)
    result = GenerateResult(gaps=plan.gaps)

    previous = load_manifest(root)
    owned = set(previous.generated_files) if previous else set()

    if cfg.generation.overwrite_policy == "fail_if_exists":
        collisions = [d.rel_path for d in plan.pages if (root / d.rel_path).exists()]
        if collisions:
            raise GenerateError(
                "overwrite_policy is fail_if_exists and these pages already "
                "exist: " + ", ".join(collisions)
            )

    for draft in plan.pages:
        _write_page(root, draft.rel_path, draft.content, owned, result)
    _write_indexes(cfg, root, now, result)

    manifest = _build_manifest(cfg, root, now, plan, result, previous)
    if previous is None or not manifests_equal_ignoring_run_time(previous, manifest):
        save_manifest(root, manifest)
        result.manifest_written = True

    result.run_file = _record_run(cfg, root, now, "generate", plan, result)
    result.report = validate_tree(root, okf_dir=cfg.generation.output_dir)
    return result


# --- update --------------------------------------------------------------------


@dataclass
class UpdateResult(WriteOutcome):
    noop: bool = False
    changes: SourceChanges = field(default_factory=SourceChanges)
    impact: dict[str, list[str]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    manifest_written: bool = False
    run_file: str | None = None
    report: ValidationReport | None = None


def run_update(cfg: ChainConfig, root: str | Path, now: str | None = None) -> UpdateResult:
    """Deterministic update: diff evidence fingerprints against the manifest,
    build an impact plan, and rewrite only affected tool-owned pages.

    With no source changes this is a strict no-op: no OKF file writes, no
    manifest write, no run metadata.
    """
    root = Path(root).resolve()
    now = now or now_stamp()

    previous = load_manifest(root)
    if previous is None:
        raise GenerateError(
            "no manifest found under .lineage-wiki/ — run `lineage-wiki generate` first"
        )
    if previous.chain_id != cfg.chain.id:
        raise GenerateError(
            f"manifest belongs to chain {previous.chain_id!r}, config is for "
            f"{cfg.chain.id!r} — multi-chain manifests land in a later milestone"
        )

    current = compute_fingerprints(cfg, root)
    changes = diff_fingerprints(previous.source_fingerprints, current)
    result = UpdateResult(changes=changes)

    if not changes.any():
        result.noop = True
        return result

    plan = plan_chain_pages(cfg, root, now)
    result.gaps = plan.gaps
    impact = build_impact_plan(cfg, root, now, changes, plan.pages)
    result.impact = dict(impact.sorted_items())

    owned = set(previous.generated_files)
    for draft in plan.pages:
        if draft.rel_path not in impact.pages:
            continue  # surgical updates: untouched evidence, untouched page
        _write_page(root, draft.rel_path, draft.content, owned, result)
    _write_indexes(cfg, root, now, result)

    manifest = _build_manifest(cfg, root, now, plan, result, previous)
    if not manifests_equal_ignoring_run_time(previous, manifest):
        save_manifest(root, manifest)
        result.manifest_written = True

    result.run_file = _record_run(cfg, root, now, "update", plan, result)
    result.report = validate_tree(root, okf_dir=cfg.generation.output_dir)
    return result
