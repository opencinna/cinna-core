import uuid
from datetime import datetime
from enum import Enum
from typing import List
from pydantic import EmailStr
from sqlmodel import Field, Relationship, SQLModel, Column, Text


# SDK constants
SDK_ANTHROPIC = "claude-code/anthropic"
SDK_MINIMAX = "claude-code/minimax"
VALID_SDK_OPTIONS = [SDK_ANTHROPIC, SDK_MINIMAX]

# AI Functions SDK constants
VALID_AI_FUNCTIONS_SDK_OPTIONS = ["system", "personal:anthropic", "personal:openai"]


# ── Role enum ──────────────────────────────────────────────────────────
#
# Phase 3 of the Agent Bundles & Installs plan introduces a three-value
# role on every user.  ``is_superuser`` continues to drive ``admin``
# privileges (and stays in sync with ``role == ADMIN``), while the
# ``USER`` vs ``DEVELOPER`` distinction gates building-mode and
# agent-CRUD access for everyone else.
class UserRole(str, Enum):
    USER = "agent-user"
    DEVELOPER = "agent-developer"
    ADMIN = "admin"


VALID_USER_ROLES = [r.value for r in UserRole]
DEVELOPER_OR_ADMIN_ROLES = {UserRole.DEVELOPER.value, UserRole.ADMIN.value}


# Shared properties
class UserBase(SQLModel):
    email: EmailStr = Field(unique=True, index=True, max_length=255)
    is_active: bool = True
    is_superuser: bool = False
    full_name: str | None = Field(default=None, max_length=255)
    username: str | None = Field(default=None, max_length=50, index=True, regex=r"^[a-zA-Z0-9_]*$")
    # Phase 3 — role-based permissions.  Default for new users is
    # ``agent-user``; superusers are upgraded to ``admin`` on creation.
    role: str = Field(default=UserRole.USER.value, max_length=32)


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
    default_ai_functions_sdk: str | None = Field(default=None, max_length=50)
    default_ai_functions_credential_id: uuid.UUID | None = None
    general_assistant_enabled: bool | None = None
    workspaces_enabled: bool | None = None
    # Default credential and model override per mode
    default_ai_credential_conversation_id: uuid.UUID | None = None
    default_ai_credential_building_id: uuid.UUID | None = None
    default_model_override_conversation: str | None = Field(default=None, max_length=255)
    default_model_override_building: str | None = Field(default=None, max_length=255)


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
    # Default AI Functions SDK preference
    default_ai_functions_sdk: str | None = Field(default="system", max_length=50)
    # Specific credential ID for AI functions (None = use default for type)
    default_ai_functions_credential_id: uuid.UUID | None = Field(default=None)
    # Default named credential per mode (FK to ai_credential table)
    default_ai_credential_conversation_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="ai_credential.id",
        ondelete="SET NULL",
    )
    default_ai_credential_building_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="ai_credential.id",
        ondelete="SET NULL",
    )
    # Default model override per mode
    default_model_override_conversation: str | None = Field(default=None, max_length=255)
    default_model_override_building: str | None = Field(default=None, max_length=255)
    # General Assistant feature flag
    general_assistant_enabled: bool = Field(default=False)
    # Whether the UI applies workspace filters to list queries. When False,
    # the sidebar workspace switcher is hidden and queries return every
    # owned entity regardless of `user_workspace_id`.
    workspaces_enabled: bool = Field(default=False)
    # Per-user monotonic counter for short-code generation (TASK-1, TASK-2, ...)
    task_sequence_counter: int = Field(default=0)
    # ── Two-Factor Authentication ────────────────────────────────────
    # Master switch — True once the user has enrolled at least one
    # second factor (passkey or TOTP) and has not disabled 2FA since.
    two_factor_enabled: bool = Field(default=False)
    # Timestamp the first factor was confirmed.
    two_factor_enrolled_at: datetime | None = Field(default=None)
    # Last successful second-factor verification (used by Security tab UI).
    two_factor_last_used_at: datetime | None = Field(default=None)
    agents: List["app.models.agents.agent.Agent"] = Relationship(back_populates="owner", cascade_delete=True)
    credentials: List["app.models.credentials.credential.Credential"] = Relationship(back_populates="owner", cascade_delete=True)


# Properties to return via API, id is always required
class UserPublic(UserBase):
    id: uuid.UUID
    has_google_account: bool = False
    has_password: bool = False
    default_sdk_conversation: str | None = SDK_ANTHROPIC
    default_sdk_building: str | None = SDK_ANTHROPIC
    default_ai_functions_sdk: str | None = "system"
    default_ai_functions_credential_id: uuid.UUID | None = None
    general_assistant_enabled: bool = False
    workspaces_enabled: bool = False
    # Default credential and model override per mode
    default_ai_credential_conversation_id: uuid.UUID | None = None
    default_ai_credential_building_id: uuid.UUID | None = None
    default_model_override_conversation: str | None = None
    default_model_override_building: str | None = None
    # Two-Factor Authentication status (derived/aggregated, never the secret)
    two_factor_enabled: bool = False
    has_passkey: bool = False
    has_totp: bool = False


class UsersPublic(SQLModel):
    data: list[UserPublic]
    count: int


class UserRolePublic(SQLModel):
    """Response for ``GET /users/me/role``."""
    role: str


class UserRoleUpdate(SQLModel):
    """Request body for ``PATCH /users/{user_id}/role`` — admin only."""
    role: str


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
