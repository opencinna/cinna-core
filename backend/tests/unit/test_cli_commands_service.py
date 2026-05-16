"""
Unit tests for CLICommandsService — pure Python, no HTTP, no database.

Covers:
  1. TestParseCommandsFile — valid YAML, empty file, malformed YAML (parse_error path),
     missing name/command fields, duplicate names, slug validation, command length cap,
     description truncation at 512 chars, 50-command limit, unknown keys ignored
  2. TestRateLimit — is_rate_limited / _mark_rate_limit behaviour
  3. TestRefreshAfterAction — best-effort pull (mocked fetch_commands)
  4. TestHandlePostActionEvent — event handler guards and delegation
  5. TestGetCachedCommands — reads from DB row without adapter call
"""
import asyncio
import uuid
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.services.agents.cli_commands_service import (
    CLICommandsService,
    CLICommandsParseError,
    CLICommandsUnavailableError,
    ParsedCLICommand,
)


# ---------------------------------------------------------------------------
# 1. parse_commands_file — pure Python, no DB/HTTP
# ---------------------------------------------------------------------------

class TestParseCommandsFile:

    def test_valid_yaml_single_command(self):
        raw = (
            "commands:\n"
            "  - name: check\n"
            "    command: uv run /app/workspace/scripts/check.py\n"
            "    description: Monthly data check\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1
        assert result[0].name == "check"
        assert result[0].command == "uv run /app/workspace/scripts/check.py"
        assert result[0].description == "Monthly data check"

    def test_valid_yaml_multiple_commands(self):
        raw = (
            "commands:\n"
            "  - name: check\n"
            "    command: uv run /app/workspace/scripts/check.py\n"
            "  - name: report\n"
            "    command: uv run /app/workspace/scripts/report.py\n"
            "    description: Generate weekly report\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 2
        assert result[0].name == "check"
        assert result[1].name == "report"

    def test_valid_yaml_no_description_is_none(self):
        raw = "commands:\n  - name: check\n    command: uv run /app/workspace/check.py\n"
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1
        assert result[0].description is None

    def test_empty_string_returns_empty_list(self):
        """Empty file: yaml.safe_load('') returns None, treated as empty list."""
        result = CLICommandsService.parse_commands_file("")
        assert result == []

    def test_empty_commands_list(self):
        """File with commands: [] returns empty list without error."""
        raw = "commands: []\n"
        result = CLICommandsService.parse_commands_file(raw)
        assert result == []

    def test_malformed_yaml_raises_parse_error(self):
        """Invalid YAML raises CLICommandsParseError."""
        raw = "commands:\n  - name: [unclosed bracket\n"
        with pytest.raises(CLICommandsParseError):
            CLICommandsService.parse_commands_file(raw)

    def test_no_commands_key_returns_empty(self):
        """Valid YAML without 'commands' key treated as empty list, no error."""
        raw = "version: 1\nsome_key: value\n"
        result = CLICommandsService.parse_commands_file(raw)
        assert result == []

    def test_commands_not_a_list_returns_empty(self):
        """'commands' key that is not a list is treated as empty."""
        raw = "commands: not-a-list\n"
        result = CLICommandsService.parse_commands_file(raw)
        assert result == []

    def test_top_level_not_mapping_returns_empty(self):
        """YAML that is a list at top level, not a mapping."""
        raw = "- item1\n- item2\n"
        result = CLICommandsService.parse_commands_file(raw)
        assert result == []

    def test_missing_name_field_skips_entry(self):
        """Entry without 'name' is skipped; other valid entries still parsed."""
        raw = (
            "commands:\n"
            "  - command: uv run /app/workspace/scripts/a.py\n"
            "  - name: good\n"
            "    command: uv run /app/workspace/scripts/b.py\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1
        assert result[0].name == "good"

    def test_missing_command_field_skips_entry(self):
        """Entry without 'command' is skipped."""
        raw = (
            "commands:\n"
            "  - name: bad\n"
            "    description: No command here\n"
            "  - name: good\n"
            "    command: uv run /app/workspace/scripts/good.py\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1
        assert result[0].name == "good"

    def test_empty_command_after_trim_skips_entry(self):
        """Command that is only whitespace is skipped."""
        raw = "commands:\n  - name: bad\n    command: '   '\n"
        result = CLICommandsService.parse_commands_file(raw)
        assert result == []

    def test_invalid_name_slug_skips_entry(self):
        """Names not matching ^[a-z][a-z0-9_-]{0,31}$ are skipped."""
        invalid_names = [
            "0starts-with-digit",
            "Has-Upper",
            "has space",
            "has!special",
            "",
            "a" * 33,  # exceeds 32 chars total
        ]
        for bad_name in invalid_names:
            raw = f"commands:\n  - name: {bad_name!r}\n    command: echo test\n"
            result = CLICommandsService.parse_commands_file(raw)
            assert result == [], f"Expected empty for name {bad_name!r}, got {result}"

    def test_valid_name_slug_with_dashes_and_underscores(self):
        """Names with dashes and underscores are valid."""
        raw = (
            "commands:\n"
            "  - name: my-command\n"
            "    command: echo test\n"
            "  - name: another_one\n"
            "    command: echo test2\n"
            "  - name: a1b2-c3_d4\n"
            "    command: echo test3\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 3

    def test_duplicate_names_first_wins(self):
        """Duplicate command names: first occurrence wins, subsequent are skipped."""
        raw = (
            "commands:\n"
            "  - name: check\n"
            "    command: echo first\n"
            "  - name: check\n"
            "    command: echo second\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1
        assert result[0].command == "echo first"

    def test_command_too_long_skips_entry(self):
        """Command string longer than 1024 chars is skipped."""
        long_cmd = "echo " + "x" * 1020
        assert len(long_cmd) > 1024
        raw = f"commands:\n  - name: long\n    command: {long_cmd!r}\n"
        result = CLICommandsService.parse_commands_file(raw)
        assert result == []

    def test_command_exactly_1024_chars_is_accepted(self):
        """Command at exactly 1024 chars is accepted."""
        cmd_1024 = "echo " + "x" * 1019
        assert len(cmd_1024) == 1024
        raw = f"commands:\n  - name: ok\n    command: {cmd_1024!r}\n"
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1

    def test_description_truncated_at_512_chars(self):
        """Description longer than 512 chars is silently truncated."""
        long_desc = "D" * 600
        raw = (
            f"commands:\n"
            f"  - name: check\n"
            f"    command: echo test\n"
            f"    description: {long_desc!r}\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1
        assert result[0].description is not None
        assert len(result[0].description) == 512

    def test_50_command_limit(self):
        """Parser stops at 50 commands; entries beyond 50 are discarded."""
        lines = ["commands:"]
        for i in range(60):
            lines.append(f"  - name: cmd{i:02d}\n    command: echo {i}")
        raw = "\n".join(lines)
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 50

    def test_unknown_keys_ignored(self):
        """Unknown per-entry keys (timeout, tags, confirm) are silently ignored."""
        raw = (
            "commands:\n"
            "  - name: check\n"
            "    command: echo test\n"
            "    timeout: 60\n"
            "    tags:\n"
            "      - monitoring\n"
            "    confirm: true\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1
        assert result[0].name == "check"

    def test_unknown_top_level_keys_ignored(self):
        """Unknown top-level keys are silently ignored."""
        raw = (
            "version: 2\n"
            "defaults:\n"
            "  timeout: 30\n"
            "commands:\n"
            "  - name: check\n"
            "    command: echo test\n"
        )
        result = CLICommandsService.parse_commands_file(raw)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# 2. Rate-limit helpers
# ---------------------------------------------------------------------------

class TestRateLimit:

    def test_not_rate_limited_initially(self):
        env_id = uuid.uuid4()
        assert CLICommandsService.is_rate_limited(env_id) is False

    def test_rate_limited_after_mark(self):
        env_id = uuid.uuid4()
        CLICommandsService._mark_rate_limit(env_id)
        assert CLICommandsService.is_rate_limited(env_id) is True

    def test_not_rate_limited_after_ttl_expires(self):
        env_id = uuid.uuid4()
        # Backdate the lock entry beyond the TTL
        from app.services.agents import cli_commands_service as _mod
        _mod._rate_limit_lock[env_id] = datetime.now(UTC) - timedelta(
            seconds=_mod.FORCE_REFRESH_TTL_SECONDS + 1
        )
        assert CLICommandsService.is_rate_limited(env_id) is False

    def test_still_rate_limited_within_ttl(self):
        env_id = uuid.uuid4()
        from app.services.agents import cli_commands_service as _mod
        _mod._rate_limit_lock[env_id] = datetime.now(UTC) - timedelta(seconds=5)
        assert CLICommandsService.is_rate_limited(env_id) is True


# ---------------------------------------------------------------------------
# 3. get_cached_commands — reads from DB row without adapter call
# ---------------------------------------------------------------------------

class TestGetCachedCommands:

    def test_returns_empty_list_when_parsed_is_none(self):
        env = MagicMock()
        env.cli_commands_parsed = None
        result = CLICommandsService.get_cached_commands(env)
        assert result == []

    def test_returns_empty_list_when_parsed_is_empty_list(self):
        env = MagicMock()
        env.cli_commands_parsed = []
        result = CLICommandsService.get_cached_commands(env)
        assert result == []

    def test_deserializes_parsed_commands(self):
        env = MagicMock()
        env.cli_commands_parsed = [
            {"name": "check", "command": "uv run /app/workspace/check.py", "description": "Monthly check"},
            {"name": "report", "command": "uv run /app/workspace/report.py", "description": None},
        ]
        result = CLICommandsService.get_cached_commands(env)
        assert len(result) == 2
        assert result[0].name == "check"
        assert result[0].command == "uv run /app/workspace/check.py"
        assert result[0].description == "Monthly check"
        assert result[1].name == "report"
        assert result[1].description is None

    def test_skips_non_dict_entries(self):
        env = MagicMock()
        env.cli_commands_parsed = [
            "not-a-dict",
            {"name": "good", "command": "echo test", "description": None},
        ]
        result = CLICommandsService.get_cached_commands(env)
        assert len(result) == 1
        assert result[0].name == "good"

    def test_skips_entries_missing_name_or_command(self):
        env = MagicMock()
        env.cli_commands_parsed = [
            {"command": "echo test", "description": None},  # missing name
            {"name": "bad"},  # missing command
            {"name": "good", "command": "echo ok", "description": None},
        ]
        result = CLICommandsService.get_cached_commands(env)
        assert len(result) == 1
        assert result[0].name == "good"


# ---------------------------------------------------------------------------
# 4. refresh_after_action — post-action best-effort pull
# ---------------------------------------------------------------------------

class TestRefreshAfterAction:

    def test_skipped_when_rate_limited(self):
        env = MagicMock()
        env.id = uuid.uuid4()
        CLICommandsService._mark_rate_limit(env.id)

        with patch.object(CLICommandsService, "fetch_commands") as mock_fetch:
            asyncio.run(CLICommandsService.refresh_after_action(env))

        mock_fetch.assert_not_called()

    def test_force_bypasses_rate_limit(self):
        """force=True fetches even when the rate-limit window is active.

        Used when WORKSPACE_FILES_CHANGED explicitly reports docs/CLI_COMMANDS.yaml
        in changed_files — we have direct evidence the cache is stale.
        """
        env = MagicMock()
        env.id = uuid.uuid4()
        CLICommandsService._mark_rate_limit(env.id)  # within rate-limit window

        async def _fake_fetch(environment, db_session=None):
            return []

        with patch.object(
            CLICommandsService, "fetch_commands", side_effect=_fake_fetch
        ) as mock_fetch:
            asyncio.run(CLICommandsService.refresh_after_action(env, force=True))

        mock_fetch.assert_called_once()

    def test_calls_fetch_when_not_rate_limited(self):
        env = MagicMock()
        env.id = uuid.uuid4()
        from app.services.agents import cli_commands_service as _mod
        _mod._rate_limit_lock.pop(env.id, None)

        async def _fake_fetch(environment, db_session=None):
            return []

        with patch.object(
            CLICommandsService, "fetch_commands", side_effect=_fake_fetch
        ) as mock_fetch:
            asyncio.run(CLICommandsService.refresh_after_action(env))

        mock_fetch.assert_called_once()

    def test_swallows_cli_commands_unavailable_error(self):
        """CLICommandsUnavailableError (env stopped, file missing) is silently swallowed."""
        env = MagicMock()
        env.id = uuid.uuid4()
        from app.services.agents import cli_commands_service as _mod
        _mod._rate_limit_lock.pop(env.id, None)

        async def _raise_unavailable(environment, db_session=None):
            raise CLICommandsUnavailableError("file_missing")

        with patch.object(
            CLICommandsService, "fetch_commands", side_effect=_raise_unavailable
        ):
            # Must not raise
            asyncio.run(CLICommandsService.refresh_after_action(env))

    def test_swallows_unexpected_exceptions(self):
        """Any unexpected exception in fetch_commands is swallowed."""
        env = MagicMock()
        env.id = uuid.uuid4()
        from app.services.agents import cli_commands_service as _mod
        _mod._rate_limit_lock.pop(env.id, None)

        async def _raise_runtime(environment, db_session=None):
            raise RuntimeError("unexpected")

        with patch.object(
            CLICommandsService, "fetch_commands", side_effect=_raise_runtime
        ):
            # Must not raise
            asyncio.run(CLICommandsService.refresh_after_action(env))


# ---------------------------------------------------------------------------
# 5. handle_post_action_event — event handler guards and delegation
# ---------------------------------------------------------------------------

class TestHandlePostActionEvent:

    def test_no_environment_id_returns_cleanly(self):
        """Handler ignores events that lack environment_id in meta."""
        with patch.object(CLICommandsService, "refresh_after_action") as mock_refresh:
            asyncio.run(
                CLICommandsService.handle_post_action_event({"meta": {"agent_id": "x"}})
            )
        mock_refresh.assert_not_called()

    def test_missing_meta_returns_cleanly(self):
        """Handler ignores events that have no meta key."""
        with patch.object(CLICommandsService, "refresh_after_action") as mock_refresh:
            asyncio.run(
                CLICommandsService.handle_post_action_event({})
            )
        mock_refresh.assert_not_called()

    def test_unknown_env_id_returns_cleanly(self):
        """Handler no-ops when session.get returns None (env not in DB)."""
        bogus_env_id = str(uuid.uuid4())

        mock_session = MagicMock()
        mock_session.get.return_value = None

        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_session)
        mock_cm.__exit__ = MagicMock(return_value=False)

        with patch(
            "app.core.db.create_session", return_value=mock_cm
        ), patch.object(CLICommandsService, "refresh_after_action") as mock_refresh:
            asyncio.run(
                CLICommandsService.handle_post_action_event(
                    {"meta": {"environment_id": bogus_env_id}}
                )
            )

        mock_refresh.assert_not_called()

    def test_valid_event_resolves_env_and_refreshes(self):
        """Positive path: valid environment_id leads to refresh_after_action call."""
        env_id = uuid.uuid4()
        env_id_str = str(env_id)

        mock_env = MagicMock()
        mock_env.id = env_id

        mock_session = MagicMock()
        mock_session.get.return_value = mock_env
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_session)
        mock_cm.__exit__ = MagicMock(return_value=False)

        async def _noop(environment, db_session=None, force=False):
            return None

        with patch(
            "app.core.db.create_session", return_value=mock_cm
        ), patch.object(
            CLICommandsService, "refresh_after_action", side_effect=_noop
        ) as mock_refresh:
            asyncio.run(
                CLICommandsService.handle_post_action_event(
                    {"meta": {"environment_id": env_id_str}}
                )
            )

        mock_refresh.assert_called_once()
        env_arg = mock_refresh.call_args.args[0]
        assert env_arg.id == env_id
        # No changed_files in the event → speculative pull, rate-limit applies
        assert mock_refresh.call_args.kwargs.get("force") is False

    def test_workspace_files_changed_with_cli_file_forces_refresh(self):
        """WORKSPACE_FILES_CHANGED with docs/CLI_COMMANDS.yaml in changed_files bypasses rate limit."""
        env_id = uuid.uuid4()
        env_id_str = str(env_id)

        mock_env = MagicMock()
        mock_env.id = env_id

        mock_session = MagicMock()
        mock_session.get.return_value = mock_env
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_session)
        mock_cm.__exit__ = MagicMock(return_value=False)

        async def _noop(environment, db_session=None, force=False):
            return None

        with patch(
            "app.core.db.create_session", return_value=mock_cm
        ), patch.object(
            CLICommandsService, "refresh_after_action", side_effect=_noop
        ) as mock_refresh:
            asyncio.run(
                CLICommandsService.handle_post_action_event(
                    {
                        "meta": {
                            "environment_id": env_id_str,
                            "changed_files": [
                                "docs/CLI_COMMANDS.yaml",
                                "docs/WORKFLOW_PROMPT.md",
                            ],
                        }
                    }
                )
            )

        mock_refresh.assert_called_once()
        assert mock_refresh.call_args.kwargs.get("force") is True

    def test_workspace_files_changed_without_cli_file_does_not_force(self):
        """WORKSPACE_FILES_CHANGED naming only other files keeps the speculative (rate-limited) path."""
        env_id = uuid.uuid4()
        env_id_str = str(env_id)

        mock_env = MagicMock()
        mock_env.id = env_id

        mock_session = MagicMock()
        mock_session.get.return_value = mock_env
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=mock_session)
        mock_cm.__exit__ = MagicMock(return_value=False)

        async def _noop(environment, db_session=None, force=False):
            return None

        with patch(
            "app.core.db.create_session", return_value=mock_cm
        ), patch.object(
            CLICommandsService, "refresh_after_action", side_effect=_noop
        ) as mock_refresh:
            asyncio.run(
                CLICommandsService.handle_post_action_event(
                    {
                        "meta": {
                            "environment_id": env_id_str,
                            "changed_files": ["docs/WORKFLOW_PROMPT.md"],
                        }
                    }
                )
            )

        mock_refresh.assert_called_once()
        assert mock_refresh.call_args.kwargs.get("force") is False
