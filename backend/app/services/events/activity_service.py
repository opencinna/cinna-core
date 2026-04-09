from uuid import UUID
from typing import Any
import logging
import asyncio
from sqlmodel import Session as DBSession, select, and_, func, desc
from app.models import Activity, ActivityCreate, ActivityUpdate, ActivityPublicExtended, ActivitiesPublicExtended, Agent, Session, AgentEnvironment, SessionMessage
from app.models.tasks.input_task import InputTask
from app.models.events.event import EventType
from app.core.db import create_session

logger = logging.getLogger(__name__)


class ActivityNotFoundError(Exception):
    """Activity does not exist."""

class ActivityPermissionError(Exception):
    """User does not own this activity."""

# Maps task status values to (activity_type, text, action_required) for lifecycle activities
_TASK_LIFECYCLE_MAP: dict[str, tuple[str, str, str]] = {
    "completed": ("task_completed", "Task completed",                           ""),
    "error":     ("task_failed",    "Task failed",                              ""),
    "blocked":   ("task_blocked",   "Task is blocked and requires attention",   "task_action_required"),
    "cancelled": ("task_cancelled", "Task was cancelled",                       ""),
}


class ActivityService:
    @staticmethod
    def create_activity(
        db_session: DBSession, user_id: UUID, data: ActivityCreate
    ) -> Activity:
        """Create a new activity"""
        # Determine workspace_id: inherit from session, task, or agent
        workspace_id = None
        if data.session_id:
            session = db_session.get(Session, data.session_id)
            if session:
                workspace_id = session.user_workspace_id
        elif data.input_task_id:
            task = db_session.get(InputTask, data.input_task_id)
            if task:
                workspace_id = task.user_workspace_id
        elif data.agent_id:
            agent = db_session.get(Agent, data.agent_id)
            if agent:
                workspace_id = agent.user_workspace_id

        activity = Activity(
            user_id=user_id,
            session_id=data.session_id,
            agent_id=data.agent_id,
            input_task_id=data.input_task_id,
            user_workspace_id=workspace_id,
            activity_type=data.activity_type,
            text=data.text,
            action_required=data.action_required,
            is_read=data.is_read,
        )
        db_session.add(activity)
        db_session.commit()
        db_session.refresh(activity)
        return activity

    # ── Ownership helper ─────────────────────────────────────────────────────

    @staticmethod
    def get_activity_with_ownership_check(
        db_session: DBSession, activity_id: UUID, user_id: UUID,
    ) -> Activity:
        """Get activity and verify the requesting user owns it.

        Raises:
            ActivityNotFoundError: activity does not exist
            ActivityPermissionError: user does not own the activity
        """
        activity = db_session.get(Activity, activity_id)
        if not activity:
            raise ActivityNotFoundError()
        if activity.user_id != user_id:
            raise ActivityPermissionError()
        return activity

    # ── CRUD ──────────────────────────────────────────────────────────────

    @staticmethod
    def get_activity(db_session: DBSession, activity_id: UUID) -> Activity | None:
        """Get activity by ID"""
        return db_session.get(Activity, activity_id)

    @staticmethod
    async def update_activity(
        db_session: DBSession, activity_id: UUID, user_id: UUID, data: ActivityUpdate,
    ) -> Activity:
        """Update activity (e.g., mark as read). Emits WebSocket event.

        Raises:
            ActivityNotFoundError, ActivityPermissionError
        """
        activity = ActivityService.get_activity_with_ownership_check(
            db_session, activity_id, user_id,
        )
        update_dict = data.model_dump(exclude_unset=True)
        activity.sqlmodel_update(update_dict)
        db_session.add(activity)
        db_session.commit()
        db_session.refresh(activity)

        from app.services.events.event_service import event_service
        await event_service.emit_event(
            event_type=EventType.ACTIVITY_UPDATED,
            model_id=activity.id,
            user_id=user_id,
            meta={"activity_type": activity.activity_type, "is_read": activity.is_read},
        )
        return activity

    @staticmethod
    async def mark_multiple_as_read(
        db_session: DBSession, user_id: UUID, activity_ids: list[UUID],
    ) -> int:
        """Mark multiple activities as read. Only touches activities owned by user_id.
        Emits a single ACTIVITY_UPDATED event (batch). Returns count updated."""
        statement = select(Activity).where(
            and_(Activity.id.in_(activity_ids), Activity.user_id == user_id)
        )
        activities = list(db_session.exec(statement).all())

        for activity in activities:
            activity.is_read = True
            db_session.add(activity)
        db_session.commit()

        from app.services.events.event_service import event_service
        for activity in activities:
            await event_service.emit_event(
                event_type=EventType.ACTIVITY_UPDATED,
                model_id=activity.id,
                user_id=user_id,
                meta={"activity_type": activity.activity_type, "is_read": True},
            )
        return len(activities)

    # ── Extended listing ──────────────────────────────────────────────────

    @staticmethod
    def _parse_workspace_filter(
        raw: str | None,
    ) -> tuple[bool, UUID | None]:
        """Parse the workspace query parameter.

        Returns (apply_filter, workspace_uuid):
          - raw is None  → (False, None)       – return all
          - raw == ""    → (True, None)         – default workspace (NULL in DB)
          - raw == UUID  → (True, <uuid>)       – specific workspace

        Raises ValueError for unparseable UUIDs.
        """
        if raw is None:
            return False, None
        if raw == "":
            return True, None
        return True, UUID(raw)

    @staticmethod
    def list_user_activities_extended(
        db_session: DBSession,
        user_id: UUID,
        *,
        agent_id: UUID | None = None,
        user_workspace_id: str | None = None,
        include_archived: bool = False,
        skip: int = 0,
        limit: int = 100,
        order_desc: bool = True,
    ) -> ActivitiesPublicExtended:
        """List activities with joined agent/session/task context.

        Handles workspace filter parsing, pagination, ordering, and count.
        By default excludes archived activities (include_archived=False).
        """
        apply_ws, ws_uuid = ActivityService._parse_workspace_filter(user_workspace_id)

        # Data query — join agent, session, task for extended fields
        query = (
            select(
                Activity,
                Agent.name,
                Agent.ui_color_preset,
                Session.title,
                InputTask.short_code,
                InputTask.title,
            )
            .outerjoin(Agent, Activity.agent_id == Agent.id)
            .outerjoin(Session, Activity.session_id == Session.id)
            .outerjoin(InputTask, Activity.input_task_id == InputTask.id)
            .where(Activity.user_id == user_id)
        )

        if not include_archived:
            query = query.where(Activity.is_archived == False)
        if agent_id:
            query = query.where(Activity.agent_id == agent_id)
        if apply_ws:
            query = query.where(Activity.user_workspace_id == ws_uuid)

        if order_desc:
            query = query.order_by(Activity.created_at.desc())
        else:
            query = query.order_by(Activity.created_at.asc())

        query = query.offset(skip).limit(limit)
        results = db_session.exec(query).all()

        data = [
            ActivityPublicExtended(
                **activity.model_dump(),
                agent_name=agent_name,
                agent_ui_color_preset=agent_ui_color_preset,
                session_title=session_title,
                task_short_code=task_short_code,
                task_title=task_title,
            )
            for activity, agent_name, agent_ui_color_preset, session_title, task_short_code, task_title in results
        ]

        # Count query
        count_q = (
            select(func.count())
            .select_from(Activity)
            .where(Activity.user_id == user_id)
        )
        if not include_archived:
            count_q = count_q.where(Activity.is_archived == False)
        if agent_id:
            count_q = count_q.where(Activity.agent_id == agent_id)
        if apply_ws:
            count_q = count_q.where(Activity.user_workspace_id == ws_uuid)
        count = db_session.exec(count_q).one()

        return ActivitiesPublicExtended(data=data, count=count)

    @staticmethod
    def archive_logs(db_session: DBSession, user_id: UUID) -> int:
        """Archive non-active log activities.

        Marks activities as archived where action_required is empty and
        activity_type is not 'session_running'. Returns count archived.
        """
        statement = select(Activity).where(
            and_(
                Activity.user_id == user_id,
                Activity.is_archived == False,
                Activity.action_required == "",
                Activity.activity_type != "session_running",
            )
        )
        activities = list(db_session.exec(statement).all())
        for a in activities:
            a.is_archived = True
            a.is_read = True
            db_session.add(a)
        db_session.commit()
        return len(activities)

    @staticmethod
    def get_activity_stats(
        db_session: DBSession,
        user_id: UUID,
    ) -> dict[str, int]:
        """Get activity statistics (unread count, action required count).
        Excludes archived activities."""
        unread_statement = select(func.count()).select_from(Activity).where(
            and_(Activity.user_id == user_id, Activity.is_read == False, Activity.is_archived == False)
        )
        unread_count = db_session.exec(unread_statement).one()

        action_required_statement = select(func.count()).select_from(Activity).where(
            and_(
                Activity.user_id == user_id,
                Activity.is_read == False,
                Activity.is_archived == False,
                Activity.action_required != "",
            )
        )
        action_required_count = db_session.exec(action_required_statement).one()

        return {"unread_count": unread_count, "action_required_count": action_required_count}

    # ── Delete ────────────────────────────────────────────────────────────

    @staticmethod
    async def delete_activity_for_user(
        db_session: DBSession, activity_id: UUID, user_id: UUID,
    ) -> None:
        """Delete a single activity with ownership check. Emits WebSocket event.

        Raises:
            ActivityNotFoundError, ActivityPermissionError
        """
        activity = ActivityService.get_activity_with_ownership_check(
            db_session, activity_id, user_id,
        )
        activity_type = activity.activity_type
        db_session.delete(activity)
        db_session.commit()

        from app.services.events.event_service import event_service
        await event_service.emit_event(
            event_type=EventType.ACTIVITY_DELETED,
            model_id=activity_id,
            user_id=user_id,
            meta={"activity_type": activity_type},
        )

    @staticmethod
    async def delete_all_for_user(
        db_session: DBSession, user_id: UUID,
    ) -> int:
        """Delete all activities for a user in bulk. Returns count deleted."""
        statement = select(Activity).where(Activity.user_id == user_id)
        activities = list(db_session.exec(statement).all())

        # Collect metadata before deleting
        activity_metas = [(a.id, a.activity_type) for a in activities]
        for activity in activities:
            db_session.delete(activity)
        db_session.commit()

        from app.services.events.event_service import event_service
        for aid, atype in activity_metas:
            await event_service.emit_event(
                event_type=EventType.ACTIVITY_DELETED,
                model_id=aid,
                user_id=user_id,
                meta={"activity_type": atype},
            )
        return len(activity_metas)

    @staticmethod
    def delete_activity(db_session: DBSession, activity_id: UUID) -> bool:
        """Delete activity by ID (internal use — no ownership check, no event)."""
        activity = db_session.get(Activity, activity_id)
        if not activity:
            return False
        db_session.delete(activity)
        db_session.commit()
        return True

    @staticmethod
    def find_activity_by_session_and_type(
        db_session: DBSession,
        session_id: UUID,
        activity_type: str
    ) -> Activity | None:
        """Find an activity by session_id and activity_type"""
        statement = (
            select(Activity)
            .where(
                and_(
                    Activity.session_id == session_id,
                    Activity.activity_type == activity_type
                )
            )
            .order_by(Activity.created_at.desc())
            .limit(1)
        )
        return db_session.exec(statement).first()

    @staticmethod
    def delete_activity_by_session_and_type(
        db_session: DBSession,
        session_id: UUID,
        activity_type: str
    ) -> bool:
        """Delete an activity by session_id and activity_type"""
        activity = ActivityService.find_activity_by_session_and_type(
            db_session=db_session,
            session_id=session_id,
            activity_type=activity_type
        )
        if not activity:
            return False

        db_session.delete(activity)
        db_session.commit()
        return True

    # Task-specific activity management methods

    @staticmethod
    def find_activity_by_task_and_type(
        db_session: DBSession,
        input_task_id: UUID,
        activity_type: str
    ) -> Activity | None:
        """Find an activity by input_task_id and activity_type"""
        statement = (
            select(Activity)
            .where(
                and_(
                    Activity.input_task_id == input_task_id,
                    Activity.activity_type == activity_type
                )
            )
            .order_by(Activity.created_at.desc())
            .limit(1)
        )
        return db_session.exec(statement).first()

    @staticmethod
    def delete_activity_by_task_and_type(
        db_session: DBSession,
        input_task_id: UUID,
        activity_type: str
    ) -> Activity | None:
        """Delete an activity by input_task_id and activity_type. Returns deleted activity for event emission."""
        activity = ActivityService.find_activity_by_task_and_type(
            db_session=db_session,
            input_task_id=input_task_id,
            activity_type=activity_type
        )
        if not activity:
            return None

        deleted_activity = Activity(
            id=activity.id,
            user_id=activity.user_id,
            activity_type=activity.activity_type,
            text=activity.text,
            action_required=activity.action_required,
            input_task_id=activity.input_task_id,
            agent_id=activity.agent_id,
            is_read=activity.is_read,
            created_at=activity.created_at,
        )
        db_session.delete(activity)
        db_session.commit()
        return deleted_activity

    # Streaming-specific activity management methods

    @staticmethod
    async def create_session_running_activity(
        db_session: DBSession,
        session_id: UUID
    ) -> Activity | None:
        """
        Create 'session_running' activity when stream starts.
        This activity will be shown if user is not watching the session.
        """
        try:
            # Prevent duplicate session_running activities for the same session
            existing = ActivityService.find_activity_by_session_and_type(
                db_session=db_session,
                session_id=session_id,
                activity_type="session_running"
            )
            if existing:
                logger.debug(f"session_running activity already exists for session {session_id}, skipping")
                return existing

            # Get session to find user_id and environment_id
            chat_session = db_session.get(Session, session_id)
            if not chat_session:
                logger.warning(f"Cannot create running activity: session {session_id} not found")
                return None

            # Get environment to find agent_id
            environment = db_session.get(AgentEnvironment, chat_session.environment_id)
            if not environment:
                logger.warning(f"Cannot create running activity: environment not found for session {session_id}")
                return None

            agent_id = environment.agent_id
            user_id = chat_session.user_id
            task_id = chat_session.source_task_id

            # Create "session_running" activity (unread by default)
            running_activity = ActivityService.create_activity(
                db_session=db_session,
                user_id=user_id,
                data=ActivityCreate(
                    session_id=session_id,
                    agent_id=agent_id,
                    input_task_id=task_id,
                    activity_type="session_running",
                    text="Session is running",
                    action_required="",
                    is_read=False
                )
            )
            logger.info(f"Created 'session_running' activity for session {session_id}")

            # Emit WebSocket event for session_running activity
            from app.services.events.event_service import event_service
            await event_service.emit_event(
                event_type=EventType.ACTIVITY_CREATED,
                model_id=running_activity.id,
                user_id=user_id,
                meta={
                    "activity_type": "session_running",
                    "session_id": str(session_id),
                    "agent_id": str(agent_id)
                }
            )

            return running_activity

        except Exception as e:
            logger.error(f"Failed to create session_running activity for session {session_id}: {e}", exc_info=True)
            return None

    @staticmethod
    async def delete_session_running_activity(
        db_session: DBSession,
        session_id: UUID
    ) -> bool:
        """
        Delete 'session_running' activity when stream completes with frontend watching.
        """
        try:
            # Find the activity before deleting to get user_id for event
            running_activity = ActivityService.find_activity_by_session_and_type(
                db_session=db_session,
                session_id=session_id,
                activity_type="session_running"
            )

            if running_activity:
                activity_id = running_activity.id
                user_id = running_activity.user_id

                ActivityService.delete_activity_by_session_and_type(
                    db_session=db_session,
                    session_id=session_id,
                    activity_type="session_running"
                )
                logger.info(f"Deleted 'session_running' activity for session {session_id} (frontend was watching)")

                # Emit WebSocket event for activity deletion
                from app.services.events.event_service import event_service
                await event_service.emit_event(
                    event_type=EventType.ACTIVITY_DELETED,
                    model_id=activity_id,
                    user_id=user_id,
                    meta={
                        "activity_type": "session_running",
                        "session_id": str(session_id)
                    }
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Failed to delete session_running activity for session {session_id}: {e}", exc_info=True)
            return False

    @staticmethod
    async def create_error_activity(
        db_session: DBSession,
        session_id: UUID,
        error_message: str | None
    ) -> Activity | None:
        """
        Create activity for background stream error.
        Called when stream fails while user was disconnected.
        """
        try:
            # Get session to find user_id and environment_id
            chat_session = db_session.get(Session, session_id)
            if not chat_session:
                logger.warning(f"Cannot create error activity: session {session_id} not found")
                return None

            # Get environment to find agent_id
            environment = db_session.get(AgentEnvironment, chat_session.environment_id)
            if not environment:
                logger.warning(f"Cannot create error activity: environment not found for session {session_id}")
                return None

            agent_id = environment.agent_id
            user_id = chat_session.user_id
            task_id = chat_session.source_task_id

            # Create descriptive text for the error activity
            activity_text = "Session encountered an error"
            if error_message:
                # Truncate error message if too long
                if len(error_message) > 100:
                    activity_text = f"Error: {error_message[:97]}..."
                else:
                    activity_text = f"Error: {error_message}"

            # Create "error_occurred" activity
            activity = ActivityService.create_activity(
                db_session=db_session,
                user_id=user_id,
                data=ActivityCreate(
                    session_id=session_id,
                    agent_id=agent_id,
                    input_task_id=task_id,
                    activity_type="error_occurred",
                    text=activity_text,
                    action_required="",
                    is_read=False
                )
            )
            logger.info(f"Created 'error_occurred' activity for session {session_id}: {activity_text}")

            # Emit WebSocket event for activity creation
            from app.services.events.event_service import event_service
            await event_service.emit_event(
                event_type=EventType.ACTIVITY_CREATED,
                model_id=activity.id,
                user_id=user_id,
                meta={
                    "activity_type": "error_occurred",
                    "session_id": str(session_id),
                    "agent_id": str(agent_id)
                }
            )

            return activity

        except Exception as e:
            logger.error(f"Failed to create error activity for session {session_id}: {e}", exc_info=True)
            return None

    @staticmethod
    async def create_completion_activities(
        db_session: DBSession,
        session_id: UUID,
        is_read: bool = False,
    ) -> tuple[Activity | None, Activity | None]:
        """
        Create activities for session completion.
        Called when a stream completes.

        Args:
            db_session: Database session
            session_id: Session ID to create activities for
            is_read: Whether to mark activities as already read. Set True when the user
                     was connected (activities appear in Logs without triggering the bell).

        Returns:
            tuple: (completed_activity, questions_activity)
        """
        try:
            # Get session to find user_id and environment_id
            chat_session = db_session.get(Session, session_id)
            if not chat_session:
                logger.warning(f"Cannot create activities: session {session_id} not found")
                return None, None

            # Get environment to find agent_id
            environment = db_session.get(AgentEnvironment, chat_session.environment_id)
            if not environment:
                logger.warning(f"Cannot create activities: environment not found for session {session_id}")
                return None, None

            agent_id = environment.agent_id
            user_id = chat_session.user_id
            task_id = chat_session.source_task_id  # link activity to originating task

            # Skip if session errored (errors have their own activity path)
            if chat_session.status == "error":
                logger.info(f"Skipping completion activities for session {session_id} with error status")
                return None, None

            # Skip generic completion activity if agent already declared a result_state
            # (the more meaningful activity was created by handle_session_state_updated)
            if chat_session.result_state:
                logger.info(f"Skipping generic completion activity for session {session_id} (result_state='{chat_session.result_state}' already set)")
                return None, None

            # Get the latest agent message to check if it has questions
            latest_message_stmt = (
                select(SessionMessage)
                .where(SessionMessage.session_id == session_id)
                .where(SessionMessage.role == "agent")
                .order_by(desc(SessionMessage.sequence_number))
                .limit(1)
            )
            latest_message = db_session.exec(latest_message_stmt).first()

            # Create "session_completed" activity
            completed_activity = ActivityService.create_activity(
                db_session=db_session,
                user_id=user_id,
                data=ActivityCreate(
                    session_id=session_id,
                    agent_id=agent_id,
                    input_task_id=task_id,
                    activity_type="session_completed",
                    text="Session completed",
                    action_required="",
                    is_read=is_read,
                )
            )
            logger.info(f"Created 'session_completed' activity for session {session_id}")

            # Emit WebSocket event for session_completed activity
            from app.services.events.event_service import event_service
            await event_service.emit_event(
                event_type=EventType.ACTIVITY_CREATED,
                model_id=completed_activity.id,
                user_id=user_id,
                meta={
                    "activity_type": "session_completed",
                    "session_id": str(session_id),
                    "agent_id": str(agent_id)
                }
            )

            # If the latest message has unanswered questions, create additional activity
            questions_activity = None
            if latest_message and latest_message.tool_questions_status == "unanswered":
                questions_activity = ActivityService.create_activity(
                    db_session=db_session,
                    user_id=user_id,
                    data=ActivityCreate(
                        session_id=session_id,
                        agent_id=agent_id,
                        input_task_id=task_id,
                        activity_type="questions_asked",
                        text="Agent asked questions that need answers",
                        action_required="answers_required",
                        is_read=is_read,
                    )
                )
                logger.info(f"Created 'questions_asked' activity for session {session_id}")

                # Emit WebSocket event for questions_asked activity
                await event_service.emit_event(
                    event_type=EventType.ACTIVITY_CREATED,
                    model_id=questions_activity.id,
                    user_id=user_id,
                    meta={
                        "activity_type": "questions_asked",
                        "session_id": str(session_id),
                        "agent_id": str(agent_id)
                    }
                )

            return completed_activity, questions_activity

        except Exception as e:
            logger.error(f"Failed to create unread completion activities for session {session_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    async def transition_running_to_completion(
        db_session: DBSession,
        session_id: UUID,
        is_read: bool = False,
    ) -> bool:
        """
        Delete 'session_running' activity and create completion activities.

        Args:
            db_session: Database session
            session_id: Session ID to transition
            is_read: Whether to mark completion activities as already read. Set True
                     when the user was connected so activities appear in Logs without
                     triggering the notification bell.
        """
        try:
            # Find the session_running activity
            running_activity = ActivityService.find_activity_by_session_and_type(
                db_session=db_session,
                session_id=session_id,
                activity_type="session_running"
            )

            if running_activity:
                activity_id = running_activity.id
                user_id = running_activity.user_id

                # Delete the running activity
                db_session.delete(running_activity)
                db_session.commit()
                logger.info(f"Deleted 'session_running' activity for session {session_id}")

                # Emit WebSocket event for activity deletion
                from app.services.events.event_service import event_service
                await event_service.emit_event(
                    event_type=EventType.ACTIVITY_DELETED,
                    model_id=activity_id,
                    user_id=user_id,
                    meta={
                        "activity_type": "session_running",
                        "session_id": str(session_id)
                    }
                )

            # Create completion activities with the given read state
            await ActivityService.create_completion_activities(db_session, session_id, is_read=is_read)
            return True

        except Exception as e:
            logger.error(f"Failed to update running activity to completion for session {session_id}: {e}", exc_info=True)
            return False

    @staticmethod
    async def transition_running_to_error(
        db_session: DBSession,
        session_id: UUID,
        error_message: str | None
    ) -> bool:
        """
        Update 'session_running' activity to error status.
        Called when stream encounters error while user was disconnected.
        """
        try:
            # Find the session_running activity
            running_activity = ActivityService.find_activity_by_session_and_type(
                db_session=db_session,
                session_id=session_id,
                activity_type="session_running"
            )

            if running_activity:
                activity_id = running_activity.id
                user_id = running_activity.user_id

                # Delete the running activity
                db_session.delete(running_activity)
                db_session.commit()
                logger.info(f"Deleted 'session_running' activity for session {session_id}")

                # Emit WebSocket event for activity deletion
                from app.services.events.event_service import event_service
                await event_service.emit_event(
                    event_type=EventType.ACTIVITY_DELETED,
                    model_id=activity_id,
                    user_id=user_id,
                    meta={
                        "activity_type": "session_running",
                        "session_id": str(session_id)
                    }
                )

            # Create error activity
            await ActivityService.create_error_activity(db_session, session_id, error_message)
            return True

        except Exception as e:
            logger.error(f"Failed to update running activity to error for session {session_id}: {e}", exc_info=True)
            return False

    # Event-driven handlers for streaming lifecycle

    @staticmethod
    async def handle_stream_started(event_data: dict[str, Any]):
        """
        Event handler for STREAM_STARTED events.

        Creates 'session_running' activity when a stream starts.
        This activity will be shown if user is not watching the session.

        Args:
            event_data: Event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            session_id = event_data.get("model_id")
            if not session_id:
                logger.warning("STREAM_STARTED event missing model_id")
                return

            with create_session() as db:
                await ActivityService.create_session_running_activity(
                    db_session=db,
                    session_id=UUID(session_id)
                )

            logger.info(f"[Event Handler] Created session_running activity for session {session_id}")

        except Exception as e:
            logger.error(f"Error in handle_stream_started: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_completed(event_data: dict[str, Any]):
        """
        Event handler for STREAM_COMPLETED events.

        Always creates completion activities regardless of connection status.
        When user was connected, activities are marked as read (is_read=True) so they
        appear in Logs without triggering the unread notification bell.
        When user was disconnected, activities are unread (is_read=False).

        Args:
            event_data: Event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            from app.services.events.event_service import event_service

            session_id = event_data.get("model_id")
            user_id = event_data.get("user_id")

            if not session_id or not user_id:
                logger.warning("STREAM_COMPLETED event missing session_id or user_id")
                return

            # Check if user was connected during stream completion
            user_connected = event_service.is_user_connected(UUID(user_id))

            # Activities are pre-marked as read when the user was watching so they
            # appear in the Logs section without triggering the unread notification bell.
            # When the user was away, activities are unread to surface the notification.
            with create_session() as db:
                success = await ActivityService.transition_running_to_completion(
                    db_session=db,
                    session_id=UUID(session_id),
                    is_read=user_connected,
                )
                if success:
                    read_state = "is_read=True" if user_connected else "is_read=False"
                    logger.info(f"[Event Handler] Created completion activities ({read_state}) for session {session_id}")

        except Exception as e:
            logger.error(f"Error in handle_stream_completed: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_error(event_data: dict[str, Any]):
        """
        Event handler for STREAM_ERROR events.

        Creates error_occurred activity (always, even if user connected).
        Deletes session_running activity if exists.

        Args:
            event_data: Event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            session_id = event_data.get("model_id")
            if not session_id:
                logger.warning("STREAM_ERROR event missing model_id")
                return

            meta = event_data.get("meta", {})
            error_message = meta.get("error_message", "Unknown error occurred")

            with create_session() as db:
                # Delete session_running activity if exists
                await ActivityService.delete_session_running_activity(
                    db_session=db,
                    session_id=UUID(session_id)
                )

                # Always create error activity (errors are important to track)
                await ActivityService.create_error_activity(
                    db_session=db,
                    session_id=UUID(session_id),
                    error_message=error_message
                )

            logger.info(f"[Event Handler] Created error activity for session {session_id}")

        except Exception as e:
            logger.error(f"Error in handle_stream_error: {e}", exc_info=True)

    @staticmethod
    async def handle_stream_interrupted(event_data: dict[str, Any]):
        """
        Event handler for STREAM_INTERRUPTED events.

        Deletes session_running activity.
        No completion activities created (session can be resumed).

        Args:
            event_data: Event dict with type, model_id, meta, user_id, timestamp
        """
        try:
            session_id = event_data.get("model_id")
            if not session_id:
                logger.warning("STREAM_INTERRUPTED event missing model_id")
                return

            with create_session() as db:
                deleted = await ActivityService.delete_session_running_activity(
                    db_session=db,
                    session_id=UUID(session_id)
                )
                if deleted:
                    logger.info(f"[Event Handler] Deleted session_running activity (interrupted) for session {session_id}")

        except Exception as e:
            logger.error(f"Error in handle_stream_interrupted: {e}", exc_info=True)

    @staticmethod
    async def handle_session_state_updated(event_data: dict[str, Any]):
        """
        Event handler for SESSION_STATE_UPDATED events.

        Creates activity when agent declares session outcome via update_session_state tool.
        Maps session state to activity type:
        - completed → session_completed activity
        - needs_input → session_feedback_required activity (action_required)
        - error → error_occurred activity
        """
        try:
            from app.services.events.event_service import event_service

            meta = event_data.get("meta", {})
            session_id = meta.get("session_id")
            state = meta.get("state")
            summary = meta.get("summary")
            user_id = event_data.get("user_id")

            if not session_id or not state or not summary or not user_id:
                logger.warning("SESSION_STATE_UPDATED event missing required fields")
                return

            # Map state to activity type and action_required
            activity_map = {
                "completed": ("session_completed", ""),
                "needs_input": ("session_feedback_required", "answers_required"),
                "error": ("error_occurred", ""),
            }

            if state not in activity_map:
                logger.warning(f"Unknown session state: {state}")
                return

            activity_type, action_required = activity_map[state]

            with create_session() as db:
                # Get session to find agent_id
                chat_session = db.get(Session, UUID(session_id))
                if not chat_session:
                    logger.warning(f"Session {session_id} not found for state update activity")
                    return

                environment = db.get(AgentEnvironment, chat_session.environment_id)
                agent_id = environment.agent_id if environment else None
                task_id = chat_session.source_task_id

                # Delete any existing session_running activity
                await ActivityService.delete_session_running_activity(
                    db_session=db,
                    session_id=UUID(session_id)
                )

                # Create the state-specific activity
                activity = ActivityService.create_activity(
                    db_session=db,
                    user_id=UUID(user_id),
                    data=ActivityCreate(
                        session_id=UUID(session_id),
                        agent_id=agent_id,
                        input_task_id=task_id,
                        activity_type=activity_type,
                        text=summary,
                        action_required=action_required,
                        is_read=False,
                    )
                )

                # Emit WebSocket event for activity creation
                await event_service.emit_event(
                    event_type=EventType.ACTIVITY_CREATED,
                    model_id=activity.id,
                    user_id=UUID(user_id),
                    meta={
                        "activity_type": activity_type,
                        "session_id": session_id,
                        "agent_id": str(agent_id) if agent_id else None,
                        "state": state,
                    }
                )

            logger.info(f"[Event Handler] Created '{activity_type}' activity for session {session_id} (state={state})")

        except Exception as e:
            logger.error(f"Error in handle_session_state_updated: {e}", exc_info=True)

    # Event-driven handlers for email task lifecycle

    @staticmethod
    async def handle_task_created(event_data: dict[str, Any]):
        """
        Event handler for TASK_CREATED events.

        Creates 'email_task_incoming' activity when an email-originated task is created.
        Only triggers for tasks with source_email_message_id.
        """
        try:
            from app.services.events.event_service import event_service

            meta = event_data.get("meta", {})
            source_email_message_id = meta.get("source_email_message_id")
            if not source_email_message_id:
                return  # Not an email-originated task

            task_id = event_data.get("model_id")
            user_id = event_data.get("user_id")
            source_agent_id = meta.get("source_agent_id")

            if not task_id or not user_id:
                logger.warning("TASK_CREATED event missing task_id or user_id")
                return

            with create_session() as db:
                activity = ActivityService.create_activity(
                    db_session=db,
                    user_id=UUID(user_id),
                    data=ActivityCreate(
                        input_task_id=UUID(task_id),
                        agent_id=UUID(source_agent_id) if source_agent_id else None,
                        activity_type="email_task_incoming",
                        text="New email task received",
                        action_required="task_review_required",
                        is_read=False,
                    )
                )

                await event_service.emit_event(
                    event_type=EventType.ACTIVITY_CREATED,
                    model_id=activity.id,
                    user_id=UUID(user_id),
                    meta={
                        "activity_type": "email_task_incoming",
                        "input_task_id": str(task_id),
                        "agent_id": source_agent_id,
                    }
                )

            logger.info(f"[Event Handler] Created 'email_task_incoming' activity for task {task_id}")

        except Exception as e:
            logger.error(f"Error in handle_task_created: {e}", exc_info=True)

    @staticmethod
    async def handle_task_status_changed(event_data: dict[str, Any]):
        """
        Event handler for TASK_STATUS_UPDATED events.

        Handles two concerns:
        1. Task lifecycle activities (ALL tasks): creates activities for significant
           status transitions (completed, error, blocked, cancelled) and dismisses
           the task_blocked activity when a task unblocks.
        2. Email task activity lifecycle (email tasks only): manages the
           email_task_incoming and email_task_reply_pending activity lifecycle.
        """
        try:
            from app.services.events.event_service import event_service

            meta = event_data.get("meta", {})
            task_id = event_data.get("model_id")
            user_id = event_data.get("user_id")
            # Support both event meta formats:
            #   update_status() emits "new_status" + "source_agent_id"
            #   update_task_status() emits "to_status" + "changed_by_agent_id"
            new_status = meta.get("new_status") or meta.get("to_status")
            source_agent_id = meta.get("source_agent_id") or meta.get("changed_by_agent_id")

            if not task_id or not user_id or not new_status:
                logger.warning("TASK_STATUS_UPDATED event missing required fields")
                return

            # ── Task lifecycle activities (runs for ALL tasks) ─────────────────────
            with create_session() as db:
                # Resolve agent_id: prefer the agent that triggered the change,
                # fall back to the task's assigned agent
                agent_id: UUID | None = UUID(source_agent_id) if source_agent_id else None
                if not agent_id:
                    task = db.get(InputTask, UUID(task_id))
                    if task and task.selected_agent_id:
                        agent_id = task.selected_agent_id

                # Dismiss task_blocked activity when task transitions away from blocked
                if new_status != "blocked":
                    deleted = ActivityService.delete_activity_by_task_and_type(
                        db_session=db,
                        input_task_id=UUID(task_id),
                        activity_type="task_blocked"
                    )
                    if deleted:
                        await event_service.emit_event(
                            event_type=EventType.ACTIVITY_DELETED,
                            model_id=deleted.id,
                            user_id=UUID(user_id),
                            meta={
                                "activity_type": "task_blocked",
                                "input_task_id": str(task_id),
                            }
                        )
                        logger.info(f"[Event Handler] Dismissed 'task_blocked' activity for task {task_id}")

                # Create lifecycle activity for significant status transitions
                if new_status in _TASK_LIFECYCLE_MAP:
                    activity_type, text, action_required = _TASK_LIFECYCLE_MAP[new_status]

                    # Guard against duplicate task_blocked activities: a task may be set to
                    # blocked multiple times before unblocking, but we only want one activity
                    if activity_type == "task_blocked" and ActivityService.find_activity_by_task_and_type(
                        db_session=db,
                        input_task_id=UUID(task_id),
                        activity_type="task_blocked"
                    ):
                        logger.info(f"[Event Handler] Skipping duplicate 'task_blocked' activity for task {task_id}")
                    else:
                        activity = ActivityService.create_activity(
                            db_session=db,
                            user_id=UUID(user_id),
                            data=ActivityCreate(
                                input_task_id=UUID(task_id),
                                agent_id=agent_id,
                                activity_type=activity_type,
                                text=text,
                                action_required=action_required,
                                is_read=False,
                            )
                        )
                        await event_service.emit_event(
                            event_type=EventType.ACTIVITY_CREATED,
                            model_id=activity.id,
                            user_id=UUID(user_id),
                            meta={
                                "activity_type": activity_type,
                                "input_task_id": str(task_id),
                                "agent_id": source_agent_id,
                            }
                        )
                        logger.info(f"[Event Handler] Created '{activity_type}' activity for task {task_id}")

            # ── Email task activity lifecycle (email tasks only) ───────────────────
            if not meta.get("is_email_task"):
                return

            with create_session() as db:
                # If status is no longer "new", dismiss the incoming activity
                if new_status != "new":
                    deleted = ActivityService.delete_activity_by_task_and_type(
                        db_session=db,
                        input_task_id=UUID(task_id),
                        activity_type="email_task_incoming"
                    )
                    if deleted:
                        await event_service.emit_event(
                            event_type=EventType.ACTIVITY_DELETED,
                            model_id=deleted.id,
                            user_id=UUID(user_id),
                            meta={
                                "activity_type": "email_task_incoming",
                                "input_task_id": str(task_id),
                            }
                        )
                        logger.info(f"[Event Handler] Deleted 'email_task_incoming' activity for task {task_id}")

                # If completed, create reply_pending activity
                if new_status == "completed":
                    activity = ActivityService.create_activity(
                        db_session=db,
                        user_id=UUID(user_id),
                        data=ActivityCreate(
                            input_task_id=UUID(task_id),
                            agent_id=UUID(source_agent_id) if source_agent_id else None,
                            activity_type="email_task_reply_pending",
                            text="Task completed. Email reply pending.",
                            action_required="reply_pending",
                            is_read=False,
                        )
                    )

                    await event_service.emit_event(
                        event_type=EventType.ACTIVITY_CREATED,
                        model_id=activity.id,
                        user_id=UUID(user_id),
                        meta={
                            "activity_type": "email_task_reply_pending",
                            "input_task_id": str(task_id),
                            "agent_id": source_agent_id,
                        }
                    )
                    logger.info(f"[Event Handler] Created 'email_task_reply_pending' activity for task {task_id}")
                else:
                    # If not completed, dismiss reply_pending if exists
                    deleted = ActivityService.delete_activity_by_task_and_type(
                        db_session=db,
                        input_task_id=UUID(task_id),
                        activity_type="email_task_reply_pending"
                    )
                    if deleted:
                        await event_service.emit_event(
                            event_type=EventType.ACTIVITY_DELETED,
                            model_id=deleted.id,
                            user_id=UUID(user_id),
                            meta={
                                "activity_type": "email_task_reply_pending",
                                "input_task_id": str(task_id),
                            }
                        )
                        logger.info(f"[Event Handler] Deleted 'email_task_reply_pending' activity for task {task_id}")

        except Exception as e:
            logger.error(f"Error in handle_task_status_changed: {e}", exc_info=True)
