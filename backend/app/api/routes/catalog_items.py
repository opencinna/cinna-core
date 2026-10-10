"""Unified catalog items API (``/api/v1/catalog/items``).

One surface for every catalog kind (agent bundles and skill packages), keyed on
the item uuid. C1 ships only the grants routes; the rules live in
:class:`~app.services.catalog.catalog_access_service.CatalogAccessService`.
Registered before ``catalog.router`` so ``/catalog/items/...`` is not read as
``/catalog/{bundle_id}``.
"""
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models.catalog.catalog_access import CatalogGrant, CatalogGrantIn
from app.services.catalog.catalog_access_service import CatalogAccessService
from app.services.catalog.catalog_events import CatalogEvents
from app.services.catalog.exceptions import CatalogError

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _http_error(exc: CatalogError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=str(exc))


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
