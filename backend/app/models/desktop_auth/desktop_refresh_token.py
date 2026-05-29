"""Desktop refresh token model — stores hashed refresh tokens with rotation support."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index
from sqlmodel import Field, SQLModel


class DesktopRefreshToken(SQLModel, table=True):
    __tablename__ = "desktop_refresh_token"
    __table_args__ = (
        Index("ix_desktop_refresh_token_hash", "token_hash", unique=True),
        Index("ix_desktop_refresh_token_family", "token_family"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    client_id: UUID = Field(
        foreign_key="desktop_oauth_client.id",
        ondelete="CASCADE",
        sa_column_kwargs={"index": True},
    )
    user_id: UUID = Field(
        foreign_key="user.id",
        ondelete="CASCADE",
    )
    token_hash: str = Field()
    token_family: UUID = Field()
    is_revoked: bool = Field(default=False)
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=DateTime(timezone=True),
    )
