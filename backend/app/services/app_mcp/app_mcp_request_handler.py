"""
App MCP Request Handler — handles send_message tool calls for the App MCP Server.

Bridges App MCP protocol requests to internal services:
1. If context_id: resume existing session (agent already selected)
2. If no context_id: route to agent via AppMCPRoutingService
3. Create/reuse session via ``ChannelIngestionService`` (plan §5.2),
   delegate to the shared streaming pipeline, and return the result.
"""
import json
import logging
import uuid

from sqlmodel import Session as DBSession

from app.core.db import create_session
from app.models import (
    Agent,
    ChannelAccessPolicy,
    Session,
    SessionSender,
)
from app.services.sessions.session_service import SessionService
from app.services.sessions.message_service import MessageService
from app.services.sessions.channel_ingestion_service import ChannelIngestionService
from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService, RoutingResult
from app.mcp.message_streaming import stream_and_collect_response
from app.utils import create_task_with_error_logging

logger = logging.getLogger(__name__)


class AppMCPRequestHandler:
    """Handles App MCP send_message tool calls."""

    @staticmethod
    async def handle_send_message(
        user_id: uuid.UUID,
        message: str,
        context_id: str | None,
        mcp_ctx=None,
    ) -> str:
        """Handle send_message tool call for App MCP.

        Returns JSON string with keys: response, context_id, agent_name.
        On error: returns JSON with keys: error, context_id.
        """
        try:
            return await AppMCPRequestHandler._handle_inner(
                user_id=user_id,
                message=message,
                context_id=context_id,
                mcp_ctx=mcp_ctx,
            )
        except Exception as e:
            logger.exception("[AppMCP] Unhandled error in send_message: %s", e)
            return json.dumps({"error": str(e), "context_id": context_id or ""})

    @staticmethod
    async def _handle_inner(
        user_id: uuid.UUID,
        message: str,
        context_id: str | None,
        mcp_ctx=None,
    ) -> str:
        """Inner handler — performs routing, session creation, and streaming."""
        # Phase 1: Resolve session (resume or create).
        with create_session() as db:
            platform_session, agent, is_new_session, routing_result = await AppMCPRequestHandler._resolve_session(
                db=db,
                user_id=user_id,
                message=message,
                context_id=context_id,
            )
            if platform_session is None:
                return json.dumps({
                    "error": (
                        agent if isinstance(agent, str)
                        else "No agents are configured for your account. Contact your admin."
                    ),
                    "context_id": "",
                })

            session_id = platform_session.id
            result_context_id = str(session_id)
            # For identity sessions, return the owner's name (not the internal agent name).
            if platform_session.integration_type == "identity_mcp":
                agent_name = (
                    (platform_session.session_metadata or {}).get("identity_owner_name")
                    or (agent.name if hasattr(agent, "name") else "")
                )
            else:
                agent_name = agent.name if hasattr(agent, "name") else ""

            # Determine effective message — use AI-transformed message when available.
            effective_message = (
                routing_result.transformed_message if routing_result else None
            ) or message

            # Store original message in session metadata for auditability when
            # transformation occurred. Stamping after session creation matches
            # pre-migration behavior; the field is not whitelisted by the
            # service's create-time stampable columns because only this path
            # writes it.
            if routing_result and routing_result.transformed_message and is_new_session:
                platform_session.session_metadata = {
                    **(platform_session.session_metadata or {}),
                    "app_mcp_original_message": message,
                }
                db.add(platform_session)

            # Create the user message as "pending" — the shared streaming
            # pipeline (``stream_and_collect_response``) will collect it,
            # mark as sent, and stream. This matches the pre-migration
            # MessageService.create_message call shape; we do NOT use
            # ``ingest_inbound_message`` here because it would kick a
            # background stream via ``initiate_stream`` and conflict with
            # ``stream_and_collect_response``'s session-lock acquisition.
            MessageService.create_message(
                session=db,
                session_id=session_id,
                role="user",
                content=effective_message,
            )

        if is_new_session:
            create_task_with_error_logging(
                SessionService.auto_generate_session_title(
                    session_id=session_id,
                    first_message_content=effective_message,
                    get_fresh_db_session=create_session,
                ),
                task_name=f"app_mcp_title_{session_id}",
            )

        # Phases 2–3: Environment readiness + streaming (shared MCP pipeline).
        response_text = await stream_and_collect_response(
            session_id=session_id,
            get_fresh_db_session=create_session,
            mcp_ctx=mcp_ctx,
            log_prefix="[AppMCP]",
        )

        # If the shared pipeline returned an error JSON, pass it through.
        if response_text.startswith("{"):
            try:
                parsed = json.loads(response_text)
                if "error" in parsed:
                    return response_text
            except (json.JSONDecodeError, KeyError):
                pass

        return json.dumps({
            "response": response_text if response_text else "No response from agent",
            "context_id": result_context_id,
            "agent_name": agent_name,
        })

    @staticmethod
    async def _resolve_session(
        db: DBSession,
        user_id: uuid.UUID,
        message: str,
        context_id: str | None,
    ) -> tuple[Session | None, Agent | str | None, bool, RoutingResult | None]:
        """Resolve or create a session.

        Returns: (session, agent, is_new_session, routing_result)
        If routing fails, returns (None, error_message_str, False, None).
        For session resumption, routing_result is None (no transformation on resume).
        """
        # Case 1: Resume existing session by context_id. Resume is a direct
        # session lookup + strict (integration_type, caller-column) match —
        # we do NOT route the resume through ``resolve_or_create_session``
        # because legacy behavior is to silently fall through to routing on
        # a miss, while the service raises ``PermissionError`` on a
        # mismatched sender. The service's ``_verify_resume_sender`` for
        # ``mcp_caller`` (tightened in Phase 3) enforces the same strict
        # check; performing it here lets us preserve the soft fall-through.
        if context_id:
            try:
                existing_session_id = uuid.UUID(context_id)
            except ValueError:
                existing_session_id = None

            if existing_session_id:
                resumed = AppMCPRequestHandler._try_resume_session(
                    db=db,
                    user_id=user_id,
                    existing_session_id=existing_session_id,
                )
                if resumed is not None:
                    session, agent_or_err, is_new_session = resumed
                    if session is None:
                        # Identity session found but binding/assignment no
                        # longer valid — surface the validity error.
                        return None, agent_or_err, False, None
                    return session, agent_or_err, is_new_session, None

            logger.debug("[AppMCP] context_id %s not found or invalid, creating new session", context_id)

        # Case 2: Route message to an agent.
        routing_result = AppMCPRoutingService.route_message(
            db_session=db,
            user_id=user_id,
            message=message,
        )
        if not routing_result:
            return None, "Could not determine which agent to use. Please be more specific, or ask your admin to configure agents for your account.", False, None

        agent = db.get(Agent, routing_result.agent_id)
        if not agent or not agent.active_environment_id:
            return None, f"Agent '{routing_result.agent_name}' does not have an active environment.", False, None

        # Identity routing: session is created in identity owner's space.
        if routing_result.is_identity and routing_result.identity_owner_id:
            session, agent_or_err, is_new = AppMCPRequestHandler._create_identity_session(
                db=db,
                routing_result=routing_result,
                agent=agent,
                caller_user_id=user_id,
            )
            return session, agent_or_err, is_new, routing_result

        # Regular app_mcp session: owned by the agent owner; caller tracked
        # via ``caller_id`` (stamped by the service from ``extra_session_kwargs``).
        sender = SessionSender.from_app_mcp(caller_user_id=user_id)
        try:
            ChannelIngestionService.assert_access(
                agent=agent,
                sender=sender,
                policy=ChannelAccessPolicy(
                    expected_owner_id=agent.owner_id,
                    require_caller_in_route=True,
                ),
            )
            session, _ = ChannelIngestionService.resolve_or_create_session(
                db=db,
                agent=agent,
                sender=sender,
                thread_key=None,
                integration_type="app_mcp",
                extra_session_kwargs={
                    "mode": routing_result.session_mode,
                    "caller_id": user_id,
                    "session_metadata_extra": {
                        "app_mcp_route_type": routing_result.route_source,
                        "app_mcp_route_id": str(routing_result.route_id),
                        "app_mcp_agent_name": routing_result.agent_name,
                        "app_mcp_session_mode": routing_result.session_mode,
                        "app_mcp_match_method": routing_result.match_method,
                    },
                },
            )
        except PermissionError as e:
            # Service-side access denial. ValueError (session not found) is
            # only raised for resume paths — unreachable here because
            # thread_key=None. RuntimeError (no active env) is pre-checked
            # at :213 above, so it also cannot fire here; let it propagate
            # if it ever did so the unexpected condition surfaces.
            logger.warning("[AppMCP] Permission denied creating app_mcp session: %s", e)
            return None, str(e), False, None

        return session, agent, True, routing_result

    @staticmethod
    def _try_resume_session(
        db: DBSession,
        user_id: uuid.UUID,
        existing_session_id: uuid.UUID,
    ) -> tuple[Session | None, Agent | str | None, bool] | None:
        """Attempt to resume an existing app_mcp or identity_mcp session.

        Returns:
          - None if no session matches the (id, caller) tuple (caller should
            fall through to routing).
          - (session, agent, False) on a successful resume.
          - (None, error_message, False) when an identity session was found
            but its binding/assignment is no longer active.
        """
        existing = SessionService.get_session(db, existing_session_id)
        if existing is None:
            return None

        # Strict (integration_type, caller-column) match. Matches the two
        # narrow SQL queries in the pre-migration handler and the service's
        # ``_verify_resume_sender`` mcp_caller branch (tightened in Phase 3).
        if existing.integration_type == "app_mcp":
            if existing.caller_id != user_id:
                return None
        elif existing.integration_type == "identity_mcp":
            if existing.identity_caller_id != user_id:
                return None
            # For identity sessions, validate that the binding and
            # assignment are still active. MCP-specific business logic.
            validity_error = AppMCPRequestHandler._check_identity_session_validity(db, existing)
            if validity_error:
                return None, validity_error, False
        else:
            return None

        agent = db.get(Agent, existing.agent_id)
        if not agent:
            return None

        logger.debug(
            "[AppMCP] Resuming %s session %s for caller %s",
            existing.integration_type,
            existing_session_id,
            user_id,
        )
        return existing, agent, False

    @staticmethod
    def _create_identity_session(
        db: DBSession,
        routing_result: "RoutingResult",
        agent: Agent,
        caller_user_id: uuid.UUID,
    ) -> tuple[Session | None, Agent | str | None, bool]:
        """Create a session in the identity owner's space for identity routing."""
        from app.models import User

        owner_id = routing_result.identity_owner_id
        owner = db.get(User, owner_id)
        caller = db.get(User, caller_user_id)

        sender = SessionSender.from_app_mcp(
            caller_user_id=caller_user_id,
            identity_caller_user_id=caller_user_id,
        )
        try:
            ChannelIngestionService.assert_access(
                agent=agent,
                sender=sender,
                policy=ChannelAccessPolicy(
                    expected_owner_id=agent.owner_id,
                    require_caller_in_route=True,
                ),
            )
            session, _ = ChannelIngestionService.resolve_or_create_session(
                db=db,
                agent=agent,
                sender=sender,
                thread_key=None,
                integration_type="identity_mcp",
                extra_session_kwargs={
                    "mode": routing_result.session_mode,
                    # Owner of the session is the identity owner, not the
                    # agent owner (consumed by ``_select_session_owner_id``).
                    "identity_owner_id": owner_id,
                    # Post-create stamping of identity-specific columns.
                    "identity_caller_id": caller_user_id,
                    "identity_binding_id": routing_result.identity_binding_id,
                    "identity_binding_assignment_id": routing_result.identity_binding_assignment_id,
                    "session_metadata_extra": {
                        "identity_caller_name": caller.full_name if caller else str(caller_user_id),
                        "identity_owner_name": owner.full_name if owner else str(owner_id),
                        "identity_match_method": routing_result.identity_stage2_match_method or "",
                        "app_mcp_route_type": "identity",
                        "app_mcp_match_method": routing_result.match_method,
                    },
                },
            )
        except PermissionError as e:
            # Service-side access denial. ValueError (session not found) is
            # only raised for resume paths — unreachable here because
            # thread_key=None. RuntimeError (no active env) is pre-checked
            # by the caller (_resolve_session :213) before this method is
            # invoked. Both can propagate as unexpected exceptions if they
            # ever fire.
            logger.warning("[AppMCP] Permission denied creating identity session: %s", e)
            return None, str(e), False

        return session, agent, True

    @staticmethod
    def _check_identity_session_validity(
        db: DBSession,
        session: Session,
    ) -> str | None:
        """Verify the identity binding and assignment are still active.

        Delegates to IdentityService.check_session_validity — the canonical
        implementation shared by all handlers.
        """
        from app.services.identity.identity_service import IdentityService
        return IdentityService.check_session_validity(db, session)
