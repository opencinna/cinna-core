"""Desktop refresh token model — stores hashed refresh tokens with rotation support."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class DesktopRefreshToken(SQLModel, table=True):
    __tablename__ = "desktop_refresh_token"

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
    token_hash: str = Field(
        sa_column_kwargs={"unique": True, "index": True},
    )
    token_family: UUID = Field(
        sa_column_kwargs={"index": True},
    )
    is_revoked: bool = Field(default=False)
    expires_at: datetime = Field()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
