"""
Admin Environment Service

Provides superuser-only operations for managing all AgentEnvironment rows
across the entire platform: listing with enrichment, template summaries,
and bulk rebuild triggering.

No new data is modified here except via the existing EnvironmentService.rebuild_environment
path (which owns all lifecycle state transitions).
"""
import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.core.config import settings
from app.core.db import create_session
from app.models import User
from app.models.agents.agent import Agent
from app.models.environments.environment import (
    AdminAgentEnvironmentPublic,
    AdminAgentEnvironmentsPublic,
    AdminBulkRebuildResponse,
    AdminBulkSkipped,
    AdminTemplateInfoPublic,
    AgentEnvironment,
)
from app.models.events.security_event import SecurityEvent
from app.models.sessions.session import Session as ChatSession
from app.services.environments.environment_service import EnvironmentService
from app.services.environments.template_image_service import template_image_service

logger = logging.getLogger(__name__)

# Statuses that block a new rebuild from being triggered
_TRANSITIONAL_STATUSES = frozenset({
    "creating",
    "building",
    "initializing",
    "starting",
    "rebuilding",
    "activating",
})

# Statuses that count an environment as "in use" without further checks
_IN_USE_STATUSES = frozenset({
    "running",
    "activating",
    "starting",
    "rebuilding",
})

# Threshold for "recent session" check (session is still considered active if
# last message was sent within this window)
_RECENT_SESSION_THRESHOLD_MINUTES = 10


def _template_exists(env_name: str) -> bool:
    """Return True if the template directory with a Dockerfile is present.

    Used to detect environments whose ``env_name`` references a template that
    is no longer installed on the server — in that case we render the row with
    ``expected_image_tag=None`` so the UI can surface the "template missing"
    state.
    """
    return (Path(settings.ENV_TEMPLATES_DIR) / env_name / "Dockerfile").exists()


