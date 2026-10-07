"""
Issued-key status: the single place an issued key's status is computed.

Pure functions plus one batched read; nothing here writes to the DB. Every
consumer (issuer access list, recipient detail, credential projections, env
sync, Desktop) reads the status through ``key_status`` so the seven values of
``AgentApiKeyStatus`` mean the same thing everywhere (plan D4, D21).

Precedence, top-down (first match wins):
  revoked_by_issuer > producer_removed > issuer_removed > expired
  > api_disabled > external_access_paused > active
The first four are terminal; the last two recover without user action.
"""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.models import Agent, AgentApiToken, AgentApiTokenKind, Credential, User
from app.models.agent_api.agent_api_token import (
    AgentApiKeyStatus,
    AgentApiKeyUnavailableReason,
)
from app.models.credentials.credential import CredentialType

# How long the previous value of a rotated key keeps authenticating (plan D6,
# D15). A second rotate inside the window replaces the previous hash.
AGENT_API_KEY_ROTATION_GRACE = timedelta(minutes=5)

TERMINAL_KEY_STATUSES: frozenset[AgentApiKeyStatus] = frozenset(
    {
        AgentApiKeyStatus.REVOKED_BY_ISSUER,
        AgentApiKeyStatus.PRODUCER_REMOVED,
        AgentApiKeyStatus.ISSUER_REMOVED,
        AgentApiKeyStatus.EXPIRED,
    }
)

REVOKED_BY_ISSUER_REASON = AgentApiKeyStatus.REVOKED_BY_ISSUER.value


def _as_utc(value: datetime) -> datetime:
    """Read a naive timestamp (driver/fixture round-trip) as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def key_status(
    token: AgentApiToken,
    agent: Agent | None,
    now: datetime | None = None,
) -> AgentApiKeyStatus:
    """Status of an issued key per plan D4.

    ``agent`` is the producer row (None when ``token.agent_id`` is None or the
    row could not be loaded).
    """
    if token.revoked_at is not None or not token.is_active:
        return AgentApiKeyStatus.REVOKED_BY_ISSUER
    if token.agent_id is None or agent is None:
        return AgentApiKeyStatus.PRODUCER_REMOVED
    if token.owner_id is None:
        return AgentApiKeyStatus.ISSUER_REMOVED
    if token.expires_at is not None and (now or datetime.now(UTC)) >= _as_utc(
        token.expires_at
    ):
        return AgentApiKeyStatus.EXPIRED
    if not agent.agent_api_enabled:
        return AgentApiKeyStatus.API_DISABLED
    if not agent.agent_api_external_access_enabled:
        return AgentApiKeyStatus.EXTERNAL_ACCESS_PAUSED
    return AgentApiKeyStatus.ACTIVE


def is_terminal(status: AgentApiKeyStatus) -> bool:
    """Terminal statuses never recover; rotate / reveal / spec refuse them."""
    return status in TERMINAL_KEY_STATUSES


def unavailable_reason_for(
    status: AgentApiKeyStatus,
) -> AgentApiKeyUnavailableReason | None:
    """Map a status to the shared cloud/Desktop ``unavailable_reason`` (D21)."""
    if status in (
        AgentApiKeyStatus.REVOKED_BY_ISSUER,
        AgentApiKeyStatus.PRODUCER_REMOVED,
        AgentApiKeyStatus.ISSUER_REMOVED,
    ):
        return "key_revoked"
    if status == AgentApiKeyStatus.EXPIRED:
        return "key_expired"
    if status in (
        AgentApiKeyStatus.API_DISABLED,
        AgentApiKeyStatus.EXTERNAL_ACCESS_PAUSED,
    ):
        return "key_paused"
    return None


def previous_valid_until(
    token: AgentApiToken, now: datetime | None = None
) -> datetime | None:
    """End of the rotation grace window, or None when it is closed."""
    if token.previous_token_hash is None or token.previous_token_expires_at is None:
        return None
    expires = _as_utc(token.previous_token_expires_at)
    return expires if expires > (now or datetime.now(UTC)) else None


@dataclass(frozen=True)
class KeyState:
    """An issued key with what its status was computed from."""

    token: AgentApiToken
    agent: Agent | None
    issuer_email: str | None
    status: AgentApiKeyStatus


def key_states_by_credential(
    session: Session,
    credential_ids: list[uuid.UUID],
    now: datetime | None = None,
) -> dict[uuid.UUID, KeyState]:
    """Batch-resolve issued-key states for a page of credentials (one query).

    Credentials that are not issued keys (connections, unbound, other types)
    are absent from the result.
    """
    if not credential_ids:
        return {}
    rows = session.exec(
        select(AgentApiToken, Agent, User.email)
        .outerjoin(Agent, Agent.id == AgentApiToken.agent_id)
        .outerjoin(User, User.id == AgentApiToken.owner_id)
        .where(
            AgentApiToken.credential_id.in_(credential_ids),
            AgentApiToken.kind == AgentApiTokenKind.EXTERNAL.value,
        )
    ).all()
    moment = now or datetime.now(UTC)
    return {
        token.credential_id: KeyState(
            token=token,
            agent=agent,
            issuer_email=issuer_email,
            status=key_status(token, agent, moment),
        )
        for token, agent, issuer_email in rows
        if token.credential_id is not None
    }


def key_state_for_credential(
    session: Session, credential_id: uuid.UUID
) -> KeyState | None:
    """``key_states_by_credential`` for one credential."""
    return key_states_by_credential(session, [credential_id]).get(credential_id)


def key_status_value(
    session: Session,
    credential: Credential,
    key_states: dict[uuid.UUID, KeyState] | None = None,
) -> str | None:
    """Issued-key status value of an ``agent_api`` credential, else None.

    ``key_states`` is a page-level ``key_states_by_credential`` result; when
    omitted the one credential is resolved on its own. Connections and
    non-``agent_api`` credentials answer None.
    """
    if credential.type != CredentialType.AGENT_API:
        return None
    if key_states is None:
        key_states = key_states_by_credential(session, [credential.id])
    state = key_states.get(credential.id)
    return state.status.value if state is not None else None
