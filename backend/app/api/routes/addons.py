"""The addons directory: one index over every addon source core offers.

``GET /addons/directory`` is what Cinna Desktop's Add picker reads. It returns
the caller's full compact index (catalog skills, marketplace skills and
plugins, MCP directory entries) with the ``policy`` its desktop must apply.
The ETag is a hash of the body; send it back as ``If-None-Match`` to get a 304.
"""
from typing import Literal

from fastapi import APIRouter, Header, Query, Response

from app.api.deps import CurrentUser, SessionDep
from app.models.addons.directory import (
    AddonsDirectoryPublic,
    DirectoryKind,
    DirectorySource,
)
from app.services.addons.addon_directory_service import (
    MAX_LIMIT,
    AddonDirectoryService,
    DirectoryQuery,
)

router = APIRouter(prefix="/addons", tags=["addons"])


@router.get(
    "/directory",
    response_model=AddonsDirectoryPublic,
    responses={304: {"description": "Not modified (`If-None-Match` matched the ETag)"}},
)
def get_addons_directory(
    session: SessionDep,
    current_user: CurrentUser,
    response: Response,
    search: str | None = Query(default=None, max_length=200),
    kind: DirectoryKind | None = None,
    category: str | None = Query(default=None, max_length=64),
    source: DirectorySource | None = None,
    runs_on: Literal["desktop"] | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=MAX_LIMIT),
    if_none_match: str | None = Header(default=None),
) -> AddonsDirectoryPublic | Response:
    """
    Every addon the caller may add, normalised to one entry shape.

    With `search` the rows are ranked: exact name, name prefix, title, tags or
    category, description; within a tier unsupported entries come last. Without
    it the full index comes back ordered by kind, then title. `limit` defaults
    to everything (at most 5000). `runs_on=desktop` drops entries that cannot
    run on a computer (a catalog skill needing a cloud-only credential type, a
    marketplace entry that is an app connector only).
    """
    result = AddonDirectoryService.directory(
        session,
        current_user,
        DirectoryQuery(
            search=search,
            kind=kind,
            category=category,
            source=source,
            runs_on=runs_on,
            skip=skip,
            limit=limit,
        ),
    )
    headers = {"ETag": result.etag, "Cache-Control": "private, no-cache"}
    if if_none_match and result.etag in {
        tag.strip() for tag in if_none_match.split(",")
    }:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return result
