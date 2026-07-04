"""Local provider/model configuration — lives outside the target repo.

``lineage-wiki configure`` writes ``~/.lineage-wiki/config.yml`` (override
the directory with ``LINEAGE_WIKI_HOME``, used by tests). The file stores
provider, model id, base URL, temperature, and the *name* of the
environment variable holding the API key. Secret values are never written,
read back, or printed.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from .providers import (
    FIXTURES_ENV,
    LLMProvider,
    MockProvider,
    ProviderError,
    build_provider,
    validate_api_key_env,
)

HOME_ENV = "LINEAGE_WIKI_HOME"

_DEFAULT_KEY_ENVS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


@dataclass
class LocalModelConfig:
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    temperature: float = 0.0

    def resolved_api_key_env(self) -> str:
        return self.api_key_env or _DEFAULT_KEY_ENVS.get(self.provider, "")


def config_dir() -> Path:
    override = os.environ.get(HOME_ENV, "")
    return Path(override) if override else Path.home() / ".lineage-wiki"


def config_path() -> Path:
    return config_dir() / "config.yml"


def load_local_config() -> LocalModelConfig | None:
    path = config_path()
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    known = {f: data.get(f) for f in LocalModelConfig.__dataclass_fields__ if f in data}
    return LocalModelConfig(**known)


def save_local_config(config: LocalModelConfig) -> Path:
    if config.api_key_env:
        config.api_key_env = validate_api_key_env(config.api_key_env)
    if config.provider not in ("openai", "anthropic"):
        raise ProviderError(
            f"unknown provider {config.provider!r} (supported: openai, anthropic)"
        )
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Written by `lineage-wiki configure`. No secrets live here: the API\n"
        "# key is read from the named environment variable at run time.\n"
        + yaml.safe_dump(asdict(config), sort_keys=False),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def resolve_llm_provider(chain_provider: str, chain_model: str) -> LLMProvider:
    """Resolution order: fixtures env (mock) → local user config → the chain
    config's ``model:`` block. Raises ProviderError with a clear next step
    when nothing is configured."""
    fixtures = os.environ.get(FIXTURES_ENV, "")
    if fixtures:
        return MockProvider.from_file(fixtures)
    local = load_local_config()
    if local is not None and local.provider:
        return build_provider(
            provider=local.provider,
            model=local.model or chain_model,
            base_url=local.base_url,
            api_key_env=local.api_key_env,
        )
    if chain_provider:
        return build_provider(provider=chain_provider, model=chain_model)
    raise ProviderError(
        "no LLM provider configured — run `lineage-wiki configure`, set the "
        f"chain's model block, or point {FIXTURES_ENV} at a fixtures file"
    )


def describe_local_config(config: LocalModelConfig) -> list[str]:
    """Status lines for `configure --show`. Never includes secret values —
    only whether the named environment variable is currently set."""
    key_env = config.resolved_api_key_env()
    key_state = "set" if os.environ.get(key_env) else "NOT set"
    return [
        f"provider     {config.provider}",
        f"model        {config.model or '(from chain config)'}",
        f"base_url     {config.base_url or '(provider default)'}",
        f"temperature  {config.temperature}",
        f"api key      read from ${key_env} at run time (currently {key_state}; "
        "value never stored or printed)",
    ]
