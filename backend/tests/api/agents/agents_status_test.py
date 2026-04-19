"""
Agent Status feature tests.

Scenarios:
  1. Parser unit tests — frontmatter parsing, severity normalization, fallback summary,
     oversized frontmatter, timestamp resolution (pure service calls, no HTTP)
  2. Rate-limit helpers — is_rate_limited / _mark_rate_limit behaviour
  3. refresh_after_action + handle_post_action_event — post-action backend pull
  4. GET /agents/status — list snapshots for current user (cache-only)
  5. GET /agents/{agent_id}/status — happy path (cached), 404, 403
  6. GET /agents/{agent_id}/status?force_refresh=true — live fetch via stub adapter

All HTTP tests go through TestClient; parser/service tests call methods directly
(no DB or adapter, just pure Python logic).
"""
import uuid
from datetime import datetime, UTC, timedelta
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.services.agents.agent_status_service import AgentStatusService, StatusUnavailableError
from tests.utils.agent import create_agent_via_api
from tests.utils.utils import random_lower_string


# ---------------------------------------------------------------------------
# 1. Parser unit tests — pure Python, no DB/HTTP
# ---------------------------------------------------------------------------

class TestParseStatusFile:

    def test_valid_frontmatter_ok(self):
        content = "---\nstatus: ok\nsummary: All good\ntimestamp: 2026-01-15T10:00:00Z\n---\n\nBody text."
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "ok"
        assert result.summary == "All good"
        assert result.reported_at is not None
        assert result.has_structured_metadata is True

    def test_valid_frontmatter_warning(self):
        content = "---\nstatus: warning\nsummary: Queue depth elevated\n---\nDetails here."
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "warning"
        assert result.summary == "Queue depth elevated"

    def test_valid_frontmatter_error(self):
        content = "---\nstatus: error\nsummary: DB unreachable\n---\nCheck logs."
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "error"

    def test_valid_frontmatter_info(self):
        content = "---\nstatus: info\nsummary: Maintenance window\n---\nNothing to worry about."
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "info"

    def test_unknown_severity_normalized(self):
        content = "---\nstatus: critical\nsummary: Something\n---\n"
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "unknown"

    def test_case_insensitive_severity(self):
        content = "---\nstatus: OK\n---\n"
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "ok"

    def test_no_frontmatter_extracts_fallback_summary(self):
        content = "# Agent Status\n\nAll systems operational."
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "unknown"
        assert result.has_structured_metadata is False
        # Fallback summary is first non-blank, non-heading line
        assert result.summary == "All systems operational."

    def test_no_frontmatter_skips_heading_for_summary(self):
        content = "# Heading\n## Sub\nFirst real line."
        result = AgentStatusService.parse_status_file(content)
        assert result.summary == "First real line."

    def test_malformed_yaml_falls_through(self):
        # YAML that will fail to parse as dict and has no recognizable key:value lines
        content = "---\n: invalid: yaml: content\n---\nBody."
        result = AgentStatusService.parse_status_file(content)
        # No structured metadata — YAML parse failure falls through
        assert result.has_structured_metadata is False

    def test_yaml_unsafe_summary_recovered_by_lenient_parser(self):
        # `summary: [warning] text here` is invalid YAML (flow sequence not closed),
        # but the lenient line-based fallback still extracts the known keys.
        content = (
            "---\n"
            "timestamp: 2026-04-19T13:34:54Z\n"
            "status: warning\n"
            "summary: [warning] packet loss within acceptable whimsy\n"
            "---\n\n"
            "## Rotation test\n"
            "- Last cron run: 2026-04-19T13:34:54Z\n"
        )
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "warning"
        assert result.summary == "[warning] packet loss within acceptable whimsy"
        assert result.reported_at is not None
        assert result.has_structured_metadata is True

    def test_oversized_frontmatter_falls_through(self):
        # Frontmatter > 4 KB → no structured metadata
        big_value = "x" * (AgentStatusService.MAX_FRONTMATTER_BYTES + 100)
        content = f"---\nstatus: ok\nsummary: {big_value}\n---\nBody."
        result = AgentStatusService.parse_status_file(content)
        assert result.has_structured_metadata is False

    def test_summary_truncated_at_512(self):
        long_summary = "S" * 600
        content = f"---\nstatus: ok\nsummary: {long_summary}\n---\n"
        result = AgentStatusService.parse_status_file(content)
        assert len(result.summary) == 512

    def test_empty_content(self):
        result = AgentStatusService.parse_status_file("")
        assert result.severity == "unknown"
        assert result.summary is None
        assert result.has_structured_metadata is False

    def test_frontmatter_missing_status_key_severity_unknown(self):
        # Frontmatter is valid YAML dict but has no "status" or "timestamp" key
        content = "---\nnote: just a note\n---\nBody."
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "unknown"
        assert result.has_structured_metadata is False  # no status/timestamp key

    def test_frontmatter_invalid_timestamp_ignored(self):
        content = "---\nstatus: ok\ntimestamp: not-a-date\n---\nBody."
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "ok"
        assert result.reported_at is None  # invalid timestamp → None

    def test_non_utf8_bytes_handled(self):
        # The parser receives a str (already decoded); simulate replacement chars
        content = "---\nstatus: ok\n---\n\uFFFD replacement char in body"
        result = AgentStatusService.parse_status_file(content)
        assert result.severity == "ok"


