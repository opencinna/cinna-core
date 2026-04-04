import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select, func

from app.api.deps import CurrentUser, CurrentUserOrGuest, GuestShareContext, SessionDep
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
    User,
)
from app.services.sessions.session_service import SessionService
from app.services.sharing.agent_guest_share_service import AgentGuestShareService

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _verify_session_access(
    caller: User | GuestShareContext,
    chat_session: Session,
    db_session: Any,
) -> None:
    """
    Verify that the caller has access to the given session.

    For anonymous guests: session must belong to their guest_share_id.
    For authenticated users: session must belong to them, OR they must
    have a grant for the session's guest_share_id, OR they must be a
    superuser.

    Raises HTTPException if access is denied.
    """
    if isinstance(caller, GuestShareContext):
        # Anonymous guest can only access sessions linked to their guest share
        if chat_session.guest_share_id != caller.guest_share_id:
            raise HTTPException(status_code=403, detail="Not enough permissions")
    else:
        current_user: User = caller
        if current_user.is_superuser:
            return
        # Owner of the session
        if chat_session.user_id == current_user.id:
            return
        # User with grant for this session's guest share
        if chat_session.guest_share_id:
            has_grant = AgentGuestShareService.check_grant(
                db_session, current_user.id, chat_session.guest_share_id
            )
            if has_grant:
                return
        raise HTTPException(status_code=400, detail="Not enough permissions")


class BulkDeleteRequest(BaseModel):
    session_ids: list[uuid.UUID]


class BulkDeleteResponse(BaseModel):
    deleted_count: int


