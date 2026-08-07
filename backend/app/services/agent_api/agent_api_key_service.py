"""
Agent REST API **external keys** — mint / list / revoke.

An external key is the second product behind the agent-api proxy (plan §2). Its
sibling, the *connection*, wires one platform agent to another and is machine-only.
A key points outward: a human copies it into a laptop script, a server, or a cron
job, and the platform treats that caller exactly like a peer agent — same proxy,
same ``policy.yaml``, same live scope grant, same ``caller`` accessor in the
producer's code. **The producer writes one authorization path, not two.**

What makes a key a key:

- ``AgentApiToken.kind == "external"`` — the single source of truth (plan D1).
- ``subject_user_id`` — the platform user the key ACTS AS. Set once at mint,
  immutable afterwards (plan D6): a token already in someone's hands must never
  start acting as a different user. Reassigning identity = revoke + re-issue.
- ``expires_at`` — optional; enforced in ``AgentApiTokenService.validate_token``.
- Its ``agent_api`` credential — the key IS a credential (plan D4), so the
  existing ``credential_id`` ``ON DELETE CASCADE`` gives revocation for free and
  the credential detail page is where the value is revealed.

Scopes are deliberately NOT stored on the key (plan D5). They live on the
``(producer, subject)`` grant — the same row the producer's Access & Scopes card
edits — so capability can be revoked without rotating the secret and the secret
can be rotated without touching capability. ``create_key`` therefore *upserts*
the grant rather than creating it.

Gating mirrors the ``/grants*`` routes: ``AgentApiService.resolve_agent_only``
(404, never a 403 — no existence leak). Minting additionally requires the
producer's ``agent_api_external_access_enabled`` opt-in.
"""
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.models import (
    Agent,
    AgentApiKeyCreate,
    AgentApiKeyCreated,
    AgentApiKeyPublic,
    AgentApiKeySubject,
    AgentApiToken,
    AgentApiTokenKind,
    CredentialCreate,
    SecurityEventCreate,
    User,
)
from app.models.credentials.credential import Credential, CredentialType
from app.models.events.security_event import (
    AGENT_API_EXTERNAL_KEY_CREATED,
    AGENT_API_EXTERNAL_KEY_REVEALED,
    AGENT_API_EXTERNAL_KEY_REVOKED,
)
from app.services.agent_api.agent_api_grant_service import AgentApiGrantService
from app.services.agent_api.agent_api_service import AgentApiError, AgentApiService
from app.services.agent_api.agent_api_token_service import AgentApiTokenService
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)


class AgentApiKeyError(AgentApiError):
    """Key-specific service error (reuses the agent-api status-code shape)."""


