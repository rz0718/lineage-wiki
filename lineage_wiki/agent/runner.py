"""Deterministic init/generate/update runs (no LLM)."""

from __future__ import annotations

import re
import os
import shutil
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..config import ChainConfig
from ..constants import (
    AGENT_BLOCK_END,
    AGENT_BLOCK_START,
    AGENT_INSTRUCTIONS_BLOCK,
    AGENT_INSTRUCTIONS_MARKER,
    GENERATED_MARKER,
    MANIFEST_DIR,
    MANIFEST_FILE,
    OKF_SUBDIRS,
    PRESERVED_SECTIONS,
    PROMPT_STUBS,
)
from ..examples import EXAMPLE_CHAIN_YAML
from ..ingestion.fingerprints import compute_fingerprint_result, compute_fingerprints
from ..ingestion.git_context import GitContext, collect_git_context
from ..ingestion.source_loader import EvidenceBundle
from ..okf.indexes import build_all_indexes
from ..okf.sections import diff_summary, merge_manual_sections
from ..okf.templates import ChainPlan, plan_chain_pages
from ..okf.validator import ValidationReport, validate_tree
from ..storage.manifest import (
    ChainManifest,
    Manifest,
    SourceChanges,
    compute_file_snapshots,
    compute_snapshot,
    diff_fingerprints,
    load_manifest,
    manifest_lock,
    manifests_equal_ignoring_run_time,
    save_manifest,
)
from ..storage.runs import RunRecord, okf_git_head, write_json_run, write_run
from ..storage.snapshots import materially_equal
from ..util import now_stamp
from .planner import build_impact_plan


class GenerateError(Exception):
    """Raised when a generate/update run must stop."""


# --- init ---------------------------------------------------------------------


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def upsert_agent_context(existing: str) -> tuple[str, str]:
    """Insert or refresh the managed OKF Wiki Context block in an agents
    file, preserving all unrelated content.

    Returns ``(new_text, action)`` with action one of ``created`` (file was
    empty/missing), ``updated`` (managed or legacy block replaced),
    ``appended`` (block added to existing content), ``unchanged``.
    """
    block = AGENT_INSTRUCTIONS_BLOCK.rstrip("\n")
    if not existing.strip():
        return AGENT_INSTRUCTIONS_BLOCK, "created"

    if AGENT_BLOCK_START in existing and AGENT_BLOCK_END in existing:
        start = existing.index(AGENT_BLOCK_START)
        end = existing.index(AGENT_BLOCK_END) + len(AGENT_BLOCK_END)
        new_text = existing[:start] + block + existing[end:]
        return (new_text, "unchanged" if new_text == existing else "updated")

    if AGENT_INSTRUCTIONS_MARKER in existing:
        # Legacy block from before the delimiters existed: replace from its
        # heading up to the next heading (or EOF), keeping everything else.
        pattern = rf"(?ms)^{re.escape(AGENT_INSTRUCTIONS_MARKER)}\n.*?(?=^#{{1,2}} |\Z)"
        new_text = re.sub(pattern, block + "\n\n", existing, count=1)
        new_text = new_text.rstrip("\n") + "\n"
        return (new_text, "unchanged" if new_text == existing else "updated")

    return existing.rstrip("\n") + "\n\n" + AGENT_INSTRUCTIONS_BLOCK, "appended"


def run_init(
    root: str | Path,
    *,
    agents: bool = False,
    github_action: bool = False,
    now: str | None = None,
) -> InitResult:
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

    if github_action:
        from ..github_action import WORKFLOW_REL_PATH, render_workflow

        write_if_missing(WORKFLOW_REL_PATH, render_workflow())

    if agents:
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = root / name
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            new_text, action = upsert_agent_context(existing)
            if action == "unchanged":
                result.skipped.append(name)
                continue
            path.write_text(new_text, encoding="utf-8")
            if action == "created":
                result.created.append(name)
            else:  # updated or appended — an existing file was modified
                result.updated.append(name)

    return result


# --- shared write machinery -------------------------------------------------------


