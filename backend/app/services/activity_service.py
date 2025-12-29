from uuid import UUID
from sqlmodel import Session as DBSession, select, and_, func
from app.models import Activity, ActivityCreate, ActivityUpdate, Agent, Session


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
