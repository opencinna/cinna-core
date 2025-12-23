import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Session,
    SessionCreate,
    SessionUpdate,
    SessionPublic,
    SessionsPublic,
    Message,
    Agent,
)
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("/", response_model=SessionPublic)
def create_session(
    *, session: SessionDep, current_user: CurrentUser, session_in: SessionCreate
) -> Any:
    """
    Create new session using agent's active environment.
    """
    # Verify agent exists and user owns it
    agent = session.get(Agent, session_in.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    if not agent.active_environment_id:
        raise HTTPException(
            status_code=400,
            detail="Agent has no active environment. Please create and activate an environment first.",
        )

    new_session = SessionService.create_session(
        db_session=session, user_id=current_user.id, data=session_in
    )
    if not new_session:
        raise HTTPException(status_code=500, detail="Failed to create session")

    return new_session


@router.get("/", response_model=SessionsPublic)
def list_sessions(session: SessionDep, current_user: CurrentUser) -> Any:
    """
    List user's sessions.
    """
    sessions = SessionService.list_user_sessions(db_session=session, user_id=current_user.id)
    return SessionsPublic(data=sessions, count=len(sessions))


@router.get("/{id}", response_model=SessionPublic)
def get_session(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """
    Get session details.
    """
    chat_session = session.get(Session, id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify ownership
    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    return chat_session


@router.patch("/{id}", response_model=SessionPublic)
def update_session(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    session_in: SessionUpdate,
) -> Any:
    """
    Update session.
    """
    chat_session = session.get(Session, id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify ownership
    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    updated_session = SessionService.update_session(
        db_session=session, session_id=id, data=session_in
    )
    return updated_session


@router.patch("/{id}/mode", response_model=SessionPublic)
def switch_session_mode(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID, new_mode: str
) -> Any:
    """
    Switch session mode (building <-> conversation).
    """
    chat_session = session.get(Session, id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify ownership
    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Validate mode
    if new_mode not in ["building", "conversation"]:
        raise HTTPException(status_code=400, detail="Invalid mode. Must be 'building' or 'conversation'")

    updated_session = SessionService.switch_mode(
        db_session=session, session_id=id, new_mode=new_mode
    )
    return updated_session


@router.delete("/{id}")
def delete_session(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Delete session.
    """
    chat_session = session.get(Session, id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify ownership
    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    SessionService.delete_session(db_session=session, session_id=id)
    return Message(message="Session deleted successfully")
