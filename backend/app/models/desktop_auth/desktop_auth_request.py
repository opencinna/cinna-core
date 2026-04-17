"""Desktop auth request model — stores a pending consent flow initiated by GET /authorize."""
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlmodel import Field, SQLModel


class DesktopAuthRequest(SQLModel, table=True):
    """Pending desktop auth consent request, keyed by nonce hash.

    Created when GET /authorize is called (public endpoint).
    The frontend consent page reads display metadata via the nonce.
    Consumed (marked used) when the user approves or denies.
    5-minute TTL enforced at creation and on read.
    """

    __tablename__ = "desktop_auth_request"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    nonce_hash: str = Field(
        sa_column_kwargs={"unique": True, "index": True},
    )
    device_name: str | None = Field(default=None, max_length=200)
    platform: str | None = Field(default=None, max_length=50)
    app_version: str | None = Field(default=None, max_length=50)
    # client_id is None when using lazy registration (no prior POST /clients call)
    client_id: str | None = Field(default=None, max_length=64)
    code_challenge: str = Field(max_length=128)
    redirect_uri: str = Field(max_length=255)
    state: str = Field(max_length=255)
    is_used: bool = Field(default=False)
    expires_at: datetime = Field(
        sa_column_kwargs={"index": True},
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