@dataclass
class WriteOutcome:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # existing, not tool-owned
    indexes_written: list[str] = field(default_factory=list)
    indexes_skipped: list[str] = field(default_factory=list)  # existing, hand-written
    diffs: dict[str, str] = field(default_factory=dict)  # rel -> one-line summary
    pending: dict[str, str] = field(default_factory=dict)  # rel -> new content
    # rel -> [(section heading, stale evidence ids)] for LLM-written sections
    # reverted because their cited evidence changed this run.
    invalidated: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict)

    def content_changed(self) -> bool:
        return bool(self.created or self.updated or self.indexes_written)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _normalize_output_dir(root: Path, output_dir: str) -> str:
    """Return a canonical root-relative OKF output directory.

    Configs should use a root-relative path such as ``okf``. For operator
    convenience, also accept paths that redundantly include the selected root
    (for example ``../wiki-repo/okf`` when ``--root ../wiki-repo``), as long as
    the resolved destination is still inside the target root.
    """
    value = output_dir.strip()
    if not value:
        raise GenerateError(
            f"unsafe output_dir {output_dir!r}: use a relative path inside the target root"
        )
    raw = Path(value)
    target = raw if raw.is_absolute() else root / raw
    try:
        # Collapse "." and ".." without following symlinks. The follow-up
        # safety check must still see symlink components such as "okf".
        rel = Path(os.path.abspath(target)).relative_to(root)
    except ValueError as exc:
        raise GenerateError(
            f"unsafe output_dir {output_dir!r}: use a relative path inside the target root"
        ) from exc
    return rel.as_posix()


def _reject_unsafe_output_dir(root: Path, output_dir: str) -> None:
    """Fail before planning writes when the OKF output directory is unsafe."""
    rel = Path(output_dir)
    if not output_dir.strip() or rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise GenerateError(
            f"unsafe output_dir {output_dir!r}: use a relative path inside the target root"
        )
    if rel == Path(".") or any(part in ("", ".") for part in rel.parts):
        raise GenerateError(f"unsafe output_dir {output_dir!r}: use a named subdirectory")

    target = root / rel
    existing = target if target.exists() else target.parent
    while existing != root and not existing.exists():
        existing = existing.parent
    if existing.exists():
        if existing.is_symlink():
            raise GenerateError(
                f"unsafe output_dir {output_dir!r}: {existing.relative_to(root).as_posix()} is a symlink"
            )
        if not existing.is_dir():
            raise GenerateError(
                f"unsafe output_dir {output_dir!r}: {existing.relative_to(root).as_posix()} is not a directory"
            )
        if not _is_relative_to(existing.resolve(), root):
            raise GenerateError(
                f"unsafe output_dir {output_dir!r}: resolved path escapes the target root"
            )


def with_safe_output_dir(cfg: ChainConfig, root: Path) -> ChainConfig:
    """Return ``cfg`` with ``generation.output_dir`` normalized to the
    canonical root-relative form (and validated). Every command that maps
    ``output_dir`` to on-disk or manifest paths must apply this, so that
    e.g. ``../wiki-repo/okf`` and ``okf`` name the same owned files."""
    output_dir = _normalize_output_dir(root, cfg.generation.output_dir)
    _reject_unsafe_output_dir(root, output_dir)
    if output_dir == cfg.generation.output_dir:
        return cfg
    normalized = cfg.model_copy(deep=True)
    normalized.generation.output_dir = output_dir
    return normalized


def _safe_target(root: Path, rel: str) -> Path:
    root = root.resolve()
    rel_path = Path(rel)
    if rel_path.is_absolute() or any(part in ("", ".", "..") for part in rel_path.parts):
        raise GenerateError(f"unsafe write target {rel!r}: must stay inside the target root")
    target = root / rel_path
    current = root
    for part in rel_path.parts[:-1]:
        current = current / part
        if current.exists():
            if current.is_symlink():
                raise GenerateError(
                    f"unsafe write target {rel!r}: parent {current.relative_to(root).as_posix()} is a symlink"
                )
            if not current.is_dir():
                raise GenerateError(
                    f"unsafe write target {rel!r}: parent {current.relative_to(root).as_posix()} is not a directory"
                )
            if not _is_relative_to(current.resolve(), root):
                raise GenerateError(
                    f"unsafe write target {rel!r}: resolved parent escapes the target root"
                )
    if target.exists() and target.is_symlink():
        raise GenerateError(f"unsafe write target {rel!r}: target is a symlink")
    return target


