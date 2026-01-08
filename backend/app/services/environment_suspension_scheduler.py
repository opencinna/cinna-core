import logging
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session, select

from app.core.db import engine
from app.models.environment import AgentEnvironment
from app.models.agent import Agent
from app.services.environment_lifecycle import EnvironmentLifecycleManager
from app.services.event_service import event_service
from app.models.event import EventType

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

# Inactivity threshold: 10 minutes
INACTIVITY_THRESHOLD_MINUTES = 10


def run_suspension_check():
    """
    Check for environments that should be suspended due to inactivity.

    An environment is suspended if:
    1. Status is 'running'
    2. last_activity_at is older than 10 minutes (or None)
    3. User is offline (not connected via WebSocket) OR environment is not active
    """
    try:
        # Run async check in event loop
        asyncio.run(_check_and_suspend_environments())
    except Exception as e:
        logger.error(f"Suspension check job failed: {e}", exc_info=True)


async def _check_and_suspend_environments():
    """Async implementation of suspension check."""
    with Session(engine) as session:
        # Get all running environments
        statement = select(AgentEnvironment).where(AgentEnvironment.status == "running")
        running_envs = session.exec(statement).all()

        if not running_envs:
            logger.debug("No running environments to check for suspension")
            return

        logger.info(f"Checking {len(running_envs)} running environments for suspension")

        threshold_time = datetime.utcnow() - timedelta(minutes=INACTIVITY_THRESHOLD_MINUTES)
        lifecycle_manager = EnvironmentLifecycleManager()

        suspended_count = 0
        for env in running_envs:
            try:
                # Get agent to check owner
                agent = session.get(Agent, env.agent_id)
                if not agent:
                    logger.warning(f"Agent {env.agent_id} not found for environment {env.id}")
                    continue

                # Check if user is online
                user_online = event_service.is_user_online(agent.owner_id)

                # Determine if environment should be suspended
                should_suspend = False

                # Case 1: No activity recorded yet and environment was created more than 10 minutes ago
                if env.last_activity_at is None:
                    if env.created_at < threshold_time:
                        logger.debug(
                            f"Environment {env.id} has no activity and was created {env.created_at}, "
                            f"threshold: {threshold_time}"
                        )
                        should_suspend = True
                # Case 2: Last activity is older than threshold
                elif env.last_activity_at < threshold_time:
                    logger.debug(
                        f"Environment {env.id} last activity: {env.last_activity_at}, "
                        f"threshold: {threshold_time}"
                    )
                    should_suspend = True

                # Don't suspend if user is online and environment is active
                if should_suspend:
                    if user_online and env.is_active:
                        logger.debug(
                            f"Skipping suspension of environment {env.id}: "
                            f"user is online and environment is active"
                        )
                        should_suspend = False

                # Suspend if needed
                if should_suspend:
                    logger.info(
                        f"Suspending environment {env.id} due to inactivity "
                        f"(last_activity: {env.last_activity_at}, user_online: {user_online}, "
                        f"is_active: {env.is_active})"
                    )

                    # Suspend environment
                    await lifecycle_manager.suspend_environment(session, env)

                    # Emit suspension event
                    await event_service.emit_event(
                        event_type=EventType.ENVIRONMENT_SUSPENDED,
                        model_id=env.id,
                        user_id=agent.owner_id,
                        meta={
                            "environment_id": str(env.id),
                            "agent_id": str(agent.id),
                            "instance_name": env.instance_name,
                            "reason": "inactivity"
                        }
                    )

                    suspended_count += 1

            except Exception as e:
                logger.error(f"Failed to suspend environment {env.id}: {e}", exc_info=True)
                continue

        if suspended_count > 0:
            logger.info(f"Suspension check complete: {suspended_count} environments suspended")
        else:
            logger.debug("Suspension check complete: no environments suspended")


def start_scheduler():
    """Start background scheduler for environment suspension checks (call on app startup)."""
    # Run every 10 minutes
    scheduler.add_job(
        run_suspension_check,
        "interval",
        minutes=10,
        id="environment_suspension_check"
    )
    scheduler.start()
    logger.info("Environment suspension scheduler started (runs every 10 minutes)")


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Environment suspension scheduler stopped")
