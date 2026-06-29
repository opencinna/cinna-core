from uuid import UUID
import uuid
import asyncio
import logging
from datetime import UTC, datetime
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, func, select
from app.models import Agent, AgentCreate, AgentPublic, AgentUpdate, User, SessionCreate, AgentHandoverConfig, AgentEnvironment, Session as ChatSession, AgentSdkConfig, InputTaskCreate
from app.models.environments.environment import AgentEnvironmentCreate
from app.services.environments.environment_service import EnvironmentService
from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager
from app.services.sessions.session_service import SessionService
from app.services.ai_functions.ai_functions_service import AIFunctionsService
from app.services.tasks.input_task_service import InputTaskService
from app.agents.skills_generator import generate_a2a_skills
from app.core.config import settings
from app.core.db import engine

logger = logging.getLogger(__name__)


def _generate_description_background(
    agent_id: UUID,
    workflow_prompt: str,
    agent_name: str | None,
    user_id: UUID | None = None,
):
    """
    Background task to generate agent description from workflow prompt.

    Runs in a separate thread to avoid blocking the main request.
    Creates its own database session for the update.

    Args:
        agent_id: Agent to update
        workflow_prompt: New workflow prompt to generate description from
        agent_name: Agent name for context
        user_id: Optional user ID for per-user provider routing
    """
    from sqlmodel import Session as SQLSession

    try:
        # Load user and check availability (system or personal key)
        with SQLSession(engine) as db_session:
            from app.models.users.user import User as UserModel
            user = db_session.get(UserModel, user_id) if user_id else None

        if not AIFunctionsService.is_available(user):
            logger.debug("AI functions not available, skipping description generation")
            return

        # Generate description with its own db session
        with SQLSession(engine) as db_session:
            from app.models.users.user import User as UserModel
            user = db_session.get(UserModel, user_id) if user_id else None
            description = AIFunctionsService.generate_description_from_workflow(
                workflow_prompt=workflow_prompt,
                agent_name=agent_name,
                user=user,
                db=db_session,
            )

            agent = db_session.get(Agent, agent_id)
            if agent:
                agent.description = description
                agent.updated_at = datetime.now(UTC)
                db_session.add(agent)
                db_session.commit()
                logger.info(f"Updated agent {agent_id} description: {description[:50]}...")
            else:
                logger.warning(f"Agent {agent_id} not found for description update")

    except Exception as e:
        logger.error(f"Failed to generate description for agent {agent_id}: {e}", exc_info=True)


def _increment_version(version: str) -> str:
    """Increment the patch version of a semantic version string."""
    try:
        parts = version.split(".")
        if len(parts) == 3:
            major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{major}.{minor}.{patch + 1}"
    except (ValueError, IndexError):
        pass
    return "1.0.1"


class CanBuildError(Exception):
    """Raised when a user is not allowed to build/sync an agent.

    Carries a ``reason`` so route/service callers can map it to the right
    HTTP status: ``"not_developer"`` / ``"foreign_install"`` → 403,
    ``"not_accessible"`` → 404 (no existence leak — see plan).
    """

    def __init__(self, reason: str, message: str):
        self.reason = reason  # "not_developer" | "foreign_install" | "not_accessible"
        self.message = message
        super().__init__(message)