def _apply_pending(root: Path, pending: dict[str, str]) -> None:
    for rel, content in pending.items():
        target = _safe_target(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, path)
    finally:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()


def _commit_staged_files(root: Path, stage: Path, rels: list[str]) -> None:
    unique_rels = list(dict.fromkeys(rels))
    targets = {rel: _safe_target(root, rel) for rel in unique_rels}
    for rel in unique_rels:
        staged = stage / rel
        if not staged.is_file():
            raise GenerateError(f"staged write missing for {rel}")
    for rel in unique_rels:
        _atomic_replace_bytes(targets[rel], (stage / rel).read_bytes())


def _raise_if_validation_failed(report: ValidationReport) -> None:
    if not report.failed():
        return
    details = "; ".join(str(issue) for issue in report.errors[:5])
    if len(report.errors) > 5:
        details += f"; ... {len(report.errors) - 5} more error(s)"
    raise GenerateError(f"staged output failed validation: {details}")


def _stale_evidence_ids(cfg: ChainConfig, changes: SourceChanges) -> frozenset[str]:
    """Evidence ids (or ``prefix:``-style prefixes) whose sources changed
    this run. LLM-written sections citing any of them are invalidated
    instead of being preserved."""
    from ..connectors.bigquery_connector import parse_table_name
    from ..util import slugify

    stale: set[str] = set()
    for path in changes.raw_docs:
        stale.add(f"raw-doc:{path}")
    for name in changes.repos:
        stale.add(f"local-repo:{name}:")  # prefix: any file of that repo
    project = cfg.sources.bigquery.project if cfg.sources.bigquery else None
    for table in changes.bigquery:
        stale.add(f"bq-schema:{table}")
        try:
            stale.add(f"bq-schema:{parse_table_name(table, project).fqtn}")
        except ValueError:
            pass
    for name in changes.reports:
        stale.add(f"report:{slugify(name)}")
    for name in changes.slack:
        stale.add(f"slack:{slugify(name)}")
    if changes.config:
        # Human notes live in the chain config; a config change may have
        # edited them.
        stale.add("human-note:")
    return frozenset(stale)


def _write_page(
    root: Path,
    rel: str,
    content: str,
    owned: set[str],
    out: WriteOutcome,
    *,
    preserve: bool = True,
    dry_run: bool = False,
    force_sections: tuple[str, ...] = (),
    stale_evidence: frozenset[str] = frozenset(),
) -> None:
    target = root / rel
    if target.exists():
        if rel not in owned:
            # Never blindly overwrite a page the tool did not generate.
            out.skipped.append(rel)
            return
        existing = target.read_text(encoding="utf-8")
        if preserve:
            page_invalidated: list[tuple[str, list[str]]] = []
            content = merge_manual_sections(
                existing,
                content,
                force=force_sections,
                stale_evidence=stale_evidence,
                invalidated=page_invalidated,
            )
            if page_invalidated:
                out.invalidated[rel] = page_invalidated
        if materially_equal(existing, content):
            out.unchanged.append(rel)
            return
        out.diffs[rel] = diff_summary(existing, content)
        out.pending[rel] = content
        out.updated.append(rel)
    else:
        out.pending[rel] = content
        out.created.append(rel)


def _index_is_owned_by_tool(rel: str, text: str, previous: Manifest | None) -> bool:
    if GENERATED_MARKER not in text:
        return False
    if previous is None:
        return True
    if rel not in set(previous.managed_indexes):
        return False
    return previous.matching_index_snapshot(rel, text)


def _write_indexes(
    cfg: ChainConfig,
    root: Path,
    now: str,
    out: WriteOutcome,
    previous: Manifest | None,
) -> list[str]:
    """Plan tool-owned index writes; returns the rels this run owns.

    An existing index is owned only when it is still recognizable as tool
    generated and, for previously managed indexes, unchanged from the last
    successful run. Manual index edits therefore make the whole index
    protected until a human deliberately reconciles it.
    """
    owned_indexes: list[str] = []
    if not cfg.generation.update_indexes:
        return owned_indexes
    okf_name = cfg.generation.output_dir
    for draft in build_all_indexes(root / okf_name, now, okf_name):
        target = root / draft.rel_path
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if not _index_is_owned_by_tool(draft.rel_path, existing, previous):
                out.indexes_skipped.append(draft.rel_path)
                continue
            owned_indexes.append(draft.rel_path)
            if materially_equal(existing, draft.content):
                continue
            out.diffs[draft.rel_path] = diff_summary(existing, draft.content)
        else:
            owned_indexes.append(draft.rel_path)
        out.pending[draft.rel_path] = draft.content
        out.indexes_written.append(draft.rel_path)
    return owned_indexes


