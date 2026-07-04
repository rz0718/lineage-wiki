"""LLM provider abstraction.

Normal operation is fully deterministic and never touches a model; providers
are resolved only when a run is started with ``--use-llm``. Resolution order:

1. ``LINEAGE_WIKI_LLM_FIXTURES`` — mock provider reading canned stage
   responses from a YAML/JSON file (unit tests and credential-free demos).
2. Local user config written by ``lineage-wiki configure``
   (``~/.lineage-wiki/config.yml`` — outside the target repo).
3. The chain config's ``model:`` block.

API keys are read from environment variables at request time and are never
stored, logged, or echoed back.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

FIXTURES_ENV = "LINEAGE_WIKI_LLM_FIXTURES"

# Pipeline stage names — fixture files key their responses by these.
STAGES = ("page_planner", "extractor", "writer", "reviewer")


class ProviderError(Exception):
    """Raised when a provider cannot be resolved or a completion fails."""


@dataclass
class LLMResponse:
    text: str
    model: str = ""


class LLMProvider:
    """Interface every provider implements. ``stage`` names the pipeline
    step making the call; the mock provider dispatches on it."""

    name = "base"

    def complete(
        self,
        *,
        stage: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        raise NotImplementedError


def _api_key(env_name: str, provider: str) -> str:
    key = os.environ.get(env_name, "")
    if not key:
        raise ProviderError(
            f"{provider} provider needs an API key in ${env_name} — export it "
            "in the environment (keys are never stored by lineage-wiki)"
        )
    return key


def _post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # exc.read() may echo request details; keep the error surface small
        # and never include headers (they carry the API key).
        raise ProviderError(f"{url}: HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"{url}: {exc.reason}") from None


@dataclass
class OpenAIProvider(LLMProvider):
    """OpenAI-compatible chat-completions endpoint (OpenAI, or anything that
    speaks the same API via ``base_url``)."""

    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    name = "openai"

    def complete(self, *, stage, system, prompt, temperature=0.0, max_tokens=4096):
        key = _api_key(self.api_key_env, self.name)
        data = _post_json(
            f"{self.base_url.rstrip('/')}/chat/completions",
            {
                "model": self.model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
            {"Authorization": f"Bearer {key}"},
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("openai response had no choices[0].message.content")
        return LLMResponse(text=text or "", model=str(data.get("model", self.model)))


@dataclass
class AnthropicProvider(LLMProvider):
    """Anthropic Messages API provider (optional)."""

    model: str
    base_url: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    name = "anthropic"

    def complete(self, *, stage, system, prompt, temperature=0.0, max_tokens=4096):
        key = _api_key(self.api_key_env, self.name)
        data = _post_json(
            f"{self.base_url.rstrip('/')}/v1/messages",
            {
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            {"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if not text:
            raise ProviderError("anthropic response had no text content blocks")
        return LLMResponse(text=text, model=str(data.get("model", self.model)))


@dataclass
class MockProvider(LLMProvider):
    """Deterministic canned responses for tests and demos.

    ``responses`` maps a stage name to either:

    - a string — returned for every call at that stage;
    - a list of strings — consumed one per call, in order (the last entry
      repeats if the stage is called more often);
    - a mapping — the first key found as a substring of the prompt wins
      (use page rel-paths to give the writer per-page responses).
    """

    responses: dict[str, object] = field(default_factory=dict)
    origin: str = "inline"
    name = "mock"
    _cursor: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> "MockProvider":
        path = Path(path)
        if not path.exists():
            raise ProviderError(f"LLM fixtures file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ProviderError(f"{path}: LLM fixtures must be a mapping")
        responses = data.get("responses", data)
        return cls(responses=dict(responses), origin=str(path))

    def complete(self, *, stage, system, prompt, temperature=0.0, max_tokens=4096):
        entry = self.responses.get(stage)
        if entry is None:
            raise ProviderError(
                f"mock provider ({self.origin}) has no response for stage "
                f"{stage!r}"
            )
        if isinstance(entry, str):
            return LLMResponse(text=entry, model="mock")
        if isinstance(entry, list):
            index = min(self._cursor.get(stage, 0), len(entry) - 1)
            self._cursor[stage] = index + 1
            return LLMResponse(text=str(entry[index]), model="mock")
        if isinstance(entry, dict):
            for key, value in entry.items():
                if str(key) in prompt:
                    return LLMResponse(text=str(value), model="mock")
            raise ProviderError(
                f"mock provider ({self.origin}) stage {stage!r}: no key "
                "matched the prompt"
            )
        raise ProviderError(f"mock provider stage {stage!r}: unsupported entry type")


_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def validate_api_key_env(value: str) -> str:
    """``configure`` accepts environment variable *names* only — anything
    that looks like key material is refused so secrets never reach disk."""
    value = value.strip()
    if not _ENV_NAME_RE.match(value):
        raise ProviderError(
            "expected an environment variable NAME (e.g. OPENAI_API_KEY), "
            "not a value — lineage-wiki never stores API keys"
        )
    return value


def build_provider(
    *,
    provider: str,
    model: str,
    base_url: str = "",
    api_key_env: str = "",
) -> LLMProvider:
    provider = (provider or "").strip().lower()
    if provider == "mock":
        raise ProviderError(
            f"mock provider is selected via the {FIXTURES_ENV} environment "
            "variable pointing at a fixtures file"
        )
    if provider == "openai":
        if not model:
            raise ProviderError("model id is required (set model.model or run `lineage-wiki configure`)")
        kwargs: dict = {"model": model}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key_env:
            kwargs["api_key_env"] = validate_api_key_env(api_key_env)
        return OpenAIProvider(**kwargs)
    if provider == "anthropic":
        if not model:
            raise ProviderError("model id is required (set model.model or run `lineage-wiki configure`)")
        kwargs = {"model": model}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key_env:
            kwargs["api_key_env"] = validate_api_key_env(api_key_env)
        return AnthropicProvider(**kwargs)
    raise ProviderError(
        f"unknown LLM provider {provider!r} (supported: openai, anthropic; "
        f"mock via {FIXTURES_ENV})"
    )
