from uuid import UUID
import logging
import asyncio
from sqlmodel import Session as DBSession, select, and_, func, desc
from app.models import Activity, ActivityCreate, ActivityUpdate, Agent, Session, AgentEnvironment, SessionMessage
from app.models.event import EventType

logger = logging.getLogger(__name__)


class ActivityService:
    @staticmethod
    def create_activity(
        db_session: DBSession, user_id: UUID, data: ActivityCreate
    ) -> Activity:
        """Create a new activity"""
        activity = Activity(
            user_id=user_id,
            session_id=data.session_id,
            agent_id=data.agent_id,
            activity_type=data.activity_type,
            text=data.text,
            action_required=data.action_required,
            is_read=data.is_read,
        )
        db_session.add(activity)
        db_session.commit()
        db_session.refresh(activity)
        return activity

    @staticmethod
    def get_activity(db_session: DBSession, activity_id: UUID) -> Activity | None:
        """Get activity by ID"""
        return db_session.get(Activity, activity_id)

    @staticmethod
    def update_activity(
        db_session: DBSession, activity_id: UUID, data: ActivityUpdate
    ) -> Activity | None:
        """Update activity (e.g., mark as read)"""
        activity = db_session.get(Activity, activity_id)
        if not activity:
            return None

        update_dict = data.model_dump(exclude_unset=True)
        activity.sqlmodel_update(update_dict)

        db_session.add(activity)
        db_session.commit()
        db_session.refresh(activity)
        return activity

    @staticmethod
    def mark_as_read(
        db_session: DBSession, activity_id: UUID
    ) -> Activity | None:
        """Mark activity as read"""
        activity = db_session.get(Activity, activity_id)
        if not activity:
            return None

        activity.is_read = True
        db_session.add(activity)
        db_session.commit()
        db_session.refresh(activity)
        return activity

    @staticmethod
    def mark_multiple_as_read(
        db_session: DBSession, activity_ids: list[UUID]
    ) -> int:
        """Mark multiple activities as read, returns count of updated activities"""
        statement = (
            select(Activity)
            .where(Activity.id.in_(activity_ids))
        )
        activities = db_session.exec(statement).all()

        count = 0
        for activity in activities:
            activity.is_read = True
            db_session.add(activity)
            count += 1

        db_session.commit()
        return count

    @staticmethod
    def list_user_activities(
        db_session: DBSession,
        user_id: UUID,
        agent_id: UUID | None = None,
        skip: int = 0,
        limit: int = 100,
        order_desc: bool = True
    ) -> list[Activity]:
        """List activities for user with optional filtering"""
        statement = select(Activity).where(Activity.user_id == user_id)

        # Filter by agent if specified
        if agent_id:
            statement = statement.where(Activity.agent_id == agent_id)

        # Add ordering
        if order_desc:
            statement = statement.order_by(Activity.created_at.desc())
        else:
            statement = statement.order_by(Activity.created_at.asc())

        # Add pagination
        statement = statement.offset(skip).limit(limit)

        return list(db_session.exec(statement).all())

    @staticmethod
    def get_activity_stats(
        db_session: DBSession,
        user_id: UUID
    ) -> dict[str, int]:
        """Get activity statistics (unread count, action required count)"""
        # Count unread activities
        unread_statement = select(func.count()).select_from(Activity).where(
            and_(
                Activity.user_id == user_id,
                Activity.is_read == False
            )
        )
        unread_count = db_session.exec(unread_statement).one()

        # Count activities requiring action (and unread)
        action_required_statement = select(func.count()).select_from(Activity).where(
            and_(
                Activity.user_id == user_id,
                Activity.is_read == False,
                Activity.action_required != ""
            )
        )
        action_required_count = db_session.exec(action_required_statement).one()

        return {
            "unread_count": unread_count,
            "action_required_count": action_required_count,
        }

    @staticmethod
    def delete_activity(db_session: DBSession, activity_id: UUID) -> bool:
        """Delete activity by ID"""
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

            # Create "session_running" activity (unread by default)
            running_activity = ActivityService.create_activity(
                db_session=db_session,
                user_id=user_id,
                data=ActivityCreate(
                    session_id=session_id,
                    agent_id=agent_id,
                    activity_type="session_running",
                    text="Session is running",
                    action_required="",
                    is_read=False
                )
            )
            logger.info(f"Created 'session_running' activity for session {session_id}")

            # Emit WebSocket event for session_running activity
            from app.services.event_service import event_service
            asyncio.create_task(
                event_service.emit_event(
                    event_type=EventType.ACTIVITY_CREATED,
                    model_id=running_activity.id,
                    user_id=user_id,
                    meta={
                        "activity_type": "session_running",
                        "session_id": str(session_id),
                        "agent_id": str(agent_id)
                    }
                )
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
                from app.services.event_service import event_service
                asyncio.create_task(
                    event_service.emit_event(
                        event_type=EventType.ACTIVITY_DELETED,
                        model_id=activity_id,
                        user_id=user_id,
                        meta={
                            "activity_type": "session_running",
                            "session_id": str(session_id)
                        }
                    )
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
                    activity_type="error_occurred",
                    text=activity_text,
                    action_required="",
                    is_read=False
                )
            )
            logger.info(f"Created 'error_occurred' activity for session {session_id}: {activity_text}")

            # Emit WebSocket event for activity creation
            from app.services.event_service import event_service
            asyncio.create_task(
                event_service.emit_event(
                    event_type=EventType.ACTIVITY_CREATED,
                    model_id=activity.id,
                    user_id=user_id,
                    meta={
                        "activity_type": "error_occurred",
                        "session_id": str(session_id),
                        "agent_id": str(agent_id)
                    }
                )
            )

            return activity

        except Exception as e:
            logger.error(f"Failed to create error activity for session {session_id}: {e}", exc_info=True)
            return None

    @staticmethod
    async def create_completion_activities(
        db_session: DBSession,
        session_id: UUID
    ) -> tuple[Activity | None, Activity | None]:
        """
        Create activities for unread session completion.
        Called when stream completes while user was disconnected.

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

            # Only create activities if session was actually completed (not interrupted)
            if chat_session.status != "completed":
                logger.info(f"Skipping activities for session {session_id} with status '{chat_session.status}' (not completed)")
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
                    activity_type="session_completed",
                    text="Session completed",
                    action_required="",
                    is_read=False
                )
            )
            logger.info(f"Created 'session_completed' activity for session {session_id}")

            # Emit WebSocket event for session_completed activity
            from app.services.event_service import event_service
            asyncio.create_task(
                event_service.emit_event(
                    event_type=EventType.ACTIVITY_CREATED,
                    model_id=completed_activity.id,
                    user_id=user_id,
                    meta={
                        "activity_type": "session_completed",
                        "session_id": str(session_id),
                        "agent_id": str(agent_id)
                    }
                )
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
                        activity_type="questions_asked",
                        text="Agent asked questions that need answers",
                        action_required="answers_required",
                        is_read=False
                    )
                )
                logger.info(f"Created 'questions_asked' activity for session {session_id}")

                # Emit WebSocket event for questions_asked activity
                asyncio.create_task(
                    event_service.emit_event(
                        event_type=EventType.ACTIVITY_CREATED,
                        model_id=questions_activity.id,
                        user_id=user_id,
                        meta={
                            "activity_type": "questions_asked",
                            "session_id": str(session_id),
                            "agent_id": str(agent_id)
                        }
                    )
                )

            return completed_activity, questions_activity

        except Exception as e:
            logger.error(f"Failed to create unread completion activities for session {session_id}: {e}", exc_info=True)
            return None, None

    @staticmethod
    async def transition_running_to_completion(
        db_session: DBSession,
        session_id: UUID
    ) -> bool:
        """
        Update 'session_running' activity to completion status.
        Called when stream completes while user was disconnected.
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
                from app.services.event_service import event_service
                asyncio.create_task(
                    event_service.emit_event(
                        event_type=EventType.ACTIVITY_DELETED,
                        model_id=activity_id,
                        user_id=user_id,
                        meta={
                            "activity_type": "session_running",
                            "session_id": str(session_id)
                        }
                    )
                )

            # Create completion activities
            await ActivityService.create_completion_activities(db_session, session_id)
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
                from app.services.event_service import event_service
                asyncio.create_task(
                    event_service.emit_event(
                        event_type=EventType.ACTIVITY_DELETED,
                        model_id=activity_id,
                        user_id=user_id,
                        meta={
                            "activity_type": "session_running",
                            "session_id": str(session_id)
                        }
                    )
                )

            # Create error activity
            await ActivityService.create_error_activity(db_session, session_id, error_message)
            return True

        except Exception as e:
            logger.error(f"Failed to update running activity to error for session {session_id}: {e}", exc_info=True)
            return False
