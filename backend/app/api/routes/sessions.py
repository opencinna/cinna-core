import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Session,
    SessionCreate,
    SessionUpdate,
    SessionPublic,
    SessionPublicExtended,
    SessionsPublic,
    SessionsPublicExtended,
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


@router.get("/", response_model=SessionsPublicExtended)
def list_sessions(session: SessionDep, current_user: CurrentUser) -> Any:
    """
    List user's sessions with external session metadata.
    """
    sessions = SessionService.list_user_sessions(db_session=session, user_id=current_user.id)

    data = [
        SessionPublicExtended(
            **s.model_dump(),
            external_session_id=SessionService.get_external_session_id(s),
            sdk_type=SessionService.get_sdk_type(s),
        )
        for s in sessions
    ]

    return SessionsPublicExtended(data=data, count=len(sessions))


@router.get("/{id}", response_model=SessionPublicExtended)
def get_session(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """
    Get session details with external session metadata.
    """
    chat_session = session.get(Session, id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify ownership
    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    return SessionPublicExtended(
        **chat_session.model_dump(),
        external_session_id=SessionService.get_external_session_id(chat_session),
        sdk_type=SessionService.get_sdk_type(chat_session),
    )


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


@router.patch("/{id}/mode", response_model=SessionPublicExtended)
def switch_session_mode(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    new_mode: str,
    clear_external_session: bool = False
) -> Any:
    """
    Switch session mode (building <-> conversation).

    Args:
        id: Session ID
        new_mode: New mode ("building" | "conversation")
        clear_external_session: If True, start a new SDK session
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

    updated_session = SessionService.update_session_mode(
        db=session,
        session=chat_session,
        new_mode=new_mode,
        clear_external_session=clear_external_session
    )

    return SessionPublicExtended(
        **updated_session.model_dump(),
        external_session_id=SessionService.get_external_session_id(updated_session),
        sdk_type=SessionService.get_sdk_type(updated_session),
    )


@router.post("/{id}/reset-sdk", response_model=Message)
def reset_sdk_session(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Message:
    """
    Clear external SDK session, forcing a new session on next message.
    Useful for starting fresh or recovering from SDK errors.
    """
    chat_session = session.get(Session, id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify ownership
    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    SessionService.clear_external_session(db=session, session=chat_session)

    return Message(message="SDK session cleared. Next message will start a new session.")


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