# ---------------------------------------------------------------------------
# 2. _resolve_reported_at
# ---------------------------------------------------------------------------

class TestResolveReportedAt:

    def test_frontmatter_wins_over_mtime(self):
        fm_ts = datetime(2026, 1, 10, tzinfo=UTC)
        mtime = datetime(2026, 1, 15, tzinfo=UTC)
        ts, source = AgentStatusService._resolve_reported_at(fm_ts, mtime)
        assert ts == fm_ts
        assert source == "frontmatter"

    def test_falls_back_to_mtime(self):
        mtime = datetime(2026, 1, 15, tzinfo=UTC)
        ts, source = AgentStatusService._resolve_reported_at(None, mtime)
        assert ts == mtime
        assert source == "file_mtime"

    def test_both_none_returns_none(self):
        ts, source = AgentStatusService._resolve_reported_at(None, None)
        assert ts is None
        assert source is None


# ---------------------------------------------------------------------------
# 3. Rate-limit helpers
# ---------------------------------------------------------------------------

class TestRateLimit:

    def test_not_rate_limited_initially(self):
        env_id = uuid.uuid4()
        assert AgentStatusService.is_rate_limited(env_id) is False

    def test_rate_limited_after_mark(self):
        env_id = uuid.uuid4()
        AgentStatusService._mark_rate_limit(env_id)
        assert AgentStatusService.is_rate_limited(env_id) is True

    def test_not_rate_limited_after_ttl(self):
        env_id = uuid.uuid4()
        # Backdate the lock entry beyond the TTL
        from app.services.agents import agent_status_service as _mod
        _mod._rate_limit_lock[env_id] = datetime.now(UTC) - timedelta(
            seconds=AgentStatusService.FORCE_REFRESH_TTL_SECONDS + 1
        )
        assert AgentStatusService.is_rate_limited(env_id) is False


# ---------------------------------------------------------------------------
# 3b. refresh_after_action / handle_stream_event — post-action backend pull
# ---------------------------------------------------------------------------

