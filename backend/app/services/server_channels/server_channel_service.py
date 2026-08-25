"""Admin-side channel management: CRUD, secrets, tokens, auto-install, grants.

Everything here is superuser-facing. The inbound pipeline only borrows one
method from this service — ``get_by_webhook_token`` — and that method is the
reason the channel lookup is enabled-only: a disabled channel must be
indistinguishable from a nonexistent one at the webhook.

Secret discipline: ``ServerChannel.encrypted_secrets`` is written here and
read only by the adapter. It is never projected into a DTO, never logged, and
an update that omits the field leaves the stored value untouched (so an admin
editing the whitelist doesn't have to re-paste a service-account key).

Policy discipline: this service owns the admin-set *defaults* — the four
policy columns and the ``server_channel_user_grant`` allowlist. It does not
resolve them against a user. That is ``ChannelPolicyService``, and keeping the
two apart is what stops a second copy of the inheritance rules appearing on
the admin side.
"""
from __future__ import annotations

import logging
import secrets as secrets_module
import uuid
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import encrypt_field
from app.models import (
    CHANNEL_AGENT_SCOPES,
    CHANNEL_VISIBILITIES,
    AgentBundle,
    AgentBundleRevision,
    AutoInstallBundlePublic,
    ChannelGrantPublic,
    ChannelRecentSender,
    ChannelSetupInstructions,
    ChannelThreadBinding,
    ServerAutoInstallBundle,
    ServerChannel,
    ServerChannelCreate,
    ServerChannelPublic,
    ServerChannelUpdate,
    ServerChannelUserGrant,
    User,
)
from app.services.server_channels.adapters.base import (
    ChannelError,
    UnknownChannelTypeError,
)
from app.services.server_channels.adapters.registry import (
    RegisteredTransport,
    get_adapter,
    get_transport,
)
from app.services.server_channels.channel_debug_buffer import ChannelDebugBuffer

logger = logging.getLogger(__name__)