class AdminEnvironmentService:
    """Service providing admin-level operations over all environments."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def list_environments(
        session: Session,
        *,
        template: Optional[str] = None,
        status: Optional[str] = None,
        is_stale: Optional[bool] = None,
        in_use: Optional[bool] = None,
        owner_id: Optional[uuid.UUID] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> AdminAgentEnvironmentsPublic:
        """
        Return all environments enriched with admin-only metadata.

        Filters are applied after enrichment because is_stale and in_use are
        computed fields (not stored directly on the row).

        Args:
            session: Database session.
            template: Filter by env_name (exact).
            status: Filter by status (exact).
            is_stale: Filter by computed is_stale flag.
            in_use: Filter by computed in_use flag.
            owner_id: Filter by agent owner user ID.
            search: ILIKE match against agent name, instance_name, owner email/username.
            skip: Pagination offset.
            limit: Page size (hard-capped at 500).

        Returns:
            AdminAgentEnvironmentsPublic with enriched rows, aggregate counts,
            and per-template summary.
        """
        limit = min(limit, 500)

        # Single query: join environment → agent → user
        query = (
            select(AgentEnvironment, Agent, User)
            .join(Agent, Agent.id == AgentEnvironment.agent_id)
            .join(User, User.id == Agent.owner_id)
        )

        # Pre-query filters (push down what we can to the DB)
        if template:
            query = query.where(AgentEnvironment.env_name == template)
        if status:
            query = query.where(AgentEnvironment.status == status)
        if owner_id:
            query = query.where(Agent.owner_id == owner_id)

        # Search (ILIKE across agent name, instance_name, email, username)
        if search:
            like_pattern = f"%{search}%"
            query = query.where(
                or_(
                    Agent.name.ilike(like_pattern),
                    AgentEnvironment.instance_name.ilike(like_pattern),
                    User.email.ilike(like_pattern),
                    User.username.ilike(like_pattern),  # type: ignore[attr-defined]
                )
            )

        rows = list(session.exec(query).all())

        # Cache expected tags per env_name to avoid re-hashing template files per row.
        # get_image_tag / compute_template_hash are deterministic and never raise;
        # the "template missing" case is detected explicitly via _template_exists.
        _tag_cache: dict[str, tuple[str | None, str | None]] = {}

        def _get_expected(env_name: str) -> tuple[str | None, str | None]:
            """Returns (expected_image_tag, expected_hash12) or (None, None) if template missing."""
            if env_name not in _tag_cache:
                if _template_exists(env_name):
                    tag = template_image_service.get_image_tag(env_name)
                    hash12 = template_image_service.compute_template_hash(env_name)
                    _tag_cache[env_name] = (tag, hash12)
                else:
                    _tag_cache[env_name] = (None, None)
            return _tag_cache[env_name]

        # Batch-load recent-session counts for all envs in the result set in a
        # single aggregated query, then index by environment_id. Avoids the N+1
        # that would otherwise run _count_recent_sessions per environment.
        env_ids = [env.id for env, _, _ in rows]
        counts_by_env: dict[uuid.UUID, int] = {}
        if env_ids:
            threshold = datetime.now(UTC) - timedelta(minutes=_RECENT_SESSION_THRESHOLD_MINUTES)
            counts_rows = session.exec(
                select(ChatSession.environment_id, func.count())
                .where(
                    ChatSession.environment_id.in_(env_ids),  # type: ignore[attr-defined]
                    ChatSession.last_message_at >= threshold,
                )
                .group_by(ChatSession.environment_id)
            ).all()
            counts_by_env = {eid: c for eid, c in counts_rows if eid is not None}

        enriched: list[AdminAgentEnvironmentPublic] = []
        for env, agent, user in rows:
            expected_tag, expected_hash = _get_expected(env.env_name)

            # Derive current hash from stored image tag (last 12 chars after the colon)
            current_hash: str | None = None
            if env.current_image_tag and ":" in env.current_image_tag:
                current_hash = env.current_image_tag.split(":", 1)[1]

            # is_stale: NULL current tag always counts as stale; a missing template
            # is also stale (admin needs to see it).
            if expected_tag is None:
                computed_is_stale = True
            else:
                computed_is_stale = env.current_image_tag != expected_tag

            # in_use and active_sessions_count (using batch-loaded counts)
            sessions_count = counts_by_env.get(env.id, 0)
            computed_in_use = AdminEnvironmentService._derive_in_use(env, sessions_count)

            row = AdminAgentEnvironmentPublic(
                id=env.id,
                agent_id=env.agent_id,
                env_name=env.env_name,
                env_version=env.env_version,
                instance_name=env.instance_name,
                type=env.type,
                status=env.status,
                status_message=env.status_message,
                is_active=env.is_active,
                created_at=env.created_at,
                updated_at=env.updated_at,
                last_health_check=env.last_health_check,
                last_activity_at=env.last_activity_at,
                agent_sdk_conversation=env.agent_sdk_conversation,
                agent_sdk_building=env.agent_sdk_building,
                model_override_conversation=env.model_override_conversation,
                model_override_building=env.model_override_building,
                use_default_ai_credentials=env.use_default_ai_credentials,
                conversation_ai_credential_id=env.conversation_ai_credential_id,
                building_ai_credential_id=env.building_ai_credential_id,
                agent_name=agent.name,
                owner_id=user.id,
                owner_email=user.email,
                owner_username=getattr(user, "username", None),
                owner_workspace_id=agent.user_workspace_id,
                current_image_tag=env.current_image_tag,
                expected_image_tag=expected_tag,
                template_hash_current=current_hash,
                template_hash_expected=expected_hash,
                is_stale=computed_is_stale,
                in_use=computed_in_use,
                active_sessions_count=sessions_count,
                last_build_at=env.last_build_at,
                sync_active=env.sync_active,
            )
            enriched.append(row)

        # Post-query filters on computed fields
        if is_stale is not None:
            enriched = [r for r in enriched if r.is_stale == is_stale]
        if in_use is not None:
            enriched = [r for r in enriched if r.in_use == in_use]

        total_count = len(enriched)
        stale_count = sum(1 for r in enriched if r.is_stale)
        in_use_count = sum(1 for r in enriched if r.in_use)

        # Paginate
        page = enriched[skip : skip + limit]

        # Build template summary from the full (unpaginated) enriched list
        templates = AdminEnvironmentService.list_templates(session)

        return AdminAgentEnvironmentsPublic(
            data=page,
            count=total_count,
            stale_count=stale_count,
            in_use_count=in_use_count,
            templates=templates,
        )

    @staticmethod
    def list_templates(session: Session) -> list[AdminTemplateInfoPublic]:
        """
        Return a per-template summary (expected tag/hash, total envs, stale envs).

        Iterates template directories under settings.ENV_TEMPLATES_DIR, excluding
        app_core_base which is the shared core overlay directory.
        """
        templates_dir = Path(settings.ENV_TEMPLATES_DIR)
        result: list[AdminTemplateInfoPublic] = []

        if not templates_dir.exists():
            return result

        for entry in sorted(templates_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name == "app_core_base":
                continue
            # Only count real templates (must have a Dockerfile)
            if not (entry / "Dockerfile").exists():
                continue

            env_name = entry.name
            expected_tag = template_image_service.get_image_tag(env_name)
            expected_hash = template_image_service.compute_template_hash(env_name)

            # Count envs + stale envs for this template
            total_query = select(AgentEnvironment).where(AgentEnvironment.env_name == env_name)
            total_envs_list = list(session.exec(total_query).all())
            total = len(total_envs_list)
            stale = sum(
                1 for e in total_envs_list
                if e.current_image_tag != expected_tag or e.current_image_tag is None
            )

            result.append(AdminTemplateInfoPublic(
                env_name=env_name,
                expected_image_tag=expected_tag,
                expected_hash=expected_hash,
                total_envs=total,
                stale_envs=stale,
            ))

        return result

    @staticmethod
    async def bulk_rebuild(
        session: Session,
        env_ids: list[uuid.UUID],
        actor: User,
    ) -> AdminBulkRebuildResponse:
        """
        Queue background rebuild tasks for the supplied environment IDs.

        Environments in transitional states are skipped (with reason). Rebuilds
        run concurrently but are throttled by a semaphore to avoid overwhelming
        the Docker daemon.

        Args:
            session: Database session (used for validation; tasks open their own sessions).
            env_ids: Ordered list of environment UUIDs to rebuild.
            actor: The superuser triggering the bulk rebuild (for audit logging).

        Returns:
            AdminBulkRebuildResponse with queued and skipped IDs.
        """
        queued: list[uuid.UUID] = []
        skipped: list[AdminBulkSkipped] = []
        audit_events: list[SecurityEvent] = []

        for env_id in env_ids:
            env = session.get(AgentEnvironment, env_id)
            if env is None:
                skipped.append(AdminBulkSkipped(environment_id=env_id, reason="not_found"))
                continue
            if env.status in _TRANSITIONAL_STATUSES:
                skipped.append(AdminBulkSkipped(environment_id=env_id, reason="status_not_allowed"))
                continue

            # Build the audit event row; we'll commit them all in one batch
            # below so a 50-env bulk rebuild doesn't issue 50 sequential commits.
            audit_events.append(
                SecurityEvent(
                    user_id=actor.id,
                    agent_id=env.agent_id,
                    environment_id=env_id,
                    event_type="admin.environment.rebuild",
                    severity="low",
                    details=json.dumps({
                        "bulk": True,
                        "initiator_user_id": str(actor.id),
                    }),
                )
            )
            queued.append(env_id)

        # Commit all audit events at once (defense-in-depth: a failure here must
        # not block the actual rebuild; we log and continue).
        if audit_events:
            try:
                session.add_all(audit_events)
                session.commit()
            except Exception as e:
                logger.warning(f"Failed to persist admin bulk-rebuild security events: {e}")
                session.rollback()

        # Schedule background tasks with concurrency semaphore
        if queued:
            semaphore = asyncio.Semaphore(settings.ADMIN_BULK_REBUILD_CONCURRENCY)

            async def _rebuild_with_semaphore(eid: uuid.UUID) -> None:
                async with semaphore:
                    try:
                        await AdminEnvironmentService._rebuild_env_background(eid)
                    except Exception as e:
                        logger.error(f"Admin bulk rebuild failed for env {eid}: {e}")

            for env_id in queued:
                asyncio.create_task(_rebuild_with_semaphore(env_id))

        return AdminBulkRebuildResponse(queued_environment_ids=queued, skipped=skipped)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_in_use(env: AgentEnvironment, sessions_count: int) -> bool:
        """
        Derive whether an environment is currently in use given its row state
        and the pre-computed recent-session count.

        An environment is considered "in use" when any of:
        1. sync_active is True (CLI tunnel is connected)
        2. Status is one of the "in use" statuses (running, activating, etc.)
        3. There is at least one session with recent last_message_at activity
        """
        if env.sync_active:
            return True
        if env.status in _IN_USE_STATUSES:
            return True
        return sessions_count > 0

    @staticmethod
    async def _rebuild_env_background(env_id: uuid.UUID) -> None:
        """
        Background coroutine to rebuild a single environment.

        Opens its own DB session so it is safe to run concurrently without
        conflicting with the request-scoped session that queued this task.
        """
        with create_session() as bg_session:
            await EnvironmentService.rebuild_environment(bg_session, env_id)
