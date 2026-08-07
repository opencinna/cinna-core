"""
Agent REST API per-user access-grant service (L2 scopes).

Owner-gated CRUD over ``agent_api_access_grant`` plus the two read paths the rest
of the feature needs:

- ``get_scope_catalog`` — the available-scope catalog the producer declared in
  ``policy.yaml`` (read from the cached, parsed policy ``scopes`` map). The
  "Access & Scopes" UI reads this to offer a typed picker. Graceful: an empty
  catalog when the producer has not declared any scopes.
- ``resolve_scopes_for_caller`` — the LIVE per-call lookup the proxy uses:
  ``grant(producer, owner) -> scopes`` (plan D5). No grant ⇒ empty list ⇒ no
  scopes header injected.

Ownership is checked via ``AgentApiService.resolve_agent_only`` (the same helper
the producer-preview routes use), which raises ``AgentApiNotFoundError`` (404,
no existence leak) for a non-owner / missing agent.

Grant create / update / delete each write a ``SecurityEvent`` (mirrors the MCP
connector ACL audit). The grant carries no secret — only scope names.
"""
import logging
import uuid
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models import (
    AgentApiAccessGrant,
    AgentApiAccessGrantCreate,
    AgentApiAccessGrantUpdate,
    AgentApiScope,
    AgentApiScopeCatalog,
    SecurityEventCreate,
    User,
)
from app.models.events.security_event import (
    AGENT_API_GRANT_CREATED,
    AGENT_API_GRANT_DELETED,
    AGENT_API_GRANT_UPDATED,
)
from app.services.agent_api.agent_api_service import (
    AgentApiError,
    AgentApiService,
)
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)


class AgentApiGrantError(AgentApiError):
    """Grant-specific service error (reuses the agent-api status-code shape)."""


