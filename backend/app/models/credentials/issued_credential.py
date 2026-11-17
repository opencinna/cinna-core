"""
Issuer's cross-agent view: access tokens the caller issued for their agents.

One row shape for three token products, served by ``GET /credentials/issued``:

- ``agent_api_key`` — an Agent REST API key issued to another platform user
  (``AgentApiToken``, kind external). The holder is that person.
- ``a2a_token`` — an A2A access token (``AgentAccessToken``). The holder is an
  external client the platform knows nothing about.
- ``acp_token`` — a bearer token under an ACP connector (``ACPToken``). The
  holder is an external client.

``status`` is normalized across the three; ``raw_status`` keeps the product's
own vocabulary (agent_api's ``AgentApiKeyStatus``; for the others it equals
``status``). Never the token value. Revoking stays per product (see the route).
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Annotated, Literal

from sqlmodel import Field, SQLModel

if TYPE_CHECKING:
    from app.models.agents.agent import Agent


class IssuedCredentialKind(str, Enum):
    """Which token product a row belongs to. Declaration order is sort order."""

    AGENT_API_KEY = "agent_api_key"
    A2A_TOKEN = "a2a_token"
    ACP_TOKEN = "acp_token"


ISSUED_KIND_ORDER: dict[str, int] = {
    kind.value: index for index, kind in enumerate(IssuedCredentialKind)
}


class IssuedCredentialStatus(str, Enum):
    """Normalized status. The last three are terminal (hidden by default)."""

    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"
    REVOKED = "revoked"
    AGENT_REMOVED = "agent_removed"


TERMINAL_ISSUED_STATUSES: frozenset[IssuedCredentialStatus] = frozenset(
    {
        IssuedCredentialStatus.EXPIRED,
        IssuedCredentialStatus.REVOKED,
        IssuedCredentialStatus.AGENT_REMOVED,
    }
)


class IssuedCredentialHolderKind(str, Enum):
    """Who holds the token: a platform user, or a client outside the platform."""

    PERSON = "person"
    EXTERNAL_CLIENT = "external_client"


class IssuedCredentialAgent(SQLModel):
    """The agent the token grants access to, as its owner sees it."""

    id: uuid.UUID
    name: str
    ui_color_preset: str | None = None
    user_workspace_id: uuid.UUID | None = None

    @classmethod
    def from_agent(cls, agent: "Agent | None") -> "IssuedCredentialAgent | None":
        if agent is None:
            return None
        return cls(
            id=agent.id,
            name=agent.name,
            ui_color_preset=agent.ui_color_preset,
            user_workspace_id=agent.user_workspace_id,
        )


class IssuedCredentialHolder(SQLModel):
    """The platform user holding an ``agent_api_key``."""

    id: uuid.UUID
    email: str | None = None
    full_name: str | None = None


class IssuedAgentApiKeyDetails(SQLModel):
    """``scopes``: the holder's current grant on the agent; None without a grant."""

    kind: Literal["agent_api_key"] = "agent_api_key"
    scopes: list[str] | None = None


class IssuedA2ATokenDetails(SQLModel):
    kind: Literal["a2a_token"] = "a2a_token"
    mode: str
    scope: str


class IssuedACPTokenDetails(SQLModel):
    kind: Literal["acp_token"] = "acp_token"
    connector_id: uuid.UUID
    connector_name: str
    mode: str


IssuedCredentialDetails = Annotated[
    IssuedAgentApiKeyDetails | IssuedA2ATokenDetails | IssuedACPTokenDetails,
    Field(discriminator="kind"),
]


class IssuedCredentialPublic(SQLModel):
    """One access token the caller issued for one of their agents.

    ``agent`` is None when the agent was deleted (only possible for
    ``agent_api_key``; A2A and ACP tokens cascade with their agent).
    ``holder`` is set only for ``agent_api_key`` and is None when that account
    is gone; ``holder_kind`` says which case applies so the UI need not infer it.
    ``last_used_at`` / ``revoked_at`` are None where the product has no such
    timestamp (A2A and ACP record revocation as a flag only).
    """

    kind: IssuedCredentialKind
    id: uuid.UUID
    label: str | None = None
    token_prefix: str
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    agent: IssuedCredentialAgent | None = None
    holder: IssuedCredentialHolder | None = None
    holder_kind: IssuedCredentialHolderKind
    status: IssuedCredentialStatus
    raw_status: str
    details: IssuedCredentialDetails


class IssuedCredentialsPublic(SQLModel):
    """``GET /credentials/issued`` response."""

    data: list[IssuedCredentialPublic]
    count: int
