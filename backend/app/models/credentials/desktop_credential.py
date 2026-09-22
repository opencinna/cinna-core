"""API schemas for delivering credentials to authenticated Desktop devices.

These are response/request shapes only (no tables). The per-item ``entry`` is
the cloud-shaped credentials.json entry produced by
``CredentialsService.prepare_credentials_for_environment``; it stays a free
dict because its ``credential_data`` differs per credential type and is
already filtered through ``AGENT_ENV_ALLOWED_FIELDS``.
"""

import uuid
from typing import Any, Literal

from sqlmodel import Field, SQLModel

from app.models.credentials.credential import CredentialType

DesktopCredentialRelation = Literal["owned", "shared"]

# Why a requested credential was not delivered. Per item, never batch-fatal.
DesktopCredentialRefusalReason = Literal[
    "not_found",
    "no_access",
    "not_local_category",
    "local_use_not_allowed",
    "unsupported_type",
]


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
    # The caller's credentials-page category. Only "mine" is ever listed;
    # carried so a Desktop can enforce the same rule on what it receives.
    category: str
    owner_email: str | None = None
    local_use_allowed: bool
    revision: str
    expires_at: int | float | str | None = None
    user_workspace_id: uuid.UUID | None = None


class DesktopCredentialList(SQLModel):
    items: list[DesktopCredentialListItem]


class DesktopCredentialMaterializeRequest(SQLModel):
    credential_ids: list[uuid.UUID] = Field(max_length=50)
    include_current_user: bool = False


class DesktopCredentialRefusal(SQLModel):
    id: uuid.UUID
    reason: DesktopCredentialRefusalReason


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
    # Always null: the owner identity token only accompanies agent_api
    # credentials, which are never delivered to a device. Kept so existing
    # Desktop clients parse.
    owner_identity: dict[str, Any] | None = None
