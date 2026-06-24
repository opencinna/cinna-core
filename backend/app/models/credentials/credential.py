import uuid
from enum import Enum
from typing import TYPE_CHECKING, List, Optional
from sqlmodel import Field, Relationship, SQLModel, Column, Text
from sqlalchemy import Index, ForeignKeyConstraint, text
from sqlalchemy.dialects.postgresql import JSON as PG_JSON

from app.models.users.user import User
from app.models.credentials.link_models import AgentCredentialLink

if TYPE_CHECKING:
    pass  # For future imports if needed


# Credential types enum
class CredentialType(str, Enum):
    EMAIL_IMAP = "email_imap"
    EMAIL_SMTP = "email_smtp"
    ODOO = "odoo"
    GMAIL_OAUTH = "gmail_oauth"
    GMAIL_OAUTH_READONLY = "gmail_oauth_readonly"
    GDRIVE_OAUTH = "gdrive_oauth"
    GDRIVE_OAUTH_READONLY = "gdrive_oauth_readonly"
    GCALENDAR_OAUTH = "gcalendar_oauth"
    GCALENDAR_OAUTH_READONLY = "gcalendar_oauth_readonly"
    GOOGLE_SERVICE_ACCOUNT = "google_service_account"
    API_TOKEN = "api_token"
    SSH_KEY = "ssh_key"
    AGENT_API = "agent_api"
    # A connection to a remote MCP server — either another platform agent's
    # agent-to-agent connector or an arbitrary external MCP server. Unlike every
    # other credential type, this is never written to credentials.json; it is
    # collected into a per-mode MCP-server manifest and injected directly into the
    # SDK runtime config (see collect_mcp_provider_manifest).
    MCP_PROVIDER = "mcp_provider"


# Shared properties for credentials
class CredentialBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    type: CredentialType
    notes: str | None = Field(default=None)
    allow_sharing: bool = Field(default=False)  # Whether this credential can be shared with other users
    allow_template_sharing: bool = Field(default=False)  # Whether this credential can be shared as a template (non-private fields are copied as defaults; the installer must supply the private ones)
    # Non-secret audience/slot id (I4). When set, the bundle publisher stamps
    # the same value on the linked spec credential AND every per-user token for
    # the same slot; their names and token values differ. Steers the install-time
    # auto-prefill matcher (top-precedence tier) without carrying any authority
    # itself — plaintext, never encrypted, never redacted. NULL = legacy behavior.
    service_uri: str | None = Field(default=None, sa_type=Text)
    # Per-mode applicability for MCP_PROVIDER credentials. These live on the
    # credential row (not inside the encrypted blob) so the per-mode MCP manifest
    # collector can filter cheaply without decrypting, and so they appear plainly
    # in the credential detail UI. Only meaningful for MCP_PROVIDER rows; ignored
    # for all other types. Default true so a freshly connected provider is active.
    mcp_mode_conversation: bool = Field(default=True)
    mcp_mode_building: bool = Field(default=True)


# Type-specific credential data models (for validation)
class EmailImapData(SQLModel):
    host: str
    port: int
    login: str
    password: str
    is_ssl: bool = True


class EmailSmtpData(SQLModel):
    host: str
    port: int
    username: str
    password: str
    from_email: str
    use_tls: bool = True
    use_ssl: bool = False


class OdooData(SQLModel):
    url: str
    database_name: str
    login: str
    api_token: str


