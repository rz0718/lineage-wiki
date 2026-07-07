"""Slack report-evidence connector: mocked messages only.

No test here touches the real Slack API — conftest forces offline mode, and
mocked messages come from tests/fixtures/slack_messages.yml (or per-test
fixture files with fresh timestamps for generate/update integration).
"""

import io
import json
import time
import urllib.error
from pathlib import Path

import pytest
import yaml

from lineage_wiki.agent.runner import run_generate, run_update
from lineage_wiki.config import ConfigError, SlackSource, load_config
from lineage_wiki.connectors import SourceUnavailableError
from lineage_wiki.connectors.slack_connector import (
    FixtureSlackClient,
    HttpSlackClient,
    SlackApiError,
    load_slack_source,
    resolve_slack_client,
)
from lineage_wiki.ingestion.fingerprints import compute_fingerprint_result
from lineage_wiki.ingestion.source_loader import load_sources

from .conftest import FIXED_NOW, REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "slack_messages.yml"
CHANNEL = "C0123456789"
# 36h before NOW is 1751870400: the "+10,000" message (1751851200) is out of
# the default window, the "+12,345" message (1751937600) is inside it.
NOW = 1752000000.0


@pytest.fixture
def wiki_root(tmp_path) -> Path:
    root = tmp_path / "wiki"
    root.mkdir()
    return root


@pytest.fixture
def fixture_client() -> FixtureSlackClient:
    return FixtureSlackClient.from_file(FIXTURES)


def _source(**overrides) -> SlackSource:
    base = dict(
        name="Gold PNL Daily Slack Alert",
        channel_id=CHANNEL,
        match_text="Gold PNL Daily",
        lookback_hours=36,
        required=False,
    )
    base.update(overrides)
    return SlackSource(**base)


def _load(client, **overrides):
    return load_slack_source(_source(**overrides), client=client, now=NOW)


# --- config ------------------------------------------------------------------------


def test_config_parses_slack_sources(tmp_path):
    cfg_file = tmp_path / "chain.yml"
    cfg_file.write_text(
        "chain: {id: c, name: C}\n"
        "sources:\n"
        "  slack:\n"
        "    - name: Gold PNL Daily Slack Alert\n"
        f"      channel_id: {CHANNEL}\n"
        '      match_text: "Gold PNL Daily"\n'
        "      lookback_hours: 36\n"
        "      report: Gold PNL Daily Report\n"
    )
    (source,) = load_config(cfg_file).sources.slack
    assert source.channel_id == CHANNEL
    assert source.report == "Gold PNL Daily Report"
    assert source.required is False
    assert source.thread_replies is True
    assert source.api_token_env == "SLACK_BOT_TOKEN"


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("channel_id", "'  '", "must be non-empty"),
        ("match_text", "''", "must be non-empty"),
        ("lookback_hours", "0", "greater than 0"),
        ("api_token_env", "not upper case", "environment variable name"),
    ],
)
def test_config_rejects_invalid_slack_fields(tmp_path, field, value, message):
    cfg_file = tmp_path / "bad.yml"
    base = {
        "name": "A",
        "channel_id": CHANNEL,
        "match_text": "x",
    }
    base[field] = value
    lines = "\n".join(f"      {k}: {v}" for k, v in base.items())
    cfg_file.write_text(
        "chain: {id: c, name: C}\nsources:\n  slack:\n    -\n" + lines + "\n"
    )
    with pytest.raises(ConfigError, match=message):
        load_config(cfg_file)


def test_config_rejects_unknown_slack_keys(tmp_path):
    cfg_file = tmp_path / "bad.yml"
    cfg_file.write_text(
        "chain: {id: c, name: C}\n"
        "sources:\n  slack:\n"
        f"    - {{name: A, channel_id: {CHANNEL}, match_text: x, api_token: shhh}}\n"
    )
    with pytest.raises(ConfigError, match="api_token"):
        load_config(cfg_file)


# --- fixture client ------------------------------------------------------------------


def test_fixture_client_filters_and_sorts_newest_first(fixture_client):
    messages = fixture_client.fetch_history(CHANNEL, oldest=NOW - 36 * 3600)
    assert [m.ts for m in messages] == ["1751995000.000600", "1751937600.000300"]
    assert messages[1].reply_count == 2


def test_fixture_client_returns_thread_replies(fixture_client):
    replies = fixture_client.fetch_replies(CHANNEL, "1751937600.000300")
    assert [r.text for r in replies] == ["breakdown attached", "confirmed vs ledger"]


