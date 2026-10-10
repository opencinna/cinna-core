"""Unified catalog items API (``/api/v1/catalog/items``).

One surface for every catalog kind (agent bundles — cloud and desktop — and
skill packages), keyed on the item uuid (README §5.1). Access rules live in
:class:`~app.services.catalog.catalog_access_service.CatalogAccessService`,
projections and item mutations in
:class:`~app.services.catalog.catalog_item_service.CatalogItemService`,
desktop agent creation in
:class:`~app.services.catalog.desktop_agent_service.DesktopAgentService`,
local installs Cinna Desktop reports in
:class:`~app.services.catalog.local_install_service.LocalInstallService`.
Registered before ``catalog.router`` so ``/catalog/items/...`` is not read as
``/catalog/{bundle_id}``.
"""
import uuid
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response, status

from app.api.deps import CurrentUser, SessionDep
from app.models.catalog.catalog_access import CatalogGrant, CatalogGrantIn
from app.models.catalog.catalog_local_install import LocalInstallIn
from app.models.catalog.catalog_item import (
    CatalogItem,
    CatalogItemDetail,
    CatalogItemsPublic,
    CatalogItemUpdate,
    DesktopAgentCreate,
)
from app.services.catalog.catalog_access_service import BUNDLE, CatalogAccessService
from app.services.catalog.catalog_events import CatalogEvents
from app.services.catalog.catalog_item_service import CatalogItemService
from app.services.catalog.desktop_agent_service import DesktopAgentService
from app.services.catalog.exceptions import CatalogError
from app.services.catalog.local_install_service import LocalInstallService

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _http_error(exc: CatalogError) -> HTTPException:
    """``detail`` is the sentence, or the structured body an error carries
    (``{code, message}``, e.g. ``cloud_only``)."""
    detail = getattr(exc, "detail", None) or str(exc)
    return HTTPException(status_code=exc.http_status, detail=detail)


@router.get("/items", response_model=CatalogItemsPublic)
def list_catalog_items(
    session: SessionDep,
    current_user: CurrentUser,
    kind: Literal["agent", "skill"] | None = Query(default=None),
    runs_on: Literal["cloud", "desktop"] | None = Query(default=None),
    scope: Literal["all"] | None = Query(default=None),
) -> Any:
    """Every catalog item the caller may list: agents (cloud and desktop) and
    skills. ``scope=all`` lists every item and is for superusers only."""
    try:
        items = CatalogItemService.list_items(
            session, current_user, kind=kind, runs_on=runs_on, scope=scope
        )
    except CatalogError as exc:
        raise _http_error(exc)
    return CatalogItemsPublic(data=items, count=len(items))


@router.post("/desktop-agents", response_model=CatalogItem)
def create_desktop_agent(
    body: DesktopAgentCreate,
    session: SessionDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
) -> Any:
    """Add an agent that runs on the caller's computer; the caller becomes its
    maintainer. Cinna Desktop then shares it to ``desktop.clone_url``."""
    try:
        bundle, readers = DesktopAgentService.create(session, current_user, body)
        item = CatalogItemService.get_item(session, current_user, bundle.id)
    except CatalogError as exc:
        raise _http_error(exc)
    background_tasks.add_task(CatalogEvents.granted, BUNDLE.wire_kind, bundle.id, readers)
    return item


@router.get("/items/{item_uuid}", response_model=CatalogItemDetail)
def get_catalog_item(
    item_uuid: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """One item and its versions, newest first (404 unless the caller may use it)."""
    try:
        return CatalogItemService.get(session, current_user, item_uuid)
    except CatalogError as exc:
        raise _http_error(exc)


@router.patch("/items/{item_uuid}", response_model=CatalogItem)
def update_catalog_item(
    item_uuid: uuid.UUID,
    body: CatalogItemUpdate,
    session: SessionDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
) -> Any:
    """Rename, re-describe, change visibility or listing (maintainer or superuser)."""
    try:
        item, spec, gained = CatalogItemService.update(session, current_user, item_uuid, body)
    except CatalogError as exc:
        raise _http_error(exc)
    background_tasks.add_task(CatalogEvents.granted, spec.wire_kind, item_uuid, gained)
    return item


@router.delete("/items/{item_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_catalog_item(
    item_uuid: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Response:
    """Remove an item (maintainer or superuser); 409 while other people use it."""
    try:
        CatalogItemService.delete(session, current_user, item_uuid)
    except CatalogError as exc:
        raise _http_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/items/{item_uuid}/grants", response_model=list[CatalogGrant])
def list_catalog_item_grants(
    item_uuid: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """Who may use, edit and maintain one item (maintainer or superuser).
    404 when the caller cannot see the item, 403 when they cannot manage it."""
    try:
        return CatalogAccessService.list_item_grants(session, item_uuid, current_user)
    except CatalogError as exc:
        raise _http_error(exc)


@router.put("/items/{item_uuid}/grants", response_model=list[CatalogGrant])
def put_catalog_item_grants(
    item_uuid: uuid.UUID,
    body: list[CatalogGrantIn],
    session: SessionDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
) -> Any:
    """Replace every grant of one item (maintainer or superuser). Maintainers
    must be people, and at least one is required."""
    try:
        grants, spec, item_id, gained = CatalogAccessService.put_item_grants(
            session, item_uuid, body, current_user
        )
    except CatalogError as exc:
        raise _http_error(exc)
    background_tasks.add_task(CatalogEvents.granted, spec.wire_kind, item_id, gained)
    return grants


@router.post("/items/{item_uuid}/local-installs", response_model=CatalogItem)
def record_local_install(
    item_uuid: uuid.UUID,
    body: LocalInstallIn,
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Cinna Desktop reports that the caller got this item on a computer (an
    agent) or pinned it into a local agent (a skill, ``host_manifest_id``
    required). A repeat moves ``revision_number``. 404 unless the caller may
    use the item (for a skill, also when a desktop agent they can use pins
    this version); 409 ``cloud_only`` for an agent that runs in Cinna; 422 for
    an unknown version or a wrong ``host_manifest_id``."""
    try:
        spec, item, access = LocalInstallService.record(
            session, current_user, item_uuid, body
        )
        return CatalogItemService.project(session, current_user, spec, item, access)
    except CatalogError as exc:
        raise _http_error(exc)


@router.delete(
    "/items/{item_uuid}/local-installs/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_local_install(
    item_uuid: uuid.UUID,
    device_id: str,
    session: SessionDep,
    current_user: CurrentUser,
    host_manifest_id: uuid.UUID | None = Query(default=None),
) -> Response:
    """Cinna Desktop reports a removal: the caller's row for this item on
    ``device_id`` (for ``host_manifest_id`` when given, else every row on that
    device). Idempotent; never touches another person's rows."""
    LocalInstallService.remove(session, current_user, item_uuid, device_id, host_manifest_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
