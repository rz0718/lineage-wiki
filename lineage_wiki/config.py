"""Chain config parsing and validation (spec section 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .util import slugify


class ConfigError(Exception):
    """Raised when a chain config file cannot be loaded or validated."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChainSpec(_StrictModel):
    id: str
    slug: str = ""
    name: str
    domain: str | None = None
    owner: str | None = None
    description: str = ""

    @field_validator("id", "name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be non-empty")
        return value

    def model_post_init(self, __context) -> None:
        if not self.slug:
            self.slug = slugify(self.id)


class RawDocSource(_StrictModel):
    path: str
    type: str = "methodology"
    required: bool = False


class RepoSource(_StrictModel):
    name: str
    host: str = "github"
    url: str | None = None
    branch: str = "main"
    local_path: str | None = None
    paths: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    required: bool = True


class BigQuerySource(_StrictModel):
    project: str | None = None
    datasets: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    include_sample_rows: bool = False
    required: bool = True

    @field_validator("tables")
    @classmethod
    def _valid_table_names(cls, tables: list[str]) -> list[str]:
        for table in tables:
            parts = table.strip().strip("`").split(".")
            if len(parts) not in (2, 3) or not all(p.strip() for p in parts):
                raise ValueError(
                    f"invalid BigQuery table name {table!r}; expected "
                    "[project.]dataset.table"
                )
        return tables

    def model_post_init(self, __context) -> None:
        for table in self.tables:
            if table.strip().strip("`").count(".") == 1 and not self.project:
                raise ValueError(
                    f"BigQuery table {table!r} has no project part and no "
                    "sources.bigquery.project is configured"
                )


class ReportSource(_StrictModel):
    name: str
    type: str = "slack_or_dashboard"
    url: str = ""
    source_mapping_notes: str = ""
    required: bool = False


class HumanNote(_StrictModel):
    title: str
    content: str


class MetricInput(_StrictModel):
    """Business term / metric supplied as chain input (optional)."""

    name: str
    definition: str = ""
    unit: str = ""
    grain: str = ""


class SourcesSpec(_StrictModel):
    raw_docs: list[RawDocSource] = Field(default_factory=list)
    repos: list[RepoSource] = Field(default_factory=list)
    bigquery: BigQuerySource | None = None
    reports: list[ReportSource] = Field(default_factory=list)
    human_notes: list[HumanNote] = Field(default_factory=list)
    metrics: list[MetricInput] = Field(default_factory=list)


class GenerationSpec(_StrictModel):
    output_dir: str = "okf"
    raw_files_dir: str = "raw_files"
    overwrite_policy: Literal["fail_if_exists", "update_existing"] = "update_existing"
    create_missing_metrics: bool = True
    update_indexes: bool = True
    require_citations: bool = True
    mark_unknowns_as_gaps: bool = True
    preserve_manual_sections: bool = True


class ModelSpec(_StrictModel):
    provider: str = "openai"
    model: str = ""
    temperature: float = 0.0


class ValidationSpec(_StrictModel):
    require_frontmatter: bool = True
    require_links_resolve: bool = True
    require_frontmatter_refs_resolve: bool = True
    require_source_citations: bool = True
    fail_on_uncited_formula: bool = True
    fail_on_placeholders_outside_known_gaps: bool = True


class SampleRowsSpec(_StrictModel):
    """Accepted for spec compatibility; sample rows are never read in
    phase 1 (schema and aggregate profiling only)."""

    enabled: bool = False
    max_rows: int = 20


class ProfilingSpec(_StrictModel):
    enabled: bool = True
    date_window_days: int = Field(default=90, gt=0)
    include_row_count: bool = True
    include_null_counts: bool = True
    include_distinct_counts: bool = True
    include_min_max: bool = True


class FormulaChecksSpec(_StrictModel):
    """Accepted for spec compatibility; formula checks land in phase 2."""

    enabled: bool = False
    date_window_days: int = 90
    tolerance: dict = Field(default_factory=dict)
    checks: list[dict] = Field(default_factory=list)


class StoreResultsSpec(_StrictModel):
    # Detailed query results belong in run metadata only; OKF pages get
    # summary conclusions (or nothing).
    okf_pages: Literal["summary_only", "none"] = "summary_only"
    run_metadata: Literal["detailed"] = "detailed"


class VerificationTableSpec(_StrictModel):
    """Optional per-table verification config: expected columns and which
    columns to profile. Anything unset is detected from the loaded schema."""

    table: str
    expect_columns: list[str] = Field(default_factory=list)
    date_column: str | None = None
    dimension_columns: list[str] = Field(default_factory=list)
    numeric_columns: list[str] = Field(default_factory=list)
    null_columns: list[str] = Field(default_factory=list)


class BigQueryVerificationSpec(_StrictModel):
    enabled: bool = False
    mode: Literal["schema_only", "profile", "formula_check", "full_verification"] = (
        "schema_only"
    )
    max_bytes_billed: int = Field(default=1_000_000_000, gt=0)
    tables: list[VerificationTableSpec] = Field(default_factory=list)
    sample_rows: SampleRowsSpec = Field(default_factory=SampleRowsSpec)
    profiling: ProfilingSpec = Field(default_factory=ProfilingSpec)
    formula_checks: FormulaChecksSpec = Field(default_factory=FormulaChecksSpec)
    store_results: StoreResultsSpec = Field(default_factory=StoreResultsSpec)

    def table_spec(self, table: str) -> VerificationTableSpec | None:
        return next((t for t in self.tables if t.table == table), None)


class ChainConfig(_StrictModel):
    chain: ChainSpec
    sources: SourcesSpec = Field(default_factory=SourcesSpec)
    generation: GenerationSpec = Field(default_factory=GenerationSpec)
    model: ModelSpec = Field(default_factory=ModelSpec)
    validation: ValidationSpec = Field(default_factory=ValidationSpec)
    bigquery_verification: BigQueryVerificationSpec = Field(
        default_factory=BigQueryVerificationSpec
    )


def load_config(path: str | Path) -> ChainConfig:
    """Load and validate a chain YAML config file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: config must be a YAML mapping")
    try:
        return ChainConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"{path}: invalid config: {problems}") from exc