def test_fixture_client_unknown_channel_is_empty(fixture_client):
    assert fixture_client.fetch_history("C0NOPE", oldest=0.0) == []
    assert fixture_client.fetch_replies("C0NOPE", "1") == []


def test_fixture_clients_expose_no_write_surface(fixture_client):
    # Structural safety guarantee: the client contract is read-only.
    for client in (fixture_client, HttpSlackClient("xoxb-test")):
        assert not hasattr(client, "post_message")
        assert not hasattr(client, "chat_postMessage")


# --- client resolution ----------------------------------------------------------------


def test_resolve_prefers_fixture_env(monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(FIXTURES))
    resolved = resolve_slack_client("SLACK_BOT_TOKEN")
    assert resolved.kind == "fixtures"
    assert resolved.client.fetch_history(CHANNEL, oldest=0.0)


def test_resolve_fails_clearly_on_missing_fixture_file(monkeypatch):
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", "does/not/exist.yml")
    with pytest.raises(SourceUnavailableError, match="fixture file not found"):
        resolve_slack_client("SLACK_BOT_TOKEN")


def test_resolve_offline_yields_reason():
    # conftest forces offline mode.
    resolved = resolve_slack_client("SLACK_BOT_TOKEN")
    assert resolved.client is None
    assert "offline mode" in resolved.reason


def test_resolve_reports_missing_token(monkeypatch):
    monkeypatch.delenv("LINEAGE_WIKI_SLACK_OFFLINE", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    resolved = resolve_slack_client("SLACK_BOT_TOKEN")
    assert resolved.client is None
    assert "SLACK_BOT_TOKEN is not set" in resolved.reason


def test_resolve_builds_http_client_from_token_env(monkeypatch):
    monkeypatch.delenv("LINEAGE_WIKI_SLACK_OFFLINE", raising=False)
    monkeypatch.setenv("MY_SLACK_TOKEN", "xoxb-test")
    resolved = resolve_slack_client("MY_SLACK_TOKEN")
    assert resolved.kind == "slack_api"
    assert isinstance(resolved.client, HttpSlackClient)


# --- load_slack_source ----------------------------------------------------------------


def test_load_picks_newest_matching_message(fixture_client):
    result = _load(fixture_client)
    assert result.available is True
    assert result.message.ts == "1751937600.000300"
    assert "+12,345" in result.message.text
    assert [r.text for r in result.replies] == [
        "breakdown attached",
        "confirmed vs ledger",
    ]


def test_load_normalizes_message_into_evidence(fixture_client):
    item = _load(fixture_client).item
    assert item.id == "slack:gold-pnl-daily-slack-alert"
    assert item.source_type == "slack_message"
    assert item.source_uri == f"slack://{CHANNEL}/1751937600.000300"
    assert item.title == "Gold PNL Daily Slack Alert"
    assert "+12,345" in item.content
    assert "--- thread replies ---" in item.content
    assert "confirmed vs ledger" in item.content
    assert item.metadata["channel_id"] == CHANNEL
    assert item.metadata["ts"] == "1751937600.000300"
    assert item.metadata["reply_count"] == 2
    assert item.fingerprint.startswith("sha256:")


def test_load_fingerprint_is_stable_and_reacts_to_thread_changes(fixture_client):
    assert _load(fixture_client).item.fingerprint == _load(fixture_client).item.fingerprint
    without_thread = _load(fixture_client, thread_replies=False)
    assert without_thread.replies == []
    assert without_thread.item.fingerprint != _load(fixture_client).item.fingerprint


def test_load_lookback_window_excludes_older_matches(fixture_client):
    narrow = _load(fixture_client, match_text="total +10,000")
    assert narrow.available is True and narrow.message is None

    wide = _load(fixture_client, match_text="total +10,000", lookback_hours=48)
    assert wide.message.ts == "1751851200.000200"
    assert wide.replies == []


def test_load_no_match_required_fails_clearly(fixture_client):
    with pytest.raises(SourceUnavailableError, match="found no message matching"):
        _load(fixture_client, channel_id="C0EMPTY", required=True)
    relaxed = load_slack_source(
        _source(channel_id="C0EMPTY", required=True),
        client=fixture_client,
        now=NOW,
        enforce_required=False,
    )
    assert relaxed.available is True and relaxed.message is None


def test_load_unavailable_optional_records_reason():
    # conftest forces offline mode, so client resolution yields nothing.
    result = load_slack_source(_source(), now=NOW)
    assert result.available is False
    assert "offline mode" in result.unavailable_reason


def test_load_unavailable_required_fails():
    with pytest.raises(SourceUnavailableError, match="required Slack source"):
        load_slack_source(_source(required=True), now=NOW)


# --- HTTP client (mocked transport) ----------------------------------------------------


def _fake_urlopen(pages, calls):
    def fake(request, timeout=None):
        calls.append(request)
        return io.BytesIO(json.dumps(pages.pop(0)).encode("utf-8"))

    return fake


def test_http_client_paginates_history(monkeypatch):
    pages = [
        {
            "ok": True,
            "has_more": True,
            "messages": [{"ts": "300.0", "text": "newest"}],
            "response_metadata": {"next_cursor": "abc"},
        },
        {"ok": True, "has_more": False, "messages": [{"ts": "100.0", "text": "oldest"}]},
    ]
    calls: list = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(pages, calls))

    messages = HttpSlackClient("xoxb-test").fetch_history(CHANNEL, oldest=50.0)
    assert [m.ts for m in messages] == ["300.0", "100.0"]
    assert len(calls) == 2
    assert "conversations.history" in calls[0].full_url
    assert "oldest=50.000000" in calls[0].full_url
    assert "cursor=abc" in calls[1].full_url
    assert calls[0].get_header("Authorization") == "Bearer xoxb-test"


