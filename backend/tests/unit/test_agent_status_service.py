"""
Unit tests for AgentStatusService — pure Python, no HTTP, no database.

Covers:
  1. TestParseStatusFile — frontmatter parsing, severity normalization, fallback
     summary, oversized frontmatter, edge cases
  2. TestResolveReportedAt — timestamp resolution priority
  3. TestRateLimit — is_rate_limited / _mark_rate_limit behaviour
  4. TestRefreshAfterAction — post-action backend pull (mocked fetch_status)

These tests call AgentStatusService methods directly and are intentionally
separated from the API-level tests in tests/api/agents/agents_status_test.py.
They live here alongside other pure-unit tests (test_a2a_stream_event_handler.py,
test_opencode_event_transformer.py) which follow the same pattern.
"""
import asyncio
import uuid
from datetime import datetime, UTC, timedelta
from unittest.mock import patch, MagicMock

import pytest

from app.services.agents.agent_status_service import AgentStatusService, StatusUnavailableError


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
# 3b. refresh_after_action / handle_post_action_event — post-action backend pull
# ---------------------------------------------------------------------------

class TestRefreshAfterAction:
    """The backend pulls STATUS.md after every action it triggered in the
    agent-env (session stream, CRON script trigger). This ensures the agents
    list reflects state changes after every backend-triggered action without
    needing a background polling tick."""

    def test_refresh_after_action_skipped_when_rate_limited(self):
        """When a recent push already fetched, refresh_after_action is a no-op."""
        env = MagicMock()
        env.id = uuid.uuid4()

        AgentStatusService._mark_rate_limit(env.id)

        with patch.object(AgentStatusService, "fetch_status") as mock_fetch:
            mock_fetch.return_value = None
            asyncio.run(AgentStatusService.refresh_after_action(env))

        mock_fetch.assert_not_called()

    def test_refresh_after_action_calls_fetch_when_not_rate_limited(self):
        """Outside the rate-limit window, fetch_status is invoked."""
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
        with patch.object(AgentStatusService, "refresh_after_action") as mock_refresh:
            asyncio.run(
                AgentStatusService.handle_post_action_event({"meta": {"agent_id": "x"}})
            )
        mock_refresh.assert_not_called()

    def test_handle_post_action_event_unknown_env_id_returns_cleanly(self):
        """Handler no-ops when session.get returns None (env not in DB).

        Mocks the DB session so that session.get(AgentEnvironment, ...) returns
        None, exercising the explicit early-return branch rather than relying on
        the outer try/except to swallow a real DB error.
        """
        bogus_env_id = str(uuid.uuid4())

        mock_session = MagicMock()
        mock_session.get.return_value = None  # env not found

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_session)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with patch(
            "app.core.db.create_session", return_value=mock_cm
        ), patch.object(AgentStatusService, "refresh_after_action") as mock_refresh:
            asyncio.run(
                AgentStatusService.handle_post_action_event(
                    {"meta": {"environment_id": bogus_env_id}}
                )
            )

        mock_refresh.assert_not_called()

    def test_handle_post_action_event_resolves_env_and_refreshes(self):
        """
        Positive path: when meta carries a valid environment_id, the handler
        loads the env from the DB session and delegates to refresh_after_action.

        Uses a mock session so no real DB is required (unit-test style, consistent
        with the rest of this class).
        """
        env_id = uuid.uuid4()
        env_id_str = str(env_id)

        # The mock environment returned by session.get
        mock_env = MagicMock()
        mock_env.id = env_id

        mock_session = MagicMock()
        mock_session.get.return_value = mock_env
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_session)
        mock_cm.__exit__ = MagicMock(return_value=False)

        async def _noop(environment, db_session=None):
            return None

        with patch(
            "app.core.db.create_session", return_value=mock_cm
        ), patch.object(
            AgentStatusService, "refresh_after_action", side_effect=_noop
        ) as mock_refresh:
            asyncio.run(
                AgentStatusService.handle_post_action_event(
                    {"meta": {"environment_id": env_id_str}}
                )
            )

        mock_refresh.assert_called_once()
        env_arg = mock_refresh.call_args.args[0]
        assert env_arg.id == env_id
