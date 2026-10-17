"""API schemas for delivering credentials to authenticated Desktop devices.

These are response/request shapes only (no tables). The per-item ``entry`` is
the cloud-shaped credentials.json entry produced by
``CredentialsService.prepare_credentials_for_environment``; it stays a free
dict because its ``credential_data`` differs per credential type and is
already filtered through ``AGENT_ENV_ALLOWED_FIELDS``.
"""

import uuid
from typing import Any, Literal

from pydantic import field_validator
from sqlmodel import Field, SQLModel

from app.models.credentials.credential import CredentialType

DesktopCredentialRelation = Literal["owned", "shared"]
DesktopOAuthStatus = Literal["pending_authorization", "ready", "reauth_required"]

# Why a requested credential was not delivered. Per item, never batch-fatal.
DesktopCredentialRefusalReason = Literal[
    "not_found",
    "no_access",
    "not_local_category",
    "local_use_not_allowed",
    "unsupported_type",
    # Google OAuth types: not authorized yet (no token stored) /
    # Google rejected the grant, the owner must re-authorize /
    # expired token that could not be refreshed right now (transient).
    "pending_authorization",
    "reauthorization_required",
    "token_unavailable",
    # Issued agent_api keys (only with ``include=agent_api_keys``). Each carries
    # the detailed ``key_status``.
    "key_revoked",  # revoked_by_issuer | producer_removed | issuer_removed
    "key_expired",  # expired
    "key_paused",  # api_disabled | external_access_paused (recovers by itself)
]


class DesktopAgentApiKeyFacts(SQLModel):
    """Non-secret facts of an issued agent_api key (D16). Null for non-keys.

    URLs are the PUBLIC origin (``settings.backend_base_url``), recomputed on
    every read. ``producer_agent_id`` / ``base_url`` / ``spec_url`` are null once
    the producer is removed; ``issuer_email`` once the issuer is removed.
    """

    producer_agent_id: uuid.UUID | None = None
    producer_name: str | None = None
    issuer_email: str | None = None
    base_url: str | None = None
    spec_url: str | None = None
    # ISO-8601 UTC, or null (never expires).
    expires_at: str | None = None


class DesktopCredentialListItem(SQLModel):
    """Metadata of one credential the caller could attach locally. No secrets."""

    id: uuid.UUID
    name: str
    type: CredentialType
    notes: str | None = None
    service_uri: str | None = None
    status: str
    is_placeholder: bool
    relation: DesktopCredentialRelation
    # The caller's credentials-page category. Only "mine" is ever listed,
    # plus "automatic" for a share ``provision-local`` created for a skill
    # installed on a Desktop (``CredentialShare.source="skill_local_install"``).
    category: str
    owner_email: str | None = None
    local_use_allowed: bool
    revision: str
    # For an issued key: the key's expiry (ISO-8601 UTC) or null.
    expires_at: int | float | str | None = None
    user_workspace_id: uuid.UUID | None = None
    # Issued agent_api keys only (AgentApiKeyStatus value); null otherwise.
    # Unknown future values: treat as unavailable.
    key_status: str | None = None
    agent_api: DesktopAgentApiKeyFacts | None = None
    # Google OAuth types only; null otherwise. Derived from the payload.
    oauth_status: DesktopOAuthStatus | None = None
    # The Google account the credential was authorized for. Null for non-OAuth
    # types, before authorization, and when ``local_use_allowed`` is false.
    granted_user_email: str | None = None


class DesktopCredentialList(SQLModel):
    items: list[DesktopCredentialListItem]


class DesktopCredentialMaterializeRequest(SQLModel):
    credential_ids: list[uuid.UUID] = Field(max_length=50)
    include_current_user: bool = False


class DesktopCredentialRefusal(SQLModel):
    id: uuid.UUID
    reason: DesktopCredentialRefusalReason
    # Set on every refusal of an issued key that was otherwise deliverable.
    key_status: str | None = None


class DesktopMaterializedCredential(SQLModel):
    id: uuid.UUID
    revision: str
    entry: dict[str, Any]
    service_account_file: dict[str, Any] | None = None
    # Always null: ssh_key credentials are never delivered to a device (the
    # private key never leaves Core). Kept so existing Desktop clients parse.
    ssh_key: None = None


class DesktopCredentialMaterializeResponse(SQLModel):
    items: list[DesktopMaterializedCredential]
    refused: list[DesktopCredentialRefusal]
    current_user: dict[str, Any] | None = None
    # Always null: issued keys bind identity to the key, connections are never
    # delivered to a device. Kept so existing Desktop clients parse.
    owner_identity: dict[str, Any] | None = None


class DesktopOAuthCredentialCreate(SQLModel):
    """Create a Google OAuth credential from a Desktop client (then authorize it)."""

    type: CredentialType
    # Defaults to the type's display name ("Gmail", "Google Drive (read-only)", ...).
    name: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        # Whitespace-only (or empty) falls back to the type's default name.
        if isinstance(value, str):
            return value.strip() or None
        return value


class DesktopOAuthAuthorizeResponse(SQLModel):
    """Where to send the user's browser to authorize a credential."""

    credential_id: uuid.UUID
    authorization_url: str
    # Epoch seconds when the authorization state expires (the poll window).
    expires_at: int