def test_http_client_paginates_thread_replies(monkeypatch):
    pages = [
        {
            "ok": True,
            "has_more": True,
            "messages": [
                {"ts": "300.0", "text": "parent"},
                {"ts": "301.0", "text": "reply one"},
            ],
            "response_metadata": {"next_cursor": "xyz"},
        },
        {"ok": True, "has_more": False, "messages": [{"ts": "302.0", "text": "reply two"}]},
    ]
    calls: list = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(pages, calls))

    replies = HttpSlackClient("xoxb-test").fetch_replies(CHANNEL, "300.0")
    assert [r.text for r in replies] == ["reply one", "reply two"]
    assert len(calls) == 2
    assert "cursor=xyz" in calls[1].full_url


def test_http_client_replies_drop_the_parent(monkeypatch):
    pages = [
        {
            "ok": True,
            "messages": [
                {"ts": "300.0", "text": "parent"},
                {"ts": "301.0", "text": "first reply"},
            ],
        }
    ]
    calls: list = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(pages, calls))

    replies = HttpSlackClient("xoxb-test").fetch_replies(CHANNEL, "300.0")
    assert [r.text for r in replies] == ["first reply"]
    assert "conversations.replies" in calls[0].full_url


def test_http_client_surfaces_slack_errors(monkeypatch):
    pages = [{"ok": False, "error": "channel_not_found"}]
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(pages, []))
    with pytest.raises(SlackApiError, match="channel_not_found"):
        HttpSlackClient("xoxb-test").fetch_history(CHANNEL, oldest=0.0)


def test_http_client_api_error_is_optional_gap_or_required_failure(monkeypatch):
    def boom(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    result = load_slack_source(_source(), client=HttpSlackClient("xoxb-test"), now=NOW)
    assert result.available is False
    assert "no route to host" in result.unavailable_reason

    with pytest.raises(SourceUnavailableError, match="required Slack source"):
        load_slack_source(
            _source(required=True), client=HttpSlackClient("xoxb-test"), now=NOW
        )


# --- fingerprints -----------------------------------------------------------------------


def _slack_cfg(example_cfg, **overrides):
    cfg = example_cfg.model_copy(deep=True)
    base = dict(
        name="Example Daily Slack Alert",
        match_text="Example Daily",
        report="Example Daily Report",
    )
    base.update(overrides)
    cfg.sources.slack = [_source(**base)]
    return cfg


def _write_live_fixture(path: Path, text: str, *, replies: tuple[str, ...] = ()) -> None:
    """A fixture whose message ts is fresh relative to real wall-clock time,
    so generate/update runs (which use time.time()) see it in-window."""
    ts = time.time() - 3600
    message = {"ts": f"{ts:.6f}", "text": text, "user": "U0ALERTBOT"}
    if replies:
        message["replies"] = [
            {"ts": f"{ts + i + 1:.6f}", "text": reply} for i, reply in enumerate(replies)
        ]
    path.write_text(yaml.safe_dump({"channels": {CHANNEL: [message]}}))


def test_fingerprints_track_the_matched_message(example_cfg, wiki_root, tmp_path, monkeypatch):
    cfg = _slack_cfg(example_cfg)
    fixtures = tmp_path / "slack.yml"
    _write_live_fixture(fixtures, "Example Daily: total +1 USD")
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(fixtures))

    first = compute_fingerprint_result(cfg, wiki_root)
    assert first.fingerprints.slack["Example Daily Slack Alert"].startswith("sha256:")

    again = compute_fingerprint_result(cfg, wiki_root)
    assert again.fingerprints.slack == first.fingerprints.slack

    _write_live_fixture(fixtures, "Example Daily: total +2 USD")
    changed = compute_fingerprint_result(cfg, wiki_root)
    assert changed.fingerprints.slack != first.fingerprints.slack


