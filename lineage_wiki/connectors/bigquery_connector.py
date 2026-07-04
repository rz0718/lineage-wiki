"""BigQuery schema connector — table metadata and schema only (Milestone 5).

Safety contract (spec section 11, ``schema_only`` mode):

- No queries are ever run: no SELECT statements, no row reads, no jobs.
- Only table *metadata* is read: columns, types, column descriptions,
  partitioning, clustering, table type, view SQL, last-modified metadata.
- No live data values are stored anywhere.
- ``include_sample_rows`` is ignored in this milestone — schema only.

The client abstraction below deliberately exposes a single
``get_table_schema`` method and no query method at all, so profiling and
formula verification (later milestones) cannot sneak in through this module.

Client resolution order (``resolve_bigquery_client``):

1. ``LINEAGE_WIKI_BQ_FIXTURES=<file>`` — load schemas from a local YAML/JSON
   fixture file (mocked schemas; used by tests and credential-free runs).
2. ``LINEAGE_WIKI_BQ_OFFLINE=1`` — treat BigQuery as unavailable (the test
   suite sets this so unit tests never touch the network).
3. ``google-cloud-bigquery`` with application-default credentials, when the
   optional ``bigquery`` extra is installed.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..config import BigQuerySource
from ..ingestion.evidence import EvidenceItem
from . import SourceUnavailableError

FIXTURES_ENV = "LINEAGE_WIKI_BQ_FIXTURES"
OFFLINE_ENV = "LINEAGE_WIKI_BQ_OFFLINE"


# --- Table names ------------------------------------------------------------------


@dataclass(frozen=True)
class TableName:
    """A parsed fully qualified BigQuery table name."""

    project: str
    dataset: str
    table: str

    @property
    def fqtn(self) -> str:
        return f"{self.project}.{self.dataset}.{self.table}"

    def __str__(self) -> str:
        return self.fqtn


def parse_table_name(name: str, default_project: str | None = None) -> TableName:
    """Parse ``project.dataset.table`` (or ``dataset.table`` with a default
    project). Backticks are stripped; anything else is a config error."""
    parts = name.strip().strip("`").split(".")
    if not all(p.strip() for p in parts):
        raise ValueError(f"invalid BigQuery table name {name!r}")
    if len(parts) == 3:
        return TableName(*parts)
    if len(parts) == 2:
        if not default_project:
            raise ValueError(
                f"BigQuery table {name!r} has no project part and no "
                "sources.bigquery.project is configured"
            )
        return TableName(default_project, *parts)
    raise ValueError(
        f"invalid BigQuery table name {name!r}; expected [project.]dataset.table"
    )


# --- Schema models ----------------------------------------------------------------


class ColumnSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    mode: str = "NULLABLE"
    description: str | None = None


class TableSchema(BaseModel):
    """Schema metadata for one table or view. Never holds row data."""

    model_config = ConfigDict(extra="forbid")

    table_id: str  # fully qualified project.dataset.table
    table_type: str = "TABLE"  # "TABLE" | "VIEW"
    description: str | None = None
    columns: list[ColumnSchema] = Field(default_factory=list)
    # e.g. {"kind": "time", "type": "DAY", "field": "snapshot_date"}
    partitioning: dict[str, Any] | None = None
    clustering: list[str] = Field(default_factory=list)
    view_sql: str | None = None
    last_modified: str | None = None

    def fingerprint(self) -> str:
        # last_modified is excluded: it moves on every data load, and the
        # manifest fingerprint must only react to actual schema changes.
        canonical = yaml.safe_dump(
            self.model_dump(exclude={"last_modified"}), sort_keys=True
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_evidence(self) -> EvidenceItem:
        return EvidenceItem(
            id=f"bq-schema:{self.table_id}",
            source_type="bigquery_schema",
            source_uri=f"bigquery:{self.table_id}",
            title=self.table_id,
            content=yaml.safe_dump(self.model_dump(), sort_keys=True, allow_unicode=True),
            metadata={
                "table_type": self.table_type,
                "n_columns": len(self.columns),
                "partitioning": self.partitioning,
                "clustering": self.clustering,
                "last_modified": self.last_modified,
            },
            fingerprint=self.fingerprint(),
        )


# --- Clients ----------------------------------------------------------------------


class FixtureBigQueryClient:
    """Schema client backed by an in-memory mapping or a YAML/JSON fixture
    file — used by tests and credential-free demo runs."""

    kind = "fixtures"

    def __init__(self, tables: dict[str, dict], origin: str = "<memory>") -> None:
        self._tables = tables
        self.origin = origin

    @classmethod
    def from_file(cls, path: str | Path) -> "FixtureBigQueryClient":
        path = Path(path)
        if not path.is_file():
            raise SourceUnavailableError(
                f"BigQuery fixture file not found: {path} ({FIXTURES_ENV})"
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SourceUnavailableError(
                f"BigQuery fixture file {path} must be a mapping of table "
                "names to schemas (optionally under a top-level `tables` key)"
            )
        tables = data.get("tables", data)
        return cls(tables, origin=str(path))

    def get_table_schema(self, name: TableName) -> TableSchema | None:
        raw = self._tables.get(name.fqtn)
        if raw is None:
            return None
        return TableSchema.model_validate({"table_id": name.fqtn, **raw})


class GoogleBigQueryClient:
    """Schema client over ``google-cloud-bigquery``. Metadata calls only
    (``get_table``); this class has no query method."""

    kind = "google"

    def __init__(self, project: str | None = None) -> None:
        from google.cloud import bigquery  # optional `bigquery` extra

        self._client = bigquery.Client(project=project)

    def get_table_schema(self, name: TableName) -> TableSchema | None:
        from google.api_core.exceptions import NotFound

        try:
            table = self._client.get_table(name.fqtn)
        except NotFound:
            return None

        columns: list[ColumnSchema] = []

        def add_fields(fields, prefix: str = "") -> None:
            for f in fields:
                columns.append(
                    ColumnSchema(
                        name=prefix + f.name,
                        type=f.field_type,
                        mode=f.mode or "NULLABLE",
                        description=f.description or None,
                    )
                )
                if f.fields:  # flatten RECORD fields as parent.child
                    add_fields(f.fields, prefix=f"{prefix}{f.name}.")

        add_fields(table.schema)

        partitioning: dict[str, Any] | None = None
        if table.time_partitioning is not None:
            partitioning = {
                "kind": "time",
                "type": table.time_partitioning.type_,
                "field": table.time_partitioning.field,
            }
        elif table.range_partitioning is not None:
            partitioning = {"kind": "range", "field": table.range_partitioning.field}

        return TableSchema(
            table_id=name.fqtn,
            table_type=table.table_type or "TABLE",
            description=table.description or None,
            columns=columns,
            partitioning=partitioning,
            clustering=list(table.clustering_fields or []),
            view_sql=table.view_query if table.table_type == "VIEW" else None,
            last_modified=table.modified.isoformat() if table.modified else None,
        )


@dataclass
class ResolvedClient:
    client: Any | None
    kind: str | None = None
    reason: str | None = None  # why client is None


def resolve_bigquery_client(project: str | None = None) -> ResolvedClient:
    fixtures = os.environ.get(FIXTURES_ENV)
    if fixtures:
        return ResolvedClient(FixtureBigQueryClient.from_file(fixtures), "fixtures")
    if os.environ.get(OFFLINE_ENV):
        return ResolvedClient(None, reason=f"offline mode via {OFFLINE_ENV}")
    try:
        return ResolvedClient(GoogleBigQueryClient(project), "google")
    except ImportError:
        reason = "google-cloud-bigquery is not installed (install the `bigquery` extra)"
    except Exception as exc:  # e.g. missing application-default credentials
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        reason = f"no usable BigQuery credentials: {first_line}"
    return ResolvedClient(None, reason=reason)


# --- Loading ----------------------------------------------------------------------


@dataclass
class BigQueryLoadResult:
    source: BigQuerySource
    available: bool
    client_kind: str | None = None
    unavailable_reason: str | None = None
    # keyed by the configured table string from sources.bigquery.tables
    schemas: dict[str, TableSchema] = field(default_factory=dict)
    items: list[EvidenceItem] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)


def load_bigquery_schemas(
    source: BigQuerySource,
    *,
    client: Any | None = None,
    enforce_required: bool = True,
) -> BigQueryLoadResult:
    """Load schema metadata for every configured table.

    ``enforce_required=False`` suppresses the required-source failure so
    fingerprinting can proceed even when BigQuery is down; loading for page
    generation keeps the default and fails clearly.
    """
    names = [parse_table_name(t, source.project) for t in source.tables]

    kind = getattr(client, "kind", "injected") if client is not None else None
    if client is None:
        resolved = resolve_bigquery_client(source.project)
        client, kind = resolved.client, resolved.kind
        if client is None:
            if source.required and enforce_required:
                raise SourceUnavailableError(
                    f"required BigQuery source is unavailable: {resolved.reason}"
                )
            return BigQueryLoadResult(
                source=source, available=False, unavailable_reason=resolved.reason
            )

    result = BigQueryLoadResult(source=source, available=True, client_kind=kind)
    for configured, name in zip(source.tables, names):
        schema = client.get_table_schema(name)
        if schema is None:
            if source.required and enforce_required:
                raise SourceUnavailableError(
                    f"required BigQuery table `{configured}` was not found "
                    f"(resolved to {name.fqtn})"
                )
            result.missing_tables.append(configured)
            continue
        result.schemas[configured] = schema
        result.items.append(schema.to_evidence())
    return result
