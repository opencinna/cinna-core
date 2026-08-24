import uuid
from datetime import UTC, datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

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

# ── Account-CLI schedule + status management ──────────────────────────
# Schedule create / update / delete / run are discrete state changes to an
# agent's automatic-execution config (run also spends tokens / spins a session),
# so each is audited per call. List / generate-preview / logs are diagnostic
# reads and are not audited (mirrors the unaudited credential reads / spec reads).
CLI_ACCOUNT_SCHEDULE_CREATED = "CLI_ACCOUNT_SCHEDULE_CREATED"
CLI_ACCOUNT_SCHEDULE_UPDATED = "CLI_ACCOUNT_SCHEDULE_UPDATED"
CLI_ACCOUNT_SCHEDULE_DELETED = "CLI_ACCOUNT_SCHEDULE_DELETED"
CLI_ACCOUNT_SCHEDULE_RUN = "CLI_ACCOUNT_SCHEDULE_RUN"
# Setting an agent's status-refresh pre-command is a config state change and is
# audited. Reading / force-refreshing the status is a diagnostic read — not
# audited (mirrors the REST status read).
CLI_ACCOUNT_STATUS_COMMAND_SET = "CLI_ACCOUNT_STATUS_COMMAND_SET"

# ── Account-CLI device-login (``cinna login``) ────────────────────────
# A device-login approval mints a fresh account CLI token (same audit as the
# setup-token path via ``CLI_ACCOUNT_TOKEN_CREATED``); these record the
# browser-side decision (who approved/rejected, which machine, source IP). Start
# and poll are unauthenticated with no user yet, so they are not audited here —
# the approval is the moment a user is bound to the request.
CLI_DEVICE_LOGIN_APPROVED = "CLI_DEVICE_LOGIN_APPROVED"
CLI_DEVICE_LOGIN_REJECTED = "CLI_DEVICE_LOGIN_REJECTED"

# ── Agent-API per-user access grant (L2 scopes) ───────────────────────
# Emitted by ``AgentApiGrantService`` when a producer agent's owner assigns,
# edits, or removes the scopes granted to a platform user on the agent's REST
# API. Each grant is a discrete capability change (it controls what that user may
# do through the proxy), so create / update / delete are audited per call —
# mirrors the MCP connector ACL audit. The identity/scope tokens themselves are
# never logged (the grant carries no secret, only scope names).
AGENT_API_GRANT_CREATED = "AGENT_API_GRANT_CREATED"
AGENT_API_GRANT_UPDATED = "AGENT_API_GRANT_UPDATED"
AGENT_API_GRANT_DELETED = "AGENT_API_GRANT_DELETED"

# ── Agent-API external keys ───────────────────────────────────────────
# Emitted by ``AgentApiKeyService``. An external key is a copy-pasteable,
# identity-bound bearer credential for code running OUTSIDE the platform, so its
# whole lifecycle is audited: minting (who issued it, for whom), revoking, and
# every reveal of the value on the credential detail page. The token value and
# its hash are NEVER logged — only the 8-char display prefix.
AGENT_API_EXTERNAL_KEY_CREATED = "AGENT_API_EXTERNAL_KEY_CREATED"
AGENT_API_EXTERNAL_KEY_REVOKED = "AGENT_API_EXTERNAL_KEY_REVOKED"
AGENT_API_EXTERNAL_KEY_REVEALED = "AGENT_API_EXTERNAL_KEY_REVEALED"

# ── Agent improvement requests ────────────────────────────────────────
# Written by the web and CLI archive-download routes when, and only when,
# ``owner_user_id != requester_user_id`` — i.e. the one cross-user data path in
# the platform, where user A's conversation content is read by user B. A
# same-user download (an owner reading a request on their own agent) is not
# audited. Payload carries the request id, target agent id, bundle id, requester
# user id, acting user id, and source IP — never any snapshot content.
IMPROVEMENT_ARCHIVE_DOWNLOADED = "IMPROVEMENT_ARCHIVE_DOWNLOADED"

