"""
App MCP Request Handler — handles send_message tool calls for the App MCP Server.

Bridges App MCP protocol requests to internal services:
1. If context_id: resume existing session (agent already selected)
2. If no context_id: route to agent via AppMCPRoutingService
3. Create/reuse session, delegate to shared streaming pipeline, return result
"""
import json
import logging
import uuid

from sqlmodel import Session as DBSession, select

from app.core.db import create_session
from app.models import Agent, Session, SessionCreate
from app.services.sessions.session_service import SessionService
from app.services.sessions.message_service import MessageService
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
        # Phase 1: Resolve session (resume or create)
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
            # For identity sessions, return the owner's name (not the internal agent name)
            if platform_session.integration_type == "identity_mcp":
                agent_name = (
                    (platform_session.session_metadata or {}).get("identity_owner_name")
                    or (agent.name if hasattr(agent, "name") else "")
                )
            else:
                agent_name = agent.name if hasattr(agent, "name") else ""

            # Determine effective message — use AI-transformed message when available
            effective_message = (
                routing_result.transformed_message if routing_result else None
            ) or message

            # Store original message in session metadata for auditability when transformation occurred
            if routing_result and routing_result.transformed_message and is_new_session:
                platform_session.session_metadata = {
                    **(platform_session.session_metadata or {}),
                    "app_mcp_original_message": message,
                }
                db.add(platform_session)

            # Create user message as "pending" — the shared streaming pipeline
            # will collect it, mark as sent, and stream (same as process_pending_messages)
            MessageService.create_message(
                session=db,
                session_id=session_id,
                role="user",
                content=effective_message,
            )

        # Background title generation for new sessions
        if is_new_session:
            create_task_with_error_logging(
                SessionService.auto_generate_session_title(
                    session_id=session_id,
                    first_message_content=effective_message,
                    get_fresh_db_session=create_session,
                ),
                task_name=f"app_mcp_title_{session_id}",
            )

        # Phases 2–3: Environment readiness + streaming (shared pipeline)
        response_text = await stream_and_collect_response(
            session_id=session_id,
            get_fresh_db_session=create_session,
            mcp_ctx=mcp_ctx,
            log_prefix="[AppMCP]",
        )

        # If the shared pipeline returned an error JSON, pass it through
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
        # Case 1: Resume existing session by context_id
        if context_id:
            try:
                existing_session_id = uuid.UUID(context_id)
            except ValueError:
                existing_session_id = None

            if existing_session_id:
                # Try resuming a regular app_mcp session (owned by caller)
                stmt = (
                    select(Session, Agent)
                    .join(Agent, Session.agent_id == Agent.id)
                    .where(
                        Session.id == existing_session_id,
                        Session.user_id == user_id,
                        Session.integration_type == "app_mcp",
                    )
                )
                result = db.exec(stmt).first()
                if result:
                    session, agent = result
                    logger.debug("[AppMCP] Resuming session %s for user %s", context_id, user_id)
                    return session, agent, False, None

                # Try resuming an identity_mcp session (owned by identity owner, caller tracked separately)
                identity_stmt = (
                    select(Session, Agent)
                    .join(Agent, Session.agent_id == Agent.id)
                    .where(
                        Session.id == existing_session_id,
                        Session.identity_caller_id == user_id,
                        Session.integration_type == "identity_mcp",
                    )
                )
                identity_result = db.exec(identity_stmt).first()
                if identity_result:
                    session, agent = identity_result
                    # Validate binding and assignment are still active
                    validity_error = AppMCPRequestHandler._check_identity_session_validity(db, session)
                    if validity_error:
                        return None, validity_error, False, None
                    logger.debug(
                        "[AppMCP] Resuming identity session %s for caller %s",
                        context_id,
                        user_id,
                    )
                    return session, agent, False, None

            logger.debug("[AppMCP] context_id %s not found or invalid, creating new session", context_id)

        # Case 2: Route message to an agent
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

        # Identity routing: session is created in identity owner's space
        if routing_result.is_identity and routing_result.identity_owner_id:
            session, agent_or_err, is_new = AppMCPRequestHandler._create_identity_session(
                db=db,
                routing_result=routing_result,
                agent=agent,
                caller_user_id=user_id,
            )
            return session, agent_or_err, is_new, routing_result

        # Regular app_mcp session: session owned by caller
        session_data = SessionCreate(
            agent_id=routing_result.agent_id,
            mode=routing_result.session_mode,
        )
        session = SessionService.create_session(
            db_session=db,
            user_id=user_id,
            data=session_data,
            integration_type="app_mcp",
        )
        if not session:
            return None, "Failed to create session.", False, None

        # Store routing metadata in session_metadata
        session.session_metadata = {
            **(session.session_metadata or {}),
            "app_mcp_route_type": routing_result.route_source,
            "app_mcp_route_id": str(routing_result.route_id),
            "app_mcp_agent_name": routing_result.agent_name,
            "app_mcp_session_mode": routing_result.session_mode,
            "app_mcp_match_method": routing_result.match_method,
        }
        db.add(session)
        db.flush()
        db.refresh(session)

        return session, agent, True, routing_result

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

        session_data = SessionCreate(
            agent_id=routing_result.agent_id,
            mode=routing_result.session_mode,
        )
        # Session is owned by the identity owner (not the caller)
        session = SessionService.create_session(
            db_session=db,
            user_id=owner_id,
            data=session_data,
            integration_type="identity_mcp",
        )
        if not session:
            return None, "Failed to create identity session.", False

        # Set identity-specific columns
        session.identity_caller_id = caller_user_id
        session.identity_binding_id = routing_result.identity_binding_id
        session.identity_binding_assignment_id = routing_result.identity_binding_assignment_id

        # Store display metadata
        session.session_metadata = {
            **(session.session_metadata or {}),
            "identity_caller_name": caller.full_name if caller else str(caller_user_id),
            "identity_owner_name": owner.full_name if owner else str(owner_id),
            "identity_match_method": routing_result.identity_stage2_match_method or "",
            "app_mcp_route_type": "identity",
            "app_mcp_match_method": routing_result.match_method,
        }
        db.add(session)
        db.flush()
        db.refresh(session)

        return session, agent, True

    @staticmethod
    def _check_identity_session_validity(
        db: DBSession,
        session: Session,
    ) -> str | None:
        """Verify the identity binding and assignment are still active.

        Returns an error message string if invalid, or None if valid.
        """
        from app.models.identity.identity_models import (
            IdentityAgentBinding,
            IdentityBindingAssignment,
        )

        if session.identity_binding_id:
            binding = db.get(IdentityAgentBinding, session.identity_binding_id)
            if not binding or not binding.is_active:
                return "This identity connection is no longer active."

        if session.identity_binding_assignment_id:
            assignment = db.get(IdentityBindingAssignment, session.identity_binding_assignment_id)
            if not assignment or not assignment.is_active or not assignment.is_enabled:
                return "This identity connection is no longer active."

        return None
