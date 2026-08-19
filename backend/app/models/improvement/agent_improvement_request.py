"""AgentImprovementRequest — a consent-gated, frozen share of one session.

A session owner (the *requester*) explicitly hands a **snapshot** of one of
their sessions, plus the tuning-relevant runtime context, to the agent's owner
(the *recipient* — a bundle publisher, or themselves for a standalone agent).

The row is the payload, not a pointer to one: once written, nothing in this
feature ever reads the source ``Session`` again. Continuing or deleting the
conversation does not change what the recipient sees. That "frozen at consent"
invariant is the whole privacy argument, so ``snapshot`` / ``context`` are
written once and never refreshed.

See ``docs/plans/agent_improvement_requests_plan.md`` §3.
"""
import uuid
from datetime import datetime, UTC

from sqlmodel import Field, SQLModel, Column
from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, text


# ── Status vocabulary ────────────────────────────────────────────────
# Deliberately NOT a Postgres enum: ``status`` is a plain VARCHAR like
# ``Session.status`` and ``AgentEnvironment.status``, so adding a value later
# needs no migration. Validation happens in the Pydantic/service layer against
# ``IMPROVEMENT_STATUSES``.
IMPROVEMENT_STATUS_NEW = "new"
IMPROVEMENT_STATUS_IN_PROGRESS = "in_progress"
IMPROVEMENT_STATUS_COMPLETED = "completed"
IMPROVEMENT_STATUS_DECLINED = "declined"

IMPROVEMENT_STATUSES: tuple[str, ...] = (
    IMPROVEMENT_STATUS_NEW,
    IMPROVEMENT_STATUS_IN_PROGRESS,
    IMPROVEMENT_STATUS_COMPLETED,
    IMPROVEMENT_STATUS_DECLINED,
)

# ── Submission source ────────────────────────────────────────────────
# Which surface the requester consented from. Same VARCHAR rationale.
IMPROVEMENT_SOURCE_WEB_UI = "web_ui"
IMPROVEMENT_SOURCE_COMMAND = "command"

IMPROVEMENT_SOURCES: tuple[str, ...] = (
    IMPROVEMENT_SOURCE_WEB_UI,
    IMPROVEMENT_SOURCE_COMMAND,
)

# API-enforced length caps (the columns themselves are unbounded TEXT).
MAX_COMMENT_CHARS = 4000
MAX_RESOLUTION_NOTE_CHARS = 2000

# Recorded in ``context.recipient.fallback_reason`` when target resolution
# could not reach a publisher install and fell back to the source agent.
FALLBACK_PUBLISHER_UNAVAILABLE = "publisher_unavailable"


class AgentImprovementRequest(SQLModel, table=True):
    """Persisted improvement request: frozen snapshot + runtime context."""

    __tablename__ = "agent_improvement_request"
    __table_args__ = (
        # The Configuration-tab card query.
        Index("ix_air_target_status", "target_agent_id", "status"),
        # The CLI cross-agent list ("everything I own, newest first").
        Index("ix_air_owner_created", "owner_user_id", text("created_at DESC")),
        # "My submitted requests".
        Index("ix_air_requester", "requester_user_id"),
        # Per-session rate-limit check.
        Index("ix_air_session", "session_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    # Provenance backlink only — the snapshot outlives the session, so a
    # deleted session detaches (SET NULL) rather than taking the request
    # with it.
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL"
    )
    # The install the session ran on (consumer side). Best-effort provenance.
    source_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    # The receiving agent — the publisher install, or the agent itself. A
    # deleted receiving agent makes the request meaningless, hence CASCADE.
    target_agent_id: uuid.UUID = Field(
        foreign_key="agent.id", nullable=False, ondelete="CASCADE"
    )
    # Set when the source install came from a bundle.
    bundle_uuid: uuid.UUID | None = Field(
        default=None, foreign_key="agent_bundle.id", ondelete="SET NULL"
    )
    # CASCADE is deliberate: a user who deletes their account withdraws the
    # data they shared.
    # Indexed by ``ix_air_requester`` below — no ``index=True`` here, which
    # would add a second, redundant auto-named index on the same column.
    requester_user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    # Denormalised recipient — drives cheap listing + authorization without
    # joining through the target agent. Redundant with the agent cascade, but
    # keeps the table clean when a user is removed.
    owner_user_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )

    # The requester's description of what went wrong (≤ 4000 chars, enforced
    # by the API layer). Rendered as plain text, never markdown.
    comment: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    status: str = Field(
        default=IMPROVEMENT_STATUS_NEW,
        sa_column=Column(
            String(16),
            nullable=False,
            server_default=text(f"'{IMPROVEMENT_STATUS_NEW}'"),
        ),
    )
    # The owner's closing note. Visible to the requester (≤ 2000 chars).
    resolution_note: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    source: str = Field(
        default=IMPROVEMENT_SOURCE_WEB_UI,
        sa_column=Column(
            String(16),
            nullable=False,
            server_default=text(f"'{IMPROVEMENT_SOURCE_WEB_UI}'"),
        ),
    )

    # Frozen transcript (plan §3.2) and frozen runtime context (plan §3.3).
    # Immutable after the write — see the module docstring.
    snapshot: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default=text("'{}'::json")),
    )
    context: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default=text("'{}'::json")),
    )

    # Cheap list-projection fields — so the card never deserializes ``snapshot``.
    snapshot_message_count: int = Field(
        default=0,
        sa_column=Column(Integer, nullable=False, server_default=text("0")),
    )
    snapshot_truncated: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )

    # TIMESTAMPTZ (not the naive TIMESTAMP some older tables use): the rolling
    # 24-hour submission rate-limit compares this column against a tz-aware
    # ``datetime.now(UTC)``, and that comparison must not depend on the database
    # session's timezone setting.
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    # Stamped on every status transition (NULL until the first one).
    status_changed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


