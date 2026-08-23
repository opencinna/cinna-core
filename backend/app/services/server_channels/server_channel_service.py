"""Admin-side channel management: CRUD, secrets, tokens, auto-install list.

Everything here is superuser-facing. The inbound pipeline only borrows one
method from this service — ``get_by_webhook_token`` — and that method is the
reason the channel lookup is enabled-only: a disabled channel must be
indistinguishable from a nonexistent one at the webhook.

Secret discipline: ``ServerChannel.encrypted_secrets`` is written here and
read only by the adapter. It is never projected into a DTO, never logged, and
an update that omits the field leaves the stored value untouched (so an admin
editing the whitelist doesn't have to re-paste a service-account key).
"""
from __future__ import annotations

import logging
import secrets as secrets_module
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import encrypt_field
from app.models import (
    AgentBundle,
    AgentBundleRevision,
    AutoInstallBundlePublic,
    ChannelRecentSender,
    ChannelSetupInstructions,
    ChannelThreadBinding,
    ServerAutoInstallBundle,
    ServerChannel,
    ServerChannelCreate,
    ServerChannelPublic,
    ServerChannelUpdate,
    User,
)
from app.services.server_channels.adapters.base import ChannelError
from app.services.server_channels.adapters.registry import get_adapter
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
    def get_by_webhook_token(session: Session, token: str) -> ServerChannel | None:
        """Resolve an ENABLED channel by webhook token.

        Enabled-only on purpose: the webhook returns the same 404 for an
        unknown token and a disabled channel, so toggling a channel off does
        not become an oracle for "this token exists".
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
    def webhook_url(channel: ServerChannel) -> str:
        """Public webhook URL for this channel.

        Built from ``settings.webhook_base_url`` — the externally reachable
        backend origin (``WEBHOOK_BASE_URL``, falling back to
        ``FRONTEND_HOST``) — never from the request, so the value an admin
        pastes into Google is right even when they reached the admin page over
        an internal address.
        """
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
            config=channel.config or {},
            email_whitelist=channel.email_whitelist,
            webhook_token=channel.webhook_token,
            webhook_url=ServerChannelService.webhook_url(channel),
            has_outbound_credentials=bool(channel.encrypted_secrets),
            created_by=channel.created_by,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )

    @staticmethod
    def get_setup_instructions(channel: ServerChannel) -> ChannelSetupInstructions:
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
        adapter = get_adapter(data.channel_type)  # raises UnknownChannelTypeError
        config = data.config or {}
        adapter.validate_config(config)

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
            config=config,
            email_whitelist=ServerChannelService._clean_whitelist(data.email_whitelist),
            encrypted_secrets=(
                encrypt_field(data.secrets) if (data.secrets or "").strip() else None
            ),
            webhook_token=ServerChannelService.generate_webhook_token(),
            created_by=user.id,
        )
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

        channel_type = patch.get("channel_type", channel.channel_type)
        if "channel_type" in patch:
            get_adapter(channel_type)  # validate the new type exists

        if "config" in patch:
            config = patch.pop("config") or {}
            get_adapter(channel_type).validate_config(config)
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

        ``has_trigger_prompt`` is resolved from each bundle's latest revision:
        a bundle without a ``router_trigger_prompt`` can never win Pass 2, and
        the admin UI flags it rather than leaving the operator to wonder why
        nothing routes.
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

        return [
            AutoInstallBundlePublic(
                bundle_uuid=bundle.id,
                bundle_id=bundle.bundle_id,
                display_name=bundle.display_name,
                visibility=bundle.visibility,
                has_trigger_prompt=bool(
                    bundle.latest_revision_id
                    and (prompts.get(bundle.latest_revision_id) or "").strip()
                ),
                added_by=entry.added_by,
                created_at=entry.created_at,
            )
            for entry, bundle in rows
        ]

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
    # Internals
    # ------------------------------------------------------------------

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
]
