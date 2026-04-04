import uuid
from datetime import datetime, timezone
from sqlmodel import Field, Relationship, SQLModel

from app.models.users.user import User


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Shared properties
class UserWorkspaceBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    icon: str | None = Field(default=None, max_length=50)


# Properties to receive on workspace creation
class UserWorkspaceCreate(UserWorkspaceBase):
    pass


# Properties to receive on workspace update
class UserWorkspaceUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    icon: str | None = Field(default=None, max_length=50)


# Database model
class UserWorkspace(UserWorkspaceBase, table=True):
    __tablename__ = "user_workspace"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    owner: User | None = Relationship()


# Properties to return via API
class UserWorkspacePublic(UserWorkspaceBase):
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class UserWorkspacesPublic(SQLModel):
    data: list[UserWorkspacePublic]
    count: int