# Sort floor for entries with no timestamp — see ``list_recent_senders``.
_EPOCH = datetime.min.replace(tzinfo=UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalise a timestamp to timezone-aware UTC.

    Naive values reach us from the bare ``DateTime`` columns this codebase uses
    (Postgres hands them back without a tzinfo); everything the process stamps
    itself is already aware. Mixing the two in a comparison raises, so they are
    reconciled at the one place both sources meet.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_WEBHOOK_TOKEN_BYTES = 32

# How many bindings the recent-senders picker scans. Ordered newest-first, then
# deduped by address, so the cap costs only the tail of a very busy channel —
# and the picker is a "who did I just talk to" list, not an audit of everyone
# who ever wrote in.
_RECENT_SENDERS_SCAN_LIMIT = 200


class ChannelNotFoundError(ChannelError):
    """No channel with the given id."""


class DuplicateChannelNameError(ChannelError):
    """Channel names are unique — the admin list is keyed on them visually."""


class InvalidChannelPolicyError(ChannelError):
    """``visibility`` / ``default_agent_scope`` was not a value we know.

    The columns are plain ``VARCHAR`` so that adding a value never needs a
    migration, and *readers* tolerate anything (see ``ChannelPolicyService``).
    The write boundary is stricter on purpose: a typo from the admin API would
    otherwise be stored, silently degrade to the conservative branch, and read
    back as a legitimate setting."""


class UnsupportedChannelOperationError(ChannelError):
    """The requested operation does not exist for this channel's transport.

    Not every channel is a webhook since the transport split, so a few admin
    actions stopped being universal — regenerating a webhook token on a polled
    channel is the first. Refused explicitly rather than quietly ignored: an
    admin who clicks "regenerate" and is told nothing will assume the old URL
    is dead and go looking for the new one that was never minted.
    """


class ServerChannelService:
    """CRUD + configuration for admin-configured channels."""

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @staticmethod
    def list_channels(session: Session) -> list[ServerChannel]:
        return list(
            session.exec(select(ServerChannel).order_by(ServerChannel.created_at)).all()
        )

    @staticmethod
    def get_channel(session: Session, channel_id: uuid.UUID) -> ServerChannel:
        channel = session.get(ServerChannel, channel_id)
        if channel is None:
            raise ChannelNotFoundError(f"Channel {channel_id} not found")
        return channel

    @staticmethod
    def get_by_webhook_token(
        session: Session, token: str | None
    ) -> ServerChannel | None:
        """Resolve an ENABLED channel by webhook token.

        Enabled-only on purpose: the webhook returns the same 404 for an
        unknown token and a disabled channel, so toggling a channel off does
        not become an oracle for "this token exists".

        A tokenless channel is unreachable through here, doubly. SQL ``=``
        against NULL is never true, so a row with a NULL ``webhook_token``
        cannot match any string a request could supply; and the empty-string
        guard below refuses a falsy token before a query is issued, so no
        caller can turn "no token" into a lookup either.
        """
        if not token:
            return None
        return session.exec(
            select(ServerChannel).where(
                ServerChannel.webhook_token == token,
                ServerChannel.enabled == True,  # noqa: E712
            )
        ).first()

    # ------------------------------------------------------------------
    # Projections
    # ------------------------------------------------------------------

    @staticmethod
    def webhook_url(channel: ServerChannel) -> str | None:
        """Public webhook URL for this channel, or ``None`` if it has no webhook.

        Built from ``settings.webhook_base_url`` — the externally reachable
        backend origin (``WEBHOOK_BASE_URL``, falling back to
        ``FRONTEND_HOST``) — never from the request, so the value an admin
        pastes into Google is right even when they reached the admin page over
        an internal address.

        ``None`` when the channel has no token. A URL assembled without one
        would read as live and resolve to nothing — the webhook route matches
        on the token segment, so the only thing such a URL can ever do is 404.
        That is precisely the hazard ``needs_webhook_token`` exists to name, and
        the token's presence is the whole test: it is minted iff the transport
        declares it needs one.
        """
        if not channel.webhook_token:
            return None
        base = settings.webhook_base_url
        return f"{base}{settings.API_V1_STR}/channels/{channel.webhook_token}/inbound"

    @staticmethod
    def to_public(channel: ServerChannel) -> ServerChannelPublic:
        """Admin projection. Carries no secret material by construction."""
        return ServerChannelPublic(
            id=channel.id,
            channel_type=channel.channel_type,
            name=channel.name,
            enabled=channel.enabled,
            auto_register_users=channel.auto_register_users,
            visibility=channel.visibility,
            default_enabled_for_users=channel.default_enabled_for_users,
            default_agent_scope=channel.default_agent_scope,
            allow_auto_install=channel.allow_auto_install,
            config=channel.config or {},
            email_whitelist=channel.email_whitelist,
            webhook_token=channel.webhook_token,
            webhook_url=ServerChannelService.webhook_url(channel),
            has_outbound_credentials=(
                ServerChannelService.has_outbound_credentials(channel)
            ),
            created_by=channel.created_by,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )

    @staticmethod
    def get_setup_instructions(channel: ServerChannel) -> ChannelSetupInstructions:
        """Adapter-shaped setup guidance.

        ``webhook_url`` is ``None`` for a transport with no webhook, and the
        adapter is handed that ``None`` rather than a fabricated URL: the panel
        is what an admin copies from, so a live-looking address here is the
        most expensive place to invent one.
        """
        adapter = get_adapter(channel.channel_type)
        url = ServerChannelService.webhook_url(channel)
        details, steps = adapter.get_setup_instructions(channel, url)
        return ChannelSetupInstructions(
            channel_type=channel.channel_type,
            webhook_url=url,
            details=details,
            steps=steps,
        )

    # ------------------------------------------------------------------
    # Debug / test targeting
    # ------------------------------------------------------------------

    @staticmethod
    def list_recent_senders(
        session: Session, channel: ServerChannel
    ) -> list[ChannelRecentSender]:
        """People this channel has seen, and the thread to reach each on.

        Two sources, merged deliberately:

        - **Thread bindings** — durable, and the authoritative "this person has
          a conversation with an agent here" set.
        - **The debug buffer** — live, and covers the gap the bindings cannot:
          a sender who was just denied by the whitelist, or whose routing is
          still in flight, has no binding yet but is exactly who an admin wants
          to send a test to while debugging.

        Bindings win on conflict: a persisted thread outlives the process.

        ``last_seen`` is normalised to timezone-aware UTC before it is stored on
        the DTO. The two sources disagree: ``channel_thread_binding.updated_at``
        is a bare ``DateTime`` column and comes back from Postgres naive, while
        the debug buffer stamps ``datetime.now(UTC)``. Sorting a mix of the two
        raises ``TypeError: can't compare offset-naive and offset-aware
        datetimes`` — and one bound user plus one buffer-only sender is the
        exact case this merge exists for.
        """
        senders: dict[str, ChannelRecentSender] = {}

        rows = session.exec(
            select(ChannelThreadBinding, User)
            .join(User, User.id == ChannelThreadBinding.user_id)  # type: ignore[arg-type]
            .where(ChannelThreadBinding.server_channel_id == channel.id)
            .order_by(ChannelThreadBinding.updated_at.desc())  # type: ignore[union-attr]
            .limit(_RECENT_SENDERS_SCAN_LIMIT)
        ).all()
        for binding, user in rows:
            email = (user.email or "").strip().lower()
            if not email or email in senders:
                continue
            senders[email] = ChannelRecentSender(
                email=email,
                display_name=user.full_name,
                thread_key=binding.thread_key,
                last_seen=_as_utc(binding.updated_at),
                bound=True,
            )

        for event in ChannelDebugBuffer.list_events(channel.id):
            email = (event.sender_email or "").strip().lower()
            if not email or not event.thread_key or email in senders:
                continue
            senders[email] = ChannelRecentSender(
                email=email,
                display_name=event.sender_display_name,
                thread_key=event.thread_key,
                last_seen=_as_utc(event.at),
                bound=False,
            )

        # Missing timestamps sort last. A ``(has_value, value)`` key would still
        # compare ``None`` against ``None`` when two entries both lack one.
        return sorted(
            senders.values(),
            key=lambda s: s.last_seen or _EPOCH,
            reverse=True,
        )

    @staticmethod
    def resolve_test_thread_key(
        session: Session, channel: ServerChannel, email: str
    ) -> str:
        """Turn an email into a thread this channel can actually post to.

        Resolution is entirely local. Google Chat's ``users/{email}`` alias is
        documented as user-authentication only and this adapter authenticates
        as an app, so an address the platform has never observed on this
        channel has no reachable destination — and saying so plainly beats a
        provider 404 an admin has to decode.
        """
        wanted = (email or "").strip().lower()
        for sender in ServerChannelService.list_recent_senders(session, channel):
            if sender.email == wanted:
                return sender.thread_key
        raise ChannelError(
            f"No conversation with {wanted} is known on this channel yet. "
            "Ask them to message the app once — the thread then appears here "
            "and in the debug panel. (An email cannot be resolved to a "
            "destination without a prior message: the provider's email alias "
            "requires user authentication, and this app authenticates as an "
            "app.)"
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @staticmethod
    def create_channel(
        session: Session, data: ServerChannelCreate, user: User
    ) -> ServerChannel:
        """Create a channel. Validates type + config before persisting."""
        transport = get_transport(data.channel_type)  # raises UnknownChannelTypeError
        config = data.config or {}
        transport.adapter.validate_config(config)
        # Shape first, then references. Splitting the two is what lets the
        # shape check run without a session while the cross-row check runs on
        # *this* one — the transaction the channel is about to be written in,
        # so an adapter can never validate against a different snapshot than
        # the caller persists into.
        transport.adapter.validate_config_references(session, config)
        ServerChannelService._validate_policy(
            visibility=data.visibility, agent_scope=data.default_agent_scope
        )

        name = (data.name or "").strip()
        if ServerChannelService._name_taken(session, name):
            raise DuplicateChannelNameError(
                f"A channel named {name!r} already exists"
            )

        channel = ServerChannel(
            channel_type=data.channel_type,
            name=name,
            enabled=data.enabled,
            auto_register_users=data.auto_register_users,
            visibility=data.visibility,
            default_enabled_for_users=data.default_enabled_for_users,
            default_agent_scope=data.default_agent_scope,
            allow_auto_install=data.allow_auto_install,
            config=config,
            email_whitelist=ServerChannelService._clean_whitelist(data.email_whitelist),
            encrypted_secrets=(
                encrypt_field(data.secrets) if (data.secrets or "").strip() else None
            ),
            # No token here. ``_ensure_webhook_token`` below is the *only* rule:
            # a transport declaring ``needs_webhook_token=True`` gets one minted,
            # one declaring False keeps ``None``. Minting unconditionally here
            # would hand a polled channel a token, and a token is what
            # ``webhook_url`` reads to decide a channel has a reachable inbound
            # URL — so the row would advertise a door it does not have.
            created_by=user.id,
        )
        ServerChannelService._ensure_webhook_token(channel, transport)
        session.add(channel)
        session.commit()
        session.refresh(channel)
        logger.info(
            "Created server channel %s (type=%s) by user %s",
            channel.id,
            channel.channel_type,
            user.id,
        )
        return channel

    @staticmethod
    def update_channel(
        session: Session, channel: ServerChannel, data: ServerChannelUpdate
    ) -> ServerChannel:
        """Patch a channel.

        The three non-column fields on the update DTO (``secrets``,
        ``regenerate_webhook_token``, and a ``config`` that needs adapter
        validation) are handled explicitly and popped, so nothing that isn't a
        real column ever reaches ``sqlmodel_update``.
        """
        patch = data.model_dump(exclude_unset=True)

        # Non-column / side-effecting fields, removed before the generic apply.
        raw_secrets = patch.pop("secrets", None)
        regenerate = patch.pop("regenerate_webhook_token", False)

        # Resolved once, up front, rather than per-branch: the token rules at
        # the bottom of this method need the transport whether or not the type
        # is being changed.
        #
        # The two cases are deliberately asymmetric. A ``channel_type`` *in the
        # patch* is admin input under validation, so one with no adapter must
        # still raise ``UnknownChannelTypeError`` here — accepting it would
        # persist a row nothing can drive. A *stored* type with no adapter is a
        # row that already exists and whose adapter left the registry; refusing
        # the patch would strand the admin, unable even to send
        # ``{"enabled": false}`` to switch off the channel that broke. So the
        # stored type resolves leniently to ``None``, and the two rules that
        # genuinely need a transport are guarded on it below.
        if "channel_type" in patch:
            channel_type: str = patch["channel_type"]
            transport: RegisteredTransport | None = get_transport(channel_type)
        else:
            channel_type = channel.channel_type
            try:
                transport = get_transport(channel_type)
            except ChannelError:
                transport = None

        # Refused here rather than beside the mint below, with every other
        # validation, so the raise happens before anything on ``channel`` has
        # been mutated. A refusal that leaves a half-applied patch on a live
        # session instance is a landmine for whatever commits next.
        #
        # Checked against the *patched* type: switching a channel onto a
        # transport with no webhook and asking for a fresh token in the same
        # request is exactly the confusion this refusal exists to name.
        if regenerate and transport is not None and not transport.needs_webhook_token:
            raise UnsupportedChannelOperationError(
                f"Channel type {channel_type!r} has no webhook, so there is "
                "no token to regenerate."
            )

        if "config" in patch:
            config = patch.pop("config") or {}
            if transport is None:
                # The leniency above is for patches that need no adapter. A
                # config patch needs one — it is the validator — and accepting
                # config nothing checked would persist it unvalidated.
                raise UnknownChannelTypeError(
                    f"Channel type {channel_type!r} has no registered adapter, "
                    "so its config cannot be validated."
                )
            transport.adapter.validate_config(config)
            transport.adapter.validate_config_references(session, config)
            channel.config = config
            flag_modified(channel, "config")

        if "name" in patch:
            patch["name"] = (patch["name"] or "").strip()
            if ServerChannelService._name_taken(
                session, patch["name"], exclude_id=channel.id
            ):
                raise DuplicateChannelNameError(
                    f"A channel named {patch['name']!r} already exists"
                )

        if "email_whitelist" in patch:
            patch["email_whitelist"] = ServerChannelService._clean_whitelist(
                patch["email_whitelist"]
            )

        # The four policy fields are ordinary columns and ride ``sqlmodel_update``
        # below, so nothing between here and the commit would catch a bad value.
        #
        # An explicit ``null`` has to be rejected by KEY PRESENCE, not by value.
        # All four are NOT NULL columns declared ``X | None`` on the DTO, and
        # ``exclude_unset`` keeps an explicitly-null field in the patch — so
        # ``{"visibility": null}``, which is exactly what a client sends meaning
        # "reset this", would be assigned straight onto the column (SQLModel
        # table models do not validate on assignment) and surface as an
        # unhandled IntegrityError at commit instead of a 422.
        for field in (
            "visibility",
            "default_enabled_for_users",
            "default_agent_scope",
            "allow_auto_install",
        ):
            if field in patch and patch[field] is None:
                raise InvalidChannelPolicyError(f"{field} cannot be null")

        if "visibility" in patch or "default_agent_scope" in patch:
            ServerChannelService._validate_policy(
                visibility=patch.get("visibility", channel.visibility),
                agent_scope=patch.get(
                    "default_agent_scope", channel.default_agent_scope
                ),
            )

        if patch:
            channel.sqlmodel_update(patch)

        # Only overwrite the stored secret when a non-empty value was supplied.
        # An admin editing any other field leaves the field blank and keeps
        # the existing credential.
        if raw_secrets is not None and raw_secrets.strip():
            channel.encrypted_secrets = encrypt_field(raw_secrets)
            ServerChannelService._invalidate_adapter_caches(channel)

        if regenerate:
            channel.webhook_token = ServerChannelService.generate_webhook_token()
            logger.info("Regenerated webhook token for channel %s", channel.id)

        # After ``sqlmodel_update``, because a ``channel_type`` patch can move a
        # channel onto a transport with different requirements from the one it
        # was created under. Skipped entirely when the stored type has no
        # adapter: there is no declaration to apply, and the row keeps whatever
        # token it already has.
        if transport is not None:
            ServerChannelService._ensure_webhook_token(channel, transport)

        channel.updated_at = datetime.now(UTC)
        session.add(channel)
        session.commit()
        session.refresh(channel)
        return channel

    @staticmethod
    def delete_channel(session: Session, channel: ServerChannel) -> None:
        """Delete a channel. Bindings cascade at the DB level."""
        ServerChannelService._invalidate_adapter_caches(channel)
        # The debug buffer is keyed by channel id and has no cascade of its
        # own, so without this its entries — including captured message text —
        # outlive the channel until the process restarts. The frontend already
        # drops the matching queries on delete; this makes the two agree.
        ChannelDebugBuffer.clear(channel.id)
        session.delete(channel)
        session.commit()
        logger.info("Deleted server channel %s", channel.id)

    @staticmethod
    def generate_webhook_token() -> str:
        return secrets_module.token_urlsafe(_WEBHOOK_TOKEN_BYTES)

    # ------------------------------------------------------------------
    # Auto-install list
    # ------------------------------------------------------------------

    @staticmethod
    def list_auto_install_bundles(session: Session) -> list[AutoInstallBundlePublic]:
        """Joined projection of the server-wide auto-install list.

        ``router_trigger_prompt`` and ``has_trigger_prompt`` are both resolved
        from each bundle's latest revision: a bundle without a
        ``router_trigger_prompt`` can never win Pass 2, and the admin UI flags
        it rather than leaving the operator to wonder why nothing routes.

        The two are derived from **one** local, not computed twice. They cannot
        disagree that way, and the Auto Routing Tuning card reads the text while
        the auto-install card reads the flag — two readers of the same fact is
        exactly where a second computation drifts.
        """
        rows = session.exec(
            select(ServerAutoInstallBundle, AgentBundle)
            .join(AgentBundle, AgentBundle.id == ServerAutoInstallBundle.bundle_uuid)
            .order_by(ServerAutoInstallBundle.created_at)
        ).all()

        # One query for every referenced revision rather than one per row.
        revision_ids = [b.latest_revision_id for _, b in rows if b.latest_revision_id]
        prompts: dict[uuid.UUID, str | None] = {}
        if revision_ids:
            revisions = session.exec(
                select(
                    AgentBundleRevision.id, AgentBundleRevision.router_trigger_prompt
                ).where(AgentBundleRevision.id.in_(revision_ids))
            ).all()
            prompts = {rev_id: prompt for rev_id, prompt in revisions}

        def _project(entry, bundle) -> AutoInstallBundlePublic:
            prompt = (
                (prompts.get(bundle.latest_revision_id) or "").strip() or None
                if bundle.latest_revision_id
                else None
            )
            return AutoInstallBundlePublic(
                bundle_uuid=bundle.id,
                bundle_id=bundle.bundle_id,
                display_name=bundle.display_name,
                visibility=bundle.visibility,
                router_trigger_prompt=prompt,
                has_trigger_prompt=prompt is not None,
                added_by=entry.added_by,
                created_at=entry.created_at,
            )

        return [_project(entry, bundle) for entry, bundle in rows]

    @staticmethod
    def add_auto_install_bundle(
        session: Session, bundle_uuid: uuid.UUID, user: User
    ) -> ServerAutoInstallBundle:
        """Add a bundle to the auto-install list. Idempotent."""
        bundle = session.get(AgentBundle, bundle_uuid)
        if bundle is None:
            raise ChannelNotFoundError(f"Bundle {bundle_uuid} not found")
        if bundle.latest_revision_id is None:
            raise ChannelError(
                "Bundle has no published revision and cannot be auto-installed"
            )

        existing = session.exec(
            select(ServerAutoInstallBundle).where(
                ServerAutoInstallBundle.bundle_uuid == bundle_uuid
            )
        ).first()
        if existing is not None:
            return existing

        entry = ServerAutoInstallBundle(bundle_uuid=bundle_uuid, added_by=user.id)
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry

    @staticmethod
    def remove_auto_install_bundle(session: Session, bundle_uuid: uuid.UUID) -> None:
        entry = session.exec(
            select(ServerAutoInstallBundle).where(
                ServerAutoInstallBundle.bundle_uuid == bundle_uuid
            )
        ).first()
        if entry is None:
            return
        session.delete(entry)
        session.commit()

    # ------------------------------------------------------------------
    # Grants — the per-user allowlist for a restricted channel
    # ------------------------------------------------------------------

    @staticmethod
    def list_grants(
        session: Session, channel: ServerChannel
    ) -> list[ChannelGrantPublic]:
        """Everyone granted this channel, joined with enough to render a row.

        Ordered by the name the admin actually sees —
        ``coalesce(nullif(full_name, ''), email)``, then ``User.id`` — the
        convention Phase 1 pinned for identity candidates and which every
        rendered person-list in this refactor follows. The ``nullif`` is not
        decoration: ``full_name`` is nullable *and* unconstrained against the
        empty string, so both ``NULL`` and ``''`` reach the database and both
        render as the email. Plain ``coalesce`` would catch only the ``NULL``
        half and leave ``''`` sorting ahead of every real name.

        Grants are returned even when the channel is currently ``public``. The
        rows are not consulted then, but they are the admin's saved allowlist
        and silently hiding them would lose the list on a visibility round-trip.
        """
        rows = session.exec(
            select(ServerChannelUserGrant, User)
            .join(User, User.id == ServerChannelUserGrant.user_id)  # type: ignore[arg-type]
            .where(ServerChannelUserGrant.server_channel_id == channel.id)
            .order_by(
                func.coalesce(func.nullif(User.full_name, ""), User.email),
                User.id,
            )
        ).all()
        return [
            ChannelGrantPublic(
                user_id=user.id,
                email=user.email,
                full_name=user.full_name,
                granted_by=grant.granted_by,
                created_at=grant.created_at,
            )
            for grant, user in rows
        ]

    @staticmethod
    def replace_grants(
        session: Session,
        channel: ServerChannel,
        user_ids: list[uuid.UUID],
        granted_by: User,
    ) -> list[ChannelGrantPublic]:
        """Set the grant list to exactly ``user_ids``.

        Replace, not merge: the admin UI edits a picker whose state *is* the
        whole list, and a delta API against a multi-admin form silently loses a
        concurrent revocation.

        Rows that survive the replace are **left alone** rather than deleted and
        re-inserted, so ``granted_by`` and ``created_at`` keep naming who first
        granted access and when — re-saving an unchanged form must not rewrite
        the audit trail to say the last person to touch the page granted
        everyone.

        Ids that do not resolve to a user are dropped rather than rejected: the
        picker sources them from ``GET /users/search``, so an unresolvable id
        means the account was deleted between picking and saving, and failing
        the whole save over it would strand the admin on a form they cannot
        submit.
        """
        wanted = list(dict.fromkeys(user_ids))  # de-dupe, keep order
        existing_users = (
            set(
                session.exec(select(User.id).where(User.id.in_(wanted))).all()  # type: ignore[union-attr]
            )
            if wanted
            else set()
        )
        target = {uid for uid in wanted if uid in existing_users}

        current = session.exec(
            select(ServerChannelUserGrant).where(
                ServerChannelUserGrant.server_channel_id == channel.id
            )
        ).all()
        current_by_user = {grant.user_id: grant for grant in current}

        for user_id, grant in current_by_user.items():
            if user_id not in target:
                session.delete(grant)

        for user_id in target:
            if user_id in current_by_user:
                continue
            session.add(
                ServerChannelUserGrant(
                    server_channel_id=channel.id,
                    user_id=user_id,
                    granted_by=granted_by.id,
                )
            )

        session.commit()
        logger.info(
            "Replaced grants on channel %s: %d user(s), by %s",
            channel.id,
            len(target),
            granted_by.id,
        )
        return ServerChannelService.list_grants(session, channel)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_policy(*, visibility: str | None, agent_scope: str | None) -> None:
        """Reject policy strings this build does not know about.

        Only non-``None`` values are checked. This is safe **because the caller
        has already rejected an explicitly-null policy field by key presence** —
        by the time a ``None`` reaches here it can only mean "not supplied", and
        the caller has resolved it against the stored value. Do not make this
        the sole guard: it cannot tell an omitted field from a null one.
        """
        if visibility is not None and visibility not in CHANNEL_VISIBILITIES:
            raise InvalidChannelPolicyError(
                f"visibility must be one of {', '.join(CHANNEL_VISIBILITIES)}"
            )
        if agent_scope is not None and agent_scope not in CHANNEL_AGENT_SCOPES:
            raise InvalidChannelPolicyError(
                "default_agent_scope must be one of "
                f"{', '.join(CHANNEL_AGENT_SCOPES)}"
            )

    @staticmethod
    def _name_taken(
        session: Session, name: str, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(ServerChannel.id).where(ServerChannel.name == name)
        if exclude_id is not None:
            stmt = stmt.where(ServerChannel.id != exclude_id)
        return session.exec(stmt).first() is not None

    @staticmethod
    def _clean_whitelist(value: str | None) -> str | None:
        """Normalise the pattern list; empty stays NULL (= deny all)."""
        if value is None:
            return None
        cleaned = ", ".join(p.strip() for p in value.split(",") if p.strip())
        return cleaned or None

    @staticmethod
    def _ensure_webhook_token(
        channel: ServerChannel, transport: RegisteredTransport
    ) -> None:
        """Give ``channel`` a webhook token iff its transport is reached by one.

        The requirement is the transport's declaration, not the channel type:
        a webhook channel with no token is not reachable at all, and now that
        ``ServerChannel.webhook_token`` is nullable that is no longer impossible
        — it is a channel that silently receives nothing. Enforced here, at both
        write paths, rather than checked at the webhook — by then the symptom is
        a 404 with no way to tell it from a bad URL.

        This is the **only** place a token is minted on create. ``create_channel``
        constructs the row with ``webhook_token=None`` and defers entirely to
        this rule, so "does this channel have a token?" has exactly one answer
        and it is the transport's.

        Minting rather than raising is deliberate: the token is generated by
        this service and an admin has no way to supply one, so refusing a
        create over a missing token would blame them for something they cannot
        provide.

        A transport that declares ``needs_webhook_token=False`` is left alone.
        It gets no token here and none is required of it. Left alone rather than
        cleared: on the update path a ``channel_type`` flip is reversible, and
        discarding the token would silently invalidate the URL an admin has
        already pasted into the platform on the other side.
        """
        if transport.needs_webhook_token and not channel.webhook_token:
            channel.webhook_token = ServerChannelService.generate_webhook_token()

    @staticmethod
    def has_outbound_credentials(channel: ServerChannel) -> bool:
        """Whether an outbound credential has been configured for ``channel``.

        Derived, never a column — and *where* it derives from is the
        transport's call, so this method asks it and does not decide. The
        default reading (``ChannelAdapter.has_outbound_credentials``) is the
        presence of the ``encrypted_secrets`` blob, which is where a transport
        declaring ``needs_outbound_credentials=True`` keeps its credential.
        Email declares ``False`` and overrides: its credential is the
        server-scoped SMTP config its ``outgoing_server_id`` names, so a fully
        operational email channel has an empty ``encrypted_secrets`` and the
        old reading reported it — wrongly, and on the one screen an admin
        checks to see whether a channel can reply — as having no way to answer.

        The delegation is what keeps that knowledge in one place. This service
        must not learn what an ``outgoing_server_id`` is, and the registry
        refuses to import an adapter that declares ``False`` without saying
        where its credential actually lives.

        Public rather than private because ``ServerChannelPublic`` declares
        this field required-with-no-default on purpose — a service that forgets
        to populate it should fail loudly — so any future projection needs the
        same derivation available, not a second copy of it.

        **Total.** Asking the transport is what introduced a way for this to
        fail, and this is not a place that may fail: see the degrade below.
        """
        try:
            transport = get_transport(channel.channel_type)
        except ChannelError:
            # A stored row whose adapter is no longer registered. ``to_public``
            # calls this per row inside a list comprehension in the admin list
            # route, so raising here would 500 the whole Channels tab — and the
            # admin would then see *no* channels, including the offending one
            # they need to disable or delete. The row becomes unmanageable
            # through the very surface that manages it.
            #
            # Same degrade ``_invalidate_adapter_caches`` makes eleven lines
            # below on the same lookup; the same one ``ServerChannel``'s
            # ``channel_type`` docs ask of any reader meeting an unrecognised
            # value; and the same one the webhook route makes deliberately for
            # ``UnknownChannelTypeError`` ("a 500 here would be an oracle").
            #
            # ``bool(channel.encrypted_secrets)`` is the honest fallback rather
            # than a flat False: it is exactly the answer this method gave
            # before the transport split, and with no transport to ask there is
            # nothing better to derive from than the column itself.
            return bool(channel.encrypted_secrets)
        return transport.adapter.has_outbound_credentials(channel)

    @staticmethod
    def _invalidate_adapter_caches(channel: ServerChannel) -> None:
        """Drop any per-channel adapter state (e.g. cached OAuth tokens)."""
        try:
            adapter = get_adapter(channel.channel_type)
        except ChannelError:
            return
        invalidate = getattr(adapter, "invalidate_token_cache", None)
        if callable(invalidate):
            invalidate(channel.id)


__all__ = [
    "ServerChannelService",
    "ChannelNotFoundError",
    "DuplicateChannelNameError",
    "InvalidChannelPolicyError",
    "UnsupportedChannelOperationError",
]
