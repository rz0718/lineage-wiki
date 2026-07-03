"""OKF baseline validator.

Extends the behavior of the catalog's ``scripts/validate_okf.py``:

  errors (always fail):
    1. A page under okf/ has no YAML frontmatter (or unparseable frontmatter).
    2. Frontmatter has no non-empty ``type``, or the ``type`` is not one of
       the title-cased OKF labels.
    3. A relative Markdown link does not resolve to an existing file.
    4. A frontmatter reference path does not resolve.
    5. A placeholder token (``TODO``, ``TBD``, ``???``, ``<path-TBD>``)
       appears outside a Known Gaps / open-issues style section. Tokens
       inside inline code spans are documentation about placeholders and are
       ignored.

  errors for tool-generated pages, warnings otherwise:
    6. A page is missing required sections for its type. Pages listed in
       ``.lineage-wiki/manifest.yml`` are tool-generated and held to the
       strict contract; hand-written pages get a warning (``--strict``
       escalates warnings to failures).
    7. A non-index page is not linked from its directory index (for
       ``metrics/`` this doubles as the term-registry membership check).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

from ..constants import (
    DIR_TO_TYPE,
    PAGE_TYPES,
    PLACEHOLDER_ALLOWED_SECTION_PATTERN,
    PLACEHOLDER_PATTERN,
    REF_KEYS,
    REQUIRED_SECTIONS,
)
from ..storage.manifest import load_manifest
from .schemas import parse_page

LINK_RE = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
PLACEHOLDER_RE = re.compile(PLACEHOLDER_PATTERN)
ALLOWED_SECTION_RE = re.compile(PLACEHOLDER_ALLOWED_SECTION_PATTERN, re.IGNORECASE)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


@dataclass
class Issue:
    level: str  # "error" | "warning"
    path: str  # repo-root-relative
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class ValidationReport:
    issues: list[Issue] = field(default_factory=list)
    n_pages: int = 0
    n_links: int = 0

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    def failed(self, strict: bool = False) -> bool:
        return bool(self.errors) or (strict and bool(self.warnings))


def _collect_ref_paths(value) -> list[str]:
    """Recursively collect every string ending in .md under a ref field."""
    found: list[str] = []
    if isinstance(value, str):
        if value.endswith(".md"):
            found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_ref_paths(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_collect_ref_paths(item))
    return found


def _placeholder_violations(body: str) -> list[tuple[int, str]]:
    """Return (line_number, token) for placeholders outside allowed sections."""
    violations: list[tuple[int, str]] = []
    allowed = False
    for lineno, line in enumerate(body.splitlines(), start=1):
        heading = HEADING_RE.match(line)
        if heading and heading.group(1) == "##":
            allowed = bool(ALLOWED_SECTION_RE.search(heading.group(2)))
        if allowed:
            continue
        stripped = INLINE_CODE_RE.sub("", line)
        for match in PLACEHOLDER_RE.finditer(stripped):
            violations.append((lineno, match.group(0)))
    return violations


def _check_sections(page_type: str, body: str) -> list[str]:
    """Return required section titles missing from the body."""
    headings = [m.group(2) for m in (HEADING_RE.match(l) for l in body.splitlines()) if m and m.group(1) == "##"]
    lowered = [h.lower() for h in headings]
    missing = []
    for required in REQUIRED_SECTIONS.get(page_type, ()):
        if not any(h.startswith(required.lower()) for h in lowered):
            missing.append(required)
    return missing


def validate_tree(root: str | Path, okf_dir: str = "okf") -> ValidationReport:
    """Validate an OKF repository rooted at ``root``."""
    root = Path(root).resolve()
    report = ValidationReport()
    okf_root = root / okf_dir

    manifest = load_manifest(root)
    managed = set(manifest.generated_files) if manifest else set()

    md_files = sorted(okf_root.rglob("*.md")) if okf_root.exists() else []
    if not md_files:
        report.issues.append(
            Issue("error", okf_dir, f"no Markdown pages found under {okf_dir}/")
        )
        return report

    extra_link_sources = [
        root / name for name in ("README.md", "OPERATION.md") if (root / name).exists()
    ]

    for path in md_files:
        rel = path.relative_to(root).as_posix()
        report.n_pages += 1
        parsed = parse_page(path.read_text(encoding="utf-8"))

        if parsed.frontmatter is None:
            if parsed.fm_error:
                report.issues.append(
                    Issue("error", rel, f"unparseable YAML frontmatter: {parsed.fm_error}")
                )
            else:
                report.issues.append(Issue("error", rel, "missing YAML frontmatter"))
        else:
            page_type = parsed.frontmatter.get("type")
            if not isinstance(page_type, str) or not page_type.strip():
                report.issues.append(
                    Issue("error", rel, "frontmatter has no non-empty `type` field")
                )
                page_type = None
            elif page_type not in PAGE_TYPES:
                report.issues.append(
                    Issue(
                        "error",
                        rel,
                        f"invalid page type {page_type!r} (expected one of: "
                        f"{', '.join(PAGE_TYPES)})",
                    )
                )
                page_type = None

            if page_type:
                for section in _check_sections(page_type, parsed.body):
                    level = "error" if rel in managed else "warning"
                    report.issues.append(
                        Issue(level, rel, f"missing required `## {section}` section for type {page_type!r}")
                    )

        for lineno, token in _placeholder_violations(parsed.body):
            report.issues.append(
                Issue(
                    "error",
                    rel,
                    f"placeholder {token!r} outside a Known Gaps section (line {lineno})",
                )
            )

    # Index membership: every non-index page in a known subdirectory must be
    # linked from that directory's index (metrics/index.md is the registry).
    indexed: dict[str, set[Path]] = {}
    for subdir in DIR_TO_TYPE:
        index_path = okf_root / subdir / "index.md"
        targets: set[Path] = set()
        if index_path.exists():
            body = parse_page(index_path.read_text(encoding="utf-8")).body
            for link in LINK_RE.findall(body):
                if not link.startswith(("http://", "https://")):
                    targets.add((index_path.parent / unquote(link)).resolve())
        indexed[subdir] = targets
    for path in md_files:
        if path.name == "index.md":
            continue
        parts = path.relative_to(okf_root).parts
        if len(parts) < 2 or parts[0] not in DIR_TO_TYPE:
            continue
        if path.resolve() not in indexed[parts[0]]:
            rel = path.relative_to(root).as_posix()
            level = "error" if rel in managed else "warning"
            what = "metrics registry" if parts[0] == "metrics" else "directory index"
            report.issues.append(
                Issue(level, rel, f"not listed in the {what} ({okf_dir}/{parts[0]}/index.md)")
            )

    # Link and frontmatter-ref resolution.
    for path in md_files + extra_link_sources:
        rel = path.relative_to(root).as_posix()
        base = path.parent
        parsed = parse_page(path.read_text(encoding="utf-8"))

        targets: list[tuple[str, str]] = [
            (link, "link") for link in LINK_RE.findall(parsed.body)
        ]
        if parsed.frontmatter:
            for key in REF_KEYS:
                if key in parsed.frontmatter:
                    targets.extend(
                        (ref, f"frontmatter ref ({key})")
                        for ref in _collect_ref_paths(parsed.frontmatter[key])
                    )

        for target, kind in targets:
            if target.startswith(("http://", "https://")):
                continue
            report.n_links += 1
            resolved = (base / unquote(target)).resolve()
            if not resolved.exists():
                report.issues.append(Issue("error", rel, f"broken {kind} -> {target}"))

    return report
