import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field

# Event type constants
CREDENTIAL_READ_ATTEMPT = "CREDENTIAL_READ_ATTEMPT"
CREDENTIAL_BASH_ACCESS = "CREDENTIAL_BASH_ACCESS"
OUTPUT_REDACTED = "OUTPUT_REDACTED"
CREDENTIAL_WRITE_ATTEMPT = "CREDENTIAL_WRITE_ATTEMPT"


class SecurityEvent(SQLModel, table=True):
    """
    Security event log — records credential access attempts, output redaction
    triggers, and other security-relevant patterns for audit and future policy
    evaluation.

    Event types:
    - CREDENTIAL_READ_ATTEMPT: SDK tool interceptor detected credential file read
    - CREDENTIAL_BASH_ACCESS: Bash command matched credential-access pattern
    - OUTPUT_REDACTED: Credential value found and redacted in agent output
    - CREDENTIAL_WRITE_ATTEMPT: Attempt to write/edit credential files
    """
    __tablename__ = "security_event"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Context — who and where
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    environment_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_environment.id", ondelete="SET NULL"
    )
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL"
    )
    guest_share_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_guest_share.id", ondelete="SET NULL"
    )

    # Event classification
    event_type: str  # See constants above
    severity: str = Field(default="medium")  # "low", "medium", "high", "critical"

    # Free-form details stored as JSON string
    details: str = Field(default="{}")

    # Reserved for future risk scoring engine
    risk_score: float | None = Field(default=None)


# --- Pydantic schemas ---

class SecurityEventCreate(SQLModel):
    agent_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    guest_share_id: uuid.UUID | None = None
    event_type: str
    severity: str = "medium"
    details: dict = Field(default_factory=dict)


class SecurityEventPublic(SQLModel):
    id: uuid.UUID
    created_at: datetime
    user_id: uuid.UUID
    agent_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    session_id: uuid.UUID | None
    guest_share_id: uuid.UUID | None
    event_type: str
    severity: str
    details: dict
    risk_score: float | None


class SecurityEventsPublic(SQLModel):
    data: list[SecurityEventPublic]
    count: int
