"""
SQLite Session Service for Google ADK

This module provides a SQLite-based session persistence service for Google ADK.
Sessions are stored in /app/workspace/databases/adk_sessions.db to persist
across container restarts and rebuilds.

Based on Google ADK's DatabaseSessionService but simplified for SQLite usage.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import uuid

from google.genai import types
from sqlalchemy import Boolean, Text, event, delete, func, ForeignKeyConstraint
from sqlalchemy.engine import create_engine, Engine
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    Session as DatabaseSessionFactory,
    sessionmaker,
)
from sqlalchemy.schema import MetaData
from sqlalchemy.types import DateTime, PickleType, String, TypeDecorator
from typing_extensions import override

from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.adk.sessions.session import Session
from google.adk.sessions.state import State
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions

logger = logging.getLogger(__name__)

# Default database path
DEFAULT_DB_DIR = "/app/workspace/databases"
DEFAULT_DB_NAME = "adk_sessions.db"

# Column size constants
DEFAULT_MAX_KEY_LENGTH = 128
DEFAULT_MAX_VARCHAR_LENGTH = 256


class DynamicJSON(TypeDecorator):
    """JSON type that uses TEXT with JSON serialization for SQLite."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return json.dumps(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return json.loads(value)
        return value


class DynamicPickleType(TypeDecorator):
    """Type decorator for pickling Python objects."""

    impl = PickleType
    cache_ok = True


class Base(DeclarativeBase):
    """Base class for database tables."""
    pass


