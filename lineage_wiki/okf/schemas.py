"""OKF page model, deterministic frontmatter rendering, and page parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

PageType = Literal[
    "Index",
    "Framework",
    "Component",
    "Output",
    "Code Link",
    "Report Template",
    "Change Check",
    "Metric",
]


class OkfPage(BaseModel):
    """Structured representation of one OKF page (spec section 9)."""

    id: str
    slug: str
    type: PageType
    title: str
    description: str
    owner: str | None = None
    status: Literal["draft", "reviewed", "approved", "deprecated"] = "draft"
    tags: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    body: str
    links: list[str] = Field(default_factory=list)


# --- Frontmatter rendering ----------------------------------------------------
#
# Hand-rolled emitter so output matches the existing catalog style byte for
# byte (plain scalars where safe, block lists, `key:` for empty values) and
# stays deterministic across PyYAML versions.

_PLAIN_SCALAR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-./:,;()'+&%]*$")
_YAML_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~"}


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = " ".join(str(value).split("\n"))
    if (
        _PLAIN_SCALAR.match(text)
        and ": " not in text
        and not text.endswith(":")
        and text == text.strip()
        and text.lower() not in _YAML_RESERVED
    ):
        return text
    dumped = yaml.safe_dump({"_": text}, width=100000, allow_unicode=True).strip()
    return dumped[2:].strip()


def _emit(key: str, value: Any, indent: int = 0) -> list[str]:
    pad = " " * indent
    if value is None:
        return [f"{pad}{key}:"]
    if isinstance(value, (str, bool, int, float)):
        return [f"{pad}{key}: {_scalar(value)}"]
    if isinstance(value, list):
        if not value:
            return [f"{pad}{key}: []"]
        lines = [f"{pad}{key}:"]
        item_pad = " " * (indent + 2)
        for item in value:
            if isinstance(item, dict):
                first = True
                for k, v in item.items():
                    prefix = f"{item_pad}- " if first else f"{item_pad}  "
                    if v is None:
                        lines.append(f"{prefix}{k}:")
                    elif isinstance(v, (str, bool, int, float)):
                        lines.append(f"{prefix}{k}: {_scalar(v)}")
                    else:
                        raise TypeError(f"unsupported nested value under {key}: {v!r}")
                    first = False
            else:
                lines.append(f"{item_pad}- {_scalar(item)}")
        return lines
    if isinstance(value, dict):
        lines = [f"{pad}{key}:"]
        for k, v in value.items():
            lines.extend(_emit(k, v, indent + 2))
        return lines
    raise TypeError(f"unsupported frontmatter value for {key}: {value!r}")


def render_frontmatter(fields: dict[str, Any]) -> str:
    """Render an ordered field mapping as a YAML frontmatter block."""
    lines = ["---"]
    for key, value in fields.items():
        lines.extend(_emit(key, value))
    lines.append("---")
    return "\n".join(lines) + "\n"


# --- Page parsing ---------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


@dataclass
class ParsedPage:
    """Result of parsing one Markdown page."""

    frontmatter: dict[str, Any] | None
    body: str
    fm_error: str | None = None


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_text, body); frontmatter_text is None if absent."""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None, text
    return match.group(1), text[match.end():]


def parse_page(text: str) -> ParsedPage:
    fm_text, body = split_frontmatter(text)
    if fm_text is None:
        return ParsedPage(frontmatter=None, body=body)
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return ParsedPage(frontmatter=None, body=body, fm_error=str(exc))
    if not isinstance(data, dict):
        return ParsedPage(frontmatter=None, body=body, fm_error="frontmatter is not a mapping")
    return ParsedPage(frontmatter=data, body=body)