# ── API schemas (Pydantic, no table=True) ────────────────────────────


class ImprovementRequestCreate(SQLModel):
    """Submission payload — the consent action."""

    session_id: uuid.UUID
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)
    # Whether to include ``app-data/memory/*.md`` — the agent's personal notes
    # about the requester — in the captured context. Defaults to True because
    # the memory area is injected into every system prompt, so without it the
    # recipient reads a prompt that is not the one that ran. It is nonetheless
    # the requester's personal content, hence a per-submission opt-out rather
    # than an unconditional capture. Prompts are always captured: they are
    # agent configuration, not personal data.
    include_memory: bool = True


class ImprovementRequestPublic(SQLModel):
    """List/row projection.

    ``requester_display`` / ``requester_email`` identify the person who shared
    the session. They are meaningful to the recipient; the requester's own
    projection of their submitted requests carries them too (it is their data).
    """

    id: uuid.UUID
    # The session this request froze. Titles are neither unique nor stable, so
    # without the id two captures of the same conversation are indistinguishable
    # in a listing — the reader has to download both archives to find out they
    # are one report, not two.
    session_id: uuid.UUID | None = None
    target_agent_id: uuid.UUID
    target_agent_name: str | None = None
    source_agent_id: uuid.UUID | None = None
    source_agent_name: str | None = None
    bundle_id: str | None = None
    # Whether the source install came from a bundle, stated rather than left to
    # be inferred from two nullable strings: a git-origin revision carries no
    # version label, so ``installed_version is None`` on a real bundle install
    # is routine and reads as "standalone" to anyone keying off it.
    is_bundle_install: bool = False
    installed_version: str | None = None
    # The install's revision number — the label to fall back on when the
    # revision has no version (``rev 9``).
    installed_revision_number: int | None = None
    requester_display: str | None = None
    requester_email: str | None = None
    comment: str | None = None
    status: str
    resolution_note: str | None = None
    source: str
    snapshot_message_count: int
    snapshot_truncated: bool
    created_at: datetime
    status_changed_at: datetime | None = None


class ImprovementRequestDetailPublic(ImprovementRequestPublic):
    """Detail projection — adds the whole frozen context block."""

    context: dict = Field(default_factory=dict)
    session_title: str | None = None


class ImprovementRequestsPublic(SQLModel):
    data: list[ImprovementRequestPublic]
    count: int


class ImprovementRequestUpdate(SQLModel):
    """Recipient-only status / resolution-note edit."""

    status: str | None = None
    resolution_note: str | None = Field(
        default=None, max_length=MAX_RESOLUTION_NOTE_CHARS
    )


class ImprovementContextPublic(SQLModel):
    """The consent modal's pre-flight payload.

    Produced by the same gate + target resolution the submission runs, so the
    modal's copy can never disagree with what submitting will actually do.
    """

    eligible: bool
    reason: str | None = None
    is_shared_externally: bool = False
    recipient_display: str | None = None
    target_agent_name: str | None = None
    bundle_id: str | None = None
    installed_version: str | None = None
    message_count: int = 0
    existing_request_count: int = 0