class AgentApiGrantService:
    """Owner-gated grant CRUD + scope catalog + live scope resolution."""

    # ------------------------------------------------------------------ #
    # Ownership                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _require_owner(
        session: Session,
        producer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool,
    ):
        """Resolve + ownership-check the producer agent (404 on non-owner)."""
        # resolve_agent_only raises AgentApiNotFoundError (404) when the agent is
        # missing OR the caller is not the owner (and not a superuser) — no leak.
        return AgentApiService.resolve_agent_only(
            session, producer_agent_id, user_id, is_superuser=is_superuser
        )

    # ------------------------------------------------------------------ #
    # CRUD                                                                #
    # ------------------------------------------------------------------ #

    @staticmethod
    def list_grants(
        session: Session,
        producer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> list[AgentApiAccessGrant]:
        """List all grants for a producer agent (owner-gated)."""
        AgentApiGrantService._require_owner(
            session, producer_agent_id, user_id, is_superuser
        )
        return list(
            session.exec(
                select(AgentApiAccessGrant).where(
                    AgentApiAccessGrant.producer_agent_id == producer_agent_id
                )
            ).all()
        )

    @staticmethod
    async def create_grant(
        session: Session,
        producer_agent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        data: AgentApiAccessGrantCreate,
        is_superuser: bool = False,
    ) -> AgentApiAccessGrant:
        """Create a grant for ``(producer, data.user_id)`` (owner-gated)."""
        AgentApiGrantService._require_owner(
            session, producer_agent_id, owner_user_id, is_superuser
        )

        # The granted user must exist (a grant to a phantom user is meaningless
        # and would never resolve at the proxy).
        if session.get(User, data.user_id) is None:
            raise AgentApiGrantError("Granted user not found", status_code=404)

        # One grant per (producer, user) — reject a duplicate explicitly (the DB
        # unique constraint backs this) so the caller gets a clean 409 not a 500.
        existing = session.exec(
            select(AgentApiAccessGrant).where(
                AgentApiAccessGrant.producer_agent_id == producer_agent_id,
                AgentApiAccessGrant.user_id == data.user_id,
            )
        ).first()
        if existing is not None:
            raise AgentApiGrantError(
                "A grant for this user already exists; edit it instead",
                status_code=409,
            )

        grant = AgentApiAccessGrant(
            producer_agent_id=producer_agent_id,
            user_id=data.user_id,
            scopes=_sanitize_scopes(data.scopes),
            created_by=owner_user_id,
        )
        session.add(grant)
        session.commit()
        session.refresh(grant)

        await AgentApiGrantService._audit(
            session,
            actor_id=owner_user_id,
            event_type=AGENT_API_GRANT_CREATED,
            agent_id=producer_agent_id,
            grant=grant,
        )
        return grant

    @staticmethod
    async def upsert_grant(
        session: Session,
        producer_agent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        subject_user_id: uuid.UUID,
        scopes: list[str] | None,
        is_superuser: bool = False,
    ) -> AgentApiAccessGrant:
        """Create or update the ``(producer, subject)`` grant (owner-gated).

        The idempotent sibling of ``create_grant``, needed because scopes are
        assigned from TWO places that address the same row (plan D5): the
        producer's Access & Scopes card and external-key minting. A key issued to
        a user who already has a grant must edit that grant, not 409 — the 409 in
        ``create_grant`` is right for the card (where a duplicate is a user
        mistake) and wrong here.

        ``scopes=None`` means "leave an existing grant's scopes alone" (and
        create an empty one if none exists), so minting a key without scopes
        never silently clears capability the owner assigned elsewhere.
        """
        AgentApiGrantService._require_owner(
            session, producer_agent_id, owner_user_id, is_superuser
        )
        if session.get(User, subject_user_id) is None:
            raise AgentApiGrantError("Granted user not found", status_code=404)

        grant = session.exec(
            select(AgentApiAccessGrant).where(
                AgentApiAccessGrant.producer_agent_id == producer_agent_id,
                AgentApiAccessGrant.user_id == subject_user_id,
            )
        ).first()

        if grant is None:
            grant = AgentApiAccessGrant(
                producer_agent_id=producer_agent_id,
                user_id=subject_user_id,
                scopes=_sanitize_scopes(scopes),
                created_by=owner_user_id,
            )
            event_type = AGENT_API_GRANT_CREATED
        else:
            if scopes is None:
                # Nothing to write. Return the existing grant untouched rather
                # than bumping updated_at and auditing a change that never
                # happened.
                return grant
            grant.scopes = _sanitize_scopes(scopes)
            grant.updated_at = datetime.now(UTC)
            event_type = AGENT_API_GRANT_UPDATED

        session.add(grant)
        session.commit()
        session.refresh(grant)

        await AgentApiGrantService._audit(
            session,
            actor_id=owner_user_id,
            event_type=event_type,
            agent_id=producer_agent_id,
            grant=grant,
        )
        return grant

    @staticmethod
    async def update_grant(
        session: Session,
        producer_agent_id: uuid.UUID,
        grant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        data: AgentApiAccessGrantUpdate,
        is_superuser: bool = False,
    ) -> AgentApiAccessGrant:
        """Update a grant's scopes (owner-gated)."""
        AgentApiGrantService._require_owner(
            session, producer_agent_id, owner_user_id, is_superuser
        )
        grant = AgentApiGrantService._load_owned_grant(
            session, producer_agent_id, grant_id
        )

        if data.scopes is not None:
            grant.scopes = _sanitize_scopes(data.scopes)
        grant.updated_at = datetime.now(UTC)
        session.add(grant)
        session.commit()
        session.refresh(grant)

        await AgentApiGrantService._audit(
            session,
            actor_id=owner_user_id,
            event_type=AGENT_API_GRANT_UPDATED,
            agent_id=producer_agent_id,
            grant=grant,
        )
        return grant

    @staticmethod
    async def delete_grant(
        session: Session,
        producer_agent_id: uuid.UUID,
        grant_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> None:
        """Delete a grant (owner-gated). Takes effect on the next call."""
        AgentApiGrantService._require_owner(
            session, producer_agent_id, owner_user_id, is_superuser
        )
        grant = AgentApiGrantService._load_owned_grant(
            session, producer_agent_id, grant_id
        )

        # Capture identity for the audit before the row is gone.
        granted_user_id = grant.user_id
        scopes = list(grant.scopes or [])
        session.delete(grant)
        session.commit()

        await AgentApiGrantService._audit_raw(
            session,
            actor_id=owner_user_id,
            event_type=AGENT_API_GRANT_DELETED,
            agent_id=producer_agent_id,
            granted_user_id=granted_user_id,
            scopes=scopes,
            grant_id=grant_id,
        )

    @staticmethod
    def _load_owned_grant(
        session: Session,
        producer_agent_id: uuid.UUID,
        grant_id: uuid.UUID,
    ) -> AgentApiAccessGrant:
        """Load a grant, asserting it belongs to the producer agent (404 else)."""
        grant = session.get(AgentApiAccessGrant, grant_id)
        if grant is None or grant.producer_agent_id != producer_agent_id:
            raise AgentApiGrantError("Grant not found", status_code=404)
        return grant

    # ------------------------------------------------------------------ #
    # Scope catalog (from the cached policy)                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_scope_catalog(
        session: Session,
        producer_agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> AgentApiScopeCatalog:
        """Return the available-scope catalog from the producer's cached policy.

        Reads ``policy["scopes"]`` — the canonical, already-normalized catalog
        produced by ``AgentApiService.parse_policy`` (a list of
        ``{name, description, requires}``) — via ``get_effective_policy``. The UI
        picker only needs ``name`` + ``description``; the ``requires`` patterns
        are platform-internal (edge enforcement) and are not surfaced here.
        Graceful: an empty catalog when the producer declared no scopes.

        The producer authors scopes in ``policy.yaml`` in either the bare
        ``{name: description}`` form or the rich
        ``{name: {description, requires: [...]}}`` form — both are normalized at
        policy-parse time, so this reader is form-agnostic.
        """
        agent = AgentApiGrantService._require_owner(
            session, producer_agent_id, user_id, is_superuser
        )
        policy = AgentApiService.get_effective_policy(session, agent)
        return AgentApiScopeCatalog(
            scopes=_catalog_from_parsed_policy(policy.get("scopes"))
        )

    # ------------------------------------------------------------------ #
    # Live per-call scope resolution (used by the proxy)                 #
    # ------------------------------------------------------------------ #

    @staticmethod
    def resolve_scopes_for_caller(
        session: Session,
        producer_agent_id: uuid.UUID,
        owner_user_id: uuid.UUID,
    ) -> list[str]:
        """Resolve ``grant(producer, owner) -> scopes`` LIVE for the proxy.

        Returns the grant's scope list, or an empty list when there is no grant
        (the producer then decides what an unscoped caller may do). Never raises;
        a lookup failure degrades to no scopes.
        """
        # Broad-except swallow is safe here: this read runs at the proxy AFTER the
        # last commit on this session (update_last_activity) and before only the
        # adapter proxy call — no later commit depends on this session.
        try:
            grant = session.exec(
                select(AgentApiAccessGrant).where(
                    AgentApiAccessGrant.producer_agent_id == producer_agent_id,
                    AgentApiAccessGrant.user_id == owner_user_id,
                )
            ).first()
        except Exception:
            logger.exception(
                "agent_api grant lookup failed for producer %s user %s; "
                "treating as no scopes",
                producer_agent_id,
                owner_user_id,
            )
            return []
        if grant is None:
            return []
        return [str(s) for s in (grant.scopes or [])]

    # ------------------------------------------------------------------ #
    # Public projection                                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def to_public(session: Session, grant: AgentApiAccessGrant) -> dict:
        """Project a grant to its public dict, resolving the user for the picker."""
        user = session.get(User, grant.user_id)
        return {
            "id": grant.id,
            "producer_agent_id": grant.producer_agent_id,
            "user_id": grant.user_id,
            "scopes": list(grant.scopes or []),
            "user": (
                {"id": user.id, "email": user.email, "full_name": user.full_name}
                if user is not None
                else None
            ),
            "created_by": grant.created_by,
            "created_at": grant.created_at,
            "updated_at": grant.updated_at,
        }

    # ------------------------------------------------------------------ #
    # Audit                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _audit(
        session: Session,
        actor_id: uuid.UUID,
        event_type: str,
        agent_id: uuid.UUID,
        grant: AgentApiAccessGrant,
    ) -> None:
        await AgentApiGrantService._audit_raw(
            session,
            actor_id=actor_id,
            event_type=event_type,
            agent_id=agent_id,
            granted_user_id=grant.user_id,
            scopes=list(grant.scopes or []),
            grant_id=grant.id,
        )

    @staticmethod
    async def _audit_raw(
        session: Session,
        actor_id: uuid.UUID,
        event_type: str,
        agent_id: uuid.UUID,
        granted_user_id: uuid.UUID,
        scopes: list[str],
        grant_id: uuid.UUID,
    ) -> None:
        """Write a SecurityEvent for a grant change. Best-effort (never raises)."""
        try:
            await SecurityEventService.create_event(
                session=session,
                user_id=actor_id,
                data=SecurityEventCreate(
                    agent_id=agent_id,
                    event_type=event_type,
                    severity="medium",
                    details={
                        "grant_id": str(grant_id),
                        "granted_user_id": str(granted_user_id),
                        "scopes": scopes,
                    },
                ),
            )
        except Exception:
            logger.exception(
                "Failed to write SecurityEvent %s for agent_api grant %s",
                event_type,
                grant_id,
            )


def _sanitize_scopes(scopes: list[str] | None) -> list[str]:
    """Normalize a grant's scope list to opaque, space-free, de-duplicated names.

    Scope names are transported in the space-separated ``X-Cinna-Caller-Scopes``
    header and split on whitespace by the producer SDK, so a name containing
    whitespace would be silently split into two. We enforce the "opaque token"
    invariant at the write boundary: trim each name, drop any that are empty or
    still contain inner whitespace, and de-duplicate while preserving order.
    """
    if not scopes:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for scope in scopes:
        if not isinstance(scope, str):
            continue
        name = scope.strip()
        # Reject names with inner whitespace (would be split by the header
        # encoding) and empties.
        if not name or any(ch.isspace() for ch in name):
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return cleaned


def _catalog_from_parsed_policy(scopes) -> list[AgentApiScope]:
    """Project the canonical parsed ``policy["scopes"]`` to the UI catalog model.

    ``scopes`` is the normalized list of ``{name, description, requires}`` dicts
    produced by ``AgentApiService.parse_policy``. We surface only ``name`` +
    ``description`` (the picker doesn't need the edge-enforcement patterns).
    Tolerant of None / unexpected shapes (returns what it can) — never raises.
    """
    if not isinstance(scopes, list):
        return []
    catalog: list[AgentApiScope] = []
    for entry in scopes:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        catalog.append(
            AgentApiScope(name=name.strip(), description=entry.get("description"))
        )
    return catalog
