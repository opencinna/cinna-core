"""
Agent REST API token service.

Mints (internally) and validates scoped opaque tokens that authenticate consumer
calls to a producer agent's REST API. Tokens are never created manually: each one
is minted by the "Connect Agent API" helper and bound to the resulting
``agent_api`` credential. Disconnecting = deleting that credential, which
cascade-deletes the token (the only revocation path).

Security model (mirrors AccessTokenService + the webapp share token):
- The token value is a ``secrets.token_urlsafe`` string surfaced **once** inside
  the credential it is minted into; only its SHA256 hash is stored, plus an
  8-char prefix for display.
- Validation is a hash lookup; active check; expiry check; ``last_used_at`` is
  bumped on use. Connection tokens are internal machine credentials and never
  expire; external keys (``kind="external"``, minted by ``AgentApiKeyService``)
  may carry an ``expires_at``. ``validate_token`` is the ONE validation path for
  both kinds.
- Ownership: the caller must own the producer agent to connect. Lookups that
  cannot find / are not owned return 404 (not 403) to avoid leaking existence.
- ``read_only_override`` may only NARROW the producer's policy, never widen it
  (enforced at the proxy edge in AgentApiService).
"""
import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    Agent,
    AgentApiConnectedAgent,
    AgentApiConnectionInfo,
    AgentApiProducerConnection,
    AgentApiToken,
    AgentApiTokenCreate,
    AgentApiTokenCreated,
    AgentApiTokenKind,
    AgentApiTokenPublic,
    ConnectAgentApiRequest,
    ConnectAgentApiResponse,
    CredentialCreate,
)
from app.models.credentials.credential import Credential, CredentialType
from app.models.users.user import User

logger = logging.getLogger(__name__)


