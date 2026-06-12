"""
Account CLI convenience-verb + escape-hatch request schemas (Phase 3).

These are **request** Pydantic models only — no ``table=True``. The convenience
verbs reuse existing response models (``AgentPublic``, ``ConnectAgentApiResponse``,
``MCPProviderConnectionResponse``, ``DiscoverableAgents``). The escape hatch's
response is a raw ``fastapi.Response`` passthrough (no model).
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import field_validator
from sqlmodel import Field, SQLModel

from app.models.credentials.credential import CredentialPublic, CredentialType


class AccountAgentCreateBody(SQLModel):
    """Thin-client agent-create body.

    The CLI sends only user-specified fields; the backend applies ALL defaults
    via the normal ``AgentService.create_agent`` path (default AI-credential
    resolution, default env template, environment creation) exactly as the UI
    does. ``env_name`` (env-template selection) is **not** honored at create time
    in v1 — the normal create path hard-codes ``settings.DEFAULT_AGENT_ENV_NAME``
    (see plan O1). The field is accepted-but-noop and documented as a follow-up.
    """

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    # Accepted-but-noop in v1 (O1). Present so the CLI contract is stable when
    # template selection lands; today the agent always gets the server default.
    env_name: str | None = Field(default=None, max_length=255)
    # Target user workspace for the new agent. The CLI fills this from the
    # account workspace's active-workspace config (``cinna account user-workspace
    # --activate``); ``None`` = the Default (unassigned) workspace. Validated to
    # belong to the account user before assignment. Credentials created by the
    # connect verbs inherit the agent's workspace automatically, so this single
    # field covers the "create in my active workspace" intent for both.
    user_workspace_id: uuid.UUID | None = None


class AccountConnectAgentApiBody(SQLModel):
    """Wrap the ``agent_api`` one-click connect helper.

    Maps directly to ``ConnectAgentApiRequest`` plus the producer agent id (a
    body field, since the account route is path-free).
    """

    producer_agent_id: uuid.UUID
    consumer_agent_id: uuid.UUID | None = None
    credential_label: str | None = Field(default=None, max_length=255)
    read_only_override: bool = False


class AccountConnectMcpBody(SQLModel):
    """Wrap the ``mcp_provider`` agent2agent connect helper.

    Maps to ``ConnectMcpProviderAgentRequest``. The CLI resolves
    ``--producer <agent>`` → ``connector_id`` via the discoverable passthrough
    (O2) before calling this.
    """

    connector_id: uuid.UUID
    consumer_agent_id: uuid.UUID | None = None
    mcp_mode_conversation: bool = True
    mcp_mode_building: bool = True
    label: str | None = Field(default=None, max_length=255)


class AccountCredentialCreateBody(SQLModel):
    """Create a *draft* credential from the account workspace.

    SECURITY: deliberately has **no** ``credential_data`` field. The account CLI
    scaffolds the credential's *structure* (name, type, audience) but never sets
    its secret value — the user fills that in the UI later (the credential shows
    as ``status="incomplete"`` until then). This keeps the account token's
    no-credential-secrets guarantee (Decision 6) intact for writes as well as
    reads. ``user_workspace_id`` targets the account's active workspace (the CLI
    fills it from ``.cinna/account.json``; validated to belong to the user).
    """

    name: str = Field(min_length=1, max_length=255)
    type: CredentialType
    notes: str | None = Field(default=None, max_length=2000)
    # Non-secret audience / slot id (e.g. a base URL the token targets).
    service_uri: str | None = Field(default=None, max_length=2048)
    allow_sharing: bool = False
    user_workspace_id: uuid.UUID | None = None


class AccountCredentialUpdateBody(SQLModel):
    """Update a credential's **metadata only** from the account workspace.

    SECURITY: like the create body, there is **no** ``credential_data`` field —
    the account CLI never writes secret values. Only descriptive / structural
    fields are editable here. All fields optional; only the provided ones are
    applied (``exclude_unset`` semantics).
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=2000)
    service_uri: str | None = Field(default=None, max_length=2048)
    allow_sharing: bool | None = None
    allow_template_sharing: bool | None = None


class AccountCredentialShareBody(SQLModel):
    """Attach a credential to an agent the account user owns (``share-with-agent``).

    Links the credential to the agent (``AgentCredentialLink``) so its
    whitelisted fields sync to the agent's environment once the user fills the
    secret value. ``agent_id`` must be owned by the account user.
    """

    agent_id: uuid.UUID


class AccountCredentialDraftResult(SQLModel):
    """Response for ``POST /account/credentials`` — the created draft plus the
    setup hints the orchestrator relays to the user.

    ``required_fields`` lists the secret/config fields the user must fill for the
    credential to become ``complete`` (derived from the platform's per-type
    required-field map). ``setup_url`` deep-links to the Credentials page where
    the user enters them.
    """

    credential: CredentialPublic
    required_fields: list[str]
    setup_url: str


class AccountCredentialTypeInfo(SQLModel):
    """One entry in the credential-type catalogue for the account CLI."""

    type: CredentialType
    required_fields: list[str]
    # Human note for conditional requirements (e.g. api_token custom variant).
    note: str | None = None


class AccountCredentialTypesPublic(SQLModel):
    data: list[AccountCredentialTypeInfo]
    count: int


class AccountApiProxyRequest(SQLModel):
    """Generic escape-hatch request: ``cinna api <METHOD> <path>``.

    ``path`` is **relative to the API root** (no ``/api/v1`` prefix — the backend
    prepends it). ``headers`` is accepted but **ignored** in v1 (O3 — safe
    default; only the minted user JWT is sent inward).
    """

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=2048)
    query: dict[str, str | list[str]] | None = None
    json_body: Any | None = None
    # Accepted but ignored in v1 (O3).
    headers: dict[str, str] | None = None

    @field_validator("method", mode="before")
    @classmethod
    def _upper_method(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v
