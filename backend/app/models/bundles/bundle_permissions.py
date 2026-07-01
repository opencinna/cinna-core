"""Bundle permissions overview schemas (no DB tables — response only).

These models back the unified **Permissions management** card on the Bundle
tab. They aggregate, read-only, two already-modeled systems:

- Bundle catalog access (``BundleAccessGrant``, gated by
  ``AgentBundle.visibility == "users"``).
- Producer Agent REST API per-user capability scopes
  (``agent_api_access_grant``, gated by ``Agent.agent_api_identity_enabled``).

No new tables are created. Every write the card performs reuses an existing,
already owner-gated endpoint; this file only describes the read-aggregation
response of ``GET /agents/{agent_id}/bundle-permissions-overview``.

Security: the response carries **no secrets**. The ``agent_api`` credential
``token`` is never serialized here — it is decrypted only to read
``producer_agent_id``. ``grants`` and ``scope_catalog`` are populated **only**
for producers where ``can_manage`` is true (caller owns the producer or is a
superuser); non-manageable producers carry empty lists and a read-only
"Managed by <owner_email>" surface.
"""
import uuid

from sqlmodel import SQLModel

from app.models.bundles.bundle_access_grant import BundleAccessGrantPublic


class BundlePermissionScopeCatalogEntry(SQLModel):
    """One ``policy.yaml`` catalog scope, for the modal's quick-add chips."""

    name: str
    description: str | None = None


class BundlePermissionGrant(SQLModel):
    """Per-user scope state on one producer.

    Minimal — display info lives in the top-level ``users`` union to avoid
    duplicating user resolution per producer.
    """

    user_id: uuid.UUID
    grant_id: uuid.UUID
    scopes: list[str] = []


class BundlePermissionProducer(SQLModel):
    """One connected, identity-enabled producer the install consumes.

    Manageable producers (``can_manage=True``) carry the scope catalog and the
    current grants; non-manageable ones carry neither (the owner-gated reads
    never run for them) and are surfaced read-only via ``owner_email``.
    """

    producer_agent_id: uuid.UUID
    producer_agent_name: str | None = None
    producer_ui_color_preset: str | None = None
    credential_id: uuid.UUID
    credential_name: str | None = None
    identity_enabled: bool
    can_manage: bool
    owner_email: str | None = None
    scope_catalog: list[BundlePermissionScopeCatalogEntry] = []
    grants: list[BundlePermissionGrant] = []


class BundlePermissionUser(SQLModel):
    """Resolved display info for every user appearing anywhere in the union.

    Drives the table rows and supplies ``fallbackLabel`` for pills.
    """

    user_id: uuid.UUID
    email: str | None = None
    full_name: str | None = None
    # Set when the user has a ``BundleAccessGrant`` (the revoke key).
    bundle_grant_id: uuid.UUID | None = None


class BundlePermissionsOverview(SQLModel):
    """Response of ``GET /agents/{agent_id}/bundle-permissions-overview``."""

    bundle_uuid: uuid.UUID | None = None
    visibility: str | None = None
    # ``visibility == "users"`` — drives whether the Bundle access column renders.
    bundle_access_applicable: bool = False
    bundle_grants: list[BundleAccessGrantPublic] = []
    producers: list[BundlePermissionProducer] = []
    users: list[BundlePermissionUser] = []
    # ``bundle_access_applicable or len(producers) > 0``.
    show_card: bool = False
