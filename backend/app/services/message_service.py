from uuid import UUID
from datetime import datetime
from sqlmodel import Session, select, func
from app.models import SessionMessage, Session as ChatSession


class MessageService:
    @staticmethod
    def create_message(
        session: Session,
        session_id: UUID,
        role: str,
        content: str,
        message_metadata: dict | None = None,
    ) -> SessionMessage:
        """Create message in session with auto-incremented sequence"""
        # Get the next sequence number for this session
        statement = select(func.max(SessionMessage.sequence_number)).where(
            SessionMessage.session_id == session_id
        )
        max_sequence = session.exec(statement).first()
        next_sequence = (max_sequence or 0) + 1

        message = SessionMessage(
            session_id=session_id,
            role=role,
            content=content,
            sequence_number=next_sequence,
            message_metadata=message_metadata or {},
        )
        session.add(message)

        # Update session's last_message_at
        chat_session = session.get(ChatSession, session_id)
        if chat_session:
            chat_session.last_message_at = datetime.utcnow()
            session.add(chat_session)

        session.commit()
        session.refresh(message)
        return message

    @staticmethod
    def get_session_messages(
        session: Session, session_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[SessionMessage]:
        """Get messages for session ordered by sequence"""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number)
            .offset(offset)
            .limit(limit)
        )
        return list(session.exec(statement).all())

    @staticmethod
    def get_last_n_messages(
        session: Session, session_id: UUID, n: int = 20
    ) -> list[SessionMessage]:
        """Get last N messages for context window"""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number.desc())
            .limit(n)
        )
        messages = list(session.exec(statement).all())
        # Reverse to get chronological order
        return list(reversed(messages))
