"""Deterministic formula verification for BigQuery (verification phase 2).

Every query is rendered from one fixed template over *explicitly configured*
formula checks — no natural-language SQL, no LLM involvement, never
``SELECT *``. The template mirrors the spec example:

    SELECT
      COUNT(*) AS checked_rows,
      COUNTIF(ABS((<expression>) - (<expected>)) > <tolerance>) AS mismatch_rows
    FROM `<table>`
    WHERE `<date_column>` >= DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)

Tolerance follows the numpy ``isclose`` convention — a row mismatches when
``ABS(actual - expected) > atol + rtol * ABS(expected)`` — so a pure
absolute tolerance is just ``rtol = 0``.

Classification of a completed check is deterministic:

- ``Matches`` — rows were checked and none mismatched.
- ``Source stale`` — *every* checked row mismatches: the implementation
  behaves consistently but differently from the documented formula, which
  points at outdated documentation.
- ``Genuine conflict`` — some rows match and some do not; docs, code, and
  data disagree without a consistent pattern, so an owner must review.
- ``Missing evidence`` — the check could not be evaluated: table not found,
  referenced columns absent from the schema, or no rows in the window.

Only aggregate counts leave BigQuery; they are stored in run metadata, and
OKF pages receive the classification and conclusions only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import FormulaCheck, FormulaChecksSpec
from ..connectors.bigquery_connector import TableSchema
from .bq_profiler import _WINDOW_PREDICATES

MATCHES = "Matches"
SOURCE_STALE = "Source stale"
GENUINE_CONFLICT = "Genuine conflict"
MISSING_EVIDENCE = "Missing evidence"

_IDENTIFIER_RE = re.compile(r"`?([A-Za-z_][A-Za-z0-9_]*)`?\s*(\()?")


def extract_identifiers(expression: str) -> set[str]:
    """Column names referenced by an arithmetic expression. Tokens followed
    by ``(`` are function calls, not columns."""
    return {
        m.group(1)
        for m in _IDENTIFIER_RE.finditer(expression)
        if not m.group(2)
    }


def _format_tolerance(value: float) -> str:
    return repr(float(value))


@dataclass
class FormulaQueryPlan:
    """One rendered formula-check query."""

    name: str
    table_id: str
    sql: str
    date_column: str | None
    window_days: int | None
    tolerance_absolute: float
    tolerance_relative: float


def build_formula_plan(
    check: FormulaCheck,
    spec: FormulaChecksSpec,
    schema: TableSchema,
) -> FormulaQueryPlan:
    """Render the deterministic verification query for one configured check.

    The caller has already confirmed the schema exists and the referenced
    columns are present (otherwise the check is Missing evidence and no SQL
    is ever built or run).
    """
    atol = (
        check.tolerance_absolute
        if check.tolerance_absolute is not None
        else spec.tolerance.absolute
    )
    rtol = (
        check.tolerance_relative
        if check.tolerance_relative is not None
        else spec.tolerance.relative
    )
    expected = check.expected_expression
    diff = f"ABS(({check.expression}) - ({expected}))"
    if rtol:
        predicate = f"{diff} > {_format_tolerance(atol)} + {_format_tolerance(rtol)} * ABS({expected})"
    else:
        predicate = f"{diff} > {_format_tolerance(atol)}"

    sql = (
        "SELECT\n"
        "  COUNT(*) AS checked_rows,\n"
        f"  COUNTIF({predicate}) AS mismatch_rows\n"
        f"FROM `{schema.table_id}`"
    )

    window_days: int | None = None
    date_column = check.date_column
    if date_column:
        col = next((c for c in schema.columns if c.name == date_column), None)
        col_type = (col.type.upper() if col else "DATE")
        template = _WINDOW_PREDICATES.get(col_type, _WINDOW_PREDICATES["DATE"])
        window_days = spec.date_window_days
        sql += "\nWHERE " + template.format(col=date_column, days=window_days)

    return FormulaQueryPlan(
        name=check.name,
        table_id=schema.table_id,
        sql=sql,
        date_column=date_column,
        window_days=window_days,
        tolerance_absolute=atol,
        tolerance_relative=rtol,
    )


def classify(checked_rows: int | None, mismatch_rows: int | None) -> str:
    if not checked_rows:  # None or 0: nothing was evaluated
        return MISSING_EVIDENCE
    if not mismatch_rows:
        return MATCHES
    if mismatch_rows >= checked_rows:
        return SOURCE_STALE
    return GENUINE_CONFLICT


@dataclass
class FormulaCheckResult:
    """Outcome of one formula check. Counts are aggregate query results and
    belong in run metadata only — pages get classification + conclusion."""

    check: FormulaCheck
    classification: str
    sql: str | None = None
    checked_rows: int | None = None
    mismatch_rows: int | None = None
    tolerance_absolute: float | None = None
    tolerance_relative: float | None = None
    window_days: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.classification == MATCHES

    def conclusion(self) -> str:
        """One-line summary safe for OKF pages (no query values)."""
        name = f"Formula check `{self.check.name}`"
        formula = f"`{self.check.expression} = {self.check.expected_expression}`"
        if self.classification == MATCHES:
            return f"{name} passed: {formula} holds within tolerance on all checked rows."
        if self.classification == SOURCE_STALE:
            return (
                f"{name} classified as Source stale: every checked row diverges "
                f"from {formula}, so the implementation is consistent but the "
                "documented formula appears outdated."
            )
        if self.classification == GENUINE_CONFLICT:
            return (
                f"{name} classified as Genuine conflict: some checked rows "
                f"diverge from {formula} — owner review required."
            )
        reason = f" ({'; '.join(self.notes)})" if self.notes else ""
        return f"{name} classified as Missing evidence: {formula} could not be verified{reason}."

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.check.name,
            "table": self.check.table,
            "expression": self.check.expression,
            "expected_expression": self.check.expected_expression,
            "classification": self.classification,
            "sql": self.sql,
            "checked_rows": self.checked_rows,
            "mismatch_rows": self.mismatch_rows,
            "tolerance_absolute": self.tolerance_absolute,
            "tolerance_relative": self.tolerance_relative,
            "window_days": self.window_days,
            "notes": self.notes,
        }


def run_formula_check(
    check: FormulaCheck,
    spec: FormulaChecksSpec,
    schema: TableSchema | None,
    client: Any,
    max_bytes_billed: int,
) -> FormulaCheckResult:
    """Evaluate one configured check against BigQuery (or a mocked client)."""
    if schema is None:
        return FormulaCheckResult(
            check=check,
            classification=MISSING_EVIDENCE,
            notes=[f"table `{check.table}` was not found in BigQuery"],
        )

    columns = {c.name for c in schema.columns}
    referenced = sorted(
        extract_identifiers(check.expression)
        | extract_identifiers(check.expected_expression)
    )
    missing = [c for c in referenced if c not in columns]
    if check.date_column and check.date_column not in columns:
        missing.append(check.date_column)
    if missing:
        cols = ", ".join(f"`{c}`" for c in missing)
        return FormulaCheckResult(
            check=check,
            classification=MISSING_EVIDENCE,
            notes=[f"columns not present in the table schema: {cols}"],
        )

    plan = build_formula_plan(check, spec, schema)
    row = client.run_formula(plan, max_bytes_billed)
    checked = row.get("checked_rows")
    mismatched = row.get("mismatch_rows")
    result = FormulaCheckResult(
        check=check,
        classification=classify(checked, mismatched),
        sql=plan.sql,
        checked_rows=checked,
        mismatch_rows=mismatched,
        tolerance_absolute=plan.tolerance_absolute,
        tolerance_relative=plan.tolerance_relative,
        window_days=plan.window_days,
    )
    if result.classification == MISSING_EVIDENCE:
        result.notes.append("no rows were available to check in the window")
    return result