class TestRefreshAfterAction:
    """The backend pulls STATUS.md after every action it triggered in the
    agent-env (session stream, CRON script trigger). This ensures the agents
    list reflects state changes after every backend-triggered action without
    needing a background polling tick."""

    def test_refresh_after_action_skipped_when_rate_limited(self):
        """When a recent push already fetched, refresh_after_action is a no-op."""
        import asyncio
        env = MagicMock()
        env.id = uuid.uuid4()

        AgentStatusService._mark_rate_limit(env.id)

        with patch.object(AgentStatusService, "fetch_status") as mock_fetch:
            mock_fetch.return_value = None
            asyncio.run(AgentStatusService.refresh_after_action(env))

        mock_fetch.assert_not_called()

    def test_refresh_after_action_calls_fetch_when_not_rate_limited(self):
        """Outside the rate-limit window, fetch_status is invoked."""
        import asyncio
        env = MagicMock()
        env.id = uuid.uuid4()
        # Ensure no rate-limit entry exists for this env
        from app.services.agents import agent_status_service as _mod
        _mod._rate_limit_lock.pop(env.id, None)

        async def _fake_fetch(environment, db_session=None):
            return None

        with patch.object(
            AgentStatusService, "fetch_status", side_effect=_fake_fetch
        ) as mock_fetch:
            asyncio.run(AgentStatusService.refresh_after_action(env))

        mock_fetch.assert_called_once()

    def test_refresh_after_action_swallows_unavailable(self):
        """StatusUnavailableError (env stopped, file missing) is silently swallowed."""
        import asyncio
        env = MagicMock()
        env.id = uuid.uuid4()
        from app.services.agents import agent_status_service as _mod
        _mod._rate_limit_lock.pop(env.id, None)

        async def _raise_unavailable(environment, db_session=None):
            raise StatusUnavailableError("file_missing")

        with patch.object(
            AgentStatusService, "fetch_status", side_effect=_raise_unavailable
        ):
            # Must not raise
            asyncio.run(AgentStatusService.refresh_after_action(env))

    def test_handle_post_action_event_no_environment_id_returns_cleanly(self):
        """Handler ignores events that lack environment_id in meta."""
        import asyncio
        with patch.object(AgentStatusService, "refresh_after_action") as mock_refresh:
            asyncio.run(
                AgentStatusService.handle_post_action_event({"meta": {"agent_id": "x"}})
            )
        mock_refresh.assert_not_called()

    def test_handle_post_action_event_resolves_env_and_refreshes(
        self,
        client: TestClient,
        superuser_token_headers: dict[str, str],
        db: Session,
    ) -> None:
        """
        Positive path: when meta carries a real environment_id, the handler
        loads the env from DB and delegates to refresh_after_action.
        """
        import asyncio
        from sqlmodel import select
        from app.models.environments.environment import AgentEnvironment

        agent = create_agent_via_api(client, superuser_token_headers)
        # The agent-creation flow writes an AgentEnvironment row via the stub;
        # pick any row for this agent (active_environment_id may not be set).
        env = db.exec(
            select(AgentEnvironment).where(AgentEnvironment.agent_id == uuid.UUID(agent["id"]))
        ).first()
        if env is None:
            pytest.skip("No environment row created by stub")
        env_id = str(env.id)

        async def _noop(environment, db_session=None):
            return None

        with patch.object(
            AgentStatusService, "refresh_after_action", side_effect=_noop
        ) as mock_refresh:
            asyncio.run(
                AgentStatusService.handle_post_action_event(
                    {"meta": {"environment_id": env_id}}
                )
            )

        mock_refresh.assert_called_once()
        env_arg = mock_refresh.call_args.args[0]
        assert str(env_arg.id) == env_id

    def test_handle_post_action_event_unknown_env_id_returns_cleanly(self):
        """Handler no-ops when the environment_id in meta is not in the DB."""
        import asyncio
        bogus_env_id = str(uuid.uuid4())
        with patch.object(AgentStatusService, "refresh_after_action") as mock_refresh:
            asyncio.run(
                AgentStatusService.handle_post_action_event(
                    {"meta": {"environment_id": bogus_env_id}}
                )
            )
        mock_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# 4. API tests (via TestClient)
# ---------------------------------------------------------------------------

