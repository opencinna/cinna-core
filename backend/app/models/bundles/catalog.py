"""Catalog + install schemas (no DB tables — request/response only).

The catalog endpoint exposes a curated, visibility-aware subset of
``AgentBundle`` rows for the current user. Installation goes through
``InstallService.install_bundle`` and accepts user-selected credentials
following the same shape as today's clone/accept-share wizard.
"""
import uuid
from datetime import datetime

from sqlmodel import SQLModel
from pydantic import BaseModel


class CatalogEntryPublic(SQLModel):
    """One row in the user's catalog."""
    bundle_id: str
    bundle_uuid: uuid.UUID
    display_name: str
    description: str | None
    publisher_handle: str | None  # Truncated identifier — never email/UUID
    publisher_name: str | None = None
    publisher_email: str | None = None
    visibility: str
    latest_revision_id: uuid.UUID | None
    latest_revision_number: int | None
    latest_version: str | None = None
    latest_published_at: datetime | None
    install_count: int
    is_installed: bool
    user_install_id: uuid.UUID | None  # set when is_installed=True
    required_credential_specs: list = []


class CatalogPublic(SQLModel):
    data: list[CatalogEntryPublic]
    count: int


class AICredentialSelections(BaseModel):
    """AI credential selections used by the install wizard."""
    conversation_credential_id: uuid.UUID | None = None
    building_credential_id: uuid.UUID | None = None


class InstallRequest(BaseModel):
    """Body of ``POST /catalog/{bundle_id}/install``.

    ``credentials`` mirrors the legacy accept-share shape:
    ``{credential_name: credential_id_str | {field: value}}``.
    """
    credentials: dict | None = None
    ai_credential_selections: AICredentialSelections | None = None


class AdminInstallRequest(InstallRequest):
    """Body of ``POST /catalog/{bundle_id}/admin-install``."""
    target_user_id: uuid.UUID


class SetUpdateModeRequest(BaseModel):
    """Body of ``PATCH /agents/{agent_id}/update-mode``."""
    update_mode: str  # "automatic" | "manual"


class EditBundleIdRequest(BaseModel):
    """Body of ``PATCH /agents/{agent_id}/bundle-id``.

    Only valid for the publisher install of a bundle that has not yet been
    published (no revisions exist). Once a revision is published, mutating
    the bundle id would silently orphan installed app-data — the API
    rejects with 409.
    """
    bundle_id: str


class CheckUpdatesResponse(BaseModel):
    """Response of ``POST /agents/{agent_id}/check-updates``."""
    pending_update: bool
    installed_revision_number: int | None
    latest_revision_number: int | None
    last_update_status: str | None
    last_sync_at: datetime | None
    update_mode: str
