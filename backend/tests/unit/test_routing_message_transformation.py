"""
Unit tests for routing message transformation.

Tests cover:
1. RouteToAgentResult dataclass construction
2. route_to_agent() JSON parsing, transformed_message extraction, and validation rules
3. AppMCPRoutingService._ai_classify() tuple return propagation
4. IdentityRoutingService._ai_classify() tuple return propagation
5. Cascade logic: Stage 1 transforms, Stage 2 transforms, both, neither
6. Sanity guards: empty, equals-original, exceeds-2x-length

All tests use mocks — no DB, no Docker, no LLM calls.
"""

import json
import uuid
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_provider_response(text: str) -> MagicMock:
    """Minimal stub for the LLM provider response."""
    resp = MagicMock()
    resp.text = text
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# 1. RouteToAgentResult dataclass
# ──────────────────────────────────────────────────────────────────────────────

class TestRouteToAgentResult:
    def test_required_field(self):
        from app.agents.app_agent_router import RouteToAgentResult
        r = RouteToAgentResult(agent_id="some-uuid")
        assert r.agent_id == "some-uuid"
        assert r.transformed_message is None

    def test_with_transformed_message(self):
        from app.agents.app_agent_router import RouteToAgentResult
        r = RouteToAgentResult(agent_id="some-uuid", transformed_message="do the thing")
        assert r.transformed_message == "do the thing"


# ──────────────────────────────────────────────────────────────────────────────
# 2. route_to_agent() — JSON parsing and validation
# ──────────────────────────────────────────────────────────────────────────────

AGENT_UUID = str(uuid.uuid4())
AGENTS = [{"id": AGENT_UUID, "name": "HR Bot", "trigger_prompt": "HR tasks"}]


def _call_route_to_agent(llm_response_text: str, message: str = "ask cinna to do the thing"):
    """Call route_to_agent() with a mocked LLM response."""
    from app.agents.app_agent_router import route_to_agent

    with patch("app.agents.app_agent_router.get_provider_manager") as mock_pm:
        mock_pm.return_value.generate_content.return_value = _make_provider_response(llm_response_text)
        return route_to_agent(message=message, available_agents=AGENTS)


class TestRouteToAgentJsonParsing:
    def test_valid_json_with_message_returns_result(self):
        payload = json.dumps({"agent_id": AGENT_UUID, "message": "do the thing"})
        result = _call_route_to_agent(payload)
        assert result is not None
        assert result.agent_id == AGENT_UUID
        assert result.transformed_message == "do the thing"

    def test_valid_json_null_message_returns_none_transformed(self):
        payload = json.dumps({"agent_id": AGENT_UUID, "message": None})
        result = _call_route_to_agent(payload)
        assert result is not None
        assert result.agent_id == AGENT_UUID
        assert result.transformed_message is None

    def test_valid_json_no_message_key_returns_none_transformed(self):
        payload = json.dumps({"agent_id": AGENT_UUID})
        result = _call_route_to_agent(payload)
        assert result is not None
        assert result.agent_id == AGENT_UUID
        assert result.transformed_message is None

    def test_agent_none_returns_none(self):
        payload = json.dumps({"agent_id": "NONE"})
        result = _call_route_to_agent(payload)
        assert result is None

    def test_malformed_json_returns_none(self):
        result = _call_route_to_agent("not json at all")
        assert result is None

    def test_empty_response_returns_none(self):
        result = _call_route_to_agent("")
        assert result is None

    def test_markdown_code_fence_stripped(self):
        raw = "```json\n" + json.dumps({"agent_id": AGENT_UUID, "message": "do the thing"}) + "\n```"
        result = _call_route_to_agent(raw)
        assert result is not None
        assert result.transformed_message == "do the thing"

    def test_invalid_uuid_format_returns_none(self):
        payload = json.dumps({"agent_id": "not-a-uuid"})
        result = _call_route_to_agent(payload)
        assert result is None


