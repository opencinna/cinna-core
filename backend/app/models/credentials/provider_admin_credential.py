"""Provider admin credentials — the instance-wide secret that mints user keys.

WHAT THIS IS
------------
One row per provider organisation an administrator has connected. It holds an
**administration** secret (an OpenAI Admin API key, say), which is categorically
different from every other secret in this codebase: it does not grant access to
a model, it grants the power to create and destroy keys for a whole provider
organisation.

WHY IT IS ITS OWN TABLE
-----------------------
Because ``ai_credential`` is *plumbing*, and all of that plumbing would reach
this secret the day it lived there. Two lines make the argument concretely:

- ``model_discovery_service`` selects **every** row in ``ai_credential`` on a
  cron and calls the provider with each key.
- ``external_account_config_service`` hands **every** row a user owns, decrypted,
  to the desktop client.

Neither has a bug. They are correct for the table they read. An admin secret
simply must not be in that table — and the isolation is structural rather than
intentional: sharing (``ai_credential_share.ai_credential_id``), environment
linking (``agent_environment.{conversation,building}_ai_credential_id``), bundle
publisher wiring and the blast-radius counts are all foreign keys pinned to
``ai_credential.id``; the environment credential bag is a fixed slot dict with no
slot to pour into; and nothing anywhere iterates ``SQLModel.metadata``. A table
that is not ``ai_credential`` is not reachable from any of them.

The precedent to read is :class:`app.models.email.mail_server_config.MailServerConfig`:
server-scoped with no ``owner_id`` at all, an encrypted secret column, a public
projection that exposes ``has_password: bool`` instead of the value, and a
superuser-or-nothing router. This copies that shape deliberately.

THE ONE GENERIC PATH A SEPARATE TABLE DOES NOT ESCAPE
-----------------------------------------------------
User deletion is a bare ``session.delete(user)`` and relies entirely on
database-level ``ON DELETE``. ``created_by_id`` is therefore **SET NULL**, never
CASCADE: deleting the superuser who happened to paste the key must not destroy
the instance's ability to mint and — worse — to *revoke* every key it has ever
minted.

WHAT A MINTED KEY ACTUALLY CARRIES
-----------------------------------
A minted key carries write access to the project's API resources. Its blast
radius is bounded by the project's monthly spend limit, which Cinna verifies is
enforcing before minting into a project, and by revocation. No narrower claim is
available to us: the provider documents default service-account permissions as
read and write of the project's API resources, and we do not request key scopes
in this phase.
"""
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlmodel import Column, Field, Index, SQLModel, Text
from sqlalchemy.dialects.postgresql import JSON as PG_JSON

from app.models.credentials.ai_credential import AICredentialType


class ProviderAdminCredentialConfig(SQLModel):
    """Non-secret configuration for one provider organisation.

    ``spend_limit_cents`` is **integer cents**, matching the provider's own
    contract. An off-by-100 here is a hundred-fold cap, so the unit is in the
    name at every layer rather than in a comment at one of them.

    ``project_id`` may be supplied by the administrator (an existing project) or
    left empty for the setup step to create one. Either way the project is
    verified to have an *enforcing* spend limit before the first key is minted —
    a limit is never applied after a key exists.
    """

    organization_id: str | None = Field(default=None, max_length=255)
    project_id: str | None = Field(default=None, max_length=255)
    spend_limit_cents: int = Field(ge=1)


class ProviderAdminCredential(SQLModel, table=True):
    """An administration secret for one provider organisation.

    Server-scoped, not user-scoped: there is no ``owner_id``. Only superusers may
    see or edit these, and the secret is never returned by any endpoint.
    """

    __tablename__ = "provider_admin_credential"
    __table_args__ = (
        Index("ix_provider_admin_credential_type", "provider_type"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(min_length=1, max_length=255, nullable=False)
    provider_type: AICredentialType = Field(..., sa_type=sa.String(50))

    #: Fernet-encrypted administration secret. Never projected, never linked to
    #: an environment, never in ``/external/account-config``.
    encrypted_secret: str = Field(sa_column=Column(Text, nullable=False))

    #: :class:`ProviderAdminCredentialConfig` as stored JSON.
    config: dict = Field(
        default_factory=dict,
        sa_column=Column(
            PG_JSON, nullable=False, server_default=sa.text("'{}'::json")
        ),
    )

    #: Last successful verification against the provider, and the coarse reason
    #: code for the last failed one. Both are non-secret and both are shown to
    #: the admin, because "the key works" and "the project is capped" are the two
    #: questions a Verify press exists to answer.
    last_verified_at: datetime | None = Field(
        default=None, sa_type=sa.DateTime(timezone=True)
    )
    last_verify_error: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    #: Audit only — which superuser connected this. SET NULL on deletion (see the
    #: module docstring): the record stays usable by any superuser, and deleting
    #: an admin account must never destroy the org secret.
    created_by_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            sa.Uuid(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ============= Projection DTOs =============


class ProviderAdminCredentialPublic(SQLModel):
    """Admin-facing projection. **Never** includes the secret.

    ``has_secret`` rather than a nullable secret field, copying
    ``MailServerConfigPublic``: a projection that carries the value's *slot* is
    one refactor away from carrying the value.
    """

    id: uuid.UUID
    name: str
    provider_type: AICredentialType
    config: ProviderAdminCredentialConfig
    has_secret: bool = True
    last_verified_at: datetime | None = None
    last_verify_error: str | None = None
    created_by_id: uuid.UUID | None = None
    #: How many managed parent records currently mint through this credential.
    #: The delete gate's input, surfaced so the admin sees it before pressing.
    minting_credential_count: int = 0
    #: How many live minted keys this credential is the only means of revoking.
    live_minted_key_count: int = 0
    #: Whether ``DELETE`` will be refused without ``force``. The **answer**; the
    #: two counts above are the explanation. A client that computed
    #: ``(a or b) > 0`` for itself would be re-deriving a server policy in the
    #: browser, and would keep answering the old way the day the rule changes.
    delete_blocked: bool = False
    created_at: datetime
    updated_at: datetime


class ProviderAdminCredentialCreate(SQLModel):
    """Connect a provider organisation."""

    name: str = Field(min_length=1, max_length=255)
    provider_type: AICredentialType
    secret: str = Field(min_length=1)
    config: ProviderAdminCredentialConfig


class ProviderAdminCredentialUpdate(SQLModel):
    """Partial update. Every field's omission is representable and means "leave
    it alone" — in particular, omitting ``secret`` keeps the stored one, so the
    admin surface never has to round-trip a secret in order to rename a record.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    secret: str | None = Field(default=None, min_length=1)
    config: ProviderAdminCredentialConfig | None = None


class ProviderAdminCredentialVerifyResult(SQLModel):
    """Outcome of a Verify press.

    Two questions, one answer object, because they fail independently and an
    admin who fixes one wants to see the other: is the secret good, and is the
    project capped by an **enforcing** spend limit?
    """

    ok: bool
    #: Provider-side identity the secret resolved to (an organisation or project
    #: id). Never key material.
    account_ref: str | None = None
    spend_limit_enforcing: bool = False
    spend_limit_cents: int | None = None
    #: Coarse reason code when ``ok`` is False.
    error: str | None = None