class StorageSession(Base):
    """Represents a session stored in the database."""

    __tablename__ = "sessions"

    app_name: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )
    id: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    state: Mapped[MutableDict[str, Any]] = mapped_column(
        MutableDict.as_mutable(DynamicJSON), default={}
    )

    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=func.now()
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )

    storage_events: Mapped[list["StorageEvent"]] = relationship(
        "StorageEvent",
        back_populates="storage_session",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<StorageSession(id={self.id}, update_time={self.update_time})>"

    @property
    def update_timestamp_tz(self) -> float:
        """Returns the time zone aware update timestamp."""
        # SQLite doesn't support timezone, convert to UTC manually
        return self.update_time.replace(tzinfo=timezone.utc).timestamp()

    def to_session(
        self,
        state: dict[str, Any] | None = None,
        events: list[Event] | None = None,
    ) -> Session:
        """Converts the storage session to a session object."""
        if state is None:
            state = {}
        if events is None:
            events = []

        return Session(
            app_name=self.app_name,
            user_id=self.user_id,
            id=self.id,
            state=state,
            events=events,
            last_update_time=self.update_timestamp_tz,
        )


class StorageEvent(Base):
    """Represents an event stored in the database."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )
    app_name: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )
    session_id: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )

    invocation_id: Mapped[str] = mapped_column(String(DEFAULT_MAX_VARCHAR_LENGTH))
    author: Mapped[str] = mapped_column(String(DEFAULT_MAX_VARCHAR_LENGTH))
    actions: Mapped[MutableDict[str, Any]] = mapped_column(DynamicPickleType)
    long_running_tool_ids_json: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    branch: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_VARCHAR_LENGTH), nullable=True
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    # Fields from llm_response.py
    content: Mapped[dict[str, Any]] = mapped_column(DynamicJSON, nullable=True)
    grounding_metadata: Mapped[dict[str, Any]] = mapped_column(
        DynamicJSON, nullable=True
    )
    custom_metadata: Mapped[dict[str, Any]] = mapped_column(
        DynamicJSON, nullable=True
    )
    usage_metadata: Mapped[dict[str, Any]] = mapped_column(
        DynamicJSON, nullable=True
    )
    citation_metadata: Mapped[dict[str, Any]] = mapped_column(
        DynamicJSON, nullable=True
    )

    partial: Mapped[bool] = mapped_column(Boolean, nullable=True)
    turn_complete: Mapped[bool] = mapped_column(Boolean, nullable=True)
    error_code: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_VARCHAR_LENGTH), nullable=True
    )
    error_message: Mapped[str] = mapped_column(String(1024), nullable=True)
    interrupted: Mapped[bool] = mapped_column(Boolean, nullable=True)

    storage_session: Mapped[StorageSession] = relationship(
        "StorageSession",
        back_populates="storage_events",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["app_name", "user_id", "session_id"],
            ["sessions.app_name", "sessions.user_id", "sessions.id"],
            ondelete="CASCADE",
        ),
    )

    @property
    def long_running_tool_ids(self) -> set[str]:
        return (
            set(json.loads(self.long_running_tool_ids_json))
            if self.long_running_tool_ids_json
            else set()
        )

    @long_running_tool_ids.setter
    def long_running_tool_ids(self, value: set[str]):
        if value is None:
            self.long_running_tool_ids_json = None
        else:
            self.long_running_tool_ids_json = json.dumps(list(value))

    @classmethod
    def from_event(cls, session: Session, event: Event) -> "StorageEvent":
        storage_event = StorageEvent(
            id=event.id,
            invocation_id=event.invocation_id,
            author=event.author,
            branch=event.branch,
            actions=event.actions,
            session_id=session.id,
            app_name=session.app_name,
            user_id=session.user_id,
            timestamp=datetime.fromtimestamp(event.timestamp),
            long_running_tool_ids=event.long_running_tool_ids,
            partial=event.partial,
            turn_complete=event.turn_complete,
            error_code=event.error_code,
            error_message=event.error_message,
            interrupted=event.interrupted,
        )
        if event.content:
            storage_event.content = event.content.model_dump(
                exclude_none=True, mode="json"
            )
        if event.grounding_metadata:
            storage_event.grounding_metadata = event.grounding_metadata.model_dump(
                exclude_none=True, mode="json"
            )
        if event.custom_metadata:
            storage_event.custom_metadata = event.custom_metadata
        if event.usage_metadata:
            storage_event.usage_metadata = event.usage_metadata.model_dump(
                exclude_none=True, mode="json"
            )
        if event.citation_metadata:
            storage_event.citation_metadata = event.citation_metadata.model_dump(
                exclude_none=True, mode="json"
            )
        return storage_event

    def to_event(self) -> Event:
        return Event(
            id=self.id,
            invocation_id=self.invocation_id,
            author=self.author,
            branch=self.branch,
            actions=EventActions().model_copy(update=self.actions.model_dump()),
            timestamp=self.timestamp.timestamp(),
            long_running_tool_ids=self.long_running_tool_ids,
            partial=self.partial,
            turn_complete=self.turn_complete,
            error_code=self.error_code,
            error_message=self.error_message,
            interrupted=self.interrupted,
            custom_metadata=self.custom_metadata,
            content=_decode_model(self.content, types.Content),
            grounding_metadata=_decode_model(
                self.grounding_metadata, types.GroundingMetadata
            ),
            usage_metadata=_decode_model(
                self.usage_metadata, types.GenerateContentResponseUsageMetadata
            ),
            citation_metadata=_decode_model(
                self.citation_metadata, types.CitationMetadata
            ),
        )


class StorageAppState(Base):
    """Represents an app state stored in the database."""

    __tablename__ = "app_states"

    app_name: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )
    state: Mapped[MutableDict[str, Any]] = mapped_column(
        MutableDict.as_mutable(DynamicJSON), default={}
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


class StorageUserState(Base):
    """Represents a user state stored in the database."""

    __tablename__ = "user_states"

    app_name: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(DEFAULT_MAX_KEY_LENGTH), primary_key=True
    )
    state: Mapped[MutableDict[str, Any]] = mapped_column(
        MutableDict.as_mutable(DynamicJSON), default={}
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable foreign keys for SQLite connections."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _decode_model(data: dict | None, model_class):
    """Decode a dictionary to a Pydantic model."""
    if data is None:
        return None
    try:
        return model_class(**data)
    except Exception:
        return None


def _merge_state(app_state: dict, user_state: dict, session_state: dict) -> dict:
    """Merge app, user, and session states with proper prefixes."""
    merged_state = copy.deepcopy(session_state)
    for key in app_state.keys():
        merged_state[State.APP_PREFIX + key] = app_state[key]
    for key in user_state.keys():
        merged_state[State.USER_PREFIX + key] = user_state[key]
    return merged_state


def _extract_state_delta(state: dict | None) -> dict:
    """Extract state deltas by prefix (app:, user:, session)."""
    if state is None:
        return {"app": {}, "user": {}, "session": {}}

    app_delta = {}
    user_delta = {}
    session_delta = {}

    for key, value in state.items():
        if key.startswith(State.APP_PREFIX):
            app_delta[key[len(State.APP_PREFIX):]] = value
        elif key.startswith(State.USER_PREFIX):
            user_delta[key[len(State.USER_PREFIX):]] = value
        else:
            session_delta[key] = value

    return {"app": app_delta, "user": user_delta, "session": session_delta}


class SQLiteSessionService(BaseSessionService):
    """
    A session service that uses SQLite for persistent storage.

    Sessions are stored in /app/workspace/databases/adk_sessions.db by default.
    This allows sessions to persist across container restarts and rebuilds.
    """

    def __init__(
        self,
        db_path: str | None = None,
        db_dir: str | None = None,
        db_name: str | None = None,
    ):
        """
        Initialize the SQLite session service.

        Args:
            db_path: Full path to the SQLite database file. If provided, db_dir and db_name are ignored.
            db_dir: Directory for the database file. Defaults to /app/workspace/databases.
            db_name: Name of the database file. Defaults to adk_sessions.db.
        """
        if db_path:
            self.db_path = Path(db_path)
        else:
            db_dir = db_dir or DEFAULT_DB_DIR
            db_name = db_name or DEFAULT_DB_NAME
            self.db_path = Path(db_dir) / db_name

        # Ensure database directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Create database engine
        db_url = f"sqlite:///{self.db_path}"
        logger.info(f"Initializing SQLite session service at: {db_url}")

        self.db_engine: Engine = create_engine(db_url)

        # Enable foreign key constraints for SQLite
        event.listen(self.db_engine, "connect", _set_sqlite_pragma)

        self.metadata: MetaData = MetaData()

        # Create session factory
        self.database_session_factory: sessionmaker[DatabaseSessionFactory] = (
            sessionmaker(bind=self.db_engine)
        )

        # Create all tables
        Base.metadata.create_all(self.db_engine)

        logger.info(f"SQLite session service initialized successfully")

    @override
    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: Optional[dict[str, Any]] = None,
        session_id: Optional[str] = None,
    ) -> Session:
        """Create a new session."""
        with self.database_session_factory() as sql_session:
            # Check if session already exists
            if session_id and sql_session.get(
                StorageSession, (app_name, user_id, session_id)
            ):
                # Return existing session instead of raising error
                logger.info(f"Session {session_id} already exists, returning existing")
                existing = sql_session.get(
                    StorageSession, (app_name, user_id, session_id)
                )
                return existing.to_session(state=existing.state)

            # Fetch or create app state
            storage_app_state = sql_session.get(StorageAppState, (app_name,))
            if not storage_app_state:
                storage_app_state = StorageAppState(app_name=app_name, state={})
                sql_session.add(storage_app_state)

            # Fetch or create user state
            storage_user_state = sql_session.get(
                StorageUserState, (app_name, user_id)
            )
            if not storage_user_state:
                storage_user_state = StorageUserState(
                    app_name=app_name, user_id=user_id, state={}
                )
                sql_session.add(storage_user_state)

            # Extract state deltas
            state_deltas = _extract_state_delta(state)
            app_state_delta = state_deltas["app"]
            user_state_delta = state_deltas["user"]
            session_state = state_deltas["session"]

            # Apply state deltas
            if app_state_delta:
                storage_app_state.state = storage_app_state.state | app_state_delta
            if user_state_delta:
                storage_user_state.state = storage_user_state.state | user_state_delta

            # Create storage session
            storage_session = StorageSession(
                app_name=app_name,
                user_id=user_id,
                id=session_id or str(uuid.uuid4()),
                state=session_state,
            )
            sql_session.add(storage_session)
            sql_session.commit()

            sql_session.refresh(storage_session)

            # Merge states for response
            merged_state = _merge_state(
                storage_app_state.state, storage_user_state.state, session_state
            )
            session = storage_session.to_session(state=merged_state)

        logger.info(f"Created session: {session.id}")
        return session

    @override
    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[Any] = None,
    ) -> Optional[Session]:
        """Get an existing session."""
        with self.database_session_factory() as sql_session:
            storage_session = sql_session.get(
                StorageSession, (app_name, user_id, session_id)
            )
            if storage_session is None:
                return None

            # Query events
            query = sql_session.query(StorageEvent).filter(
                StorageEvent.app_name == app_name,
                StorageEvent.user_id == user_id,
                StorageEvent.session_id == storage_session.id,
            )

            if config and hasattr(config, "after_timestamp") and config.after_timestamp:
                after_dt = datetime.fromtimestamp(config.after_timestamp)
                query = query.filter(StorageEvent.timestamp >= after_dt)

            num_events = None
            if config and hasattr(config, "num_recent_events"):
                num_events = config.num_recent_events

            storage_events = (
                query.order_by(StorageEvent.timestamp.desc())
                .limit(num_events)
                .all()
            )

            # Fetch states
            storage_app_state = sql_session.get(StorageAppState, (app_name,))
            storage_user_state = sql_session.get(
                StorageUserState, (app_name, user_id)
            )

            app_state = storage_app_state.state if storage_app_state else {}
            user_state = storage_user_state.state if storage_user_state else {}
            session_state = storage_session.state

            # Merge states
            merged_state = _merge_state(app_state, user_state, session_state)

            # Convert events
            events = [e.to_event() for e in reversed(storage_events)]
            session = storage_session.to_session(state=merged_state, events=events)

        return session

    @override
    async def list_sessions(
        self, *, app_name: str, user_id: Optional[str] = None
    ):
        """List sessions for an app and optional user."""
        with self.database_session_factory() as sql_session:
            query = sql_session.query(StorageSession).filter(
                StorageSession.app_name == app_name
            )
            if user_id is not None:
                query = query.filter(StorageSession.user_id == user_id)
            results = query.all()

            # Fetch app state
            storage_app_state = sql_session.get(StorageAppState, (app_name,))
            app_state = storage_app_state.state if storage_app_state else {}

            # Fetch user states
            user_states_map = {}
            if user_id is not None:
                storage_user_state = sql_session.get(
                    StorageUserState, (app_name, user_id)
                )
                if storage_user_state:
                    user_states_map[user_id] = storage_user_state.state
            else:
                all_user_states = (
                    sql_session.query(StorageUserState)
                    .filter(StorageUserState.app_name == app_name)
                    .all()
                )
                for storage_user_state in all_user_states:
                    user_states_map[storage_user_state.user_id] = storage_user_state.state

            sessions = []
            for storage_session in results:
                session_state = storage_session.state
                user_state = user_states_map.get(storage_session.user_id, {})
                merged_state = _merge_state(app_state, user_state, session_state)
                sessions.append(storage_session.to_session(state=merged_state))

            # Return a simple object with sessions list
            return type("ListSessionsResponse", (), {"sessions": sessions})()

    @override
    async def delete_session(
        self, app_name: str, user_id: str, session_id: str
    ) -> None:
        """Delete a session."""
        with self.database_session_factory() as sql_session:
            stmt = delete(StorageSession).where(
                StorageSession.app_name == app_name,
                StorageSession.user_id == user_id,
                StorageSession.id == session_id,
            )
            sql_session.execute(stmt)
            sql_session.commit()

        logger.info(f"Deleted session: {session_id}")

    @override
    async def append_event(self, session: Session, event: Event) -> Event:
        """Append an event to a session."""
        if event.partial:
            return event

        with self.database_session_factory() as sql_session:
            storage_session = sql_session.get(
                StorageSession, (session.app_name, session.user_id, session.id)
            )

            if storage_session is None:
                logger.warning(f"Session {session.id} not found in database")
                return event

            # Check for stale session
            if storage_session.update_timestamp_tz > session.last_update_time:
                logger.warning(
                    f"Stale session detected: storage={storage_session.update_timestamp_tz}, "
                    f"session={session.last_update_time}"
                )

            # Fetch states
            storage_app_state = sql_session.get(StorageAppState, (session.app_name,))
            storage_user_state = sql_session.get(
                StorageUserState, (session.app_name, session.user_id)
            )

            # Extract and apply state delta
            if event.actions and event.actions.state_delta:
                state_deltas = _extract_state_delta(event.actions.state_delta)
                app_state_delta = state_deltas["app"]
                user_state_delta = state_deltas["user"]
                session_state_delta = state_deltas["session"]

                if app_state_delta and storage_app_state:
                    storage_app_state.state = storage_app_state.state | app_state_delta
                if user_state_delta and storage_user_state:
                    storage_user_state.state = storage_user_state.state | user_state_delta
                if session_state_delta:
                    storage_session.state = storage_session.state | session_state_delta

            # Store event
            sql_session.add(StorageEvent.from_event(session, event))

            sql_session.commit()
            sql_session.refresh(storage_session)

            # Update session timestamp
            session.last_update_time = storage_session.update_timestamp_tz

        # Also update in-memory session
        await super().append_event(session=session, event=event)
        return event


def create_sqlite_session_service(
    db_dir: str = DEFAULT_DB_DIR,
    db_name: str = DEFAULT_DB_NAME,
) -> SQLiteSessionService:
    """
    Factory function to create a SQLite session service.

    Args:
        db_dir: Directory for the database file. Defaults to /app/workspace/databases.
        db_name: Name of the database file. Defaults to adk_sessions.db.

    Returns:
        SQLiteSessionService instance.
    """
    return SQLiteSessionService(db_dir=db_dir, db_name=db_name)
