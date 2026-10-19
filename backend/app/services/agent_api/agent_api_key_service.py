"""
Agent REST API **issued keys** — issue / access list / revoke / rotate / reveal.

An issued key is the second product behind the agent-api proxy. Its sibling, the
*connection*, wires one platform agent to another and is machine-only. A key
points outward: the producer's owner (the **issuer**) gives API access to a
platform user (the **recipient**, possibly the issuer themself), who copies it
into a laptop script, a server, a cron job, or links it to their own agents. The
platform treats that caller exactly like a peer agent — same proxy, same
``policy.yaml``, same live scope grant, same ``caller`` accessor in the
producer's code.

Who holds what:

- ``AgentApiToken.kind == "external"`` — the single source of truth.
- ``owner_id`` — the issuer. ``subject_user_id`` — the recipient; the key acts
  as them. Both are set at issue; the recipient never changes.
- The key's ``agent_api`` credential is **owned by the recipient**. Only they
  can reveal, rotate, link, or delete it (no superuser bypass). The issuer gets
  the value back on issue only when they issued it to themselves.
- Revocation by the issuer is **soft**: the token row is marked revoked and the
  recipient's credential survives so they see why it stopped. Only the
  recipient deleting the credential removes the row (cascade).
- Status comes from ``agent_api_key_status.key_status`` and nowhere else.

Scopes are NOT stored on the key. They live on the ``(producer, recipient)``
grant, so capability can change without rotating the secret and the secret can
rotate without touching capability. ``create_key`` therefore *upserts* the grant.

Producer-side gating mirrors the ``/grants*`` routes:
``AgentApiService.resolve_agent_only`` (404, never a 403 — no existence leak).
Issuing additionally requires the producer's ``agent_api_external_access_enabled``.
"""
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, func, select

from app.models import (
    Agent,
    AgentApiAccessEntry,
    AgentApiAccessGrant,
    AgentApiAccessGroupEntry,
    AgentApiAccessList,
    AgentApiConnectedAgent,
    AgentApiKeyCreate,
    AgentApiKeyCreated,
    AgentApiKeyHolderInfo,
    AgentApiKeyProducer,
    AgentApiKeyPublic,
    AgentApiKeyRotateResponse,
    AgentApiKeyStatus,
    AgentApiKeyUser,
    AgentApiToken,
    AgentApiTokenKind,
    CredentialCreate,
    SecurityEventCreate,
    User,
)
from app.models.credentials.credential import Credential, CredentialType
from app.models.credentials.link_models import AgentCredentialLink
from app.models.events.security_event import (
    AGENT_API_EXTERNAL_KEY_CREATED,
    AGENT_API_EXTERNAL_KEY_DELETED_BY_HOLDER,
    AGENT_API_EXTERNAL_KEY_REVEALED,
    AGENT_API_EXTERNAL_KEY_REVOKED,
    AGENT_API_EXTERNAL_KEY_ROTATED,
)
from app.services.agent_api.agent_api_grant_service import AgentApiGrantService
from app.services.users.user_group_access import UserGroupAccess
from app.services.agent_api.agent_api_key_status import (
    AGENT_API_KEY_ROTATION_GRACE,
    REVOKED_BY_ISSUER_REASON,
    KeyState,
    is_terminal,
    key_state_for_credential,
    key_status,
    previous_valid_until,
)
from app.services.agent_api.agent_api_service import AgentApiError, AgentApiService
from app.services.agent_api.agent_api_token_service import AgentApiTokenService
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

# D23: slug of the producer name, capped, then "-api".
_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_LENGTH = 40


class AgentApiKeyError(AgentApiError):
    """Key-specific service error (reuses the agent-api status-code shape)."""


