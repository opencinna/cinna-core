"""
CLI Device-Login Request model + schemas (RFC 8628 device-authorization grant).

Server-side state machine for one ``cinna login`` attempt. The CLI starts a
request (unauthenticated), prints a ``user_code`` and opens the browser; a
signed-in platform user approves/rejects it; the CLI polls until the backend
returns a fresh account CLI token.

Security notes:
- Only the SHA-256 ``device_code_hash`` is stored — the raw ``device_code`` is
  returned to the CLI once and never persisted (mirrors ``CLIToken.token_hash``).
- ``user_code`` is stored NORMALIZED (uppercase, no dashes) for lookup; the
  dashed form is purely a display concern.
- ``account_token_jwt`` is a TRANSIENT raw JWT held only between approval and the
  first successful ``authorized`` poll, then nulled (the row is single-use and
  short-lived).
"""
import uuid
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


class CLIDeviceLoginRequest(SQLModel, table=True):
    __tablename__ = "cli_device_login_request"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # SHA-256 of the raw device_code. The CLI echoes the raw value; we look up by
    # hash. Never store plaintext.
    device_code_hash: str = Field(max_length=128, index=True, unique=True)
    # Human code, normalized uppercase with no dashes (e.g. "WX7K9Q2P"). Indexed
    # for the browser display lookup. Not column-unique — collisions are handled
    # by a generation-time retry in the service.
    user_code: str = Field(max_length=16, index=True)
    # One of: pending, approved, denied, expired, consumed. See the service
    # state machine.
    status: str = Field(default="pending", max_length=20, index=True)
    # Display label + becomes the minted CLIToken.name.
    machine_name: str = Field(max_length=100)
    # Display label + minted CLIToken.machine_info.
    machine_info: str | None = Field(default=None, max_length=200)
    # Set at approval — binds the request to whoever approved it.
    approved_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", nullable=True, ondelete="CASCADE"
    )
    # The CLIToken minted at approval. SET NULL so revoking/deleting the token
    # row doesn't orphan-delete this audit row.
    minted_token_id: uuid.UUID | None = Field(
        default=None, foreign_key="cli_token.id", nullable=True, ondelete="SET NULL"
    )
    # Transient raw JWT, stored only between approval and the first authorized
    # poll, then nulled.
    account_token_jwt: str | None = Field(default=None)
    # Source IP at start (audit).
    client_ip: str | None = Field(default=None, max_length=64)
    # Last poll timestamp — drives the per-request slow_down.
    last_polled_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime


# ── Pydantic request / response schemas (no table) ───────────────────────────


class DeviceLoginStartRequest(SQLModel):
    """CLI → backend: begin a device-login request.

    Lengths mirror the table columns so over-long labels are rejected as a clean
    422 at this unauthenticated endpoint rather than a DB truncation 500.
    """

    machine_name: str = Field(max_length=100)
    machine_info: str | None = Field(default=None, max_length=200)


class DeviceLoginStartResponse(SQLModel):
    """Backend → CLI: device + user codes and the verification URLs (RFC 8628)."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    expires_in: int


class DeviceLoginPollRequest(SQLModel):
    """CLI → backend: poll with the raw device_code."""

    device_code: str


class DeviceLoginPollResponse(SQLModel):
    """Backend → CLI: always HTTP 200; the flow state lives in ``status``.

    Only the ``authorized`` state carries the extra fields; the route uses
    ``response_model_exclude_none`` so the other statuses are bare ``{status}``.
    """

    status: str
    account_token: str | None = None
    platform_url: str | None = None
    frontend_url: str | None = None
    machine_name: str | None = None


class DeviceLoginRequestPublic(SQLModel):
    """Browser display metadata. No device_code, token, IP, or approver."""

    user_code: str
    machine_name: str
    machine_info: str | None
    status: str


class DeviceLoginResolveBody(SQLModel):
    """Browser → backend: approve / reject body."""

    user_code: str
