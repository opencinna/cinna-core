import uuid
from datetime import datetime, UTC
from sqlalchemy import Index
from sqlmodel import SQLModel, Field

# Event type constants
CREDENTIAL_READ_ATTEMPT = "CREDENTIAL_READ_ATTEMPT"
CREDENTIAL_BASH_ACCESS = "CREDENTIAL_BASH_ACCESS"
OUTPUT_REDACTED = "OUTPUT_REDACTED"
CREDENTIAL_WRITE_ATTEMPT = "CREDENTIAL_WRITE_ATTEMPT"

# ── Two-Factor Authentication (MFA) event-type constants ─────────────
# Emitted by ``MfaService`` and the login routes during 2FA enrollment,
# verification, and management flows. See the audit-trail section of
# ``docs/drafts/user-2fa-passkeys-totp_plan.md``.
MFA_ENROLLED = "MFA_ENROLLED"
MFA_DISABLED = "MFA_DISABLED"
MFA_CHALLENGE_ISSUED = "MFA_CHALLENGE_ISSUED"
MFA_CHALLENGE_SUCCESS = "MFA_CHALLENGE_SUCCESS"
MFA_CHALLENGE_FAILED = "MFA_CHALLENGE_FAILED"
MFA_RECOVERY_CODE_USED = "MFA_RECOVERY_CODE_USED"
MFA_RATE_LIMITED = "MFA_RATE_LIMITED"
MFA_SIGN_COUNT_REGRESSION = "MFA_SIGN_COUNT_REGRESSION"
MFA_PASSKEY_INVALID_ORIGIN = "MFA_PASSKEY_INVALID_ORIGIN"
MFA_RECOVERY_CODES_REGENERATED = "MFA_RECOVERY_CODES_REGENERATED"
# Trusted-device ("Do not ask on this device") audit events. Registered
# when a trusted-device token is minted on verify; Used when a valid
# token skips the login challenge. The plaintext token is never logged.
MFA_TRUSTED_DEVICE_REGISTERED = "MFA_TRUSTED_DEVICE_REGISTERED"
MFA_TRUSTED_DEVICE_USED = "MFA_TRUSTED_DEVICE_USED"

# ── Agent environment console event-type constants ───────────────────
# Emitted by ``EnvironmentConsoleService`` when an owner+developer opens or
# closes an interactive web terminal against their agent's Docker environment.
# A full shell exposes synced credential files, so every open/close is audited
# with the acting user and source IP. ``event_type`` is a free-form ``str``
# column (no Postgres enum) so these constants need no migration.
AGENT_ENV_TERMINAL_OPENED = "AGENT_ENV_TERMINAL_OPENED"
AGENT_ENV_TERMINAL_CLOSED = "AGENT_ENV_TERMINAL_CLOSED"

# ── Account CLI workspace event-type constants ───────────────────────
# Emitted by ``AccountCLIService``. An account CLI token is a mint-and-discover
# credential that can spawn per-agent (building) child tokens, so its creation
# and every child mint are audited with the acting user and source IP.
CLI_ACCOUNT_TOKEN_CREATED = "CLI_ACCOUNT_TOKEN_CREATED"
CLI_ACCOUNT_CHILD_TOKEN_MINTED = "CLI_ACCOUNT_CHILD_TOKEN_MINTED"
CLI_ACCOUNT_CHILD_TOKEN_REVOKED = "CLI_ACCOUNT_CHILD_TOKEN_REVOKED"
# ── Phase 3 — convenience verbs + generic API escape hatch ───────────
# ``CLI_ACCOUNT_CONNECT_*`` are discrete, infrequent, state-changing grants
# (wiring two agents together) and are audited per call. ``API_PROXY_CALL`` is
# written ONLY on an exclusion hit (someone/something probing an off-limits
# surface through the escape hatch) — allowed proxy calls are not audited here
# (the inner route audits its own sensitive writes; per-call audit would flood).
CLI_ACCOUNT_CONNECT_AGENT_API = "CLI_ACCOUNT_CONNECT_AGENT_API"
CLI_ACCOUNT_CONNECT_MCP = "CLI_ACCOUNT_CONNECT_MCP"
CLI_ACCOUNT_API_PROXY_CALL = "CLI_ACCOUNT_API_PROXY_CALL"
# ── Account-CLI credential drafting verbs ────────────────────────────
# The account CLI scaffolds credentials as *drafts* (no secret values — the user
# fills them in the UI) and wires them to agents. The account token can never
# read or write a credential's secret value (Decision 6); these verbs only touch
# metadata + structure, so each discrete state-changing call is audited.
CLI_ACCOUNT_CREDENTIAL_CREATED = "CLI_ACCOUNT_CREDENTIAL_CREATED"
CLI_ACCOUNT_CREDENTIAL_UPDATED = "CLI_ACCOUNT_CREDENTIAL_UPDATED"
CLI_ACCOUNT_CREDENTIAL_DELETED = "CLI_ACCOUNT_CREDENTIAL_DELETED"
CLI_ACCOUNT_CREDENTIAL_SHARED_WITH_AGENT = "CLI_ACCOUNT_CREDENTIAL_SHARED_WITH_AGENT"
# ── Account-CLI agent-api producer management ─────────────────────────
# Toggling a producer agent's REST API on/off is a discrete state change
# (it opens/closes the consumer-facing proxy surface) and is audited per call.
# ``_refresh`` (re-harvest spec/policy) and spec reads are diagnostic, not
# state-changing grants, so they are not audited (mirrors the unaudited
# credential *reads*).
CLI_ACCOUNT_AGENT_API_ENABLED = "CLI_ACCOUNT_AGENT_API_ENABLED"

# Restarting a producer/agent environment from the account CLI is a discrete,
# build-rights state change (it bounces the running container), so it is audited
# per call. ``agent-api call`` and ``agent show`` are diagnostic reads/previews
# and are not audited (mirrors ``_refresh`` / spec reads above).
CLI_ACCOUNT_ENV_RESTARTED = "CLI_ACCOUNT_ENV_RESTARTED"


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
    __table_args__ = (
        Index("ix_security_event_guest_share_created", "guest_share_id", "created_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), index=True
    )

    # Context — who and where
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL", index=True
    )
    environment_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_environment.id", ondelete="SET NULL"
    )
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL", index=True
    )
    guest_share_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_guest_share.id", ondelete="SET NULL"
    )

    # Event classification
    event_type: str = Field(index=True)  # See constants above
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
