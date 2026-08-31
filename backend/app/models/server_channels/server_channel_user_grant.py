"""ServerChannelUserGrant — the per-user allowlist for a restricted channel.

A grant answers exactly one question: *may this person use this channel at
all?* It is consulted **only** when ``ServerChannel.visibility`` is not
``"public"``. A public channel needs no rows here and never gets any — the
absence of grants on a public channel is not "nobody is allowed", it is "the
question was not asked".

That asymmetry is deliberate. Materializing a grant per user at channel-create
time would break for the two populations this feature is built around: senders
the channel auto-registered (they have no UI session in which a row could be
created) and every user created after the channel was configured. The same
reasoning governs ``ChannelUserSetting`` — see that module.

A grant is **access**, not enablement. Being granted does not switch the
channel on for the user; that is ``channel_user_setting.is_enabled`` resolved
against ``ServerChannel.default_enabled_for_users``. Both must hold, and both
are resolved together in ``ChannelPolicyService``.

Both foreign keys are to ``user`` and they carry different cascades on purpose:

- ``user_id`` ``ON DELETE CASCADE`` — the grant is meaningless without its
  subject, and a dangling grant would be a row nothing can ever revoke.
- ``granted_by`` ``ON DELETE SET NULL`` — the audit attribution is nice to
  have, but deleting the admin who issued a grant must not silently revoke
  access for everyone they granted. Same choice as
  ``ServerChannel.created_by`` and ``ServerAutoInstallBundle.added_by``.

Deleting the channel cascades every grant away with it.
"""
import uuid
from datetime import UTC, datetime

from pydantic import EmailStr
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class ServerChannelUserGrant(SQLModel, table=True):
    """One person permitted to use one restricted channel."""

    __tablename__ = "server_channel_user_grant"
    __table_args__ = (
        UniqueConstraint(
            "server_channel_id", "user_id", name="uq_server_channel_user_grant"
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Leading column of the unique constraint above, so no separate index.
    server_channel_id: uuid.UUID = Field(
        foreign_key="server_channel.id", ondelete="CASCADE"
    )
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE", index=True)
    # Who issued the grant. NULL once that admin's account is deleted — the
    # grant itself survives (see the module docstring).
    granted_by: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ChannelGrantPublic(SQLModel):
    """One grant, joined with enough of the user to render a row.

    The picker needs a name, not a UUID. This is the same minimal projection
    ``UserSearchResult`` uses — id, email, full name — and nothing more: a
    grant list is an admin-only view of *who may use a channel*, not a user
    directory.
    """

    user_id: uuid.UUID
    email: EmailStr
    full_name: str | None = None
    granted_by: uuid.UUID | None = None
    created_at: datetime


class ChannelGrantsUpdate(SQLModel):
    """Admin PUT body — the complete grant set, not a delta.

    Replace-the-set rather than add/remove verbs: the admin UI edits a picker
    whose state *is* the whole list, and a delta API against a multi-admin form
    silently loses a concurrent revocation.
    """

    user_ids: list[uuid.UUID] = Field(default_factory=list)