class TestRouteToAgentTransformedMessageValidation:
    """Sanity guards for the transformed_message field."""

    def test_empty_string_transformed_message_discarded(self):
        payload = json.dumps({"agent_id": AGENT_UUID, "message": ""})
        result = _call_route_to_agent(payload)
        assert result is not None
        assert result.transformed_message is None

    def test_whitespace_only_transformed_message_discarded(self):
        payload = json.dumps({"agent_id": AGENT_UUID, "message": "   "})
        result = _call_route_to_agent(payload, message="do something useful")
        assert result is not None
        assert result.transformed_message is None

    def test_transformed_message_equal_to_original_discarded(self):
        original = "ask cinna to do the thing"
        payload = json.dumps({"agent_id": AGENT_UUID, "message": original})
        result = _call_route_to_agent(payload, message=original)
        assert result is not None
        assert result.transformed_message is None

    def test_transformed_message_exceeding_2x_original_length_discarded(self):
        original = "short"
        # More than 2 * len("short") = 10 characters
        long_msg = "this is a much longer message that definitely exceeds two times the original"
        payload = json.dumps({"agent_id": AGENT_UUID, "message": long_msg})
        result = _call_route_to_agent(payload, message=original)
        assert result is not None
        assert result.transformed_message is None

    def test_transformed_message_exactly_2x_original_length_kept(self):
        original = "abcde"  # len 5
        exactly_2x = "a" * 10  # len 10, equal to 2x
        payload = json.dumps({"agent_id": AGENT_UUID, "message": exactly_2x})
        result = _call_route_to_agent(payload, message=original)
        assert result is not None
        assert result.transformed_message == exactly_2x

    def test_valid_stripped_message_kept(self):
        original = "ask cinna to generate employee report"
        stripped = "generate employee report"
        payload = json.dumps({"agent_id": AGENT_UUID, "message": stripped})
        result = _call_route_to_agent(payload, message=original)
        assert result is not None
        assert result.transformed_message == stripped

    def test_empty_agents_list_returns_none(self):
        from app.agents.app_agent_router import route_to_agent
        result = route_to_agent(message="anything", available_agents=[])
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# 3. RoutingResult transformed_message field
# ──────────────────────────────────────────────────────────────────────────────

class TestRoutingResultDataclass:
    def test_default_transformed_message_is_none(self):
        from app.services.app_mcp.app_mcp_routing_service import RoutingResult
        r = RoutingResult(
            agent_id=uuid.uuid4(),
            agent_name="Test",
            session_mode="conversation",
            route_id=uuid.uuid4(),
            route_source="user",
            match_method="ai",
        )
        assert r.transformed_message is None

    def test_can_set_transformed_message(self):
        from app.services.app_mcp.app_mcp_routing_service import RoutingResult
        r = RoutingResult(
            agent_id=uuid.uuid4(),
            agent_name="Test",
            session_mode="conversation",
            route_id=uuid.uuid4(),
            route_source="user",
            match_method="ai",
            transformed_message="stripped task",
        )
        assert r.transformed_message == "stripped task"


# ──────────────────────────────────────────────────────────────────────────────
# 4. IdentityRoutingResult transformed_message field
# ──────────────────────────────────────────────────────────────────────────────

class TestIdentityRoutingResultDataclass:
    def test_default_transformed_message_is_none(self):
        from app.services.identity.identity_routing_service import IdentityRoutingResult
        r = IdentityRoutingResult(
            agent_id=uuid.uuid4(),
            agent_name="Test",
            session_mode="conversation",
            binding_id=uuid.uuid4(),
            binding_assignment_id=uuid.uuid4(),
            match_method="ai",
        )
        assert r.transformed_message is None

    def test_can_set_transformed_message(self):
        from app.services.identity.identity_routing_service import IdentityRoutingResult
        r = IdentityRoutingResult(
            agent_id=uuid.uuid4(),
            agent_name="Test",
            session_mode="conversation",
            binding_id=uuid.uuid4(),
            binding_assignment_id=uuid.uuid4(),
            match_method="ai",
            transformed_message="core task",
        )
        assert r.transformed_message == "core task"


