import uuid
from typing import List
from pydantic import EmailStr
from sqlmodel import Field, Relationship, SQLModel, Column, Text


# SDK constants
SDK_ANTHROPIC = "claude-code/anthropic"
SDK_MINIMAX = "claude-code/minimax"
SDK_OPENAI_COMPATIBLE = "google-adk-wr/openai-compatible"
VALID_SDK_OPTIONS = [SDK_ANTHROPIC, SDK_MINIMAX, SDK_OPENAI_COMPATIBLE]


# Shared properties
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True, max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=50, index=True, regex=r"^[a-zA-Z0-9_]*$")


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
    username: str | None = Field(default=None, max_length=50, regex=r"^[a-zA-Z0-9_]*$")
    default_sdk_conversation: str | None = Field(default=None, max_length=50)
    default_sdk_building: str | None = Field(default=None, max_length=50)
    general_assistant_enabled: bool | None = None


class UpdatePassword(SQLModel):
    current_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# Database model, database table inferred from class name
class User(UserBase, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    hashed_password: str | None = None
    google_id: str | None = Field(default=None, max_length=255, unique=True, index=True)
    # AI Service Credentials (encrypted JSON)
    ai_credentials_encrypted: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # Default SDK preferences for new environments
    default_sdk_conversation: str | None = Field(default=SDK_ANTHROPIC, max_length=50)
    default_sdk_building: str | None = Field(default=SDK_ANTHROPIC, max_length=50)
    # General Assistant feature flag
    general_assistant_enabled: bool = Field(default=True)
    items: List["app.models.item.Item"] = Relationship(back_populates="owner", cascade_delete=True)
    agents: List["app.models.agent.Agent"] = Relationship(back_populates="owner", cascade_delete=True)
    credentials: List["app.models.credential.Credential"] = Relationship(back_populates="owner", cascade_delete=True)


# Properties to return via API, id is always required
class UserPublic(UserBase):
    id: uuid.UUID
    has_google_account: bool = False
    has_password: bool = False
    default_sdk_conversation: str | None = SDK_ANTHROPIC
    default_sdk_building: str | None = SDK_ANTHROPIC
    general_assistant_enabled: bool = True


class UsersPublic(SQLModel):
    data: list[UserPublic]
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
    allow_email_change: bool = True


# AI Service Credentials schemas
class AIServiceCredentials(SQLModel):
    """Decrypted AI service credentials"""
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None  # For future use
    google_ai_api_key: str | None = None  # For future use
    minimax_api_key: str | None = None
    # OpenAI Compatible provider credentials
    openai_compatible_api_key: str | None = None
    openai_compatible_base_url: str | None = None
    openai_compatible_model: str | None = None


class AIServiceCredentialsUpdate(SQLModel):
    """Update AI service credentials (partial update)"""
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_ai_api_key: str | None = None
    minimax_api_key: str | None = None
    # OpenAI Compatible provider credentials
    openai_compatible_api_key: str | None = None
    openai_compatible_base_url: str | None = None
    openai_compatible_model: str | None = None


class UserPublicWithAICredentials(UserPublic):
    """User info indicating which AI credentials are set (not the actual keys)"""
    has_anthropic_api_key: bool = False
    has_openai_api_key: bool = False
    has_google_ai_api_key: bool = False
    has_minimax_api_key: bool = False
    has_openai_compatible_api_key: bool = False