class AgentApiTokenError(Exception):
    """Base exception for agent API token errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class AgentApiTokenNotFoundError(AgentApiTokenError):
    def __init__(self, message: str = "Token not found"):
        super().__init__(message, status_code=404)


@dataclass
class ConnectedProducerRow:
    """Internal projection of one producer the consumer install connects to.

    Carries only the non-secret annotations the bundle-permissions overview
    needs. ``can_manage`` is the single authority that decides whether the
    owner-gated grant / scope-catalog reads run for this producer.
    """

    producer_agent_id: uuid.UUID
    producer_agent_name: str | None
    producer_ui_color_preset: str | None
    credential_id: uuid.UUID
    credential_name: str | None
    identity_enabled: bool
    can_manage: bool
    owner_email: str | None = None


class AgentApiTokenService:
    """Service for managing agent REST API tokens."""

    # ------------------------------------------------------------------ #
    # URL helpers                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_base_url(agent_id: uuid.UUID) -> str:
        """Absolute consumer-facing base URL for the producer's proxy."""
        host = settings.FRONTEND_HOST.rstrip("/")
        return f"{host}{settings.API_V1_STR}/agent-api/{agent_id}"

    @staticmethod
    def build_spec_url(agent_id: uuid.UUID) -> str:
        """Absolute consumer-facing OpenAPI spec URL."""
        return f"{AgentApiTokenService.build_base_url(agent_id)}/openapi.json"

    # ------------------------------------------------------------------ #
    # Hashing                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def hash_token(token: str) -> str:
        """SHA256 hex digest of the opaque token value.

        Public because external-key minting (``AgentApiKeyService``) must produce
        hashes that ``validate_token`` below can look up — one hashing rule for
        both token kinds.
        """
        return hashlib.sha256(token.encode()).hexdigest()

    # ------------------------------------------------------------------ #
    # Ownership                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _verify_agent_ownership(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> Agent:
        """Return the agent if owned by the user, else raise 404 (no existence leak)."""
        agent = session.get(Agent, agent_id)
        if not agent:
            raise AgentApiTokenNotFoundError("Agent not found")
        if agent.owner_id != user_id and not is_superuser:
            raise AgentApiTokenNotFoundError("Agent not found")
        return agent

    @staticmethod
    def _build_connected_agent(
        session: Session, agent: Agent
    ) -> AgentApiConnectedAgent:
        """
        Build the consumer-agent projection, resolving the owner's email so the
        UI can disambiguate identical agent names (e.g. several bundle installs
        of the same agent owned by different users).
        """
        owner = session.get(User, agent.owner_id)
        return AgentApiConnectedAgent(
            id=agent.id,
            name=agent.name,
            ui_color_preset=agent.ui_color_preset,
            owner_name=owner.full_name if owner is not None else None,
            owner_email=owner.email if owner is not None else None,
        )

    # ------------------------------------------------------------------ #
    # CRUD                                                                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def create_token(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        data: AgentApiTokenCreate,
        is_superuser: bool = False,
    ) -> AgentApiTokenCreated:
        """
        Mint a new opaque token for the producer agent.

        Returns the token value (once), plus base_url + spec_url so the owner can
        build an ``agent_api`` credential.
        """
        AgentApiTokenService._verify_agent_ownership(
            session, agent_id, user_id, is_superuser
        )

        token_value = secrets.token_urlsafe(32)
        token_hash = AgentApiTokenService.hash_token(token_value)
        token_prefix = token_value[:8]

        token = AgentApiToken(
            agent_id=agent_id,
            owner_id=user_id,
            token_hash=token_hash,
            token_prefix=token_prefix,
            label=data.label,
            read_only_override=data.read_only_override,
            is_active=True,
        )
        session.add(token)
        session.commit()
        session.refresh(token)

        logger.info("Created agent_api token %s for agent %s", token.id, agent_id)

        return AgentApiTokenCreated(
            **AgentApiTokenService._to_public(token).model_dump(),
            token=token_value,
            base_url=AgentApiTokenService.build_base_url(agent_id),
            spec_url=AgentApiTokenService.build_spec_url(agent_id),
        )

    @staticmethod
    async def connect_agent_api(
        session: Session,
        producer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        data: ConnectAgentApiRequest,
        is_superuser: bool = False,
    ) -> ConnectAgentApiResponse:
        """
        One-click "Connect to another agent" helper (plan §6.3).

        Collapses steps 3–5 of the publish→consume flow into one action:
          1. Mint an ``agent_api`` token on the producer agent.
          2. Create an ``agent_api`` credential owned by the caller, pre-filled
             with {base_url, token, spec_url, label, producer_agent_id}.
          3. Optionally link that credential to a chosen consumer agent (which
             syncs it into the consumer's running containers).

        Access: the caller must own the producer agent (or be superuser) — minting
        a token requires producer-side authority. Cross-user *consumption* is then
        achieved by sharing the resulting credential (CredentialShare), not by this
        helper.

        Note: the credential is created with ``allow_sharing=False`` by default;
        the owner enables sharing explicitly afterwards (same as any credential).
        """
        from app.services.credentials.credentials_service import CredentialsService

        agent = AgentApiTokenService._verify_agent_ownership(
            session, producer_agent_id, user_id, is_superuser
        )

        if not agent.agent_api_enabled:
            raise AgentApiTokenError(
                "Agent REST API is disabled for this producer agent", status_code=400
            )

        label = data.credential_label or f"{agent.name} API"

        # Derive the credential's workspace so it lands in the same workspace
        # group as the agent it belongs to (Automatic Credentials grouping).
        # Consumer-first: the credential is configured on and synced into the
        # consumer's containers, so the consumer's workspace is the strongest
        # ownership signal. Fall back to the producer's workspace when the
        # connect is made from the global picker (no consumer). NULL (default
        # workspace) when neither agent carries a workspace — unchanged behavior.
        #
        # The consumer agent is validated UP FRONT with the same authority
        # check used at link time. This both (a) avoids stamping the credential
        # with a workspace from an agent the caller doesn't own, and (b) fails
        # before any token/credential is minted, so a non-owned
        # ``consumer_agent_id`` can never leave an orphaned credential behind.
        workspace_id: uuid.UUID | None = agent.user_workspace_id
        if data.consumer_agent_id is not None:
            consumer_agent = session.get(Agent, data.consumer_agent_id)
            if consumer_agent is None:
                raise AgentApiTokenError(
                    "Consumer agent not found", status_code=404
                )
            if not is_superuser and consumer_agent.owner_id != user_id:
                raise AgentApiTokenError(
                    "You do not own the consumer agent", status_code=403
                )
            workspace_id = consumer_agent.user_workspace_id

        # 1. Mint the token (returns value once + base_url + spec_url).
        created = AgentApiTokenService.create_token(
            session,
            producer_agent_id,
            user_id,
            AgentApiTokenCreate(
                label=label,
                read_only_override=data.read_only_override,
            ),
            is_superuser=is_superuser,
        )

        # 2. Create the agent_api credential owned by the caller.
        credential = CredentialsService.create_credential(
            session,
            CredentialCreate(
                name=label,
                type=CredentialType.AGENT_API,
                notes=f"Proxy to agent {agent.name} (ID: {producer_agent_id}) REST API",
                allow_sharing=False,
                user_workspace_id=workspace_id,
                credential_data={
                    "base_url": created.base_url,
                    "spec_url": created.spec_url,
                    "token": created.token,
                    "label": label,
                    "producer_agent_id": str(producer_agent_id),
                },
            ),
            owner_id=user_id,
        )

        # 2b. Bind the token to the credential so deleting the credential
        #     (disconnecting) cascade-deletes the token — the only revoke path.
        token_row = session.get(AgentApiToken, created.id)
        if token_row is not None:
            token_row.credential_id = credential.id
            session.add(token_row)
            session.commit()

        # 3. Optionally link to the consumer agent (syncs into its containers).
        linked_consumer_agent_id: uuid.UUID | None = None
        if data.consumer_agent_id is not None:
            try:
                await CredentialsService.link_credential_to_agent(
                    session,
                    agent_id=data.consumer_agent_id,
                    credential_id=credential.id,
                    owner_id=user_id,
                    is_superuser=is_superuser,
                )
                linked_consumer_agent_id = data.consumer_agent_id
            except ValueError as e:
                # Credential is already created; surface the link failure clearly.
                raise AgentApiTokenError(
                    f"Token + credential created, but linking to the consumer agent "
                    f"failed: {e}",
                    status_code=400,
                )

        return ConnectAgentApiResponse(
            credential_id=credential.id,
            token_id=created.id,
            token_prefix=created.token_prefix,
            base_url=created.base_url,
            spec_url=created.spec_url,
            linked_consumer_agent_id=linked_consumer_agent_id,
        )

    @staticmethod
    def get_connection_info(
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> AgentApiConnectionInfo:
        """
        Describe what an ``agent_api`` credential connects to (for its detail
        view): the producer agent, the consumer agents it is linked to, and the
        spec/base URLs. Owner-only.
        """
        from app.services.credentials.credentials_service import CredentialsService

        credential = session.get(Credential, credential_id)
        if not credential or (
            credential.owner_id != user_id and not is_superuser
        ):
            raise AgentApiTokenNotFoundError("Credential not found")
        if credential.type != CredentialType.AGENT_API:
            raise AgentApiTokenError(
                "Credential is not an agent_api connection", status_code=400
            )

        data = CredentialsService.decrypt_credential_data(
            session=session, credential=credential
        )
        producer_agent_id_raw = data.get("producer_agent_id")
        producer_agent_id = (
            uuid.UUID(producer_agent_id_raw) if producer_agent_id_raw else None
        )

        producer_agent_name: str | None = None
        producer_ui_color_preset: str | None = None
        if producer_agent_id is not None:
            producer = session.get(Agent, producer_agent_id)
            if producer is not None:
                producer_agent_name = producer.name
                producer_ui_color_preset = producer.ui_color_preset

        # read_only is taken from the token bound to this credential.
        read_only = False
        token = session.exec(
            select(AgentApiToken).where(
                AgentApiToken.credential_id == credential_id
            )
        ).first()
        if token is not None:
            read_only = token.read_only_override

        consumer_agent_ids = CredentialsService.get_affected_agents(
            session, credential_id
        )
        consumer_agents: list[AgentApiConnectedAgent] = []
        for aid in consumer_agent_ids:
            agent = session.get(Agent, aid)
            if agent is not None:
                consumer_agents.append(
                    AgentApiTokenService._build_connected_agent(session, agent)
                )

        return AgentApiConnectionInfo(
            producer_agent_id=producer_agent_id,
            producer_agent_name=producer_agent_name,
            producer_ui_color_preset=producer_ui_color_preset,
            base_url=data.get("base_url", ""),
            spec_url=data.get("spec_url", ""),
            read_only=read_only,
            consumer_agents=consumer_agents,
        )

    @staticmethod
    def list_producer_connections(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> list[AgentApiProducerConnection]:
        """
        List the connections to a producer agent's API — i.e. who is consuming
        it. Each connection is one ``agent_api`` credential (minted by the
        connect helper for this producer) plus the consumer agents it is linked
        to. Surfaced on the producer's "Agent REST API" card. Owner-only.

        External keys are a separate product with their own list (see
        ``AgentApiKeyService.list_keys``) and are excluded here — the two
        surfaces stay disjoint.
        """
        from app.services.credentials.credentials_service import CredentialsService

        AgentApiTokenService._verify_agent_ownership(
            session, agent_id, user_id, is_superuser
        )

        tokens = session.exec(
            select(AgentApiToken)
            .where(
                AgentApiToken.agent_id == agent_id,
                AgentApiToken.kind == AgentApiTokenKind.CONNECTION.value,
            )
            .order_by(AgentApiToken.created_at.desc())
        ).all()

        connections: list[AgentApiProducerConnection] = []
        for token in tokens:
            credential_name: str | None = None
            consumer_agents: list[AgentApiConnectedAgent] = []
            if token.credential_id is not None:
                credential = session.get(Credential, token.credential_id)
                if credential is not None:
                    credential_name = credential.name
                for aid in CredentialsService.get_affected_agents(
                    session, token.credential_id
                ):
                    agent = session.get(Agent, aid)
                    if agent is not None:
                        consumer_agents.append(
                            AgentApiTokenService._build_connected_agent(
                                session, agent
                            )
                        )

            connections.append(
                AgentApiProducerConnection(
                    token_id=token.id,
                    credential_id=token.credential_id,
                    credential_name=credential_name,
                    token_prefix=token.token_prefix,
                    read_only=token.read_only_override,
                    consumer_agents=consumer_agents,
                    created_at=token.created_at,
                )
            )
        return connections

    @staticmethod
    def list_connected_producers(
        session: Session,
        consumer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> list[ConnectedProducerRow]:
        """Discover the identity-enabled producers this install consumes.

        The inverse direction of ``list_producer_connections`` (producer →
        consumers): here we walk the *consumer* install's linked ``agent_api``
        credentials to find the *producers* it calls, so the bundle-permissions
        overview can offer per-producer scope management.

        Read-only. Never mutates; never raises on a single bad credential (logs
        and skips). The credential ``token`` is **never** read — only
        ``producer_agent_id`` is decrypted out of the credential data.

        Returns only producers with ``agent_api_identity_enabled`` true (scope
        management is meaningless otherwise — a producer with identity OFF has
        no scope concept). An owner who wants scopes on a currently-OFF producer
        enables identity on that producer's own Integrations tab first.

        Deduped by ``producer_agent_id`` (the first linked credential wins;
        multiple credentials pointing at the same producer is unusual but
        possible).

        Connections only: an external key that an owner happened to link to one
        of their agents is not a connection and must not appear here, keeping
        this surface disjoint from the key list exactly like
        ``list_producer_connections``.
        """
        from app.services.credentials.credentials_service import CredentialsService

        credentials = CredentialsService.get_agent_credentials(
            session, consumer_agent_id
        )

        # One batched lookup for the whole link set (no per-credential query).
        kinds = AgentApiTokenService.get_kinds_by_credential(
            session,
            [c.id for c in credentials if c.type == CredentialType.AGENT_API],
        )

        rows: list[ConnectedProducerRow] = []
        seen_producers: set[uuid.UUID] = set()

        for credential in credentials:
            if credential.type != CredentialType.AGENT_API:
                continue
            if kinds.get(credential.id) != AgentApiTokenKind.CONNECTION.value:
                continue
            try:
                data = CredentialsService.decrypt_credential_data(
                    session=session, credential=credential
                )
            except Exception:
                logger.warning(
                    "Skipping agent_api credential %s: failed to decrypt data",
                    credential.id,
                )
                continue

            raw = data.get("producer_agent_id")
            if not raw:
                continue
            try:
                producer_agent_id = uuid.UUID(str(raw))
            except (ValueError, TypeError):
                logger.warning(
                    "Skipping agent_api credential %s: malformed producer_agent_id %r",
                    credential.id,
                    raw,
                )
                continue

            if producer_agent_id in seen_producers:
                continue

            producer = session.get(Agent, producer_agent_id)
            if producer is None:
                # Dangling connection — the producer agent was deleted.
                continue
            seen_producers.add(producer_agent_id)

            if not producer.agent_api_identity_enabled:
                # No scope concept — not surfaced.
                continue

            owner = session.get(User, producer.owner_id)
            rows.append(
                ConnectedProducerRow(
                    producer_agent_id=producer.id,
                    producer_agent_name=producer.name,
                    producer_ui_color_preset=producer.ui_color_preset,
                    credential_id=credential.id,
                    credential_name=credential.name,
                    identity_enabled=True,
                    can_manage=is_superuser or producer.owner_id == user_id,
                    owner_email=owner.email if owner is not None else None,
                )
            )
        return rows

    @staticmethod
    async def delete_producer_connection(
        session: Session,
        agent_id: uuid.UUID,
        token_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> None:
        """
        Disconnect one connection from the producer side: deletes the connection
        credential (which cascade-deletes the token) when present, else deletes
        the orphaned token directly. Owner-only; 404 if the token is unknown,
        not on this producer agent, or is an external key (those are revoked
        through their own route so the two surfaces stay disjoint).
        """
        from app.services.credentials.credentials_service import CredentialsService

        AgentApiTokenService._verify_agent_ownership(
            session, agent_id, user_id, is_superuser
        )

        token = session.get(AgentApiToken, token_id)
        if (
            not token
            or token.agent_id != agent_id
            or token.kind != AgentApiTokenKind.CONNECTION.value
        ):
            raise AgentApiTokenNotFoundError("Connection not found")

        if token.credential_id is not None:
            # Deleting the credential cascade-deletes the token and triggers the
            # standard credential-removed sync to any linked consumer envs.
            try:
                await CredentialsService.delete_credential(
                    session,
                    credential_id=token.credential_id,
                    owner_id=user_id,
                    is_superuser=is_superuser,
                )
                return
            except ValueError:
                # Credential already gone / not owned — fall through to drop the
                # now-orphaned token so the connection still disappears.
                session.expire_all()
                token = session.get(AgentApiToken, token_id)

        if token is not None:
            session.delete(token)
            session.commit()
        logger.info("Deleted agent_api connection %s (agent %s)", token_id, agent_id)

    # ------------------------------------------------------------------ #
    # Validation (consumer-facing)                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    def validate_token(
        session: Session,
        agent_id: uuid.UUID,
        token_value: str,
        external_access_enabled: bool = True,
    ) -> AgentApiToken | None:
        """
        Validate a presented token value for the given producer agent.

        Returns the active token row (and bumps ``last_used_at``), or None if
        invalid / revoked / wrong agent / expired / disabled by the producer's
        external-access kill switch. THE single validation path for both token
        kinds — every rejection reason lives here, so none of them can bump
        ``last_used_at`` or write to the DB on the way out.

        Connection tokens never expire (``expires_at`` is NULL). External keys
        may (plan D8): an expired key returns None so the caller raises the SAME
        401 as an invalid one — the distinction is deliberately not leaked.

        ``external_access_enabled`` is the producer's
        ``agent_api_external_access_enabled`` opt-in, passed in by the proxy
        (which has already loaded the Agent). It is the KILL SWITCH: off means
        every external key stops authenticating at once, while agent-to-agent
        connections keep working.
        """
        if not token_value:
            return None
        token_hash = AgentApiTokenService.hash_token(token_value)
        token = session.exec(
            select(AgentApiToken).where(
                AgentApiToken.token_hash == token_hash,
                AgentApiToken.agent_id == agent_id,
                AgentApiToken.is_active == True,  # noqa: E712
            )
        ).first()
        if not token:
            return None
        if AgentApiTokenService.is_expired(token):
            return None
        if (
            token.kind == AgentApiTokenKind.EXTERNAL.value
            and not external_access_enabled
        ):
            return None

        token.last_used_at = datetime.now(UTC)
        session.add(token)
        session.commit()
        session.refresh(token)
        return token

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def is_expired(token: AgentApiToken, now: datetime | None = None) -> bool:
        """Whether the token is past its ``expires_at`` (NULL = never expires).

        The column is TIMESTAMPTZ, but a value that round-trips through a driver
        or a test fixture can still arrive naive; those are read as UTC so the
        comparison never raises.
        """
        expires_at = token.expires_at
        if expires_at is None:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return (now or datetime.now(UTC)) >= expires_at

    @staticmethod
    def get_kinds_by_credential(
        session: Session,
        credential_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, str]:
        """Map ``credential_id -> AgentApiToken.kind`` for a page of credentials.

        Batched (one query) so the credentials list can classify ``agent_api``
        rows into "connection" vs "external" without an N+1. Credentials with no
        bound token are simply absent from the result; callers treat a missing
        entry as a connection (the legacy shape).
        """
        if not credential_ids:
            return {}
        rows = session.exec(
            select(AgentApiToken.credential_id, AgentApiToken.kind).where(
                AgentApiToken.credential_id.in_(credential_ids)
            )
        ).all()
        return {credential_id: kind for credential_id, kind in rows if credential_id}

    @staticmethod
    def get_kind_for_credential(
        session: Session, credential_id: uuid.UUID
    ) -> str | None:
        """``AgentApiToken.kind`` for one credential, or None when unbound."""
        return AgentApiTokenService.get_kinds_by_credential(
            session, [credential_id]
        ).get(credential_id)

    @staticmethod
    def is_restricted_agent_api_credential(
        session: Session, credential_id: uuid.UUID
    ) -> bool:
        """Whether this ``agent_api`` credential must NOT be shared or synced.

        The single predicate behind the two *security* rules for agent_api
        credentials (the cosmetic tab split is separate — see
        ``classify_credential_category``). Two cases, both fail-closed:

        - ``kind == "external"`` — an identity-bound key. Sharing it means "act
          as user X"; syncing it into a container bypasses the anonymous
          connection model (plan D4).
        - **No bound token row at all.** An ``agent_api`` credential whose token
          is gone is dead in either mode — the stored value authenticates
          nothing — and we can no longer tell which mode it was. Handing a dead
          secret to a container or another user has no upside, so an orphan is
          treated as restricted rather than as a connection.
        """
        return (
            AgentApiTokenService.get_kind_for_credential(session, credential_id)
            != AgentApiTokenKind.CONNECTION.value
        )

    @staticmethod
    def _to_public(token: AgentApiToken) -> AgentApiTokenPublic:
        return AgentApiTokenPublic(
            id=token.id,
            agent_id=token.agent_id,
            token_prefix=token.token_prefix,
            label=token.label,
            read_only_override=token.read_only_override,
            is_active=token.is_active,
            last_used_at=token.last_used_at,
            created_at=token.created_at,
            updated_at=token.updated_at,
        )
