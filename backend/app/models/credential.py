import uuid
from enum import Enum
from typing import TYPE_CHECKING, List, Optional
from sqlmodel import Field, Relationship, SQLModel, Column, Text
from sqlalchemy import Index, ForeignKeyConstraint, text

from app.models.user import User
from app.models.link_models import AgentCredentialLink

if TYPE_CHECKING:
    pass  # For future imports if needed


# Credential types enum
class CredentialType(str, Enum):
    EMAIL_IMAP = "email_imap"
    ODOO = "odoo"
    GMAIL_OAUTH = "gmail_oauth"
    GMAIL_OAUTH_READONLY = "gmail_oauth_readonly"
    GDRIVE_OAUTH = "gdrive_oauth"
    GDRIVE_OAUTH_READONLY = "gdrive_oauth_readonly"
    GCALENDAR_OAUTH = "gcalendar_oauth"
    GCALENDAR_OAUTH_READONLY = "gcalendar_oauth_readonly"
    GOOGLE_SERVICE_ACCOUNT = "google_service_account"
    API_TOKEN = "api_token"


# Shared properties for credentials
class CredentialBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    type: CredentialType
    notes: str | None = Field(default=None)
    allow_sharing: bool = Field(default=False)  # Whether this credential can be shared with other users


# Type-specific credential data models (for validation)
class EmailImapData(SQLModel):
    host: str
    port: int
    login: str
    password: str
    is_ssl: bool = True


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


# Properties to receive on credential creation
class CredentialCreate(CredentialBase):
    # credential_data will contain the type-specific data (EmailImapData, OdooData, or GmailOAuthData)
    # Optional to allow creating credentials with just name and type, then filling details later
    credential_data: dict | None = None
    user_workspace_id: uuid.UUID | None = None


# Properties to receive on credential update
class CredentialUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = None
    credential_data: dict | None = None
    allow_sharing: bool | None = None  # Update sharing permission


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
        default=None, foreign_key="user_workspace.id", ondelete="CASCADE"
    )

    # Placeholder fields (for clones when original credential is not shareable)
    is_placeholder: bool = Field(default=False)
    placeholder_source_id: uuid.UUID | None = Field(default=None)  # FK in __table_args__

    owner: User | None = Relationship(back_populates="credentials")
    agents: List["app.models.agent.Agent"] = Relationship(
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
    # Placeholder fields for clones
    is_placeholder: bool = False
    placeholder_source_id: uuid.UUID | None = None
    status: str | None = None  # "complete" | "incomplete" for UI (computed field)


# Properties to return via API with decrypted data
class CredentialWithData(CredentialPublic):
    credential_data: dict


class CredentialsPublic(SQLModel):
    data: list[CredentialPublic]
    count: int
