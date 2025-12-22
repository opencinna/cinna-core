import uuid
from enum import Enum

from pydantic import EmailStr
from sqlmodel import Field, Relationship, SQLModel, Column, Text


# Shared properties
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True, max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on creation
class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128)


class UserRegister(SQLModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


# Properties to receive via API on update, all are optional
class UserUpdate(UserBase):
    email: EmailStr | None = Field(default=None, max_length=255)  # type: ignore
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserUpdateMe(SQLModel):
    full_name: str | None = Field(default=None, max_length=255)
    email: EmailStr | None = Field(default=None, max_length=255)


class UpdatePassword(SQLModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# Database model, database table inferred from class name
class User(UserBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    hashed_password: str | None = None
    google_id: str | None = Field(default=None, max_length=255, unique=True, index=True)
    items: list["Item"] = Relationship(back_populates="owner", cascade_delete=True)
    agents: list["Agent"] = Relationship(back_populates="owner", cascade_delete=True)
    credentials: list["Credential"] = Relationship(back_populates="owner", cascade_delete=True)


# Properties to return via API, id is always required
class UserPublic(UserBase):
    id: uuid.UUID
    has_google_account: bool = False
    has_password: bool = False


class UsersPublic(SQLModel):
    data: list[UserPublic]
    count: int


# Shared properties
class ItemBase(SQLModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=255)


# Properties to receive on item creation
class ItemCreate(ItemBase):
    pass


# Properties to receive on item update
class ItemUpdate(ItemBase):
    title: str | None = Field(default=None, min_length=1, max_length=255)  # type: ignore


# Database model, database table inferred from class name
class Item(ItemBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="items")


# Properties to return via API, id is always required
class ItemPublic(ItemBase):
    id: uuid.UUID
    owner_id: uuid.UUID


class ItemsPublic(SQLModel):
    data: list[ItemPublic]
    count: int


# Shared properties
class AgentBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    workflow_prompt: str | None = Field(default=None)
    entrypoint_prompt: str | None = Field(default=None)


# Properties to receive on agent creation
class AgentCreate(AgentBase):
    pass


# Properties to receive on agent update
class AgentUpdate(AgentBase):
    name: str | None = Field(default=None, min_length=1, max_length=255)  # type: ignore


# Many-to-many link table for agents and credentials
class AgentCredentialLink(SQLModel, table=True):
    __tablename__ = "agent_credential_link"
    agent_id: uuid.UUID = Field(
        foreign_key="agent.id", primary_key=True, ondelete="CASCADE"
    )
    credential_id: uuid.UUID = Field(
        foreign_key="credential.id", primary_key=True, ondelete="CASCADE"
    )


# Database model, database table inferred from class name
class Agent(AgentBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    owner: User | None = Relationship(back_populates="agents")
    credentials: list["Credential"] = Relationship(
        back_populates="agents", link_model=AgentCredentialLink
    )


# Properties to return via API, id is always required
class AgentPublic(AgentBase):
    id: uuid.UUID
    owner_id: uuid.UUID


class AgentsPublic(SQLModel):
    data: list[AgentPublic]
    count: int


# Properties to return agent with credentials
class AgentWithCredentials(AgentPublic):
    credentials: list["CredentialPublic"]


# Request to link credential to agent
class AgentCredentialLinkRequest(SQLModel):
    credential_id: uuid.UUID


# Credential types enum
class CredentialType(str, Enum):
    EMAIL_IMAP = "email_imap"
    ODOO = "odoo"
    GMAIL_OAUTH = "gmail_oauth"


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


# Properties to receive on credential creation
class CredentialCreate(CredentialBase):
    # credential_data will contain the type-specific data (EmailImapData, OdooData, or GmailOAuthData)
    # Optional to allow creating credentials with just name and type, then filling details later
    credential_data: dict | None = None


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
    owner: User | None = Relationship(back_populates="credentials")
    agents: list["Agent"] = Relationship(
        back_populates="credentials", link_model=AgentCredentialLink
    )


# Properties to return via API (without sensitive data)
class CredentialPublic(CredentialBase):
    id: uuid.UUID
    owner_id: uuid.UUID


# Properties to return via API with decrypted data
class CredentialWithData(CredentialPublic):
    credential_data: dict


class CredentialsPublic(SQLModel):
    data: list[CredentialPublic]
    count: int


# Generic message
class Message(SQLModel):
    message: str


# JSON payload containing access token
class Token(SQLModel):
    access_token: str
    token_type: str = "bearer"


# Contents of JWT token
class TokenPayload(SQLModel):
    sub: str | None = None


class NewPassword(SQLModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


# OAuth models
class SetPassword(SQLModel):
    new_password: str = Field(min_length=8, max_length=128)


class OAuthConfig(SQLModel):
    google_enabled: bool
