"""ChannelPolicyService — the one place channel policy is resolved.

WHAT THIS ANSWERS
-----------------
"Given this channel and this person, what may happen?" Four separate facts —
an admin kill switch, an access allowlist, a per-user toggle, and an agent
scope — live on two tables with different meanings for absence, and every one
of them has a way of being read wrong. They are read here, together, once.

**Nothing else may re-derive these rules.** Not the router, not the inbound
pipeline, not the API projections, and above all not the frontend. Two
implementations of an inheritance rule do not stay equal; they drift into a UI
that says a channel is on while the router treats it as off, which is
undiagnosable from either side.

THE RULE THIS SERVICE EXISTS FOR
--------------------------------
**Absence of a ``channel_user_setting`` row means "the channel default
applies."** (Master plan §3.3.) Never require the row. Two populations will
never have one — senders the channel auto-registered, who have no UI session
in which to create one, and every user created after the channel was
configured — so a resolution that starts by fetching a row and giving up if it
is missing is wrong for exactly the users this feature was built for.

Nothing in this module writes. Rows are created lazily, on the user's first
edit, in ``PUT /users/me/channels/{channel_id}`` and nowhere else. A read path
that materializes a settings row is a bug, not an optimisation: it converts
"following the admin default" into "frozen at whatever the default was the
first time this user was looked at".

WHY THE RETURN VALUE IS A FROZEN DATACLASS
------------------------------------------
``resolve`` is called before routing is scheduled and its result is handed into
``ChannelRoutingService.decide``, which runs its work in threads with their own
short-lived sessions. An ORM row crossing that boundary turns the reader's next
attribute access into a lazy reload against a closed session — the exact hazard
``tests/architecture/channel_routing_purity_test.py`` fact #4 ("it returns plain
data") exists to prevent. So: ids, bools, strings, and a ``frozenset``. No ORM
instances, no ``Session``, nothing lazily loaded, and no mutable container that
a consumer could edit and hand on.

TOLERATING UNKNOWN VALUES
-------------------------
``visibility`` and ``agent_scope`` are plain ``VARCHAR`` (see
``ServerChannelBase``), so a value this code has never heard of can reach it —
from a newer deployment, a hand-edited row, or a value removed in a later
release. Both are degraded **conservatively**, and in both cases that falls out
of writing the permissive branch as the narrow one:

- visibility: only the literal ``"public"`` skips the grant check. Anything
  else behaves as restricted.
- agent scope: only ``"all"`` means every owned agent and only ``"list"`` means
  the saved list. Anything else — including ``"none"`` and any unknown string —
  resolves to ``"none"``.

Unknown-scope failing to ``"none"`` rather than ``"all"`` is deliberate. It is
the visible failure: the candidate provider records every owned agent as a skip
with a reason, so a trace says *why* nothing routed. Degrading to ``"all"``
would be the invisible one — an over-broad ballot that looks like it worked.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlmodel import Session as DBSession, select

from app.models import (
    CHANNEL_AGENT_SCOPE_ALL,
    CHANNEL_AGENT_SCOPE_LIST,
    CHANNEL_AGENT_SCOPE_NONE,
    CHANNEL_VISIBILITY_PUBLIC,
    Agent,
    ChannelUserAgent,
    ChannelUserSetting,
    ServerChannel,
    ServerChannelUserGrant,
)


@dataclass(frozen=True)
class ResolvedChannelPolicy:
    """What one person may do on one channel. Plain data, safe to hand around.

    Frozen and built only from scalars: this instance is passed into routing,
    which runs on its own sessions, so it must be complete at construction and
    unable to reach back into the database for anything.
    """

    #: The channel this was resolved against, or ``None`` for the one case
    #: where there is no channel to resolve against at all — see
    #: :meth:`for_no_channel`. ``None`` is not "a channel we could not
    #: identify"; it is "this decision does not belong to a channel".
    #:
    #: **Documentation, not plumbing.** Nothing reads it: routing takes its own
    #: ``channel_id`` parameter (it is trace metadata there, and travels on
    #: paths that carry no policy at all). It is here so an instance is
    #: self-describing in a log or a debugger, and it is the caller's job not
    #: to pair a policy with a different channel's id — nothing checks that,
    #: and nothing should start silently correcting for it either.
    channel_id: uuid.UUID | None

    #: The full conjunction — ``channel.enabled`` AND access AND the resolved
    #: user toggle. The only field routing needs to decide whether to proceed.
    is_available: bool

    #: ``"all"`` | ``"list"`` | ``"none"``. Always one of those three; unknown
    #: stored values have already been normalised (see the module docstring).
    agent_scope: str

    #: The agent list, and ``None`` when ``agent_scope != "list"``. ``None``
    #: and ``frozenset()`` mean different things and must not be conflated:
    #: ``None`` is "the list is not the mechanism here", the empty set is "the
    #: list is the mechanism and it is empty" — which routes nothing.
    allowed_agent_ids: frozenset[uuid.UUID] | None

    #: Set only when the user pinned an agent they **still own**. Ownership is
    #: re-checked at resolution time because the foreign key guarantees the
    #: agent exists, never that it belongs to this user.
    pinned_agent_id: uuid.UUID | None

    #: Phase 3 reads this. Never inherited from the channel — master plan §3.4.
    #: Resolved for the **sender**: their consent to a message of theirs being
    #: routed into another person's workspace. Not the receiver's gate — see
    #: ``ChannelUserSetting``'s docstring.
    allow_identity_routing: bool

    #: Whether routing Pass 2 (auto-install from the catalog) may run.
    allow_auto_install: bool

    @classmethod
    def for_no_channel(cls) -> ResolvedChannelPolicy:
        """The policy for a routing decision that runs **outside any channel**.

        There is exactly one *production* caller shape this exists for: a
        hand-typed ``POST /admin/routing/simulate`` whose request names no
        ``channel_id``. (Test helpers also use it, deliberately, to say "this
        scenario is not about channel policy" — see
        ``tests/utils/server_channel.route_installed``. That is the same
        statement, made by a caller that has no channel either.)
        That run has no ``ServerChannel`` row to resolve against — not a
        channel whose policy happens to be permissive, *no channel* — and the
        honest answer to "what may this person do here" is "the question does
        not apply", which in routing's vocabulary is an unrestricted ballot.

        **It is not a default and must never be used as one.** It is named for
        its one case, rather than ``permissive()`` or ``default()``, precisely
        so that reaching for it on a path that *does* have a channel reads
        wrong at the call site. Every such path — the webhook, and a replay of
        a stored trace that names a channel — resolves the real policy through
        :meth:`ChannelPolicyService.resolve`. A convenience default here would
        give the feature a second, silently permissive policy source, and the
        first symptom would be a simulate that disagrees with the webhook it
        exists to reproduce.

        ``allow_identity_routing`` is ``False`` while everything else is
        permissive, and that asymmetry is the point (master plan §3.4):
        routing into another person's workspace is opt-in, per person, by that
        person. The absence of a channel is not their consent.
        """
        return cls(
            channel_id=None,
            is_available=True,
            agent_scope=CHANNEL_AGENT_SCOPE_ALL,
            allowed_agent_ids=None,
            pinned_agent_id=None,
            allow_identity_routing=False,
            allow_auto_install=True,
        )


@dataclass(frozen=True)
class ChannelPolicyView:
    """``ResolvedChannelPolicy`` plus the provenance the settings UI needs.

    ``is_available`` is a conjunction, and a UI that can only show the answer
    cannot tell a user *which* term failed — "my channel is off" has four
    distinct causes with four different remedies (ask an admin to enable the
    channel, ask for a grant, flip your own switch, or nothing is wrong).
    Every term is therefore reported separately, alongside a flag per
    inheritable field saying whether the value came from the user or the
    channel default.

    This is the API-facing shape. Routing takes ``.policy`` and nothing else.
    """

    policy: ResolvedChannelPolicy

    #: Term 1 of the conjunction: the admin kill switch.
    channel_enabled: bool
    #: Term 2: public visibility, or a grant row for this user.
    is_granted: bool
    #: Term 3: the resolved user toggle, on its own.
    is_enabled_for_user: bool

    #: True when term 3 came from ``channel.default_enabled_for_users``.
    is_enabled_inherited: bool
    #: True when the agent scope came from ``channel.default_agent_scope``.
    agent_scope_inherited: bool

    #: Whether a ``channel_user_setting`` row exists at all. Distinct from the
    #: two ``*_inherited`` flags: a row can exist with both fields still NULL,
    #: because ``allow_identity_routing`` and the agent list also live on it.
    has_setting_row: bool

    #: The user's saved agent selection, filtered to agents they still own,
    #: returned whatever the resolved scope is. The UI shows the previous
    #: selection when switching back to "choose agents"; routing must use
    #: ``policy.allowed_agent_ids``, which is ``None`` unless the scope is
    #: actually ``"list"``.
    owned_agent_ids: tuple[uuid.UUID, ...]

    #: Echoed so the UI can label an inherited value with what it follows
    #: ("following the admin default (on)") without a second request.
    channel_default_enabled: bool
    channel_default_agent_scope: str


class ChannelPolicyService:
    """Resolves admin defaults against one user's optional overrides."""

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    @staticmethod
    def resolve(
        db: DBSession, channel: ServerChannel, user_id: uuid.UUID
    ) -> ResolvedChannelPolicy:
        """Policy for ``user_id`` on ``channel``, as plain data.

        Delegates to :meth:`describe` so the inherit rules have exactly one
        implementation. Callers on the routing path want this narrower value —
        it carries nothing a router could misread as a UI hint.
        """
        return ChannelPolicyService.describe(db, channel, user_id).policy

    @staticmethod
    def describe(
        db: DBSession, channel: ServerChannel, user_id: uuid.UUID
    ) -> ChannelPolicyView:
        """Full resolution, with the provenance of every inheritable field.

        Read the channel's attributes up front, off the instance the caller
        handed in, before anything else touches the session. The queries below
        do not commit, so this is safe today — reading first keeps it safe by
        construction rather than by coincidence, the same discipline the
        candidate providers follow.
        """
        channel_id = channel.id
        channel_enabled = bool(channel.enabled)
        visibility = channel.visibility or ""
        default_enabled = bool(channel.default_enabled_for_users)
        default_scope = ChannelPolicyService._normalise_scope(
            channel.default_agent_scope
        )
        allow_auto_install = bool(channel.allow_auto_install)

        # --- Term 2: access. A public channel is not asked about grants at
        # all, so the absence of grant rows on one is not "nobody". ---
        is_granted = (
            True
            if visibility == CHANNEL_VISIBILITY_PUBLIC
            else ChannelPolicyService._has_grant(db, channel_id, user_id)
        )

        setting = ChannelPolicyService.get_setting_row(db, channel_id, user_id)

        # --- Term 3: the user toggle. NULL — and a missing row entirely —
        # means inherit. These two cases must stay indistinguishable here. ---
        stored_enabled = setting.is_enabled if setting is not None else None
        is_enabled_inherited = stored_enabled is None
        is_enabled_for_user = (
            default_enabled if is_enabled_inherited else bool(stored_enabled)
        )

        stored_scope = setting.agent_scope if setting is not None else None
        agent_scope_inherited = stored_scope is None
        agent_scope = (
            default_scope
            if agent_scope_inherited
            else ChannelPolicyService._normalise_scope(stored_scope)
        )

        allow_identity_routing = (
            bool(setting.allow_identity_routing) if setting is not None else False
        )

        # The saved selection, filtered to agents the user still owns. The
        # ownership filter is not decoration: ``channel_user_agent.agent_id``
        # cascades on agent *deletion*, which is not the same as the agent
        # ceasing to be this user's.
        owned_agent_ids = (
            ChannelPolicyService._owned_agent_ids(db, setting.id, user_id)
            if setting is not None
            else ()
        )

        # ``None`` unless the list is the mechanism — see the field docstring.
        allowed_agent_ids = (
            frozenset(owned_agent_ids)
            if agent_scope == CHANNEL_AGENT_SCOPE_LIST
            else None
        )

        pinned_agent_id = (
            ChannelPolicyService._owned_pin(db, setting.pinned_agent_id, user_id)
            if setting is not None
            else None
        )

        policy = ResolvedChannelPolicy(
            channel_id=channel_id,
            # Order matters only for readability — ``and`` is commutative here
            # because every term is already computed. The order is the plan's:
            # kill switch, access, user toggle.
            is_available=channel_enabled and is_granted and is_enabled_for_user,
            agent_scope=agent_scope,
            allowed_agent_ids=allowed_agent_ids,
            pinned_agent_id=pinned_agent_id,
            allow_identity_routing=allow_identity_routing,
            allow_auto_install=allow_auto_install,
        )

        return ChannelPolicyView(
            policy=policy,
            channel_enabled=channel_enabled,
            is_granted=is_granted,
            is_enabled_for_user=is_enabled_for_user,
            is_enabled_inherited=is_enabled_inherited,
            agent_scope_inherited=agent_scope_inherited,
            has_setting_row=setting is not None,
            owned_agent_ids=owned_agent_ids,
            channel_default_enabled=default_enabled,
            channel_default_agent_scope=default_scope,
        )

    # ------------------------------------------------------------------
    # Access, without the rest of the resolution
    # ------------------------------------------------------------------

    @staticmethod
    def can_access(
        db: DBSession, channel: ServerChannel, user_id: uuid.UUID
    ) -> bool:
        """Terms 1 and 2 only: the channel is on and this person may use it.

        Deliberately excludes the user's own toggle. This is the predicate the
        user-facing routes list and address channels by — somebody who switched
        a channel off for themselves must still see it, or they can never
        switch it back on, and ``PUT`` must still reach it.

        A channel this returns ``False`` for is answered as 404 by those
        routes: "you may not use this channel" and "no such channel" must be
        indistinguishable, for the same reason the webhook answers 404 to both
        an unknown token and a disabled channel.
        """
        if not channel.enabled:
            return False
        if (channel.visibility or "") == CHANNEL_VISIBILITY_PUBLIC:
            return True
        return ChannelPolicyService._has_grant(db, channel.id, user_id)

    # ------------------------------------------------------------------
    # Internals + row access
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_scope(value: str | None) -> str:
        """Map a stored scope onto the three values consumers may see.

        Unknown values — including ``None`` — become ``"none"``. See the module
        docstring for why the conservative direction is the visible one.
        """
        if value == CHANNEL_AGENT_SCOPE_ALL:
            return CHANNEL_AGENT_SCOPE_ALL
        if value == CHANNEL_AGENT_SCOPE_LIST:
            return CHANNEL_AGENT_SCOPE_LIST
        return CHANNEL_AGENT_SCOPE_NONE

    @staticmethod
    def _has_grant(
        db: DBSession, channel_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Existence check only — the row itself is never needed here."""
        return (
            db.exec(
                select(ServerChannelUserGrant.id).where(
                    ServerChannelUserGrant.server_channel_id == channel_id,
                    ServerChannelUserGrant.user_id == user_id,
                )
            ).first()
            is not None
        )

    @staticmethod
    def get_setting_row(
        db: DBSession, channel_id: uuid.UUID, user_id: uuid.UUID
    ) -> ChannelUserSetting | None:
        """The user's row, or ``None``. ``None`` is a valid, common answer.

        Public because ``UserChannelService``'s write path needs the row itself
        rather than a resolution of it, and one lookup shared with the read path
        is better than two spellings of "find this user's row". It returns an
        ORM instance, so it is for request-scoped callers only — never for
        anything that crosses into routing's worker threads. Use
        :meth:`resolve` there.

        This method must never create the row it failed to find. The lazy
        creation point is ``UserChannelService.upsert_settings``, reached only
        from ``PUT /users/me/channels/{channel_id}``; see the module docstring.
        """
        return db.exec(
            select(ChannelUserSetting).where(
                ChannelUserSetting.server_channel_id == channel_id,
                ChannelUserSetting.user_id == user_id,
            )
        ).first()

    @staticmethod
    def _owned_agent_ids(
        db: DBSession, setting_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[uuid.UUID, ...]:
        """The saved agent list, joined against ownership.

        Ordered by agent name so the UI and any trace built from this render in
        one stable order, matching ``ChannelCandidateProvider``'s
        ``.order_by(Agent.name, Agent.id)``.
        """
        rows = db.exec(
            select(ChannelUserAgent.agent_id)
            .join(Agent, Agent.id == ChannelUserAgent.agent_id)  # type: ignore[arg-type]
            .where(
                ChannelUserAgent.channel_user_setting_id == setting_id,
                Agent.owner_id == user_id,
            )
            .order_by(Agent.name, Agent.id)
        ).all()
        return tuple(rows)

    @staticmethod
    def _owned_pin(
        db: DBSession, pinned_agent_id: uuid.UUID | None, user_id: uuid.UUID
    ) -> uuid.UUID | None:
        """The pin, but only if ``user_id`` still owns the agent.

        The foreign key enforces existence and nothing more: it survives the
        agent changing hands, and it would happily let a stale pin route a
        channel message onto somebody else's agent. Checked on write too — this
        is the check that still holds after the write, which is the one that
        matters.
        """
        if pinned_agent_id is None:
            return None
        owned = db.exec(
            select(Agent.id).where(
                Agent.id == pinned_agent_id, Agent.owner_id == user_id
            )
        ).first()
        return pinned_agent_id if owned is not None else None


__all__ = [
    "ChannelPolicyService",
    "ChannelPolicyView",
    "ResolvedChannelPolicy",
]
