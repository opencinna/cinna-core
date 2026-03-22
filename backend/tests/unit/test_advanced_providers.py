"""
Advanced-provider and cross-SDK tests.

Tests cover:
1. OpenCode adapter SUPPORTED_PROVIDERS list
2. Cross-SDK scenario: Claude Code (building) + OpenCode (conversation)
   — config generation produces per-mode directories with correct configs
3. OpenCode config structure validation (tools, MCP, permissions)

All filesystem writes use tmp_path fixtures so no real environment directories
are required. No Docker, no real API keys needed.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BACKEND_DIR = Path(__file__).parents[2]
_ADAPTER_DIR = (
    _BACKEND_DIR
    / "app"
    / "env-templates"
    / "app_core_base"
    / "core"
    / "server"
    / "adapters"
)


def _make_environment(
    sdk_building="claude-code/anthropic",
    sdk_conversation="opencode/openai",
    model_override_building=None,
    model_override_conversation=None,
    conversation_cred_id=None,
    building_cred_id=None,
):
    """Build a minimal mock AgentEnvironment suitable for lifecycle tests."""
    env = MagicMock()
    env.id = "test-env-id"
    env.agent_sdk_building = sdk_building
    env.agent_sdk_conversation = sdk_conversation
    env.model_override_building = model_override_building
    env.model_override_conversation = model_override_conversation
    env.conversation_ai_credential_id = conversation_cred_id
    env.building_ai_credential_id = building_cred_id
    return env


# ---------------------------------------------------------------------------
# 1. OpenCode adapter SUPPORTED_PROVIDERS
# ---------------------------------------------------------------------------

class TestOpenCodeAdapterSupportedProviders:
    """Verify the adapter's SUPPORTED_PROVIDERS list."""

    def test_core_providers_present(self):
        """SUPPORTED_PROVIDERS includes the currently implemented providers."""
        src = (_ADAPTER_DIR / "opencode_sdk_adapter.py").read_text()
        start_idx = src.find("SUPPORTED_PROVIDERS")
        if start_idx == -1:
            pytest.fail("SUPPORTED_PROVIDERS not found in opencode_sdk_adapter.py")
        block_end = src.find("]", start_idx)
        block = src[start_idx:block_end + 1]

        for provider in ("anthropic", "openai", "openai_compatible", "google"):
            assert provider in block, f"'{provider}' not in SUPPORTED_PROVIDERS"


# ---------------------------------------------------------------------------
# 2. Cross-SDK: Claude Code (building) + OpenCode (conversation)
# ---------------------------------------------------------------------------

class TestCrossSDKConfigGeneration:
    """
    Config generation for cross-SDK setup:
    - Building: Claude Code (no opencode config generated)
    - Conversation: OpenCode + OpenAI
    """

    def test_conversation_config_generated(self, tmp_path):
        """Conversation opencode.json is generated for opencode/openai."""
        from app.services.environment_lifecycle import EnvironmentLifecycleManager

        mgr = EnvironmentLifecycleManager()
        env = _make_environment(
            sdk_building="claude-code/anthropic",
            sdk_conversation="opencode/openai",
            model_override_conversation="openai/gpt-4o-mini",
        )

        mgr._generate_opencode_config_files(
            tmp_path,
            env,
            openai_api_key="sk-openai-test",
        )

        conv_config_path = tmp_path / "app" / "core" / ".opencode" / "conversation" / "opencode.json"
        assert conv_config_path.exists(), "conversation/opencode.json must be created"

        config = json.loads(conv_config_path.read_text())
        assert config["model"] == "openai/gpt-4o-mini"

    def test_no_building_config_for_claude_code(self, tmp_path):
        """Building config is NOT generated when building SDK is claude-code."""
        from app.services.environment_lifecycle import EnvironmentLifecycleManager

        mgr = EnvironmentLifecycleManager()
        env = _make_environment(
            sdk_building="claude-code/anthropic",
            sdk_conversation="opencode/openai",
        )

        mgr._generate_opencode_config_files(
            tmp_path,
            env,
            openai_api_key="sk-openai-test",
        )

        building_config_path = tmp_path / "app" / "core" / ".opencode" / "building" / "opencode.json"
        assert not building_config_path.exists(), (
            "building/opencode.json must NOT be created when SDK is claude-code"
        )

    def test_both_modes_generated_for_opencode(self, tmp_path):
        """Both building and conversation configs generated when both use opencode."""
        from app.services.environment_lifecycle import EnvironmentLifecycleManager

        mgr = EnvironmentLifecycleManager()
        env = _make_environment(
            sdk_building="opencode/anthropic",
            sdk_conversation="opencode/openai",
        )

        mgr._generate_opencode_config_files(
            tmp_path,
            env,
            anthropic_api_key="sk-ant-test",
            openai_api_key="sk-openai-test",
        )

        opencode_dir = tmp_path / "app" / "core" / ".opencode"
        assert (opencode_dir / "building" / "opencode.json").exists()
        assert (opencode_dir / "conversation" / "opencode.json").exists()


