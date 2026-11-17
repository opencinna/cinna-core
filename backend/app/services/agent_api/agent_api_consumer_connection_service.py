"""
Agent-API connections seen from the consumer side, for the account CLI.

A builder working from the account workspace needs what the consumer agent's
env has: which ``agent_api`` credentials are linked, the URLs the env calls,
whether each one works, and a way to read the producer's spec and file
capability requests with the linked connection — without ever seeing the
token. Everything here is keyed by a consumer agent the caller owns.

- ``list_connections`` (C2): one row per linked ``agent_api`` credential,
  shaped exactly as env prep delivers it (``_shape_agent_api_keys_for_env`` +
  ``agent_api_env_urls``), so the CLI and the env never disagree.
- ``resolve_connection``: one usable connection, for C3/C4.
- ``get_spec`` (C3), ``file_capability_request`` / ``list_capability_requests``
  (C4): the env's ``_cinna`` and spec routes, run server-side with the linked
  connection. A filed request is indistinguishable from an env-filed one.

Errors are ``AgentApiError`` with a ``code`` (the account CLI's coded refusal).
Every 404 is ``not_found``; the message names what is missing ("Agent not
found" vs "No agent API connection <id> is linked to this agent"), but a
foreign agent and a missing one read the same, so ownership never leaks. The
token value never leaves this module: it
lives only on ``ResolvedConnection`` (excluded from its ``repr``) to feed the
secret scrubber, and is never returned, logged or put on an exception.
"""
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlmodel import Session, col, select

from app.models import Agent, AgentApiToken, AgentApiTokenKind, Credential, User
from app.models.agent_api.agent_api_token import AgentApiKeyStatus
from app.models.cli.account_convenience import (
    AccountAgentApiConnection,
    AccountAgentApiConnectionsPublic,
)
from app.models.credentials.credential import CredentialType
from app.models.improvement.agent_improvement_request import (
    AgentApiCapabilityRequestCreate,
    AgentApiCapabilityRequestPublic,
    AgentApiCapabilityRequestsPublic,
)
from app.services.agent_api.agent_api_grant_service import AgentApiGrantService
from app.services.agent_api.agent_api_key_status import (
    connection_status,
    key_states_by_credential,
    unavailable_reason_for,
)
from app.services.agent_api.agent_api_service import (
    AgentApiError,
    AgentApiPolicyError,
    AgentApiService,
)
from app.services.credentials.credentials_service import CredentialsService
from app.services.improvement.improvement_request_service import (
    ImprovementRequestService,
)

NOT_FOUND_CODE = "not_found"
CONNECTION_UNUSABLE_CODE = "connection_unusable"

# C3: codes for spec refusals the agent-api service raises without one.
_SPEC_ERROR_CODES: dict[int, str] = {
    400: "api_disabled",
    403: "spec_not_exposed",
    502: "producer_error",
    503: "env_unavailable",
}

_UNUSABLE_PLACEHOLDER = "placeholder"
_UNUSABLE_NOT_DELIVERED = "not_delivered"


def _agent_not_found() -> AgentApiError:
    return AgentApiError("Agent not found", status_code=404, code=NOT_FOUND_CODE)


def _connection_not_linked(credential_id: uuid.UUID) -> AgentApiError:
    return AgentApiError(
        f"No agent API connection {credential_id} is linked to this agent",
        status_code=404,
        code=NOT_FOUND_CODE,
    )


def _connection_unusable(reason: str | None) -> AgentApiError:
    return AgentApiError(
        f"This connection is not usable: {reason or 'unavailable'}",
        status_code=409,
        code=CONNECTION_UNUSABLE_CODE,
    )


@dataclass(frozen=True)
class ResolvedConnection:
    """A usable connection of a consumer agent the caller owns."""

    consumer: Agent
    credential: Credential
    token: AgentApiToken
    producer: Agent
    row: AccountAgentApiConnection
    # Only for the secret scrubber when filing; never returned or logged.
    token_value: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _ConnectionEntry:
    """A C2 row plus the objects it was computed from (in-process only)."""

    credential: Credential
    token: AgentApiToken | None
    producer: Agent | None
    row: AccountAgentApiConnection
    token_value: str | None = field(default=None, repr=False)