# ──────────────────────────────────────────────────────────────────────────────
# 5. AppMCPRoutingService._ai_classify() tuple return
# ──────────────────────────────────────────────────────────────────────────────

class TestAppMCPRoutingServiceAiClassify:
    def _make_route(self, agent_id: uuid.UUID, name: str = "Agent") -> MagicMock:
        route = MagicMock()
        route.agent_id = agent_id
        route.agent_name = name
        route.trigger_prompt = "some trigger"
        return route

    def test_returns_tuple_with_transformed_message(self):
        # Import the service module first so its lazy imports are resolved
        import app.services.app_mcp.app_mcp_routing_service  # noqa: F401
        from app.services.ai_functions.ai_functions_service import AIFunctionsService
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService
        from app.agents.app_agent_router import RouteToAgentResult

        agent_id = uuid.uuid4()
        route = self._make_route(agent_id)
        routing_result = RouteToAgentResult(
            agent_id=str(agent_id),
            transformed_message="core task",
        )

        with patch.object(AIFunctionsService, "route_to_agent", return_value=routing_result):
            result = AppMCPRoutingService._ai_classify("ask cinna to core task", [route])

        assert result is not None
        matched_route, transformed = result
        assert matched_route is route
        assert transformed == "core task"

    def test_returns_none_transformed_when_ai_returns_no_message(self):
        import app.services.app_mcp.app_mcp_routing_service  # noqa: F401
        from app.services.ai_functions.ai_functions_service import AIFunctionsService
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService
        from app.agents.app_agent_router import RouteToAgentResult

        agent_id = uuid.uuid4()
        route = self._make_route(agent_id)
        routing_result = RouteToAgentResult(
            agent_id=str(agent_id),
            transformed_message=None,
        )

        with patch.object(AIFunctionsService, "route_to_agent", return_value=routing_result):
            result = AppMCPRoutingService._ai_classify("direct task", [route])

        assert result is not None
        matched_route, transformed = result
        assert matched_route is route
        assert transformed is None

    def test_returns_none_when_ai_returns_no_match(self):
        import app.services.app_mcp.app_mcp_routing_service  # noqa: F401
        from app.services.ai_functions.ai_functions_service import AIFunctionsService
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        route = self._make_route(uuid.uuid4())

        with patch.object(AIFunctionsService, "route_to_agent", return_value=None):
            result = AppMCPRoutingService._ai_classify("unrelated message", [route])

        assert result is None

    def test_returns_none_when_agent_id_not_in_routes(self):
        import app.services.app_mcp.app_mcp_routing_service  # noqa: F401
        from app.services.ai_functions.ai_functions_service import AIFunctionsService
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService
        from app.agents.app_agent_router import RouteToAgentResult

        route = self._make_route(uuid.uuid4())
        # AI returns a different agent_id not in routes
        routing_result = RouteToAgentResult(agent_id=str(uuid.uuid4()))

        with patch.object(AIFunctionsService, "route_to_agent", return_value=routing_result):
            result = AppMCPRoutingService._ai_classify("message", [route])

        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# 6. IdentityRoutingService._ai_classify() tuple return
# ──────────────────────────────────────────────────────────────────────────────