@router.post("/", response_model=SessionPublic)
def create_session(
    *, session: SessionDep, caller: CurrentUserOrGuest, session_in: SessionCreate
) -> Any:
    """
    Create new session using agent's active environment.

    Supports both authenticated users and guest share callers.
    Guest share sessions are forced to conversation mode.
    """
    # Verify agent exists
    agent = session.get(Agent, session_in.agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    guest_share_id: uuid.UUID | None = None

    if isinstance(caller, GuestShareContext):
        # Anonymous guest: derive guest_share_id from JWT claims
        guest_share_id = session_in.guest_share_id or caller.guest_share_id
        if guest_share_id != caller.guest_share_id:
            raise HTTPException(
                status_code=403,
                detail="Guest share ID mismatch",
            )
        # Verify agent matches JWT claims
        if session_in.agent_id != caller.agent_id:
            raise HTTPException(status_code=403, detail="Agent ID mismatch with guest share")
        # Force conversation mode for guest share sessions
        if session_in.mode != "conversation":
            raise HTTPException(
                status_code=400,
                detail="Guest share sessions only support conversation mode",
            )
        # Use the agent owner as the user_id so the session belongs to the
        # owner's account (guest runs in owner's environment)
        user_id = caller.owner_id
    else:
        # Authenticated user
        current_user: User = caller
        guest_share_id = session_in.guest_share_id

        if guest_share_id:
            # User is creating a session via a guest share grant.
            # Verify they have a grant OR are the agent owner.
            is_owner = agent.owner_id == current_user.id
            has_grant = AgentGuestShareService.check_grant(
                session, current_user.id, guest_share_id
            )
            if not is_owner and not has_grant:
                raise HTTPException(status_code=403, detail="No active grant for this guest share")
            # Force conversation mode for guest share sessions
            if session_in.mode != "conversation":
                raise HTTPException(
                    status_code=400,
                    detail="Guest share sessions only support conversation mode",
                )
            # Use the agent owner as user_id (session runs in owner's env)
            user_id = agent.owner_id
        else:
            # Regular user session (existing behavior)
            if not current_user.is_superuser and (agent.owner_id != current_user.id):
                raise HTTPException(status_code=400, detail="Not enough permissions")
            user_id = current_user.id

    if not agent.active_environment_id:
        raise HTTPException(
            status_code=400,
            detail="Agent has no active environment. Please create and activate an environment first.",
        )

    new_session = SessionService.create_session(
        db_session=session, user_id=user_id, data=session_in,
        guest_share_id=guest_share_id,
        dashboard_block_id=session_in.dashboard_block_id,
    )
    if not new_session:
        raise HTTPException(status_code=500, detail="Failed to create session")

    return new_session


@router.get("/", response_model=SessionsPublicExtended)
def list_sessions(
    session: SessionDep,
    caller: CurrentUserOrGuest,
    skip: int = 0,
    limit: int = 100,
    order_by: str = "created_at",  # "created_at" | "updated_at" | "last_message_at"
    order_desc: bool = True,
    user_workspace_id: str | None = None,
    agent_id: uuid.UUID | None = None,
    guest_share_id: uuid.UUID | None = None,
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
        guest_share_id: Optional guest share ID filter
    """
    # Determine owner_user_id and guest share filtering based on caller type
    if isinstance(caller, GuestShareContext):
        # Anonymous guest: automatically filter by guest_share_id from JWT claims
        owner_user_id = caller.owner_id
        guest_share_filter = caller.guest_share_id
    else:
        current_user: User = caller
        owner_user_id = current_user.id
        guest_share_filter = guest_share_id

        if guest_share_filter:
            # Authenticated user requesting guest share sessions.
            # Verify they have a grant or own the agent.
            # We need the agent_id from the guest share to check ownership.
            from app.models import AgentGuestShare
            gs = session.get(AgentGuestShare, guest_share_filter)
            if gs:
                agent_obj = session.get(Agent, gs.agent_id)
                is_owner = agent_obj and agent_obj.owner_id == current_user.id
                has_grant = AgentGuestShareService.check_grant(
                    session, current_user.id, guest_share_filter
                )
                if not is_owner and not has_grant and not current_user.is_superuser:
                    raise HTTPException(
                        status_code=403,
                        detail="No access to this guest share's sessions",
                    )
                # Use the agent owner's user_id to find sessions
                owner_user_id = gs.owner_id

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
        .where(Session.user_id == owner_user_id)
    )

    # Apply guest share filter
    if guest_share_filter:
        statement = statement.where(Session.guest_share_id == guest_share_filter)

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
        .where(Session.user_id == owner_user_id)
    )
    if guest_share_filter:
        count_statement = count_statement.where(Session.guest_share_id == guest_share_filter)
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
def get_session(session: SessionDep, caller: CurrentUserOrGuest, id: uuid.UUID) -> Any:
    """
    Get session details with external session metadata and agent name.

    Supports both authenticated users and guest share callers.
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

    # Verify access
    _verify_session_access(caller, chat_session, session)

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


@router.post("/{id}/recover", response_model=Message)
async def recover_session(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Message:
    """
    Mark session for recovery. Clears SDK session and sets recovery_pending flag.
    If the last user message was followed only by system errors, it is automatically
    re-queued and streaming is initiated — no duplicate message is created.
    """
    chat_session = session.get(Session, id)
    if not chat_session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Verify ownership
    if not current_user.is_superuser and (chat_session.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    has_resendable = SessionService.mark_session_for_recovery(db=session, session=chat_session)

    # Add a system message to the chat indicating recovery
    from app.services.sessions.message_service import MessageService
    MessageService.create_message(
        session=session,
        session_id=id,
        role="system",
        content="Session recovered",
    )

    if has_resendable:
        # Trigger streaming for the re-pending message
        from app.core.db import create_session as create_db_session
        await SessionService.initiate_stream(
            session_id=id,
            get_fresh_db_session=create_db_session,
        )
        return Message(message="Session marked for recovery. Resending last message.")

    return Message(message="Session marked for recovery. Next message will create a fresh AI session with conversation history.")


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
        from app.services.tasks.input_task_service import InputTaskService
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
            from app.services.tasks.input_task_service import InputTaskService
            InputTaskService.reset_task_if_no_sessions(db_session=session, task_id=source_task_id)

        deleted_count += 1

    return BulkDeleteResponse(deleted_count=deleted_count)
