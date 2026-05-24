"""User App Data endpoints — Phase 1 of agent bundles & installs.

Mounted under ``/api/v1/users/me/app-data``. Owner-only — admins do NOT get
override access (matches the "private profile" framing in the plan).

Endpoints:
- ``GET    /users/me/app-data``                 — list user's volumes with size + linked install
- ``POST   /users/me/app-data/{id}/recompute-size`` — force size recompute
- ``DELETE /users/me/app-data/{id}``            — wipe an orphaned volume

Volumes are created lazily by ``EnvironmentLifecycleManager`` when an agent
environment is configured; this surface is purely user-visible CRUD.
"""
import uuid

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models.bundles.app_data_volume import (
    AppDataVolumePublic,
    AppDataVolumesPublic,
)
from app.services.bundles.app_data_service import AppDataService

router = APIRouter(prefix="/users/me/app-data", tags=["app-data"])


def _to_public(volume, install_name: str | None) -> AppDataVolumePublic:
    return AppDataVolumePublic(
        id=volume.id,
        bundle_id=volume.bundle_id,
        catalog_type=volume.catalog_type,
        volume_name=volume.volume_name,
        size_bytes=volume.size_bytes,
        last_size_check_at=volume.last_size_check_at,
        current_install_id=volume.current_install_id,
        current_install_name=install_name,
        is_orphaned=volume.is_orphaned,
        created_at=volume.created_at,
        updated_at=volume.updated_at,
    )


@router.get("", response_model=AppDataVolumesPublic)
def list_app_data_volumes(
    session: SessionDep, current_user: CurrentUser
) -> AppDataVolumesPublic:
    """Return all app-data volumes owned by the caller."""
    rows = AppDataService.list_user_volumes(session, current_user.id)
    data = [_to_public(volume, install_name) for volume, install_name in rows]
    return AppDataVolumesPublic(data=data, count=len(data))


@router.post(
    "/{volume_id}/recompute-size", response_model=AppDataVolumePublic
)
def recompute_app_data_size(
    volume_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> AppDataVolumePublic:
    """Force a fresh size walk for a single volume.

    Sizes are tracked lazily — this endpoint exists for the "Refresh size"
    button in the Settings → App Data tab.
    """
    volume = AppDataService.get_for_user(session, volume_id, current_user.id)
    if not volume:
        raise HTTPException(status_code=404, detail="App data volume not found")

    AppDataService.recompute_size(session, volume)
    install_name = AppDataService.get_install_name(session, volume)
    return _to_public(volume, install_name)


@router.delete("/{volume_id}", status_code=204)
def wipe_app_data_volume(
    volume_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> None:
    """Permanently delete an orphaned volume — row + on-disk tree.

    Wipe is allowed only on volumes flagged ``is_orphaned = true``. Phase 2
    will flip that flag from ``InstallService.uninstall``; until then, the
    UI disables the wipe action for attached rows.
    """
    volume = AppDataService.get_for_user(session, volume_id, current_user.id)
    if not volume:
        raise HTTPException(status_code=404, detail="App data volume not found")

    try:
        AppDataService.wipe_volume(session, volume)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
