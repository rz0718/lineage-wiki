"""`lineage-wiki` CLI: init, generate, update, verify-bq, validate, configure."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from . import __version__
from .agent.llm_pipeline import LLMPipelineError
from .agent.runner import GenerateError, run_generate, run_init, run_update
from .config import ChainConfig, ConfigError, load_config
from .connectors import SourceUnavailableError
from .credentials import (
    LocalModelConfig,
    config_path,
    describe_local_config,
    load_local_config,
    save_local_config,
)
from .okf.validator import ValidationReport, validate_tree
from .okf.verifier import VerificationError, run_verify_bq
from .providers import ProviderError

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


def _print_write_outcome(result, dry_run: bool) -> None:
    """Shared created/updated/skipped/index reporting for generate runs."""
    create_label = "create" if dry_run else "created"
    update_label = "update" if dry_run else "updated"
    for rel in result.created:
        typer.echo(f"{create_label:<9} {rel}")
    for rel in result.updated:
        typer.echo(f"{update_label:<9} {rel}")
        if rel in result.diffs:
            typer.echo(f"          diff: {result.diffs[rel]}")
    for rel in result.unchanged:
        typer.echo(f"unchanged {rel}")
    for rel in result.skipped:
        typer.secho(
            f"protected {rel} (exists but is not tool-generated; left untouched)",
            fg=typer.colors.YELLOW,
        )
    for rel in result.indexes_written:
        typer.echo(f"index     {rel}")
        if rel in result.diffs:
            typer.echo(f"          diff: {result.diffs[rel]}")
    for rel in result.indexes_skipped:
        typer.secho(
            f"protected {rel} (existing index is not tool-generated; left untouched)",
            fg=typer.colors.YELLOW,
        )


@app.command()
def generate(
    config: Annotated[
        Path, typer.Option("--config", help="Chain YAML config file.")
    ],
    root: RootOpt = Path("."),
    target_repo: Annotated[
        Optional[Path],
        typer.Option(
            "--target-repo",
            help="Target OKF repo root (alias for --root; takes precedence).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report what the run would do without writing any file.",
        ),
    ] = False,
    use_llm: Annotated[
        bool,
        typer.Option(
            "--use-llm",
            help=(
                "Enrich planned pages with grounded, evidence-cited LLM "
                "content. Default is fully deterministic (no model calls)."
            ),
        ),
    ] = False,
) -> None:
    """Deterministically scaffold one chain's OKF pages, indexes, and manifest."""
    if target_repo is not None:
        root = target_repo
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        result = run_generate(cfg, root, dry_run=dry_run, use_llm=use_llm)
    except (GenerateError, SourceUnavailableError, ProviderError, LLMPipelineError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)

    for line in result.llm:
        typer.echo(f"llm       {line}")

    if dry_run:
        typer.secho(
            "DRY RUN — no files were written (writes below are what a real "
            "run would do).",
            fg=typer.colors.CYAN,
        )
        typer.echo("evidence:")
        for line in result.evidence:
            typer.echo(f"  - {line}")

    _print_write_outcome(result, dry_run)

    would = "would be written" if dry_run else "written"
    typer.echo(
        f"manifest  {would if result.manifest_written else 'unchanged (no-op run)'}"
    )
    if dry_run:
        typer.echo("run       not recorded (dry run)")
    else:
        typer.echo(f"run       {result.run_file or 'not recorded (no content change)'}")

    typer.echo(f"known gaps recorded: {len(result.gaps)}")
    if dry_run:
        for gap in result.gaps:
            typer.echo(f"  - {gap}")
        typer.echo("bigquery verification:")
        for line in result.verification:
            typer.echo(f"  - {line}")
        typer.echo("validation (as if the run had been applied):")

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

    _print_write_outcome(result, dry_run=False)
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
    """Verify configured BigQuery tables (schema_only, profile, or
    formula_check mode).

    Schema metadata checks plus, per mode, safe aggregate profiling or
    deterministic formula checks (never SELECT *; always capped by
    max_bytes_billed). Detailed results go to .lineage-wiki/runs/; OKF pages
    get summary conclusions only.
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
    for fr in result.formula_checks:
        status = "ok" if fr.ok else "FAIL"
        color = typer.colors.GREEN if fr.ok else typer.colors.RED
        typer.secho(
            f"{status:<9} formula {fr.check.name} — {fr.classification}", fg=color
        )
        typer.echo(f"  - {fr.conclusion()}")
    for rel in result.pages_updated:
        typer.echo(f"page      {rel} (verification sections updated)")
    for rel in result.pages_skipped:
        typer.secho(f"skipped   {rel}", fg=typer.colors.YELLOW)
    typer.echo(f"run       {result.run_file}")
    if not result.ok:
        typer.secho("verify-bq FAILED — see issues above.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("verify-bq OK.", fg=typer.colors.GREEN)


@app.command()
def configure(
    provider: Annotated[
        Optional[str],
        typer.Option("--provider", help="LLM provider: openai or anthropic."),
    ] = None,
    model: Annotated[
        Optional[str], typer.Option("--model", help="Model id.")
    ] = None,
    base_url: Annotated[
        Optional[str],
        typer.Option("--base-url", help="Override the provider API base URL."),
    ] = None,
    api_key_env: Annotated[
        Optional[str],
        typer.Option(
            "--api-key-env",
            help=(
                "NAME of the environment variable holding the API key "
                "(e.g. OPENAI_API_KEY). Keys themselves are never stored."
            ),
        ),
    ] = None,
    temperature: Annotated[
        Optional[float], typer.Option("--temperature", help="Sampling temperature.")
    ] = None,
    show: Annotated[
        bool,
        typer.Option("--show", help="Print the current configuration (no secrets)."),
    ] = False,
) -> None:
    """Store provider/model settings in ~/.lineage-wiki/config.yml.

    Secrets never touch this file or this terminal: only the *name* of the
    environment variable to read the API key from is recorded.
    """
    existing = load_local_config() or LocalModelConfig()
    if show and provider is None and model is None:
        if not existing.provider:
            typer.secho(
                f"no local model config yet ({config_path()}) — run "
                "`lineage-wiki configure --provider openai --model <id>`",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit()
        for line in describe_local_config(existing):
            typer.echo(line)
        raise typer.Exit()

    updated = LocalModelConfig(
        provider=(provider if provider is not None else existing.provider),
        model=(model if model is not None else existing.model),
        base_url=(base_url if base_url is not None else existing.base_url),
        api_key_env=(api_key_env if api_key_env is not None else existing.api_key_env),
        temperature=(
            temperature if temperature is not None else existing.temperature
        ),
    )
    try:
        path = save_local_config(updated)
    except ProviderError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho(f"wrote {path} (mode 600, no secrets stored)", fg=typer.colors.GREEN)
    for line in describe_local_config(updated):
        typer.echo(line)


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
