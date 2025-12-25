# Re-export all models for backward compatibility
from sqlmodel import SQLModel
from .link_models import AgentCredentialLink
from .user import (
    User,
    UserCreate,
    UserRegister,
    UserUpdate,
    UserUpdateMe,
    UserPublic,
    UsersPublic,
    UpdatePassword,
    Message,
    NewPassword,
    Token,
    TokenPayload,
    SetPassword,
    OAuthConfig,
)
from .agent import (
    Agent,
    AgentCreate,
    AgentUpdate,
    AgentPublic,
    AgentsPublic,
    AgentWithCredentials,
    AgentCredentialLinkRequest,
)
from .credential import (
    Credential,
    CredentialCreate,
    CredentialUpdate,
    CredentialPublic,
    CredentialsPublic,
    CredentialType,
    EmailImapData,
    OdooData,
    GmailOAuthData,
    CredentialWithData,
)
from .item import (
    Item,
    ItemCreate,
    ItemUpdate,
    ItemPublic,
    ItemsPublic,
)
from .environment import (
    AgentEnvironment,
    AgentEnvironmentCreate,
    AgentEnvironmentUpdate,
    AgentEnvironmentPublic,
    AgentEnvironmentsPublic,
)
from .session import (
    Session,
    SessionCreate,
    SessionUpdate,
    SessionPublic,
    SessionPublicExtended,
    SessionsPublic,
    SessionsPublicExtended,
    SessionMessage,
    MessageCreate,
    MessagePublic,
    MessagesPublic,
)

__all__ = [
    # Core
    "SQLModel",
    # Link models
    "AgentCredentialLink",
    # Users
    "User",
    "UserCreate",
    "UserRegister",
    "UserUpdate",
    "UserUpdateMe",
    "UserPublic",
    "UsersPublic",
    "UpdatePassword",
    "Message",
    "NewPassword",
    "Token",
    "TokenPayload",
    "SetPassword",
    "OAuthConfig",
    # Agents
    "Agent",
    "AgentCreate",
    "AgentUpdate",
    "AgentPublic",
    "AgentsPublic",
    "AgentCredentialLink",
    "AgentWithCredentials",
    "AgentCredentialLinkRequest",
    # Credentials
    "Credential",
    "CredentialCreate",
    "CredentialUpdate",
    "CredentialPublic",
    "CredentialsPublic",
    "CredentialType",
    "EmailImapData",
    "OdooData",
    "GmailOAuthData",
    "CredentialWithData",
    # Items
    "Item",
    "ItemCreate",
    "ItemUpdate",
    "ItemPublic",
    "ItemsPublic",
    # Environments
    "AgentEnvironment",
    "AgentEnvironmentCreate",
    "AgentEnvironmentUpdate",
    "AgentEnvironmentPublic",
    "AgentEnvironmentsPublic",
    # Sessions
    "Session",
    "SessionCreate",
    "SessionUpdate",
    "SessionPublic",
    "SessionPublicExtended",
    "SessionsPublic",
    "SessionsPublicExtended",
    # Messages
    "SessionMessage",
    "MessageCreate",
    "MessagePublic",
    "MessagesPublic",
]