def _build_manifest(
    cfg: ChainConfig,
    root: Path,
    now: str,
    plan: ChainPlan,
    out: WriteOutcome,
    previous: ChainManifest | None,
    owned_indexes: list[str],
    *,
    fingerprint_root: Path | None = None,
) -> ChainManifest:
    okf_name = cfg.generation.output_dir
    owned = set(previous.generated_files) if previous else set()
    generated_files = sorted(
        (owned | {d.rel_path for d in plan.pages}) - set(out.skipped)
    )
    # Only indexes this run actually owns are managed — hand-written indexes
    # must never enter the manifest, or a later run would overwrite them.
    managed_indexes = sorted(owned_indexes)
    contents: dict[str, str] = {}
    for rel in generated_files + managed_indexes:
        path = root / rel
        if path.exists():
            contents[rel] = path.read_text(encoding="utf-8")
    return ChainManifest(
        chain_slug=cfg.chain.slug,
        output_dir=okf_name,
        generated_files=generated_files,
        managed_indexes=managed_indexes,
        file_snapshots=compute_file_snapshots(contents),
        # Slack loads are reused from the plan's bundle: the manifest must
        # fingerprint the message the pages were rendered from, not whatever
        # newer message a second fetch might return mid-run.
        source_fingerprints=compute_fingerprints(
            cfg,
            fingerprint_root or root,
            previous.source_fingerprints if previous is not None else None,
            slack_loads=plan.bundle.slack or None,
        ),
        last_run_at=now,
        last_content_snapshot=compute_snapshot(contents),
        okf_git_head=okf_git_head(fingerprint_root or root),
    )


def _with_chain_entry(
    previous: Manifest | None, chain_id: str, entry: ChainManifest
) -> Manifest:
    manifest = previous.model_copy(deep=True) if previous else Manifest()
    manifest.chains[chain_id] = entry
    return manifest


def _chain_entries_equal_ignoring_run_time(
    a: ChainManifest | None, b: ChainManifest | None
) -> bool:
    if a is None or b is None:
        return a is b
    left = a.model_dump()
    right = b.model_dump()
    for data in (left, right):
        data.pop("last_run_at", None)
        data.pop("okf_git_head", None)
    return left == right


def _record_run(
    cfg: ChainConfig,
    root: Path,
    now: str,
    command: str,
    plan: ChainPlan,
    out: WriteOutcome,
    *,
    git_root: Path | None = None,
) -> str | None:
    """Write run metadata — but never churn it on no-op runs."""
    if not out.content_changed():
        return None
    git_root = git_root or root
    path = write_run(
        root,
        RunRecord(
            updatedAt=now,
            command=command,
            chainId=cfg.chain.id,
            model=cfg.model.model,
            okfGitHead=okf_git_head(git_root),
            contentChanged=True,
            createdFiles=sorted(out.created),
            updatedFiles=sorted(out.updated + out.indexes_written),
            gaps=len(plan.gaps),
            divergences=0,  # deterministic runs perform no cross-checking
        ),
    )
    return path.relative_to(root).as_posix()


# --- evidence / verification summaries ------------------------------------------


