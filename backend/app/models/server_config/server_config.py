import uuid
from datetime import UTC, datetime

from sqlalchemy import Text
from sqlmodel import Field, SQLModel


class ServerConfig(SQLModel, table=True):
    """
    Singleton server-wide configuration.

    Only one row ever exists; it is created lazily on first access. Holds the
    admin-configurable disclaimer settings shown to users at login.
    """
    __tablename__ = "server_config"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Disclaimer settings
    disclaimer_enabled: bool = Field(default=False)
    disclaimer_markdown: str = Field(default="", sa_type=Text)
    # "new_users" -> show once per browser; "every_login" -> show once per session
    disclaimer_display_mode: str = Field(default="new_users")
    # Bumped on every content/mode change so acknowledged users re-see edits.
    disclaimer_version: int = Field(default=1)

    # Audit
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_by_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )


class ServerConfigUpdate(SQLModel):
    """Admin update payload — all fields optional."""
    disclaimer_enabled: bool | None = None
    disclaimer_markdown: str | None = None
    disclaimer_display_mode: str | None = None


class DisclaimerPublic(SQLModel):
    """Disclaimer projection returned to any authenticated user."""
    enabled: bool
    markdown: str
    display_mode: str
    version: int
