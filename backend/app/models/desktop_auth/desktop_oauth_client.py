"""Desktop OAuth client model — represents a registered desktop app installation."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class DesktopOAuthClientBase(SQLModel):
    device_name: str = Field(max_length=200)
    platform: str | None = Field(default=None, max_length=50)
    app_version: str | None = Field(default=None, max_length=50)


class DesktopOAuthClient(DesktopOAuthClientBase, table=True):
    __tablename__ = "desktop_oauth_client"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    client_id: str = Field(
        max_length=64,
        sa_column_kwargs={"unique": True, "index": True, "name": "client_id"},
    )
    user_id: UUID = Field(
        foreign_key="user.id",
        ondelete="CASCADE",
        sa_column_kwargs={"index": True},
    )
    is_revoked: bool = Field(default=False)
    last_used_at: datetime | None = Field(default=None, nullable=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DesktopOAuthClientCreate(SQLModel):
    device_name: str = Field(max_length=200)
    platform: str | None = None
    app_version: str | None = None


class DesktopOAuthClientPublic(SQLModel):
    client_id: str
    device_name: str
    platform: str | None = None
    app_version: str | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    is_revoked: bool