def _describe_evidence(cfg: ChainConfig, bundle: EvidenceBundle) -> list[str]:
    """Human-readable one-liners for every configured evidence source."""
    lines: list[str] = []
    for item in bundle.raw_docs:
        n = item.metadata.get("lines", "?")
        lines.append(f"raw doc `{item.source_uri}` — loaded ({n} lines)")
    for path in bundle.missing_raw_docs:
        lines.append(f"raw doc `{path}` — missing (recorded as a Known Gap)")
    for load in bundle.repos:
        if load.available:
            head = (load.git_head or "no git head")[:12]
            line = (
                f"repo `{load.repo.name}` — loaded {len(load.files)} file(s) "
                f"from local clone @ {head}"
            )
            if load.missing_paths:
                line += f"; {len(load.missing_paths)} configured path(s) missing"
            lines.append(line)
        else:
            where = (
                f"no local clone at `{load.repo.local_path}`"
                if load.repo.local_path
                else "no local_path configured"
            )
            lines.append(f"repo `{load.repo.name}` — {where}; unverified reference")
    bq = bundle.bigquery
    if bq is not None:
        if bq.available:
            line = f"bigquery ({bq.client_kind}) — {len(bq.schemas)} schema(s) ingested"
            if bq.missing_tables:
                line += f"; not found: {', '.join(bq.missing_tables)}"
            lines.append(line)
        else:
            n_tables = len(cfg.sources.bigquery.tables) if cfg.sources.bigquery else 0
            lines.append(
                f"bigquery — unavailable ({bq.unavailable_reason}); "
                f"{n_tables} table schema(s) not ingested"
            )
    for note in bundle.human_notes:
        lines.append(f"human note `{note.title}` — included")
    for report in bundle.reports:
        mapped = "with source mapping" if report.content else "no source mapping"
        lines.append(f"report `{report.title}` — {mapped}")
    for load in bundle.slack:
        src = load.source
        if not load.available:
            lines.append(f"slack `{src.name}` — unavailable ({load.unavailable_reason})")
        elif load.message is None:
            lines.append(
                f"slack `{src.name}` — no message matching {src.match_text!r} "
                f"within {src.lookback_hours}h"
            )
        else:
            line = f"slack `{src.name}` — matched message @ {load.message.ts}"
            if load.replies:
                line += f" (+{len(load.replies)} thread replies)"
            lines.append(line)
    return lines


def _describe_verification(cfg: ChainConfig, bundle: EvidenceBundle) -> list[str]:
    """What `verify-bq` would do with this config (generate never queries)."""
    spec = cfg.bigquery_verification
    if not spec.enabled:
        return ["bigquery_verification is disabled — verification skipped"]
    lines = [
        f"bigquery_verification enabled (mode: {spec.mode}, "
        f"max_bytes_billed: {spec.max_bytes_billed})"
    ]
    tables = cfg.sources.bigquery.tables if cfg.sources.bigquery else []
    bq = bundle.bigquery
    for table in tables:
        ingested = bool(bq and bq.available and bq.schemas.get(table))
        state = "schema ingested" if ingested else "schema not ingested"
        lines.append(f"would verify `{table}` ({state})")
    if spec.mode == "formula_check" and spec.formula_checks.enabled:
        for check in spec.formula_checks.checks:
            lines.append(
                f"would run formula check `{check.name}` on `{check.table}`"
            )
    lines.append(
        "generate never queries BigQuery — run `lineage-wiki verify-bq` to verify"
    )
    return lines


# --- dry-run shadow tree ---------------------------------------------------------


@contextmanager
def _shadow_tree(root: Path, okf_name: str, pending: dict[str, str]) -> Iterator[Path]:
    """A temporary mirror of the target repo with this run's pending page
    writes applied: the okf/ tree and .lineage-wiki/ are copied, everything
    else (raw_files/, clones, …) is symlinked so relative links still
    resolve. Dry runs write only here — never into the real repo."""
    with tempfile.TemporaryDirectory(prefix="lineage-wiki-dry-run-") as td:
        shadow = Path(td) / "repo"
        shadow.mkdir()
        okf_parts = Path(okf_name).parts
        okf_top = okf_parts[0] if okf_parts else okf_name
        if root.exists():
            for entry in root.iterdir():
                if entry.name in (okf_top, MANIFEST_DIR, ".git"):
                    continue
                (shadow / entry.name).symlink_to(entry)
            src_okf = root / okf_name
            if src_okf.is_dir():
                (shadow / okf_name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src_okf, shadow / okf_name)
            else:
                (shadow / okf_name).mkdir(parents=True)
            src_state = root / MANIFEST_DIR
            if src_state.is_dir():
                shutil.copytree(src_state, shadow / MANIFEST_DIR)
        else:
            (shadow / okf_name).mkdir(parents=True)
        _apply_pending(shadow, pending)
        yield shadow


# --- generate ------------------------------------------------------------------


