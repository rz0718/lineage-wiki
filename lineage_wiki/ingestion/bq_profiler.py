"""Safe aggregate profiling for BigQuery tables (verification phase 1).

Safety contract (spec section 11, ``profile`` mode):

- Queries are built from deterministic templates over schema-derived column
  names — never from natural language, never ``SELECT *``.
- Aggregates only: COUNT, COUNTIF(IS NULL), APPROX_COUNT_DISTINCT, MIN, MAX.
  No row-level data ever leaves BigQuery.
- Every real query sets ``maximum_bytes_billed``.
- When a date column is known (configured, partition field, or detected),
  the scan is limited to the configured date window.

The query client mirrors the schema connector's resolution: fixture file
(``LINEAGE_WIKI_BQ_FIXTURES``, ``profiles:`` section) -> offline guard
(``LINEAGE_WIKI_BQ_OFFLINE``) -> ``google-cloud-bigquery``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import ProfilingSpec, VerificationTableSpec
from ..connectors import SourceUnavailableError
from ..connectors.bigquery_connector import (
    FIXTURES_ENV,
    OFFLINE_ENV,
    ColumnSchema,
    TableSchema,
)

DATE_TYPES = {"DATE", "TIMESTAMP", "DATETIME"}
NUMERIC_TYPES = {"INTEGER", "INT64", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"}

_WINDOW_PREDICATES = {
    "DATE": "`{col}` >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)",
    "TIMESTAMP": "`{col}` >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)",
    "DATETIME": "`{col}` >= DATETIME_SUB(CURRENT_DATETIME(), INTERVAL {days} DAY)",
}


# --- Query planning ---------------------------------------------------------------


@dataclass(frozen=True)
class ProfileMetric:
    alias: str
    kind: str  # row_count | date_min | date_max | null_count | distinct_count | min | max
    column: str | None = None


@dataclass
class ProfileQueryPlan:
    """One deterministic aggregate query for one table."""

    table_id: str
    sql: str
    metrics: list[ProfileMetric]
    date_column: str | None
    window_days: int | None


def _profilable(columns: list[ColumnSchema]) -> list[ColumnSchema]:
    """Top-level, non-repeated columns with identifier-safe names.

    Nested RECORD children (dotted names) and repeated fields cannot be
    aggregated with the flat templates, and backticks in a name would break
    quoting — all are skipped deterministically.
    """
    return [
        c
        for c in columns
        if "." not in c.name and "`" not in c.name and c.mode != "REPEATED"
    ]


def pick_date_column(
    schema: TableSchema, table_spec: VerificationTableSpec | None
) -> ColumnSchema | None:
    """Configured date column > time-partitioning field > first date-typed
    column, always resolved against the loaded schema."""
    columns = {c.name: c for c in _profilable(schema.columns)}
    if table_spec and table_spec.date_column:
        return columns.get(table_spec.date_column)
    part = schema.partitioning or {}
    if part.get("kind") == "time" and part.get("field") in columns:
        return columns[part["field"]]
    return next(
        (c for c in _profilable(schema.columns) if c.type.upper() in DATE_TYPES), None
    )


def build_profile_plan(
    schema: TableSchema,
    profiling: ProfilingSpec,
    table_spec: VerificationTableSpec | None = None,
) -> ProfileQueryPlan | None:
    """Build the single aggregate profiling query for one table. Returns
    None when every profiling signal is disabled or nothing is profilable."""
    columns = _profilable(schema.columns)
    by_name = {c.name: c for c in columns}

    def selected(configured: list[str], detected: list[ColumnSchema]) -> list[ColumnSchema]:
        if configured:
            return [by_name[n] for n in configured if n in by_name]
        return detected

    date_col = pick_date_column(schema, table_spec)
    metrics: list[ProfileMetric] = []
    exprs: list[str] = []

    def add(alias: str, kind: str, expr: str, column: str | None = None) -> None:
        metrics.append(ProfileMetric(alias=alias, kind=kind, column=column))
        exprs.append(f"  {expr} AS {alias}")

    if profiling.include_row_count:
        add("row_count", "row_count", "COUNT(*)")

    if date_col is not None:
        add(f"date_min__{date_col.name}", "date_min", f"MIN(`{date_col.name}`)", date_col.name)
        add(f"date_max__{date_col.name}", "date_max", f"MAX(`{date_col.name}`)", date_col.name)

    if profiling.include_null_counts:
        null_cols = selected(
            table_spec.null_columns if table_spec else [],
            [c for c in columns if c.mode != "REQUIRED"],
        )
        for c in null_cols:
            add(f"null__{c.name}", "null_count", f"COUNTIF(`{c.name}` IS NULL)", c.name)

    if profiling.include_distinct_counts:
        dim_cols = selected(
            table_spec.dimension_columns if table_spec else [],
            [c for c in columns if c.type.upper() == "STRING"],
        )
        for c in dim_cols:
            add(
                f"distinct__{c.name}",
                "distinct_count",
                f"APPROX_COUNT_DISTINCT(`{c.name}`)",
                c.name,
            )

    if profiling.include_min_max:
        num_cols = selected(
            table_spec.numeric_columns if table_spec else [],
            [c for c in columns if c.type.upper() in NUMERIC_TYPES],
        )
        for c in num_cols:
            add(f"min__{c.name}", "min", f"MIN(`{c.name}`)", c.name)
            add(f"max__{c.name}", "max", f"MAX(`{c.name}`)", c.name)

    if not metrics:
        return None

    sql = "SELECT\n" + ",\n".join(exprs) + f"\nFROM `{schema.table_id}`"
    window_days: int | None = None
    if date_col is not None and date_col.type.upper() in _WINDOW_PREDICATES:
        window_days = profiling.date_window_days
        predicate = _WINDOW_PREDICATES[date_col.type.upper()].format(
            col=date_col.name, days=window_days
        )
        sql += f"\nWHERE {predicate}"

    return ProfileQueryPlan(
        table_id=schema.table_id,
        sql=sql,
        metrics=metrics,
        date_column=date_col.name if date_col else None,
        window_days=window_days,
    )


# --- Query clients ----------------------------------------------------------------


class FixtureProfileClient:
    """Mocked query results from the schema fixture file: the ``profiles:``
    section answers profiling plans (keyed by fully qualified table name,
    semantic keys row_count / date / nulls / distinct / min_max) and the
    ``formula_checks:`` section answers formula plans (keyed by check name,
    with checked_rows / mismatch_rows)."""

    kind = "fixtures"

    def __init__(
        self,
        profiles: dict[str, dict],
        formulas: dict[str, dict] | None = None,
        origin: str = "<memory>",
    ) -> None:
        self._profiles = profiles
        self._formulas = formulas or {}
        self.origin = origin

    @classmethod
    def from_file(cls, path: str | Path) -> "FixtureProfileClient":
        path = Path(path)
        if not path.is_file():
            raise SourceUnavailableError(
                f"BigQuery fixture file not found: {path} ({FIXTURES_ENV})"
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
        return cls(
            data.get("profiles", {}),
            formulas=data.get("formula_checks", {}),
            origin=str(path),
        )

    def run_formula(self, plan, max_bytes_billed: int) -> dict[str, Any]:
        return dict(self._formulas.get(plan.name, {}))

    def run_profile(self, plan: ProfileQueryPlan, max_bytes_billed: int) -> dict[str, Any]:
        fixture = self._profiles.get(plan.table_id, {})
        row: dict[str, Any] = {}
        for m in plan.metrics:
            if m.kind == "row_count":
                row[m.alias] = fixture.get("row_count")
            elif m.kind in ("date_min", "date_max"):
                bounds = (fixture.get("date") or {}).get(m.column) or {}
                row[m.alias] = bounds.get(m.kind.removeprefix("date_"))
            elif m.kind == "null_count":
                row[m.alias] = (fixture.get("nulls") or {}).get(m.column)
            elif m.kind == "distinct_count":
                row[m.alias] = (fixture.get("distinct") or {}).get(m.column)
            elif m.kind in ("min", "max"):
                bounds = (fixture.get("min_max") or {}).get(m.column) or {}
                row[m.alias] = bounds.get(m.kind)
        return row


class GoogleProfileClient:
    """Aggregate profiling and formula queries over
    ``google-cloud-bigquery``. Every query runs with ``maximum_bytes_billed``
    set and standard SQL."""

    kind = "google"

    def __init__(self, project: str | None = None) -> None:
        from google.cloud import bigquery  # optional `bigquery` extra

        self._bigquery = bigquery
        self._client = bigquery.Client(project=project)

    def _query_single_row(self, sql: str, max_bytes_billed: int) -> dict[str, Any]:
        job_config = self._bigquery.QueryJobConfig(
            maximum_bytes_billed=max_bytes_billed,
            use_legacy_sql=False,
        )
        rows = list(self._client.query(sql, job_config=job_config).result())
        return dict(rows[0]) if rows else {}

    def run_profile(self, plan: ProfileQueryPlan, max_bytes_billed: int) -> dict[str, Any]:
        return self._query_single_row(plan.sql, max_bytes_billed)

    def run_formula(self, plan, max_bytes_billed: int) -> dict[str, Any]:
        return self._query_single_row(plan.sql, max_bytes_billed)


@dataclass
class ResolvedProfileClient:
    client: Any | None
    kind: str | None = None
    reason: str | None = None


def resolve_profile_client(project: str | None = None) -> ResolvedProfileClient:
    fixtures = os.environ.get(FIXTURES_ENV)
    if fixtures:
        return ResolvedProfileClient(FixtureProfileClient.from_file(fixtures), "fixtures")
    if os.environ.get(OFFLINE_ENV):
        return ResolvedProfileClient(None, reason=f"offline mode via {OFFLINE_ENV}")
    try:
        return ResolvedProfileClient(GoogleProfileClient(project), "google")
    except ImportError:
        reason = "google-cloud-bigquery is not installed (install the `bigquery` extra)"
    except Exception as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        reason = f"no usable BigQuery credentials: {first_line}"
    return ResolvedProfileClient(None, reason=reason)


# --- Result assembly --------------------------------------------------------------


@dataclass
class ProfileResult:
    """Profiling values for one table. Values here are written to run
    metadata only — OKF pages receive derived conclusions, never values."""

    table_id: str
    sql: str
    date_column: str | None = None
    window_days: int | None = None
    row_count: int | None = None
    date_min: str | None = None
    date_max: str | None = None
    null_counts: dict[str, int] = field(default_factory=dict)
    distinct_counts: dict[str, int] = field(default_factory=dict)
    min_max: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "table": self.table_id,
            "sql": self.sql,
            "date_column": self.date_column,
            "window_days": self.window_days,
            "row_count": self.row_count,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "null_counts": self.null_counts,
            "distinct_counts": self.distinct_counts,
            "min_max": self.min_max,
        }


def run_profile_plan(
    plan: ProfileQueryPlan, client: Any, max_bytes_billed: int
) -> ProfileResult:
    row = client.run_profile(plan, max_bytes_billed)
    result = ProfileResult(
        table_id=plan.table_id,
        sql=plan.sql,
        date_column=plan.date_column,
        window_days=plan.window_days,
    )
    for m in plan.metrics:
        value = row.get(m.alias)
        if m.kind == "row_count":
            result.row_count = value
        elif m.kind == "date_min":
            result.date_min = str(value) if value is not None else None
        elif m.kind == "date_max":
            result.date_max = str(value) if value is not None else None
        elif m.kind == "null_count" and value is not None:
            result.null_counts[m.column] = value
        elif m.kind == "distinct_count" and value is not None:
            result.distinct_counts[m.column] = value
        elif m.kind in ("min", "max") and value is not None:
            result.min_max.setdefault(m.column, {})[m.kind] = (
                value if isinstance(value, (int, float)) else str(value)
            )
    return result
