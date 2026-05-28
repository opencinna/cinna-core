"""
Unit tests for the cinna.mcp descriptor builder and slug helpers — pure Python,
no HTTP, no database.

Covers:
  1. build_cinna_mcp_descriptor: contract shape (version, tool_name, display_name,
     description, input_schema, capabilities, example_prompts, run_commands)
  2. input_schema omits context_id (desktop is stateful) — only {message}
  3. run_commands capability + array driven by environment.cli_commands_parsed
  4. router_trigger_prompt is folded into the description; description override
     and display_name override are honored
  5. capabilities.files is always True (universal upload), resources always False
  6. The descriptor reuses the canonical send_message input schema (shared
     contract module) — cannot drift from the MCP connector tool
  7. slugify_tool_name: lowercasing, non-alnum collapse, trimming, length cap,
     empty fallback
  8. deconflict_tool_name: deterministic discriminator from a stable id
  9. build_agent_card attaches the urn:cinna:mcp extension alongside urn:cinna:sdk:
"""
from unittest.mock import MagicMock

from app.mcp.tool_contracts import build_send_message_input_schema
from app.services.a2a.a2a_service import (
    A2AService,
    CINNA_MCP_DESCRIPTOR_VERSION,
    CINNA_MCP_EXTENSION_URI,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_agent(
    *,
    name="Email Agent",
    description="Handles email tasks.",
    router_trigger_prompt="Call me to send or summarize emails.",
    example_prompts=None,
    agent_id="00000000-0000-0000-0000-000000000001",
):
    """Build a minimal mock Agent with concrete (non-Mock) attribute values."""
    agent = MagicMock()
    agent.name = name
    agent.description = description
    agent.router_trigger_prompt = router_trigger_prompt
    agent.example_prompts = example_prompts if example_prompts is not None else []
    agent.id = agent_id
    agent.a2a_config = {"enabled": True, "version": "1.0.0", "skills": []}
    return agent


def _make_environment(cli_commands_parsed):
    env = MagicMock()
    env.cli_commands_parsed = cli_commands_parsed
    env.agent_sdk_conversation = "claude-code/anthropic"
    env.agent_sdk_building = None
    return env


# ---------------------------------------------------------------------------
# 1–6: build_cinna_mcp_descriptor
# ---------------------------------------------------------------------------


class TestBuildCinnaMcpDescriptor:

    def test_contract_shape_with_cli_commands(self):
        agent = _make_agent(example_prompts=["generate report", "summarize Q3"])
        env = _make_environment([
            {"name": "deploy", "command": "make deploy", "description": "Deploy the app"},
            {"name": "check", "command": "make check", "description": None},
        ])

        d = A2AService.build_cinna_mcp_descriptor(agent, env, tool_name="email_agent")

        assert d["version"] == CINNA_MCP_DESCRIPTOR_VERSION
        assert d["tool_name"] == "email_agent"
        assert d["display_name"] == "Email Agent"
        assert d["example_prompts"] == ["generate report", "summarize Q3"]

        # capabilities
        assert d["capabilities"]["files"] is True
        assert d["capabilities"]["resources"] is False
        assert d["capabilities"]["run_commands"] is True

        # run_commands array
        names = [c["name"] for c in d["run_commands"]]
        assert names == ["deploy", "check"]
        deploy = next(c for c in d["run_commands"] if c["name"] == "deploy")
        assert deploy["invocation"] == "/run:deploy"
        assert deploy["description"] == "Deploy the app"
        # A command with no description omits the key entirely (never null) —
        # the descriptor rides in AgentExtension.params, which exclude_none does
        # not recurse into.
        check = next(c for c in d["run_commands"] if c["name"] == "check")
        assert "description" not in check

    def test_input_schema_omits_context_id(self):
        agent = _make_agent()
        d = A2AService.build_cinna_mcp_descriptor(agent, None, tool_name="x")

        props = d["input_schema"]["properties"]
        assert "message" in props
        assert "context_id" not in props, (
            "Desktop descriptor must omit context_id — the desktop is stateful"
        )
        assert d["input_schema"]["required"] == ["message"]

    def test_input_schema_matches_canonical_contract(self):
        """The descriptor input schema is the shared canonical schema (no drift)."""
        agent = _make_agent()
        d = A2AService.build_cinna_mcp_descriptor(agent, None, tool_name="x")
        assert d["input_schema"] == build_send_message_input_schema(
            include_context_id=False
        )

    def test_no_cli_commands_yields_false_capability(self):
        agent = _make_agent()
        for env in (None, _make_environment([]), _make_environment(None)):
            d = A2AService.build_cinna_mcp_descriptor(agent, env, tool_name="x")
            assert d["run_commands"] == []
            assert d["capabilities"]["run_commands"] is False

    def test_router_trigger_prompt_folded_into_description(self):
        agent = _make_agent(
            router_trigger_prompt="Use me for invoicing questions.",
            description="A different description.",
        )
        d = A2AService.build_cinna_mcp_descriptor(agent, None, tool_name="x")
        # router_trigger_prompt is the routing signal — it must be folded in,
        # taking precedence over agent.description.
        assert "Use me for invoicing questions." in d["description"]
        assert "A different description." not in d["description"]

    def test_description_falls_back_to_agent_description(self):
        agent = _make_agent(router_trigger_prompt=None, description="Fallback desc.")
        d = A2AService.build_cinna_mcp_descriptor(agent, None, tool_name="x")
        assert "Fallback desc." in d["description"]

    def test_description_without_any_summary(self):
        agent = _make_agent(router_trigger_prompt=None, description=None)
        d = A2AService.build_cinna_mcp_descriptor(agent, None, tool_name="x")
        # Still a non-empty base description from the shared contract.
        assert d["description"].strip() != ""

    def test_display_name_and_description_overrides(self):
        agent = _make_agent()
        d = A2AService.build_cinna_mcp_descriptor(
            agent,
            None,
            tool_name="route_tool",
            display_name="Shared Route Name",
            description="Route trigger prompt.",
        )
        assert d["display_name"] == "Shared Route Name"
        assert d["description"] == "Route trigger prompt."

    def test_empty_example_prompts(self):
        agent = _make_agent(example_prompts=[])
        d = A2AService.build_cinna_mcp_descriptor(agent, None, tool_name="x")
        assert d["example_prompts"] == []


# ---------------------------------------------------------------------------
# 7–8: slug helpers
# ---------------------------------------------------------------------------


class TestSlugHelpers:

    def test_slugify_basic(self):
        assert A2AService.slugify_tool_name("Email Agent") == "email_agent"

    def test_slugify_collapses_non_alnum(self):
        assert A2AService.slugify_tool_name("Email  Agent!! v2") == "email_agent_v2"

    def test_slugify_trims_edges(self):
        assert A2AService.slugify_tool_name("  --Hello--  ") == "hello"

    def test_slugify_empty_fallback(self):
        assert A2AService.slugify_tool_name("") == "agent"
        assert A2AService.slugify_tool_name(None) == "agent"
        assert A2AService.slugify_tool_name("!!!") == "agent"

    def test_slugify_length_cap(self):
        long_name = "a" * 200
        slug = A2AService.slugify_tool_name(long_name)
        assert len(slug) <= A2AService._TOOL_NAME_MAX_LENGTH

    def test_deconflict_is_deterministic(self):
        agent_id = "12345678-aaaa-bbbb-cccc-1234567890ab"
        a = A2AService.deconflict_tool_name("email_agent", agent_id)
        b = A2AService.deconflict_tool_name("email_agent", agent_id)
        assert a == b == "email_agent_12345678"

    def test_deconflict_differs_per_id(self):
        a = A2AService.deconflict_tool_name("agent", "11111111-0000-0000-0000-000000000000")
        b = A2AService.deconflict_tool_name("agent", "22222222-0000-0000-0000-000000000000")
        assert a != b

    def test_deconflict_respects_length_cap(self):
        long_base = "x" * 200
        out = A2AService.deconflict_tool_name(long_base, "abcdef12-0000-0000-0000-000000000000")
        assert len(out) <= A2AService._TOOL_NAME_MAX_LENGTH
        assert out.endswith("_abcdef12")


# ---------------------------------------------------------------------------
# 9: build_agent_card attaches the extension
# ---------------------------------------------------------------------------


class TestCardExtensionAttachment:

    def test_card_has_cinna_mcp_extension(self):
        agent = _make_agent()
        env = _make_environment([
            {"name": "deploy", "command": "make deploy", "description": "Deploy"},
        ])
        card = A2AService.build_agent_card(agent, env, "https://example.com")

        uris = [e.uri for e in (card.capabilities.extensions or [])]
        assert CINNA_MCP_EXTENSION_URI in uris
        # SDK extension still present (regression guard)
        assert any(u.startswith("urn:cinna:sdk:") for u in uris)

        mcp_ext = next(
            e for e in card.capabilities.extensions if e.uri == CINNA_MCP_EXTENSION_URI
        )
        assert mcp_ext.required is False
        assert mcp_ext.params["version"] == CINNA_MCP_DESCRIPTOR_VERSION
        assert mcp_ext.params["tool_name"]  # non-empty default slug

    def test_card_extension_uses_explicit_tool_name(self):
        agent = _make_agent()
        card = A2AService.build_agent_card(
            agent, None, "https://example.com", mcp_tool_name="explicit_slug"
        )
        mcp_ext = next(
            e for e in card.capabilities.extensions if e.uri == CINNA_MCP_EXTENSION_URI
        )
        assert mcp_ext.params["tool_name"] == "explicit_slug"

    def test_card_extension_survives_v1_adapter(self):
        """The urn:cinna:mcp extension is preserved by the v1.0 outbound adapter."""
        agent = _make_agent()
        env = _make_environment([])
        for protocol in ("v0.3", "v1.0"):
            card_dict = A2AService.get_agent_card_dict(
                agent, env, "https://example.com", protocol=protocol
            )
            exts = card_dict["capabilities"].get("extensions", [])
            uris = [e["uri"] for e in exts]
            assert CINNA_MCP_EXTENSION_URI in uris, (
                f"urn:cinna:mcp missing for protocol={protocol}"
            )