# ── Server channels ───────────────────────────────────────────────────
# The channel webhook is the platform's only unauthenticated ingress that can
# create sessions, so both its admin lifecycle and its rejections are audited.
#
# The two rejection events are attacker-triggerable and therefore THROTTLED at
# the emit site (one row per source per window) — an unthrottled audit row on a
# public endpoint is itself a denial-of-service vector. Neither payload carries
# message text or the bearer JWT.
#
# Admin-action rows are attributed to the acting superuser; rejection rows are
# attributed to the channel's creator (there is no authenticated user to blame
# for an anonymous request), and the auto-register row to the account created.
SERVER_CHANNEL_CREATED = "SERVER_CHANNEL_CREATED"
# Also carries auto-install *list* mutations, distinguished by an ``action``
# key in the payload (``auto_install_list_add`` / ``auto_install_list_remove``)
# rather than by their own event types — they are edits to channel routing
# configuration, and a reader filtering on this type wants to see them.
SERVER_CHANNEL_UPDATED = "SERVER_CHANNEL_UPDATED"
SERVER_CHANNEL_DELETED = "SERVER_CHANNEL_DELETED"
SERVER_CHANNEL_TOKEN_REGENERATED = "SERVER_CHANNEL_TOKEN_REGENERATED"
# Inbound request failed adapter signature verification.
SERVER_CHANNEL_VERIFICATION_FAILED = "SERVER_CHANNEL_VERIFICATION_FAILED"
# Verified sender was not on the channel's email whitelist (or is inactive).
SERVER_CHANNEL_SENDER_DENIED = "SERVER_CHANNEL_SENDER_DENIED"
# A passwordless, transport-confirmed account was created for a whitelisted
# sender. Provenance (which channel) is in the payload.
SERVER_CHANNEL_USER_AUTO_REGISTERED = "SERVER_CHANNEL_USER_AUTO_REGISTERED"
# Pass-2 routing installed a catalog bundle for an external sender.
SERVER_CHANNEL_AUTO_INSTALL = "SERVER_CHANNEL_AUTO_INSTALL"
# A user turned identity routing on or off for themselves on one channel
# (``channel_user_setting.allow_identity_routing``). Attributed to that user —
# this is the one channel row written by an ordinary person about their own
# settings rather than by an admin or the webhook.
#
# Audited because of what the "on" state permits, which no other per-user
# channel setting does: a message of theirs can open a session inside ANOTHER
# person's workspace, owned by that person and readable by them. It is opt-in,
# per person, and it never inherits from a channel default (master plan §3.4) —
# so it is only ever true because somebody deliberately made it true, and that
# is exactly the fact worth being able to establish afterwards. The payload
# carries the channel and the new value; it never carries message text, in
# keeping with every other row in this section.
SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED = "SERVER_CHANNEL_IDENTITY_ROUTING_CHANGED"
# A superuser sent an admin test message out through a channel. Audited
# because the target may be a *named person's* real conversation: the
# email-targeted form resolves to a thread belonging to an identified user, so
# this writes arbitrary text into somewhere they read. The debug buffer records
# it too, but that is in-memory and clearable — this is the durable record.
SERVER_CHANNEL_TEST_SEND = "SERVER_CHANNEL_TEST_SEND"
# A superuser cleared stored routing traces (one channel's, or all of them).
# Audited because this is not merely destructive: it is one of the two paths
# that actually ERASE external senders' stored message text, and the one the
# admin UI names to an operator who has just turned the text gate off (the other
# being retention expiry). A privacy control that leaves no record of having
# been used cannot be shown to have been used. Payload carries the scope and the
# row count — never a message body, as with SERVER_CHANNEL_TEST_SEND above.
ROUTING_TRACES_CLEARED = "ROUTING_TRACES_CLEARED"
# A superuser ran routing simulate or replay against ANOTHER account's routing
# state (`details.mode` says which). Audited because of what the response
# contains: which agents and bundles that user has installed, their names, and
# their owners' trigger prompts. Nearly all of that is already visible in a
# stored routing_decision row the moment the user sends one message — what
# simulate adds is that it does not have to wait for them to send one, so an
# admin can enumerate a user who has never touched the channel. That reach is
# deliberate (the tool's main use is diagnosing a first message that failed to
# route, which by definition has no trace), so this row plus the per-admin rate
# limit are what keep it accountable and non-bulk rather than narrowing it.
#
# Payload names BOTH ends — the acting admin (SecurityEvent.user_id) and the
# target (details.target_user_id / target_user_email). An audit row saying only
# "an admin ran a simulate" does not answer the question anybody would later
# ask, which is "against whom". Never the message body, following
# SERVER_CHANNEL_TEST_SEND: these rows are broadly readable, and the message
# lives on the routing_decision row behind the superuser-only trace API and the
# ROUTING_TRACE_STORE_MESSAGE_TEXT gate. Written BEFORE the run, not after, so
# a simulate that spends LLM budget and then fails still leaves a record.
ROUTING_SIMULATE_RUN = "ROUTING_SIMULATE_RUN"


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
