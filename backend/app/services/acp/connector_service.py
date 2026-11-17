"""Owner-managed ACP connectors and hashed, connector-scoped bearer tokens."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from sqlmodel import Session, select

from app.core.config import settings
from app.models.acp.acp_connector import (
    ACPConnector,
    ACPConnectorCreate,
    ACPConnectorPublic,
    ACPConnectorUpdate,
)
from app.models.acp.acp_token import (
    ACPToken,
    ACPTokenCreate,
    ACPTokenCreated,
    ACPTokenPublic,
)
from app.models.agents.agent import Agent
from app.models.credentials.issued_credential import (
    TERMINAL_ISSUED_STATUSES,
    IssuedACPTokenDetails,
    IssuedCredentialAgent,
    IssuedCredentialHolderKind,
    IssuedCredentialKind,
    IssuedCredentialPublic,
    IssuedCredentialStatus,
)
from app.models.users.user import User
from app.services.users.role_service import RoleService
from app.utils import as_utc


@dataclass(frozen=True)
class ACPAuthentication:
    connector: ACPConnector
    token: ACPToken
    agent: Agent
    owner: User


class ACPConnectorService:
    @staticmethod
    def list_connectors(
        db: Session, agent_id: uuid.UUID, owner_id: uuid.UUID
    ) -> list[ACPConnector]:
        return list(
            db.exec(
                select(ACPConnector)
                .where(
                    ACPConnector.agent_id == agent_id, ACPConnector.owner_id == owner_id
                )
                .order_by(ACPConnector.created_at.desc())
            ).all()
        )

    @staticmethod
    def delete_connector(db: Session, connector: ACPConnector) -> None:
        db.delete(connector)
        db.commit()

    @staticmethod
    def list_tokens(db: Session, connector_id: uuid.UUID) -> list[ACPTokenPublic]:
        tokens = db.exec(
            select(ACPToken)
            .where(ACPToken.connector_id == connector_id)
            .order_by(ACPToken.created_at.desc())
        ).all()
        return [ACPTokenPublic.model_validate(token) for token in tokens]

    @staticmethod
    def revoke_token(db: Session, token: ACPToken) -> ACPTokenPublic:
        token.revoked = True
        db.add(token)
        db.commit()
        db.refresh(token)
        return ACPTokenPublic.model_validate(token)

    @staticmethod
    def delete_token(db: Session, token: ACPToken) -> None:
        db.delete(token)
        db.commit()

    @staticmethod
    def server_url(connector_id: uuid.UUID) -> str | None:
        base = settings.ACP_SERVER_BASE_URL
        if not base:
            return None
        parsed = urlsplit(base)
        scheme = {"https": "wss", "http": "ws"}.get(parsed.scheme, parsed.scheme)
        return urlunsplit(
            (scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/{connector_id}", "", "")
        )

    @staticmethod
    def to_public(connector: ACPConnector) -> ACPConnectorPublic:
        return ACPConnectorPublic.model_validate(
            connector,
            update={"acp_server_url": ACPConnectorService.server_url(connector.id)},
        )

    @staticmethod
    def create_connector(
        db: Session, agent: Agent, data: ACPConnectorCreate
    ) -> ACPConnector:
        connector = ACPConnector(
            agent_id=agent.id, owner_id=agent.owner_id, **data.model_dump()
        )
        db.add(connector)
        db.commit()
        db.refresh(connector)
        return connector

    @staticmethod
    def update_connector(
        db: Session, connector: ACPConnector, data: ACPConnectorUpdate
    ) -> ACPConnector:
        connector.sqlmodel_update(data.model_dump(exclude_unset=True))
        connector.updated_at = datetime.now(UTC)
        db.add(connector)
        db.commit()
        db.refresh(connector)
        return connector

    @staticmethod
    def create_token(
        db: Session, connector: ACPConnector, data: ACPTokenCreate
    ) -> ACPTokenCreated:
        raw_token = "acp_" + secrets.token_urlsafe(48)
        token = ACPToken(
            connector_id=connector.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            prefix=raw_token[:12],
            label=data.label,
            expires_at=datetime.now(UTC) + timedelta(days=data.expires_in_days),
        )
        db.add(token)
        db.commit()
        db.refresh(token)
        return ACPTokenCreated(
            **ACPTokenPublic.model_validate(token).model_dump(), token=raw_token
        )

    @staticmethod
    def _mode_allowed(connector: ACPConnector, owner: User) -> bool:
        """Building-mode connectors need an owner who is still a developer."""
        return connector.mode != "building" or RoleService.is_developer(owner)

    @staticmethod
    def authenticate(
        connector_id: uuid.UUID, raw_token: str, db: Session
    ) -> ACPAuthentication | None:
        """Revalidate current authorization, even inside a long-lived DB session.

        Only the digest is stored. ACP credentials cannot authenticate to any
        other connector or to the platform API. Callers should invoke this on
        every request and during active prompts so revocation takes effect.
        This method does not commit or otherwise mutate the caller's transaction.
        """
        if not raw_token.startswith("acp_") or len(raw_token) > 256:
            return None
        digest = hashlib.sha256(raw_token.encode()).hexdigest()
        rows = db.exec(
            select(ACPConnector, ACPToken, Agent, User)
            .join(ACPToken, ACPToken.connector_id == ACPConnector.id)
            .join(Agent, Agent.id == ACPConnector.agent_id)
            .join(User, User.id == ACPConnector.owner_id)
            .where(
                ACPConnector.id == connector_id,
                ACPConnector.is_active.is_(True),
                ACPToken.token_hash == digest,
                ACPToken.revoked.is_(False),
                ACPToken.expires_at > datetime.now(UTC),
                User.is_active.is_(True),
                Agent.is_active.is_(True),
                Agent.owner_id == ACPConnector.owner_id,
            )
            .execution_options(populate_existing=True)
        ).first()
        if rows is None:
            return None
        connector, token, agent, owner = rows
        if not ACPConnectorService._mode_allowed(connector, owner):
            return None
        return ACPAuthentication(
            connector=connector, token=token, agent=agent, owner=owner
        )

    @staticmethod
    def mark_used(db: Session, token: ACPToken) -> None:
        """Record a successfully accepted connection separately from validation."""
        token.last_used_at = datetime.now(UTC)
        db.add(token)
        db.commit()

    @staticmethod
    def list_issued_by(
        db: Session,
        owner_id: uuid.UUID,
        include_ended: bool = False,
        filter_workspace: bool = False,
        workspace_id: uuid.UUID | None = None,
    ) -> list[IssuedCredentialPublic]:
        """Every token under a connector ``owner_id`` owns, as issued rows.

        Rows for ``GET /credentials/issued`` (unsorted; the aggregator orders).
        Issuer = the connector's ``owner_id`` AND the agent's owner, the same
        pair the owner routes gate on, so every row returned is revocable by
        the caller. Status: revoked > expired > paused (``authenticate`` would
        reject it: connector/agent/owner inactive, or building mode without the
        developer role) > active; ended rows only with ``include_ended``. One
        query plus one owner lookup.
        """
        statement = (
            select(ACPToken, ACPConnector, Agent)
            .join(ACPConnector, ACPConnector.id == ACPToken.connector_id)
            .join(Agent, Agent.id == ACPConnector.agent_id)
            .where(ACPConnector.owner_id == owner_id, Agent.owner_id == owner_id)
        )
        if filter_workspace:
            statement = statement.where(Agent.user_workspace_id == workspace_id)

        owner = db.get(User, owner_id)
        owner_active = owner is not None and owner.is_active
        now = datetime.now(UTC)
        result: list[IssuedCredentialPublic] = []
        for token, connector, agent in db.exec(statement).all():
            if token.revoked:
                status = IssuedCredentialStatus.REVOKED
            elif as_utc(token.expires_at) <= now:
                status = IssuedCredentialStatus.EXPIRED
            elif (
                not connector.is_active
                or not agent.is_active
                or not owner_active
                or not ACPConnectorService._mode_allowed(connector, owner)
            ):
                status = IssuedCredentialStatus.PAUSED
            else:
                status = IssuedCredentialStatus.ACTIVE
            if not include_ended and status in TERMINAL_ISSUED_STATUSES:
                continue
            result.append(
                IssuedCredentialPublic(
                    kind=IssuedCredentialKind.ACP_TOKEN,
                    id=token.id,
                    label=token.label,
                    token_prefix=token.prefix,
                    created_at=token.created_at,
                    expires_at=token.expires_at,
                    last_used_at=token.last_used_at,
                    agent=IssuedCredentialAgent.from_agent(agent),
                    holder_kind=IssuedCredentialHolderKind.EXTERNAL_CLIENT,
                    status=status,
                    raw_status=status.value,
                    details=IssuedACPTokenDetails(
                        connector_id=connector.id,
                        connector_name=connector.name,
                        mode=connector.mode,
                    ),
                )
            )
        return result