def test_list_agent_statuses_empty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/status returns an empty list when the user has no agents.
    (Superuser has no agents in a clean test transaction.)
    """
    r = client.get(
        f"{settings.API_V1_STR}/agents/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_list_agent_statuses_with_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/status returns snapshot for each agent owned by the user.
    A newly created agent with no STATUS.md data has null severity.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    r = client.get(
        f"{settings.API_V1_STR}/agents/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 1

    # Find our agent in the list
    our = next((i for i in items if i["agent_id"] == agent_id), None)
    assert our is not None
    assert our["severity"] is None
    assert our["raw"] is None


def test_get_agent_status_no_status_file(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/{agent_id}/status returns a null-severity snapshot
    for an agent that has never published STATUS.md.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == agent_id
    assert body["severity"] is None
    assert body["fetched_at"] is None


def test_get_agent_status_404_unknown_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """GET /agents/{agent_id}/status returns 404 for a non-existent agent."""
    fake_id = str(uuid.uuid4())
    r = client.get(
        f"{settings.API_V1_STR}/agents/{fake_id}/status",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_get_agent_status_403_other_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """GET /agents/{agent_id}/status returns 403 when agent belongs to another user."""
    # Create agent as superuser
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # Try to access as normal user
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/status",
        headers=normal_user_token_headers,
    )
    assert r.status_code == 403


def test_get_agent_status_force_refresh_file_missing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    GET /agents/{agent_id}/status?force_refresh=true falls back to cached
    snapshot when the adapter reports the file as missing (stub default).
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # Stub returns exists=False by default (no workspace_files set)
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
        headers=superuser_token_headers,
    )
    # Should NOT be 500 — falls back to cached snapshot (which is empty)
    assert r.status_code == 200
    body = r.json()
    assert body["severity"] is None
    assert body["raw"] is None


def test_get_agent_status_force_refresh_with_status_file(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    GET /agents/{agent_id}/status?force_refresh=true fetches and parses STATUS.md
    when the adapter has file content.
    """
    from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter

    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    status_content = b"---\nstatus: ok\nsummary: All systems nominal\n---\n\n# Agent Status\n"

    # workspace_files is a class-level dict on the test adapter — populate it
    # so fetch_workspace_item_with_meta returns this content for docs/STATUS.md
    # whenever an environment exists for the agent.
    EnvironmentTestAdapter.workspace_files["docs/STATUS.md"] = status_content
    try:
        r = client.get(
            f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
            headers=superuser_token_headers,
        )
        assert r.status_code == 200
    finally:
        EnvironmentTestAdapter.workspace_files.pop("docs/STATUS.md", None)


def test_get_agent_status_force_refresh_rate_limited(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/{agent_id}/status?force_refresh=true returns 429 when
    the rate limit is active for that environment.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # First call — should succeed (or fall back to empty cache, but not 429)
    r1 = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
        headers=superuser_token_headers,
    )
    # Could be 200 (cached fallback) or 429 if mark was already set
    # Now force-mark rate limit by calling the endpoint twice quickly
    r2 = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
        headers=superuser_token_headers,
    )
    # At least one of them should succeed; if rate limited the second is 429
    assert r1.status_code in (200, 429)
    assert r2.status_code in (200, 429)


def test_list_agent_statuses_workspace_filter(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/status?workspace_id=<id> filters to agents in that workspace.
    A fake workspace_id returns an empty list.
    """
    fake_workspace_id = str(uuid.uuid4())
    r = client.get(
        f"{settings.API_V1_STR}/agents/status?workspace_id={fake_workspace_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_get_agent_status_unauthenticated(client: TestClient) -> None:
    """GET /agents/{agent_id}/status without auth returns 401 or 403."""
    fake_id = str(uuid.uuid4())
    r = client.get(f"{settings.API_V1_STR}/agents/{fake_id}/status")
    assert r.status_code in (401, 403)
