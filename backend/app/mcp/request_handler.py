"""
MCP Request Handler - handles MCP tool requests.

This module bridges MCP protocol requests to internal services,
handling message send operations through the service layer.

All data access is done through the service layer (SessionService, MessageService)
rather than direct database queries (following the same pattern as A2ARequestHandler).
"""
import json
import logging
from typing import Callable
from uuid import UUID

from sqlmodel import Session as DbSession

from app.models import Agent
from app.models.environments.environment import AgentEnvironment
from app.models.mcp.mcp_connector import MCPConnector
from app.services.bundles.install_gate_dispatcher import InstallGateDispatcher
from app.services.sessions.session_service import SessionService
from app.services.sessions.message_service import MessageService
from app.mcp.message_streaming import stream_and_collect_response

logger = logging.getLogger(__name__)


class MCPRequestHandler:
    """
    Handles MCP tool requests by delegating to internal services.

    Follows the same isolation pattern as A2ARequestHandler:
    - Receives pre-resolved entities (agent, environment, connector)
    - All data access through SessionService and MessageService
    - No direct database queries in this handler
    """

    def __init__(
        self,
        agent: Agent,
        environment: AgentEnvironment,
        connector: MCPConnector,
        get_db_session: Callable[[], DbSession],
        authenticated_user_id: UUID | None = None,
    ):
        self.agent = agent
        self.environment = environment
        self.connector = connector
        self.get_db_session = get_db_session
        self.authenticated_user_id = authenticated_user_id

    async def handle_send_message(
        self,
        message: str,
        mcp_session_id: str | None = None,
        context_id: str | None = None,
        mcp_ctx=None,
    ) -> str:
        """
        Handle send_message tool call.

        Uses the same SessionService/MessageService pipeline as A2A integration:
        1. Get/create platform session for this MCP connector
        2. Create user message
        3. Delegate to shared streaming pipeline (environment readiness + stream + collect)
        4. Return JSON with response text and context_id

        Args:
            message: User message content
            mcp_session_id: Optional MCP transport session ID (from client header)
            context_id: Optional context_id for per-chat session isolation

        Returns:
            JSON string with "response" and "context_id" fields
        """
        # Phase 1: Get/create session and create user message
        with self.get_db_session() as db:
            try:
                platform_session, is_new_session = SessionService.get_or_create_mcp_session(
                    db_session=db,
                    connector=self.connector,
                    mcp_session_id=mcp_session_id,
                    context_id=context_id,
                    authenticated_user_id=self.authenticated_user_id,
                )
            except ValueError as e:
                return json.dumps({"error": str(e), "context_id": ""})

            session_id = platform_session.id
            result_context_id = str(session_id)

            # Trigger title generation for new sessions before the install
            # gate, so gate-blocked sessions still get a title derived from
            # the user message. The title is derived from user input via
            # ``AIFunctionsService`` (or falls back to a truncated body when
            # no AI provider is available) and does not require the install's
            # own agent to be runnable.
            if is_new_session:
                SessionService.ensure_title_for_new_session(
                    session_id=session_id,
                    first_message_content=message,
                    get_fresh_db_session=self.get_db_session,
                )

            # Install readiness gate (plan §6.2 — MCP rendering). When the
            # install has empty placeholders or broken publisher creds we
            # persist the user's message + a synthesised system reply (so
            # the platform chat tab shows what happened, mirroring chat
            # channel behaviour) and return the gate's JSON envelope to
            # the MCP client without touching the LLM.
            gate_result = InstallGateDispatcher.check(db, self.agent)
            if gate_result is not None:
                logger.info(
                    "[MCP] Install readiness gate blocking agent %s (status=%s, %d missing)",
                    self.agent.id, gate_result.status, len(gate_result.missing),
                )

                # Persist the user message so the platform-side session
                # history shows it. Marked ``sent`` because no LLM dispatch
                # is going to consume it.
                MessageService.create_message(
                    session=db,
                    session_id=session_id,
                    role="user",
                    content=message,
                    sent_to_agent_status="sent",
                )

                # Persist the gate's synthesised reply as a system message
                # so the chat history shows it identically to chat-channel
                # gate trips. No pending messages to scrub — the user
                # message above was created already-sent.
                InstallGateDispatcher.persist_for_session(
                    db,
                    session_id=session_id,
                    gate_result=gate_result,
                )

                await InstallGateDispatcher.emit_events(
                    agent=self.agent,
                    gate_result=gate_result,
                    channel="mcp",
                    session_id=session_id,
                )

                return json.dumps({
                    "response": gate_result.user_message,
                    "context_id": result_context_id,
                    "setup_url": gate_result.setup_url,
                })

            # Create user message as "pending" — the shared streaming pipeline
            # will collect it, mark as sent, and stream (same as process_pending_messages)
            MessageService.create_message(
                session=db,
                session_id=session_id,
                role="user",
                content=message,
            )

        # Phases 2–3: Environment readiness + streaming (shared pipeline)
        response_text = await stream_and_collect_response(
            session_id=session_id,
            get_fresh_db_session=self.get_db_session,
            mcp_ctx=mcp_ctx,
            log_prefix="[MCP]",
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
        })