class TestIdentityRoutingServiceAiClassify:
    def _make_binding(self, agent_id: uuid.UUID) -> MagicMock:
        binding = MagicMock()
        binding.agent_id = agent_id
        binding.trigger_prompt = "some trigger"
        return binding

    def test_returns_tuple_with_transformed_message(self):
        from app.services.identity.identity_routing_service import IdentityRoutingService
        from app.agents.app_agent_router import RouteToAgentResult

        agent_id = uuid.uuid4()
        binding = self._make_binding(agent_id)
        routing_result = RouteToAgentResult(
            agent_id=str(agent_id),
            transformed_message="final task",
        )
        mock_db = MagicMock()
        mock_agent = MagicMock()
        mock_agent.name = "Agent"
        mock_db.get.return_value = mock_agent

        # route_to_agent is imported inside the method body; patch at its source
        with patch(
            "app.agents.app_agent_router.route_to_agent",
            return_value=routing_result,
        ):
            result = IdentityRoutingService._ai_classify("ask john to final task", [binding], mock_db)

        assert result is not None
        matched_binding, transformed = result
        assert matched_binding is binding
        assert transformed == "final task"

    def test_returns_none_transformed_when_ai_returns_no_message(self):
        from app.services.identity.identity_routing_service import IdentityRoutingService
        from app.agents.app_agent_router import RouteToAgentResult

        agent_id = uuid.uuid4()
        binding = self._make_binding(agent_id)
        routing_result = RouteToAgentResult(agent_id=str(agent_id), transformed_message=None)
        mock_db = MagicMock()
        mock_agent = MagicMock()
        mock_agent.name = "Agent"
        mock_db.get.return_value = mock_agent

        with patch(
            "app.agents.app_agent_router.route_to_agent",
            return_value=routing_result,
        ):
            result = IdentityRoutingService._ai_classify("direct task", [binding], mock_db)

        assert result is not None
        matched_binding, transformed = result
        assert matched_binding is binding
        assert transformed is None

    def test_returns_none_when_ai_returns_no_match(self):
        from app.services.identity.identity_routing_service import IdentityRoutingService

        binding = self._make_binding(uuid.uuid4())
        mock_db = MagicMock()

        with patch(
            "app.agents.app_agent_router.route_to_agent",
            return_value=None,
        ):
            result = IdentityRoutingService._ai_classify("unrelated", [binding], mock_db)

        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# 7. _route_identity() cascade logic
# ──────────────────────────────────────────────────────────────────────────────

