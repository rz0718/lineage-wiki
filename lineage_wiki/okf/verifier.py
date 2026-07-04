"""BigQuery verification (phase 1: schema_only and profile modes).

Separate from offline OKF validation (`validate`) because it needs BigQuery
access and — in profile mode — incurs query cost. Detailed results (including
SQL and profiled values) are stored only under ``.lineage-wiki/runs/``; OKF
output pages receive summary conclusions under ``## Verification Status``,
never row-level data and never live metric values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import ChainConfig
from ..connectors import SourceUnavailableError
from ..connectors.bigquery_connector import TableSchema, load_bigquery_schemas
from ..ingestion.bq_profiler import (
    ProfileResult,
    build_profile_plan,
    resolve_profile_client,
    run_profile_plan,
)
from ..storage.manifest import load_manifest
from ..storage.runs import write_json_run
from ..util import now_stamp, slugify


class VerificationError(Exception):
    """Raised when a verify-bq run cannot proceed."""


@dataclass
class TableVerification:
    table: str
    exists: bool = False
    table_type: str | None = None
    fingerprint: str | None = None
    expected_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    schema: TableSchema | None = None
    profile: ProfileResult | None = None
    conclusions: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


@dataclass
class VerifyBqResult:
    mode: str
    notes: list[str] = field(default_factory=list)
    tables: list[TableVerification] = field(default_factory=list)
    run_file: str | None = None
    pages_updated: list[str] = field(default_factory=list)
    pages_skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tables)


# --- Schema-level checks ------------------------------------------------------------


def _verify_table_schema(
    table: str, schema: TableSchema | None, expected_columns: list[str]
) -> TableVerification:
    tv = TableVerification(table=table, expected_columns=expected_columns)
    if schema is None:
        tv.issues.append("table was not found in BigQuery")
        tv.conclusions.append("Table was not found in BigQuery.")
        return tv

    tv.exists = True
    tv.schema = schema
    tv.table_type = schema.table_type
    tv.fingerprint = schema.fingerprint()
    tv.conclusions.append(
        f"Table exists in BigQuery ({schema.table_type}, "
        f"{len(schema.columns)} columns)."
    )

    if expected_columns:
        present = {c.name for c in schema.columns}
        tv.missing_columns = [c for c in expected_columns if c not in present]
        if tv.missing_columns:
            missing = ", ".join(f"`{c}`" for c in tv.missing_columns)
            tv.issues.append(f"missing expected columns: {missing}")
            tv.conclusions.append(f"Missing expected columns: {missing}.")
        else:
            tv.conclusions.append(
                f"All {len(expected_columns)} expected columns are present."
            )

    part = schema.partitioning or {}
    if part.get("kind") == "time":
        where = f" on `{part['field']}`" if part.get("field") else " (ingestion time)"
        tv.conclusions.append(f"Time-partitioned ({part.get('type')}){where}.")
    elif part.get("kind") == "range":
        tv.conclusions.append(f"Range-partitioned on `{part.get('field')}`.")
    if schema.clustering:
        clustered = ", ".join(f"`{c}`" for c in schema.clustering)
        tv.conclusions.append(f"Clustered by {clustered}.")
    if schema.view_sql:
        tv.conclusions.append("View SQL captured in run metadata.")
    tv.conclusions.append(
        f"Schema fingerprinted for change tracking (`{tv.fingerprint[:23]}…`)."
    )
    return tv


# --- Profile conclusions --------------------------------------------------------------


def _profile_conclusions(profile: ProfileResult, tv: TableVerification) -> list[str]:
    """Summary conclusions only — profiled values stay in run metadata."""
    lines: list[str] = []
    if profile.window_days is not None:
        lines.append(
            f"Aggregate profiling ran over the last {profile.window_days} days "
            f"on `{profile.date_column}`."
        )
    else:
        lines.append(
            "Aggregate profiling ran without a date window (no date column "
            "was detected or configured)."
        )
    if profile.row_count is not None:
        if profile.row_count > 0:
            lines.append("Rows are present in the profiled window.")
        else:
            lines.append("No rows were found in the profiled window.")
            tv.issues.append("no rows in the profiled window")
    if profile.date_min is not None or profile.date_max is not None:
        lines.append(
            f"Date coverage captured for `{profile.date_column}` "
            "(bounds in run metadata)."
        )
    if profile.null_counts:
        with_nulls = sorted(c for c, n in profile.null_counts.items() if n)
        if with_nulls:
            cols = ", ".join(f"`{c}`" for c in with_nulls)
            lines.append(f"NULLs present in: {cols} (counts in run metadata).")
        else:
            lines.append(
                f"No NULLs found in {len(profile.null_counts)} profiled column(s)."
            )
    if profile.distinct_counts:
        cols = ", ".join(f"`{c}`" for c in sorted(profile.distinct_counts))
        lines.append(f"Distinct counts captured for: {cols} (values in run metadata).")
    if profile.min_max:
        cols = ", ".join(f"`{c}`" for c in sorted(profile.min_max))
        lines.append(f"Min/max captured for: {cols} (values in run metadata).")
    return lines


# --- OKF page updates -----------------------------------------------------------------


_SECTION_RE = re.compile(
    r"(?ms)^(## Verification Status\n).*?(?=^## |\Z)"
)


def _replace_verification_section(text: str, body: str) -> str | None:
    """Replace the body of ``## Verification Status``; None when absent."""
    if not _SECTION_RE.search(text):
        return None
    return _SECTION_RE.sub(lambda m: m.group(1) + "\n" + body.rstrip() + "\n\n", text)


