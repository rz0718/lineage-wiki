"""`lineage-wiki` CLI: init, generate, update, verify-bq, validate."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from . import __version__
from .agent.runner import GenerateError, run_generate, run_init, run_update
from .config import ChainConfig, ConfigError, load_config
from .connectors import SourceUnavailableError
from .okf.validator import ValidationReport, validate_tree
from .okf.verifier import VerificationError, run_verify_bq

app = typer.Typer(
    name="lineage-wiki",
    help=(
        "OpenWiki for data products: generate and maintain Open Knowledge "
        "Format (OKF) documentation from chain configs."
    ),
    add_completion=False,
    no_args_is_help=True,
)

RootOpt = Annotated[
    Path,
    typer.Option("--root", help="Target repo root (where okf/ lives).", show_default=True),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"lineage-wiki {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = None,
) -> None:
    """Lineage Wiki."""


def _print_report(report: ValidationReport, strict: bool) -> None:
    typer.echo(f"Checked {report.n_pages} okf pages, {report.n_links} links/refs.")
    for issue in report.errors:
        typer.secho(f"  error: {issue}", fg=typer.colors.RED)
    for issue in report.warnings:
        typer.secho(f"  warning: {issue}", fg=typer.colors.YELLOW)
    if report.failed(strict):
        typer.secho(
            f"FAILED — {len(report.errors)} error(s), {len(report.warnings)} warning(s).",
            fg=typer.colors.RED,
        )
    else:
        suffix = f" ({len(report.warnings)} warning(s))" if report.warnings else ""
        typer.secho(f"OK — knowledge graph is clean{suffix}.", fg=typer.colors.GREEN)


@app.command()
def init(
    root: RootOpt = Path("."),
    agents: Annotated[
        bool,
        typer.Option(
            "--agents",
            help="Append the OKF instruction block to AGENTS.md and CLAUDE.md.",
        ),
    ] = False,
) -> None:
    """Scaffold config examples, prompt stubs, and the okf/ structure."""
    result = run_init(root, agents=agents)
    for rel in result.created:
        typer.echo(f"created   {rel}")
    for rel in result.skipped:
        typer.echo(f"exists    {rel}")
    typer.secho(
        f"init done — {len(result.created)} file(s) created, "
        f"{len(result.skipped)} already present.",
        fg=typer.colors.GREEN,
    )


@app.command()
def generate(
    config: Annotated[
        Path, typer.Option("--config", help="Chain YAML config file.")
    ],
    root: RootOpt = Path("."),
) -> None:
    """Deterministically scaffold one chain's OKF pages, indexes, and manifest."""
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        result = run_generate(cfg, root)
    except (GenerateError, SourceUnavailableError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    for rel in result.created:
        typer.echo(f"created   {rel}")
    for rel in result.updated:
        typer.echo(f"updated   {rel}")
    for rel in result.unchanged:
        typer.echo(f"unchanged {rel}")
    for rel in result.skipped:
        typer.secho(
            f"skipped   {rel} (exists but is not tool-generated; left untouched)",
            fg=typer.colors.YELLOW,
        )
    for rel in result.indexes_written:
        typer.echo(f"index     {rel}")
    typer.echo(
        f"manifest  {'written' if result.manifest_written else 'unchanged (no-op run)'}"
    )
    typer.echo(f"run       {result.run_file or 'not recorded (no content change)'}")
    typer.echo(f"known gaps recorded: {len(result.gaps)}")

    _print_report(result.report, strict=False)
    if result.report.failed():
        raise typer.Exit(code=1)


def _load_config_or_exit(config: Path) -> ChainConfig:
    try:
        return load_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def update(
    config: Annotated[
        Path, typer.Option("--config", help="Chain YAML config file.")
    ],
    root: RootOpt = Path("."),
) -> None:
    """Deterministic update: diff source fingerprints, rewrite only affected pages."""
    cfg = _load_config_or_exit(config)
    try:
        result = run_update(cfg, root)
    except (GenerateError, SourceUnavailableError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if result.noop:
        typer.secho(
            "no-op: no source changes detected — no files written, no run "
            "metadata recorded.",
            fg=typer.colors.GREEN,
        )
        return

    typer.echo("evidence changes:")
    for line in result.changes.describe():
        typer.echo(f"  - {line}")
    typer.echo("impact plan (pages to consider):")
    for rel, reasons in result.impact.items():
        typer.echo(f"  - {rel}  [{'; '.join(reasons)}]")

    for rel in result.created:
        typer.echo(f"created   {rel}")
    for rel in result.updated:
        typer.echo(f"updated   {rel}")
    for rel in result.unchanged:
        typer.echo(f"unchanged {rel}")
    for rel in result.skipped:
        typer.secho(
            f"skipped   {rel} (exists but is not tool-generated; left untouched)",
            fg=typer.colors.YELLOW,
        )
    for rel in result.indexes_written:
        typer.echo(f"index     {rel}")
    typer.echo(
        f"manifest  {'written' if result.manifest_written else 'unchanged'}"
    )
    typer.echo(f"run       {result.run_file or 'not recorded (no content change)'}")

    if result.report is not None:
        _print_report(result.report, strict=False)
        if result.report.failed():
            raise typer.Exit(code=1)


@app.command("verify-bq")
def verify_bq(
    config: Annotated[
        Path, typer.Option("--config", help="Chain YAML config file.")
    ],
    root: RootOpt = Path("."),
) -> None:
    """Verify configured BigQuery tables (schema_only or profile mode).

    Schema metadata checks plus, in profile mode, safe aggregate queries
    (never SELECT *; always capped by max_bytes_billed). Detailed results go
    to .lineage-wiki/runs/; OKF pages get summary conclusions only.
    """
    cfg = _load_config_or_exit(config)
    try:
        result = run_verify_bq(cfg, root)
    except (VerificationError, SourceUnavailableError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.echo(f"mode      {result.mode}")
    for note in result.notes:
        typer.secho(f"note      {note}", fg=typer.colors.YELLOW)
    for tv in result.tables:
        status = "ok" if tv.ok else "FAIL"
        color = typer.colors.GREEN if tv.ok else typer.colors.RED
        typer.secho(f"{status:<9} {tv.table}", fg=color)
        for line in tv.conclusions:
            typer.echo(f"  - {line}")
    for rel in result.pages_updated:
        typer.echo(f"page      {rel} (Verification Status updated)")
    for rel in result.pages_skipped:
        typer.secho(f"skipped   {rel}", fg=typer.colors.YELLOW)
    typer.echo(f"run       {result.run_file}")
    if not result.ok:
        typer.secho("verify-bq FAILED — see issues above.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("verify-bq OK.", fg=typer.colors.GREEN)


@app.command()
def validate(
    root: RootOpt = Path("."),
    okf_dir: Annotated[
        str, typer.Option("--okf-dir", help="OKF directory name under the root.")
    ] = "okf",
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Treat warnings (e.g. section gaps on hand-written pages) as failures."),
    ] = False,
) -> None:
    """Validate frontmatter, page types, required sections, links, refs, and placeholders."""
    report = validate_tree(root, okf_dir=okf_dir)
    _print_report(report, strict)
    if report.failed(strict):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