class TestRouteIdentityCascadeLogic:
    def _make_selected_route(self, owner_id: uuid.UUID) -> MagicMock:
        route = MagicMock()
        route.identity_owner_id = owner_id
        route.identity_owner_name = "John"
        route.agent_name = "John"
        route.route_id = uuid.uuid4()
        return route

    def _make_stage2_result(self, transformed: str | None = None) -> MagicMock:
        from app.services.identity.identity_routing_service import IdentityRoutingResult
        return IdentityRoutingResult(
            agent_id=uuid.uuid4(),
            agent_name="Agent",
            session_mode="conversation",
            binding_id=uuid.uuid4(),
            binding_assignment_id=uuid.uuid4(),
            match_method="ai",
            transformed_message=transformed,
        )

    def _patch_stage2(self, return_value):
        """Patch IdentityRoutingService.route_within_identity at its source module."""
        from app.services.identity import identity_routing_service
        return patch.object(
            identity_routing_service.IdentityRoutingService,
            "route_within_identity",
            return_value=return_value,
        )

    def test_stage2_transformed_takes_precedence(self):
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        owner_id = uuid.uuid4()
        selected_route = self._make_selected_route(owner_id)
        stage2_result = self._make_stage2_result("final task")

        with self._patch_stage2(stage2_result):
            result = AppMCPRoutingService._route_identity(
                db_session=MagicMock(),
                selected_route=selected_route,
                caller_user_id=uuid.uuid4(),
                message="ask cinna to ask john to final task",
                stage1_method="ai",
                transformed_message="ask john to final task",  # Stage 1 result
            )

        assert result is not None
        assert result.transformed_message == "final task"  # Stage 2 wins

    def test_falls_back_to_stage1_when_stage2_does_not_transform(self):
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        owner_id = uuid.uuid4()
        selected_route = self._make_selected_route(owner_id)
        stage2_result = self._make_stage2_result(None)  # Stage 2 didn't transform

        with self._patch_stage2(stage2_result):
            result = AppMCPRoutingService._route_identity(
                db_session=MagicMock(),
                selected_route=selected_route,
                caller_user_id=uuid.uuid4(),
                message="ask cinna to ask john to do thing",
                stage1_method="ai",
                transformed_message="ask john to do thing",  # Stage 1 result
            )

        assert result is not None
        assert result.transformed_message == "ask john to do thing"  # Stage 1 fallback

    def test_no_transformation_from_either_stage(self):
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        owner_id = uuid.uuid4()
        selected_route = self._make_selected_route(owner_id)
        stage2_result = self._make_stage2_result(None)

        with self._patch_stage2(stage2_result):
            result = AppMCPRoutingService._route_identity(
                db_session=MagicMock(),
                selected_route=selected_route,
                caller_user_id=uuid.uuid4(),
                message="direct task",
                stage1_method="ai",
                transformed_message=None,  # Stage 1 didn't transform
            )

        assert result is not None
        assert result.transformed_message is None

    def test_stage2_receives_stage1_transformed_message_as_input(self):
        """Stage 2 routing receives Stage 1's transformed message, not the original."""
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        owner_id = uuid.uuid4()
        selected_route = self._make_selected_route(owner_id)
        stage2_result = self._make_stage2_result("final task")
        stage1_transformed = "ask john to final task"

        with self._patch_stage2(stage2_result) as mock_stage2:
            AppMCPRoutingService._route_identity(
                db_session=MagicMock(),
                selected_route=selected_route,
                caller_user_id=uuid.uuid4(),
                message="ask cinna to ask john to final task",
                stage1_method="ai",
                transformed_message=stage1_transformed,
            )

        # Stage 2 should have received the Stage 1 transformed message
        call_kwargs = mock_stage2.call_args.kwargs
        assert call_kwargs["message"] == stage1_transformed

    def test_stage2_receives_original_message_when_stage1_did_not_transform(self):
        """When Stage 1 didn't transform, Stage 2 gets the original message."""
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        owner_id = uuid.uuid4()
        selected_route = self._make_selected_route(owner_id)
        stage2_result = self._make_stage2_result(None)
        original_message = "direct task for john"

        with self._patch_stage2(stage2_result) as mock_stage2:
            AppMCPRoutingService._route_identity(
                db_session=MagicMock(),
                selected_route=selected_route,
                caller_user_id=uuid.uuid4(),
                message=original_message,
                stage1_method="only_one",
                transformed_message=None,
            )

        call_kwargs = mock_stage2.call_args.kwargs
        assert call_kwargs["message"] == original_message

    def test_returns_none_when_stage2_returns_none(self):
        from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService

        owner_id = uuid.uuid4()
        selected_route = self._make_selected_route(owner_id)

        with self._patch_stage2(None):
            result = AppMCPRoutingService._route_identity(
                db_session=MagicMock(),
                selected_route=selected_route,
                caller_user_id=uuid.uuid4(),
                message="message",
                stage1_method="ai",
            )

        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# 8. AIFunctionsService.route_to_agent() return type pass-through
# ──────────────────────────────────────────────────────────────────────────────

class TestAIFunctionsServiceRouteToAgent:
    def test_passes_through_route_to_agent_result(self):
        from app.services.ai_functions.ai_functions_service import AIFunctionsService
        from app.agents.app_agent_router import RouteToAgentResult

        expected = RouteToAgentResult(agent_id="test-id", transformed_message="stripped")

        with patch(
            "app.services.ai_functions.ai_functions_service.route_to_agent_from_agents",
            return_value=expected,
        ):
            result = AIFunctionsService.route_to_agent(
                message="ask cinna to stripped",
                available_agents=[{"id": "test-id", "name": "X", "trigger_prompt": "X"}],
            )

        assert result is expected

    def test_returns_none_on_exception(self):
        from app.services.ai_functions.ai_functions_service import AIFunctionsService

        with patch(
            "app.services.ai_functions.ai_functions_service.route_to_agent_from_agents",
            side_effect=RuntimeError("LLM unavailable"),
        ):
            result = AIFunctionsService.route_to_agent(
                message="anything",
                available_agents=[],
            )

        assert result is None
