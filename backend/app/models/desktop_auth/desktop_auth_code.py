"""Desktop authorization code model — ephemeral, single-use, 5-minute TTL."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class DesktopAuthCode(SQLModel, table=True):
    __tablename__ = "desktop_auth_code"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    code_hash: str = Field(
        sa_column_kwargs={"unique": True, "index": True},
    )
    user_id: UUID = Field(
        foreign_key="user.id",
        ondelete="CASCADE",
    )
    client_id: str = Field(max_length=64)
    code_challenge: str = Field(max_length=128)
    redirect_uri: str = Field(max_length=255)
    is_used: bool = Field(default=False)
    expires_at: datetime = Field()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