class AgentService:
    # ── Building-rights authorization ──────────────────────────────────

    @staticmethod
    def is_foreign_install(agent: Agent) -> bool:
        """True for a consumer (bundle-owned, non-publisher) install.

        Such installs have a publisher-managed, read-only-ish workspace:
        they may be enabled/disabled and run, but cannot be edited, built,
        or synced for local development. Publisher installs
        (``is_publisher_install=True``) and standalone agents
        (``bundle_uuid is None``) stay fully buildable.
        """
        return agent.bundle_uuid is not None and not agent.is_publisher_install

    @staticmethod
    def user_can_access(session: Session, user: User, agent: Agent) -> bool:
        """Whether the user can *see* the agent at all.

        Mirrors the access set ``list_agents`` returns for the user, which is
        always owner-scoped (``Agent.owner_id == user_id``). Foreign-bundle
        installs are themselves per-user owned rows
        (each carries the consuming user's ``owner_id``), so ownership alone
        fully captures the access set — there is no cross-tenant visibility.

        Used by the account-CLI listing and the mint-target access check
        (which 404s rather than leaking existence).
        """
        return agent.owner_id == user.id

    @staticmethod
    def can_build(session: Session, user: User, agent: Agent) -> bool:
        """One predicate: may this user build/sync this agent?

        ``can_build := is_developer(user) AND not is_foreign_install(agent)
        AND user_can_access(user, agent)``.

        This is the single authorization gate for the account-CLI mint
        endpoint, the per-agent CLI setup-token route, and the ``can_build``
        flag in the accessible-agents listing.
        """
        from app.services.users.role_service import RoleService

        return (
            RoleService.is_developer(user)
            and not AgentService.is_foreign_install(agent)
            and AgentService.user_can_access(session, user, agent)
        )

    @staticmethod
    def assert_can_build(session: Session, user: User, agent: Agent) -> None:
        """Raise ``CanBuildError`` if the user cannot build/sync the agent.

        Access is checked first so an inaccessible agent raises
        ``"not_accessible"`` (404) without leaking why; then role, then the
        foreign-install guard.
        """
        from app.services.users.role_service import RoleService

        if not AgentService.user_can_access(session, user, agent):
            raise CanBuildError("not_accessible", "Agent not found")
        if not RoleService.is_developer(user):
            raise CanBuildError(
                "not_developer",
                "This action requires the agent-developer role.",
            )
        if AgentService.is_foreign_install(agent):
            raise CanBuildError(
                "foreign_install",
                "This is an installed bundle; its workspace is "
                "publisher-managed and can't be synced for local development.",
            )

    @staticmethod
    def compute_capability_flags(
        session: Session, agent_ids: list[UUID]
    ) -> dict[UUID, dict[str, bool]]:
        """Batched lookup of active integration flags for a set of agents.

        Returns a mapping ``agent_id -> {has_email_integration, has_mcp_connectors,
        has_webhooks, git_versioning_enabled}``. Only *enabled/active* integrations
        count. Agents with no matching rows are absent from the per-capability sets
        and default to False.

        Single grouped query per capability keeps the agents-list endpoint off the
        N+1 path.
        """
        from app.models.email.agent_email_integration import AgentEmailIntegration
        from app.models.mcp.mcp_connector import MCPConnector
        from app.models.agents.agent_webhook import AgentWebhook
        from app.models.bundles.agent_git_source import AgentGitSource

        if not agent_ids:
            return {}

        email_ids = set(
            session.exec(
                select(AgentEmailIntegration.agent_id).where(
                    AgentEmailIntegration.agent_id.in_(agent_ids),
                    AgentEmailIntegration.enabled == True,  # noqa: E712
                )
            ).all()
        )
        mcp_ids = set(
            session.exec(
                select(MCPConnector.agent_id).where(
                    MCPConnector.agent_id.in_(agent_ids),
                    MCPConnector.is_active == True,  # noqa: E712
                )
            ).all()
        )
        webhook_ids = set(
            session.exec(
                select(AgentWebhook.agent_id).where(
                    AgentWebhook.agent_id.in_(agent_ids),
                    AgentWebhook.enabled == True,  # noqa: E712
                )
            ).all()
        )
        # Git versioning is "enabled" when an AgentGitSource row exists (presence
        # is the source of truth — connect creates it, disconnect deletes it).
        git_ids = set(
            session.exec(
                select(AgentGitSource.agent_id).where(
                    AgentGitSource.agent_id.in_(agent_ids),
                )
            ).all()
        )

        return {
            aid: {
                "has_email_integration": aid in email_ids,
                "has_mcp_connectors": aid in mcp_ids,
                "has_webhooks": aid in webhook_ids,
                "git_versioning_enabled": aid in git_ids,
            }
            for aid in agent_ids
        }

    @staticmethod
    def to_public_with_clone_info(
        session: Session,
        agent: Agent,
        capabilities: dict[str, bool] | None = None,
    ) -> AgentPublic:
        """Convert Agent to AgentPublic with resolved bundle information.

        The legacy method name is kept temporarily for callers; the body now
        resolves the new ``installed_revision_number`` for installs of
        published bundles.

        ``capabilities`` may carry precomputed integration flags (see
        ``compute_capability_flags``) to avoid per-agent queries in list views;
        when omitted they are computed for this single agent.
        """
        from app.models.bundles.agent_bundle_revision import AgentBundleRevision

        installed_revision_number: int | None = None
        installed_revision_version: str | None = None
        if agent.installed_revision_id:
            rev = session.get(AgentBundleRevision, agent.installed_revision_id)
            if rev:
                installed_revision_number = rev.revision_number
                installed_revision_version = rev.version

        if capabilities is None:
            capabilities = AgentService.compute_capability_flags(
                session, [agent.id]
            ).get(agent.id, {})

        return AgentPublic(
            id=agent.id,
            name=agent.name,
            description=agent.description,
            workflow_prompt=agent.workflow_prompt,
            entrypoint_prompt=agent.entrypoint_prompt,
            refiner_prompt=agent.refiner_prompt,
            router_trigger_prompt=agent.router_trigger_prompt,
            status_refresh_command=agent.status_refresh_command,
            is_active=agent.is_active,
            active_environment_id=agent.active_environment_id,
            ui_color_preset=agent.ui_color_preset,
            show_on_dashboard=agent.show_on_dashboard,
            conversation_mode_ui=agent.conversation_mode_ui,
            agent_sdk_config=agent.agent_sdk_config,
            a2a_config=agent.a2a_config,
            example_prompts=agent.example_prompts,
            inactivity_period_limit=agent.inactivity_period_limit,
            webapp_enabled=agent.webapp_enabled,
            agent_api_enabled=agent.agent_api_enabled,
            agent_api_identity_enabled=agent.agent_api_identity_enabled,
            has_email_integration=capabilities.get("has_email_integration", False),
            has_mcp_connectors=capabilities.get("has_mcp_connectors", False),
            has_webhooks=capabilities.get("has_webhooks", False),
            git_versioning_enabled=capabilities.get("git_versioning_enabled", False),
            created_at=agent.created_at,
            updated_at=agent.updated_at,
            owner_id=agent.owner_id,
            user_workspace_id=agent.user_workspace_id,
            bundle_id=agent.bundle_id,
            bundle_uuid=agent.bundle_uuid,
            installed_revision_id=agent.installed_revision_id,
            installed_revision_number=installed_revision_number,
            installed_revision_version=installed_revision_version,
            is_publisher_install=agent.is_publisher_install,
            update_mode=agent.update_mode,
            pending_update=agent.pending_update,
            pending_update_at=agent.pending_update_at,
            last_sync_at=agent.last_sync_at,
            last_update_status=agent.last_update_status,
            publish_settings=agent.publish_settings or {},
        )

    @staticmethod
    def list_agents(
        session: Session,
        user_id: UUID,
        skip: int = 0,
        limit: int = 100,
        workspace_filter: UUID | None = None,
        apply_workspace_filter: bool = False,
    ) -> tuple[list[Agent], int]:
        """
        List agents for a user.

        All users (including superusers) only see their own agents.

        Args:
            session: Database session
            user_id: User ID to filter agents by owner
            skip: Number of records to skip
            limit: Maximum number of records to return
            workspace_filter: Workspace UUID to filter by (None means default workspace)
            apply_workspace_filter: Whether to apply the workspace filter

        Returns:
            Tuple of (list of agents, total count)
        """
        from sqlalchemy import and_, func, or_

        count_statement = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.owner_id == user_id)
        )
        statement = (
            select(Agent)
            .where(Agent.owner_id == user_id)
        )

        if apply_workspace_filter:
            # Include agents in the requested workspace, plus foreign-bundle
            # installs (workspace-agnostic — they have user_workspace_id=None
            # and bundle_uuid set, but is_publisher_install is False). Plain
            # default-workspace agents are NOT shown in non-default views.
            foreign_install_condition = and_(
                Agent.bundle_uuid.is_not(None),
                Agent.is_publisher_install == False,
                Agent.user_workspace_id.is_(None),
            )
            workspace_condition = or_(
                Agent.user_workspace_id == workspace_filter,
                foreign_install_condition,
            )
            count_statement = count_statement.where(workspace_condition)
            statement = statement.where(workspace_condition)

        count = session.exec(count_statement).one()
        agents = session.exec(statement.offset(skip).limit(limit)).all()

        return list(agents), count

    @staticmethod
    def _enforce_agent_creation_limit(*, session: Session, user: User) -> None:
        """Raise ValueError if ``user`` is at/over their agent-creation cap.

        - superuser → unlimited (returns immediately)
        - else limit = AGENT_LIMIT_CONFIRMED if user.email_confirmed
                       else AGENT_LIMIT_UNCONFIRMED

        Counts user-CREATED standalone agents only:
        ``owner_id == user.id AND bundle_uuid IS NULL AND
        is_publisher_install == False``. Bundle installs (consumer copies of
        others' work) do NOT count (D8) — the anti-abuse concern is users
        spinning up many original agents, each with its own env/mailbox.
        """
        if user.is_superuser:
            return
        limit = (
            settings.AGENT_LIMIT_CONFIRMED
            if user.email_confirmed
            else settings.AGENT_LIMIT_UNCONFIRMED
        )
        count = session.exec(
            select(func.count())
            .select_from(Agent)
            .where(
                Agent.owner_id == user.id,
                Agent.bundle_uuid.is_(None),  # type: ignore[union-attr]
                Agent.is_publisher_install == False,  # noqa: E712
            )
        ).one()
        if count >= limit:
            if user.email_confirmed:
                raise ValueError(f"Agent limit reached ({limit}).")
            raise ValueError(
                f"Agent limit reached ({limit}). "
                f"Confirm your email to raise the limit to "
                f"{settings.AGENT_LIMIT_CONFIRMED}."
            )

    @staticmethod
    async def create_agent(session: Session, user_id: UUID, data: AgentCreate, user: User) -> Agent:
        """Create new agent with default environment"""
        AgentService._enforce_agent_creation_limit(session=session, user=user)
        from app.services.bundles.bundle_id_service import BundleIdService

        # Generate the agent UUID up front so the auto-generated bundle_id can
        # be derived from it before the row is inserted (the column is NOT NULL).
        agent_id = uuid.uuid4()
        bundle_id = BundleIdService.generate_bundle_id(agent_id)
        agent = Agent.model_validate(
            data,
            update={"owner_id": user_id, "id": agent_id, "bundle_id": bundle_id},
        )
        session.add(agent)
        session.commit()
        session.refresh(agent)

        # Create default environment for the agent
        # auto_start=True means it will automatically start and activate after build completes
        default_env_data = AgentEnvironmentCreate(
            env_name=settings.DEFAULT_AGENT_ENV_NAME,
            env_version=settings.DEFAULT_AGENT_ENV_VERSION,
            instance_name="Default",
            type="docker",
            config={}
        )
        default_env = await EnvironmentService.create_environment(
            session=session,
            agent_id=agent.id,
            data=default_env_data,
            user=user,
            auto_start=True  # Automatically start after build completes
        )

        # Note: Environment will be in "creating" status initially
        # The background task will build it and then auto-start/activate
        # UI can poll GET /environments/{id} to track progress

        # Refresh agent to get updated state
        session.refresh(agent)
        return agent

    @staticmethod
    def get_agent_with_environment(session: Session, agent_id: UUID) -> Agent | None:
        """Get agent with active environment details"""
        statement = select(Agent).where(Agent.id == agent_id)
        return session.exec(statement).first()

    @staticmethod
    def handle_workflow_prompt_change(
        agent: Agent,
        new_workflow_prompt: str,
        trigger_description_update: bool = True,
        user_id: UUID | None = None,
    ) -> None:
        """
        Handle workflow_prompt change - regenerate A2A skills and trigger description update.

        All LLM-powered operations (A2A skills, description) run in a background thread
        to avoid blocking the asyncio event loop. A2A skills are only regenerated if the
        agent already has A2A configured (has existing skills).

        Args:
            agent: The agent being updated (used to read current state, not modified)
            new_workflow_prompt: The new workflow prompt value
            trigger_description_update: If True, triggers background description generation
            user_id: Optional user ID for per-user AI provider routing
        """
        import threading

        # Run LLM-powered regeneration in a background thread to avoid blocking
        # the asyncio event loop (these are synchronous LLM calls that can take
        # seconds, especially when providers hit rate limits and cascade).
        agent_id = agent.id
        agent_name = agent.name
        # Only regenerate A2A skills if A2A is enabled for this agent.
        # This avoids wasting LLM tokens on agents that don't use A2A.
        a2a_enabled = bool(
            agent.a2a_config and agent.a2a_config.get("enabled", False)
        )
        current_version = agent.a2a_config.get("version", "1.0.0") if agent.a2a_config else "1.0.0"

        def _regenerate_in_background():
            from sqlmodel import Session as SQLSession

            # Generate A2A skills (only if agent already has A2A configured)
            if a2a_enabled:
                try:
                    new_skills = generate_a2a_skills(new_workflow_prompt)
                    with SQLSession(engine) as db:
                        agent_db = db.get(Agent, agent_id)
                        if agent_db:
                            # Preserve existing config fields (e.g., "enabled")
                            updated_config = dict(agent_db.a2a_config or {})
                            updated_config.update({
                                "skills": new_skills,
                                "version": _increment_version(current_version),
                                "generated_at": datetime.now(UTC).isoformat()
                            })
                            agent_db.a2a_config = updated_config
                            flag_modified(agent_db, "a2a_config")
                            db.add(agent_db)
                            db.commit()
                            logger.info(f"Regenerated A2A skills for agent {agent_id}: {len(new_skills)} skills")
                except Exception as e:
                    logger.warning(f"Failed to generate A2A skills for agent {agent_id}: {e}")
            else:
                logger.debug(f"Skipping A2A skills generation for agent {agent_id}: no existing A2A config")

            # Generate description
            if trigger_description_update:
                _generate_description_background(
                    agent_id, new_workflow_prompt, agent_name, user_id=user_id
                )

        thread = threading.Thread(
            target=_regenerate_in_background,
            daemon=True
        )
        thread.start()
        logger.info(f"Triggered background regeneration for agent {agent_id} (a2a_skills={a2a_enabled}, description={trigger_description_update})")

    @staticmethod
    async def update_agent(
        session: Session,
        agent_id: UUID,
        data: AgentUpdate,
        user_id: UUID | None = None,
    ) -> Agent | None:
        """Update agent and sync prompts to active environment if changed."""
        from app.models import AgentEnvironment
        from app.services.environments.environment_service import EnvironmentService

        agent = session.get(Agent, agent_id)
        if not agent:
            return None

        update_dict = data.model_dump(exclude_unset=True)

        # Handle workflow_prompt change with unified method
        if "workflow_prompt" in update_dict and update_dict["workflow_prompt"] != agent.workflow_prompt:
            AgentService.handle_workflow_prompt_change(
                agent=agent,
                new_workflow_prompt=update_dict["workflow_prompt"],
                trigger_description_update=True,
                user_id=user_id,
            )

        agent.sqlmodel_update(update_dict)

        # Bump the per-prompt logical clock for each prompt field that actually
        # changed in this update. This is the DB-side LWW tiebreaker for the
        # prompt-sync reconcile — a UI edit must out-rank a stale env mtime.
        prompt_fields = {"workflow_prompt", "entrypoint_prompt", "refiner_prompt"}
        now = datetime.now(UTC)
        changed_prompt_fields = update_dict.keys() & prompt_fields
        for field in changed_prompt_fields:
            setattr(agent, f"{field}_updated_at", now)

        session.add(agent)
        session.commit()
        session.refresh(agent)

        # An explicit UI save is unambiguous DB intent → force-push to the active
        # environment and reset the baselines so a concurrent env edit is
        # intentionally overridden (and not echoed back on the next reconcile).
        if changed_prompt_fields and agent.active_environment_id:
            environment = session.get(AgentEnvironment, agent.active_environment_id)
            if environment and environment.status == "running":
                try:
                    await EnvironmentService.sync_agent_prompts_to_environment(
                        environment=environment,
                        workflow_prompt=agent.workflow_prompt,
                        entrypoint_prompt=agent.entrypoint_prompt,
                        refiner_prompt=agent.refiner_prompt,
                        session=session,
                    )
                except Exception as e:
                    logger.warning(f"Failed to sync prompts to environment after agent update: {e}")

        # Propagate ``router_trigger_prompt`` changes to the install's
        # auto-managed App MCP route. The focused
        # ``PATCH /agents/{id}/router-trigger-prompt`` endpoint already
        # does this; mirror the behaviour here so the generic
        # ``PUT /agents/{id}`` path stays consistent — otherwise a
        # publisher edit via the standard Edit form silently fails to
        # reach the router until the next apply-update.
        if "router_trigger_prompt" in update_dict:
            try:
                from app.services.app_mcp.app_agent_route_service import (
                    AppAgentRouteService,
                )

                AppAgentRouteService.sync_router_trigger_prompt_from_agent(
                    db_session=session, agent=agent,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.warning(
                    "Failed to sync router_trigger_prompt to auto-managed route "
                    "for agent %s after PUT: %s",
                    agent.id, exc,
                )

        return agent

    @staticmethod
    def set_active_environment(session: Session, agent_id: UUID, env_id: UUID) -> Agent | None:
        """Set active environment for agent"""
        agent = session.get(Agent, agent_id)
        if not agent:
            return None

        agent.active_environment_id = env_id
        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent

    @staticmethod
    async def delete_agent(session: Session, agent_id: UUID) -> bool:
        """
        Delete agent and cleanup all associated resources.

        Steps:
        1. Mark the user's app-data volume orphaned (Phase 2 — preserves
           per-user data across uninstall/reinstall cycles).
        2. Delete all of the agent's sessions (env->session FK is SET NULL, so
           sessions would otherwise linger orphaned after their environments
           are cascade-deleted). Messages cascade from sessions.
        3. Get all environments for the agent
        4. Delete each environment (stops containers, cleans up Docker resources)
           - EnvironmentService.delete_environment handles clearing active_environment_id
        5. Delete agent
        """
        from app.services.bundles.app_data_service import AppDataService

        agent = session.get(Agent, agent_id)
        if not agent:
            return False

        # Mark the per-user app-data volume orphaned BEFORE deleting the row.
        # ``AppDataService.wipe_volume`` requires ``is_orphaned=true``, and
        # the volume is keyed on (user_id, bundle_id, catalog_type) so a
        # future reinstall from the same source reattaches automatically.
        #
        # Slot selection (must match ``_resolve_app_data_host_path``):
        #   - consumer install (``bundle_uuid != NULL`` AND
        #     ``is_publisher_install=False``) → ``"server"`` slot
        #   - publisher install / unpublished standalone agent → NULL slot
        # We MUST scope by ``catalog_type`` — both slots can coexist for
        # the same (user, bundle), and orphaning the wrong one would strand
        # the wrong data.
        try:
            volume_catalog_type = agent.app_data_catalog_type
            volume = AppDataService.get_by_user_bundle(
                session,
                agent.owner_id,
                agent.bundle_id,
                catalog_type=volume_catalog_type,
            )
            if volume is None:
                # Lookup miss after deletion is unusual — the volume should
                # have been created on first env activation. Log a breadcrumb
                # so on-call has something to grep if a user reports lost
                # app-data after deleting an install.
                logger.info(
                    "No app-data volume found for agent %s (bundle_id=%s, "
                    "catalog_type=%r) during delete — nothing to orphan",
                    agent_id, agent.bundle_id, volume_catalog_type,
                )
            elif not volume.is_orphaned:
                AppDataService.mark_orphaned(session, volume)
        except Exception as e:
            logger.warning(
                f"Failed to mark app-data volume orphaned for agent {agent_id}: {e}"
            )

        # Delete sessions tied to this agent before deleting its environments.
        # env->session FK is ON DELETE SET NULL (so individual env deletion
        # detaches sessions rather than wiping them); when the whole agent
        # goes away we still want its sessions gone.
        session_stmt = select(ChatSession).where(ChatSession.agent_id == agent_id)
        agent_sessions = session.exec(session_stmt).all()
        for chat_session in agent_sessions:
            session.delete(chat_session)
        if agent_sessions:
            session.commit()
            logger.info(f"Deleted {len(agent_sessions)} sessions for agent {agent_id}")

        # Get all environments for this agent
        environments = EnvironmentService.list_agent_environments(session, agent_id)

        # Delete each environment (this properly cleans up Docker resources)
        # delete_environment() will automatically clear active_environment_id if needed
        for env in environments:
            try:
                await EnvironmentService.delete_environment(session, env.id)
            except Exception as e:
                logger.warning(f"Failed to delete environment {env.id}: {e}")

        # Delete agent (DB cascades will handle any remaining records)
        session.delete(agent)
        session.commit()
        return True

    @staticmethod
    async def create_agent_flow(
        session: Session,
        user: User,
        description: str,
        mode: str,
        auto_create_session: bool = False,
        user_workspace_id: UUID | None = None,
        agent_sdk_conversation: str | None = None,
        agent_sdk_building: str | None = None,
        env_name: str | None = None,
        model_override_conversation: str | None = None,
        model_override_building: str | None = None,
        use_default_ai_credentials: bool = True,
        conversation_ai_credential_id: UUID | None = None,
        building_ai_credential_id: UUID | None = None,
    ):
        """
        Create full agent flow: agent + environment + (optionally) session
        This is an async generator that yields progress updates

        Args:
            auto_create_session: If True, automatically create session after environment is ready.
                               If False, stop after environment is ready (for credential sharing).
            agent_sdk_conversation: SDK to use for conversation mode (e.g., "claude-code/anthropic")
            agent_sdk_building: SDK to use for building mode
            env_name: Environment template name; falls back to settings.DEFAULT_AGENT_ENV_NAME when None.
            model_override_conversation: Optional per-mode model override (e.g., "claude-haiku-4-5").
            model_override_building: Optional per-mode model override.
            use_default_ai_credentials: When True (default), the environment uses the user's
                account-default AI credentials; when False, the explicit credential IDs below pin
                specific credentials.
            conversation_ai_credential_id: Optional explicit AI credential UUID for conversation mode.
            building_ai_credential_id: Optional explicit AI credential UUID for building mode.
        """
        agent = None
        environment = None

        try:
            # Enforce the agent-creation cap before any work (mirrors
            # create_agent). The route also checks this up front so the
            # client gets a clean 4xx instead of a mid-stream error.
            AgentService._enforce_agent_creation_limit(session=session, user=user)

            # Step 1: Create agent from description
            yield {
                "step": "creating_agent",
                "message": "Generating agent configuration...",
                "current_step": "create_agent"
            }

            # Generate agent name, entrypoint_prompt, and workflow_prompt from description using LLM
            if AIFunctionsService.is_available(user):
                try:
                    config = AIFunctionsService.generate_agent_configuration(
                        description, user=user, db=session
                    )
                    agent_name = config.get("name", f"Agent: {description[:30]}...")
                    entrypoint_prompt = config.get("entrypoint_prompt", description)
                    workflow_prompt = config.get("workflow_prompt", f"You are an AI agent designed to: {description}")
                except Exception as e:
                    # Fallback to simple logic if LLM fails
                    agent_number = len(session.exec(select(Agent).where(Agent.owner_id == user.id)).all()) + 1
                    agent_name = f"Agent #{agent_number}"
                    entrypoint_prompt = description
                    workflow_prompt = f"You are an AI agent designed to: {description}"
            else:
                # Use simple logic when AI functions not available
                agent_number = len(session.exec(select(Agent).where(Agent.owner_id == user.id)).all()) + 1
                agent_name = f"Agent #{agent_number}"
                entrypoint_prompt = description
                workflow_prompt = f"You are an AI agent designed to: {description}"

            agent_data = AgentCreate(
                name=agent_name,
                description=description,
                workflow_prompt=workflow_prompt,
                entrypoint_prompt=entrypoint_prompt,
                user_workspace_id=user_workspace_id,
            )

            from app.services.bundles.bundle_id_service import BundleIdService
            agent_id = uuid.uuid4()
            bundle_id = BundleIdService.generate_bundle_id(agent_id)
            agent = Agent.model_validate(
                agent_data,
                update={"owner_id": user.id, "id": agent_id, "bundle_id": bundle_id},
            )
            session.add(agent)
            session.commit()
            session.refresh(agent)

            yield {
                "step": "agent_created",
                "message": f"Agent '{agent_name}' created successfully",
                "agent_id": str(agent.id),
                "current_step": "create_agent"
            }

            # Step 2: Create and start default environment
            yield {
                "step": "environment_starting",
                "message": "Building default environment...",
                "current_step": "start_environment"
            }

            default_env_data = AgentEnvironmentCreate(
                env_name=env_name or settings.DEFAULT_AGENT_ENV_NAME,
                env_version=settings.DEFAULT_AGENT_ENV_VERSION,
                instance_name="Default",
                type="docker",
                config={},
                agent_sdk_conversation=agent_sdk_conversation,
                agent_sdk_building=agent_sdk_building,
                model_override_conversation=model_override_conversation,
                model_override_building=model_override_building,
                use_default_ai_credentials=use_default_ai_credentials,
                conversation_ai_credential_id=conversation_ai_credential_id,
                building_ai_credential_id=building_ai_credential_id,
            )

            environment = await EnvironmentService.create_environment(
                session=session,
                agent_id=agent.id,
                data=default_env_data,
                user=user,
                auto_start=True
            )

            # Wait for environment to be ready (poll status)
            max_wait_time = 300  # 5 minutes
            poll_interval = 2  # 2 seconds
            elapsed_time = 0

            while elapsed_time < max_wait_time:
                session.refresh(environment)

                if environment.status == "running":
                    # Set agent's active environment
                    agent.active_environment_id = environment.id
                    session.add(agent)
                    session.commit()

                    yield {
                        "step": "environment_ready",
                        "message": "Environment is ready",
                        "agent_id": str(agent.id),
                        "environment_id": str(environment.id),
                        "current_step": "start_environment"
                    }
                    break
                elif environment.status == "error":
                    raise Exception(f"Environment failed to start: {environment.status_message}")
                else:
                    yield {
                        "step": "environment_starting",
                        "message": f"Environment status: {environment.status}...",
                        "current_step": "start_environment"
                    }

                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval
            else:
                raise Exception("Environment failed to start within timeout")

            # If auto_create_session is False, stop here (for credential sharing)
            if not auto_create_session:
                yield {
                    "step": "completed",
                    "message": "Agent and environment created successfully. Ready for credential sharing.",
                    "agent_id": str(agent.id),
                    "environment_id": str(environment.id),
                    "current_step": "redirect"
                }
                return

            # Step 3: Create session (only if auto_create_session is True)
            yield {
                "step": "session_creating",
                "message": "Creating conversation session...",
                "current_step": "create_session"
            }

            # Create session
            session_data = SessionCreate(
                agent_id=agent.id,
                mode=mode,
                title=None
            )

            new_session = SessionService.create_session(
                db_session=session,
                user_id=user.id,
                data=session_data
            )

            if not new_session:
                raise Exception("Failed to create session")

            yield {
                "step": "session_created",
                "message": "Session created successfully",
                "session_id": str(new_session.id),
                "current_step": "create_session"
            }

            # Step 4: Complete
            yield {
                "step": "completed",
                "message": "Agent creation completed",
                "agent_id": str(agent.id),
                "environment_id": str(environment.id),
                "session_id": str(new_session.id),
                "current_step": "redirect"
            }

        except Exception as e:
            yield {
                "step": "error",
                "message": str(e),
                "current_step": "create_agent" if not agent else ("start_environment" if not environment else "create_session")
            }

    @staticmethod
    async def sync_agent_handover_config(session: Session, agent_id: UUID) -> None:
        """
        Sync handover configuration to agent-env.

        Called after creating, updating, or deleting handover configs.
        Queries all enabled handovers for the agent, formats them, and pushes
        the configuration to the agent's active environment.

        Args:
            session: Database session
            agent_id: UUID of the agent
        """
        # Get agent with active environment
        agent = AgentService.get_agent_with_environment(session=session, agent_id=agent_id)
        if not agent or not agent.active_environment_id:
            logger.warning(f"Agent {agent_id} has no active environment, skipping handover sync")
            return

        environment_id = agent.active_environment_id

        # Get all enabled handovers for this agent
        handover_configs = session.exec(
            select(AgentHandoverConfig)
            .where(AgentHandoverConfig.source_agent_id == agent_id)
            .where(AgentHandoverConfig.enabled == True)
        ).all()

        # Format handovers for agent-env
        handovers_list = []
        for config in handover_configs:
            handovers_list.append({
                "id": str(config.target_agent_id),
                "name": config.target_agent.name,
                "prompt": config.handover_prompt
            })

        # Generate overall task creation prompt (includes both handover and inbox task modes)
        handover_prompt = (
            "## TASK CREATION INSTRUCTIONS\n\n"
            "You have the `create_agent_task` tool available in this conversation. "
            "This tool allows you to create tasks in two modes:\n\n"
        )

        # Direct handover section (only if handovers are configured)
        if handovers_list:
            handover_prompt += (
                "### 1. Direct Handover (to configured agents)\n\n"
                "When you complete a task that matches the trigger conditions below, "
                "you MUST immediately call the tool IN THE SAME RESPONSE - do not wait, do not ask for permission.\n\n"
                "**CONFIGURED HANDOVERS:**\n"
            )

            for h in handovers_list:
                handover_prompt += f"\n**→ {h['name']}** (ID: {h['id']})\n{h['prompt']}\n"

            handover_prompt += (
                "\n**How to execute direct handover:**\n"
                "Call `create_agent_task` with:\n"
                "- `task_message`: The context message as specified in the instructions above\n"
                "- `target_agent_id`: UUID of the target agent (shown above)\n"
                "- `target_agent_name`: Name of the target agent (shown above)\n\n"
            )

        # Inbox task section (always available)
        handover_prompt += (
            "### " + ("2. " if handovers_list else "1. ") + "Inbox Task (for user review)\n\n"
            "When you identify work that needs human decision on how to proceed, "
            "or when the appropriate agent is not clear, create an inbox task.\n\n"
            "**When to use inbox tasks:**\n"
            "- Work that requires human judgment on approach\n"
            "- Tasks where agent selection needs user input\n"
            "- Follow-up work identified during current task execution\n"
            "- Complex tasks that need user refinement before execution\n\n"
            "**How to create an inbox task:**\n"
            "Call `create_agent_task` with ONLY:\n"
            "- `task_message`: Clear description of the task/work item\n\n"
            "Do NOT provide `target_agent_id` or `target_agent_name` - the user will select the agent.\n"
            "The task will appear in the user's inbox where they can:\n"
            "- Review and refine the task description\n"
            "- Select an appropriate agent\n"
            "- Execute when ready\n"
        )

        # Add feedback handling instructions (for receiving sub-task feedback)
        handover_prompt += (
            "\n### Handling Sub-Task Feedback\n\n"
            "When a sub-task reports back, you receive a message prefixed with:\n"
            "- `[Sub-task completed]` - Acknowledge the result, inform the user if all tasks are done\n"
            "- `[Sub-task needs input]` - Call `respond_to_task(task_id, message)` with your answer\n"
            "- `[Sub-task error]` - Decide whether to retry or inform the user\n\n"
            "The message metadata contains `task_id` for use with the `respond_to_task` tool.\n"
        )

        # Get environment adapter and sync config
        try:
            environment = session.get(AgentEnvironment, environment_id)
            if environment:
                lifecycle_manager = EnvironmentLifecycleManager()
                adapter = lifecycle_manager.get_adapter(environment)
                await adapter.set_agent_handover_config(
                    handovers=handovers_list,
                    handover_prompt=handover_prompt
                )
                logger.info(f"Synced {len(handovers_list)} handover(s) to environment {environment_id}")
        except Exception as e:
            logger.error(f"Failed to sync handover config to environment: {e}")

    @staticmethod
    async def create_agent_task(
        session: Session,
        user: User,
        task_message: str,
        source_session_id: UUID,
        target_agent_id: UUID | None = None,
        target_agent_name: str | None = None,
    ) -> tuple[bool, UUID | None, UUID | None, str | None]:
        """
        Create a task from an agent.

        If target_agent_id is provided: Direct handover (existing behavior)
        - Validates target agent exists and user has access
        - Creates InputTask with auto-refine (via InputTaskService)
        - Executes task - creates session and sends message (via InputTaskService)
        - Logs system message in source session about task creation

        If target_agent_id is None: Inbox task (new behavior)
        - Creates InputTask without agent selection
        - Does NOT auto-refine (user will refine manually)
        - Does NOT execute (user will select agent and execute)
        - Logs system message in source session about task creation

        Args:
            session: Database session
            user: User executing the task creation
            task_message: Message/description for the task
            source_session_id: Source session UUID (for logging)
            target_agent_id: Target agent UUID (optional - if None, creates inbox task)
            target_agent_name: Target agent name (optional - required if target_agent_id provided)

        Returns:
            Tuple of (success: bool, task_id: UUID | None, session_id: UUID | None, error: str | None)
            - session_id is None for inbox tasks (no auto-execute)
        """
        from app.services.sessions.message_service import MessageService

        try:
            # Get source session to inherit workspace
            source_session = session.get(ChatSession, source_session_id)
            user_workspace_id = source_session.user_workspace_id if source_session else None

            if target_agent_id:
                # DIRECT HANDOVER MODE (existing behavior)
                # Get target agent
                target_agent = session.get(Agent, target_agent_id)
                if not target_agent:
                    return False, None, None, "Target agent not found"

                # Check permissions
                if target_agent.owner_id != user.id:
                    return False, None, None, "Not enough permissions to access target agent"

                # Verify target agent has active environment
                if not target_agent.active_environment_id:
                    return False, None, None, "Target agent has no active environment"

                # Create InputTask with auto-refine (if agent has refiner_prompt)
                task_data = InputTaskCreate(
                    original_message=task_message,
                    selected_agent_id=target_agent_id,
                    user_workspace_id=user_workspace_id,
                    agent_initiated=True,
                    auto_execute=True,
                    source_session_id=source_session_id,
                )

                task, message_to_send = InputTaskService.create_task_with_auto_refine(
                    db_session=session,
                    user_id=user.id,
                    data=task_data,
                )

                logger.info(f"Created task {task.id} for handover to agent {target_agent_id}")

                # Execute task (creates session, links it, sends message)
                success, new_session, error = await InputTaskService.execute_task(
                    db_session=session,
                    task=task,
                    user_id=user.id,
                    message_to_send=message_to_send,
                )

                if not success:
                    return False, task.id, None, error

                # Log system message in source session about task creation
                if source_session:
                    MessageService.create_message(
                        session=session,
                        session_id=source_session_id,
                        role="system",
                        content=f"📋 Task created for '{target_agent.name}'",
                        message_metadata={
                            "task_created": True,
                            "task_id": str(task.id),
                            "target_agent_id": str(target_agent_id),
                            "target_agent_name": target_agent.name,
                            "session_id": str(new_session.id),
                        }
                    )

                logger.info(
                    f"Handover executed: Created task {task.id} and session {new_session.id} "
                    f"for agent {target_agent_id}, source session: {source_session_id}"
                )

                return True, task.id, new_session.id, None

            else:
                # INBOX TASK MODE (new behavior)
                # Create InputTask without agent selection, without auto-refine or execute
                task_data = InputTaskCreate(
                    original_message=task_message,
                    selected_agent_id=None,  # No agent selected
                    user_workspace_id=user_workspace_id,
                    agent_initiated=True,
                    auto_execute=False,  # User must execute manually
                    source_session_id=source_session_id,
                )

                # Create task WITHOUT auto-refine (user will refine manually)
                task = InputTaskService.create_task(
                    db_session=session,
                    user_id=user.id,
                    data=task_data,
                )

                logger.info(f"Created inbox task {task.id} from session {source_session_id}")

                # Log system message in source session about inbox task creation
                if source_session:
                    MessageService.create_message(
                        session=session,
                        session_id=source_session_id,
                        role="system",
                        content="📋 Task created in user's inbox",
                        message_metadata={
                            "task_created": True,
                            "task_id": str(task.id),
                            "inbox_task": True,
                        }
                    )

                return True, task.id, None, None  # No session_id for inbox tasks

        except Exception as e:
            logger.error(f"Error creating agent task: {str(e)}")
            return False, None, None, str(e)

    @staticmethod
    async def execute_handover(
        session: Session,
        user_id: UUID,
        target_agent_id: UUID,
        target_agent_name: str,
        handover_message: str,
        source_session_id: UUID
    ) -> tuple[bool, UUID | None, str | None]:
        """
        Deprecated: Use create_agent_task instead.

        Execute agent handover by creating a task, optionally refining it, and auto-executing.
        This method is kept for backward compatibility.

        Returns:
            Tuple of (success: bool, task_id: UUID | None, error: str | None)
        """
        logger.warning("Deprecated method execute_handover called, use create_agent_task instead")

        # Get user from session
        from app.models import User
        user = session.get(User, user_id)
        if not user:
            return False, None, "User not found"

        success, task_id, session_id, error = await AgentService.create_agent_task(
            session=session,
            user=user,
            task_message=handover_message,
            source_session_id=source_session_id,
            target_agent_id=target_agent_id,
            target_agent_name=target_agent_name,
        )

        return success, task_id, error

    # SDK Config Methods

    @staticmethod
    def get_sdk_config(session: Session, agent_id: UUID) -> AgentSdkConfig:
        """
        Get SDK configuration for an agent.

        Returns AgentSdkConfig with sdk_tools and allowed_tools lists.
        If agent_sdk_config is empty or None, returns empty lists.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            return AgentSdkConfig(sdk_tools=[], allowed_tools=[])

        config = agent.agent_sdk_config or {}
        return AgentSdkConfig(
            sdk_tools=config.get("sdk_tools", []),
            allowed_tools=config.get("allowed_tools", [])
        )

    @staticmethod
    def add_allowed_tools(session: Session, agent_id: UUID, tools: list[str]) -> AgentSdkConfig:
        """
        Add tools to the allowed_tools list.

        Merges new tools with existing allowed_tools (no duplicates).
        Returns updated AgentSdkConfig.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Get current config or initialize
        if not agent.agent_sdk_config:
            agent.agent_sdk_config = {"sdk_tools": [], "allowed_tools": []}

        # Get current allowed tools
        current_allowed = set(agent.agent_sdk_config.get("allowed_tools", []))

        # Add new tools
        current_allowed.update(tools)

        # Update config
        agent.agent_sdk_config["allowed_tools"] = list(current_allowed)

        # Mark as modified for SQLAlchemy to detect the change
        flag_modified(agent, "agent_sdk_config")

        session.add(agent)
        session.commit()
        session.refresh(agent)

        return AgentSdkConfig(
            sdk_tools=agent.agent_sdk_config.get("sdk_tools", []),
            allowed_tools=agent.agent_sdk_config.get("allowed_tools", [])
        )

    @staticmethod
    def get_pending_tools(session: Session, agent_id: UUID) -> list[str]:
        """
        Get tools that need approval.

        Returns tools that are in sdk_tools but not in allowed_tools.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            return []

        config = agent.agent_sdk_config or {}
        sdk_tools = set(config.get("sdk_tools", []))
        allowed_tools = set(config.get("allowed_tools", []))

        # Pending = sdk_tools - allowed_tools (case-insensitive to handle
        # legacy PascalCase data mixed with new lowercase convention)
        allowed_lower = {t.lower() for t in allowed_tools}
        pending = [t for t in sdk_tools if t.lower() not in allowed_lower]
        return pending

    @staticmethod
    def update_sdk_tools(session: Session, agent_id: UUID, tools: list[str]) -> AgentSdkConfig:
        """
        Update the sdk_tools list (incrementally - adds new tools, keeps existing).

        Called when init message is received from agent-env to update discovered tools.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Get current config or initialize
        if not agent.agent_sdk_config:
            agent.agent_sdk_config = {"sdk_tools": [], "allowed_tools": []}

        # Get current sdk_tools
        current_sdk_tools = set(agent.agent_sdk_config.get("sdk_tools", []))

        # Add new tools (incremental)
        current_sdk_tools.update(tools)

        # Update config
        agent.agent_sdk_config["sdk_tools"] = list(current_sdk_tools)

        # Mark as modified for SQLAlchemy to detect the change
        flag_modified(agent, "agent_sdk_config")

        session.add(agent)
        session.commit()
        session.refresh(agent)

        return AgentSdkConfig(
            sdk_tools=agent.agent_sdk_config.get("sdk_tools", []),
            allowed_tools=agent.agent_sdk_config.get("allowed_tools", [])
        )

    @staticmethod
    async def sync_allowed_tools_to_environment(session: Session, agent_id: UUID) -> bool:
        """
        Sync allowed_tools to agent's active environment.

        This syncs only the settings.json (not plugin files) to update allowed_tools.
        Called after approving tools via /allowed-tools endpoint.

        Args:
            session: Database session
            agent_id: Agent UUID

        Returns:
            True if sync was successful, False otherwise
        """
        from app.services.plugins.llm_plugin_service import LLMPluginService

        agent = session.get(Agent, agent_id)
        if not agent:
            logger.warning(f"Agent {agent_id} not found for allowed_tools sync")
            return False

        if not agent.active_environment_id:
            logger.warning(f"Agent {agent_id} has no active environment, skipping allowed_tools sync")
            return False

        environment = session.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            logger.warning(f"Active environment {agent.active_environment_id} not found")
            return False

        if environment.status != "running":
            logger.warning(f"Environment {environment.id} is not running (status: {environment.status})")
            return False

        try:
            # Get allowed_tools from agent SDK config
            allowed_tools = []
            if agent.agent_sdk_config:
                allowed_tools = agent.agent_sdk_config.get("allowed_tools", [])

            # Build the plugin manifest carrying allowed_tools. The container
            # install routine merges allowed_tools into settings.json and skips
            # re-cloning plugins already present at their pinned ref (idempotent
            # via the per-plugin .cinna_plugin_ref marker), so a mere tool
            # approval doesn't refetch files.
            manifest = LLMPluginService.build_plugin_manifest(
                session=session,
                agent_id=agent_id,
                allowed_tools=allowed_tools,
            )

            # Get lifecycle manager and adapter
            lifecycle_manager = EnvironmentLifecycleManager()
            adapter = lifecycle_manager.get_adapter(environment)

            await adapter.set_plugins(manifest)

            logger.info(f"Synced allowed_tools to environment {environment.id} for agent {agent_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to sync allowed_tools to environment: {e}")
            return False
