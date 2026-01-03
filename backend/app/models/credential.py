import uuid
from enum import Enum
from typing import List, Optional
from sqlmodel import Field, Relationship, SQLModel, Column, Text

from app.models.user import User
from app.models.link_models import AgentCredentialLink


# Credential types enum
class CredentialType(str, Enum):
    EMAIL_IMAP = "email_imap"
    ODOO = "odoo"
    GMAIL_OAUTH = "gmail_oauth"
    API_TOKEN = "api_token"


# Shared properties for credentials
class CredentialBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    type: CredentialType
    notes: str | None = Field(default=None)


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


# Database model
class Credential(CredentialBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # Store encrypted credential data as text
    encrypted_data: str = Field(sa_column=Column(Text, nullable=False))
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    user_workspace_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_workspace.id", ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="credentials")
    agents: List["app.models.agent.Agent"] = Relationship(
        back_populates="credentials", link_model=AgentCredentialLink
    )


# Properties to return via API (without sensitive data)
class CredentialPublic(CredentialBase):
    id: uuid.UUID
    owner_id: uuid.UUID
    user_workspace_id: uuid.UUID | None


# Properties to return via API with decrypted data
class CredentialWithData(CredentialPublic):
    credential_data: dict


class CredentialsPublic(SQLModel):
    data: list[CredentialPublic]
    count: int
