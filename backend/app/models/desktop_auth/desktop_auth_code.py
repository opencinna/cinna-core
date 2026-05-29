"""Desktop authorization code model — ephemeral, single-use, 5-minute TTL."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index
from sqlmodel import Field, SQLModel


class DesktopAuthCode(SQLModel, table=True):
    __tablename__ = "desktop_auth_code"
    __table_args__ = (
        Index("ix_desktop_auth_code_hash", "code_hash", unique=True),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code_hash: str = Field()
    user_id: UUID = Field(
        foreign_key="user.id",
        ondelete="CASCADE",
    )
    client_id: str = Field(max_length=64)
    code_challenge: str = Field(max_length=128)
    redirect_uri: str = Field(max_length=255)
    is_used: bool = Field(default=False)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=DateTime(timezone=True),
    )