def test_fingerprints_preserved_when_slack_unavailable(example_cfg, wiki_root, tmp_path, monkeypatch):
    cfg = _slack_cfg(example_cfg)
    fixtures = tmp_path / "slack.yml"
    _write_live_fixture(fixtures, "Example Daily: total +1 USD")
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(fixtures))
    online = compute_fingerprint_result(cfg, wiki_root).fingerprints

    monkeypatch.delenv("LINEAGE_WIKI_SLACK_FIXTURES")
    offline = compute_fingerprint_result(cfg, wiki_root, online)
    assert offline.fingerprints.slack == online.slack
    assert any("preserved its prior fingerprint" in w for w in offline.warnings)


def test_fingerprint_slack_reuses_provided_loads(example_cfg, wiki_root, tmp_path, monkeypatch):
    """The manifest must fingerprint the message the pages were rendered
    from — not whatever newer message a second fetch might return."""
    from lineage_wiki.connectors.slack_connector import load_slack_sources

    cfg = _slack_cfg(example_cfg)
    fixtures = tmp_path / "slack.yml"
    _write_live_fixture(fixtures, "Example Daily: total +1 USD")
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(fixtures))
    loads = load_slack_sources(cfg.sources.slack)

    # A newer alert lands after the pages were rendered from `loads`.
    _write_live_fixture(fixtures, "Example Daily: total +2 USD")
    pinned = compute_fingerprint_result(cfg, wiki_root, slack_loads=loads).fingerprints
    assert pinned.slack["Example Daily Slack Alert"] == loads[0].item.fingerprint
    refetched = compute_fingerprint_result(cfg, wiki_root).fingerprints
    assert refetched.slack != pinned.slack


def test_generate_manifest_reuses_plan_slack_load(
    example_cfg, wiki_root, tmp_path, monkeypatch
):
    import lineage_wiki.connectors.slack_connector as slack_connector
    from lineage_wiki.storage.manifest import load_manifest

    fixtures = tmp_path / "slack.yml"
    _write_live_fixture(fixtures, "Example Daily: total +1 USD")
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(fixtures))
    cfg = _slack_cfg(example_cfg)
    expected = slack_connector.load_slack_sources(cfg.sources.slack)[0].item.fingerprint

    # Fingerprinting resolves load_slack_sources lazily; page planning holds
    # its own reference, so a run that re-fetched for the manifest would trip
    # this stub while a plan-reusing run never calls it.
    def refuse(*args, **kwargs):
        raise AssertionError("manifest fingerprinting must reuse the plan's slack load")

    monkeypatch.setattr(slack_connector, "load_slack_sources", refuse)
    result = run_generate(cfg, wiki_root, now=FIXED_NOW)
    assert result.report.errors == []

    entry = load_manifest(wiki_root).chains[cfg.chain.id]
    assert entry.source_fingerprints.slack["Example Daily Slack Alert"] == expected


def test_config_hash_stable_without_slack_sources(example_cfg, wiki_root):
    # Chains without sources.slack must keep their pre-slack config hash, so
    # legacy manifests do not flag `chain config changed` forever.
    fingerprints = compute_fingerprint_result(example_cfg, wiki_root).fingerprints
    assert fingerprints.slack == {}
    with_empty_list = example_cfg.model_copy(deep=True)
    with_empty_list.sources.slack = []
    assert (
        compute_fingerprint_result(with_empty_list, wiki_root).fingerprints.config
        == fingerprints.config
    )


# --- generate / update integration ------------------------------------------------------