# ---------------------------------------------------------------------------
# 3. OpenCode config structure validation
# ---------------------------------------------------------------------------

class TestOpenCodeConfigStructure:
    """Verify structure of generated opencode.json config files."""

    def test_config_has_tools_section(self, tmp_path):
        """Generated config enables built-in tools."""
        from app.services.environment_lifecycle import EnvironmentLifecycleManager

        mgr = EnvironmentLifecycleManager()
        env = _make_environment(
            sdk_building="opencode/anthropic",
            sdk_conversation="opencode/anthropic",
        )

        mgr._generate_opencode_config_files(
            tmp_path, env, anthropic_api_key="sk-ant-test",
        )

        config = json.loads(
            (tmp_path / "app" / "core" / ".opencode" / "conversation" / "opencode.json").read_text()
        )
        tools = config.get("tools", {})
        for tool in ("bash", "read", "write", "edit", "glob", "grep"):
            assert tools.get(tool) is True, f"Tool '{tool}' should be enabled"

    def test_config_has_mcp_bridges(self, tmp_path):
        """Generated config includes MCP bridge servers."""
        from app.services.environment_lifecycle import EnvironmentLifecycleManager

        mgr = EnvironmentLifecycleManager()
        env = _make_environment(
            sdk_building="opencode/anthropic",
            sdk_conversation="opencode/anthropic",
        )

        mgr._generate_opencode_config_files(
            tmp_path, env, anthropic_api_key="sk-ant-test",
        )

        config = json.loads(
            (tmp_path / "app" / "core" / ".opencode" / "conversation" / "opencode.json").read_text()
        )
        mcp = config.get("mcp", {})
        assert "knowledge" in mcp, "knowledge MCP bridge missing"
        assert "task" in mcp, "task MCP bridge missing"
        assert "collaboration" in mcp, "collaboration MCP bridge missing"

    def test_config_has_server_ports(self, tmp_path):
        """Building and conversation configs use different ports."""
        from app.services.environment_lifecycle import EnvironmentLifecycleManager

        mgr = EnvironmentLifecycleManager()
        env = _make_environment(
            sdk_building="opencode/anthropic",
            sdk_conversation="opencode/anthropic",
        )

        mgr._generate_opencode_config_files(
            tmp_path, env, anthropic_api_key="sk-ant-test",
        )

        opencode_dir = tmp_path / "app" / "core" / ".opencode"
        building_config = json.loads((opencode_dir / "building" / "opencode.json").read_text())
        conv_config = json.loads((opencode_dir / "conversation" / "opencode.json").read_text())

        assert building_config["server"]["port"] == 4096
        assert conv_config["server"]["port"] == 4097

    def test_config_file_permissions(self, tmp_path):
        """Config files have restricted permissions (0600)."""
        from app.services.environment_lifecycle import EnvironmentLifecycleManager
        import stat

        mgr = EnvironmentLifecycleManager()
        env = _make_environment(
            sdk_building="opencode/anthropic",
            sdk_conversation="opencode/anthropic",
        )

        mgr._generate_opencode_config_files(
            tmp_path, env, anthropic_api_key="sk-ant-test",
        )

        config_path = tmp_path / "app" / "core" / ".opencode" / "conversation" / "opencode.json"
        mode = config_path.stat().st_mode & 0o777
        assert mode == 0o600, f"Expected 0600 permissions, got {oct(mode)}"
