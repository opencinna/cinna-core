"""UserChannelService — what a person may see and change about their channels.

The read half is a thin projection over :class:`ChannelPolicyService`: this
module holds **no** inheritance logic of its own and must never grow any. The
write half is the one place in the codebase allowed to create a
``channel_user_setting`` row.

WHY LAZY CREATION IS A RULE AND NOT AN OPTIMISATION
--------------------------------------------------
A settings row is an assertion that its owner made a choice. Creating one on a
read — or at channel-create time, for every user — writes that assertion on
behalf of people who never made it, and a stored ``is_enabled`` then stops
following the admin default forever (see ``ChannelUserSetting``'s docstring).
So: ``upsert_settings`` creates, everything else reads.

WHY THIS DOES NOT RETURN ``ServerChannelPublic``
------------------------------------------------
``ServerChannelPublic`` is the superuser projection. It carries
``webhook_token`` — the unguessable path segment that *is* the channel's
inbound authentication — plus ``config``, ``email_whitelist`` and
``has_outbound_credentials``. None of that may reach a regular user, and the
defence is a separate model (``UserChannelPublic``) rather than a field
exclusion: a model cannot leak a field it does not declare, whereas an
exclusion list silently stops covering whatever is added to the admin model
next.

OWNERSHIP IS CHECKED ON WRITE, AND AGAIN ON READ
------------------------------------------------
``channel_user_agent.agent_id`` and ``channel_user_setting.pinned_agent_id``
are foreign keys to ``agent``, and a foreign key enforces existence, never
ownership. The write path rejects an agent the caller does not own;
``ChannelPolicyService`` re-checks at resolution time, because ownership can
stop being true after the write and the check that still holds afterwards is
the one that protects routing.
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import Session as DBSession, select

from app.models import (
    CHANNEL_AGENT_SCOPES,
    Agent,
    ChannelUserAgent,
    ChannelUserSetting,
    ServerChannel,
    UserChannelPublic,
    UserChannelUpdate,
)
from app.services.server_channels.adapters.base import ChannelError
from app.services.server_channels.channel_policy_service import (
    ChannelPolicyService,
    ChannelPolicyView,
)

logger = logging.getLogger(__name__)


class ChannelNotAvailableError(ChannelError):
    """The caller may not use this channel — or it does not exist.

    One error for both, on purpose. The routes turn it into a 404 with no
    detail, so a restricted channel the caller has not been granted is
    indistinguishable from a channel id that was never real. The webhook's
    404-on-unknown-or-disabled rule exists for the same reason.
    """


class InvalidUserChannelSettingError(ChannelError):
    """The submitted settings are not valid for this caller."""


class UserChannelService:
    """Read and write one person's channel settings."""

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @staticmethod
    def list_for_user(
        session: DBSession, user_id: uuid.UUID
    ) -> list[UserChannelPublic]:
        """Every channel this person may use, with their resolved policy.

        "May use" is the *access* half of availability — the channel is enabled
        and the caller is granted it. Deliberately **not** the full
        availability conjunction: someone who switched a channel off for
        themselves must still see the row, or they can never switch it back on.
        Their toggle is reported as ``is_enabled`` on the projection instead,
        with the whole conjunction as ``is_available``.

        The access half is read off the resolution rather than from a separate
        ``can_access`` call, so one channel costs one resolution instead of
        repeating the grant lookup for every row that passes the filter.

        Resolution is per channel, and that is deliberate rather than
        overlooked: channels are a handful of admin-configured rows (Google
        Chat, email, App MCP), not a user-scaled table. Batching would mean a
        second implementation of the inherit rules living in this module, which
        is the one thing ``ChannelPolicyService``'s docstring forbids — a bad
        trade against a query count bounded by the number of channels a server
        has.
        """
        channels = session.exec(
            select(ServerChannel)
            .where(ServerChannel.enabled == True)  # noqa: E712
            .order_by(ServerChannel.name, ServerChannel.id)
        ).all()

        visible: list[UserChannelPublic] = []
        for channel in channels:
            view = ChannelPolicyService.describe(session, channel, user_id)
            # ``channel_enabled`` is already true by the query above; it is
            # re-read from the view rather than assumed, so the filter states
            # the whole access rule in one place.
            if not (view.channel_enabled and view.is_granted):
                continue
            visible.append(UserChannelService._project(channel, view))
        return visible

    @staticmethod
    def get_accessible_channel(
        session: DBSession, channel_id: uuid.UUID, user_id: uuid.UUID
    ) -> ServerChannel:
        """Resolve a channel the caller may address, or raise.

        Raises :class:`ChannelNotAvailableError` both for a channel that does
        not exist and for one the caller may not use — see that class.
        """
        channel = session.get(ServerChannel, channel_id)
        if channel is None or not ChannelPolicyService.can_access(
            session, channel, user_id
        ):
            raise ChannelNotAvailableError("Channel not found")
        return channel

    @staticmethod
    def to_public(
        session: DBSession, channel: ServerChannel, user_id: uuid.UUID
    ) -> UserChannelPublic:
        """Project one channel for its user. Every value already resolved."""
        return UserChannelService._project(
            channel, ChannelPolicyService.describe(session, channel, user_id)
        )

    @staticmethod
    def _project(
        channel: ServerChannel, view: ChannelPolicyView
    ) -> UserChannelPublic:
        """Shape an already-resolved view into the wire model.

        Split from :meth:`to_public` so the list path can resolve once and
        project, instead of resolving, filtering, then resolving again. It
        performs no query and holds no rules — every value it reads has already
        been decided by ``ChannelPolicyService``.
        """
        return UserChannelPublic(
            id=channel.id,
            channel_type=channel.channel_type,
            name=channel.name,
            is_available=view.policy.is_available,
            is_enabled=view.is_enabled_for_user,
            is_enabled_inherited=view.is_enabled_inherited,
            channel_default_enabled=view.channel_default_enabled,
            agent_scope=view.policy.agent_scope,
            agent_scope_inherited=view.agent_scope_inherited,
            channel_default_agent_scope=view.channel_default_agent_scope,
            agent_ids=list(view.owned_agent_ids),
            pinned_agent_id=view.policy.pinned_agent_id,
            allow_identity_routing=view.policy.allow_identity_routing,
            has_settings=view.has_setting_row,
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @staticmethod
    def upsert_settings(
        session: DBSession,
        channel: ServerChannel,
        user_id: uuid.UUID,
        data: UserChannelUpdate,
    ) -> UserChannelPublic:
        """Create or patch the caller's settings row. The only creation point.

        Field semantics come from ``model_dump(exclude_unset=True)``:

        - **omitted** — left exactly as it was;
        - **explicit ``null``** — cleared, which for ``is_enabled`` and
          ``agent_scope`` means *revert to the channel default*;
        - **a value** — stored as the caller's explicit choice.

        The middle case is why this cannot be a plain ``if value is not None``
        loop: ``None`` is both "not supplied" and "inherit", and collapsing them
        would make an inheritable field impossible to un-set through the API.
        """
        patch = data.model_dump(exclude_unset=True)

        if "allow_identity_routing" in patch and patch["allow_identity_routing"] is None:
            # No channel-level default exists for this field by design (master
            # plan §3.4), so there is nothing to inherit and an explicit null is
            # meaningless. Rejected rather than ignored — a silently dropped
            # security-relevant write is worse than a 422.
            raise InvalidUserChannelSettingError(
                "allow_identity_routing has no inherited state and cannot be null"
            )

        scope = patch.get("agent_scope")
        if "agent_scope" in patch and scope is not None:
            if scope not in CHANNEL_AGENT_SCOPES:
                raise InvalidUserChannelSettingError(
                    f"agent_scope must be one of {', '.join(CHANNEL_AGENT_SCOPES)}"
                )

        agent_ids: list[uuid.UUID] | None = None
        if "agent_ids" in patch:
            agent_ids = list(dict.fromkeys(patch["agent_ids"] or []))
            UserChannelService._assert_owned(session, agent_ids, user_id)

        if "pinned_agent_id" in patch and patch["pinned_agent_id"] is not None:
            UserChannelService._assert_owned(
                session, [patch["pinned_agent_id"]], user_id
            )

        setting = ChannelPolicyService.get_setting_row(session, channel.id, user_id)

        if setting is None and not patch:
            # An empty body against no row would otherwise create one whose
            # every inheritable field is still NULL. Inheritance would be
            # unaffected, but ``has_settings`` would flip to True — offering
            # "reset to defaults" to somebody who changed nothing, and leaving a
            # row that no later reader can tell apart from a deliberate choice.
            return UserChannelService.to_public(session, channel, user_id)

        if setting is None:
            setting = UserChannelService._create_setting(session, channel.id, user_id)

        for field in ("is_enabled", "agent_scope", "pinned_agent_id"):
            if field in patch:
                setattr(setting, field, patch[field])
        if "allow_identity_routing" in patch:
            setting.allow_identity_routing = bool(patch["allow_identity_routing"])

        if agent_ids is not None:
            UserChannelService._replace_agent_list(session, setting.id, agent_ids)

        setting.updated_at = datetime.now(UTC)
        session.add(setting)
        session.commit()
        session.refresh(channel)
        return UserChannelService.to_public(session, channel, user_id)

    @staticmethod
    def delete_settings(
        session: DBSession, channel: ServerChannel, user_id: uuid.UUID
    ) -> UserChannelPublic:
        """Drop the caller's row, returning them to pure inheritance.

        A no-op when there is no row — which is the same end state, so it is
        not an error. The agent list cascades away with the row; the caller is
        then indistinguishable from someone who has never opened the page,
        which is the entire point of the endpoint.
        """
        setting = ChannelPolicyService.get_setting_row(session, channel.id, user_id)
        if setting is not None:
            session.delete(setting)
            session.commit()
            logger.info(
                "Reset channel settings for user %s on channel %s",
                user_id,
                channel.id,
            )
            session.refresh(channel)
        return UserChannelService.to_public(session, channel, user_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _create_setting(
        session: DBSession, channel_id: uuid.UUID, user_id: uuid.UUID
    ) -> ChannelUserSetting:
        """Materialise the caller's settings row. The only creation point.

        Two simultaneous saves — a double-clicked button is enough — both see
        no row and both insert, and the loser would hit
        ``uq_channel_user_setting``. ``INSERT ... ON CONFLICT DO UPDATE`` makes
        that outcome impossible rather than recoverable: one statement, one
        row, no error to catch and no nested transaction to scope a rollback
        to. Each writer then applies its own patch to the row it got back, so a
        same-user double-PUT is last-write-wins over their own settings —
        which is what anybody pressing Save twice expects.

        WHY AN UPSERT AND NOT AN INSERT-THEN-RETRY
        ------------------------------------------
        There are three shapes for "concurrent first-edit of a per-user row":
        insert-and-retry-on-conflict, let the unique violation surface, and the
        native upsert. The retry is the most complex of the three — it needs a
        savepoint to keep the failed INSERT from poisoning the caller's
        transaction (this method runs partway through an edit), an
        ``IntegrityError`` catch, and a re-read to distinguish the conflict it
        expects from a real constraint failure it must not swallow. The upsert
        is atomic, defends against exactly the same race, and is a third of the
        code. It was chosen on that comparison alone; the retry shape it
        replaced was correct, just larger than the problem.

        ``on_conflict_do_update`` also matches this codebase's existing spelling
        for the same problem — see ``AgentGuestShareService.activate_guest_share``.

        WHAT THE ORM USED TO SUPPLY, AND WHERE IT COMES FROM NOW
        -------------------------------------------------------
        A Core-level ``insert()`` skips Pydantic entirely, so what the ORM path
        was contributing had to be enumerated rather than assumed. It was:
        ``id`` (``default_factory=uuid.uuid4``), ``created_at`` and
        ``updated_at`` (``default_factory`` on ``datetime.now(UTC)``),
        ``allow_identity_routing`` (``default=False``), and NULL for the three
        nullable-meaning-inherit columns. Every one of them is passed explicitly
        in ``values()`` below — including the NULLs, stated rather than omitted
        so that "a fresh row inherits everything" is visible in the statement.
        SQLModel does map those field defaults onto column-level Python defaults
        that a Core insert would still apply, so this is belt-and-braces; the
        point is that the row's contents are now written down in one place
        instead of depending on that mapping staying true.

        Nothing else was being run: ``ChannelUserSetting`` declares no field or
        model validators, no ``onupdate``, and no mapper events — and a
        ``table=True`` SQLModel does not validate on construction in any case,
        so the ORM path was not enforcing ``agent_scope``'s ``max_length`` here
        either (the column type does, in both paths).

        The conflict branch updates ``updated_at`` and nothing else. It must
        never assign the inheritable columns: those arrive as NULL in
        ``values()``, and writing them over an existing row would erase the
        settings of whoever got there first.
        """
        now = datetime.now(UTC)
        session.exec(  # type: ignore[call-overload]
            pg_insert(ChannelUserSetting)
            .values(
                id=uuid.uuid4(),
                server_channel_id=channel_id,
                user_id=user_id,
                is_enabled=None,
                agent_scope=None,
                pinned_agent_id=None,
                allow_identity_routing=False,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_channel_user_setting",
                set_={"updated_at": now},
            )
        )

        # Re-read rather than RETURNING: the caller patches attributes on this
        # object and the agent-list write needs its id, so it has to be a real
        # ORM instance. One shared spelling of "find this user's row" is worth
        # the extra SELECT, which is inside the same transaction as the insert
        # and therefore always finds it.
        setting = ChannelPolicyService.get_setting_row(session, channel_id, user_id)
        if setting is None:  # pragma: no cover — the upsert above guarantees it
            raise RuntimeError(
                "channel_user_setting missing immediately after its upsert"
            )
        return setting

    @staticmethod
    def _assert_owned(
        session: DBSession, agent_ids: list[uuid.UUID], user_id: uuid.UUID
    ) -> None:
        """Every id must name an agent this caller owns.

        One query for the whole list. The error names no agent: an id the
        caller does not own is answered identically whether it exists or not,
        so the endpoint cannot be used to probe for other people's agents.
        """
        if not agent_ids:
            return
        owned = set(
            session.exec(
                select(Agent.id).where(
                    Agent.id.in_(agent_ids),  # type: ignore[union-attr]
                    Agent.owner_id == user_id,
                )
            ).all()
        )
        if len(owned) != len(set(agent_ids)):
            raise InvalidUserChannelSettingError(
                "One or more agents were not found among your agents"
            )

    @staticmethod
    def _replace_agent_list(
        session: DBSession, setting_id: uuid.UUID, agent_ids: list[uuid.UUID]
    ) -> None:
        """Set the agent list to exactly ``agent_ids``.

        Survivors are left in place rather than deleted and re-inserted: the
        rows carry no user-visible data today, but churning primary keys on
        every save would make any future audit of this table unreadable.
        """
        current = session.exec(
            select(ChannelUserAgent).where(
                ChannelUserAgent.channel_user_setting_id == setting_id
            )
        ).all()
        current_by_agent = {row.agent_id: row for row in current}
        target = set(agent_ids)

        for agent_id, row in current_by_agent.items():
            if agent_id not in target:
                session.delete(row)
        for agent_id in agent_ids:
            if agent_id not in current_by_agent:
                session.add(
                    ChannelUserAgent(
                        channel_user_setting_id=setting_id, agent_id=agent_id
                    )
                )


__all__ = [
    "ChannelNotAvailableError",
    "InvalidUserChannelSettingError",
    "UserChannelService",
]
