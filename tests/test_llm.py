"""LLM layer: providers, configure, grounding rules, and the mocked pipeline.

Every test runs against canned responses — no live model, ever. The
deterministic default path must keep working with no provider configured.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from lineage_wiki.agent.grounding import Claim, Conflict, GroundingContext
from lineage_wiki.agent.runner import run_generate, run_update
from lineage_wiki.cli import app
from lineage_wiki.config import BigQuerySource, load_config
from lineage_wiki.connectors.bigquery_connector import (
    BigQueryLoadResult,
    ColumnSchema,
    TableSchema,
)
from lineage_wiki.credentials import (
    config_path,
    load_local_config,
    resolve_llm_provider,
    save_local_config,
    LocalModelConfig,
)
from lineage_wiki.ingestion.evidence import EvidenceItem
from lineage_wiki.ingestion.source_loader import EvidenceBundle
from lineage_wiki.providers import (
    AnthropicProvider,
    LLMProvider,
    MockProvider,
    OpenAIProvider,
    ProviderError,
    build_provider,
    validate_api_key_env,
)

from .conftest import FIXED_NOW, REPO_ROOT

runner = CliRunner()

EXAMPLE_CONFIG = REPO_ROOT / "chains" / "example.yml"
LLM_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "llm_responses.yml"
BQ_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "bigquery_schemas.yml"

SNAPSHOT_TABLE = "example-project.analytics.example_daily_snapshot"
RAW_DOC_ID = "raw-doc:raw_files/example/methodology.md"
SCHEMA_ID = f"bq-schema:{SNAPSHOT_TABLE}"


# --- providers ---------------------------------------------------------------


def test_mock_provider_string_list_and_dict_entries(tmp_path):
    provider = MockProvider(
        responses={
            "extractor": '{"claims": []}',
            "writer": {"page-a": "A", "page-b": "B"},
            "reviewer": ["first", "second"],
        }
    )
    assert provider.complete(stage="extractor", system="", prompt="x").text == '{"claims": []}'
    assert provider.complete(stage="writer", system="", prompt="... page-b ...").text == "B"
    assert provider.complete(stage="reviewer", system="", prompt="").text == "first"
    assert provider.complete(stage="reviewer", system="", prompt="").text == "second"
    assert provider.complete(stage="reviewer", system="", prompt="").text == "second"
    with pytest.raises(ProviderError, match="no response for stage"):
        provider.complete(stage="page_planner", system="", prompt="")


def test_openai_provider_requires_env_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = build_provider(provider="openai", model="gpt-x")
    with pytest.raises(ProviderError, match=r"\$OPENAI_API_KEY"):
        provider.complete(stage="extractor", system="s", prompt="p")


def test_anthropic_provider_requires_env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = build_provider(provider="anthropic", model="claude-x")
    with pytest.raises(ProviderError, match=r"\$ANTHROPIC_API_KEY"):
        provider.complete(stage="extractor", system="s", prompt="p")


def _openai_response(text, finish_reason):
    return {
        "model": "m",
        "choices": [
            {"message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}
        ],
    }


def test_openai_provider_continues_truncated_responses(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    provider = build_provider(provider="openai", model="gpt-x")
    responses = iter(
        [
            _openai_response('{"claims": [{"id": "c1", "te', "length"),
            _openai_response('xt": "done"}]}', "stop"),
        ]
    )
    requests = []

    def fake_post(url, payload, headers):
        requests.append(payload)
        return next(responses)

    monkeypatch.setattr("lineage_wiki.providers._post_json", fake_post)
    response = provider.complete(stage="extractor", system="s", prompt="p")
    assert response.text == '{"claims": [{"id": "c1", "text": "done"}]}'
    assert json.loads(response.text) == {"claims": [{"id": "c1", "text": "done"}]}
    assert {request["max_tokens"] for request in requests} == {4096}
    # The second request must carry the partial answer as an assistant prefill.
    assert requests[1]["messages"][-1] == {
        "role": "assistant",
        "content": '{"claims": [{"id": "c1", "te',
    }


def test_openai_prefill_strips_trailing_whitespace(monkeypatch):
    """Anthropic backends behind OpenAI-compatible endpoints (OpenRouter)
    reject a prefill ending in whitespace with HTTP 400 — the partial must
    be rstripped before being sent back."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    provider = build_provider(provider="openai", model="gpt-x")
    responses = iter(
        [
            _openai_response('{"claims": [1, ', "length"),
            _openai_response("2]}", "stop"),
        ]
    )
    requests = []

    def fake_post(url, payload, headers):
        requests.append(payload)
        return next(responses)

    monkeypatch.setattr("lineage_wiki.providers._post_json", fake_post)
    response = provider.complete(stage="extractor", system="s", prompt="p")
    assert requests[1]["messages"][-1] == {
        "role": "assistant",
        "content": '{"claims": [1,',
    }
    assert json.loads(response.text) == {"claims": [1, 2]}


