"""Superuser administration of the core MCP directory.

Outside ``/admin/*`` on purpose, so the account CLI can verify it. Every
account sees the non-banned entries through ``GET /addons/directory``; these
routes are the write side (and the one view that includes banned entries).

Errors carry ``detail = {code, message}``: 404 ``entry_not_found``,
409 ``name_taken``, 422 ``invalid_entry`` / ``index_version_unsupported``,
502 ``index_fetch_failed`` / ``index_too_large`` / ``index_invalid``.
"""
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import Message, User
from app.models.mcp.mcp_directory_entry import (
    McpDirectoryAccessUpdate,
    McpDirectoryEntriesPublic,
    McpDirectoryEntryCreate,
    McpDirectoryEntryPublic,
    McpDirectoryEntryUpdate,
    McpDirectoryImportResult,
)
from app.services.mcp.mcp_directory_service import (
    McpDirectoryError,
    McpDirectoryService,
)

router = APIRouter(
    prefix="/mcp-directory",
    tags=["mcp-directory"],
    dependencies=[Depends(get_current_active_superuser)],
)

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


def _http_error(exc: McpDirectoryError) -> HTTPException:
    return HTTPException(
        status_code=exc.status, detail={"code": exc.code, "message": exc.message}
    )


@router.get("/entries", response_model=McpDirectoryEntriesPublic)
def list_mcp_directory_entries(session: SessionDep) -> Any:
    """Every entry, banned ones included, ordered by title."""
    return McpDirectoryService.list_entries(session)


@router.post("/entries", response_model=McpDirectoryEntryPublic)
def create_mcp_directory_entry(
    session: SessionDep, current_user: SuperUser, data: McpDirectoryEntryCreate
) -> Any:
    """Add a manual entry. http / sse need an https `url`; stdio needs a `package`."""
    try:
        row = McpDirectoryService.create_entry(session, data, current_user)
    except McpDirectoryError as exc:
        raise _http_error(exc)
    return McpDirectoryService.to_public(row)


@router.put("/entries/{entry_id}", response_model=McpDirectoryEntryPublic)
def update_mcp_directory_entry(
    session: SessionDep, entry_id: uuid.UUID, data: McpDirectoryEntryUpdate
) -> Any:
    """Change an entry. An imported entry's index fields are overwritten by the next import."""
    try:
        row = McpDirectoryService.update_entry(session, entry_id, data)
    except McpDirectoryError as exc:
        raise _http_error(exc)
    return McpDirectoryService.to_public(row)


@router.delete("/entries/{entry_id}", response_model=Message)
def delete_mcp_directory_entry(session: SessionDep, entry_id: uuid.UUID) -> Any:
    """Remove an entry. An imported one comes back on the next import; ban it instead to keep it out."""
    try:
        McpDirectoryService.delete_entry(session, entry_id)
    except McpDirectoryError as exc:
        raise _http_error(exc)
    return Message(message="MCP directory entry deleted")


@router.put("/entries/{entry_id}/access", response_model=McpDirectoryEntryPublic)
def set_mcp_directory_entry_access(
    session: SessionDep,
    current_user: SuperUser,
    entry_id: uuid.UUID,
    data: McpDirectoryAccessUpdate,
) -> Any:
    """Ban (`banned`, optional `reason`) or unban (`default`) an entry."""
    try:
        row = McpDirectoryService.set_access(session, entry_id, data, current_user)
    except McpDirectoryError as exc:
        raise _http_error(exc)
    return McpDirectoryService.to_public(row)


@router.post("/import", response_model=McpDirectoryImportResult)
def import_mcp_directory_index(session: SessionDep, current_user: SuperUser) -> Any:
    """
    Import the opencinna index (`MCP_DIRECTORY_INDEX_URL`).

    Creates missing entries, overwrites the index fields of entries it created
    before, skips index entries with no https remote (`no_remote`) or whose
    name a manual entry holds (`name_taken`), and lists imported entries the
    index no longer has as `stale` without deleting them.
    """
    try:
        return McpDirectoryService.import_index(session, current_user)
    except McpDirectoryError as exc:
        raise _http_error(exc)
