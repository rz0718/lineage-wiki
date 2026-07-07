"""Slack report-evidence connector (``conversations.history`` / ``.replies``).

Fetches the newest message in a configured channel whose text contains
``match_text`` and normalizes it — plus its thread replies — into one
EvidenceItem, so report pages can quote what the live alert actually said.

Safety contract:

- Read-only Web API calls only: ``conversations.history`` and
  ``conversations.replies``. Nothing is ever posted or modified.
- The bot token is read from the environment variable named by
  ``api_token_env`` at run time; it is never stored, logged, or printed.

Client resolution order (``resolve_slack_client``, mirrors the BigQuery
connector):

1. ``LINEAGE_WIKI_SLACK_FIXTURES=<file>`` — load channel messages from a
   local YAML/JSON fixture file (mocked messages; used by tests and
   token-free runs).
2. ``LINEAGE_WIKI_SLACK_OFFLINE=1`` — treat Slack as unavailable (the test
   suite sets this so unit tests never touch the network).
3. HTTPS calls to slack.com using the token from ``api_token_env``. The
   token needs a history scope for the channel (e.g. ``channels:history``).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import SlackSource
from ..ingestion.evidence import EvidenceItem
from ..ingestion.fingerprints import sha_bytes
from ..util import slugify
from . import SourceUnavailableError

FIXTURES_ENV = "LINEAGE_WIKI_SLACK_FIXTURES"
OFFLINE_ENV = "LINEAGE_WIKI_SLACK_OFFLINE"

SLACK_API_BASE = "https://slack.com/api"


class SlackApiError(Exception):
    """A Slack Web API call failed (transport error or ``ok: false``)."""


# --- Messages ---------------------------------------------------------------------


@dataclass
class SlackMessage:
    """The subset of a Slack message the connector keeps. Never includes
    attachments, files, or user profile data — text and identity only."""

    ts: str
    text: str
    user: str | None = None
    thread_ts: str | None = None
    reply_count: int = 0


def _parse_message(raw: dict) -> SlackMessage:
    return SlackMessage(
        ts=str(raw.get("ts", "")),
        text=str(raw.get("text", "")),
        user=raw.get("user") or raw.get("bot_id"),
        thread_ts=raw.get("thread_ts"),
        reply_count=int(raw.get("reply_count", 0) or 0),
    )


def _message_sort_key(message: SlackMessage) -> float:
    try:
        return float(message.ts)
    except ValueError:
        return 0.0


# --- Clients ----------------------------------------------------------------------


class FixtureSlackClient:
    """Message client backed by a YAML/JSON fixture file mapping channel ids
    to message lists — used by tests and token-free demo runs.

    Fixture shape (top-level ``channels`` key optional)::

        channels:
          C0123456789:
            - ts: "1751851200.000100"
              text: "Gold PNL Daily: +12,345 USD"
              user: U111
              replies:
                - ts: "1751851300.000200"
                  text: "breakdown attached"
    """

    kind = "fixtures"

    def __init__(self, channels: dict[str, list[dict]], origin: str = "<memory>") -> None:
        self._channels = channels
        self.origin = origin

    @classmethod
    def from_file(cls, path: str | Path) -> "FixtureSlackClient":
        path = Path(path)
        if not path.is_file():
            raise SourceUnavailableError(
                f"Slack fixture file not found: {path} ({FIXTURES_ENV})"
            )
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SourceUnavailableError(
                f"Slack fixture file {path} must be a mapping of channel ids "
                "to message lists (optionally under a top-level `channels` key)"
            )
        channels = data.get("channels", data)
        return cls(channels, origin=str(path))

    def fetch_history(self, channel_id: str, oldest: float) -> list[SlackMessage]:
        raw = self._channels.get(channel_id) or []
        messages = [_parse_message(m) for m in raw]
        for message, entry in zip(messages, raw):
            if entry.get("replies"):
                message.reply_count = len(entry["replies"])
                message.thread_ts = message.thread_ts or message.ts
        recent = [m for m in messages if _message_sort_key(m) >= oldest]
        return sorted(recent, key=_message_sort_key, reverse=True)

    def fetch_replies(self, channel_id: str, thread_ts: str) -> list[SlackMessage]:
        for entry in self._channels.get(channel_id) or []:
            if str(entry.get("ts", "")) == thread_ts:
                return [_parse_message(m) for m in entry.get("replies", [])]
        return []


class HttpSlackClient:
    """Read-only client over the Slack Web API (stdlib urllib, no extra
    dependency). Exposes exactly two methods — history and replies — so
    nothing else can sneak in through this module."""

    kind = "slack_api"

    # One channel is one alert stream; 10 pages x 200 messages is far beyond
    # any realistic lookback window and caps the worst-case API usage.
    PAGE_LIMIT = 200
    MAX_PAGES = 10

    def __init__(
        self, token: str, *, base_url: str = SLACK_API_BASE, timeout: float = 30.0
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _call(self, method: str, params: dict[str, str]) -> dict:
        url = f"{self._base_url}/{method}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token}"}
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                    payload = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 1:
                    try:
                        delay = float(exc.headers.get("Retry-After") or 1.0)
                    except ValueError:
                        delay = 1.0
                    time.sleep(min(delay, 30.0))
                    continue
                raise SlackApiError(f"{method} failed with HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise SlackApiError(f"{method} request failed: {reason}") from exc
            except ValueError as exc:
                raise SlackApiError(f"{method} returned invalid JSON") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            error = payload.get("error", "unknown") if isinstance(payload, dict) else "unknown"
            raise SlackApiError(f"{method} returned error: {error}")
        return payload

    def fetch_history(self, channel_id: str, oldest: float) -> list[SlackMessage]:
        messages: list[SlackMessage] = []
        cursor: str | None = None
        for _ in range(self.MAX_PAGES):
            params = {
                "channel": channel_id,
                "oldest": f"{oldest:.6f}",
                "limit": str(self.PAGE_LIMIT),
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._call("conversations.history", params)
            messages.extend(_parse_message(m) for m in payload.get("messages", []))
            cursor = (payload.get("response_metadata") or {}).get("next_cursor") or None
            if not payload.get("has_more") or not cursor:
                break
        return sorted(messages, key=_message_sort_key, reverse=True)

    def fetch_replies(self, channel_id: str, thread_ts: str) -> list[SlackMessage]:
        messages: list[SlackMessage] = []
        cursor: str | None = None
        for _ in range(self.MAX_PAGES):
            params = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": str(self.PAGE_LIMIT),
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._call("conversations.replies", params)
            messages.extend(_parse_message(m) for m in payload.get("messages", []))
            cursor = (payload.get("response_metadata") or {}).get("next_cursor") or None
            if not payload.get("has_more") or not cursor:
                break
        # conversations.replies returns the parent as the first entry.
        return [m for m in messages if m.ts != thread_ts]


@dataclass
class ResolvedSlackClient:
    client: Any | None
    kind: str | None = None
    reason: str | None = None  # why client is None


def resolve_slack_client(api_token_env: str) -> ResolvedSlackClient:
    fixtures = os.environ.get(FIXTURES_ENV)
    if fixtures:
        return ResolvedSlackClient(FixtureSlackClient.from_file(fixtures), "fixtures")
    if os.environ.get(OFFLINE_ENV):
        return ResolvedSlackClient(None, reason=f"offline mode via {OFFLINE_ENV}")
    token = os.environ.get(api_token_env, "")
    if not token:
        return ResolvedSlackClient(
            None, reason=f"environment variable {api_token_env} is not set"
        )
    return ResolvedSlackClient(HttpSlackClient(token), "slack_api")


# --- Loading ----------------------------------------------------------------------


@dataclass
class SlackLoadResult:
    source: SlackSource
    available: bool
    client_kind: str | None = None
    unavailable_reason: str | None = None
    message: SlackMessage | None = None  # newest match; None when no match
    replies: list[SlackMessage] = field(default_factory=list)
    item: EvidenceItem | None = None

    @property
    def matched(self) -> bool:
        return self.message is not None


def _to_evidence(
    source: SlackSource, message: SlackMessage, replies: list[SlackMessage]
) -> EvidenceItem:
    content = message.text
    if replies:
        thread = "\n\n".join(f"[reply @ {r.ts}] {r.text}" for r in replies)
        content += "\n\n--- thread replies ---\n\n" + thread
    digest_source = "\0".join(
        [source.channel_id, message.ts, message.text]
        + [f"{r.ts}\0{r.text}" for r in replies]
    )
    return EvidenceItem(
        id=f"slack:{slugify(source.name)}",
        source_type="slack_message",
        source_uri=f"slack://{source.channel_id}/{message.ts}",
        title=source.name,
        content=content,
        metadata={
            "channel_id": source.channel_id,
            "ts": message.ts,
            "user": message.user,
            "match_text": source.match_text,
            "lookback_hours": source.lookback_hours,
            "reply_count": len(replies),
        },
        fingerprint=sha_bytes(digest_source.encode("utf-8")),
    )


def load_slack_source(
    source: SlackSource,
    *,
    client: Any | None = None,
    now: float | None = None,
    enforce_required: bool = True,
) -> SlackLoadResult:
    """Fetch the newest matching message (and its thread) for one source.

    ``enforce_required=False`` suppresses the required-source failure so
    fingerprinting can proceed even when Slack is down; loading for page
    generation keeps the default and fails clearly.
    """
    kind = getattr(client, "kind", "injected") if client is not None else None
    if client is None:
        resolved = resolve_slack_client(source.api_token_env)
        client, kind = resolved.client, resolved.kind
        if client is None:
            if source.required and enforce_required:
                raise SourceUnavailableError(
                    f"required Slack source `{source.name}` is unavailable: "
                    f"{resolved.reason}"
                )
            return SlackLoadResult(
                source=source, available=False, unavailable_reason=resolved.reason
            )

    oldest = (now if now is not None else time.time()) - source.lookback_hours * 3600
    try:
        history = client.fetch_history(source.channel_id, oldest)
        matches = [m for m in history if source.match_text in m.text]
        message = max(matches, key=_message_sort_key) if matches else None
        replies: list[SlackMessage] = []
        if message is not None and source.thread_replies and (
            message.reply_count > 0 or message.thread_ts
        ):
            replies = client.fetch_replies(
                source.channel_id, message.thread_ts or message.ts
            )
    except SlackApiError as exc:
        if source.required and enforce_required:
            raise SourceUnavailableError(
                f"required Slack source `{source.name}` is unavailable: {exc}"
            ) from exc
        return SlackLoadResult(
            source=source,
            available=False,
            client_kind=kind,
            unavailable_reason=str(exc),
        )

    if message is None:
        if source.required and enforce_required:
            raise SourceUnavailableError(
                f"required Slack source `{source.name}` found no message "
                f"matching {source.match_text!r} in channel "
                f"{source.channel_id} within the last {source.lookback_hours}h"
            )
        return SlackLoadResult(source=source, available=True, client_kind=kind)

    return SlackLoadResult(
        source=source,
        available=True,
        client_kind=kind,
        message=message,
        replies=replies,
        item=_to_evidence(source, message, replies),
    )


def load_slack_sources(
    sources: list[SlackSource],
    *,
    client: Any | None = None,
    now: float | None = None,
    enforce_required: bool = True,
) -> list[SlackLoadResult]:
    return [
        load_slack_source(
            source, client=client, now=now, enforce_required=enforce_required
        )
        for source in sources
    ]
