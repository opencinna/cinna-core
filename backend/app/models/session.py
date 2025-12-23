import uuid
from datetime import datetime
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON, UniqueConstraint


class Session(SQLModel, table=True):
    __tablename__ = "session"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    environment_id: uuid.UUID = Field(foreign_key="agent_environment.id", ondelete="CASCADE")
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    title: str | None = None
    mode: str = "conversation"  # "building" | "conversation"
    status: str = "active"  # "active" | "paused" | "completed" | "error"
    session_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_message_at: datetime | None = None


class SessionMessage(SQLModel, table=True):
    __tablename__ = "message"
    __table_args__ = (UniqueConstraint("session_id", "sequence_number"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: uuid.UUID = Field(foreign_key="session.id", ondelete="CASCADE")
    role: str  # "user" | "agent" | "system"
    content: str
    sequence_number: int
    message_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))
    timestamp: datetime = Field(default_factory=datetime.utcnow)


# Pydantic Schemas
class SessionCreate(SQLModel):
    agent_id: uuid.UUID  # Will use active environment
    title: str | None = None
    mode: str = "conversation"  # "building" | "conversation"


class SessionUpdate(SQLModel):
    title: str | None = None
    status: str | None = None
    mode: str | None = None


class SessionPublic(SQLModel):
    id: uuid.UUID
    environment_id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    mode: str
    status: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None


class SessionsPublic(SQLModel):
    data: list[SessionPublic]
    count: int


class MessageCreate(SQLModel):
    content: str


class MessagePublic(SQLModel):
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    sequence_number: int
    timestamp: datetime
    message_metadata: dict


class MessagesPublic(SQLModel):
    data: list[MessagePublic]
    count: int
