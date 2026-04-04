import logging
import asyncio
from datetime import datetime, UTC
from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import select

from app.core.db import create_session
from app.models.environments.environment import AgentEnvironment
from app.models.agents.agent import Agent
from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager
from app.services.events.event_service import event_service
from app.models.events.event import EventType

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

STATUS_CHECK_INTERVAL_MINUTES = 10


def run_status_check():
    """
    Check all environments that the DB considers 'running' and verify
    that the underlying container is actually alive and healthy.

    If a container has crashed or become unreachable, set the environment
    status to 'error' and notify the owner via WebSocket.
    """
    try:
        asyncio.run(_check_environment_statuses())
    except Exception as e:
        logger.error(f"Environment status check job failed: {e}", exc_info=True)


async def _check_environment_statuses():
    """Async implementation of environment status check."""
    with create_session() as session:
        statement = select(AgentEnvironment).where(AgentEnvironment.status == "running")
        running_envs = session.exec(statement).all()

        if not running_envs:
            logger.debug("No running environments to check status")
            return

        logger.info(f"Checking status of {len(running_envs)} running environments")

        lifecycle_manager = EnvironmentLifecycleManager()

        error_count = 0
        for env in running_envs:
            try:
                adapter = lifecycle_manager.get_adapter(env)
                health = await adapter.health_check()

                # Update last health check timestamp
                env.last_health_check = datetime.now(UTC)

                if health.status != "healthy":
                    # Verify container is actually gone/crashed (not just slow)
                    container_status = await adapter.get_status()

                    if container_status != "running":
                        logger.warning(
                            f"Environment {env.id} is unhealthy: "
                            f"health={health.status}, container={container_status}, "
                            f"message={health.message}"
                        )

                        env.status = "error"
                        env.status_message = (
                            f"Environment became unreachable: {health.message}"
                        )

                        # Get agent for owner_id
                        agent = session.get(Agent, env.agent_id)
                        if agent:
                            await event_service.emit_event(
                                event_type=EventType.ENVIRONMENT_STATUS_CHANGED,
                                model_id=env.id,
                                user_id=agent.owner_id,
                                meta={
                                    "environment_id": str(env.id),
                                    "agent_id": str(agent.id),
                                    "instance_name": env.instance_name,
                                    "status": "error",
                                    "reason": "health_check_failed",
                                    "message": health.message,
                                },
                            )

                        error_count += 1

                session.add(env)
                session.commit()

            except Exception as e:
                logger.error(
                    f"Failed to check status of environment {env.id}: {e}",
                    exc_info=True,
                )
                continue

        if error_count > 0:
            logger.info(
                f"Status check complete: {error_count} environments marked as error"
            )
        else:
            logger.debug("Status check complete: all environments healthy")


def start_scheduler():
    """Start background scheduler for environment status checks (call on app startup)."""
    scheduler.add_job(
        run_status_check,
        "interval",
        minutes=STATUS_CHECK_INTERVAL_MINUTES,
        id="environment_status_check",
    )
    scheduler.start()
    logger.info(
        f"Environment status scheduler started (runs every {STATUS_CHECK_INTERVAL_MINUTES} minutes)"
    )


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Environment status scheduler stopped")
