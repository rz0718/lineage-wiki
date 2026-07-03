"""Deterministic symbol indexing over loaded repo files (no LLM).

Locates each configured symbol inside the configured, loaded paths and
records where it is defined or referenced. Kinds, most specific first:

- ``def`` / ``class``: a Python definition line
- ``assignment``: a top-level-looking ``SYMBOL = ...`` line
- ``reference``: any other word-boundary occurrence
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..connectors.local_repo_connector import RepoLoadResult

_KIND_ORDER = {"def": 0, "class": 0, "assignment": 1, "reference": 2}


@dataclass
class SymbolHit:
    symbol: str
    path: str  # repo-relative
    line: int
    kind: str  # "def" | "class" | "assignment" | "reference"

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line}"


@dataclass
class CodeIndex:
    repo_name: str
    hits: dict[str, list[SymbolHit]] = field(default_factory=dict)  # symbol -> hits
    missing_symbols: list[str] = field(default_factory=list)

    def best_hit(self, symbol: str) -> SymbolHit | None:
        candidates = self.hits.get(symbol)
        return candidates[0] if candidates else None


def _scan_file(symbol: str, path: str, content: str) -> SymbolHit | None:
    """Best hit for one symbol in one file (definition beats reference)."""
    escaped = re.escape(symbol)
    def_re = re.compile(rf"^\s*(?:async\s+def|def|class)\s+{escaped}\b")
    assign_re = re.compile(rf"^{escaped}\s*=")
    word_re = re.compile(rf"\b{escaped}\b")

    best: SymbolHit | None = None
    for lineno, line in enumerate(content.splitlines(), start=1):
        if def_re.match(line):
            kind = "class" if line.lstrip().startswith("class") else "def"
            return SymbolHit(symbol, path, lineno, kind)
        if best is None or _KIND_ORDER[best.kind] > 1:
            if assign_re.match(line):
                best = SymbolHit(symbol, path, lineno, "assignment")
            elif best is None and word_re.search(line):
                best = SymbolHit(symbol, path, lineno, "reference")
    return best


def index_repo(load: RepoLoadResult) -> CodeIndex:
    index = CodeIndex(repo_name=load.repo.name)
    for symbol in load.repo.symbols:
        hits: list[SymbolHit] = []
        for item in load.files:
            hit = _scan_file(symbol, item.metadata["path"], item.content)
            if hit:
                hits.append(hit)
        hits.sort(key=lambda h: (_KIND_ORDER[h.kind], h.path, h.line))
        if hits:
            index.hits[symbol] = hits
        else:
            index.missing_symbols.append(symbol)
    return index