class GmailOAuthData(SQLModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: int | None = None
    scope: str | None = None


class ApiTokenData(SQLModel):
    api_token_type: str  # "bearer" or "custom"
    api_token_template: str = "Authorization: Bearer {TOKEN}"
    api_token: str


class GoogleServiceAccountData(SQLModel):
    type: str  # must be "service_account"
    project_id: str
    private_key_id: str
    private_key: str
    client_email: str
    client_id: str | None = None
    auth_uri: str | None = None
    token_uri: str | None = None
    auth_provider_x509_cert_url: str | None = None
    client_x509_cert_url: str | None = None
    universe_domain: str | None = None


class SSHKeyCredentialData(SQLModel):
    """
    Normalised shape of an ssh_key credential's encrypted blob.

    Only `public_key`, `private_key`, `fingerprint`, and `key_type` are required.
    `passphrase` is optional (MVP: rejected on import — see error-handling docs).
    `host_aliases` is optional (defaults to all hosts via `*`).
    """
    public_key: str
    private_key: str
    fingerprint: str
    key_type: str  # "rsa" | "ed25519" | "ecdsa" | "dss"
    passphrase: str | None = None
    host_aliases: list[str] | None = None


class MCPProviderData(SQLModel):
    """
    Normalised shape of an ``mcp_provider`` credential's encrypted blob.

    Connects a consumer agent's SDK to a remote MCP server. Created by the
    "Connect MCP Provider" helper, never hand-edited. The whole blob is encrypted
    at rest; ``token`` / ``oauth_client_secret`` / ``oauth_refresh_token`` are the
    secrets and are never written to any container artifact other than the
    per-mode MCP manifest (and the backend-only OAuth secrets never leave the
    backend at all).

    OAuth/DCR fields are stored here in Phase 1–3 but the live DCR + refresh
    machinery that populates / rotates them is Phase 5.
    """
    # The MCP server URL the consumer's SDK connects to. For agent2agent this is
    # {MCP_SERVER_BASE_URL}/{connector_id}/mcp; for external it is user-entered.
    endpoint_url: str
    # "streamable-http" (default) or "sse".
    transport: str = "streamable-http"
    # "agent2agent" | "fixed_token" | "oauth_dcr" | "none".
    auth_mode: str = "agent2agent"
    # Display name / SDK server key seed.
    label: str | None = None
    # (agent2agent only) producer agent UUID — UI display + back-reference.
    target_agent_id: str | None = None
    # (agent2agent only) producer mcp_connector UUID.
    target_connector_id: str | None = None
    # Bearer token sent as ``Authorization: Bearer …``. For agent2agent this is
    # the producer's direct token; for fixed_token it's the user-entered token;
    # for oauth_dcr it's the current (refreshed) access token. None for "none".
    token: str | None = None
    # ── oauth_dcr only (Phase 5 populates these) ──────────────────────────────
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None  # backend-only, never whitelisted
    oauth_refresh_token: str | None = None  # backend-only, never whitelisted
    oauth_token_expires_at: int | None = None  # unix ts; drives pre-stream refresh
    oauth_authorization_server: str | None = None  # AS metadata base URL
    oauth_scope: str | None = None
    oauth_resource: str | None = None  # RFC 8707 resource param (the endpoint URL)


# Properties to receive on credential creation
class CredentialCreate(CredentialBase):
    # credential_data will contain the type-specific data (EmailImapData, OdooData, or GmailOAuthData)
    # Optional to allow creating credentials with just name and type, then filling details later
    credential_data: dict | None = None
    user_workspace_id: uuid.UUID | None = None
    template_private_fields: list[str] | None = None


# Properties to receive on credential update
class CredentialUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = None
    credential_data: dict | None = None
    allow_sharing: bool | None = None  # Update sharing permission
    allow_template_sharing: bool | None = None  # Toggle template-sharing
    template_private_fields: list[str] | None = None  # Fields the installer must supply when installing a bundle that uses this credential as a template
    service_uri: str | None = None  # Editable non-secret audience/slot id (I4)
    # Per-mode toggles for MCP_PROVIDER credentials (ignored for other types).
    mcp_mode_conversation: bool | None = None
    mcp_mode_building: bool | None = None


# Database model
class Credential(CredentialBase, table=True):
    __table_args__ = (
        # Partial indexes for efficient querying
        Index(
            "ix_credential_allow_sharing",
            "allow_sharing",
            postgresql_where=text("allow_sharing = true"),
        ),
        Index(
            "ix_credential_placeholder",
            "is_placeholder",
            postgresql_where=text("is_placeholder = true"),
        ),
        # Partial index for the service_uri matcher tier — the vast majority of
        # rows have NULL service_uri, so a partial index stays small.
        Index(
            "ix_credential_service_uri",
            "service_uri",
            postgresql_where=text("service_uri IS NOT NULL"),
        ),
        # Named foreign key for placeholder_source_id
        ForeignKeyConstraint(
            ["placeholder_source_id"],
            ["credential.id"],
            name="fk_credential_placeholder_source",
            ondelete="SET NULL",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Store encrypted credential data as text
    encrypted_data: str = Field(sa_column=Column(Text, nullable=False))
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    user_workspace_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_workspace.id", ondelete="SET NULL"
    )

    # Names of credential_data fields that are private when this credential is
    # shared as a template. Stored as a JSON array of strings (empty means
    # "no fields are private", which is rare — a template that copies every
    # field is functionally a full share).
    template_private_fields: list[str] = Field(
        default_factory=list,
        sa_column=Column(PG_JSON, nullable=False, server_default="[]"),
    )

    # Placeholder fields (for clones when original credential is not shareable)
    is_placeholder: bool = Field(default=False)
    placeholder_source_id: uuid.UUID | None = Field(default=None)  # FK in __table_args__

    owner: User | None = Relationship(back_populates="credentials")
    agents: List["app.models.agents.agent.Agent"] = Relationship(
        back_populates="credentials", link_model=AgentCredentialLink
    )

    # Relationship to source (for placeholders)
    placeholder_source: Optional["Credential"] = Relationship(
        sa_relationship_kwargs={"remote_side": "Credential.id"}
    )


# Properties to return via API (without sensitive data)
class CredentialPublic(CredentialBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    user_workspace_id: uuid.UUID | None
    share_count: int = 0  # Number of users this credential is shared with
    is_shared: bool = False  # True if this credential is shared with the user (not owned)
    owner_email: str | None = None  # Email of the owner (only set for shared credentials)
    template_private_fields: list[str] = []
    # Placeholder fields for clones
    is_placeholder: bool = False
    placeholder_source_id: uuid.UUID | None = None
    status: str | None = None  # "complete" | "incomplete" for UI (computed field)
    # Tab discriminator computed via CredentialsService.classify_credential_category.
    # "mine" | "automatic" | "bundle". For owned rows this is "mine" or "automatic".
    category: str = "mine"
    # Count of agents linked to this credential via AgentCredentialLink.
    agent_usage_count: int = 0
    # True if the credential is used in ≥1 of the owner's bundles.
    used_in_bundle: bool = False


# Properties to return via API with decrypted data
class CredentialWithData(CredentialPublic):
    credential_data: dict


class CredentialsPublic(SQLModel):
    data: list[CredentialPublic]
    count: int


class CredentialBundleUsage(SQLModel):
    """One bundle that uses this credential.

    ``publisher_install_id`` is the publisher install's ``Agent.id`` —
    the frontend uses it to deep-link into the agent's Bundle tab where
    bundle settings live (the platform doesn't expose a standalone
    ``/bundles/{uuid}`` route).

    ``provided_by`` is the resolved provisioning mode for this credential
    in the bundle (``"user"`` | ``"publisher"`` | ``"template"``),
    computed via ``PublishService.resolve_provided_by``. The frontend
    uses it to split usages between the Sharing and Share-as-Template
    cards.
    """
    bundle_uuid: uuid.UUID
    bundle_id: str
    display_name: str
    publisher_install_id: uuid.UUID | None = None
    provided_by: str  # "user" | "publisher" | "template"


class CredentialBundleUsages(SQLModel):
    data: list[CredentialBundleUsage]
    count: int


class CredentialAffectedAgent(SQLModel):
    """An agent owned by the requester that links the credential."""

    id: uuid.UUID
    name: str
    ui_color_preset: str | None = None


class CredentialDeletionImpact(SQLModel):
    """Blast-radius classification for deleting a credential.

    ``tier`` grades the deletion impact:

    - ``0`` (self-only): the credential is only used by the requester's own
      agents; no direct shares, no publisher-provided (PBP) usage in a
      published bundle. Deletion is allowed.
    - ``1`` (direct shares): direct ``CredentialShare`` rows exist but the
      credential is not PBP in any published bundle. Deletion is allowed with
      a warning (recipients lose access immediately).
    - ``2`` (PBP in published bundle with ≥1 active foreign install): deleting
      breaks other users' installs. Blocked by default (HTTP 409); the owner
      may force the deletion via ``force=true``.

    ``bundle_usages`` is every bundle whose publisher install links this
    credential, in any provisioning mode (``publisher``/``template``/``user``).
    It is shown informationally so the UI can always tell the user the
    credential is part of a bundle, regardless of tier.

    ``bundle_pbp_usages`` is the PBP subset (``provided_by == "publisher"``)
    that drives the Tier-2 block and lets the UI deep-link to each affected
    bundle. ``active_install_count`` is the number of foreign installs
    (non-publisher) that link the credential.
    """

    tier: int
    affected_own_agents: list[CredentialAffectedAgent] = []
    direct_share_count: int = 0
    bundle_usages: list[CredentialBundleUsage] = []
    bundle_pbp_usages: list[CredentialBundleUsage] = []
    active_install_count: int = 0
