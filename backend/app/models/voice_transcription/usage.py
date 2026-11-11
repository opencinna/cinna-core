"""Voice transcription relay: per-session usage ledger and API shapes.

One ``VoiceTranscriptionUsage`` row is written when a voice session that sent
a valid ``start`` frame ends (written once, by the session service). It holds
durations and outcome only: never transcript text, audio, keys or tokens.
The monthly cap sums ``audio_seconds`` per user over the current month, which
is what the ``(user_id, started_at)`` index serves.
"""
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import DateTime, Index
from sqlmodel import Field, SQLModel

VOICE_CLIENT_KIND_DESKTOP = "desktop"
VOICE_CLIENT_KIND_MOBILE = "mobile"
VOICE_CLIENT_KIND_WEB = "web"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class VoiceTranscriptionUsage(SQLModel, table=True):
    __tablename__ = "voice_transcription_usage"
    __table_args__ = (
        Index("ix_voice_transcription_usage_user_started", "user_id", "started_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", nullable=False)
    # The relay's own session id (not a chat session); one row per session.
    session_id: uuid.UUID = Field(nullable=False, unique=True)
    started_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    ended_at: datetime = Field(
        default_factory=_utc_now, sa_type=DateTime(timezone=True), nullable=False
    )
    # PCM bytes accepted / 32 000, replayed audio included.
    audio_seconds: float = Field(default=0.0, nullable=False)
    # The client's declared replay, clamped to [0, audio_seconds]; reporting only.
    replay_seconds: float = Field(default=0.0, nullable=False)
    provider_id: uuid.UUID | None = Field(
        default=None, foreign_key="ai_provider.id", ondelete="SET NULL"
    )
    provider_type: str | None = Field(default=None, max_length=50)
    model: str | None = Field(default=None, max_length=255)
    # ``desktop`` / ``mobile`` / ``web`` from the token's ``client_kind``.
    client_kind: str = Field(default=VOICE_CLIENT_KIND_WEB, max_length=16, nullable=False)
    # ``stop`` | ``cancelled`` | a protocol error code.
    outcome: str = Field(max_length=32, nullable=False)


class VoiceTranscriptionUsagePublic(SQLModel):
    """The caller's own usage for the current month."""
    enabled: bool
    used_seconds: float
    # ``None`` = no cap.
    cap_seconds: float | None
    period_end: datetime


class VoiceTranscriptionUsageRow(SQLModel):
    user_id: uuid.UUID
    email: str
    full_name: str | None = None
    seconds: float
    sessions: int


class VoiceTranscriptionUsageReport(SQLModel):
    """Admin per-user usage for one month."""
    period_start: datetime
    period_end: datetime
    rows: list[VoiceTranscriptionUsageRow]
    total_seconds: float
    total_sessions: int


class VoiceTranscriptionProviderOption(SQLModel):
    """A speech-capable AI provider an admin may pick."""
    id: uuid.UUID
    name: str
    type: str


class VoiceTranscriptionTestRequest(SQLModel):
    """Admin "Test": probe this provider, or the configured one when omitted."""
    provider_id: uuid.UUID | None = None


class VoiceTranscriptionTestResult(SQLModel):
    ok: bool
    error: Literal["invalid_key", "unreachable", "quota", "disabled"] | None = None