def _page_summary(tv: TableVerification, mode: str, run_file: str | None) -> str:
    if mode == "profile" and tv.profile is not None:
        lead = "Verified from BigQuery schema metadata and safe aggregate profiling."
    else:
        lead = "Verified from BigQuery schema metadata (schema only)."
    lines = [lead, ""]
    lines.extend(f"- {c}" for c in tv.conclusions)
    if run_file:
        lines.append(
            f"- Detailed verification results: `{run_file}` (query results are "
            "stored in run metadata only, never on OKF pages)."
        )
    return "\n".join(lines)


def _update_output_pages(
    cfg: ChainConfig, root: Path, result: VerifyBqResult
) -> None:
    okf = cfg.generation.output_dir
    manifest = load_manifest(root)
    owned = set(manifest.generated_files) if manifest else set()
    for tv in result.tables:
        rel = f"{okf}/outputs/{slugify(tv.table.split('.')[-1])}.md"
        path = root / rel
        if not path.exists():
            result.pages_skipped.append(f"{rel} (page does not exist — run generate first)")
            continue
        if rel not in owned:
            result.pages_skipped.append(f"{rel} (not tool-generated; left untouched)")
            continue
        text = path.read_text(encoding="utf-8")
        replaced = _replace_verification_section(
            text, _page_summary(tv, result.mode, result.run_file)
        )
        if replaced is None:
            result.pages_skipped.append(f"{rel} (no `## Verification Status` section)")
            continue
        if replaced != text:
            path.write_text(replaced, encoding="utf-8")
            result.pages_updated.append(rel)


# --- Entry point ------------------------------------------------------------------------


def run_verify_bq(
    cfg: ChainConfig,
    root: str | Path,
    now: str | None = None,
    *,
    schema_client: Any | None = None,
    profile_client: Any | None = None,
) -> VerifyBqResult:
    """Verify configured BigQuery tables per ``bigquery_verification``.

    Clients are injectable for tests; by default they resolve exactly like
    the schema connector (fixture file, offline guard, google client).
    """
    root = Path(root).resolve()
    now = now or now_stamp()
    spec = cfg.bigquery_verification

    if not spec.enabled:
        raise VerificationError(
            "bigquery_verification.enabled is false in the chain config — "
            "enable it to run verify-bq"
        )
    if spec.mode not in ("schema_only", "profile"):
        raise VerificationError(
            f"bigquery_verification.mode {spec.mode!r} is not implemented yet "
            "(phase 1 supports schema_only and profile)"
        )
    source = cfg.sources.bigquery
    if source is None or not source.tables:
        raise VerificationError(
            "no BigQuery tables configured under sources.bigquery.tables"
        )

    result = VerifyBqResult(mode=spec.mode)
    if spec.sample_rows.enabled:
        result.notes.append(
            "sample_rows.enabled is set but sample rows are not read in "
            "phase 1 — schema and aggregate profiling only"
        )

    # verify-bq makes BigQuery required by definition: fail clearly when the
    # client is unavailable, whatever sources.bigquery.required says.
    load = load_bigquery_schemas(source, client=schema_client, enforce_required=False)
    if not load.available:
        raise VerificationError(f"BigQuery is unavailable: {load.unavailable_reason}")

    profiling_active = spec.mode == "profile" and spec.profiling.enabled
    if spec.mode == "profile" and not spec.profiling.enabled:
        result.notes.append(
            "mode is profile but profiling.enabled is false — schema checks only"
        )
    if profiling_active and profile_client is None:
        resolved = resolve_profile_client(source.project)
        if resolved.client is None:
            raise VerificationError(
                f"BigQuery query client is unavailable: {resolved.reason}"
            )
        profile_client = resolved.client

    for table in source.tables:
        table_spec = spec.table_spec(table)
        expected = table_spec.expect_columns if table_spec else []
        tv = _verify_table_schema(table, load.schemas.get(table), expected)
        if profiling_active and tv.schema is not None:
            plan = build_profile_plan(tv.schema, spec.profiling, table_spec)
            if plan is None:
                tv.conclusions.append(
                    "No profilable columns or enabled profiling signals for "
                    "this table."
                )
            else:
                tv.profile = run_profile_plan(plan, profile_client, spec.max_bytes_billed)
                tv.conclusions.extend(_profile_conclusions(tv.profile, tv))
        result.tables.append(tv)

    payload: dict[str, Any] = {
        "updatedAt": now,
        "command": "verify-bq",
        "chainId": cfg.chain.id,
        "mode": spec.mode,
        "max_bytes_billed": spec.max_bytes_billed,
        "notes": result.notes,
        "ok": result.ok,
        "tables": [
            {
                "table": tv.table,
                "exists": tv.exists,
                "table_type": tv.table_type,
                "schema_fingerprint": tv.fingerprint,
                "expected_columns": tv.expected_columns,
                "missing_columns": tv.missing_columns,
                "view_sql": tv.schema.view_sql if tv.schema else None,
                "conclusions": tv.conclusions,
                "issues": tv.issues,
                "profile": tv.profile.to_payload() if tv.profile else None,
            }
            for tv in result.tables
        ],
    }
    run_path = write_json_run(root, now, "verify-bq", payload)
    result.run_file = run_path.relative_to(root).as_posix()

    if spec.store_results.okf_pages == "summary_only":
        _update_output_pages(cfg, root, result)
    return result
