"""Catalog & install endpoints.

Two surfaces:
  - ``GET /catalog`` — visibility-aware list of installable bundles.
  - ``POST /catalog/{bundle_id}/install`` — install for the current user.
  - ``POST /catalog/{bundle_id}/admin-install`` — admin-only path to
    install on behalf of another user (Phase 2 preview; the audit log
    side of the flow lands later).
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.models.agents.agent import AgentPublic
from app.models.bundles.catalog import (
    AdminInstallRequest,
    CatalogEntryPublic,
    CatalogPublic,
    InstallRequest,
)
from app.models.users.user import User
from app.services.agents.agent_service import AgentService
from app.services.bundles.bundle_service import BundleService
from app.services.bundles.catalog_service import CatalogService
from app.services.bundles.install_service import InstallError, InstallService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/catalog", tags=["catalog"])


@router.get("/", response_model=CatalogPublic)
def list_catalog(session: SessionDep, current_user: CurrentUser) -> CatalogPublic:
    entries = CatalogService.list_for_user(session, current_user)
    return CatalogPublic(data=entries, count=len(entries))


@router.get("/{bundle_id}", response_model=CatalogEntryPublic)
def get_catalog_entry(
    bundle_id: str,
    session: SessionDep,
    current_user: CurrentUser,
) -> CatalogEntryPublic:
    entry = CatalogService.get_for_user(session, bundle_id, current_user)
    if not entry:
        raise HTTPException(status_code=404, detail="Bundle not found")
    return entry


@router.post("/{bundle_id}/install", response_model=AgentPublic)
async def install_bundle(
    bundle_id: str,
    session: SessionDep,
    current_user: CurrentUser,
    request: InstallRequest | None = None,
) -> AgentPublic:
    bundle = BundleService.get_bundle_by_id(session, bundle_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found")
    if not CatalogService.user_can_install(session, bundle, current_user):
        raise HTTPException(status_code=403, detail="Bundle is not installable")
    try:
        install = await InstallService.install_bundle(
            session=session,
            user=current_user,
            bundle=bundle,
            request=request,
        )
    except InstallError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AgentService.to_public_with_clone_info(session, install)


@router.post(
    "/{bundle_id}/admin-install",
    response_model=AgentPublic,
    dependencies=[Depends(get_current_active_superuser)],
)
async def admin_install_bundle(
    bundle_id: str,
    request: AdminInstallRequest,
    session: SessionDep,
) -> AgentPublic:
    """Admin: install a bundle for ``request.target_user_id``."""
    bundle = BundleService.get_bundle_by_id(session, bundle_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="Bundle not found")
    target_user = session.get(User, request.target_user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found")
    try:
        install = await InstallService.admin_install(
            session=session,
            target_user=target_user,
            bundle=bundle,
            request=request,
        )
    except InstallError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AgentService.to_public_with_clone_info(session, install)
