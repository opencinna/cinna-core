"""Configuring an AI provider from a test.

RULE-1 EXEMPTION, DOCUMENTED
---------------------------
``backend/tests/README.md`` Rule 1 keeps ``tests/api/`` off ``app.services``.
This is the single, named place that import lives for AI providers, in the same
spirit as ``tests/utils/account_provisioning.py`` (a transaction-aborting seam
with no HTTP equivalent) and ``tests/utils/key_provisioning.py`` (a converge pass
whose scheduler never runs under pytest).

DECIDED: THIS STAYS A SERVICE SEAM, NOT API WRAPPERS
-----------------------------------------------------
This module used to promise it would become plain API wrappers "the day the
routes existed." ``/admin/ai-providers`` landed in Phase 4 of the
ai-credential-providers plan — a provider can now be created, edited, verified,
rotated, applied and deleted over HTTP, and that router's own coverage lives in
``tests/api/ai_credentials/admin_ai_providers_test.py`` — and the promise was
revisited and explicitly declined, not merely left undone. Record the reason
here, in the module, because the person who next reaches for this docstring
will not have read the conversation that closed the question:

The domain tests that reach for this module assert service-level outcomes —
the reconcile result a policy edit produced, the ``AIProviderInUseError`` a
delete gate raised and the impact it carries, the ``MemberAddition`` an
automatic grant returned. Those are values, not response bodies, and rewriting
the call sites to go through HTTP would replace them with re-derived
assertions on projections — churn, and a loss of resolution, rather than
coverage. The route surface is already tested where it belongs; this module is
how the large majority of call sites across the suite configure a provider
without making every unrelated test a route test. (The exact count drifts as
tests are added; do not enshrine a number here — count the importers if the
question comes up again.)

What is **not** claimed here: that the wrappers and the routes agree field for
field. They call the same service methods the routes call, which is the reason
to trust them, and that is the whole of the claim.

WHAT IS AND IS NOT EXEMPTED
---------------------------
Only the provider service and the two models it needs. Everything else these
tests do — creating accounts, reading back who was provisioned, editing
membership, applying to existing users — still goes through the API, from the
test files. ``apply_provider_to_existing`` below is the one exception, kept for
the service-level reason given above; the equivalent HTTP path is
``POST /admin/ai-providers/{id}/apply-to-existing``, called directly (not
through this module) by tests that want the route's own behaviour. There used
to be a second HTTP entry point onto the identical reconcile — ``POST
/admin/llm-providers/{id}/apply-to-existing``, on the managed credential
itself — but it was removed as redundant (§5.7, ai-credential-providers plan):
duplicate work for a provider-owned record, and an honest but useless
``candidate_count: 0`` for a manual one.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.provider_admin_credential import (
    AIProviderCreate,
    AIProviderKind,
    AIProviderUpdate,
    ProviderAdminCredentialConfig,
)
from app.models.users.user import User
from app.services.credentials.ai_providers_service import (
    AIProviderConflictError,
    AIProviderInUseError,
    ai_providers_service,
)

__all__ = [
    "AIProviderConflictError",
    "AIProviderInUseError",
    "DEFAULT_API_KEY",
    "MINTED_ADMIN_SECRET",
    "apply_provider_to_existing",
    "create_provider_credential",
    "delete_provider",
    "get_provider",
    "grant_members_automatically",
    "orphan_provider",
    "provider_actor",
    "rotate_provider_key",
    "strand_managed_credential",
    "update_provider",
    "verify_provider",
]

# Any syntactically plausible Anthropic key. A ``fixed_key`` provider stores it
# encrypted and nothing calls the vendor with it unless a test asks for a probe,
# so its only requirement is that it round-trips.
DEFAULT_API_KEY = "sk-ant-test-fixed-key"

# An organisation administration secret. Never used against a model — the whole
# point of the ``minted`` kind is that this creates keys rather than being one.
MINTED_ADMIN_SECRET = "sk-admin-test-org-secret"


def provider_actor(db: Session) -> User:
    """The seeded superuser, as the acting administrator.

    Provider writes are audited to an actor and ``created_by_id`` is not
    nullable-by-omission in any of these helpers: an unattributed provider is
    exactly what the audit trail exists to prevent, so the tests attribute the
    same way a route would.
    """
    actor = db.exec(
        select(User).where(User.email == settings.FIRST_SUPERUSER)
    ).first()
    assert actor is not None, (
        "The seeded superuser is missing; the test database was not "
        "initialised."
    )
    return actor


def create_provider_credential(
    db: Session,
    *,
    name: str | None = None,
    kind: str = AIProviderKind.FIXED_KEY.value,
    credential_type: str = "anthropic",
    secret: str | None = None,
    target_user_ids: list[str] | None = None,
    auto_provision_roles: list[str] | None = None,
    set_as_default: bool = False,
    set_user_sdk_defaults: bool = False,
    sdk_default_modes: list[str] | None = None,
    default_model: str | None = None,
    available_models: list[str] | None = None,
    model_override_conversation: str | None = None,
    model_override_building: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    project_id: str | None = None,
    organization_id: str | None = None,
    expiry_notification_date: Any = None,
) -> dict[str, Any]:
    """Create a provider **and its one managed credential**, and grant members.

    Returns the shape ``tests/utils/managed_ai_credential.create_managed_credential``
    returns — ``{"record": …, "added": …, "skipped": …}`` — plus
    ``provider_id``, so a test that used to configure auto-provisioning on the
    credential swaps one call and keeps every assertion.
    """
    from tests.utils.utils import random_lower_string

    kind_value = AIProviderKind(kind)
    if secret is None:
        secret = (
            MINTED_ADMIN_SECRET
            if kind_value == AIProviderKind.MINTED
            else DEFAULT_API_KEY
        )
    if kind_value == AIProviderKind.MINTED and project_id is None:
        project_id = f"proj_{random_lower_string()[:10]}"

    payload = AIProviderCreate(
        name=name or f"Provider {random_lower_string()[:8]}",
        kind=kind_value,
        type=credential_type,
        secret=secret,
        config=ProviderAdminCredentialConfig(
            organization_id=organization_id, project_id=project_id
        ),
        base_url=base_url,
        model=model,
        auto_provision_roles=auto_provision_roles or [],
        set_as_default=set_as_default,
        set_user_sdk_defaults=set_user_sdk_defaults,
        **(
            {"sdk_default_modes": sdk_default_modes}
            if sdk_default_modes is not None
            else {}
        ),
        default_model=default_model,
        available_models=available_models,
        model_override_conversation=model_override_conversation,
        model_override_building=model_override_building,
        expiry_notification_date=expiry_notification_date,
        target_user_ids=[uuid.UUID(str(u)) for u in (target_user_ids or [])],
    )
    provider, result = ai_providers_service.create(
        db, payload, provider_actor(db)
    )
    body = result.model_dump(mode="json")
    body["provider_id"] = str(provider.id)
    return body


def update_provider(db: Session, provider_id: str, **fields: Any) -> dict[str, Any]:
    """``AIProvidersService.update`` — a policy edit that re-applies to members.

    Returns ``{"provider": …, "result": …}``: the provider projection and the
    reconcile result the write-through produced, because a policy edit's whole
    point is what it did to existing members.
    """
    provider, result = ai_providers_service.update(
        db,
        uuid.UUID(str(provider_id)),
        AIProviderUpdate(**fields),
        provider_actor(db),
    )
    return {
        "provider": ai_providers_service.to_public(db, provider).model_dump(
            mode="json"
        ),
        "result": result.model_dump(mode="json") if result else None,
    }


def rotate_provider_key(
    db: Session, provider_id: str, api_key: str
) -> dict[str, Any]:
    """``POST /{id}/rotate-key`` as a service call. 400s on a minted provider."""
    provider, result = ai_providers_service.rotate_key(
        db, uuid.UUID(str(provider_id)), api_key, provider_actor(db)
    )
    return {
        "provider": ai_providers_service.to_public(db, provider).model_dump(
            mode="json"
        ),
        "result": result.model_dump(mode="json"),
    }


def verify_provider(db: Session, provider_id: str) -> dict[str, Any]:
    """One Verify press, run synchronously from the test thread."""
    return asyncio.run(
        ai_providers_service.verify(db, uuid.UUID(str(provider_id)))
    ).model_dump(mode="json")


def apply_provider_to_existing(
    db: Session, provider_id: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """The provider-side entry point into "apply to existing users"."""
    return ai_providers_service.apply_to_existing(
        db, uuid.UUID(str(provider_id)), provider_actor(db), dry_run=dry_run
    ).model_dump(mode="json")


def delete_provider(
    db: Session, provider_id: str, *, force: bool = False
) -> dict[str, Any]:
    """The delete gate. Raises ``AIProviderInUseError`` unless ``force``.

    ``asyncio.run`` from the test thread, the same way ``converge_keys`` drives
    the converge pass: the forced path awaits the provider revocations before it
    drops the secret they need, so the method is a coroutine.
    """
    return asyncio.run(
        ai_providers_service.delete(
            db, uuid.UUID(str(provider_id)), provider_actor(db), force=force
        )
    ).model_dump(mode="json")


def get_provider(db: Session, provider_id: str) -> dict[str, Any]:
    """``GET /admin/ai-providers/{id}`` as a service call."""
    return ai_providers_service.get(
        db, uuid.UUID(str(provider_id))
    ).model_dump(mode="json")


def strand_managed_credential(db: Session, credential_id: str) -> None:
    """NULL a managed credential's ``provider_id``, leaving it with no source.

    A deliberately unreachable state, reproduced here on purpose. It used to be
    produced by forcing a provider organisation's disconnect, which NULLed the
    pointer on every record that minted through it; migration ``c23d6b59a8f5``
    made the FK ``ON DELETE RESTRICT`` precisely to outlaw that, and
    ``AIProvidersService.delete`` deletes the credential first instead. Setting
    the column to NULL is still legal SQL — RESTRICT governs deleting the
    *provider*, not clearing a pointer — so this is now the only way to stand a
    membership in front of the "nothing to mint with" branch and check that it
    still claims an attempt rather than retrying for ever.
    """
    from app.models.credentials.managed_ai_credential import ManagedAICredential

    parent = db.get(ManagedAICredential, uuid.UUID(str(credential_id)))
    assert parent is not None, f"No managed credential {credential_id}"
    parent.provider_id = None
    db.add(parent)
    db.commit()


def orphan_provider(db: Session, credential_id: str) -> None:
    """Delete a provider's managed credential, leaving the provider standing.

    A **legacy** state, reproduced here on purpose because no route can make one
    any more. ``POST /admin/provider-admin-credentials`` used to write a provider
    with no credential and Phase 4 deleted it; the other way in — deleting the
    credential through ``/admin/llm-providers`` — is refused by
    ``ManagedAICredentialsService.delete``, precisely so a provider cannot be
    left auto-provisioning with nothing to grant through.

    Rows that predate the deletion still exist, and ``AIProvidersService`` still
    has to describe and delete them, so this is the seam that stands one up.
    """
    from app.models.credentials.managed_ai_credential import ManagedAICredential

    parent = db.get(ManagedAICredential, uuid.UUID(str(credential_id)))
    assert parent is not None, f"No managed credential {credential_id}"
    db.delete(parent)
    db.commit()


def grant_members_automatically(
    db: Session, credential_id: str, user_ids: list[str]
) -> dict[str, Any]:
    """``add_members`` with the automatic path's incumbent-wins rule.

    The seam is the **direct** observation point for the skips a grant
    produces. It is no longer the only one: Phase 3 left
    ``AccountProvisioningService`` throwing them away, and Phase 4 added
    ``ProvisioningReport.default_slot_skips``, which
    ``InvitationService`` carries onto the wire as
    ``InviteProvisioningSummary.default_slot_skips``. So a
    ``ManagedDefaultSlotSkip`` can now also be observed through an invite, one
    projection removed and only for the paths an invite takes.

    Calling ``add_members`` here still earns its place: it returns the
    ``MemberAddition`` itself, so a test can assert on the skip a *single*
    grant produced without an account-creation story around it, and without
    depending on which fields the invite summary chose to carry.

    Returns ``added`` / ``skipped`` / ``default_slot_skips`` as plain dicts.
    """
    from app.models.credentials.managed_ai_credential import ManagedAICredential
    from app.services.credentials.managed_ai_credentials_service import (
        managed_ai_credentials_service,
    )

    parent = db.get(ManagedAICredential, uuid.UUID(str(credential_id)))
    assert parent is not None, f"No managed credential {credential_id}"
    addition = managed_ai_credentials_service.add_members(
        db,
        parent=parent,
        user_ids=[uuid.UUID(str(u)) for u in user_ids],
        actor=None,
    )
    return {
        "added": [m.model_dump(mode="json") for m in addition.added],
        "skipped": [s.model_dump(mode="json") for s in addition.skipped],
        "default_slot_skips": [
            s.model_dump(mode="json") for s in addition.default_slot_skips
        ],
    }
