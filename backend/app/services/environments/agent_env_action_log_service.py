"""Agent-environment action-log service.

Thin CRUD over the append-only ``AgentEnvActionLog`` table. All writes are
best-effort at call sites (a logging failure must never abort the lifecycle or
the scheduler poll) — callers wrap ``record`` defensively.
"""

import logging
import uuid

from sqlmodel import Session, desc, func, select

from app.models import AgentEnvActionLog

logger = logging.getLogger(__name__)


class AgentEnvActionLogService:
    """Create and query agent-environment action-log rows."""

    @staticmethod
    def record(
        db_session: Session,
        *,
        environment_id: uuid.UUID,
        agent_id: uuid.UUID,
        action: str,
        status: str,
        cause: str | None = None,
        summary: str | None = None,
        detail: str | None = None,
    ) -> AgentEnvActionLog:
        """Construct + add + commit + refresh an immutable action-log row.

        ``detail`` is operational text only — callers must sanitize anything
        that could carry a secret (e.g. credential-sync errors log the reason,
        never the payload).
        """
        log = AgentEnvActionLog(
            environment_id=environment_id,
            agent_id=agent_id,
            action=action,
            status=status,
            cause=cause,
            summary=summary,
            detail=detail,
        )
        db_session.add(log)
        db_session.commit()
        db_session.refresh(log)
        logger.debug(
            "Recorded env action-log for environment %s: action=%s, status=%s",
            environment_id,
            action,
            status,
        )
        return log

    @staticmethod
    def list_for_environment(
        db_session: Session,
        environment_id: uuid.UUID,
        limit: int = 50,
    ) -> list[AgentEnvActionLog]:
        """Recent action-log rows for an environment, newest first."""
        statement = (
            select(AgentEnvActionLog)
            .where(AgentEnvActionLog.environment_id == environment_id)
            .order_by(desc(AgentEnvActionLog.executed_at))
            .limit(limit)
        )
        return list(db_session.exec(statement).all())

    @staticmethod
    def count_for_environment(
        db_session: Session,
        environment_id: uuid.UUID,
    ) -> int:
        """Total action-log rows for an environment (for the list response count)."""
        statement = select(func.count()).where(
            AgentEnvActionLog.environment_id == environment_id
        )
        return db_session.exec(statement).one()

    @staticmethod
    def latest_critical(
        db_session: Session,
        environment_id: uuid.UUID,
    ) -> AgentEnvActionLog | None:
        """Most recent ``status="error"`` row — the card's primary cause line."""
        statement = (
            select(AgentEnvActionLog)
            .where(
                AgentEnvActionLog.environment_id == environment_id,
                AgentEnvActionLog.status == "error",
            )
            .order_by(desc(AgentEnvActionLog.executed_at))
            .limit(1)
        )
        return db_session.exec(statement).first()
