"""Pending Google OAuth authorization states for credentials (table only).

One row per started authorization (web or Desktop). The raw state never
touches the database: ``state_hash`` is ``sha256(state).hexdigest()``. A row is
consumed (deleted) exactly once by the callback; expired rows are purged
opportunistically when a new flow starts. Never exposed through the API.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

CredentialOAuthOrigin = Literal["web", "desktop"]


class CredentialOAuthState(SQLModel, table=True):
    __tablename__ = "credential_oauth_state"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    state_hash: str = Field(max_length=64, unique=True, index=True)
    credential_id: uuid.UUID = Field(
        foreign_key="credential.id", nullable=False, ondelete="CASCADE", index=True
    )
    user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # "web" | "desktop" (CredentialOAuthOrigin); plain string, no PG enum.
    origin: str = Field(max_length=16)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={"server_default": sa.func.now()},
    )
    expires_at: datetime = Field(sa_type=sa.DateTime(timezone=True), index=True)