class AgentApiConsumerConnectionService:
    """Consumer-side agent-API connections of an agent the caller owns."""

    @staticmethod
    def _owned_consumer(db: Session, user: User, agent_id: uuid.UUID) -> Agent:
        """The consumer agent, strictly owned by the caller (no superuser bypass:
        the env's identity is the agent owner)."""
        agent = db.get(Agent, agent_id)
        if agent is None or agent.owner_id != user.id:
            raise _agent_not_found()
        return agent

    @staticmethod
    def _entries(db: Session, user: User, agent: Agent) -> list[_ConnectionEntry]:
        """Every ``agent_api`` credential linked to ``agent``, as its env sees it."""
        all_linked = CredentialsService.get_agent_credentials(db, agent.id)
        linked = sorted(
            (
                credential
                for credential in all_linked
                if credential.type == CredentialType.AGENT_API
            ),
            key=lambda c: (c.name.lower(), str(c.id)),
        )
        if not linked:
            return []

        # Decrypted server-side; values stay in-process.
        env_entries = [
            CredentialsService.credential_to_env_dict(
                credential,
                CredentialsService.decrypt_credential_data(db, credential),
            )
            for credential in linked
        ]
        stored_by_id = {entry["id"]: entry for entry in env_entries}
        shaped_by_id = {
            entry["id"]: entry
            for entry in CredentialsService._shape_agent_api_keys_for_env(
                db,
                env_entries,
                env_owner_id=agent.owner_id,
                for_desktop=False,
            )
        }

        credential_ids = [credential.id for credential in linked]
        connection_tokens: dict[uuid.UUID, tuple[AgentApiToken, Agent | None]] = {}
        token_rows = db.exec(
            select(AgentApiToken, Agent)
            .outerjoin(Agent, col(Agent.id) == col(AgentApiToken.agent_id))
            .where(
                col(AgentApiToken.credential_id).in_(credential_ids),
                AgentApiToken.kind == AgentApiTokenKind.CONNECTION.value,
            )
        ).all()
        for token, producer in token_rows:
            current = connection_tokens.get(token.credential_id)
            # Prefer a live token should a credential ever carry two.
            if current is None or (current[0].revoked_at and not token.revoked_at):
                connection_tokens[token.credential_id] = (token, producer)
        key_states = key_states_by_credential(db, credential_ids)
        ambiguous_slots = _ambiguous_slots(all_linked)

        owner_ids = {c.owner_id for c in linked if c.owner_id != user.id}
        owner_emails: dict[uuid.UUID, str] = (
            dict(
                db.exec(
                    select(User.id, User.email).where(col(User.id).in_(owner_ids))
                ).all()
            )
            if owner_ids
            else {}
        )

        return [
            AgentApiConsumerConnectionService._entry(
                db,
                user,
                agent,
                credential,
                stored=stored_by_id[str(credential.id)],
                shaped=shaped_by_id.get(str(credential.id)),
                connection=connection_tokens.get(credential.id),
                key_state=key_states.get(credential.id),
                owner_email=owner_emails.get(credential.owner_id),
                ambiguous_slots=ambiguous_slots,
            )
            for credential in linked
        ]

    @staticmethod
    def _entry(
        db: Session,
        user: User,
        agent: Agent,
        credential: Credential,
        *,
        stored: dict,
        shaped: dict | None,
        connection: tuple[AgentApiToken, Agent | None] | None,
        key_state,
        owner_email: str | None,
        ambiguous_slots: set[str],
    ) -> _ConnectionEntry:
        """Project one linked credential (first matching status rule wins)."""
        token: AgentApiToken | None = None
        producer: Agent | None = None
        key_status: str | None = None
        usable = False
        reason: str | None = None

        if key_state is not None:
            token, producer = key_state.token, key_state.agent
        elif connection is not None:
            token, producer = connection

        if credential.is_placeholder:
            reason = _UNUSABLE_PLACEHOLDER
        elif shaped is None:
            reason = _UNUSABLE_NOT_DELIVERED
        else:
            status = (
                key_state.status
                if key_state is not None
                else connection_status(token, producer)
                if token is not None
                else AgentApiKeyStatus.PRODUCER_REMOVED
            )
            key_status = status.value
            usable = status == AgentApiKeyStatus.ACTIVE
            reason = unavailable_reason_for(status)

        source = shaped if shaped is not None else stored
        data = source.get("credential_data") or {}
        urls = CredentialsService.agent_api_env_urls(
            {name: data[name] for name in ("base_url", "spec_url") if name in data},
            rewrite_origin=True,
            usable=usable,
        )

        producer_agent_id = token.agent_id if token is not None else None
        if producer_agent_id is None:
            producer_agent_id = _as_uuid(
                (stored.get("credential_data") or {}).get("producer_agent_id")
            )

        scopes: list[str] = []
        if usable and producer is not None and AgentApiService.resolve_identity_enabled(
            producer, token
        ):
            scopes = AgentApiGrantService.resolve_scopes_for_caller(
                db, producer.id, agent.owner_id
            )

        is_shared = credential.owner_id != user.id
        slot = credential.service_uri or None
        row = AccountAgentApiConnection(
            credential_id=credential.id,
            name=credential.name,
            slot=slot,
            is_shared=is_shared,
            owner_email=(owner_email or "") if is_shared else None,
            producer_agent_id=producer_agent_id,
            producer_agent_name=producer.name if producer is not None else None,
            base_url=urls.get("base_url"),
            spec_url=urls.get("spec_url"),
            improvement_requests_url=urls.get("improvement_requests_url"),
            key_status=key_status,
            scopes=list(scopes),
            usable=usable,
            unavailable_reason=None if usable else reason,
            sdk_lookup=(
                slot if slot and slot not in ambiguous_slots else str(credential.id)
            ),
        )
        token_value = (stored.get("credential_data") or {}).get("token")
        return _ConnectionEntry(
            credential=credential,
            token=token,
            producer=producer,
            row=row,
            token_value=token_value if isinstance(token_value, str) else None,
        )

    @staticmethod
    def list_connections(
        db: Session, user: User, agent_id: uuid.UUID
    ) -> AccountAgentApiConnectionsPublic:
        """C2: the ``agent_api`` credentials linked to a consumer agent."""
        agent = AgentApiConsumerConnectionService._owned_consumer(db, user, agent_id)
        rows = [
            entry.row
            for entry in AgentApiConsumerConnectionService._entries(db, user, agent)
        ]
        return AccountAgentApiConnectionsPublic(data=rows, count=len(rows))

    @staticmethod
    def _linked_entry(
        db: Session, user: User, agent_id: uuid.UUID, credential_id: uuid.UUID
    ) -> tuple[Agent, _ConnectionEntry]:
        """The consumer agent and one of its linked connections, usable or not.

        404 ``not_found``: "Agent not found" unless the caller owns the agent,
        then "No agent API connection ... is linked" unless the credential is.
        """
        agent = AgentApiConsumerConnectionService._owned_consumer(db, user, agent_id)
        entry = next(
            (
                e
                for e in AgentApiConsumerConnectionService._entries(db, user, agent)
                if e.credential.id == credential_id
            ),
            None,
        )
        if entry is None:
            raise _connection_not_linked(credential_id)
        return agent, entry

    @staticmethod
    def resolve_connection(
        db: Session, user: User, agent_id: uuid.UUID, credential_id: uuid.UUID
    ) -> ResolvedConnection:
        """One usable connection: 404 ``not_found`` unless linked to an agent the
        caller owns; 409 ``connection_unusable`` when it would not work in the env.
        """
        agent, entry = AgentApiConsumerConnectionService._linked_entry(
            db, user, agent_id, credential_id
        )
        if not entry.row.usable or entry.token is None or entry.producer is None:
            raise _connection_unusable(entry.row.unavailable_reason)
        return ResolvedConnection(
            consumer=agent,
            credential=entry.credential,
            token=entry.token,
            producer=entry.producer,
            row=entry.row,
            token_value=entry.token_value,
        )

    @staticmethod
    async def get_spec(
        db: Session, user: User, agent_id: uuid.UUID, credential_id: uuid.UUID
    ) -> dict:
        """C3: the producer's spec, as the consumer's ``spec_url`` serves it.

        Same path as the token-authenticated spec route after validation
        (wake the producer env, honour ``expose_spec``); the token's
        ``last_used_at`` is not bumped (no token is presented).
        """
        resolved = AgentApiConsumerConnectionService.resolve_connection(
            db, user, agent_id, credential_id
        )
        try:
            environment = await AgentApiService.resolve_running_producer_env(
                db, resolved.producer
            )
            policy = await AgentApiService.load_policy(db, environment)
            if not policy.get("expose_spec", True):
                raise AgentApiPolicyError("Spec is not exposed by this API", status_code=403)
            spec = await AgentApiService.get_spec(db, environment)
        except AgentApiError as e:
            if e.code is None and e.status_code in _SPEC_ERROR_CODES:
                e.code = _SPEC_ERROR_CODES[e.status_code]
            raise
        AgentApiService.update_last_activity(db, environment)
        return spec

    @staticmethod
    async def file_capability_request(
        db: Session,
        user: User,
        resolved: ResolvedConnection,
        body: AgentApiCapabilityRequestCreate,
    ) -> AgentApiCapabilityRequestPublic:
        """C4: file a capability request with the linked connection.

        Same service call as the env's ``_cinna`` route, so attribution, rate
        limits (per requester) and scrubbing are shared. Raises
        ``ImprovementRequestDenied`` for the route to map.
        """
        row = await ImprovementRequestService.create_from_agent_api(
            db,
            producer=resolved.producer,
            token=resolved.token,
            caller=user,
            consumer_agent_id=resolved.consumer.id,
            body=body,
            raw_secrets={resolved.token_value} - {None},
        )
        return ImprovementRequestService.to_capability_public(row)

    @staticmethod
    def list_capability_requests(
        db: Session, user: User, agent_id: uuid.UUID, credential_id: uuid.UUID
    ) -> AgentApiCapabilityRequestsPublic:
        """C4: the caller's own capability requests on the connection's producer.

        Works on an unusable connection too (revoked / expired key, API
        disabled, placeholder) so requests filed earlier stay visible; only a
        connection whose producer agent is gone (or unknown) answers 409
        ``connection_unusable``.
        """
        _, entry = AgentApiConsumerConnectionService._linked_entry(
            db, user, agent_id, credential_id
        )
        producer = entry.producer
        if producer is None and entry.row.producer_agent_id is not None:
            producer = db.get(Agent, entry.row.producer_agent_id)
        if producer is None:
            raise _connection_unusable(entry.row.unavailable_reason)
        rows, count = ImprovementRequestService.list_for_requester_on_target(
            db, user.id, producer.id
        )
        return AgentApiCapabilityRequestsPublic(
            data=[ImprovementRequestService.to_capability_public(r) for r in rows],
            count=count,
        )


def _ambiguous_slots(credentials: list[Credential]) -> set[str]:
    """Slots (``service_uri``) carried by more than one linked credential.

    The env SDK's ``_find_slot`` returns the first match for a slot across every
    linked credential, so a shared slot (e.g. the user's own connection and one
    shared with them) cannot pick a specific row; those rows are reached by
    credential id instead.
    """
    counts = Counter(c.service_uri for c in credentials if c.service_uri)
    return {slot for slot, count in counts.items() if count > 1}


def _as_uuid(value: object) -> uuid.UUID | None:
    """Parse a stored id; None when absent or malformed."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None
