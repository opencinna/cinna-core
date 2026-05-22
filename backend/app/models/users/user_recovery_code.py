"""Single-use 2FA recovery code.

Eight codes are generated at first enrollment (or regeneration); each
row tracks one code's lifecycle.  Stored as a bcrypt hash (same
``get_password_hash`` used for user passwords).  Plaintext is shown
exactly once at generation time and never again.
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class UserRecoveryCode(SQLModel, table=True):
    """Bcrypt-hashed recovery code.

    A ``batch_id`` groups all 8 codes from one generation event so
    regeneration can wipe a whole batch atomically.
    """
    __tablename__ = "user_recovery_code"
    __table_args__ = (
        Index("ix_user_recovery_code_user_used", "user_id", "used_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(
        foreign_key="user.id", ondelete="CASCADE", index=True
    )
    code_hash: str = Field()
    used_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    batch_id: uuid.UUID = Field()


# ── Public schemas ─────────────────────────────────────────────────────


class RecoveryCodeStatus(SQLModel):
    """Response of ``GET /users/me/mfa/recovery-codes``.

    Never includes the plaintext codes — those are returned exactly
    once at generation/regeneration time via
    :class:`RecoveryCodesPlaintext`.
    """
    remaining_count: int
    total_count: int
    last_regenerated_at: datetime | None


class RecoveryCodesPlaintext(SQLModel):
    """One-shot response containing fresh plaintext recovery codes.

    Returned by ``POST /users/me/mfa/recovery-codes/regenerate`` and by
    the enrollment-finish flows that turn 2FA on for the first time.
    """
    codes: list[str]
    generated_at: datetime
    regenerate_warning: bool = True