def test_openai_provider_gives_up_when_truncation_never_ends(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    provider = build_provider(provider="openai", model="gpt-x")
    monkeypatch.setattr(
        "lineage_wiki.providers._post_json",
        lambda url, payload, headers: _openai_response("chunk", "length"),
    )
    with pytest.raises(ProviderError, match="still truncated"):
        provider.complete(stage="extractor", system="s", prompt="p")


def test_anthropic_provider_continues_truncated_responses(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = build_provider(provider="anthropic", model="claude-x")
    responses = iter(
        [
            {
                "model": "m",
                "content": [{"type": "text", "text": '{"claims": '}],
                "stop_reason": "max_tokens",
            },
            {
                "model": "m",
                "content": [{"type": "text", "text": "[]}"}],
                "stop_reason": "end_turn",
            },
        ]
    )
    requests = []

    def fake_post(url, payload, headers):
        requests.append(payload)
        return next(responses)

    monkeypatch.setattr("lineage_wiki.providers._post_json", fake_post)
    response = provider.complete(stage="extractor", system="s", prompt="p")
    assert json.loads(response.text) == {"claims": []}
    assert {request["max_tokens"] for request in requests} == {4096}
    # Anthropic prefill must not end with whitespace, so the partial is
    # rstripped before being sent back.
    assert requests[1]["messages"][-1] == {
        "role": "assistant",
        "content": '{"claims":',
    }


def test_provider_max_token_defaults_and_explicit_override(monkeypatch):
    for provider_class in (
        LLMProvider,
        OpenAIProvider,
        AnthropicProvider,
        MockProvider,
    ):
        default = inspect.signature(provider_class.complete).parameters[
            "max_tokens"
        ].default
        assert default == 4096, provider_class.__name__

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    requests = []

    def fake_post(url, payload, headers):
        requests.append(payload)
        return _openai_response("{}", "stop")

    monkeypatch.setattr("lineage_wiki.providers._post_json", fake_post)
    provider = build_provider(provider="openai", model="gpt-x")
    provider.complete(
        stage="extractor",
        system="s",
        prompt="p",
        max_tokens=8192,
    )
    assert requests[0]["max_tokens"] == 8192

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    requests.clear()

    def fake_anthropic_post(url, payload, headers):
        requests.append(payload)
        return {
            "model": "m",
            "content": [{"type": "text", "text": "{}"}],
            "stop_reason": "end_turn",
        }

    monkeypatch.setattr(
        "lineage_wiki.providers._post_json", fake_anthropic_post
    )
    provider = build_provider(provider="anthropic", model="claude-x")
    provider.complete(
        stage="extractor",
        system="s",
        prompt="p",
        max_tokens=8192,
    )
    assert requests[0]["max_tokens"] == 8192


def test_post_json_surfaces_provider_error_message(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    from lineage_wiki.providers import _post_json

    def raise_400(request, timeout):
        raise urllib.error.HTTPError(
            "https://x/api", 400, "Bad Request", hdrs=None,
            fp=io.BytesIO(b'{"error": {"message": "model xyz not found"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_400)
    with pytest.raises(ProviderError, match="HTTP 400 Bad Request — model xyz not found"):
        _post_json("https://x/api", {}, {})

    # A body that is not JSON stays out of the error surface entirely.
    def raise_400_html(request, timeout):
        raise urllib.error.HTTPError(
            "https://x/api", 400, "Bad Request", hdrs=None,
            fp=io.BytesIO(b"<html>huge opaque page</html>"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", raise_400_html)
    with pytest.raises(ProviderError, match=r"HTTP 400 Bad Request$"):
        _post_json("https://x/api", {}, {})


def test_post_json_retries_once_on_timeout(monkeypatch):
    import urllib.request

    from lineage_wiki.providers import _post_json

    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise TimeoutError("stalled")
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert _post_json("https://x/api", {}, {}) == {"ok": True}
    assert len(calls) == 2

    # A second consecutive timeout fails cleanly.
    calls.clear()

    def always_timeout(request, timeout):
        calls.append(request)
        raise TimeoutError("stalled")

    monkeypatch.setattr(urllib.request, "urlopen", always_timeout)
    with pytest.raises(ProviderError, match="retried once"):
        _post_json("https://x/api", {}, {})
    assert len(calls) == 2


def test_build_provider_rejects_unknown():
    with pytest.raises(ProviderError, match="unknown LLM provider"):
        build_provider(provider="bard", model="x")


def test_api_key_env_must_be_a_name_not_a_value():
    assert validate_api_key_env("OPENAI_API_KEY") == "OPENAI_API_KEY"
    for secret_looking in ("sk-abc123", "Bearer xyz", "my key"):
        with pytest.raises(ProviderError, match="never stores API keys"):
            validate_api_key_env(secret_looking)


def test_resolution_order(monkeypatch):
    # 1. fixtures env wins
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", str(LLM_FIXTURES))
    assert resolve_llm_provider("openai", "gpt-x").name == "mock"
    # 2. local config
    monkeypatch.delenv("LINEAGE_WIKI_LLM_FIXTURES")
    save_local_config(LocalModelConfig(provider="anthropic", model="claude-x"))
    assert resolve_llm_provider("openai", "gpt-x").name == "anthropic"
    # 3. chain config fallback
    config_path().unlink()
    assert resolve_llm_provider("openai", "gpt-x").name == "openai"
    # 4. nothing configured -> clear error
    with pytest.raises(ProviderError, match="lineage-wiki configure"):
        resolve_llm_provider("", "")


# --- configure ---------------------------------------------------------------


def test_configure_writes_local_config_without_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    result = runner.invoke(
        app,
        [
            "configure",
            "--provider", "openai",
            "--model", "gpt-x",
            "--api-key-env", "OPENAI_API_KEY",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no secrets stored" in result.output
    assert "sk-super-secret-value" not in result.output

    path = config_path()
    text = path.read_text(encoding="utf-8")
    assert "sk-super-secret-value" not in text
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_local_config()
    assert loaded.provider == "openai" and loaded.model == "gpt-x"

    shown = runner.invoke(app, ["configure", "--show"])
    assert shown.exit_code == 0
    assert "currently set" in shown.output
    assert "sk-super-secret-value" not in shown.output


def test_configure_rejects_key_material():
    result = runner.invoke(
        app,
        ["configure", "--provider", "openai", "--model", "m", "--api-key-env", "sk-oops"],
    )
    assert result.exit_code == 1
    assert "never stores API keys" in result.output


def test_configure_show_before_any_config():
    result = runner.invoke(app, ["configure", "--show"])
    assert result.exit_code == 0
    assert "no local model config yet" in result.output


# --- grounding rules -----------------------------------------------------------


def _bundle() -> EvidenceBundle:
    doc = EvidenceItem(
        id=RAW_DOC_ID,
        source_type="raw_doc",
        source_uri="raw_files/example/methodology.md",
        title="Methodology",
        content=(
            "# Methodology\n\n"
            "Total value = quantity * price\n"
            "The daily snapshot pipeline is covered by this method.\n"
        ),
    )
    bundle = EvidenceBundle(raw_docs=[doc])
    schema = TableSchema(
        table_id=SNAPSHOT_TABLE,
        columns=[ColumnSchema(name="snapshot_date", type="DATE"),
                 ColumnSchema(name="total_value", type="NUMERIC")],
    )
    bundle.bigquery = BigQueryLoadResult(
        source=BigQuerySource(tables=[SNAPSHOT_TABLE]),
        available=True,
        client_kind="fixtures",
        schemas={SNAPSHOT_TABLE: schema},
        items=[schema.to_evidence()],
    )
    return bundle


def test_claims_must_cite_known_evidence():
    ctx = GroundingContext(_bundle())
    ok = ctx.check_claim(
        Claim(
            id="c",
            kind="fact",
            text="The daily snapshot pipeline is covered by this method.",
            evidence_ids=[RAW_DOC_ID],
            quote="daily snapshot pipeline is covered",
        )
    )
    assert ok.accepted
    bad = ctx.check_claim(Claim(id="c", kind="fact", text="x", evidence_ids=["nope"]))
    assert not bad.accepted and "unknown evidence ids" in bad.reason
    none = ctx.check_claim(Claim(id="c", kind="fact", text="x"))
    assert not none.accepted


def test_natural_language_claims_require_verbatim_quote_in_cited_source():
    ctx = GroundingContext(_bundle())
    missing_quote = ctx.check_claim(
        Claim(id="f1", kind="fact", text="Covered by the method.", evidence_ids=[RAW_DOC_ID])
    )
    assert not missing_quote.accepted and "no supporting quote" in missing_quote.reason

    fabricated = ctx.check_claim(
        Claim(
            id="f2",
            kind="fact",
            text="Unsupported fact with a valid-looking citation.",
            evidence_ids=[RAW_DOC_ID],
            quote="this quote does not appear",
        )
    )
    assert not fabricated.accepted and "quote not found" in fabricated.reason


def test_formula_requires_verbatim_quote_in_cited_source():
    ctx = GroundingContext(_bundle())
    ok = ctx.check_claim(
        Claim(
            id="f1", kind="formula", text="total_value = quantity * price",
            evidence_ids=[RAW_DOC_ID],
            quote="total   VALUE =\nquantity * price",  # whitespace/case-insensitive
        )
    )
    assert ok.accepted
    missing_quote = ctx.check_claim(
        Claim(id="f2", kind="formula", text="x = y", evidence_ids=[RAW_DOC_ID])
    )
    assert not missing_quote.accepted and "no supporting quote" in missing_quote.reason
    fabricated = ctx.check_claim(
        Claim(id="f3", kind="formula", text="x = y", evidence_ids=[RAW_DOC_ID],
              quote="not in the doc at all")
    )
    assert not fabricated.accepted and "quote not found" in fabricated.reason
    wrong_source = ctx.check_claim(
        Claim(id="f4", kind="formula", text="x = y", evidence_ids=[SCHEMA_ID],
              quote="total value")
    )
    assert not wrong_source.accepted and "methodology/code/note" in wrong_source.reason


def test_column_claims_require_bq_schema_evidence():
    ctx = GroundingContext(_bundle())
    ok = ctx.check_claim(
        Claim(id="c1", kind="column", text="Column total_value holds the product.",
              evidence_ids=[SCHEMA_ID])
    )
    assert ok.accepted
    no_schema = ctx.check_claim(
        Claim(id="c2", kind="column", text="Column total_value ...",
              evidence_ids=[RAW_DOC_ID])
    )
    assert not no_schema.accepted and "no ingested BigQuery schema" in no_schema.reason
    bogus_column = ctx.check_claim(
        Claim(id="c3", kind="column", text="Column made_up_col exists.",
              evidence_ids=[SCHEMA_ID])
    )
    assert not bogus_column.accepted and "names no column" in bogus_column.reason


def test_code_path_claims_require_repo_evidence():
    ctx = GroundingContext(_bundle())
    bad = ctx.check_claim(
        Claim(id="p1", kind="code_path", text="Computed in main.py",
              evidence_ids=[RAW_DOC_ID])
    )
    assert not bad.accepted and "repository file evidence" in bad.reason


def test_conflicts_require_known_evidence():
    ctx = GroundingContext(_bundle())
    ok = ctx.check_conflict(
        Conflict(
            topic="Formula wording",
            detail="doc vs schema",
            evidence_ids=[RAW_DOC_ID, SCHEMA_ID],
            quotes=["Total value = quantity * price", "total_value"],
        )
    )
    assert ok.accepted
    bad = ctx.check_conflict(Conflict(topic="x", detail="y", evidence_ids=["nope"]))
    assert not bad.accepted
    unquoted = ctx.check_conflict(
        Conflict(topic="x", detail="y", evidence_ids=[RAW_DOC_ID, SCHEMA_ID])
    )
    assert not unquoted.accepted and "supporting quotes" in unquoted.reason
    fabricated = ctx.check_conflict(
        Conflict(
            topic="x",
            detail="y",
            evidence_ids=[RAW_DOC_ID, SCHEMA_ID],
            quotes=["not in any cited evidence"],
        )
    )
    assert not fabricated.accepted and "quote not found" in fabricated.reason


def test_section_bodies_must_cite_and_must_not_contain_sql():
    ctx = GroundingContext(_bundle())
    accepted_claims = [
        Claim(
            id="f1",
            kind="formula",
            text="total_value = quantity * price",
            evidence_ids=[RAW_DOC_ID],
            quote="Total value = quantity * price",
        ),
        Claim(
            id="c1",
            kind="column",
            text="Column total_value holds the product.",
            evidence_ids=[SCHEMA_ID],
        ),
    ]
    ok = ctx.check_section_body(
        f"`total_value = quantity * price` [src: {RAW_DOC_ID}]", accepted_claims
    )
    assert ok.accepted
    uncited = ctx.check_section_body("Plain assertion.", accepted_claims)
    assert not uncited.accepted and "citation markers" in uncited.reason
    foreign = ctx.check_section_body("Text. [src: other-id]", accepted_claims)
    assert not foreign.accepted and "outside the accepted claims" in foreign.reason
    unsupported = ctx.check_section_body(
        f"Valid-looking but unrelated assertion. [src: {RAW_DOC_ID}]",
        accepted_claims,
    )
    assert not unsupported.accepted and "not traceable" in unsupported.reason
    sql_fence = ctx.check_section_body(
        f"```sql\nSELECT 1\n```\n[src: {RAW_DOC_ID}]", accepted_claims
    )
    assert not sql_fence.accepted and "SQL is not allowed" in sql_fence.reason
    sql_inline = ctx.check_section_body(
        f"Run select x from t daily. [src: {RAW_DOC_ID}]", accepted_claims
    )
    assert not sql_inline.accepted


# --- end-to-end pipeline with mocked responses -------------------------------------


def _setup_target(tmp_path, monkeypatch) -> Path:
    doc = tmp_path / "raw_files" / "example" / "methodology.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        (
            "# Example Methodology\n\n"
            "This methodology covers the daily example metric snapshot pipeline.\n\n"
            "Total value = quantity * price\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(BQ_FIXTURES))
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", str(LLM_FIXTURES))
    return tmp_path


def test_generate_use_llm_end_to_end(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root), "--use-llm"],
    )
    assert result.exit_code == 0, result.output
    assert "llm       claims accepted: 3, rejected: 3" in result.output

    framework = (root / "okf" / "frameworks" / "example-chain.md").read_text(
        encoding="utf-8"
    )
    # Accepted, cited sections were written. The Scope body cited the claim
    # id (c6); the published page must carry the resolved evidence id.
    assert (
        "daily example metric snapshot pipeline. [src: raw-doc:" in framework
    )
    assert "[src: c6]" not in framework
    # The writer echoed '## Core Formula'; the heading was normalized.
    assert "`total_value = quantity * price` [src: " in framework
    # The rejected formula became a Known Gap instead of being published.
    assert "- (llm) A proposed formula (profit = revenue - cost) was rejected" in framework
    assert "profit = revenue - cost\n" not in framework.split("## Known Gaps")[0]
    # The accepted conflict became a Known Doc-vs-Code Divergence.
    assert "**Formula wording**" in framework
    # The model's attempt to write Known Gaps directly was blocked.
    assert "The model must not write this section." not in framework

    # SQL-bearing section was rejected: the output page keeps its scaffold.
    output_page = (root / "okf" / "outputs" / "example-daily-snapshot.md").read_text(
        encoding="utf-8"
    )
    assert "SELECT * FROM example_daily_snapshot" not in output_page
    assert "```sql" not in output_page

    # Detailed transcript lands in run metadata, not in pages.
    run_files = sorted((root / ".lineage-wiki" / "runs").glob("*generate-llm.json"))
    assert run_files, "generate-llm transcript missing"
    transcript = json.loads(run_files[-1].read_text(encoding="utf-8"))
    assert len(transcript["claims_accepted"]) == 3
    assert any("SQL is not allowed" in r for r in transcript["rejected"])
    assert any("not in the allowed list" in r for r in transcript["rejected"])
    # The planner's raw output and its filtered-out entries are diagnosable.
    assert len(transcript["planner_pages"]) == 3
    assert any(
        "planner: page 'okf/invented/not-in-plan.md' is not in the "
        "deterministic plan" in r
        for r in transcript["rejected"]
    )
    assert any(
        "section 'Known Gaps' does not match an enrichable heading" in r
        for r in transcript["rejected"]
    )


def test_claim_id_citations_resolve_to_evidence_ids():
    """[src: <claim-id>] markers are rewritten to the claim's verified
    evidence ids; unknown ids are left for the grounding check to reject."""
    from lineage_wiki.agent.llm_pipeline import _resolve_claim_citations

    claims = [
        Claim(
            id="c1",
            kind="fact",
            text="A fact.",
            evidence_ids=[RAW_DOC_ID, SCHEMA_ID],
        )
    ]
    known = {RAW_DOC_ID, SCHEMA_ID}
    resolved = _resolve_claim_citations("A fact. [src: c1]", claims, known)
    assert resolved == f"A fact. [src: {RAW_DOC_ID}] [src: {SCHEMA_ID}]"
    # Real evidence ids and unknown ids pass through untouched.
    untouched = f"A fact. [src: {RAW_DOC_ID}] [src: c99]"
    assert _resolve_claim_citations(untouched, claims, known) == untouched


def test_warning_when_accepted_claims_are_never_published(tmp_path, monkeypatch):
    """Accepted claims that never reach a page must be called out in the
    run summary, not silently discarded (the planner-returns-nothing
    failure mode)."""
    root = _setup_target(tmp_path, monkeypatch)
    fixtures = yaml.safe_load(LLM_FIXTURES.read_text(encoding="utf-8"))
    fixtures["responses"]["page_planner"] = json.dumps(
        {
            "pages": [
                {
                    "rel_path": "okf/frameworks/example-chain.md",
                    "sections": ["Nonexistent Heading"],
                }
            ]
        }
    )
    override = tmp_path / "planner_empty.yml"
    override.write_text(yaml.safe_dump(fixtures), encoding="utf-8")
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", str(override))

    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root), "--use-llm"],
    )
    assert result.exit_code == 0, result.output
    assert "pages enriched: 0" in result.output
    assert (
        "warning: 3 accepted claim(s) were not published — the planner "
        "selected no enrichable sections" in result.output
    )
    # The reason the planner's selection was dropped is shown inline.
    assert "'Nonexistent Heading' does not match an enrichable heading" in result.output


def test_generate_use_llm_resolves_known_gaps(tmp_path, monkeypatch):
    """A page must not simultaneously claim a fact is unverified while
    showing the verified, cited fact — the specific regression this
    reconciliation logic exists to prevent."""
    root = _setup_target(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root), "--use-llm"],
    )
    assert result.exit_code == 0, result.output
    assert "llm       gaps resolved: 2" in result.output

    framework = (root / "okf" / "frameworks" / "example-chain.md").read_text(
        encoding="utf-8"
    )
    gaps = framework.split("## Known Gaps")[1].split("## Known Doc-vs-Code")[0]

    # Resolved: a grounded Core Formula was published, and the BigQuery
    # table's schema was cross-checked via the recorded divergence.
    assert "have not been extracted from" not in gaps
    assert "has not been cross-checked" not in gaps
    # Still open: nothing in this run touched these.
    assert "No component pages exist yet" in gaps
    assert "no local clone available" in gaps
    assert "no verified source mapping" in gaps
    # The rejected formula still lands as its own gap.
    assert "A proposed formula (profit = revenue - cost) was rejected" in gaps


def test_llm_sections_survive_deterministic_rerun(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    first = run_generate(cfg, root, FIXED_NOW, use_llm=True)
    assert first.llm

    framework_rel = "okf/frameworks/example-chain.md"
    before = (root / framework_rel).read_text(encoding="utf-8")
    gaps_before = before.split("## Known Gaps")[1].split("## Known Doc-vs-Code")[0]
    assert "have not been extracted from" not in gaps_before
    assert "has not been cross-checked" not in gaps_before

    second = run_generate(cfg, root, FIXED_NOW)  # deterministic, no LLM
    after = (root / framework_rel).read_text(encoding="utf-8")
    assert framework_rel in second.unchanged
    assert after == before  # cited sections, gap bullets, divergences all kept

    update = run_update(cfg, root, FIXED_NOW)
    assert update.noop


def test_llm_planner_selects_component_pages_but_cannot_invent(tmp_path):
    """Configured component pages are eligible planner targets; pages
    outside the deterministic plan are dropped with a recorded reason, and
    headings echoed with the Markdown '## ' marker still match."""
    from lineage_wiki.agent.llm_pipeline import EnrichmentResult, _run_planner
    from lineage_wiki.agent.prompts import load_prompts
    from lineage_wiki.okf.templates import plan_chain_pages

    from .test_templates import component_cfg

    cfg = component_cfg(load_config(EXAMPLE_CONFIG))
    plan = plan_chain_pages(cfg, tmp_path, FIXED_NOW)
    component_rel = "okf/components/example-total-value.md"
    assert component_rel in {d.rel_path for d in plan.pages}

    provider = MockProvider(
        responses={
            "page_planner": json.dumps(
                {
                    "pages": [
                        {
                            "rel_path": component_rel,
                            "sections": ["## What It Represents", "Formula / Logic"],
                        },
                        {
                            "rel_path": "okf/components/invented-component.md",
                            "sections": ["What It Represents"],
                        },
                    ]
                }
            )
        }
    )
    result = EnrichmentResult()
    jobs = _run_planner(provider, load_prompts(tmp_path), cfg, plan, result, 0.0)
    assert [j.rel_path for j in jobs] == [component_rel]
    # '## ' prefix normalized away, not silently dropped.
    assert jobs[0].sections == ["What It Represents", "Formula / Logic"]
    assert any(
        "planner: page 'okf/components/invented-component.md' is not in the "
        "deterministic plan" in r
        for r in result.rejected
    )
    # The raw planner output is preserved for the transcript.
    assert len(result.planner_pages) == 2


def test_dry_run_with_llm_writes_nothing(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    result = runner.invoke(
        app,
        [
            "generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root),
            "--use-llm", "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "llm       claims accepted: 3" in result.output
    assert not (root / "okf").exists()
    assert not (root / ".lineage-wiki").exists()


def test_default_generate_never_touches_a_provider(tmp_path, monkeypatch):
    # A broken fixtures path would explode if the deterministic path ever
    # resolved a provider.
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", "/does/not/exist.yml")
    result = runner.invoke(
        app, ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output


def test_use_llm_without_any_provider_fails_clearly(tmp_path):
    cfg_text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    data = yaml.safe_load(cfg_text)
    data["model"] = {"provider": "", "model": ""}
    config = tmp_path / "chain.yml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = runner.invoke(
        app, ["generate", "--config", str(config), "--root", str(tmp_path), "--use-llm"]
    )
    assert result.exit_code == 1
    assert "no LLM provider configured" in result.output


def test_bad_json_from_model_fails_the_stage_clearly(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    broken = tmp_path / "broken.yml"
    broken.write_text("responses:\n  page_planner: 'not json'\n", encoding="utf-8")
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", str(broken))
    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root), "--use-llm"],
    )
    assert result.exit_code == 1
    assert "page_planner stage did not return valid JSON" in result.output


def test_prompt_stub_overrides_are_loaded(tmp_path):
    from lineage_wiki.agent.prompts import load_prompts

    defaults = load_prompts(tmp_path)
    assert "Never invent formulas" in defaults.system

    override = tmp_path / ".lineage-wiki" / "prompts" / "system.md"
    override.parent.mkdir(parents=True)
    override.write_text("# Custom system prompt\n", encoding="utf-8")
    loaded = load_prompts(tmp_path)
    assert loaded.system == "# Custom system prompt\n"
    assert loaded.extractor == defaults.extractor


# --- enrichment deny-list (deterministic section ownership) --------------------


def test_enrichment_denied_sections_by_page_type():
    from lineage_wiki.constants import enrichment_denied_sections

    assert enrichment_denied_sections("okf/outputs/foo.md") == (
        "Column Definitions",
    )
    assert enrichment_denied_sections("okf/change-checks/foo.md") == (
        "How to Trigger a Review",
        "Required Agent Behavior",
    )
    assert enrichment_denied_sections("okf/frameworks/foo.md") == ()
    assert enrichment_denied_sections("foo.md") == ()


def test_planner_never_offers_denylisted_sections(tmp_path):
    """Deny-listed sections are excluded from the plannable heading list, so
    a model that selects them anyway is rejected — they can never reach a
    writer/reviewer call."""
    from lineage_wiki.agent.llm_pipeline import EnrichmentResult, _run_planner
    from lineage_wiki.agent.prompts import load_prompts
    from lineage_wiki.okf.templates import plan_chain_pages

    cfg = load_config(EXAMPLE_CONFIG)
    plan = plan_chain_pages(cfg, tmp_path, FIXED_NOW)
    output_rel = "okf/outputs/example-daily-snapshot.md"
    check_rel = "okf/change-checks/example-chain-review-rules.md"
    assert {output_rel, check_rel} <= {d.rel_path for d in plan.pages}

    provider = MockProvider(
        responses={
            "page_planner": json.dumps(
                {
                    "pages": [
                        {
                            "rel_path": output_rel,
                            "sections": ["Column Definitions", "Column Meanings"],
                        },
                        {
                            "rel_path": check_rel,
                            "sections": [
                                "How to Trigger a Review",
                                "Required Agent Behavior",
                                "Code Change Triggers",
                            ],
                        },
                    ]
                }
            )
        }
    )
    result = EnrichmentResult()
    jobs = _run_planner(provider, load_prompts(tmp_path), cfg, plan, result, 0.0)
    by_rel = {j.rel_path: j.sections for j in jobs}
    assert by_rel[output_rel] == ["Column Meanings"]
    assert by_rel[check_rel] == ["Code Change Triggers"]
    for denied in (
        "Column Definitions",
        "How to Trigger a Review",
        "Required Agent Behavior",
    ):
        assert any(
            f"section {denied!r} does not match an enrichable heading" in r
            for r in result.rejected
        ), denied


def _override_llm_fixtures(tmp_path, monkeypatch, *, planner, writer) -> None:
    fixtures = yaml.safe_load(LLM_FIXTURES.read_text(encoding="utf-8"))
    fixtures["responses"]["page_planner"] = json.dumps(planner)
    fixtures["responses"]["writer"] = writer
    override = tmp_path / "llm_fixtures_override.yml"
    override.write_text(yaml.safe_dump(fixtures), encoding="utf-8")
    monkeypatch.setenv("LINEAGE_WIKI_LLM_FIXTURES", str(override))


def test_denylisted_sections_stay_deterministic_end_to_end(tmp_path, monkeypatch):
    """Even a writer that emits bodies for deny-listed sections cannot
    replace them: the schema table and the procedural change-check sections
    stay deterministic, while `Column Meanings` and the evidence-bearing
    trigger sections remain enrichable under the full citation rules."""
    from lineage_wiki.okf.sections import split_sections

    root = _setup_target(tmp_path, monkeypatch)
    output_rel = "okf/outputs/example-daily-snapshot.md"
    check_rel = "okf/change-checks/example-chain-review-rules.md"
    column_claim = "The daily snapshot exposes a total_value column."
    fact_claim = "This methodology covers the daily example metric snapshot pipeline."
    _override_llm_fixtures(
        tmp_path,
        monkeypatch,
        planner={
            "pages": [
                {
                    "rel_path": output_rel,
                    "sections": ["Column Definitions", "Column Meanings"],
                },
                {
                    "rel_path": check_rel,
                    "sections": [
                        "How to Trigger a Review",
                        "Required Agent Behavior",
                        "Code Change Triggers",
                    ],
                },
            ]
        },
        writer={
            output_rel: json.dumps(
                {
                    "sections": [
                        {
                            "heading": "Column Definitions",
                            "body": f"{column_claim} [src: {SCHEMA_ID}]",
                        },
                        {
                            "heading": "Column Meanings",
                            "body": f"{column_claim} [src: {SCHEMA_ID}]",
                        },
                    ]
                }
            ),
            check_rel: json.dumps(
                {
                    "sections": [
                        {
                            "heading": "How to Trigger a Review",
                            "body": f"{fact_claim} [src: {RAW_DOC_ID}]",
                        },
                        {
                            "heading": "Required Agent Behavior",
                            "body": f"{fact_claim} [src: {RAW_DOC_ID}]",
                        },
                        {
                            "heading": "Code Change Triggers",
                            "body": f"{fact_claim} [src: {RAW_DOC_ID}]",
                        },
                    ]
                }
            ),
        },
    )

    result = runner.invoke(
        app,
        ["generate", "--config", str(EXAMPLE_CONFIG), "--root", str(root), "--use-llm"],
    )
    assert result.exit_code == 0, result.output

    output_page = (root / output_rel).read_text(encoding="utf-8")
    _, sections = split_sections(output_page)
    by_heading = dict(sections)
    # The deterministic full schema table survived the LLM run untouched...
    assert "| Column | Type | Mode | Description |" in by_heading["Column Definitions"]
    assert "[src:" not in by_heading["Column Definitions"]
    # ...while the grounded meaning landed in the enrichable section.
    assert column_claim in by_heading["Column Meanings"]
    assert f"[src: {SCHEMA_ID}]" in by_heading["Column Meanings"]

    check_page = (root / check_rel).read_text(encoding="utf-8")
    _, sections = split_sections(check_page)
    by_heading = dict(sections)
    assert "Read the diff in the changed source" in by_heading["How to Trigger a Review"]
    assert "[src:" not in by_heading["How to Trigger a Review"]
    assert "classify the change as exactly one of" in by_heading["Required Agent Behavior"]
    assert "[src:" not in by_heading["Required Agent Behavior"]
    # Evidence-bearing trigger sections stay enrichable under full grounding.
    assert fact_claim in by_heading["Code Change Triggers"]
    assert f"[src: {RAW_DOC_ID}]" in by_heading["Code Change Triggers"]

    # The denied writer output was dropped at the allowed-list check.
    run_files = sorted((root / ".lineage-wiki" / "runs").glob("*generate-llm.json"))
    transcript = json.loads(run_files[-1].read_text(encoding="utf-8"))
    for denied in (
        "Column Definitions",
        "How to Trigger a Review",
        "Required Agent Behavior",
    ):
        assert any(
            f"section {denied!r} was not in the allowed list" in r
            for r in transcript["rejected"]
        ), denied


# --- LLM grounding status reconciliation (Goal 2) --------------------------------


FRAMEWORK_REL = "okf/frameworks/example-chain.md"


def _status_section(page: str) -> str:
    return page.split("## Verification Status")[1].split("\n## ")[0]


def test_llm_run_reconciles_scaffold_verification_status(tmp_path, monkeypatch):
    """After a successful LLM run, a still-scaffold-marked Verification
    Status reflects the run's grounding results — derived from transcript
    data, date-free, and worded as claim grounding, never as BigQuery
    verification."""
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    result = run_generate(cfg, root, FIXED_NOW, use_llm=True)
    assert any("verification status reconciled" in line for line in result.llm)

    framework = (root / FRAMEWORK_REL).read_text(encoding="utf-8")
    status = _status_section(framework)
    assert (
        "LLM claim grounding has run for this chain: 3 accepted grounded "
        "claim(s), 3 rejected claim(s), 1 recorded divergence(s)." in status
    )
    assert "Core Formula, Scope" in status  # sections written on this page
    assert "1 rejected formula claim(s)" in status
    assert "it is not BigQuery verification" in status
    # Stale scaffold wording is gone where the transcript supports better.
    assert "Unverified scaffold" not in status
    assert "extraction pending" not in status
    assert "No fact on this page has been cross-checked" not in status
    assert (
        "| Raw methodology | Ingested — 1 doc(s) loaded; "
        "LLM claim grounding: 1 published citation(s) |" in status
    )
    # The divergence's published schema citation supersedes the BQ row's
    # pending phrase; rows with no published citation keep theirs.
    assert "cross-check pending" not in status
    assert "| Report mappings | Not ingested (scaffold) |" in status
    # Date-free body: run dates live in frontmatter/run metadata only.
    assert "2026-07" not in status

    # Never forced: the reconciliation must ride the preserved-section
    # merge, not sections_written (which bypasses preservation).
    run_files = sorted((root / ".lineage-wiki" / "runs").glob("*generate-llm.json"))
    transcript = json.loads(run_files[-1].read_text(encoding="utf-8"))
    assert transcript["sections_written"] == {FRAMEWORK_REL: ["Core Formula", "Scope"]}
    assert transcript["status_reconciled"] == [FRAMEWORK_REL]


def test_grounding_status_is_stable_across_reruns_on_later_dates(tmp_path, monkeypatch):
    """Re-running (LLM or deterministic) on a later date must not change the
    page body just because the date changed — the grounding note is
    date-free and refreshes to identical text."""
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    run_generate(cfg, root, FIXED_NOW, use_llm=True)
    before = (root / FRAMEWORK_REL).read_text(encoding="utf-8")

    second = run_generate(cfg, root, "2026-07-05T00:00:00Z", use_llm=True)
    assert FRAMEWORK_REL in second.unchanged
    third = run_generate(cfg, root, "2026-07-06T00:00:00Z")  # deterministic
    assert FRAMEWORK_REL in third.unchanged
    after = (root / FRAMEWORK_REL).read_text(encoding="utf-8")
    assert after == before
    assert "LLM claim grounding has run" in _status_section(after)


def _overwrite_status(root, body: str) -> None:
    from lineage_wiki.okf.sections import replace_section

    page = (root / FRAMEWORK_REL).read_text(encoding="utf-8")
    edited = replace_section(page, "Verification Status", body)
    assert edited != page
    (root / FRAMEWORK_REL).write_text(edited, encoding="utf-8")


def test_human_edited_verification_status_survives_llm_rerun(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    run_generate(cfg, root, FIXED_NOW, use_llm=True)
    human = "Reviewed by the data platform team; formulas signed off."
    _overwrite_status(root, human)

    run_generate(cfg, root, FIXED_NOW, use_llm=True)
    status = _status_section((root / FRAMEWORK_REL).read_text(encoding="utf-8"))
    assert human in status
    assert "LLM claim grounding" not in status


def test_human_note_appended_to_grounding_status_survives_reruns(
    tmp_path, monkeypatch
):
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    run_generate(cfg, root, FIXED_NOW, use_llm=True)
    page = (root / FRAMEWORK_REL).read_text(encoding="utf-8")
    grounding = _status_section(page).strip()
    human = "Owner note: reviewed claims remain under manual review."
    _overwrite_status(root, f"{grounding}\n\n{human}")

    run_generate(cfg, root, FIXED_NOW, use_llm=True)
    run_generate(cfg, root, FIXED_NOW)

    status = _status_section((root / FRAMEWORK_REL).read_text(encoding="utf-8"))
    assert "LLM claim grounding has run" in status
    assert human in status


def test_verify_bq_owned_verification_status_survives_llm_rerun(tmp_path, monkeypatch):
    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    run_generate(cfg, root, FIXED_NOW, use_llm=True)
    verified = (
        "Verified from BigQuery schema metadata (schema only).\n\n"
        "- Table exists in BigQuery (TABLE, 4 columns)."
    )
    _overwrite_status(root, verified)

    run_generate(cfg, root, FIXED_NOW, use_llm=True)
    status = _status_section((root / FRAMEWORK_REL).read_text(encoding="utf-8"))
    assert "Verified from BigQuery schema metadata" in status
    assert "LLM claim grounding" not in status


def test_grounding_status_merge_semantics():
    from lineage_wiki.okf.sections import merge_manual_sections

    def page(status_body: str, scope: str = "Scaffold scope.") -> str:
        return (
            "---\ntype: Framework\n---\n# Page\n\n"
            f"## Scope\n\n{scope}\n\n"
            f"## Verification Status\n\n{status_body}\n"
        )

    scaffold = "Unverified scaffold framework page generated deterministically."
    grounding_v1 = "LLM claim grounding has run for this chain: 1 accepted grounded claim(s)."
    grounding_v2 = "LLM claim grounding has run for this chain: 5 accepted grounded claim(s)."
    verify_bq = "Verified from BigQuery schema metadata (schema only)."
    human = "Signed off by the owner."

    # Scaffold refreshes to a grounding note (a fresh LLM run landed).
    merged = merge_manual_sections(
        page(scaffold), page(grounding_v1), allow_status_refresh=True
    )
    assert grounding_v1 in merged and scaffold not in merged
    # A grounding note is not discarded by a deterministic (scaffold) rerun…
    merged = merge_manual_sections(page(grounding_v1), page(scaffold))
    assert grounding_v1 in merged and scaffold not in merged
    # …but a newer grounding note replaces it.
    merged = merge_manual_sections(
        page(grounding_v1), page(grounding_v2), allow_status_refresh=True
    )
    assert grounding_v2 in merged and grounding_v1 not in merged
    # verify-bq results and human notes are never refreshed.
    merged = merge_manual_sections(page(verify_bq), page(grounding_v1))
    assert verify_bq in merged and grounding_v1 not in merged
    merged = merge_manual_sections(page(human), page(grounding_v1))
    assert human in merged and grounding_v1 not in merged
    # Marker presence is not ownership proof: without a matching manifest
    # snapshot, mixed tool/human status is preserved exactly.
    mixed = grounding_v1 + "\n\nOwner note: do not replace this review."
    merged = merge_manual_sections(page(mixed), page(grounding_v2))
    assert mixed in merged and grounding_v2 not in merged


def test_grounding_status_reverts_when_grounded_sections_are_invalidated():
    """A grounding status must not outlive the grounded sections it
    describes: when this run invalidates the page's cited content, the
    status reverts to the scaffold draft instead of surviving preserved."""
    from lineage_wiki.okf.sections import merge_manual_sections

    scaffold = "Unverified scaffold framework page generated deterministically."
    grounding = "LLM claim grounding has run for this chain: 1 accepted grounded claim(s)."
    existing = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Scope\n\nLLM text about scope. [src: raw-doc:docs/a.md]\n\n"
        f"## Verification Status\n\n{grounding}\n"
    )
    draft = (
        "---\ntype: Framework\n---\n# Page\n\n"
        "## Scope\n\nScaffold scope.\n\n"
        f"## Verification Status\n\n{scaffold}\n"
    )
    # No stale evidence: both the cited section and the status survive.
    merged = merge_manual_sections(existing, draft)
    assert "LLM text about scope." in merged and grounding in merged
    # The cited doc changed: the section is invalidated, the status reverts.
    merged = merge_manual_sections(
        existing,
        draft,
        stale_evidence=frozenset({"raw-doc:docs/a.md"}),
        allow_status_refresh=True,
    )
    assert "LLM text about scope." not in merged
    assert grounding not in merged
    assert scaffold in merged


# --- migration force path for template-owned sections ---------------------------


def _plant_llm_body_in_column_definitions(root, cfg) -> tuple[str, str]:
    """Generate deterministically, then overwrite the output page's
    `Column Definitions` with citation-bearing prose, simulating a page an
    older LLM run enriched before the section was deny-listed."""
    from lineage_wiki.okf.sections import replace_section

    run_generate(cfg, root, FIXED_NOW)
    rel = "okf/outputs/example-daily-snapshot.md"
    page = (root / rel).read_text(encoding="utf-8")
    llm_body = f"The daily snapshot exposes a total_value column. [src: {SCHEMA_ID}]"
    edited = replace_section(page, "Column Definitions", llm_body)
    assert edited != page
    (root / rel).write_text(edited, encoding="utf-8")
    return rel, llm_body


def test_migration_restores_deterministic_schema_table(tmp_path, monkeypatch):
    """When a deny-listed section carries `[src:]` content and the file still
    matches its last tool-written manifest snapshot (machine-written, never
    edited), the deterministic body is force-restored."""
    from lineage_wiki.okf.sections import split_sections
    from lineage_wiki.storage.manifest import (
        compute_snapshot,
        load_manifest,
        save_manifest,
    )

    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    rel, llm_body = _plant_llm_body_in_column_definitions(root, cfg)
    # Record the edited file as the tool's own last-written state.
    manifest = load_manifest(root)
    entry = manifest.chains[cfg.chain.id]
    edited = (root / rel).read_text(encoding="utf-8")
    entry.file_snapshots[rel] = compute_snapshot({rel: edited})
    save_manifest(root, manifest)

    result = run_generate(cfg, root, FIXED_NOW)
    assert rel in result.updated
    assert result.warnings == []
    after = (root / rel).read_text(encoding="utf-8")
    _, sections = split_sections(after)
    body = dict(sections)["Column Definitions"]
    assert "| Column | Type | Mode | Description |" in body
    assert llm_body not in body
    assert "[src:" not in body


def test_migration_warns_instead_of_clobbering_edited_pages(tmp_path, monkeypatch):
    """`[src:]` alone is not proof a section is machine-written: when the
    file drifted from its manifest snapshot (someone edited it since the
    tool last wrote it), the cited body is preserved and a warning names
    the page and section for manual migration."""
    from lineage_wiki.okf.sections import split_sections

    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    rel, llm_body = _plant_llm_body_in_column_definitions(root, cfg)
    # Manifest still records the pre-edit snapshot: the page has drifted.

    result = run_generate(cfg, root, FIXED_NOW)
    after = (root / rel).read_text(encoding="utf-8")
    _, sections = split_sections(after)
    assert dict(sections)["Column Definitions"].strip().endswith(llm_body)
    assert any(
        rel in w and "'Column Definitions'" in w for w in result.warnings
    ), result.warnings


def _plant_uncited_column_definitions(root, cfg) -> tuple[str, str]:
    from lineage_wiki.okf.sections import replace_section

    run_generate(cfg, root, FIXED_NOW)
    rel = "okf/outputs/example-daily-snapshot.md"
    page = (root / rel).read_text(encoding="utf-8")
    human_body = "Human-maintained schema notes without citation markers."
    edited = replace_section(page, "Column Definitions", human_body)
    assert edited != page
    (root / rel).write_text(edited, encoding="utf-8")
    return rel, human_body


def test_migration_preserves_uncited_drifted_section_with_warning(
    tmp_path, monkeypatch
):
    from lineage_wiki.okf.sections import split_sections

    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    rel, human_body = _plant_uncited_column_definitions(root, cfg)

    result = run_generate(cfg, root, FIXED_NOW)

    page = (root / rel).read_text(encoding="utf-8")
    assert human_body in dict(split_sections(page)[1])["Column Definitions"]
    assert any(
        rel in warning and "'Column Definitions'" in warning
        for warning in result.warnings
    )


def test_plan_only_preserves_uncited_drifted_section_with_warning(
    tmp_path, monkeypatch
):
    fixture = tmp_path / "bigquery.yml"
    fixture_data = yaml.safe_load(BQ_FIXTURES.read_text(encoding="utf-8"))
    fixture.write_text(yaml.safe_dump(fixture_data), encoding="utf-8")

    root = _setup_target(tmp_path, monkeypatch)
    monkeypatch.setenv("LINEAGE_WIKI_BQ_FIXTURES", str(fixture))
    cfg = load_config(EXAMPLE_CONFIG)
    rel, human_body = _plant_uncited_column_definitions(root, cfg)
    before = (root / rel).read_text(encoding="utf-8")

    columns = fixture_data["tables"][SNAPSHOT_TABLE]["columns"]
    columns[-1]["description"] = "Updated deterministic schema description."
    fixture.write_text(yaml.safe_dump(fixture_data), encoding="utf-8")

    result = run_update(cfg, root, "2026-07-08T00:00:00Z", plan_only=True)

    assert result.plan_only
    assert f"unchanged {rel}" in result.actions
    assert any(
        rel in warning and "'Column Definitions'" in warning
        for warning in result.warnings
    )
    assert (root / rel).read_text(encoding="utf-8") == before
    assert human_body in before


def test_migration_refreshes_uncited_section_when_snapshot_matches(
    tmp_path, monkeypatch
):
    from lineage_wiki.okf.sections import split_sections
    from lineage_wiki.storage.manifest import (
        compute_snapshot,
        load_manifest,
        save_manifest,
    )

    root = _setup_target(tmp_path, monkeypatch)
    cfg = load_config(EXAMPLE_CONFIG)
    rel, old_body = _plant_uncited_column_definitions(root, cfg)
    manifest = load_manifest(root)
    current = (root / rel).read_text(encoding="utf-8")
    manifest.chains[cfg.chain.id].file_snapshots[rel] = compute_snapshot(
        {rel: current}
    )
    save_manifest(root, manifest)

    result = run_generate(cfg, root, FIXED_NOW)

    page = (root / rel).read_text(encoding="utf-8")
    body = dict(split_sections(page)[1])["Column Definitions"]
    assert old_body not in body
    assert "| Column | Type | Mode | Description |" in body
    assert result.warnings == []