@dataclass
class GenerateResult(WriteOutcome):
    gaps: list[str] = field(default_factory=list)
    manifest_written: bool = False
    run_file: str | None = None
    report: ValidationReport = field(default_factory=ValidationReport)
    dry_run: bool = False
    evidence: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    llm: list[str] = field(default_factory=list)


def _finalize_staged_run(
    cfg: ChainConfig,
    root: Path,
    now: str,
    command: str,
    plan: ChainPlan,
    out,
    previous_manifest: Manifest | None,
    previous_entry: ChainManifest | None,
    *,
    dry_run: bool = False,
    fingerprint_root: Path | None = None,
    llm_transcript: dict | None = None,
) -> None:
    """Stage pending writes, validate the staged tree, then atomically commit.

    The real repository is untouched until staged validation passes. Manifest
    writes are committed last so source baselines cannot advance ahead of the
    content they describe.
    """
    manifest_rel: str | None = None
    run_rels: list[str] = []
    lock = nullcontext() if dry_run else manifest_lock(root)
    with lock:
        latest_manifest = previous_manifest if dry_run else load_manifest(root)
        latest_entry = (
            latest_manifest.chains.get(cfg.chain.id) if latest_manifest else None
        )
        if not _chain_entries_equal_ignoring_run_time(
            previous_entry, latest_entry
        ):
            raise GenerateError(
                f"manifest entry for chain {cfg.chain.id!r} changed during "
                "this run; retry the command"
            )

        with _shadow_tree(root, cfg.generation.output_dir, dict(out.pending)) as shadow:
            owned_indexes = _write_indexes(cfg, shadow, now, out, latest_manifest)
            _apply_pending(shadow, out.pending)

            entry = _build_manifest(
                cfg,
                shadow,
                now,
                plan,
                out,
                latest_entry,
                owned_indexes,
                fingerprint_root=fingerprint_root or root,
            )
            manifest = _with_chain_entry(latest_manifest, cfg.chain.id, entry)
            if out.content_changed() and (
                latest_manifest is None
                or not manifests_equal_ignoring_run_time(latest_manifest, manifest)
            ):
                save_manifest(shadow, manifest)
                out.manifest_written = True
                manifest_rel = MANIFEST_FILE

            if not dry_run:
                run_file = _record_run(
                    cfg, shadow, now, command, plan, out, git_root=root
                )
                out.run_file = run_file
                if run_file:
                    run_rels.append(run_file)
                if llm_transcript is not None and out.content_changed():
                    path = write_json_run(shadow, now, "generate-llm", llm_transcript)
                    run_rels.append(path.relative_to(shadow).as_posix())

            out.report = validate_tree(shadow, okf_dir=cfg.generation.output_dir)
            if dry_run:
                return

            commit_rels = list(out.pending) + run_rels
            if manifest_rel:
                commit_rels.append(manifest_rel)
            if commit_rels:
                _raise_if_validation_failed(out.report)
            _commit_staged_files(root, shadow, commit_rels)


