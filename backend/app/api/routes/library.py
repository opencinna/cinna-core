"""Team library API (``/api/v1/library``) — cinna-desktop team-sharing README §4.2.

Thin routes over :class:`LibraryService`; library domain errors map to HTTP
through their ``http_status`` (404 / 403 / 409 / 422). The git transport itself
lives in ``git_http.py`` at ``/git/<repo_id>.git``.
"""
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.models.library.git_repo import (
    LibraryAgent,
    LibraryAgentAdmin,
    LibraryAgentCreate,
    LibraryGrantIn,
    LibraryGrantPublic,
    LibraryListing,
)
from app.services.library.exceptions import LibraryError
from app.services.library.library_events import LibraryEvents
from app.services.library.library_service import LibraryService

router = APIRouter(prefix="/library", tags=["library"])


def _http_error(exc: LibraryError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=str(exc))


@router.get("", response_model=LibraryListing)
def list_library(session: SessionDep, current_user: CurrentUser) -> Any:
    """Every library agent the caller can get, with their access level."""
    return LibraryService.list_library(session, current_user)


@router.post("/agents", response_model=LibraryAgent)
def create_library_agent(
    session: SessionDep,
    current_user: CurrentUser,
    body: LibraryAgentCreate,
    background_tasks: BackgroundTasks,
) -> Any:
    """Add an agent to the team library; the caller becomes its maintainer and
    only the people in ``grants`` (none when omitted) can get it."""
    try:
        agent, readers = LibraryService.create_agent(session, current_user, body)
    except LibraryError as exc:
        raise _http_error(exc)
    background_tasks.add_task(LibraryEvents.granted, agent.repo_id, readers)
    return agent


@router.get(
    "/admin/agents",
    response_model=list[LibraryAgentAdmin],
    dependencies=[Depends(get_current_active_superuser)],
)
def list_library_admin_agents(session: SessionDep) -> Any:
    """Superuser listing of every library agent with its owner and grants."""
    return LibraryService.list_admin_agents(session)


@router.get("/agents/{repo_id}", response_model=LibraryAgent)
def get_library_agent(
    session: SessionDep, current_user: CurrentUser, repo_id: uuid.UUID
) -> Any:
    try:
        return LibraryService.get_agent(session, current_user, repo_id)
    except LibraryError as exc:
        raise _http_error(exc)


@router.get("/agents/{repo_id}/grants", response_model=list[LibraryGrantPublic])
def get_library_agent_grants(
    session: SessionDep, current_user: CurrentUser, repo_id: uuid.UUID
) -> Any:
    """Who can get, change and maintain one agent (maintainer or superuser)."""
    try:
        return LibraryService.list_grants(session, current_user, repo_id)
    except LibraryError as exc:
        raise _http_error(exc)


@router.put("/agents/{repo_id}/grants", response_model=list[LibraryGrantPublic])
def put_library_agent_grants(
    session: SessionDep,
    current_user: CurrentUser,
    repo_id: uuid.UUID,
    body: list[LibraryGrantIn],
    background_tasks: BackgroundTasks,
) -> Any:
    """Replace every grant of one agent (maintainer or superuser). Maintainers
    must be people, and at least one is required."""
    try:
        grants, gained = LibraryService.replace_grants(session, current_user, repo_id, body)
    except LibraryError as exc:
        raise _http_error(exc)
    background_tasks.add_task(LibraryEvents.granted, repo_id, gained)
    return grants


@router.delete("/agents/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_library_agent(
    session: SessionDep, current_user: CurrentUser, repo_id: uuid.UUID
) -> None:
    """Remove an agent; refused while another person has it in Cinna."""
    try:
        LibraryService.delete_agent(session, current_user, repo_id)
    except LibraryError as exc:
        raise _http_error(exc)