def test_generate_quotes_slack_evidence_on_report_page(
    example_cfg, wiki_root, tmp_path, monkeypatch
):
    fixtures = tmp_path / "slack.yml"
    _write_live_fixture(
        fixtures, "Example Daily: total +12,345 USD", replies=("breakdown attached",)
    )
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(fixtures))

    cfg = _slack_cfg(example_cfg)
    result = run_generate(cfg, wiki_root, now=FIXED_NOW)
    assert result.report.errors == []
    assert any("matched message" in line for line in result.evidence)

    page = (wiki_root / "okf" / "report-templates" / "example-daily-report.md").read_text()
    assert "## Slack Evidence" in page
    assert '"Example Daily" in channel `C0123456789`' in page
    assert "> Example Daily: total +12,345 USD" in page
    assert "Thread replies (1):" in page
    assert "> breakdown attached" in page


def test_generate_without_slack_keeps_report_page_unchanged(example_cfg, wiki_root):
    run_generate(example_cfg, wiki_root, now=FIXED_NOW)
    page = (wiki_root / "okf" / "report-templates" / "example-daily-report.md").read_text()
    assert "Slack Evidence" not in page


def test_generate_slack_unavailable_is_a_known_gap(example_cfg, wiki_root):
    # conftest forces offline mode.
    cfg = _slack_cfg(example_cfg)
    result = run_generate(cfg, wiki_root, now=FIXED_NOW)
    assert result.report.errors == []
    gaps = "\n".join(result.gaps)
    assert "Slack source `Example Daily Slack Alert` is unavailable" in gaps

    page = (wiki_root / "okf" / "report-templates" / "example-daily-report.md").read_text()
    assert "## Slack Evidence" in page
    assert "is unavailable: offline mode" in page


def test_generate_fails_clearly_when_slack_required(example_cfg, wiki_root):
    cfg = _slack_cfg(example_cfg, required=True)
    with pytest.raises(SourceUnavailableError, match="required Slack source"):
        run_generate(cfg, wiki_root, now=FIXED_NOW)


def test_generate_flags_unresolved_report_link(example_cfg, wiki_root):
    cfg = _slack_cfg(example_cfg, report="No Such Report")
    result = run_generate(cfg, wiki_root, now=FIXED_NOW)
    gaps = "\n".join(result.gaps)
    assert "names report `No Such Report` which is not configured" in gaps


def test_source_loader_includes_slack_items(example_cfg, wiki_root, tmp_path, monkeypatch):
    fixtures = tmp_path / "slack.yml"
    _write_live_fixture(fixtures, "Example Daily: total +1 USD")
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(fixtures))
    bundle = load_sources(_slack_cfg(example_cfg), wiki_root)
    types = {i.source_type for i in bundle.all_items()}
    assert "slack_message" in types


def test_update_reacts_to_new_message_and_noops_otherwise(
    example_cfg, wiki_root, tmp_path, monkeypatch
):
    fixtures = tmp_path / "slack.yml"
    _write_live_fixture(fixtures, "Example Daily: total +1 USD")
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(fixtures))
    cfg = _slack_cfg(example_cfg)

    run_generate(cfg, wiki_root, now=FIXED_NOW)

    # Unchanged message -> strict no-op.
    result = run_update(cfg, wiki_root, now=FIXED_NOW)
    assert result.noop is True

    # A newer alert message -> the linked report page is rewritten.
    _write_live_fixture(fixtures, "Example Daily: total +2 USD")
    result = run_update(cfg, wiki_root, now=FIXED_NOW)
    assert result.noop is False
    assert "slack source `Example Daily Slack Alert` changed" in {
        reason
        for reasons in result.impact.values()
        for reason in reasons
    }
    report_rel = "okf/report-templates/example-daily-report.md"
    assert report_rel in result.impact
    page = (wiki_root / report_rel).read_text()
    assert "total +2 USD" in page
    assert "total +1 USD" not in page


def test_update_noops_through_a_slack_outage(
    example_cfg, wiki_root, tmp_path, monkeypatch
):
    fixtures = tmp_path / "slack.yml"
    _write_live_fixture(fixtures, "Example Daily: total +1 USD")
    monkeypatch.setenv("LINEAGE_WIKI_SLACK_FIXTURES", str(fixtures))
    cfg = _slack_cfg(example_cfg)
    run_generate(cfg, wiki_root, now=FIXED_NOW)

    # Slack goes down -> the prior fingerprint is preserved and the run
    # stays a warned no-op instead of churning the report page.
    monkeypatch.delenv("LINEAGE_WIKI_SLACK_FIXTURES")
    result = run_update(cfg, wiki_root, now=FIXED_NOW)
    assert result.noop is True
    assert any("preserved its prior fingerprint" in w for w in result.warnings)
