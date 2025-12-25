from uuid import UUID
from datetime import datetime
from sqlmodel import Session as DBSession, select
from sqlalchemy.orm.attributes import flag_modified
from app.models import Session, SessionCreate, SessionUpdate, Agent, AgentEnvironment


class SessionService:
    @staticmethod
    def create_session(
        db_session: DBSession, user_id: UUID, data: SessionCreate
    ) -> Session | None:
        """Create session using agent's active environment"""
        # Get agent to find active environment
        agent = db_session.get(Agent, data.agent_id)
        if not agent or not agent.active_environment_id:
            return None

        session = Session(
            environment_id=agent.active_environment_id,
            user_id=user_id,
            title=data.title,
            mode=data.mode,
        )
        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return session

    @staticmethod
    def get_session(db_session: DBSession, session_id: UUID) -> Session | None:
        """Get session by ID"""
        return db_session.get(Session, session_id)

    @staticmethod
    def update_session(
        db_session: DBSession, session_id: UUID, data: SessionUpdate
    ) -> Session | None:
        """Update session (title, status, mode)"""
        session = db_session.get(Session, session_id)
        if not session:
            return None

        update_dict = data.model_dump(exclude_unset=True)
        session.sqlmodel_update(update_dict)
        session.updated_at = datetime.utcnow()

        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return session

    @staticmethod
    def switch_mode(db_session: DBSession, session_id: UUID, new_mode: str) -> Session | None:
        """Switch session mode (building <-> conversation)"""
        session = db_session.get(Session, session_id)
        if not session:
            return None

        session.mode = new_mode
        session.updated_at = datetime.utcnow()

        db_session.add(session)
        db_session.commit()
        db_session.refresh(session)
        return session

    @staticmethod
    def list_user_sessions(db_session: DBSession, user_id: UUID) -> list[Session]:
        """List all sessions for user"""
        statement = select(Session).where(Session.user_id == user_id)
        return list(db_session.exec(statement).all())

    @staticmethod
    def list_agent_sessions(db_session: DBSession, agent_id: UUID) -> list[Session]:
        """List all sessions for agent (across all environments)"""
        # Get all environments for this agent
        env_statement = select(AgentEnvironment).where(AgentEnvironment.agent_id == agent_id)
        environments = db_session.exec(env_statement).all()
        env_ids = [env.id for env in environments]

        if not env_ids:
            return []

        # Get all sessions for these environments
        statement = select(Session).where(Session.environment_id.in_(env_ids))
        return list(db_session.exec(statement).all())

    @staticmethod
    def delete_session(db_session: DBSession, session_id: UUID) -> bool:
        """Delete session"""
        session = db_session.get(Session, session_id)
        if not session:
            return False

        db_session.delete(session)
        db_session.commit()
        return True

    # External SDK session management methods

    @staticmethod
    def get_external_session_id(session: Session) -> str | None:
        """Get external SDK session ID from metadata"""
        return session.session_metadata.get("external_session_id")

    @staticmethod
    def set_external_session_id(
        db: DBSession,
        session: Session,
        external_session_id: str,
        sdk_type: str = "claude_code"
    ) -> Session:
        """
        Set external SDK session ID in metadata.
        Called after first message to SDK to store the session ID for resumption.
        """
        session.session_metadata["external_session_id"] = external_session_id
        session.session_metadata["sdk_type"] = sdk_type

        # Mark metadata as modified for SQLAlchemy
        flag_modified(session, "session_metadata")

        session.updated_at = datetime.utcnow()
        db.add(session)
        db.commit()
        db.refresh(session)

        return session

    @staticmethod
    def get_sdk_type(session: Session) -> str | None:
        """Get SDK type from metadata"""
        return session.session_metadata.get("sdk_type")

    @staticmethod
    def should_create_new_sdk_session(session: Session) -> bool:
        """
        Determine if we should create a new SDK session or resume existing.

        Returns True if:
        - No external session ID exists
        - Session mode was just switched (different SDK type)
        """
        return not session.session_metadata.get("external_session_id")

    @staticmethod
    def clear_external_session(db: DBSession, session: Session) -> Session:
        """
        Clear external session metadata.
        Used when switching modes or restarting a session.
        """
        session.session_metadata.pop("external_session_id", None)
        session.session_metadata.pop("sdk_type", None)
        session.session_metadata.pop("last_sdk_message_id", None)

        flag_modified(session, "session_metadata")

        session.updated_at = datetime.utcnow()
        db.add(session)
        db.commit()
        db.refresh(session)

        return session

    @staticmethod
    def update_session_mode(
        db: DBSession,
        session: Session,
        new_mode: str,
        clear_external_session: bool = False
    ) -> Session:
        """
        Update session mode.

        Args:
            db: Database session
            session: Session to update
            new_mode: New mode ("building" | "conversation")
            clear_external_session: If True, clear external session ID
                                    (useful when switching between SDKs)
        """
        old_mode = session.mode
        session.mode = new_mode
        session.updated_at = datetime.utcnow()

        # If switching modes and flag is set, clear external session
        if clear_external_session and old_mode != new_mode:
            session.session_metadata.pop("external_session_id", None)
            session.session_metadata.pop("sdk_type", None)
            session.session_metadata.pop("last_sdk_message_id", None)

            flag_modified(session, "session_metadata")

        db.add(session)
        db.commit()
        db.refresh(session)

        return session
