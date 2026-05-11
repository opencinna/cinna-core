"""Catalog + install schemas (no DB tables — request/response only).

The catalog endpoint exposes a curated, visibility-aware subset of
``AgentBundle`` rows for the current user. Installation goes through
``InstallService.install_bundle`` and accepts user-selected credentials
following the explicit per-spec shape introduced in Phase 3 of the
install-experience-redesign plan (with a backwards-compat shim at the
service layer for legacy clients still sending the dict[str, str | dict]
payload).
"""
import uuid
from datetime import datetime
from typing import Literal

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
    # Publisher-provided AI credential FKs mirrored straight from the bundle
    # row (Phase 1 of the install redesign). Phase 2+ uses these to skip
    # the AI credential picker on the install screen and link the
    # publisher's row at install time.
    publisher_ai_credential_conversation_id: uuid.UUID | None = None
    publisher_ai_credential_building_id: uuid.UUID | None = None


class CatalogPublic(SQLModel):
    data: list[CatalogEntryPublic]
    count: int


class AICredentialSelections(BaseModel):
    """AI credential selections used by the install wizard.

    ``use_publisher_ai`` is a UI hint — it tells the backend the frontend
    has acknowledged a publisher-provides-AI bundle. The backend never
    depends on this flag: when the bundle has ``publisher_ai_credential_*_id``
    set, those rows are always linked regardless of what the frontend sent.
    """
    conversation_credential_id: uuid.UUID | None = None
    building_credential_id: uuid.UUID | None = None
    use_publisher_ai: bool = False


class InstallCredentialSelection(BaseModel):
    """Per-spec credential selection in the install request body.

    ``mode`` semantics (matches plan §5):
      - ``use_existing``: link the user's credential identified by
        ``credential_id``. Validated server-side: must be owner-accessible
        and not target a publisher-provides spec.
      - ``placeholder``: create an empty placeholder ``Credential``;
        runtime gate (Phase 4) prompts the user to fill it later.
      - ``publisher_provides``: UI hint that the frontend has acknowledged
        a publisher-provided spec. Backend takes the revision as truth and
        ignores this for branch selection.
      - ``skip``: equivalent to ``placeholder`` for now; reserved for a
        future "do not even link this credential" semantics.
    """
    mode: Literal["use_existing", "placeholder", "publisher_provides", "skip"]
    credential_id: uuid.UUID | None = None


class InstallRequest(BaseModel):
    """Body of ``POST /catalog/{bundle_id}/install``.

    ``credentials`` is keyed on the spec ``name`` (from
    ``required_credential_specs``) → :class:`InstallCredentialSelection`.

    Phase 5: the legacy ``{name: uuid_string | dict}`` shape was dropped
    along with the install-time shim. Only the typed
    :class:`InstallCredentialSelection` shape is accepted now.
    """
    credentials: dict[str, InstallCredentialSelection] | None = None
    ai_credential_selections: AICredentialSelections | None = None


class AdminInstallRequest(InstallRequest):
    """Body of ``POST /catalog/{bundle_id}/admin-install``."""
    target_user_id: uuid.UUID


class InstallContextPublisherSummary(BaseModel):
    """Lightweight publisher-credential descriptor (name + type, no secrets).

    Surfaced on the install context so the install screen can label
    publisher-shared specs without leaking the underlying secret.
    """
    name: str
    type: str


class InstallContextSpec(BaseModel):
    """One service-credential spec, augmented with auto-prefill suggestions.

    ``provided_by`` mirrors the revision spec; ``publisher_summary`` is
    populated only when ``provided_by="publisher"`` (and the row is still
    resolvable). ``suggested_credential_id`` / ``suggested_credential_name``
    are the auto-prefill matcher's output for PBU specs — pure suggestion;
    nothing is committed until the user submits the install.

    For ``provided_by="template"`` specs, ``template_private_fields`` lists
    the field names the installer is expected to fill in after install.
    The non-private template values are not surfaced here — they live on
    the materialised placeholder credential and are returned by the
    setup-credentials endpoint instead.
    """
    name: str
    type: str
    description: str | None = None
    provided_by: Literal["user", "publisher", "template"] = "user"
    publisher_summary: InstallContextPublisherSummary | None = None
    suggested_credential_id: uuid.UUID | None = None
    suggested_credential_name: str | None = None
    template_private_fields: list[str] = []


class InstallContextAIPublisherSummaries(BaseModel):
    """Publisher AI credential summaries for the install context."""
    conversation: InstallContextPublisherSummary | None = None
    building: InstallContextPublisherSummary | None = None


class CatalogInstallContext(BaseModel):
    """Response body for ``GET /catalog/{bundle_id}/install-context``.

    Powers the single-screen install page — the catalog entry plus
    enough metadata for the form to default-populate every section
    without further round-trips.

    ``credentials_payload_schema`` is always ``None`` at runtime; it
    exists purely so the generated OpenAPI client emits a public
    :class:`InstallCredentialSelection` TypeScript type. The frontend
    reads that type when building the install request body (the
    request type itself is left as a loose dict to preserve the
    backward-compat shim that accepts the legacy
    ``dict[str, uuid_str | data_dict]`` shape).
    """
    bundle: CatalogEntryPublic
    ai_provided_by_publisher: bool
    ai_publisher_credential_summaries: InstallContextAIPublisherSummaries
    service_specs: list[InstallContextSpec]
    credentials_payload_schema: InstallCredentialSelection | None = None


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


# ─── Phase 4 — install setup gate ──────────────────────────────────────


class SetupStatusMissingItem(BaseModel):
    """One credential item the install is currently missing.

    Mirrors :class:`app.services.bundles.install_readiness_gate.GateMissingItem`
    but as a Pydantic model so it shows up in the generated OpenAPI client.
    The frontend banner / setup page renders this list.
    """
    spec_name: str
    spec_type: str
    reason: Literal[
        "placeholder_empty",
        "publisher_credential_missing",
        "publisher_credential_unshared",
    ]
    is_ai: bool = False


class SetupStatusResponse(BaseModel):
    """Response of ``GET /agents/{agent_id}/setup-status``.

    Intentionally omits ``user_message`` — the chat / MCP / A2A renderers
    use that field, but the frontend banner renders its own copy from the
    typed ``missing`` list.
    """
    status: Literal["ready", "needs_setup", "publisher_broken"]
    missing: list[SetupStatusMissingItem]
    setup_url: str | None = None


class SetupCredentialSummary(BaseModel):
    """One placeholder credential the install owner can fill in.

    Used by ``GET /agents/{agent_id}/setup-credentials``. Only credentials
    owned by the install owner AND linked to the install AND
    ``is_placeholder=True`` are surfaced (publisher-shared rows are not
    user-fillable).

    For credentials materialised from a bundle template, ``template_private_fields``
    lists the field names the installer is expected to fill in, and
    ``template_prefilled_data`` carries the publisher's non-private values
    so the setup page can render them as read-only context. For non-template
    placeholders both fields are empty.
    """
    id: uuid.UUID
    name: str
    type: str
    description: str | None = None
    template_private_fields: list[str] = []
    template_prefilled_data: dict = {}