def run_generate(
    cfg: ChainConfig,
    root: str | Path,
    now: str | None = None,
    *,
    dry_run: bool = False,
    use_llm: bool = False,
    llm_provider=None,
) -> GenerateResult:
    """Deterministically scaffold one chain's OKF pages, indexes, and manifest.

    With ``dry_run=True`` nothing under ``root`` is written: page and index
    writes are classified against the real tree, then applied to a temporary
    shadow copy so the reported index contents, manifest decision, and
    validation status are exactly what a real run would produce.
    """
    root = Path(root).resolve()
    cfg = with_safe_output_dir(cfg, root)
    now = now or now_stamp()

    plan = plan_chain_pages(cfg, root, now)
    result = GenerateResult(gaps=plan.gaps, dry_run=dry_run)
    result.evidence = _describe_evidence(cfg, plan.bundle)
    result.verification = _describe_verification(cfg, plan.bundle)

    # Sections the current run intentionally rewrote (LLM pipeline output);
    # they bypass preservation so a fresh LLM run can update its own content.
    force_by_rel: dict[str, tuple[str, ...]] = {}
    enrichment = None
    if use_llm:
        # Imported lazily: the deterministic default path never touches
        # provider code and never needs a model.
        from ..credentials import resolve_llm_provider
        from .llm_pipeline import run_llm_enrichment

        provider = llm_provider or resolve_llm_provider(
            cfg.model.provider, cfg.model.model
        )
        enrichment = run_llm_enrichment(cfg, root, plan, provider)
        for draft in plan.pages:
            if draft.rel_path in enrichment.drafts:
                draft.content = enrichment.drafts[draft.rel_path]
        for rel, sections in enrichment.sections_written.items():
            force_by_rel[rel] = tuple(sections)
        if enrichment.divergences:
            framework_rel = f"{cfg.generation.output_dir}/frameworks/{cfg.chain.slug}.md"
            force_by_rel[framework_rel] = force_by_rel.get(framework_rel, ()) + (
                "Known Doc-vs-Code Divergences",
            )
        result.llm = enrichment.summary

    previous_manifest = load_manifest(root)
    previous = (
        previous_manifest.chains.get(cfg.chain.id) if previous_manifest else None
    )
    owned = set(previous.generated_files) if previous else set()

    if cfg.generation.overwrite_policy == "fail_if_exists":
        collisions = [d.rel_path for d in plan.pages if (root / d.rel_path).exists()]
        if collisions:
            raise GenerateError(
                "overwrite_policy is fail_if_exists and these pages already "
                "exist: " + ", ".join(collisions)
            )

    preserve = cfg.generation.preserve_manual_sections
    for draft in plan.pages:
        _write_page(
            root, draft.rel_path, draft.content, owned, result,
            preserve=preserve, dry_run=dry_run,
            force_sections=force_by_rel.get(draft.rel_path, ()),
        )

    _finalize_staged_run(
        cfg,
        root,
        now,
        "generate",
        plan,
        result,
        previous_manifest,
        previous,
        dry_run=dry_run,
        fingerprint_root=root,
        llm_transcript=enrichment.transcript if enrichment is not None else None,
    )
    return result


# --- update --------------------------------------------------------------------


@dataclass
class UpdateResult(WriteOutcome):
    noop: bool = False
    plan_only: bool = False
    changes: SourceChanges = field(default_factory=SourceChanges)
    impact: dict[str, list[str]] = field(default_factory=dict)
    git_context: list[str] = field(default_factory=list)
    indexes_affected: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)  # plan-only: proposed actions
    risks: list[str] = field(default_factory=list)  # plan-only: validation risks
    gaps: list[str] = field(default_factory=list)
    manifest_written: bool = False
    run_file: str | None = None
    report: ValidationReport | None = None
    warnings: list[str] = field(default_factory=list)


def _collect_repo_contexts(
    cfg: ChainConfig, root: Path, previous: ChainManifest
) -> dict[str, GitContext]:
    contexts: dict[str, GitContext] = {}
    for repo in cfg.sources.repos:
        if not repo.local_path:
            continue
        local = (root / repo.local_path).resolve()
        if not local.is_dir():
            continue
        baseline = previous.source_fingerprints.repos.get(repo.name)
        contexts[repo.name] = collect_git_context(
            local,
            label=f"repo `{repo.name}`",
            baseline=baseline.git_head if baseline else None,
        )
    return contexts


