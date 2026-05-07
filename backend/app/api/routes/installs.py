"""Install-side endpoints — publish, uninstall, apply-update, etc.

Mounted with prefix ``/agents/{agent_id}/...`` because every Agent row IS
an Install in the new model. Routes that mutate an install always validate
ownership against the current user.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import CurrentUser, SessionDep, require_developer
from app.models.agents.agent import Agent, AgentPublic
from app.models.bundles.agent_bundle_revision import (
    AgentBundleRevisionPublic,
    PublishRequest,
)
from app.models.bundles.catalog import (
    CheckUpdatesResponse,
    EditBundleIdRequest,
    SetUpdateModeRequest,
)
from app.services.agents.agent_service import AgentService
from app.services.bundles.install_service import InstallError, InstallService
from app.services.bundles.publish_service import PublishService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["installs"])


def _resolve_install_owned(session, agent_id: uuid.UUID, current_user) -> Agent:
    install = session.get(Agent, agent_id)
    if not install:
        raise HTTPException(status_code=404, detail="Agent not found")
    if (
        install.owner_id != current_user.id
        and not current_user.is_superuser
    ):
        raise HTTPException(status_code=403, detail="Not your agent")
    return install


# Phase 3 — publish, edit-bundle-id, and update-mode toggles are
# developer-only.  Uninstall, apply-update, and check-updates remain
# open to install owners regardless of role so an agent-user can keep
# their installs current after a downgrade.


# ── Publish ────────────────────────────────────────────────────


@router.post(
    "/{agent_id}/publish",
    response_model=AgentBundleRevisionPublic,
    dependencies=[Depends(require_developer)],
)
async def publish_agent(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    request: PublishRequest | None = None,
) -> AgentBundleRevisionPublic:
    """Snapshot the install workspace into a new bundle revision.

    First publish creates the ``AgentBundle`` row and promotes the install
    to ``is_publisher_install=True``. Subsequent publishes append a new
    ``AgentBundleRevision``.

    Phase 3 — restricted to ``agent-developer`` and ``admin`` roles.
    """
    install = _resolve_install_owned(session, agent_id, current_user)
    try:
        revision = await PublishService.publish(
            session=session,
            install=install,
            publisher_user_id=current_user.id,
            release_notes=request.release_notes if request else None,
            display_name=request.display_name if request else None,
            description=request.description if request else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Wire response with install_count (just-published revision = 0+ installs).
    from app.api.routes.bundles import _revision_to_public

    return _revision_to_public(session, revision)


# ── Uninstall ─────────────────────────────────────────────────


@router.post("/{agent_id}/uninstall")
async def uninstall_install(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> dict:
    """Delete the install + env; mark per-user app-data orphaned (preserved)."""
    install = _resolve_install_owned(session, agent_id, current_user)
    if install.is_publisher_install:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot uninstall the publisher install. Delete the agent + "
                "bundle from the bundle management UI instead."
            ),
        )
    try:
        await InstallService.uninstall(session, install)
    except InstallError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "uninstalled"}


# ── Apply update ───────────────────────────────────────────────


@router.post("/{agent_id}/apply-update", response_model=AgentPublic)
async def apply_update(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentPublic:
    install = _resolve_install_owned(session, agent_id, current_user)
    try:
        install = await InstallService.apply_update(session, install)
    except InstallError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AgentService.to_public_with_clone_info(session, install)


@router.post("/{agent_id}/check-updates", response_model=CheckUpdatesResponse)
def check_updates(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> CheckUpdatesResponse:
    install = _resolve_install_owned(session, agent_id, current_user)
    result = InstallService.check_for_updates(session, install)
    return CheckUpdatesResponse(**result)


@router.patch("/{agent_id}/update-mode", response_model=AgentPublic)
def set_update_mode(
    agent_id: uuid.UUID,
    request: SetUpdateModeRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentPublic:
    install = _resolve_install_owned(session, agent_id, current_user)
    try:
        install = InstallService.set_update_mode(
            session=session, install=install, mode=request.update_mode
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AgentService.to_public_with_clone_info(session, install)


@router.patch(
    "/{agent_id}/bundle-id",
    response_model=AgentPublic,
    dependencies=[Depends(require_developer)],
)
def edit_bundle_id(
    agent_id: uuid.UUID,
    request: EditBundleIdRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentPublic:
    """Phase 3 — restricted to ``agent-developer`` and ``admin`` roles."""
    install = _resolve_install_owned(session, agent_id, current_user)
    install = InstallService.edit_bundle_id(
        session=session, install=install, new_bundle_id=request.bundle_id
    )
    return AgentService.to_public_with_clone_info(session, install)
