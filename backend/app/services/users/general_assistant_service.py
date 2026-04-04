"""
General Assistant Service — manages the user's General Assistant agent.

The General Assistant is a special system-created agent that:
- Operates in building mode only
- Has a dedicated env template with pre-loaded platform docs and example scripts
- Is identified by is_general_assistant=True on the Agent record
- One per user, accessible across all workspaces (user_workspace_id=None)
"""
import logging
import threading
from uuid import UUID

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.users.user import User

logger = logging.getLogger(__name__)

GA_AGENT_NAME = "General Assistant"
GA_COLOR_PRESET = "violet"
GA_ENV_TEMPLATE = "general-assistant-env"
GA_BUILDING_PROMPT = """You are the General Assistant for the Workflow Runner platform. Your job is to help users set up, configure, and manage their agentic workflows by interacting with the platform's own API.

## How You Work

You are a building-mode agent. You read documentation, write Python scripts, and execute them to call the platform's backend API. You have:

1. **Feature documentation** in `./knowledge/platform/README.md` — the feature map and discovery guide. Read this first to understand what platform features exist, then navigate into `./knowledge/platform/application/` and `./knowledge/platform/agents/` for detailed business-logic docs on each feature
2. **REST API reference** in `./knowledge/platform/api_reference/` — auto-generated endpoint specs grouped by domain. Read `api_reference/README.md` for the index, then open the relevant file (e.g. `agents.md`, `sessions.md`, `credentials.md`) to see exact endpoints, parameters, request bodies, and response types
3. **Example scripts** in `./scripts/examples/` — working code patterns you can adapt. Read `scripts/examples/README.md` for the index
4. **Environment variables** — `BACKEND_URL` and `AGENT_AUTH_TOKEN` are pre-configured for authenticated API calls

## Your Process

1. **Understand the request** — Ask clarifying questions using AskUserQuestion if the user's requirements are vague
2. **Discover features** — Read `./knowledge/platform/README.md` to identify which platform features are involved, then read the relevant feature docs for context
3. **Look up API specs** — Read the matching files in `./knowledge/platform/api_reference/` to find the exact endpoints, parameters, and request bodies you need
4. **Execute step-by-step** — Write and run scripts for each step, verifying success before proceeding
5. **Report results** — Summarize what was created with IDs and links

## Rules

- ALWAYS read `./knowledge/platform/README.md` before starting any setup task
- ALWAYS read the relevant API reference file in `./knowledge/platform/api_reference/` before writing a script
- ALWAYS check `./scripts/examples/` for existing patterns before writing from scratch
- ALWAYS verify each API call succeeded before proceeding to the next step
- NEVER expose credential values (passwords, tokens, API keys) in your messages
- NEVER attempt to modify your own agent configuration
- When creating agents, use building mode sessions to set up their prompts and scripts
- Report progress after each major step (workspace created, agent created, etc.)"""

GA_DESCRIPTION = (
    "Your platform assistant — helps set up agents, workspaces, and automations "
    "by writing and running scripts against the platform API."
)


class GeneralAssistantService:

    class NotEnabledError(Exception):
        """Raised when GA feature is not enabled for the user."""

    class AlreadyExistsError(Exception):
        """Raised when a GA already exists for the user."""

    @staticmethod
    async def ensure_or_create(session: Session, user: User) -> Agent:
        """
        Create the General Assistant for a user, with pre-condition checks.

        Raises:
            NotEnabledError: If general_assistant_enabled is False on the user.
            AlreadyExistsError: If a GA already exists for this user.
        """
        if not user.general_assistant_enabled:
            raise GeneralAssistantService.NotEnabledError()

        existing = GeneralAssistantService.get_general_assistant(session, user.id)
        if existing:
            raise GeneralAssistantService.AlreadyExistsError()

        return await GeneralAssistantService.create_general_assistant(session, user)

    @staticmethod
    def get_general_assistant(session: Session, user_id: UUID) -> Agent | None:
        """Get the user's General Assistant agent if it exists."""
        statement = select(Agent).where(
            Agent.owner_id == user_id,
            Agent.is_general_assistant == True,  # noqa: E712
        )
        return session.exec(statement).first()

    @staticmethod
    async def create_general_assistant(session: Session, user: User) -> Agent:
        """
        Create the General Assistant agent for a user.

        Creates the agent record, then attempts to build an environment using the
        'general-assistant-env' template. Falls back to the default env template
        if the GA template is not found.
        """
        from app.models.environments.environment import AgentEnvironmentCreate
        from app.services.environments.environment_service import EnvironmentService
        from app.core.config import settings

        # Create the agent record
        agent = Agent(
            name=GA_AGENT_NAME,
            owner_id=user.id,
            user_workspace_id=None,  # GA is workspace-agnostic
            is_general_assistant=True,
            ui_color_preset=GA_COLOR_PRESET,
            show_on_dashboard=True,
            workflow_prompt=GA_BUILDING_PROMPT,
            description=GA_DESCRIPTION,
        )
        session.add(agent)
        session.commit()
        session.refresh(agent)

        # Try GA-specific template first, fall back to default if not found
        for env_name, env_version in [
            (GA_ENV_TEMPLATE, "1.0.0"),
            (settings.DEFAULT_AGENT_ENV_NAME, settings.DEFAULT_AGENT_ENV_VERSION),
        ]:
            try:
                env_data = AgentEnvironmentCreate(
                    env_name=env_name,
                    env_version=env_version,
                    instance_name="Default",
                    type="docker",
                    config={},
                )
                await EnvironmentService.create_environment(
                    session=session,
                    agent_id=agent.id,
                    data=env_data,
                    user=user,
                    auto_start=True,
                )
                logger.info(
                    "Created GA environment for user %s using template '%s'",
                    user.id,
                    env_name,
                )
                break
            except Exception as exc:
                if env_name == GA_ENV_TEMPLATE:
                    logger.warning(
                        "GA template '%s' unavailable, falling back to default: %s",
                        GA_ENV_TEMPLATE,
                        exc,
                    )
                else:
                    logger.error(
                        "Failed to create GA environment for user %s: %s",
                        user.id,
                        exc,
                        exc_info=True,
                    )
                    raise

        session.refresh(agent)
        return agent

    @staticmethod
    async def ensure_general_assistant(session: Session, user: User) -> Agent:
        """
        Idempotent — returns existing GA or creates a new one.
        Used during new user registration to guarantee GA exists.
        """
        existing = GeneralAssistantService.get_general_assistant(session, user.id)
        if existing:
            return existing
        return await GeneralAssistantService.create_general_assistant(session, user)

    @staticmethod
    def trigger_auto_create_background(user_id: UUID) -> None:
        """
        Trigger GA auto-creation in a daemon background thread.
        Safe to call from sync routes and service methods — does not block the caller.
        """
        from app.core.db import engine

        def _create() -> None:
            import asyncio
            from sqlmodel import Session as SQLSession

            try:
                with SQLSession(engine) as db:
                    user_obj = db.get(User, user_id)
                    if user_obj and user_obj.general_assistant_enabled:
                        existing = GeneralAssistantService.get_general_assistant(db, user_id)
                        if not existing:
                            asyncio.run(
                                GeneralAssistantService.create_general_assistant(db, user_obj)
                            )
            except Exception as exc:
                logger.error(
                    "Background GA creation failed for user %s: %s",
                    user_id,
                    exc,
                    exc_info=True,
                )

        threading.Thread(target=_create, daemon=True).start()
