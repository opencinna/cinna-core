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
    from app.models import Agent, AgentEnvironment

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


async def wake_suspended_environment(
    environment: "AgentEnvironment",
    agent: "Agent | None",
    *,
    log_prefix: str = "environment",
) -> None:
    """Wake a *suspended* environment in place, best-effort.

    The light counterpart to :func:`ensure_environment_running`: it activates a
    suspended env and returns, rather than polling to a running state and
    raising. That is what a user-initiated **refresh** wants — a cache pull
    should wake a sleeping agent, but must never turn into a 120-second request
    or a 500 because the container is unhealthy.

    No-op when the env is already running, when no agent is available, or for
    any status other than ``suspended``: ``stopped`` / ``error`` need a heavier
    full start that a refresh button has no business triggering.

    Activation rotates the env auth token, so the refreshed ``status`` /
    ``status_changed_at`` / ``config`` are copied back onto the caller's
    instance — including when a *parallel* request did the activation, which is
    why the copy-back happens unconditionally. ``status_changed_at`` travels
    with ``status`` as a value copy: re-stamping it here would hide the real
    age of the transition from the status-repair reconciler.

    Never raises: a failure leaves the caller on its normal not-running path.
    """
    if agent is None or environment.status != "suspended":
        return

    from app.core.db import create_session
    from app.models import Agent as AgentModel, AgentEnvironment
    from app.services.environments.environment_lifecycle import (
        EnvironmentLifecycleManager,
    )

    try:
        lifecycle = EnvironmentLifecycleManager()
        with create_session() as session:
            fresh_env = session.get(AgentEnvironment, environment.id)
            if fresh_env is None:
                return
            if fresh_env.status == "suspended":
                fresh_agent = session.get(AgentModel, fresh_env.agent_id)
                if fresh_agent is None:
                    return
                logger.info(
                    "%s_resume_environment agent_id=%s env_id=%s action=activating",
                    log_prefix, environment.agent_id, environment.id,
                )
                await lifecycle.activate_suspended_environment(
                    db_session=session,
                    environment=fresh_env,
                    agent=fresh_agent,
                    emit_events=True,
                )
            environment.status = fresh_env.status
            environment.status_changed_at = fresh_env.status_changed_at
            environment.config = fresh_env.config
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning(
            "%s_resume_environment agent_id=%s env_id=%s action=failed reason=%s",
            log_prefix, environment.agent_id, environment.id, exc,
        )


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
