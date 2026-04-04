"""
Security Event Service - Business logic for security event operations.
"""
import json
import uuid
import logging
from sqlmodel import Session, select, func

from app.models.events.security_event import (
    SecurityEvent,
    SecurityEventCreate,
    SecurityEventPublic,
    SecurityEventsPublic,
)

logger = logging.getLogger(__name__)


def _safe_uuid(value: str | None) -> uuid.UUID | None:
    """Parse a UUID string, returning None for None or invalid values."""
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        return None


class SecurityEventService:
    """
    Service for creating and querying security events.

    Security events are created by:
    - SDK-level interceptors detecting credential file access (Phase 1)
    - Output redaction pipeline when a credential value is found in output (Phase 2)

    The /report endpoint is the synchronous blockable path — it must respond quickly.
    The / POST endpoint is fire-and-forget for non-blocking informational events.
    """

    @staticmethod
    def to_public(event: SecurityEvent) -> SecurityEventPublic:
        """Convert a SecurityEvent DB model to its public schema.

        Handles parsing the JSON-encoded details string back to a dict.
        """
        details: dict = {}
        if event.details:
            try:
                details = json.loads(event.details)
            except (json.JSONDecodeError, TypeError):
                details = {}
        return SecurityEventPublic(
            id=event.id,
            created_at=event.created_at,
            user_id=event.user_id,
            agent_id=event.agent_id,
            environment_id=event.environment_id,
            session_id=event.session_id,
            guest_share_id=event.guest_share_id,
            event_type=event.event_type,
            severity=event.severity,
            details=details,
            risk_score=event.risk_score,
        )

    @staticmethod
    async def create_event(
        session: Session,
        user_id: uuid.UUID,
        data: SecurityEventCreate,
    ) -> SecurityEvent:
        """
        Create a security event record.

        Args:
            session: Database session
            user_id: ID of the user whose environment triggered the event
            data: Event data

        Returns:
            Created SecurityEvent instance
        """
        event = SecurityEvent(
            user_id=user_id,
            agent_id=data.agent_id,
            environment_id=data.environment_id,
            session_id=data.session_id,
            guest_share_id=data.guest_share_id,
            event_type=data.event_type,
            severity=data.severity,
            details=json.dumps(data.details),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return event

    @staticmethod
    async def create_event_from_report(
        session: Session,
        user_id: uuid.UUID,
        event_type: str,
        severity: str,
        details: dict,
        tool_name: str | None = None,
        tool_input: str | None = None,
        environment_id: str | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> SecurityEvent:
        """
        Create a security event from a raw report payload (blockable endpoint).

        Normalizes the report data: merges tool_name/tool_input into details
        and parses UUID strings. This keeps payload transformation out of the
        route layer.

        Args:
            session: Database session
            user_id: ID of the user whose environment triggered the event
            event_type: Security event type constant
            severity: Event severity level
            details: Free-form event details
            tool_name: SDK tool name (merged into details)
            tool_input: File path or command string (merged into details)
            environment_id: Environment UUID as string (parsed safely)
            session_id: Session UUID as string (parsed safely)
            agent_id: Agent UUID as string (parsed safely)

        Returns:
            Created SecurityEvent instance
        """
        merged_details = dict(details)
        if tool_name:
            merged_details["tool_name"] = tool_name
        if tool_input:
            merged_details["tool_input"] = tool_input

        create_data = SecurityEventCreate(
            environment_id=_safe_uuid(environment_id),
            session_id=_safe_uuid(session_id),
            agent_id=_safe_uuid(agent_id),
            event_type=event_type,
            severity=severity,
            details=merged_details,
        )

        return await SecurityEventService.create_event(
            session=session,
            user_id=user_id,
            data=create_data,
        )

    @staticmethod
    async def list_events(
        session: Session,
        user_id: uuid.UUID,
        agent_id: uuid.UUID | None = None,
        environment_id: uuid.UUID | None = None,
        session_id_filter: uuid.UUID | None = None,
        event_type: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[SecurityEvent], int]:
        """
        Query security events for a user with optional filters.

        Results are ordered by created_at descending (newest first).

        Args:
            session: Database session
            user_id: Filter to this user's events only
            agent_id: Optional filter by agent
            environment_id: Optional filter by environment
            session_id_filter: Optional filter by session (named to avoid shadowing)
            event_type: Optional filter by event type string
            skip: Pagination offset
            limit: Maximum results to return

        Returns:
            Tuple of (events list, total count)
        """
        base_filter = SecurityEvent.user_id == user_id

        query = select(SecurityEvent).where(base_filter)
        count_query = (
            select(func.count())
            .select_from(SecurityEvent)
            .where(base_filter)
        )

        if agent_id is not None:
            query = query.where(SecurityEvent.agent_id == agent_id)
            count_query = count_query.where(SecurityEvent.agent_id == agent_id)

        if environment_id is not None:
            query = query.where(SecurityEvent.environment_id == environment_id)
            count_query = count_query.where(SecurityEvent.environment_id == environment_id)

        if session_id_filter is not None:
            query = query.where(SecurityEvent.session_id == session_id_filter)
            count_query = count_query.where(SecurityEvent.session_id == session_id_filter)

        if event_type is not None:
            query = query.where(SecurityEvent.event_type == event_type)
            count_query = count_query.where(SecurityEvent.event_type == event_type)

        query = query.order_by(SecurityEvent.created_at.desc()).offset(skip).limit(limit)

        events = list(session.exec(query).all())
        count = session.exec(count_query).one()

        return events, count
