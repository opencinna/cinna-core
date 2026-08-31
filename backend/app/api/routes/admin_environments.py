"""
Admin Agent Environments API

Superuser-only endpoints for managing all AgentEnvironment records
across the platform. Provides listing and bulk/individual rebuild
operations.

All routes require an authenticated superuser (403 for anyone else).
"""
import uuid
import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import SessionDep, get_current_active_superuser
from app.core.config import settings
from app.models import User, Message
from app.models.environments.environment import (
    AdminAgentEnvironmentsPublic,
    AdminBulkRebuildRequest,
    AdminBulkRebuildResponse,
)
from app.services.environments.admin_environment_service import AdminEnvironmentService
from app.services.environments.environment_service import (
    EnvironmentService,
    EnvironmentNotFoundError,
    AgentNotFoundError,
)
from app.services.events.security_event_service import SecurityEventService
from app.models.events.security_event import SecurityEventCreate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/agent-environments", tags=["admin-environments"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.get("/", response_model=AdminAgentEnvironmentsPublic)
def list_admin_environments(
    session: SessionDep,
    current_user: SuperUser,
    template: Optional[str] = Query(None, description="Filter by template name (env_name)"),
    status: Optional[str] = Query(None, description="Filter by environment status"),
    is_stale: Optional[bool] = Query(None, description="Filter by staleness (current_image_tag != expected)"),
    in_use: Optional[bool] = Query(None, description="Filter by in-use flag"),
    update_available: Optional[bool] = Query(
        None,
        description=(
            "Filter by bundle update availability (consumer install running "
            "an older revision than the bundle's latest)"
        ),
    ),
    owner_id: Optional[uuid.UUID] = Query(None, description="Filter by agent owner user ID"),
    search: Optional[str] = Query(None, description="Search agent name, instance name, owner email/username"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
) -> Any:
    """
    List all agent environments across the platform (admin view).

    Returns enriched rows with owner info, template staleness, in-use flags,
    and bundle install state. Filters for is_stale, in_use, and
    update_available are applied after enrichment.
    """
    return AdminEnvironmentService.list_environments(
        session=session,
        template=template,
        status=status,
        is_stale=is_stale,
        in_use=in_use,
        update_available=update_available,
        owner_id=owner_id,
        search=search,
        skip=skip,
        limit=limit,
    )


@router.post("/bulk-rebuild", response_model=AdminBulkRebuildResponse)
async def bulk_rebuild_environments(
    session: SessionDep,
    current_user: SuperUser,
    body: AdminBulkRebuildRequest,
) -> Any:
    """
    Queue a rebuild for each environment in the request.

    Environments in transitional states are skipped (returned in skipped list).
    Rebuilds run in parallel, throttled by ADMIN_BULK_REBUILD_CONCURRENCY.
    Returns immediately; status updates arrive via ENVIRONMENT_STATUS_CHANGED events.
    """
    if len(body.environment_ids) > settings.ADMIN_ENV_MAX_BULK_SIZE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many environments in a single bulk request. "
                f"Maximum is {settings.ADMIN_ENV_MAX_BULK_SIZE}. "
                f"Split your selection and retry."
            ),
        )

    return await AdminEnvironmentService.bulk_rebuild(
        session=session,
        env_ids=body.environment_ids,
        actor=current_user,
    )


@router.post("/{env_id}/rebuild", response_model=Message)
async def rebuild_single_environment(
    env_id: uuid.UUID,
    session: SessionDep,
    current_user: SuperUser,
) -> Any:
    """
    Trigger a rebuild for a single environment (admin path).

    Thin wrapper around the existing rebuild path; uses the superuser access
    bypass so the admin can rebuild any user's environment.
    """
    try:
        environment, _agent = EnvironmentService.get_environment_with_access_check(
            session=session,
            env_id=env_id,
            user_id=current_user.id,
            is_superuser=True,
        )
    except EnvironmentNotFoundError:
        raise HTTPException(status_code=404, detail="Environment not found")
    except AgentNotFoundError:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Emit audit event
    try:
        await SecurityEventService.create_event(
            session=session,
            user_id=current_user.id,
            data=SecurityEventCreate(
                environment_id=env_id,
                agent_id=environment.agent_id,
                event_type="admin.environment.rebuild",
                severity="low",
                details={
                    "bulk": False,
                    "initiator_user_id": str(current_user.id),
                },
            ),
        )
    except Exception as e:
        logger.warning(f"Failed to emit security event for single rebuild {env_id}: {e}")

    # Run rebuild in background (same pattern as user-triggered rebuild)
    import asyncio
    asyncio.create_task(AdminEnvironmentService._rebuild_env_background(env_id))

    return Message(message="Rebuild queued")
