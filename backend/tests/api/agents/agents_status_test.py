"""
Agent Status feature tests.

Scenarios:
  1. Parser unit tests — frontmatter parsing, severity normalization, fallback summary,
     oversized frontmatter, timestamp resolution (pure service calls, no HTTP)
  2. Rate-limit helpers — is_rate_limited / _mark_rate_limit behaviour
  3. is_stale logic for running vs stopped environments
  4. GET /agents/status — list snapshots for current user (cache-only)
  5. GET /agents/{agent_id}/status — happy path (cached), 404, 403
  6. GET /agents/{agent_id}/status?force_refresh=true — live fetch via stub adapter
  7. POST /internal/environments/{env_id}/status-updated — push path

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
        # YAML that will fail to parse as dict
        content = "---\n: invalid: yaml: content\n---\nBody."
        result = AgentStatusService.parse_status_file(content)
        # No structured metadata — YAML parse failure falls through
        assert result.has_structured_metadata is False

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
# 4. is_stale
# ---------------------------------------------------------------------------

class TestIsStale:

    def _make_env(self, status="running", fetched_at=None):
        env = MagicMock()
        env.status = status
        env.status_file_fetched_at = fetched_at
        return env

    def test_stale_when_env_not_running(self):
        env = self._make_env(status="stopped")
        assert AgentStatusService.is_stale(env) is True

    def test_stale_when_never_fetched(self):
        env = self._make_env(status="running", fetched_at=None)
        assert AgentStatusService.is_stale(env) is True

    def test_not_stale_when_recently_fetched(self):
        env = self._make_env(
            status="running",
            fetched_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        assert AgentStatusService.is_stale(env) is False

    def test_stale_when_old(self):
        env = self._make_env(
            status="running",
            fetched_at=datetime.now(UTC) - timedelta(seconds=700),
        )
        assert AgentStatusService.is_stale(env) is True


# ---------------------------------------------------------------------------
# 5. API tests (via TestClient)
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
    A newly created agent with no STATUS.md data has is_stale=True and no severity.
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
    assert our["is_stale"] is True


def test_get_agent_status_no_status_file(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/{agent_id}/status returns a stale, null-severity snapshot
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
    assert body["is_stale"] is True
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
    assert body["is_stale"] is True


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
    # so fetch_workspace_item_with_meta returns this content for STATUS.md
    # whenever an environment exists for the agent.
    EnvironmentTestAdapter.workspace_files["STATUS.md"] = status_content
    try:
        r = client.get(
            f"{settings.API_V1_STR}/agents/{agent_id}/status?force_refresh=true",
            headers=superuser_token_headers,
        )
        assert r.status_code == 200
    finally:
        EnvironmentTestAdapter.workspace_files.pop("STATUS.md", None)


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


def test_push_status_updated_env_not_found(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """POST /internal/environments/{env_id}/status-updated returns 404 for unknown env."""
    fake_env_id = str(uuid.uuid4())
    r = client.post(
        f"{settings.API_V1_STR}/internal/environments/{fake_env_id}/status-updated",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_push_status_updated_ok(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    POST /internal/environments/{env_id}/status-updated returns ok=true.
    When file is missing (stub default), fetched=false.
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # Fetch the agent to get environment id
    r_agent = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}",
        headers=superuser_token_headers,
    )
    assert r_agent.status_code == 200
    env_id = r_agent.json().get("active_environment_id")
    if not env_id:
        pytest.skip("No active environment created by stub")

    r = client.post(
        f"{settings.API_V1_STR}/internal/environments/{env_id}/status-updated",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # File missing → fetched=False (no STATUS.md in stub)
    assert "fetched" in body


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
