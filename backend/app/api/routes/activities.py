import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Activity,
    ActivityCreate,
    ActivityUpdate,
    ActivityPublic,
    ActivityPublicExtended,
    ActivitiesPublic,
    ActivitiesPublicExtended,
    ActivityStats,
    Agent,
    Session,
)
from app.services.events.activity_service import ActivityService
from app.services.events.event_service import event_service
from app.models.events.event import EventType

router = APIRouter(prefix="/activities", tags=["activities"])


@router.post("/", response_model=ActivityPublic)
def create_activity(
    *, session: SessionDep, current_user: CurrentUser, activity_in: ActivityCreate
) -> Any:
    """
    Create new activity.
    """
    # If agent_id is provided, verify ownership
    if activity_in.agent_id:
        agent = session.get(Agent, activity_in.agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        if not current_user.is_superuser and (agent.owner_id != current_user.id):
            raise HTTPException(status_code=400, detail="Not enough permissions")

    # If session_id is provided, verify ownership
    if activity_in.session_id:
        session_obj = session.get(Session, activity_in.session_id)
        if not session_obj:
            raise HTTPException(status_code=404, detail="Session not found")
        if not current_user.is_superuser and (session_obj.user_id != current_user.id):
            raise HTTPException(status_code=400, detail="Not enough permissions")

    new_activity = ActivityService.create_activity(
        db_session=session, user_id=current_user.id, data=activity_in
    )

    return new_activity


@router.get("/", response_model=ActivitiesPublicExtended)
def list_activities(
    session: SessionDep,
    current_user: CurrentUser,
    agent_id: uuid.UUID | None = None,
    user_workspace_id: str | None = None,
    skip: int = 0,
    limit: int = 100,
    order_desc: bool = True
) -> Any:
    """
    List user's activities with optional filtering by agent and workspace.

    Args:
        agent_id: Optional agent ID to filter by
        user_workspace_id: Optional workspace filter
            - None (not provided): returns all activities
            - Empty string (""): filters for default workspace (NULL)
            - UUID string: filters for that workspace
        skip: Number of records to skip
        limit: Number of records to return
        order_desc: Order descending if True, ascending if False
    """
    # Parse workspace filter
    workspace_filter: uuid.UUID | None = None
    apply_workspace_filter = False

    if user_workspace_id is None:
        # Parameter not provided - return all activities
        apply_workspace_filter = False
    elif user_workspace_id == "":
        # Empty string means default workspace (NULL in database)
        workspace_filter = None
        apply_workspace_filter = True
    else:
        # Parse as UUID
        try:
            workspace_filter = uuid.UUID(user_workspace_id)
            apply_workspace_filter = True
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Invalid workspace ID format")

    # Build query with joins to get agent name and session title
    query = (
        select(Activity, Agent.name, Agent.ui_color_preset, Session.title)
        .outerjoin(Agent, Activity.agent_id == Agent.id)
        .outerjoin(Session, Activity.session_id == Session.id)
        .where(Activity.user_id == current_user.id)
    )

    # Filter by agent if specified
    if agent_id:
        query = query.where(Activity.agent_id == agent_id)

    # Filter by workspace if specified
    if apply_workspace_filter:
        query = query.where(Activity.user_workspace_id == workspace_filter)

    # Add ordering
    if order_desc:
        query = query.order_by(Activity.created_at.desc())
    else:
        query = query.order_by(Activity.created_at.asc())

    # Add pagination
    query = query.offset(skip).limit(limit)

    results = session.exec(query).all()

    data = [
        ActivityPublicExtended(
            **activity.model_dump(),
            agent_name=agent_name,
            agent_ui_color_preset=agent_ui_color_preset,
            session_title=session_title,
        )
        for activity, agent_name, agent_ui_color_preset, session_title in results
    ]

    # Get total count for this user (with filter if provided)
    count_query = select(Activity).where(Activity.user_id == current_user.id)
    if agent_id:
        count_query = count_query.where(Activity.agent_id == agent_id)

    count = len(session.exec(count_query).all())

    return ActivitiesPublicExtended(data=data, count=count)


@router.get("/stats", response_model=ActivityStats)
def get_activity_stats(
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Get activity statistics (unread count, action required count).
    """
    stats = ActivityService.get_activity_stats(
        db_session=session, user_id=current_user.id
    )

    return ActivityStats(**stats)


@router.patch("/{activity_id}", response_model=ActivityPublic)
async def update_activity(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    activity_id: uuid.UUID,
    activity_in: ActivityUpdate
) -> Any:
    """
    Update activity (e.g., mark as read).
    """
    activity = session.get(Activity, activity_id)
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    # Verify ownership
    if not current_user.is_superuser and (activity.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    updated_activity = ActivityService.update_activity(
        db_session=session, activity_id=activity_id, data=activity_in
    )

    if not updated_activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    # Emit WebSocket event for activity update
    await event_service.emit_event(
        event_type=EventType.ACTIVITY_UPDATED,
        model_id=updated_activity.id,
        user_id=current_user.id,
        meta={
            "activity_type": updated_activity.activity_type,
            "is_read": updated_activity.is_read
        }
    )

    return updated_activity


@router.post("/mark-read", response_model=dict)
async def mark_activities_as_read(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    activity_ids: list[uuid.UUID]
) -> Any:
    """
    Mark multiple activities as read.
    """
    # Verify all activities belong to current user
    activities_to_update = []
    for activity_id in activity_ids:
        activity = session.get(Activity, activity_id)
        if not activity:
            continue
        if not current_user.is_superuser and (activity.user_id != current_user.id):
            raise HTTPException(status_code=400, detail="Not enough permissions")
        activities_to_update.append(activity)

    count = ActivityService.mark_multiple_as_read(
        db_session=session, activity_ids=activity_ids
    )

    # Emit WebSocket events for each updated activity
    for activity in activities_to_update:
        await event_service.emit_event(
            event_type=EventType.ACTIVITY_UPDATED,
            model_id=activity.id,
            user_id=current_user.id,
            meta={
                "activity_type": activity.activity_type,
                "is_read": True
            }
        )

    return {"updated_count": count}


@router.delete("/{activity_id}")
async def delete_activity(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    activity_id: uuid.UUID
) -> Any:
    """
    Delete activity.
    """
    activity = session.get(Activity, activity_id)
    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    # Verify ownership
    if not current_user.is_superuser and (activity.user_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Store activity info before deletion for event emission
    activity_type = activity.activity_type
    user_id = activity.user_id

    success = ActivityService.delete_activity(
        db_session=session, activity_id=activity_id
    )

    if not success:
        raise HTTPException(status_code=404, detail="Activity not found")

    # Emit WebSocket event for activity deletion
    await event_service.emit_event(
        event_type=EventType.ACTIVITY_DELETED,
        model_id=activity_id,
        user_id=user_id,
        meta={
            "activity_type": activity_type
        }
    )

    return {"message": "Activity deleted successfully"}


@router.delete("/")
async def delete_all_activities(
    *,
    session: SessionDep,
    current_user: CurrentUser
) -> Any:
    """
    Delete all activities for the current user.
    """
    # Get all activities for the current user
    query = select(Activity).where(Activity.user_id == current_user.id)
    activities = session.exec(query).all()

    # Delete all activities
    deleted_count = 0
    for activity in activities:
        success = ActivityService.delete_activity(
            db_session=session, activity_id=activity.id
        )
        if success:
            deleted_count += 1
            # Emit WebSocket event for each activity deletion
            await event_service.emit_event(
                event_type=EventType.ACTIVITY_DELETED,
                model_id=activity.id,
                user_id=current_user.id,
                meta={
                    "activity_type": activity.activity_type
                }
            )

    return {"message": f"Deleted {deleted_count} activities", "deleted_count": deleted_count}
