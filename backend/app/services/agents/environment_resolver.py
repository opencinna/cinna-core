"""
Environment Resolver — shared helpers for resolving and auto-activating an
agent's active environment.

These helpers were originally part of ``AgentSchedulerService`` and are reused
by any feature that needs to run code inside an agent's Docker environment on
behalf of a backend-initiated action (scheduled script triggers, webhook
script triggers, etc.). Keeping them in a dedicated module avoids cross-service
coupling.

Both helpers are static-style functions — no state, no class — to make reuse
explicit and test-friendly.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Callable, TYPE_CHECKING

from sqlmodel import Session as DBSession

if TYPE_CHECKING:
    from app.models import AgentEnvironment

logger = logging.getLogger(__name__)


def get_active_environment(
    session: DBSession,
    agent_id: uuid.UUID,
) -> "AgentEnvironment | None":
    """
    Return the agent's active environment, or None if not configured.

    Args:
        session: Database session
        agent_id: Agent UUID to look up

    Returns:
        AgentEnvironment if the agent has an ``active_environment_id`` set and
        the row exists, otherwise None.
    """
    from app.models import Agent, AgentEnvironment

    agent = session.get(Agent, agent_id)
    if not agent or not agent.active_environment_id:
        return None
    return session.get(AgentEnvironment, agent.active_environment_id)


async def ensure_environment_running(
    environment: "AgentEnvironment",
    get_fresh_db_session: Callable[[], DBSession],
) -> "AgentEnvironment":
    """
    Activate the environment if it is suspended or stopped. Return the running
    environment or raise.

    Reuses activation patterns from ``SessionService`` — suspended → activate,
    stopped → start. Polls every 5 seconds up to 120 seconds for a running
    status.

    The actual activation is performed via the
    ``EnvironmentLifecycleManager`` with a fresh DB session (the passed-in
    ``environment`` may be bound to a request-scoped session that won't
    survive long activations).

    Args:
        environment: AgentEnvironment to activate.
        get_fresh_db_session: Callable returning a DB session context manager
            (used for activation and for polling so we pick up status changes
            made by other processes).

    Returns:
        Running AgentEnvironment (refreshed from DB).

    Raises:
        RuntimeError: If the environment is in an error or unexpected state,
            the agent cannot be fetched, or activation times out after
            120 seconds.
    """
    from app.models import Agent, AgentEnvironment
    from app.services.environments.environment_lifecycle import (
        EnvironmentLifecycleManager,
    )

    status = environment.status
    env_id = environment.id

    if status == "running":
        return environment

    if status == "error":
        raise RuntimeError(
            f"Environment {env_id} is in error state and cannot be activated"
        )

    lifecycle = EnvironmentLifecycleManager()

    if status == "suspended":
        logger.info(f"Activating suspended environment {env_id}")
        with get_fresh_db_session() as fresh_session:
            fresh_env = fresh_session.get(AgentEnvironment, env_id)
            if not fresh_env:
                raise RuntimeError(
                    f"Environment {env_id} disappeared before activation"
                )
            fresh_agent = fresh_session.get(Agent, fresh_env.agent_id)
            if not fresh_agent:
                raise RuntimeError(
                    f"Agent {fresh_env.agent_id} for environment {env_id} not found"
                )
            await lifecycle.activate_suspended_environment(
                db_session=fresh_session,
                environment=fresh_env,
                agent=fresh_agent,
                emit_events=True,
            )
    elif status == "stopped":
        logger.info(f"Starting stopped environment {env_id}")
        with get_fresh_db_session() as fresh_session:
            fresh_env = fresh_session.get(AgentEnvironment, env_id)
            if not fresh_env:
                raise RuntimeError(
                    f"Environment {env_id} disappeared before activation"
                )
            fresh_agent = fresh_session.get(Agent, fresh_env.agent_id)
            if not fresh_agent:
                raise RuntimeError(
                    f"Agent {fresh_env.agent_id} for environment {env_id} not found"
                )
            await lifecycle.start_environment(
                db_session=fresh_session,
                environment=fresh_env,
                agent=fresh_agent,
            )
    elif status in ("activating", "starting"):
        # Another process has already triggered activation — just poll
        logger.info(
            f"Environment {env_id} is already {status}, polling..."
        )
    else:
        raise RuntimeError(
            f"Environment {env_id} is in unexpected state '{status}' — cannot proceed"
        )

    # Poll until running or timeout (120 seconds)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 120
    while loop.time() < deadline:
        await asyncio.sleep(5)
        with get_fresh_db_session() as fresh_session:
            fresh_env = fresh_session.get(AgentEnvironment, env_id)
            if not fresh_env:
                raise RuntimeError(
                    f"Environment {env_id} disappeared during activation"
                )
            if fresh_env.status == "running":
                logger.info(f"Environment {env_id} is now running")
                return fresh_env
            if fresh_env.status == "error":
                raise RuntimeError(
                    f"Environment {env_id} entered error state during activation"
                )
            logger.debug(
                f"Environment {env_id} status={fresh_env.status}, continuing to poll"
            )

    raise RuntimeError(
        f"Environment {env_id} activation timed out after 120 seconds"
    )