def _plan_only_assessment(
    cfg: ChainConfig,
    root: Path,
    previous_manifest: Manifest,
    previous: ChainManifest,
    plan: ChainPlan,
    impact_pages: dict[str, list[str]],
    result: UpdateResult,
) -> None:
    """Fill proposed actions and validation risks without writing anything."""
    drafts = {d.rel_path: d for d in plan.pages}
    owned = set(previous.generated_files)
    preserve = cfg.generation.preserve_manual_sections
    stale_evidence = _stale_evidence_ids(cfg, result.changes)
    for rel in sorted(impact_pages):
        draft = drafts.get(rel)
        target = root / rel
        if draft is None:
            result.actions.append(
                f"review    {rel} (not tool-generated; the tool will not edit it)"
            )
            if target.exists():
                result.risks.append(
                    f"{rel} is affected but hand-written — needs a manual review"
                )
            continue
        if not target.exists():
            result.actions.append(f"create    {rel}")
            continue
        if rel not in owned:
            result.actions.append(f"protected {rel} (exists but is not tool-owned)")
            result.risks.append(
                f"{rel} is affected but not tool-owned — the update will skip "
                "it; reconcile manually"
            )
            continue
        existing = target.read_text(encoding="utf-8")
        content = draft.content
        if preserve:
            page_invalidated: list[tuple[str, list[str]]] = []
            content = merge_manual_sections(
                existing, content,
                stale_evidence=stale_evidence, invalidated=page_invalidated,
            )
            for heading, stale_ids in page_invalidated:
                result.risks.append(
                    f"{rel}: LLM-written section '{heading}' cites changed "
                    f"evidence ({', '.join(stale_ids)}) and will be reverted "
                    "to scaffold — re-run `generate --use-llm` after the update"
                )
        if materially_equal(existing, content):
            result.actions.append(f"unchanged {rel}")
        else:
            result.actions.append(
                f"update    {rel} ({diff_summary(existing, content)})"
            )

    okf_name = cfg.generation.output_dir
    for rel in result.indexes_affected:
        target = root / rel
        if target.exists():
            text = target.read_text(encoding="utf-8")
            if not _index_is_owned_by_tool(rel, text, previous_manifest):
                result.risks.append(
                    f"{rel} is hand-written — new/renamed pages must be "
                    "listed there manually"
                )

    report = validate_tree(root, okf_dir=okf_name)
    affected = set(impact_pages)
    for issue in report.errors + report.warnings:
        if getattr(issue, "path", None) in affected:
            result.risks.append(f"pre-existing on {issue.path}: {issue.message}")


def run_update(
    cfg: ChainConfig,
    root: str | Path,
    now: str | None = None,
    *,
    plan_only: bool = False,
) -> UpdateResult:
    """Deterministic update: diff evidence fingerprints against the manifest,
    collect git context, build an impact plan, and rewrite only affected
    tool-owned pages.

    With no source changes this is a strict no-op: no OKF file writes, no
    manifest write, no run metadata. With ``plan_only=True`` nothing is
    written in any case — the result carries the proposed actions and
    validation risks instead.
    """
    root = Path(root).resolve()
    cfg = with_safe_output_dir(cfg, root)
    now = now or now_stamp()

    previous_manifest = load_manifest(root)
    if previous_manifest is None:
        raise GenerateError(
            "no manifest found under .lineage-wiki/ — run `lineage-wiki generate` first"
        )
    previous = previous_manifest.chains.get(cfg.chain.id)
    if previous is None:
        raise GenerateError(
            f"no manifest entry for chain {cfg.chain.id!r} — run "
            "`lineage-wiki generate` for this chain first"
        )

    fingerprint_result = compute_fingerprint_result(
        cfg, root, previous.source_fingerprints
    )
    current = fingerprint_result.fingerprints
    changes = diff_fingerprints(previous.source_fingerprints, current)
    result = UpdateResult(
        changes=changes,
        plan_only=plan_only,
        warnings=fingerprint_result.warnings,
    )

    okf_context = collect_git_context(
        root, label="okf repo", baseline=previous.okf_git_head
    )
    repo_contexts = _collect_repo_contexts(cfg, root, previous)
    result.git_context = [okf_context.describe()] + [
        ctx.describe() for _, ctx in sorted(repo_contexts.items())
    ]

    if not changes.any():
        result.noop = True
        return result

    plan = plan_chain_pages(cfg, root, now)
    result.gaps = plan.gaps
    impact = build_impact_plan(cfg, root, now, changes, plan.pages, repo_contexts)
    result.impact = dict(impact.sorted_items())
    if cfg.generation.update_indexes:
        result.indexes_affected = impact.affected_indexes(cfg.generation.output_dir)

    if plan_only:
        _plan_only_assessment(
            cfg, root, previous_manifest, previous, plan, result.impact, result
        )
        return result

    owned = set(previous.generated_files)
    preserve = cfg.generation.preserve_manual_sections
    stale_evidence = _stale_evidence_ids(cfg, changes)
    for draft in plan.pages:
        if draft.rel_path not in impact.pages:
            continue  # surgical updates: untouched evidence, untouched page
        _write_page(
            root, draft.rel_path, draft.content, owned, result,
            preserve=preserve, stale_evidence=stale_evidence,
        )
    _finalize_staged_run(
        cfg,
        root,
        now,
        "update",
        plan,
        result,
        previous_manifest,
        previous,
        fingerprint_root=root,
    )
    return result
