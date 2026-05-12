"""
Phase 5 — Agent name in router classification context.

Light unit-level tests that verify:
  1. AppMCPRoutingService._ai_classify passes an ``agent_name`` key in
     every element of the ``available_agents`` list it sends to
     AIFunctionsService.route_to_agent.

  2. The ``prompt_examples`` field is included (empty string when None
     on the route).

No DB, no Docker, no LLM calls — mocks only.
"""
import uuid
from unittest.mock import MagicMock, patch
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Test 1 + 2: _ai_classify sends agent_name + prompt_examples in payload
# ---------------------------------------------------------------------------


def test_ai_classify_passes_agent_name_in_payload() -> None:
    """
    AppMCPRoutingService._ai_classify must include ``name`` in every
    element of the ``available_agents`` list forwarded to
    AIFunctionsService.route_to_agent.

    This is the Phase 5 regression guard: adding ``agent_name`` to the
    classification context so the LLM can disambiguate e.g. "Calendar
    Planner" vs "Vacation Planner" with identical trigger words.
    """
    from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService
    from app.services.app_mcp.app_agent_route_service import EffectiveRoute

    agent_uuid = uuid.uuid4()
    route = EffectiveRoute(
        route_id=uuid.uuid4(),
        agent_id=agent_uuid,
        agent_name="Calendar Planner",
        session_mode="conversation",
        trigger_prompt="Schedule meetings and manage calendar events",
        message_patterns=None,
        prompt_examples="book a meeting\nschedule event",
        source="user",
        identity_owner_id=None,
        identity_owner_name=None,
    )

    captured_agents: list[dict] = []

    def _mock_route_to_agent(message: str, available_agents: list[dict]):
        captured_agents.extend(available_agents)
        return None  # no routing result — caller handles None

    with patch(
        "app.services.ai_functions.ai_functions_service.AIFunctionsService.route_to_agent",
        side_effect=_mock_route_to_agent,
    ):
        AppMCPRoutingService._ai_classify(
            message="Can you schedule a meeting for tomorrow?",
            routes=[route],
        )

    assert len(captured_agents) == 1, (
        f"Expected 1 agent dict, got {len(captured_agents)}"
    )
    agent_dict = captured_agents[0]

    # Phase 5: agent_name must be present
    assert "name" in agent_dict, (
        f"Phase 5 regression: 'name' key missing from available_agents payload. "
        f"Got keys: {list(agent_dict.keys())}"
    )
    assert agent_dict["name"] == "Calendar Planner"

    # trigger_prompt must be present
    assert "trigger_prompt" in agent_dict
    assert agent_dict["trigger_prompt"] == "Schedule meetings and manage calendar events"

    # agent id must be present
    assert agent_dict["id"] == str(agent_uuid)

    # prompt_examples carried through
    assert "prompt_examples" in agent_dict
    assert "book a meeting" in agent_dict["prompt_examples"]


def test_ai_classify_passes_empty_string_for_none_prompt_examples() -> None:
    """
    When a route has no prompt_examples (None), the ``available_agents``
    payload should carry an empty string rather than None, so the LLM
    prompt template doesn't break on None concatenation.
    """
    from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService
    from app.services.app_mcp.app_agent_route_service import EffectiveRoute

    route = EffectiveRoute(
        route_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        agent_name="Invoice Bot",
        session_mode="conversation",
        trigger_prompt="Process invoices",
        message_patterns=None,
        prompt_examples=None,  # deliberately None
        source="user",
        identity_owner_id=None,
        identity_owner_name=None,
    )

    captured_agents: list[dict] = []

    def _capture(message, available_agents):
        captured_agents.extend(available_agents)
        return None

    with patch(
        "app.services.ai_functions.ai_functions_service.AIFunctionsService.route_to_agent",
        side_effect=_capture,
    ):
        AppMCPRoutingService._ai_classify(
            message="process this invoice",
            routes=[route],
        )

    assert len(captured_agents) == 1
    agent_dict = captured_agents[0]
    # prompt_examples should be an empty string (or at least not None)
    prompt_ex = agent_dict.get("prompt_examples")
    assert prompt_ex is not None, "prompt_examples must not be None in the payload"
    assert isinstance(prompt_ex, str)