class AgentApiKeyService:
    """Issued-key lifecycle: issuer side (producer-gated) and recipient side
    (credential-owner-gated)."""

    # ------------------------------------------------------------------ #
    # Issue                                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def default_service_uri(producer_name: str | None) -> str:
        """Default slot for an issued key's credential (plan D23).

        Lowercase, runs of non-``[a-z0-9]`` become ``-``, trimmed of ``-``, the
        slug capped at 40 chars (and re-trimmed so the cut never leaves a
        trailing ``-``), empty becomes ``agent``; then ``-api``.
        ``"CRM Agent"`` → ``"crm-agent-api"``. The migration backfill uses the
        same rule in SQL.
        """
        slug = _SLUG_SEPARATORS.sub("-", (producer_name or "").lower()).strip("-")
        slug = slug[:_SLUG_MAX_LENGTH].strip("-")
        return f"{slug or 'agent'}-api"

    @staticmethod
    async def create_key(
        session: Session,
        producer_agent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        data: AgentApiKeyCreate,
        is_superuser: bool = False,
    ) -> AgentApiKeyCreated:
        """Issue a key on ``producer_agent_id`` to a recipient.

        ``owner_user_id`` is the issuer (the caller). The recipient is
        ``data.issued_to_user_id`` or, when omitted, the issuer.

        Steps, in order, so a rejected request leaves no live key behind:
          1. Owner-gate the producer (404, no leak) and check both opt-ins.
          2. Resolve the recipient (404 if unknown).
          3. Upsert the ``(producer, recipient)`` grant when ``scopes`` was given.
          4. Mint the opaque token + its ``agent_api`` credential (owned by the
             recipient), then bind them. Credential creation is compensated: if
             it fails, the token row is removed, so a failed issue never leaves
             a live unbound key behind.
          5. Audit CREATED on the issuer's account.

        The value is returned only on a self-issue. A key issued to someone else
        comes back with ``token = None``: the recipient reveals it from their
        own credential.
        """
        agent = AgentApiKeyService._require_owner(
            session, producer_agent_id, owner_user_id, is_superuser
        )

        # Two opt-ins, both 400. External access is the deliberate act that
        # exposes this API to issued keys; agent_api_enabled is the pre-existing
        # invariant the connect helper also enforces.
        if not agent.agent_api_external_access_enabled:
            raise AgentApiKeyError(
                "External API keys are disabled for this agent. Enable external "
                "access on the producer's Agent REST API card first.",
                status_code=400,
                code="external_access_disabled",
            )
        if not agent.agent_api_enabled:
            raise AgentApiKeyError(
                "Agent REST API is disabled for this producer agent",
                status_code=400,
                code="api_disabled",
            )

        recipient_id = data.issued_to_user_id or owner_user_id
        recipient = session.get(User, recipient_id)
        if recipient is None:
            raise AgentApiKeyError(
                "Subject user not found",
                status_code=404,
                code="recipient_not_found",
            )
        issuer = session.get(User, owner_user_id)
        issuer_email = issuer.email if issuer is not None else str(owner_user_id)
        is_self_issue = recipient.id == owner_user_id

        label = data.label or f"{agent.name} API key"
        service_uri = (data.service_uri or "").strip() or (
            AgentApiKeyService.default_service_uri(agent.name)
        )
        expires_at: datetime | None = None
        if data.expires_in_days is not None:
            expires_at = datetime.now(UTC) + timedelta(days=data.expires_in_days)

        base_url = AgentApiTokenService.build_base_url(producer_agent_id)
        spec_url = AgentApiTokenService.build_spec_url(producer_agent_id)

        # Scopes live on the grant. Upsert BEFORE minting so a grant failure
        # aborts with nothing issued.
        if data.scopes is not None:
            await AgentApiGrantService.upsert_grant(
                session,
                producer_agent_id=producer_agent_id,
                owner_user_id=owner_user_id,
                subject_user_id=recipient.id,
                scopes=data.scopes,
                is_superuser=is_superuser,
            )

        token_value = secrets.token_urlsafe(32)
        token = AgentApiToken(
            agent_id=producer_agent_id,
            owner_id=owner_user_id,
            token_hash=AgentApiTokenService.hash_token(token_value),
            token_prefix=token_value[:8],
            label=label,
            read_only_override=data.read_only_override,
            kind=AgentApiTokenKind.EXTERNAL.value,
            subject_user_id=recipient.id,
            expires_at=expires_at,
            is_active=True,
        )
        session.add(token)
        session.commit()
        session.refresh(token)
        token_id = token.id

        # The key IS a credential, owned by the recipient. Sharing is forced
        # OFF (assert_sharing_allowed also refuses it later). Workspace: the
        # producer's, but ONLY when the recipient owns the producer — anyone
        # else's credential must never be stamped with a workspace from a
        # different account.
        from app.services.credentials.credentials_service import CredentialsService

        workspace_id = (
            agent.user_workspace_id if agent.owner_id == recipient.id else None
        )
        try:
            credential = CredentialsService.create_credential(
                session,
                CredentialCreate(
                    name=label,
                    type=CredentialType.AGENT_API,
                    notes=f"Issued by {issuer_email} for {agent.name}",
                    allow_sharing=False,
                    service_uri=service_uri,
                    user_workspace_id=workspace_id,
                    credential_data={
                        "base_url": base_url,
                        "spec_url": spec_url,
                        "token": token_value,
                        "label": label,
                        "producer_agent_id": str(producer_agent_id),
                    },
                ),
                owner_id=recipient.id,
            )
        except Exception:
            # Compensate: drop the token so no live, unrevocable key survives a
            # failed issue. The value was never returned to anyone.
            session.rollback()
            orphan = session.get(AgentApiToken, token_id)
            if orphan is not None:
                session.delete(orphan)
                session.commit()
            logger.exception(
                "Failed to create the credential for a new agent_api issued key "
                "on agent %s; rolled the token back",
                producer_agent_id,
            )
            raise

        token.credential_id = credential.id
        session.add(token)
        session.commit()
        session.refresh(token)

        await AgentApiKeyService._audit(
            session,
            account_user_id=owner_user_id,
            event_type=AGENT_API_EXTERNAL_KEY_CREATED,
            token=token,
        )
        logger.info(
            "Issued agent_api key %s on agent %s to user %s (issuer %s)",
            token.id,
            producer_agent_id,
            recipient.id,
            owner_user_id,
        )

        public = AgentApiKeyService._public_with_context(session, token, agent)
        return AgentApiKeyCreated(
            **public.model_dump(),
            token=token_value if is_self_issue else None,
            # ALWAYS the public URL. AGENT_ENV_BACKEND_URL is an env-sync-only
            # rewrite for containers.
            base_url=base_url,
            spec_url=spec_url,
        )

    # ------------------------------------------------------------------ #
    # Issuer view: API access per person                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def list_access(
        session: Session,
        producer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> AgentApiAccessList:
        """Everyone with access to this producer: their grant and issued keys.

        The union of grants and issued keys, grouped by user. Terminal keys are
        included (the UI puts them behind a disclosure). Never the value, never
        the recipient's agent names — only a linked-agent count.
        """
        agent = AgentApiKeyService._require_owner(
            session, producer_agent_id, user_id, is_superuser
        )
        grants = session.exec(
            select(AgentApiAccessGrant).where(
                AgentApiAccessGrant.producer_agent_id == producer_agent_id
            )
        ).all()
        tokens = session.exec(
            select(AgentApiToken)
            .where(
                AgentApiToken.agent_id == producer_agent_id,
                AgentApiToken.kind == AgentApiTokenKind.EXTERNAL.value,
            )
            .order_by(AgentApiToken.created_at.desc())
        ).all()

        users = AgentApiKeyService._load_users(
            session,
            [g.user_id for g in grants] + [t.subject_user_id for t in tokens],
        )
        link_counts = AgentApiKeyService._linked_agent_counts(
            session, [t.credential_id for t in tokens if t.credential_id]
        )
        now = datetime.now(UTC)

        entries: dict[uuid.UUID, AgentApiAccessEntry] = {}

        def entry_for(person_id: uuid.UUID) -> AgentApiAccessEntry | None:
            if person_id not in entries:
                person = users.get(person_id)
                if person is None:
                    return None
                entries[person_id] = AgentApiAccessEntry(user=person)
            return entries[person_id]

        for grant in grants:
            entry = entry_for(grant.user_id)
            if entry is not None:
                entry.grant_id = grant.id
                entry.scopes = list(grant.scopes or [])
        for token in tokens:
            if token.subject_user_id is None:
                continue
            entry = entry_for(token.subject_user_id)
            if entry is not None:
                entry.keys.append(
                    AgentApiKeyService._to_public(
                        token,
                        agent,
                        issued_to=users.get(token.subject_user_id),
                        linked_agent_count=link_counts.get(token.credential_id, 0),
                        now=now,
                    )
                )

        data = sorted(
            entries.values(), key=lambda e: ((e.user.email or "").lower(), str(e.user.id))
        )
        return AgentApiAccessList(
            data=data,
            count=len(data),
            groups=AgentApiKeyService._group_entries(
                session, producer_agent_id, user_id, is_superuser
            ),
            external_access_enabled=agent.agent_api_external_access_enabled,
            identity_enabled=agent.agent_api_identity_enabled,
        )

    @staticmethod
    def _group_entries(
        session: Session,
        producer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool,
    ) -> list[AgentApiAccessGroupEntry]:
        """The user groups holding scopes on this producer, sorted by name."""
        grants = AgentApiGrantService.list_group_grants(
            session, producer_agent_id, user_id, is_superuser
        )
        counts = UserGroupAccess.member_counts(session, {g.group_id for g in grants})
        entries: list[AgentApiAccessGroupEntry] = []
        for grant in grants:
            row = AgentApiGrantService.group_to_public(session, grant, counts)
            entries.append(
                AgentApiAccessGroupEntry(
                    grant_id=row["id"],
                    group_id=row["group_id"],
                    name=row["group"]["name"] if row["group"] else "",
                    member_count=row["member_count"],
                    scopes=row["scopes"],
                )
            )
        return entries

    # ------------------------------------------------------------------ #
    # Revoke (issuer)                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def revoke_key(
        session: Session,
        producer_agent_id: uuid.UUID,
        key_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> AgentApiKeyPublic:
        """Revoke a key immediately (producer-owner-gated). Soft and idempotent.

        The token row is marked revoked (and its rotation grace closed); the
        recipient's credential is never deleted from this side — they see
        ``revoked_by_issuer`` until they delete it. The credential's
        ``updated_at`` is touched and the recipient's linked environments are
        re-synced so running agents lose the value.

        The ``(producer, recipient)`` grant is left alone: capability and secret
        have independent lifecycles.
        """
        from app.services.credentials.credentials_service import CredentialsService

        agent = AgentApiKeyService._require_owner(
            session, producer_agent_id, user_id, is_superuser
        )
        token = AgentApiKeyService._load_owned_key(session, producer_agent_id, key_id)

        if token.revoked_at is not None:
            return AgentApiKeyService._public_with_context(session, token, agent)

        now = datetime.now(UTC)
        token.is_active = False
        token.revoked_at = now
        token.revoked_reason = REVOKED_BY_ISSUER_REASON
        token.previous_token_hash = None
        token.previous_token_expires_at = None
        token.updated_at = now
        session.add(token)
        credential = (
            session.get(Credential, token.credential_id)
            if token.credential_id is not None
            else None
        )
        if credential is not None:
            credential.updated_at = now
            session.add(credential)
        session.commit()
        session.refresh(token)

        if credential is not None:
            await CredentialsService.event_credential_updated(
                session=session, credential_id=credential.id
            )

        await AgentApiKeyService._audit(
            session,
            account_user_id=user_id,
            event_type=AGENT_API_EXTERNAL_KEY_REVOKED,
            token=token,
        )
        if token.subject_user_id is not None and token.subject_user_id != user_id:
            await AgentApiKeyService._audit(
                session,
                account_user_id=token.subject_user_id,
                event_type=AGENT_API_EXTERNAL_KEY_REVOKED,
                token=token,
            )
        logger.info(
            "Revoked agent_api issued key %s on agent %s", key_id, producer_agent_id
        )
        return AgentApiKeyService._public_with_context(session, token, agent)

    # ------------------------------------------------------------------ #
    # Recipient side (credential owner only, no superuser bypass)          #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def rotate_key(
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AgentApiKeyRotateResponse:
        """Swap an issued key's value in place (recipient only, plan D6).

        Same credential id, same token row. The previous value keeps
        authenticating for ``AGENT_API_KEY_ROTATION_GRACE``; a second rotate
        inside the window replaces it. The new value is written through
        ``CredentialsService.update_credential`` so ``updated_at`` moves and the
        recipient's linked environments re-sync (``service_uri`` untouched).
        The token row is locked for the duration.

        404 unless the caller owns the credential; 400 if it is not an issued
        key; 409 when the key is in a terminal status.
        """
        from app.services.credentials.credentials_service import CredentialsService

        credential, state = AgentApiKeyService._load_holder_key(
            session, credential_id, user_id
        )
        # Serialise concurrent rotates of the same key: lock the token row and
        # re-derive the status from the locked row, so token_hash and the
        # stored value can never come from two different rotates.
        token = session.exec(
            select(AgentApiToken)
            .where(AgentApiToken.id == state.token.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        locked_state = KeyState(
            token=token,
            agent=state.agent,
            issuer_email=state.issuer_email,
            status=key_status(token, state.agent),
        )
        AgentApiKeyService._refuse_terminal(locked_state, "rotated")

        now = datetime.now(UTC)
        new_value = secrets.token_urlsafe(32)
        grace_until = now + AGENT_API_KEY_ROTATION_GRACE
        token.previous_token_hash = token.token_hash
        token.previous_token_expires_at = grace_until
        token.token_hash = AgentApiTokenService.hash_token(new_value)
        token.token_prefix = new_value[:8]
        token.rotated_at = now
        token.issuer_saw_value = False
        token.updated_at = now
        session.add(token)

        # The value is written here and only here: the generic credential
        # update never accepts a token for an issued key. One commit for the
        # token row and the encrypted value (service_uri untouched).
        stored = CredentialsService.decrypt_credential_data(
            session=session, credential=credential
        )
        CredentialsService.write_credential_data(
            credential, {**stored, "token": new_value}
        )
        credential.updated_at = now
        session.add(credential)
        session.commit()
        session.refresh(token)

        await CredentialsService.event_credential_updated(
            session=session, credential_id=credential_id
        )

        await AgentApiKeyService._audit(
            session,
            account_user_id=user_id,
            event_type=AGENT_API_EXTERNAL_KEY_ROTATED,
            token=token,
        )
        logger.info(
            "Rotated agent_api issued key %s (credential %s)", token.id, credential_id
        )
        return AgentApiKeyRotateResponse(
            token=new_value,
            token_prefix=token.token_prefix,
            previous_valid_until=grace_until,
        )

    @staticmethod
    async def reveal_external_key(
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> str:
        """Return an issued key's value and audit the reveal.

        The way to read a key back after issue. Deliberately NOT
        ``GET /credentials/{id}/with-data``: that route fires on every
        detail-page open, so auditing there would record "page opened" rather
        than "secret revealed" — and ``with-data`` strips ``token`` for issued
        keys precisely so this endpoint is the single audited door.

        **Credential owner (the recipient) only — no superuser bypass,
        deliberately.** Reveal is disclosure; an admin who suspects a key is
        compromised should have it revoked, not read it. The issuer cannot read
        a key issued to someone else either.

        404 for "no such credential", "not yours", *and* "admin but not the
        owner" — no existence leak. 400 when the credential is not an issued
        key. 409 when the key is in a terminal status.
        """
        from app.services.credentials.credentials_service import CredentialsService

        credential, state = AgentApiKeyService._load_holder_key(
            session, credential_id, user_id
        )
        AgentApiKeyService._refuse_terminal(state, "revealed")

        data = CredentialsService.decrypt_credential_data(
            session=session, credential=credential
        )
        token_value = data.get("token")
        if not token_value:
            raise AgentApiKeyError(
                "This key's value is no longer stored and cannot be revealed. "
                "Rotate it, or ask the issuer for a new one.",
                status_code=400,
            )

        await AgentApiKeyService._audit(
            session,
            account_user_id=user_id,
            event_type=AGENT_API_EXTERNAL_KEY_REVEALED,
            token=state.token,
        )
        logger.info(
            "Revealed agent_api issued key %s (credential %s) to user %s",
            state.token.id,
            credential_id,
            user_id,
        )
        return token_value

    @staticmethod
    def get_holder_info(
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AgentApiKeyHolderInfo:
        """The recipient's view of an issued key (credential owner only)."""
        credential, state = AgentApiKeyService._load_holder_key(
            session, credential_id, user_id
        )
        token = state.token
        agent = state.agent

        producer: AgentApiKeyProducer | None = None
        scopes: list[str] = []
        base_url: str | None = None
        spec_url: str | None = None
        if agent is not None:
            producer = AgentApiKeyProducer(
                id=agent.id, name=agent.name, ui_color_preset=agent.ui_color_preset
            )
            base_url = AgentApiTokenService.build_base_url(agent.id)
            spec_url = AgentApiTokenService.build_spec_url(agent.id)
            if token.subject_user_id is not None:
                # Personal grant ∪ the recipient's active groups: what the
                # proxy will actually inject on a call with this key.
                scopes = AgentApiGrantService.effective_scopes(
                    session, agent.id, token.subject_user_id
                )

        issuer: AgentApiKeyUser | None = None
        if token.owner_id is not None:
            issuer = AgentApiKeyService._load_users(session, [token.owner_id]).get(
                token.owner_id
            )

        return AgentApiKeyHolderInfo(
            credential_id=credential.id,
            token_prefix=token.token_prefix,
            status=state.status,
            service_uri=credential.service_uri,
            producer=producer,
            issuer=issuer,
            scopes=scopes,
            read_only=token.read_only_override,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            rotated_at=token.rotated_at,
            previous_valid_until=previous_valid_until(token),
            issuer_saw_value=token.issuer_saw_value,
            base_url=base_url,
            spec_url=spec_url,
            linked_agents=AgentApiKeyService._linked_agents(session, credential.id),
        )

    @staticmethod
    async def get_holder_spec(
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """The producer's OpenAPI spec for the key's recipient.

        Same gates as the consumer spec route (producer env resolved on the
        producer's behalf, ``expose_spec`` honoured). 409 unless the key is
        active; transport failures become 502.
        """
        _credential, state = AgentApiKeyService._load_holder_key(
            session, credential_id, user_id
        )
        agent = state.agent
        if state.status != AgentApiKeyStatus.ACTIVE or agent is None:
            # Only a key that would authenticate at the proxy gets the spec.
            raise AgentApiKeyError(
                f"This key cannot be used right now ({state.status.value})",
                status_code=409,
            )

        try:
            environment = await AgentApiService.resolve_running_producer_env(
                session, agent
            )
            policy = await AgentApiService.load_policy(session, environment)
            if not policy.get("expose_spec", True):
                raise AgentApiKeyError(
                    "Spec is not exposed by this API", status_code=403
                )
            spec = await AgentApiService.get_spec(session, environment)
        except AgentApiError:
            raise
        except Exception as e:
            logger.error(
                "agent_api holder spec failed for credential %s: %s", credential_id, e
            )
            raise AgentApiKeyError(
                "Could not fetch the producer's API spec", status_code=502
            ) from e
        AgentApiService.update_last_activity(session, environment)
        return spec

    @staticmethod
    async def on_holder_delete(session: Session, credential: Credential) -> None:
        """Audit the recipient deleting their issued-key credential.

        Called by ``CredentialsService.delete_credential`` before the row is
        removed (the token cascades with it). Writes DELETED_BY_HOLDER on the
        issuer's account; a no-op for anything that is not an issued key or
        whose issuer is gone.
        """
        if credential.type != CredentialType.AGENT_API:
            return
        token = session.exec(
            select(AgentApiToken).where(
                AgentApiToken.credential_id == credential.id,
                AgentApiToken.kind == AgentApiTokenKind.EXTERNAL.value,
            )
        ).first()
        if token is None or token.owner_id is None:
            return
        await AgentApiKeyService._audit(
            session,
            account_user_id=token.owner_id,
            event_type=AGENT_API_EXTERNAL_KEY_DELETED_BY_HOLDER,
            token=token,
        )

    # ------------------------------------------------------------------ #
    # Producer-side changes that alter every key's status                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def issued_key_credential_ids(
        session: Session, producer_agent_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Credential ids of every key issued on a producer."""
        rows = session.exec(
            select(AgentApiToken.credential_id).where(
                AgentApiToken.agent_id == producer_agent_id,
                AgentApiToken.kind == AgentApiTokenKind.EXTERNAL.value,
                AgentApiToken.credential_id.is_not(None),
            )
        ).all()
        return [cid for cid in rows if cid is not None]

    @staticmethod
    async def resync_issued_key_credentials(
        session: Session, credential_ids: list[uuid.UUID]
    ) -> None:
        """Touch ``updated_at`` and re-sync linked environments for these keys.

        Used after a producer is deleted or its API / external access is
        toggled: every key's status changed without its row being written.
        Never raises — the producer-side change has already committed.
        """
        if not credential_ids:
            return
        from app.services.credentials.credentials_service import CredentialsService

        try:
            now = datetime.now(UTC)
            credentials = session.exec(
                select(Credential).where(Credential.id.in_(credential_ids))
            ).all()
            for credential in credentials:
                credential.updated_at = now
                session.add(credential)
            session.commit()
            for credential_id in credential_ids:
                await CredentialsService.event_credential_updated(
                    session=session, credential_id=credential_id
                )
        except Exception:
            logger.exception(
                "Failed to re-sync %d issued-key credential(s) after a producer change",
                len(credential_ids),
            )

    @staticmethod
    def is_external_key_credential(
        session: Session,
        credential_id: uuid.UUID,
        credential_type: CredentialType | str | None = None,
    ) -> bool:
        """Should this credential be treated as an external key, not a connection?

        Drives the ``GET /credentials/{id}/with-data`` token strip and the
        ``update_credential`` carry-forward. ``credential_type`` lets the caller
        skip the lookup for the overwhelming majority of credentials (every
        non-``agent_api`` type); when omitted it is read from the row.

        **Unbound ⇒ False, deliberately** — note this diverges from
        ``AgentApiTokenService.is_restricted_agent_api_credential``, which treats
        an unbound ``agent_api`` credential as restricted. The divergence is
        intentional because the two predicates answer different questions:
        that one gates *sharing and env sync* (handing a dead secret onward has
        no upside, so fail closed), this one gates whether the owner may see a
        value **they already own**.

        Failing closed here would lock the owner out of a credential they
        created by hand (``POST /credentials/`` accepts ``type="agent_api"``
        with an arbitrary ``token``, which never has a bound row) — they could
        neither read it back through ``with-data`` nor reveal it, since the
        reveal endpoint requires a bound external-key row.

        Issued-key rows outlive revocation and producer / issuer removal (soft
        revoke, SET NULL), so a key credential stays recognisable here until the
        recipient deletes it. Credentials orphaned before the issued-keys
        migration (token cascaded away with its old subject) cannot be
        identified and stay as they were.
        """
        if credential_type is not None:
            type_value = getattr(credential_type, "value", credential_type)
            if type_value != CredentialType.AGENT_API.value:
                return False
        token = session.exec(
            select(AgentApiToken).where(
                AgentApiToken.credential_id == credential_id,
                AgentApiToken.kind == AgentApiTokenKind.EXTERNAL.value,
            )
        ).first()
        return token is not None

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_owner(
        session: Session,
        producer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool,
    ) -> Agent:
        """Resolve + ownership-check the producer agent (404 on non-owner)."""
        return AgentApiService.resolve_agent_only(
            session, producer_agent_id, user_id, is_superuser=is_superuser
        )

    @staticmethod
    def _load_owned_key(
        session: Session,
        producer_agent_id: uuid.UUID,
        key_id: uuid.UUID,
    ) -> AgentApiToken:
        """Load a key, asserting it is an external key on this producer (404 else)."""
        token = session.get(AgentApiToken, key_id)
        if (
            token is None
            or token.agent_id != producer_agent_id
            or token.kind != AgentApiTokenKind.EXTERNAL.value
        ):
            raise AgentApiKeyError(
                "Key not found", status_code=404, code="key_not_found"
            )
        return token

    @staticmethod
    def _load_holder_key(
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> tuple[Credential, KeyState]:
        """Load an issued key for its recipient (credential owner only).

        404 when the credential does not exist or the caller does not own it
        (superusers included); 400 when it is not an issued key.
        """
        credential = session.get(Credential, credential_id)
        if credential is None or credential.owner_id != user_id:
            raise AgentApiKeyError("Credential not found", status_code=404)
        if credential.type != CredentialType.AGENT_API:
            raise AgentApiKeyError(
                "This credential is not an agent API key", status_code=400
            )
        state = key_state_for_credential(session, credential_id)
        if state is None:
            raise AgentApiKeyError(
                "This credential is not an agent API key", status_code=400
            )
        return credential, state

    @staticmethod
    def _refuse_terminal(state: KeyState, action: str) -> None:
        """409 when the key can never work again."""
        if is_terminal(state.status):
            raise AgentApiKeyError(
                f"This key can no longer be {action} ({state.status.value})",
                status_code=409,
            )

    @staticmethod
    def _load_users(
        session: Session, user_ids: list[uuid.UUID | None]
    ) -> dict[uuid.UUID, AgentApiKeyUser]:
        """Batch-resolve users for a page of keys / grants."""
        wanted = {uid for uid in user_ids if uid is not None}
        if not wanted:
            return {}
        users = session.exec(select(User).where(User.id.in_(wanted))).all()
        return {
            user.id: AgentApiKeyUser(
                id=user.id, email=user.email, full_name=user.full_name
            )
            for user in users
        }

    @staticmethod
    def _linked_agent_counts(
        session: Session, credential_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """``credential_id -> number of linked agents`` in one query."""
        if not credential_ids:
            return {}
        rows = session.exec(
            select(AgentCredentialLink.credential_id, func.count())
            .where(AgentCredentialLink.credential_id.in_(credential_ids))
            .group_by(AgentCredentialLink.credential_id)
        ).all()
        return {credential_id: count for credential_id, count in rows}

    @staticmethod
    def _linked_agents(
        session: Session, credential_id: uuid.UUID
    ) -> list[AgentApiConnectedAgent]:
        """The recipient's agents this key credential is linked to."""
        agents = session.exec(
            select(Agent)
            .join(AgentCredentialLink, AgentCredentialLink.agent_id == Agent.id)
            .where(AgentCredentialLink.credential_id == credential_id)
            .order_by(Agent.name)
        ).all()
        return [
            AgentApiConnectedAgent(
                id=agent.id, name=agent.name, ui_color_preset=agent.ui_color_preset
            )
            for agent in agents
        ]

    @staticmethod
    def _public_with_context(
        session: Session, token: AgentApiToken, agent: Agent | None
    ) -> AgentApiKeyPublic:
        """``_to_public`` for one row, resolving recipient and link count."""
        return AgentApiKeyService._to_public(
            token,
            agent,
            issued_to=AgentApiKeyService._load_users(
                session, [token.subject_user_id]
            ).get(token.subject_user_id) if token.subject_user_id else None,
            linked_agent_count=AgentApiKeyService._linked_agent_counts(
                session, [token.credential_id] if token.credential_id else []
            ).get(token.credential_id, 0),
        )

    @staticmethod
    def _to_public(
        token: AgentApiToken,
        agent: Agent | None,
        issued_to: AgentApiKeyUser | None = None,
        linked_agent_count: int = 0,
        now: datetime | None = None,
    ) -> AgentApiKeyPublic:
        """Project a key row for the issuer. Never carries the token value."""
        status = key_status(token, agent, now)
        return AgentApiKeyPublic(
            id=token.id,
            credential_id=token.credential_id,
            agent_id=token.agent_id,
            label=token.label,
            token_prefix=token.token_prefix,
            issued_to=issued_to,
            read_only=token.read_only_override,
            is_active=token.is_active,
            status=status,
            is_usable=status == AgentApiKeyStatus.ACTIVE,
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            revoked_at=token.revoked_at,
            rotated_at=token.rotated_at,
            linked_agent_count=linked_agent_count,
            created_at=token.created_at,
        )

    # ------------------------------------------------------------------ #
    # Audit                                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _audit(
        session: Session,
        account_user_id: uuid.UUID,
        event_type: str,
        token: AgentApiToken,
    ) -> None:
        """Write a SecurityEvent for a key lifecycle change on one account.

        Never raises. Records the 8-char display prefix only — the token value
        and its hash are never logged.
        """
        try:
            await SecurityEventService.create_event(
                session=session,
                user_id=account_user_id,
                data=SecurityEventCreate(
                    agent_id=token.agent_id,
                    event_type=event_type,
                    severity="high",
                    details={
                        "key_id": str(token.id),
                        "issued_to_user_id": (
                            str(token.subject_user_id)
                            if token.subject_user_id
                            else None
                        ),
                        "issuer_user_id": (
                            str(token.owner_id) if token.owner_id else None
                        ),
                        "token_prefix": token.token_prefix,
                    },
                ),
            )
        except Exception:
            logger.exception(
                "Failed to write SecurityEvent %s for agent_api issued key %s",
                event_type,
                token.id,
            )
