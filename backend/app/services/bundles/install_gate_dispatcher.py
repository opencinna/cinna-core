"""Install gate dispatcher — shared orchestration for the four channels.

The :class:`InstallReadinessGate` itself is a pure, read-only check. The
side-effects required when the gate blocks (emitting
``INSTALL_SETUP_REQUIRED`` and ``PUBLISHER_CREDENTIAL_BROKEN`` WebSocket
events, persisting a synthesised system reply for channels that have a
session anchor, building the structured ``missing`` payload) are identical
in spirit across chat, MCP, A2A, and webhook — only the response shape
differs per channel.

This module owns those side-effects so each call site collapses to:

1. ``dispatcher.check(db, agent)`` → ``GateResult | None``.
2. If blocked: ``await dispatcher.emit_events(...)`` and optionally
   ``dispatcher.persist_for_session(...)`` then render the channel-native
   reply.
"""
from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from sqlmodel import Session as DBSession

from app.models.agents.agent import Agent
from app.models.events.event import EventType
from app.services.bundles.install_readiness_gate import (
    GateResult,
    InstallReadinessGate,
)

logger = logging.getLogger(__name__)


Channel = Literal["chat", "mcp", "a2a", "webhook"]


class InstallGateDispatcher:
    """Centralised orchestration for the install-readiness gate.

    All methods are static — the dispatcher carries no state.
    """

    # ── Gate check ────────────────────────────────────────────────

    @staticmethod
    def check(db: DBSession, agent: Agent) -> GateResult | None:
        """Run the gate; return ``None`` when ready or on unexpected error.

        Defensive: any exception falls through to ``None`` so an unrelated
        bug in the gate cannot lock users out of chat. Callers should
        treat ``None`` as "allow through".
        """
        try:
            result = InstallReadinessGate.check(db, agent)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "Install readiness gate raised for agent %s; allowing through: %s",
                agent.id,
                exc,
            )
            return None

        if result.status == "ready":
            return None
        return result

    # ── Event emission ────────────────────────────────────────────

    @staticmethod
    async def emit_events(
        *,
        agent: Agent,
        gate_result: GateResult,
        channel: Channel,
        session_id: UUID | None = None,
        webhook_id: UUID | None = None,
    ) -> None:
        """Emit ``INSTALL_SETUP_REQUIRED`` plus, when applicable,
        ``PUBLISHER_CREDENTIAL_BROKEN``. Identical ``meta`` payload across
        channels. Best-effort: emission errors are logged and swallowed.
        """
        from app.services.events.event_service import event_service

        meta = InstallGateDispatcher._build_event_meta(
            agent=agent,
            gate_result=gate_result,
            channel=channel,
            session_id=session_id,
            webhook_id=webhook_id,
        )

        try:
            await event_service.emit_event(
                event_type=EventType.INSTALL_SETUP_REQUIRED,
                model_id=agent.id,
                user_id=agent.owner_id,
                meta=meta,
            )
            if gate_result.status == "publisher_broken":
                await event_service.emit_event(
                    event_type=EventType.PUBLISHER_CREDENTIAL_BROKEN,
                    model_id=agent.id,
                    user_id=agent.owner_id,
                    meta=meta,
                )
        except Exception as exc:  # pragma: no cover — diagnostic-only
            logger.warning(
                "Failed to emit install-setup events for agent %s (channel=%s): %s",
                agent.id,
                channel,
                exc,
            )

    # ── Per-session persistence (chat + MCP) ──────────────────────

    @staticmethod
    def persist_for_session(
        db: DBSession,
        *,
        session_id: UUID,
        gate_result: GateResult,
        pending_message_ids: list[UUID] | None = None,
    ) -> None:
        """Persist the gate's synthesised reply as a ``system``-role message
        and mark any pending user messages as ``sent`` so they don't restream.

        Used by channels that have a per-conversation ``Session`` anchor
        (chat, MCP). External channels without a session (A2A on
        gate-block, webhook) skip this step.
        """
        from app.services.sessions.message_service import MessageService

        message_metadata = InstallGateDispatcher.build_message_metadata(gate_result)
        MessageService.create_message(
            session=db,
            session_id=session_id,
            role="system",
            content=gate_result.user_message,
            message_metadata=message_metadata,
            sent_to_agent_status="sent",
            status="completed",
        )

        if pending_message_ids:
            try:
                MessageService.mark_messages_as_sent(db, pending_message_ids)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to mark pending messages sent after gate trip on session %s: %s",
                    session_id,
                    exc,
                )

    # ── Chat-channel stream events ────────────────────────────────

    @staticmethod
    async def emit_chat_stream(
        *,
        session_id: UUID,
        gate_result: GateResult,
    ) -> None:
        """Emit the assistant+stream_completed events so an open chat UI
        renders the gate reply identically to a normal assistant turn.

        Best-effort: the persisted system message is the source of truth.
        """
        from app.services.events.event_service import event_service

        try:
            await event_service.emit_stream_event(
                session_id,
                "assistant",
                {
                    "type": "assistant",
                    "content": gate_result.user_message,
                    "event_seq": 1,
                    "metadata": {
                        "install_setup_required": True,
                        "setup_url": gate_result.setup_url,
                        "gate_status": gate_result.status,
                    },
                },
            )
            await event_service.emit_stream_event(
                session_id,
                "stream_completed",
                {
                    "status": "completed",
                    "session_id": str(session_id),
                },
            )
        except Exception as exc:  # pragma: no cover — diagnostic-only
            logger.warning(
                "Failed to emit gate stream event for session %s: %s",
                session_id,
                exc,
            )

    # ── Payload builders ──────────────────────────────────────────

    @staticmethod
    def build_message_metadata(gate_result: GateResult) -> dict[str, Any]:
        """Build the ``message_metadata`` dict persisted on the system
        reply message. Used by chat and MCP.
        """
        return {
            "synthesized": True,
            "install_setup_required": True,
            "setup_url": gate_result.setup_url,
            "missing": InstallGateDispatcher._missing_payload(gate_result),
            "gate_status": gate_result.status,
        }

    @staticmethod
    def build_a2a_data_payload(gate_result: GateResult) -> dict[str, Any]:
        """Build the ``DataPart`` payload returned inside the synthetic
        A2A Task when the gate blocks.
        """
        return {
            "type": "cinna.setup_required",
            "setup_url": gate_result.setup_url,
            "missing": InstallGateDispatcher._missing_payload(gate_result),
        }

    @staticmethod
    def build_webhook_log_summary(gate_result: GateResult) -> dict[str, Any]:
        """Build the JSON-serialisable summary recorded on the webhook
        log row when the gate blocks an invocation.
        """
        return {
            "status": "setup_required",
            "setup_url": gate_result.setup_url,
            "missing": InstallGateDispatcher._missing_payload(gate_result),
        }

    # ── Internal helpers ──────────────────────────────────────────

    @staticmethod
    def _missing_payload(gate_result: GateResult) -> list[dict[str, Any]]:
        """Serialise ``GateMissingItem`` list to a JSON-safe dict list."""
        return [
            {
                "spec_name": m.spec_name,
                "spec_type": m.spec_type,
                "reason": m.reason,
                "is_ai": m.is_ai,
            }
            for m in gate_result.missing
        ]

    @staticmethod
    def _build_event_meta(
        *,
        agent: Agent,
        gate_result: GateResult,
        channel: Channel,
        session_id: UUID | None,
        webhook_id: UUID | None,
    ) -> dict[str, Any]:
        """Uniform meta dict for both ``INSTALL_SETUP_REQUIRED`` and
        ``PUBLISHER_CREDENTIAL_BROKEN``. ``channel`` is always set;
        ``session_id`` and ``webhook_id`` are always present as nullable
        keys so consumers can rely on the shape.
        """
        return {
            "agent_id": str(agent.id),
            "channel": channel,
            "session_id": str(session_id) if session_id is not None else None,
            "webhook_id": str(webhook_id) if webhook_id is not None else None,
            "setup_url": gate_result.setup_url,
            "status": gate_result.status,
            "missing_count": len(gate_result.missing),
        }
