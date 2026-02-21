import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select, func

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Session,
    SessionCreate,
    SessionUpdate,
    SessionPublic,
    SessionPublicExtended,
    SessionsPublic,
    SessionsPublicExtended,
    SessionMessage,
    Message,
    Agent,
    AgentEnvironment,
)
from app.services.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


class BulkDeleteRequest(BaseModel):
    session_ids: list[uuid.UUID]


class BulkDeleteResponse(BaseModel):
    deleted_count: int


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
def list_sessions(
    session: SessionDep,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 100,
    order_by: str = "created_at",  # "created_at" | "updated_at" | "last_message_at"
    order_desc: bool = True,
    user_workspace_id: str | None = None,
    agent_id: uuid.UUID | None = None,
) -> Any:
    """
    List user's sessions with external session metadata and agent names.

    Args:
        skip: Number of records to skip
        limit: Number of records to return
        order_by: Field to order by (created_at, updated_at, last_message_at)
        order_desc: Order descending if True, ascending if False
        user_workspace_id: Optional workspace filter
            - None (not provided): returns all sessions
            - Empty string (""): filters for default workspace (NULL)
            - UUID string: filters for that workspace
        agent_id: Optional agent ID filter
    """
    # Parse workspace filter
    workspace_filter: uuid.UUID | None = None
    apply_filter = False

    if user_workspace_id is None:
        # Parameter not provided - return all sessions
        apply_filter = False
    elif user_workspace_id == "":
        # Empty string means default workspace (NULL in database)
        workspace_filter = None
        apply_filter = True
    else:
        # Parse as UUID
        try:
            workspace_filter = uuid.UUID(user_workspace_id)
            apply_filter = True
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Invalid workspace ID format")

    # Subquery for message count per session
    msg_count_subq = (
        select(
            SessionMessage.session_id,
            func.count(SessionMessage.id).label("message_count"),
        )
        .group_by(SessionMessage.session_id)
        .subquery()
    )

    # Subquery for last message content per session (highest sequence_number)
    last_msg_seq_subq = (
        select(
            SessionMessage.session_id,
            func.max(SessionMessage.sequence_number).label("max_seq"),
        )
        .group_by(SessionMessage.session_id)
        .subquery()
    )
    last_msg_subq = (
        select(
            SessionMessage.session_id,
            SessionMessage.content.label("last_content"),
        )
        .join(
            last_msg_seq_subq,
            (SessionMessage.session_id == last_msg_seq_subq.c.session_id)
            & (SessionMessage.sequence_number == last_msg_seq_subq.c.max_seq),
        )
        .subquery()
    )

    # Join Session with AgentEnvironment and Agent to get agent name and color
    statement = (
        select(
            Session,
            Agent.id,
            Agent.name,
            Agent.ui_color_preset,
            msg_count_subq.c.message_count,
            last_msg_subq.c.last_content,
        )
        .join(AgentEnvironment, Session.environment_id == AgentEnvironment.id)
        .join(Agent, AgentEnvironment.agent_id == Agent.id)
        .outerjoin(msg_count_subq, Session.id == msg_count_subq.c.session_id)
        .outerjoin(last_msg_subq, Session.id == last_msg_subq.c.session_id)
        .where(Session.user_id == current_user.id)
    )

    # Apply workspace filter
    if apply_filter:
        statement = statement.where(Session.user_workspace_id == workspace_filter)

    # Apply agent_id filter
    if agent_id is not None:
        statement = statement.where(Agent.id == agent_id)

    # Get total count before pagination
    count_statement = (
        select(func.count())
        .select_from(Session)
        .join(AgentEnvironment, Session.environment_id == AgentEnvironment.id)
        .join(Agent, AgentEnvironment.agent_id == Agent.id)
        .where(Session.user_id == current_user.id)
    )
    if apply_filter:
        count_statement = count_statement.where(Session.user_workspace_id == workspace_filter)
    if agent_id is not None:
        count_statement = count_statement.where(Agent.id == agent_id)
    total_count = session.exec(count_statement).one()

    # Add ordering
    order_field = getattr(Session, order_by, Session.created_at)
    if order_desc:
        statement = statement.order_by(order_field.desc())
    else:
        statement = statement.order_by(order_field.asc())

    # Add pagination
    statement = statement.offset(skip).limit(limit)

    results = session.exec(statement).all()

    data = [
        SessionPublicExtended(
            **s.model_dump(),
            external_session_id=SessionService.get_external_session_id(s),
            sdk_type=SessionService.get_sdk_type(s),
            agent_id=a_id,
            agent_name=a_name,
            agent_ui_color_preset=a_color,
            message_count=msg_count or 0,
            last_message_content=(last_content[:200] if last_content else None),
        )
        for s, a_id, a_name, a_color, msg_count, last_content in results
    ]

    return SessionsPublicExtended(data=data, count=total_count)


@router.get("/{id}", response_model=SessionPublicExtended)
def get_session(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """
    Get session details with external session metadata and agent name.
    """
    # Join with AgentEnvironment and Agent to get agent name and color
    statement = (
        select(Session, Agent.id, Agent.name, Agent.ui_color_preset)
        .join(AgentEnvironment, Session.environment_id == AgentEnvironment.id)
        .join(Agent, AgentEnvironment.agent_id == Agent.id)
        .where(Session.id == id)
    )

    result = session.exec(statement).first()
    if not result:
        raise HTTPException(status_code=404, detail="Session not found")

    chat_session, agent_id, agent_name, agent_ui_color_preset = result

    # Verify ownership
    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    return SessionPublicExtended(
        **chat_session.model_dump(),
        external_session_id=SessionService.get_external_session_id(chat_session),
        sdk_type=SessionService.get_sdk_type(chat_session),
        agent_id=agent_id,
        agent_name=agent_name,
        agent_ui_color_preset=agent_ui_color_preset,
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

    # Get agent name
    agent_statement = (
        select(Agent.name)
        .join(AgentEnvironment, AgentEnvironment.agent_id == Agent.id)
        .where(AgentEnvironment.id == updated_session.environment_id)
    )
    agent_name = session.exec(agent_statement).first()

    return SessionPublicExtended(
        **updated_session.model_dump(),
        external_session_id=SessionService.get_external_session_id(updated_session),
        sdk_type=SessionService.get_sdk_type(updated_session),
        agent_name=agent_name,
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

    source_task_id = SessionService.delete_session(db_session=session, session_id=id)

    # If session was linked to a task, check if task needs status reset
    if source_task_id:
        from app.services.input_task_service import InputTaskService
        InputTaskService.reset_task_if_no_sessions(db_session=session, task_id=source_task_id)

    return Message(message="Session deleted successfully")


@router.post("/bulk-delete", response_model=BulkDeleteResponse)
def bulk_delete_sessions(
    session: SessionDep,
    current_user: CurrentUser,
    request: BulkDeleteRequest,
) -> Any:
    """
    Delete multiple sessions at once.
    """
    deleted_count = 0

    for session_id in request.session_ids:
        chat_session = session.get(Session, session_id)
        if not chat_session:
            continue

        # Verify ownership
        if not current_user.is_superuser and (chat_session.user_id != current_user.id):
            continue

        source_task_id = SessionService.delete_session(db_session=session, session_id=session_id)

        if source_task_id:
            from app.services.input_task_service import InputTaskService
            InputTaskService.reset_task_if_no_sessions(db_session=session, task_id=source_task_id)

        deleted_count += 1

    return BulkDeleteResponse(deleted_count=deleted_count)
