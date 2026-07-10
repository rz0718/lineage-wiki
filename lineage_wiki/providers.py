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

# Broadly compatible with legacy OpenAI/Anthropic models. Callers may request
# a larger value explicitly; continuation handles responses that hit this cap.
DEFAULT_MAX_TOKENS = 4096

# Pipeline stage names — fixture files key their responses by these.
STAGES = ("page_planner", "extractor", "writer", "reviewer")

# How many follow-up "continue" requests a provider may issue when a
# completion is cut off by an output-token cap. Endpoints (OpenRouter in
# particular) can clamp max_tokens well below what we request, so a long
# JSON answer may need several rounds to finish.
MAX_CONTINUATIONS = 8


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
        max_tokens: int = DEFAULT_MAX_TOKENS,
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


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Extract the provider's error message from an HTTP error response.

    Providers return JSON like ``{"error": {"message": "..."}}``; only that
    message field is surfaced (truncated), never the raw body.
    """
    try:
        data = json.loads(exc.read().decode("utf-8", errors="replace"))
    except Exception:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message", ""))
    elif isinstance(error, str):
        message = error
    else:
        message = ""
    message = message.strip()
    if not message:
        return ""
    return f" — {message[:300]}"


def _post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    body = json.dumps(payload).encode("utf-8")
    # One retry on timeout only: a stalled completion is the common transient
    # failure, and losing a whole multi-stage pipeline run to a single slow
    # request is far more expensive than one duplicate attempt.
    for attempt in (1, 2):
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", **headers}
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Never echo the raw body or headers (they can carry request
            # details / the API key); surface only the provider's structured
            # error message, which is what a 4xx needs to be actionable.
            raise ProviderError(
                f"{url}: HTTP {exc.code} {exc.reason}{_error_detail(exc)}"
            ) from None
        except urllib.error.URLError as exc:
            # A connect-phase timeout arrives as URLError wrapping a
            # TimeoutError; treat it like the read-phase one below.
            if isinstance(exc.reason, TimeoutError) and attempt == 1:
                continue
            raise ProviderError(f"{url}: {exc.reason}") from None
        except TimeoutError as exc:
            # A timeout while reading the response body (after headers arrive)
            # surfaces as a bare TimeoutError, not URLError — catch it
            # explicitly so large/slow completions fail cleanly instead of
            # crashing with a raw traceback.
            if attempt == 1:
                continue
            raise ProviderError(
                f"{url}: request timed out waiting for a response "
                "(retried once)"
            ) from exc
    raise AssertionError("unreachable")


@dataclass
class OpenAIProvider(LLMProvider):
    """OpenAI-compatible chat-completions endpoint (OpenAI, or anything that
    speaks the same API via ``base_url``)."""

    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    name = "openai"

    def complete(
        self,
        *,
        stage,
        system,
        prompt,
        temperature=0.0,
        max_tokens=DEFAULT_MAX_TOKENS,
    ):
        key = _api_key(self.api_key_env, self.name)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        accumulated = ""
        model_seen = self.model
        for _ in range(1 + MAX_CONTINUATIONS):
            # On continuation rounds the partial answer is sent back as a
            # trailing assistant message; OpenRouter (and Anthropic-style
            # backends) treat that as a prefill and resume mid-token-stream.
            # Anthropic backends reject prefill with trailing whitespace
            # (HTTP 400), so strip it (insignificant between JSON tokens).
            if accumulated:
                accumulated = accumulated.rstrip()
            request_messages = messages + (
                [{"role": "assistant", "content": accumulated}] if accumulated else []
            )
            data = _post_json(
                f"{self.base_url.rstrip('/')}/chat/completions",
                {
                    "model": self.model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "messages": request_messages,
                },
                {"Authorization": f"Bearer {key}"},
            )
            try:
                choice = data["choices"][0]
                message = choice["message"]
                text = message["content"]
            except (KeyError, IndexError, TypeError):
                raise ProviderError("openai response had no choices[0].message.content")
            model_seen = str(data.get("model", self.model))
            if not text and not accumulated:
                finish_reason = choice.get("finish_reason", "unknown")
                extra_keys = [k for k in message if k not in ("role", "content")]
                reasoning_len = 0
                for field_name in ("reasoning", "reasoning_content"):
                    value = message.get(field_name)
                    if isinstance(value, str):
                        reasoning_len += len(value)
                raise ProviderError(
                    f"{self.model}: message.content was empty (finish_reason="
                    f"{finish_reason!r}"
                    + (f", reasoning field length={reasoning_len}" if reasoning_len else "")
                    + (f", other message keys={extra_keys}" if extra_keys else "")
                    + "). If finish_reason is 'length', the model likely spent "
                    "max_tokens on reasoning before producing an answer — try a "
                    "non-reasoning model or a much larger max_tokens."
                )
            if not text:
                # A continuation round that adds nothing will never finish.
                raise ProviderError(
                    f"{self.model}: continuation after a truncated response "
                    "returned no content"
                )
            accumulated += text
            if choice.get("finish_reason") != "length":
                return LLMResponse(text=accumulated, model=model_seen)
        raise ProviderError(
            f"{self.model}: response still truncated (finish_reason='length') "
            f"after {MAX_CONTINUATIONS} continuation request(s) — the endpoint "
            "is capping output tokens well below the requested max_tokens"
        )


@dataclass
class AnthropicProvider(LLMProvider):
    """Anthropic Messages API provider (optional)."""

    model: str
    base_url: str = "https://api.anthropic.com"
    api_key_env: str = "ANTHROPIC_API_KEY"
    name = "anthropic"

    def complete(
        self,
        *,
        stage,
        system,
        prompt,
        temperature=0.0,
        max_tokens=DEFAULT_MAX_TOKENS,
    ):
        key = _api_key(self.api_key_env, self.name)
        accumulated = ""
        model_seen = self.model
        for _ in range(1 + MAX_CONTINUATIONS):
            messages = [{"role": "user", "content": prompt}]
            if accumulated:
                # Assistant prefill makes the model resume the cut-off
                # answer. The API rejects prefill with trailing whitespace,
                # so strip it (insignificant between JSON tokens).
                accumulated = accumulated.rstrip()
                messages.append({"role": "assistant", "content": accumulated})
            data = _post_json(
                f"{self.base_url.rstrip('/')}/v1/messages",
                {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system,
                    "messages": messages,
                },
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            if not text and not accumulated:
                raise ProviderError("anthropic response had no text content blocks")
            if not text:
                raise ProviderError(
                    f"{self.model}: continuation after a truncated response "
                    "returned no content"
                )
            accumulated += text
            model_seen = str(data.get("model", self.model))
            if data.get("stop_reason") != "max_tokens":
                return LLMResponse(text=accumulated, model=model_seen)
        raise ProviderError(
            f"{self.model}: response still truncated (stop_reason="
            f"'max_tokens') after {MAX_CONTINUATIONS} continuation request(s)"
        )


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

    def complete(
        self,
        *,
        stage,
        system,
        prompt,
        temperature=0.0,
        max_tokens=DEFAULT_MAX_TOKENS,
    ):
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
