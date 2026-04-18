"""
ExternalA2ARequestHandler — dispatches external A2A requests to the right agent.

Responsibilities:
  1. Resolve a TargetContext from (target_type, target_id, user) via
     ExternalAccessPolicy — access checks live there, not here.
  2. Delegate to ExternalA2AContextHandler's *_with_context methods.
  3. Expose a ``handle_jsonrpc`` method that handles: protocol resolution,
     body parsing, method dispatch, v1.0 adapter transform, and domain
     exception → JSON-RPC error-envelope mapping.
  4. Skip the a2a_config.enabled gate (the external surface is owner-only
     for agent targets, and caller-scoped for route / identity targets).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional
from uuid import UUID

from sqlmodel import Session as DBSession

from app.models import Agent, Session as ChatSession, User
from app.models.environments.environment import AgentEnvironment
from app.services.a2a.a2a_v1_adapter import A2AV1Adapter
from app.services.a2a.jsonrpc_utils import (
    jsonrpc_error,
    jsonrpc_success,
    resolve_protocol,
)
from app.services.external.errors import (
    ExternalAccessError,
    IdentityBindingRevokedError,
    InvalidExternalParamsError,
    NoActiveEnvironmentError,
    TargetNotAccessibleError,
)
from app.services.external.external_a2a_context_handler import (
    ExternalA2AContextHandler,
    TargetContext,
)
from app.services.external.external_access_policy import ExternalAccessPolicy
from app.services.identity.identity_routing_service import IdentityRoutingService
from app.services.identity.identity_service import IdentityService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSONRPCOutcome — result carrier for handle_jsonrpc
# ---------------------------------------------------------------------------


@dataclass
class JSONRPCOutcome:
    """Outcome of a handle_jsonrpc call.

    Exactly one of ``result_envelope`` or ``stream`` will be set.

    Attributes:
        result_envelope: A plain JSON-RPC dict ready to be wrapped in JSONResponse.
        stream: An async generator of SSE strings ready to be wrapped in
            StreamingResponse.
    """

    result_envelope: Optional[dict] = None
    stream: Optional[AsyncIterator[str]] = None

    @property
    def is_stream(self) -> bool:
        return self.stream is not None


# ---------------------------------------------------------------------------
# ExternalA2ARequestHandler
# ---------------------------------------------------------------------------


class ExternalA2ARequestHandler:
    """Dispatches external A2A requests to ExternalA2AContextHandler."""

    def __init__(
        self,
        get_db_session: Callable[[], DBSession],
        backend_base_url: str = "",
    ) -> None:
        self.get_db_session = get_db_session
        self.backend_base_url = backend_base_url

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def handle_request(
        self,
        *,
        db: DBSession,
        user: User,
        target_type: str,
        target_id: UUID,
        raw_body: bytes,
        protocol_param: str | None,
        client_kind: str | None,
        external_client_id: str | None,
    ) -> JSONRPCOutcome:
        """End-to-end entry point that owns parse + protocol resolution.

        Emits ``-32700`` for a malformed body and ``-32602`` for an unknown
        protocol version, so route handlers don't have to repeat that plumbing.
        """
        try:
            protocol = resolve_protocol(protocol_param)
        except InvalidExternalParamsError as e:
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(None, e.jsonrpc_code, e.message)
            )

        try:
            request_body = json.loads(raw_body) if raw_body else None
        except (ValueError, TypeError):
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(None, -32700, "Parse error")
            )

        return await self.handle_jsonrpc(
            db=db,
            user=user,
            target_type=target_type,
            target_id=target_id,
            request_body=request_body,
            protocol=protocol,
            client_kind=client_kind,
            external_client_id=external_client_id,
        )

    async def handle_jsonrpc(
        self,
        *,
        db: DBSession,
        user: User,
        target_type: str,
        target_id: UUID,
        request_body: dict,
        protocol: str,
        client_kind: str | None,
        external_client_id: str | None,
    ) -> JSONRPCOutcome:
        """Parse, dispatch, and return the JSON-RPC outcome for a single request.

        Handles:
        - JSON-RPC envelope validation
        - v1.0 inbound adapter transform
        - Target context resolution
        - Method dispatch to ExternalA2AContextHandler
        - v1.0 outbound adapter transform
        - Domain exception → JSON-RPC error-code mapping

        Args:
            db: Active database session.
            user: Authenticated caller.
            target_type: "agent", "app_mcp_route", or "identity".
            target_id: UUID of the target.
            request_body: Parsed JSON-RPC request dict.
            protocol: "v1.0" or "v0.3" (already resolved by the route layer).
            client_kind: Optional client kind from JWT (e.g. "desktop").
            external_client_id: Optional client id from JWT.

        Returns:
            JSONRPCOutcome with either ``result_envelope`` or ``stream`` set.
        """
        use_v1 = protocol == "v1.0"

        # Validate JSON-RPC envelope
        if not isinstance(request_body, dict):
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(None, -32600, "Invalid Request")
            )
        if request_body.get("jsonrpc") != "2.0":
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(
                    request_body.get("id"),
                    -32600,
                    "Invalid Request: jsonrpc must be '2.0'",
                )
            )

        if use_v1:
            request_body = A2AV1Adapter.transform_request_inbound(request_body)

        method = request_body.get("method")
        request_id = request_body.get("id")
        params = request_body.get("params", {})

        if not method:
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(
                    request_id, -32600, "Invalid Request: method is required"
                )
            )

        try:
            context = self._resolve_context(
                db=db,
                user=user,
                target_type=target_type,
                target_id=target_id,
                params=params,
                client_kind=client_kind,
                external_client_id=external_client_id,
            )
        except ExternalAccessError as e:
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(request_id, e.jsonrpc_code, e.message)
            )
        except InvalidExternalParamsError as e:
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(request_id, e.jsonrpc_code, e.message)
            )

        try:
            return await self._dispatch_context(
                context=context,
                method=method,
                params=params,
                request_id=request_id,
                use_v1=use_v1,
            )
        except ExternalAccessError as e:
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(request_id, e.jsonrpc_code, e.message)
            )
        except InvalidExternalParamsError as e:
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(request_id, e.jsonrpc_code, e.message)
            )
        except Exception as e:
            logger.error(
                "Error handling external A2A request: %s", e, exc_info=True
            )
            return JSONRPCOutcome(
                result_envelope=jsonrpc_error(
                    request_id, -32603, f"Internal error: {str(e)}"
                )
            )

    # ------------------------------------------------------------------
    # Target resolution — thin wrappers that call ExternalAccessPolicy
    # ------------------------------------------------------------------

    def _resolve_context(
        self,
        *,
        db: DBSession,
        user: User,
        target_type: str,
        target_id: UUID,
        params: dict[str, Any],
        client_kind: str | None,
        external_client_id: str | None,
    ) -> TargetContext:
        """Resolve a TargetContext for the given target type.

        Raises:
            TargetNotAccessibleError: Any access-check failure.
            NoActiveEnvironmentError: Target agent has no active environment.
            InvalidExternalParamsError: Unknown target_type.
        """
        if target_type == "agent":
            return self._resolve_agent_context(
                db, user, target_id,
                client_kind=client_kind,
                external_client_id=external_client_id,
            )
        if target_type == "app_mcp_route":
            return self._resolve_route_context(
                db, user, target_id,
                client_kind=client_kind,
                external_client_id=external_client_id,
            )
        if target_type == "identity":
            return self._resolve_identity_context(
                db, user, target_id, params,
                client_kind=client_kind,
                external_client_id=external_client_id,
            )
        raise InvalidExternalParamsError(f"Unsupported target_type: {target_type!r}")

    def _resolve_agent_context(
        self,
        db: DBSession,
        user: User,
        agent_id: UUID,
        client_kind: str | None = None,
        external_client_id: str | None = None,
    ) -> TargetContext:
        """Resolve TargetContext for target_type="agent" via ExternalAccessPolicy."""
        agent, environment = ExternalAccessPolicy.resolve_agent(
            db, user, agent_id, require_env=True
        )
        return TargetContext(
            agent=agent,
            environment=environment,
            integration_type="external",
            session_owner_id=user.id,
            client_kind=client_kind,
            external_client_id=external_client_id,
        )

    def _resolve_route_context(
        self,
        db: DBSession,
        user: User,
        route_id: UUID,
        client_kind: str | None = None,
        external_client_id: str | None = None,
    ) -> TargetContext:
        """Resolve TargetContext for target_type="app_mcp_route" via ExternalAccessPolicy."""
        route, effective = ExternalAccessPolicy.resolve_route(db, user, route_id)

        agent = db.get(Agent, route.agent_id)
        if agent is None:
            raise TargetNotAccessibleError("Route agent not found")

        if not agent.active_environment_id:
            raise NoActiveEnvironmentError()
        environment = db.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            raise NoActiveEnvironmentError()

        return TargetContext(
            agent=agent,
            environment=environment,
            integration_type="app_mcp",
            session_owner_id=agent.owner_id,
            caller_id=user.id,
            match_method="external_direct",
            route_id=route.id,
            route_source=effective.source,
            client_kind=client_kind,
            external_client_id=external_client_id,
        )

    def _resolve_identity_context(
        self,
        db: DBSession,
        user: User,
        owner_id: UUID,
        params: dict[str, Any],
        client_kind: str | None = None,
        external_client_id: str | None = None,
    ) -> TargetContext:
        """Resolve TargetContext for target_type="identity".

        Two paths:
        1. Resume: params carries a task_id that maps to an existing
           identity_mcp session owned by owner_id with identity_caller_id == user.id.
        2. Fresh route: no task_id. Stage 2 routing picks the binding/agent.

        Raises:
            TargetNotAccessibleError: Owner not found, or caller has no accessible bindings.
            NoActiveEnvironmentError: Routed agent has no active environment.
            IdentityBindingRevokedError: Resumed session's binding was revoked.
        """
        # Verify owner exists first (quick check before heavier binding queries)
        owner = db.get(User, owner_id)
        if not owner:
            raise TargetNotAccessibleError("Identity not found or access denied")

        message_data = params.get("message", {}) if isinstance(params, dict) else {}
        task_id = None
        if isinstance(message_data, dict):
            task_id = message_data.get("taskId") or message_data.get("task_id")
        if not task_id:
            task_id = params.get("id") if isinstance(params, dict) else None

        # Try to resume an existing session FIRST — this allows revoked-binding
        # errors ("no longer active") to surface correctly even when the caller
        # no longer has any active bindings (require_identity_access would raise
        # TargetNotAccessibleError before we get to the revocation message).
        resumed = self._try_resume_identity_session(db, user, owner_id, task_id)
        if resumed is not None:
            session, agent = resumed
            if not agent.active_environment_id:
                raise NoActiveEnvironmentError()
            environment = db.get(AgentEnvironment, agent.active_environment_id)
            if not environment:
                raise NoActiveEnvironmentError()
            return TargetContext(
                agent=agent,
                environment=environment,
                integration_type="identity_mcp",
                session_owner_id=owner_id,
                identity_caller_id=user.id,
                identity_binding_id=session.identity_binding_id,
                identity_binding_assignment_id=session.identity_binding_assignment_id,
                identity_owner_name=owner.full_name or owner.email or str(owner_id),
                identity_caller_name=user.full_name or user.email or str(user.id),
                client_kind=client_kind,
                external_client_id=external_client_id,
            )

        # Fresh route — verify caller still has at least one accessible binding,
        # then run Stage 2 to pick the binding/agent.
        ExternalAccessPolicy.require_identity_access(db, user, owner_id)
        first_message = self._extract_message_text(message_data)

        routing_result = IdentityRoutingService.route_within_identity(
            db_session=db,
            owner_id=owner_id,
            caller_user_id=user.id,
            message=first_message,
        )
        if routing_result is None:
            raise TargetNotAccessibleError(
                "No accessible agent matched the message for this identity"
            )

        agent = db.get(Agent, routing_result.agent_id)
        if agent is None:
            raise TargetNotAccessibleError("Routed identity agent not found")

        if not agent.active_environment_id:
            raise NoActiveEnvironmentError()
        environment = db.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            raise NoActiveEnvironmentError()

        return TargetContext(
            agent=agent,
            environment=environment,
            integration_type="identity_mcp",
            session_owner_id=owner_id,
            identity_caller_id=user.id,
            match_method="identity",
            identity_binding_id=routing_result.binding_id,
            identity_binding_assignment_id=routing_result.binding_assignment_id,
            identity_stage2_match_method=routing_result.match_method,
            identity_owner_name=owner.full_name or owner.email or str(owner_id),
            identity_caller_name=user.full_name or user.email or str(user.id),
            client_kind=client_kind,
            external_client_id=external_client_id,
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _make_handler(self, context: TargetContext) -> ExternalA2AContextHandler:
        """Create an ExternalA2AContextHandler pre-loaded with the resolved context."""
        return ExternalA2AContextHandler(
            context=context,
            get_db_session=self.get_db_session,
            backend_base_url=self.backend_base_url,
        )

    async def _dispatch_context(
        self,
        context: TargetContext,
        method: str,
        params: dict[str, Any],
        request_id: Any,
        use_v1: bool,
    ) -> JSONRPCOutcome:
        """Dispatch a JSON-RPC method for an already-resolved TargetContext."""
        handler = self._make_handler(context)

        if method == "message/stream":
            logger.info(
                "[ExternalA2A] streaming | agent=%s integration=%s",
                context.agent.id, context.integration_type,
            )
            return JSONRPCOutcome(
                stream=handler.handle_message_stream(params, request_id)
            )

        if method == "message/send":
            logger.info(
                "[ExternalA2A] send | agent=%s integration=%s",
                context.agent.id, context.integration_type,
            )
            task = await handler.handle_message_send(params)
            result = task.model_dump(by_alias=True, exclude_none=True)
            if use_v1:
                result = A2AV1Adapter.transform_task_outbound(result)
            return JSONRPCOutcome(
                result_envelope=jsonrpc_success(request_id, result)
            )

        if method == "tasks/get":
            task = await handler.handle_tasks_get(params)
            if task is None:
                return JSONRPCOutcome(
                    result_envelope=jsonrpc_error(request_id, -32001, "Task not found")
                )
            result = task.model_dump(by_alias=True, exclude_none=True)
            if use_v1:
                result = A2AV1Adapter.transform_task_outbound(result)
            return JSONRPCOutcome(
                result_envelope=jsonrpc_success(request_id, result)
            )

        if method == "tasks/cancel":
            result = await handler.handle_tasks_cancel(params)
            return JSONRPCOutcome(
                result_envelope=jsonrpc_success(request_id, result)
            )

        if method == "tasks/list":
            tasks = await handler.handle_tasks_list(params)
            results = [t.model_dump(by_alias=True, exclude_none=True) for t in tasks]
            if use_v1:
                results = [A2AV1Adapter.transform_task_outbound(r) for r in results]
            return JSONRPCOutcome(
                result_envelope=jsonrpc_success(request_id, results)
            )

        raise InvalidExternalParamsError(f"Method not found: {method}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _try_resume_identity_session(
        db: DBSession,
        user: User,
        owner_id: UUID,
        task_id: Any,
    ) -> tuple[ChatSession, Agent] | None:
        """Return (session, agent) when task_id resumes a live identity session.

        Returns None when task_id is absent, not a UUID, or does not match an
        identity session for this caller.

        Raises:
            IdentityBindingRevokedError: Session matches but binding was revoked.
        """
        if not task_id:
            return None
        try:
            session_id = UUID(str(task_id))
        except (ValueError, TypeError):
            return None

        session = db.get(ChatSession, session_id)
        if session is None:
            return None
        if session.integration_type != "identity_mcp":
            return None
        if session.user_id != owner_id:
            return None
        if session.identity_caller_id != user.id:
            return None

        validity_error = IdentityService.check_session_validity(db, session)
        if validity_error:
            raise IdentityBindingRevokedError(validity_error)

        if session.agent_id is None:
            raise TargetNotAccessibleError("Identity session has no bound agent")
        agent = db.get(Agent, session.agent_id)
        if agent is None:
            raise TargetNotAccessibleError("Identity session agent not found")
        return session, agent

    @staticmethod
    def _extract_message_text(message_data: Any) -> str:
        """Pull the user's text out of the JSON-RPC message params for Stage 2 routing."""
        if not isinstance(message_data, dict):
            return ""
        parts = message_data.get("parts") or []
        if not isinstance(parts, list):
            return ""
        chunks: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                chunks.append(part["text"])
                continue
            root = part.get("root")
            if isinstance(root, dict) and isinstance(root.get("text"), str):
                chunks.append(root["text"])
        return "\n".join(chunks)

    @staticmethod
    def _extract_session_hint(params: dict[str, Any]) -> str:
        """Extract a human-readable session/task hint for logging."""
        if not isinstance(params, dict):
            return "new"
        message_data = params.get("message", {})
        if isinstance(message_data, dict):
            task_id = message_data.get("taskId") or message_data.get("task_id")
            if task_id:
                return str(task_id)
        task_id = params.get("id")
        if task_id:
            return str(task_id)
        return "new"
