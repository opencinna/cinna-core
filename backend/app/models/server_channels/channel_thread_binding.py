"""ChannelThreadBinding — the (channel, thread) → (user, agent, session) map.

This is the conversation state of the Server Channels feature. One row per
external thread; ``thread_key`` is always the channel-native thread identity
(Google Chat: ``message.thread.name``, e.g. ``spaces/AAA/threads/BBB``).

Why a dedicated table rather than a column on ``Session`` (the approach the
email integration takes): the binding exists *before* the session does — it
is created the moment an auto-install starts, while the environment is still
building and inbound messages have to be parked. It also makes resume
routing-free: a thread that already has a binding never re-runs the router,
the same fixed-agent-per-context principle App MCP uses.

Lifecycle::

    pending_install ──(env running, messages flushed)──▶ active
            │
            └──(env build / install failed)──▶ failed
                     │
                     └──(next inbound message deletes the row and re-routes)

``failed`` is terminal only until the next message: self-heal by deletion +
re-routing, so a transient build failure never wedges a thread permanently.
FK cascades are part of the design: uninstalling the agent or deleting the
channel drops the binding (next message re-routes); deleting the session
nulls ``session_id`` (next message opens a fresh session on the same agent).
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Text, UniqueConstraint
from sqlmodel import Column, Field, SQLModel

# Binding status values. Plain constants (not an Enum) to match the codebase's
# status-string convention and keep the column a bare varchar.
CHANNEL_BINDING_PENDING_INSTALL = "pending_install"
CHANNEL_BINDING_ACTIVE = "active"
CHANNEL_BINDING_FAILED = "failed"

CHANNEL_BINDING_STATUSES = (
    CHANNEL_BINDING_PENDING_INSTALL,
    CHANNEL_BINDING_ACTIVE,
    CHANNEL_BINDING_FAILED,
)


class ChannelThreadBinding(SQLModel, table=True):
    """Binds one external channel thread to one platform session."""

    __tablename__ = "channel_thread_binding"
    __table_args__ = (
        UniqueConstraint(
            "server_channel_id", "thread_key", name="uq_channel_thread_binding_thread"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    server_channel_id: uuid.UUID = Field(
        foreign_key="server_channel.id", ondelete="CASCADE"
    )
    # Channel-native thread identity. Unique per channel (see __table_args__).
    thread_key: str = Field(max_length=512)
    # The resolved platform user — the external sender's own account. Sessions
    # for this binding are owned by this user, never by the agent publisher.
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    # Uninstalling the agent cascades the binding away ⇒ next message re-routes.
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")
    # NULL while pending_install, or after the session was deleted.
    session_id: uuid.UUID | None = Field(
        default=None, foreign_key="session.id", ondelete="SET NULL", index=True
    )
    status: str = Field(default=CHANNEL_BINDING_PENDING_INSTALL, max_length=32)
    # Messages parked while the environment builds:
    # [{"text": str, "external_message_id": str | None, "received_at": str}]
    #
    # Plain JSON column: ``binding.pending_messages.append(...)`` is NOT
    # dirty-tracked and the commit silently drops the parked message. Assign a
    # new list, or call
    # ``sqlalchemy.orm.attributes.flag_modified(binding, "pending_messages")``
    # before committing — the convention used across this codebase.
    pending_messages: list = Field(
        default_factory=list, sa_column=Column(JSON, nullable=False)
    )
    # Webhook redelivery dedup — channels re-send on a slow ack.
    last_external_message_id: str | None = Field(default=None, max_length=255)
    last_error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
