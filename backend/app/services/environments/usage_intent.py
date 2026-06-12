"""Usage-intent handling for agent environments.

A "usage intent" is a signal that a user intends to interact with an agent
environment (e.g. opening a session, viewing a workspace file, clicking the
agent on the dashboard). It bumps ``last_activity_at`` and, when the resolved
environment is suspended, triggers background activation so the environment is
ready by the time the user actually sends a message.

This logic is shared by two entry points:
- the ``agent_usage_intent`` WebSocket handler (``services/events/event_service.py``)
- the ``POST /environments/{id}/usage-intent`` REST route (``api/routes/environments.py``)

Both delegate to :func:`register_usage_intent` so behavior stays identical.
"""

import logging
from datetime import datetime, UTC
from uuid import UUID

from sqlmodel import Session as DBSession

from app.models.agents.agent import Agent
from app.models.environments.environment import AgentEnvironment
from app.utils import create_task_with_error_logging

logger = logging.getLogger(__name__)


async def _activate_async(env_id: UUID, agent_id: UUID) -> bool:
    """Activate a suspended environment in the main event loop with a fresh session.

    Runs as a background task (no detached ORM objects are passed in — only ids,
    re-fetched here). Obtains the lifecycle manager via the
    ``EnvironmentService.get_lifecycle_manager()`` singleton accessor (PROJECT
    INVARIANT — do NOT instantiate ``EnvironmentLifecycleManager`` directly).
    """
    from app.core.db import engine as db_engine
    from app.services.environments.environment_service import EnvironmentService

    with DBSession(db_engine) as fresh_session:
        fresh_env = fresh_session.get(AgentEnvironment, env_id)
        fresh_agent = fresh_session.get(Agent, agent_id)

        if not fresh_env or not fresh_agent:
            logger.error(
                "Environment %s or agent %s not found during usage-intent activation",
                env_id,
                agent_id,
            )
            return False

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        result = await lifecycle_manager.activate_suspended_environment(
            db_session=fresh_session,
            environment=fresh_env,
            agent=fresh_agent,
            emit_events=True,
        )
        logger.info("Background activation completed for environment %s", env_id)
        return result


def register_usage_intent(
    db_session: DBSession,
    user_id: UUID,
    environment_id: UUID,
) -> dict:
    """Record a usage intent for an environment and trigger activation if needed.

    Resolves the passed environment to the agent's *active* environment when the
    two differ (the passed one may be stale/non-active), bumps
    ``last_activity_at`` on the resolved environment, and — if it is suspended —
    schedules background activation.

    NOTE: this function does NOT perform access control. Callers that are not
    already trusted (e.g. the REST route) MUST verify the user may access the
    environment/agent before calling this.

    Args:
        db_session: Active database session.
        user_id: The user signalling intent (for logging only).
        environment_id: The environment the user intends to use.

    Returns:
        Dict with ``status`` ("activating" | "ok"), ``message``, and the resolved
        ``environment_id`` (str).

    Raises:
        ValueError: If the environment or its agent cannot be found.
    """
    environment = db_session.get(AgentEnvironment, environment_id)
    if not environment:
        raise ValueError("Environment not found")

    agent = db_session.get(Agent, environment.agent_id)
    if not agent:
        raise ValueError("Agent not found")

    # Resolve to the agent's active environment when the passed one is not active.
    # The resolved env belongs to the same agent, so it inherits the ownership
    # that the caller (REST route) already access-checked on the requested id.
    # If a future sharing model decouples env-level access from agent ownership,
    # re-validate access on the resolved env here.
    if agent.active_environment_id and agent.active_environment_id != environment.id:
        active_env = db_session.get(AgentEnvironment, agent.active_environment_id)
        if active_env:
            logger.info(
                "Resolving usage intent from non-active environment %s (status=%s) "
                "to active environment %s (status=%s)",
                environment.id,
                environment.status,
                active_env.id,
                active_env.status,
            )
            environment = active_env

    resolved_environment_id = environment.id

    # Bump last activity so suspension schedulers see recent use.
    environment.last_activity_at = datetime.now(UTC)
    db_session.add(environment)
    db_session.commit()

    if environment.status == "suspended":
        logger.info(
            "User %s triggered activation for suspended environment %s",
            user_id,
            resolved_environment_id,
        )
        create_task_with_error_logging(
            _activate_async(resolved_environment_id, agent.id),
            task_name=f"activate_from_usage_intent_{resolved_environment_id}",
        )
        return {
            "status": "activating",
            "message": "Environment activation started",
            "environment_id": str(resolved_environment_id),
        }

    return {
        "status": "ok",
        "message": f"Environment status: {environment.status}",
        "environment_id": str(resolved_environment_id),
    }