class AgentApiKeyService:
    """Owner-gated external-key lifecycle."""

    # ------------------------------------------------------------------ #
    # Mint                                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def create_key(
        session: Session,
        producer_agent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        data: AgentApiKeyCreate,
        is_superuser: bool = False,
    ) -> AgentApiKeyCreated:
        """Mint an external key on ``producer_agent_id`` bound to a subject user.

        Steps, in order, so a rejected request leaves no live key behind:
          1. Owner-gate the producer (404, no leak) and check both opt-ins.
          2. Resolve the subject user (404 if unknown).
          3. Upsert the ``(producer, subject)`` grant when ``scopes`` was given.
          4. Mint the opaque token + its ``agent_api`` credential, then bind
             them. The credential creation is compensated: if it fails, the
             token row is removed, so a failed mint never leaves a live unbound
             key behind (an unbound key would authenticate at the proxy with no
             credential to revoke it through).
          5. Audit.

        Returns the token value — the only time the platform hands it back
        unprompted. It stays revealable afterwards through the credential detail
        page (it is the deliverable, unlike a connection's machine token).
        """
        agent = AgentApiKeyService._require_owner(
            session, producer_agent_id, owner_user_id, is_superuser
        )

        # Two opt-ins, both 400. External access is the deliberate act that
        # exposes this API to copy-pasteable keys; agent_api_enabled is the
        # pre-existing invariant the connect helper also enforces (a key against
        # a disabled API would authenticate and then 404 at the proxy).
        if not agent.agent_api_external_access_enabled:
            raise AgentApiKeyError(
                "External API keys are disabled for this agent. Enable external "
                "access on the producer's Agent REST API card first.",
                status_code=400,
            )
        if not agent.agent_api_enabled:
            raise AgentApiKeyError(
                "Agent REST API is disabled for this producer agent",
                status_code=400,
            )

        subject = session.get(User, data.subject_user_id)
        if subject is None:
            raise AgentApiKeyError("Subject user not found", status_code=404)

        label = data.label or f"{agent.name} API key"
        expires_at: datetime | None = None
        if data.expires_in_days is not None:
            expires_at = datetime.now(UTC) + timedelta(days=data.expires_in_days)

        base_url = AgentApiTokenService.build_base_url(producer_agent_id)
        spec_url = AgentApiTokenService.build_spec_url(producer_agent_id)

        # Scopes live on the grant, never on the key (plan D5). Upsert, because
        # the subject may already have a grant from the Access & Scopes card.
        # Done BEFORE minting so a grant failure aborts with nothing issued; the
        # reverse order would leave a live key whose scopes silently never
        # landed. A grant left behind by a later mint failure is harmless — the
        # two lifecycles are independent by design.
        if data.scopes is not None:
            await AgentApiGrantService.upsert_grant(
                session,
                producer_agent_id=producer_agent_id,
                owner_user_id=owner_user_id,
                subject_user_id=subject.id,
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
            subject_user_id=subject.id,
            expires_at=expires_at,
            is_active=True,
        )
        session.add(token)
        session.commit()
        session.refresh(token)
        # Held separately: after a rollback below the ORM object may be expired,
        # and re-reading an attribute would itself hit the DB.
        token_id = token.id

        # The key IS a credential (plan D4): it rides the existing detail page,
        # reveal path, and cascade-on-delete revocation. Sharing is forced OFF —
        # sharing an identity-bound key means "here, act as user X".
        #
        # Workspace: no consumer agent exists for a key, so fall back to the
        # producer's workspace (the existing consumer-first rule's tail) — but
        # ONLY when the credential's owner is the agent's owner. A superuser
        # minting on someone else's producer would otherwise stamp their own
        # credential with a workspace belonging to a different account.
        from app.services.credentials.credentials_service import CredentialsService

        workspace_id = (
            agent.user_workspace_id if agent.owner_id == owner_user_id else None
        )
        try:
            credential = CredentialsService.create_credential(
                session,
                CredentialCreate(
                    name=label,
                    type=CredentialType.AGENT_API,
                    notes=(
                        f"External API key for agent {agent.name} "
                        f"(ID: {producer_agent_id}), acting as {subject.email}"
                    ),
                    allow_sharing=False,
                    user_workspace_id=workspace_id,
                    credential_data={
                        "base_url": base_url,
                        "spec_url": spec_url,
                        "token": token_value,
                        "label": label,
                        "producer_agent_id": str(producer_agent_id),
                    },
                ),
                owner_id=owner_user_id,
            )
        except Exception:
            # Compensate: drop the token so no live, unrevocable key survives a
            # failed mint. The value was never returned to anyone.
            session.rollback()
            orphan = session.get(AgentApiToken, token_id)
            if orphan is not None:
                session.delete(orphan)
                session.commit()
            logger.exception(
                "Failed to create the credential for a new agent_api external "
                "key on agent %s; rolled the token back",
                producer_agent_id,
            )
            raise

        token.credential_id = credential.id
        session.add(token)
        session.commit()
        session.refresh(token)

        await AgentApiKeyService._audit(
            session,
            actor_id=owner_user_id,
            event_type=AGENT_API_EXTERNAL_KEY_CREATED,
            agent_id=producer_agent_id,
            token=token,
        )
        logger.info(
            "Minted agent_api external key %s on agent %s for user %s",
            token.id,
            producer_agent_id,
            subject.id,
        )

        public = AgentApiKeyService._to_public(session, token)
        return AgentApiKeyCreated(
            **public.model_dump(),
            token=token_value,
            # ALWAYS the public URL. AGENT_ENV_BACKEND_URL is an env-sync-only
            # rewrite for containers; an external caller is not in the Docker
            # network and must get the address it can actually reach.
            base_url=base_url,
            spec_url=spec_url,
        )

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def list_keys(
        session: Session,
        producer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> list[AgentApiKeyPublic]:
        """List this producer's external keys (owner-gated). Never the value."""
        agent = AgentApiKeyService._require_owner(
            session, producer_agent_id, user_id, is_superuser
        )
        tokens = session.exec(
            select(AgentApiToken)
            .where(
                AgentApiToken.agent_id == producer_agent_id,
                AgentApiToken.kind == AgentApiTokenKind.EXTERNAL.value,
            )
            .order_by(AgentApiToken.created_at.desc())
        ).all()
        # Resolve every subject in ONE query — a key list is a per-user list, so
        # projecting row-by-row would be a guaranteed N+1.
        subjects = AgentApiKeyService._load_subjects(
            session, [t.subject_user_id for t in tokens]
        )
        return [
            AgentApiKeyService._to_public(
                session,
                t,
                subjects=subjects,
                external_access_enabled=agent.agent_api_external_access_enabled,
            )
            for t in tokens
        ]

    # ------------------------------------------------------------------ #
    # Revoke                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def revoke_key(
        session: Session,
        producer_agent_id: uuid.UUID,
        key_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> None:
        """Revoke a key immediately (owner-gated).

        Deletes the bound credential, which cascade-deletes the token — the same
        single revocation path a disconnect uses. Falls back to deleting an
        unbound token directly.

        Revocation is UNCONDITIONAL. It passes ``force=True`` past the
        blast-radius gate: that gate exists to stop an owner casually breaking
        other people's bundle installs, but a leaked bearer key must be killable
        instantly and must never 409/500 because someone published an agent that
        links it.

        The ``(producer, subject)`` grant is deliberately left alone (plan D5):
        that user may still reach this producer through their own agents, and
        capability and secret have independent lifecycles.
        """
        from app.services.credentials.credentials_service import (
            CredentialInUseError,
            CredentialsService,
        )

        AgentApiKeyService._require_owner(
            session, producer_agent_id, user_id, is_superuser
        )
        token = AgentApiKeyService._load_owned_key(session, producer_agent_id, key_id)

        # Capture the audit facts before the row is gone.
        subject_user_id = token.subject_user_id
        token_prefix = token.token_prefix
        credential_id = token.credential_id

        if credential_id is not None:
            # Delete AS THE CREDENTIAL'S OWNER. `delete_credential` compares
            # `credential.owner_id` to `owner_id` and does not honour its own
            # `is_superuser` flag, so a superuser revoking a key minted by
            # another owner would otherwise be refused and strand the
            # credential. Authorization for this call was already settled by the
            # producer-ownership gate above.
            credential = session.get(Credential, credential_id)
            try:
                await CredentialsService.delete_credential(
                    session,
                    credential_id=credential_id,
                    owner_id=(
                        credential.owner_id if credential is not None else user_id
                    ),
                    is_superuser=is_superuser,
                    force=True,
                )
                token = None
            except (ValueError, CredentialInUseError):
                # Credential already gone (or unexpectedly unowned) — log it and
                # fall through so the key still disappears. Never silent: a
                # stranded credential would keep listing a dead key.
                logger.exception(
                    "Could not delete the credential %s behind external key %s; "
                    "dropping the token directly",
                    credential_id,
                    key_id,
                )
                session.expire_all()
                token = session.get(AgentApiToken, key_id)

        if token is not None:
            session.delete(token)
            session.commit()

        await AgentApiKeyService._audit_raw(
            session,
            actor_id=user_id,
            event_type=AGENT_API_EXTERNAL_KEY_REVOKED,
            agent_id=producer_agent_id,
            key_id=key_id,
            subject_user_id=subject_user_id,
            token_prefix=token_prefix,
        )
        logger.info(
            "Revoked agent_api external key %s on agent %s", key_id, producer_agent_id
        )

    # ------------------------------------------------------------------ #
    # Reveal                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def reveal_external_key(
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> str:
        """Return an external key's value and audit the reveal (plan D4).

        The **only** path that hands a key back after mint. It is deliberately
        NOT ``GET /credentials/{id}/with-data``: that route fires on every
        detail-page open, so auditing there would record "page opened" rather
        than "secret revealed" — and ``with-data`` strips ``token`` for external
        keys precisely so this endpoint is the single audited door.

        **Owner only — no superuser bypass, deliberately.** The consistency axis
        is not ``revoke_key`` (which does honour ``is_superuser``) but
        ``CredentialsService.get_credential_with_data``, the hard-owner-only
        endpoint this replaces: honouring superuser here would silently *widen*
        who can read another user's secret as a by-product of a refactor. Revoke
        and reveal are different acts — revoke is containment, which a platform
        admin plausibly needs; reveal is disclosure, which they do not. An admin
        who suspects a key is compromised should kill it, not read it. Do not
        "make this consistent with revoke" — the asymmetry is the point, and
        ``test_reveal_not_readable_by_non_owner_superuser`` pins it.

        404 for "no such credential", "not yours", *and* "you are an admin but
        not the owner" — no existence leak. 400 when the credential is real and
        reachable but is not an external key (an ``agent_api`` *connection*,
        whose token is machine-only, or any other type).

        The SecurityEvent write stays non-raising (``_audit``), like every other
        key event — but unlike the old best-effort hook, the returned value is
        the point of the call, so failures to resolve it are errors, not no-ops.
        """
        credential = session.get(Credential, credential_id)
        if credential is None or credential.owner_id != user_id:
            raise AgentApiKeyError("Credential not found", status_code=404)

        if credential.type != CredentialType.AGENT_API:
            raise AgentApiKeyError(
                "This credential is not an agent API key", status_code=400
            )

        token = session.exec(
            select(AgentApiToken).where(
                AgentApiToken.credential_id == credential_id,
                AgentApiToken.kind == AgentApiTokenKind.EXTERNAL.value,
            )
        ).first()
        if token is None:
            raise AgentApiKeyError(
                "This credential is not an agent API key", status_code=400
            )

        from app.services.credentials.credentials_service import CredentialsService

        data = CredentialsService.decrypt_credential_data(
            session=session, credential=credential
        )
        token_value = data.get("token")
        if not token_value:
            # A bound key with no stored value cannot be revealed or rotated —
            # the holder's copy is the only one left. Say so instead of handing
            # back an empty string the UI would happily display.
            raise AgentApiKeyError(
                "This key's value is no longer stored and cannot be revealed. "
                "Revoke it and issue a new one.",
                status_code=400,
            )

        await AgentApiKeyService._audit(
            session,
            actor_id=user_id,
            event_type=AGENT_API_EXTERNAL_KEY_REVEALED,
            agent_id=token.agent_id,
            token=token,
        )
        logger.info(
            "Revealed agent_api external key %s (credential %s) to user %s",
            token.id,
            credential_id,
            user_id,
        )
        return token_value

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

        KNOWN GAP: an *orphaned key* credential lands in this same state and so
        keeps returning its stored value through ``with-data`` unaudited —
        ``subject_user_id`` is ``ON DELETE CASCADE``, so deleting the subject
        user drops the token row while the credential survives. The exposure is
        bounded to a **dead** string: with no token row, ``validate_token``
        matches nothing and the value 401s at the proxy (pinned by
        ``test_orphaned_key_no_longer_authenticates``). Closing it properly
        means removing the credential when the token cascades, rather than
        widening this predicate.
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
            raise AgentApiKeyError("Key not found", status_code=404)
        return token

    @staticmethod
    def _load_subjects(
        session: Session, subject_ids: list[uuid.UUID | None]
    ) -> dict[uuid.UUID, AgentApiKeySubject]:
        """Batch-resolve subject users for a page of keys."""
        wanted = {sid for sid in subject_ids if sid is not None}
        if not wanted:
            return {}
        users = session.exec(select(User).where(User.id.in_(wanted))).all()
        return {
            user.id: AgentApiKeySubject(
                id=user.id, email=user.email, full_name=user.full_name
            )
            for user in users
        }

    @staticmethod
    def _to_public(
        session: Session,
        token: AgentApiToken,
        subjects: dict[uuid.UUID, AgentApiKeySubject] | None = None,
        external_access_enabled: bool = True,
    ) -> AgentApiKeyPublic:
        """Project a key row for the API. Never carries the token value.

        ``subjects`` is the batched lookup used by ``list_keys``; the single-row
        callers (mint) let it resolve on demand.

        ``external_access_enabled`` folds the producer's kill switch into
        ``is_usable`` so the list never claims a key works while the proxy is
        rejecting every one of them.
        """
        subject: AgentApiKeySubject | None = None
        if token.subject_user_id is not None:
            if subjects is not None:
                subject = subjects.get(token.subject_user_id)
            else:
                subject = AgentApiKeyService._load_subjects(
                    session, [token.subject_user_id]
                ).get(token.subject_user_id)
        return AgentApiKeyPublic(
            id=token.id,
            credential_id=token.credential_id,
            agent_id=token.agent_id,
            label=token.label,
            token_prefix=token.token_prefix,
            subject=subject,
            read_only=token.read_only_override,
            is_active=token.is_active,
            is_usable=token.is_active
            and external_access_enabled
            and not AgentApiTokenService.is_expired(token),
            expires_at=token.expires_at,
            last_used_at=token.last_used_at,
            created_at=token.created_at,
        )

    # ------------------------------------------------------------------ #
    # Audit                                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _audit(
        session: Session,
        actor_id: uuid.UUID,
        event_type: str,
        agent_id: uuid.UUID,
        token: AgentApiToken,
    ) -> None:
        await AgentApiKeyService._audit_raw(
            session,
            actor_id=actor_id,
            event_type=event_type,
            agent_id=agent_id,
            key_id=token.id,
            subject_user_id=token.subject_user_id,
            token_prefix=token.token_prefix,
        )

    @staticmethod
    async def _audit_raw(
        session: Session,
        actor_id: uuid.UUID,
        event_type: str,
        agent_id: uuid.UUID,
        key_id: uuid.UUID,
        subject_user_id: uuid.UUID | None,
        token_prefix: str,
    ) -> None:
        """Write a SecurityEvent for a key lifecycle change. Never raises.

        Records the 8-char display prefix only — the token value and its hash are
        never logged.
        """
        try:
            await SecurityEventService.create_event(
                session=session,
                user_id=actor_id,
                data=SecurityEventCreate(
                    agent_id=agent_id,
                    event_type=event_type,
                    severity="high",
                    details={
                        "key_id": str(key_id),
                        "subject_user_id": (
                            str(subject_user_id) if subject_user_id else None
                        ),
                        "token_prefix": token_prefix,
                    },
                ),
            )
        except Exception:
            logger.exception(
                "Failed to write SecurityEvent %s for agent_api external key %s",
                event_type,
                key_id,
            )
