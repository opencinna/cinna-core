"""Admin API for **AI providers** — where keys come from, and who gets one.

One superuser surface over ``ai_provider``: a provider holds a secret, the
vendor type it belongs to, the roles whose new accounts receive a key from it,
and the wiring policy applied when that happens. It owns exactly one
``managed_ai_credential``, which owns the member list and is edited on
``/admin/llm-providers``.

WHAT THIS ROUTER REPLACES
-------------------------
``/admin/provider-admin-credentials``, deleted with this module's arrival. That
surface described the same table when it held only organisation administration
secrets; the table now also holds ordinary model keys (``kind="fixed_key"``),
and the vocabulary moved with it — see §2.1 of the ai-credential-providers
plan. ``/admin/provider-adapters`` moved here as ``GET /adapters``, unchanged in
shape: an adapter describes a **type**, not a provider.

THE SECRET IS WRITE-ONLY, AND THERE IS NO REVEAL ENDPOINT
----------------------------------------------------------
``AIProviderPublic`` carries ``has_secret: bool`` and never the value, copying
``MailServerConfigPublic``. A provider's fixed key is decrypted only to write a
member's child ``AICredential``, inside ``ManagedAICredentialsService``; a
minted provider's administration secret is decrypted only by
``KeyProvisioningService``. Neither path passes through here.

Every route takes ``get_current_active_superuser`` (§4.2 — superuser-only end to
end; there is no user-facing provider surface and no user-facing projection of
one), and every mutation writes a ``SecurityEvent`` keyed to the acting admin.
Provider-side identifiers are allowed in event details — they are what makes a
provider nameable after the fact — and key material never is.
"""
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import Message, User
from app.models.credentials.managed_ai_credential import (
    ManagedAICredentialApplyResult,
)
from app.models.credentials.provider_adapter import (
    ProviderAdapterPublic,
    ProviderAdaptersPublic,
)
from app.models.credentials.provider_admin_credential import (
    AIProviderCreate,
    AIProviderPublic,
    AIProviderRotateKey,
    AIProviderUpdate,
    AIProviderVerifyResult,
)
from app.models.events.security_event import SecurityEventCreate
from app.services.ai_providers import registry
from app.services.credentials.ai_providers_service import (
    AIProviderConflictError,
    AIProviderInUseError,
    ai_providers_service,
)
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ai-providers", tags=["admin-ai-providers"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


async def _audit(
    session: SessionDep,
    admin: User,
    event_type: str,
    details: dict[str, Any],
) -> None:
    """One admin-initiated provider act, recorded. Never key material."""
    await SecurityEventService.create_event(
        session=session,
        user_id=admin.id,
        data=SecurityEventCreate(
            event_type=event_type, severity="medium", details=details
        ),
    )


def _conflict_409(exc: AIProviderConflictError) -> HTTPException:
    """Turn a ``(role, mode)`` collision into a 409 the dialog can render.

    Structured rather than a prose string: the dialog highlights the offending
    role/mode cell and links to the other provider, and it can do neither from a
    sentence.

    The envelope shape is inherited verbatim from ``admin_llm_providers.py``,
    which produced it while the rule lived on the managed credential and kept
    these handlers after the rule moved, precisely so this router would have a
    worked example to copy rather than a shape to invent. The ``code`` and the
    two ``conflicting_credential_*`` keys keep their names for that reason: the
    surface that parses them is one dialog, and renaming the keys to say
    "provider" would be a wire change with no reader asking for it.
    """
    return HTTPException(
        status_code=409,
        detail={
            "code": "auto_provision_conflict",
            "message": str(exc),
            "conflicting_credential_id": str(exc.conflicting_id),
            "conflicting_credential_name": exc.conflicting_name,
            "role": exc.role,
            "mode": exc.mode,
        },
    )


# ── Adapters ────────────────────────────────────────────────────────────────
#
# Declared before ``/{provider_id}`` so the literal wins the match. The path
# parameter is a UUID and ``adapters`` would fail to parse as one, so the order
# is belt and braces rather than the only thing holding it — but the belt is
# free and the day someone widens the parameter type is not the day to discover
# this.


@router.get("/adapters", response_model=ProviderAdaptersPublic)
def list_provider_adapters(current_user: SuperUser) -> Any:
    """Every provider **type** the server supports, as its adapter declares it.

    Derived from the registry, so a sixth type appears here the moment its
    adapter is registered — no list to update, and no way for this endpoint to
    disagree with the validation it describes.

    ``supports_minting`` is the whole of the create wizard's answer to "may
    this type be minted": a provider carries its own administration secret, so
    minting has no precondition beyond the adapter being able to do it. The
    enforcing rule is ``AIProvidersService._validate_shape``, which reads the
    same single term.
    """
    adapters = registry.all_adapters()
    data = [
        ProviderAdapterPublic(
            type=adapter.type,
            label=adapter.label,
            account_config_display_name=adapter.account_config_display_name,
            account_config_slug=adapter.account_config_slug,
            sdk_engine=adapter.sdk_engine,
            requires_base_url=adapter.requires_base_url,
            requires_model=adapter.requires_model,
            supports_model_listing=adapter.supports_model_listing,
            issues_oauth_tokens=adapter.issues_oauth_tokens,
            supports_minting=adapter.supports_minting,
            admin_config_schema=(
                adapter.key_provisioner.config_schema()
                if adapter.key_provisioner is not None
                else None
            ),
        )
        for adapter in adapters
    ]
    return ProviderAdaptersPublic(data=data, count=len(data))


# ── CRUD ────────────────────────────────────────────────────────────────────


@router.post("/", response_model=AIProviderPublic)
async def create_ai_provider(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: AIProviderCreate,
) -> Any:
    """Connect a key source and say who automatically gets one.

    Creates the provider **and its one managed credential** in a single
    transaction, then grants ``target_user_ids`` through it. Creating the
    record does not contact the provider — press Verify for that, so a provider
    outage cannot stop an admin from recording the configuration.

    409 when this configuration would take a ``(role, mode)`` default slot
    another provider already claims (§5.5's write-time rule).
    """
    try:
        provider, result = ai_providers_service.create(
            session, data, current_user
        )
    except AIProviderConflictError as exc:
        raise _conflict_409(exc)
    await _audit(
        session,
        current_user,
        "admin.ai_provider.created",
        {
            "provider_id": str(provider.id),
            "kind": provider.kind,
            "provider_type": str(
                getattr(provider.provider_type, "value", provider.provider_type)
            ),
            "auto_provision_roles": list(provider.auto_provision_roles or []),
            "member_count": len(result.added),
        },
    )
    return ai_providers_service.to_public(session, provider)


@router.get("/", response_model=list[AIProviderPublic])
def list_ai_providers(session: SessionDep, current_user: SuperUser) -> Any:
    """Every connected provider. Secrets are never included."""
    return ai_providers_service.list(session)


@router.get("/{provider_id}", response_model=AIProviderPublic)
def get_ai_provider(
    session: SessionDep, current_user: SuperUser, provider_id: uuid.UUID
) -> Any:
    return ai_providers_service.get(session, provider_id)


@router.patch("/{provider_id}", response_model=AIProviderPublic)
async def update_ai_provider(
    *,
    session: SessionDep,
    current_user: SuperUser,
    provider_id: uuid.UUID,
    data: AIProviderUpdate,
) -> Any:
    """Partial update — and a policy edit re-applies to every existing member.

    That write-through is the point of editing here rather than on the
    credential: changing ``default_model`` or a per-mode override rewrites what
    the provider's members hold, and a cleared override retracts the pins it
    wrote.

    **A null reconcile result is not an error.** ``AIProvidersService.update``
    returns ``None`` for the reconcile half when the provider owns no managed
    credential — a *lone* provider row. ``/admin/provider-admin-credentials``
    made those and this module replaces it, and the other way to make one —
    deleting the credential and leaving the provider standing — is refused by
    ``ManagedAICredentialsService.delete``. So the state is legacy-only, and
    ``ai_providers_service_test.py::test_a_provider_with_no_credential_still_projects_and_still_deletes``
    reproduces it through a test seam rather than a route.

    There is nothing to re-apply to, so the update is applied to the provider
    row and the response is the provider projection either way — the response
    model §5.7 specifies for this endpoint, and the reason the null never has to
    be represented on the wire at all. The one place it *is* visible is the
    audit row, where ``members_updated`` is ``None`` rather than ``0``: "the
    provider owns no credential" and "the edit changed nothing for its members"
    are different facts and must not be recorded as the same one.

    Answering 404 or 409 instead would refuse to *rename* such a row, which is
    the one thing an admin might reasonably want to do to one before deleting
    it.
    """
    try:
        provider, result = ai_providers_service.update(
            session, provider_id, data, current_user
        )
    except AIProviderConflictError as exc:
        raise _conflict_409(exc)
    await _audit(
        session,
        current_user,
        "admin.ai_provider.updated",
        {
            "provider_id": str(provider_id),
            "auto_provision_roles": list(provider.auto_provision_roles or []),
            # How far the policy edit reached. ``None`` when the provider owns
            # no credential — distinct from ``0``, which means it owns one and
            # the edit changed nothing for its members.
            "members_updated": None if result is None else result.updated_count,
        },
    )
    return ai_providers_service.to_public(session, provider)


@router.delete("/{provider_id}")
async def delete_ai_provider(
    session: SessionDep,
    current_user: SuperUser,
    provider_id: uuid.UUID,
    force: bool = Query(
        default=False,
        description=(
            "Delete even though people hold a key from this provider. Minted "
            "keys are revoked at the provider first."
        ),
    ),
) -> Message:
    """Disconnect a provider.

    Refused with **409** carrying an ``AIProviderDeleteImpact`` while anybody
    still holds a credential from it: named people rather than a count, because
    "3 users will lose a key" is not something an administrator can check before
    pressing. ``force=true`` revokes every minted key at the provider, then
    removes the child credentials, the memberships, the managed credential and
    the provider, in that order (§5.4).

    A provider nobody holds a key from deletes without a confirmation.
    """
    try:
        impact = await ai_providers_service.delete(
            session, provider_id, current_user, force=force
        )
    except AIProviderInUseError as in_use:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ai_provider_in_use",
                "message": str(in_use),
                "impact": in_use.impact.model_dump(mode="json"),
            },
        )
    await _audit(
        session,
        current_user,
        "admin.ai_provider.deleted",
        {
            "provider_id": str(provider_id),
            "forced": force,
            # What the force overrode, not merely that it was used. This is the
            # last place anyone can learn how many people lost a key and how
            # many keys were destroyed at the vendor.
            "member_count": impact.member_count,
            "minted_key_count": impact.minted_key_count,
        },
    )
    return Message(message="AI provider deleted successfully")


# ── Actions ─────────────────────────────────────────────────────────────────


@router.post("/{provider_id}/verify", response_model=AIProviderVerifyResult)
async def verify_ai_provider(
    session: SessionDep, current_user: SuperUser, provider_id: uuid.UUID
) -> Any:
    """Ask the provider whether this configuration still works.

    Two different questions by ``kind``, and the result says which was asked. A
    ``minted`` provider is asked whether its administration secret authenticates
    *and* whether the project carries a hard spend limit; the limit is only ever
    read, and a project carrying none is refused for minting. A ``fixed_key``
    provider has no project, so ``checked_spend_limit`` is false and the spend
    fields are not answers.
    """
    result = await ai_providers_service.verify(session, provider_id)
    await _audit(
        session,
        current_user,
        "admin.ai_provider.verified",
        {
            "provider_id": str(provider_id),
            "ok": result.ok,
            "checked_spend_limit": result.checked_spend_limit,
            "spend_limit_enforcing": result.spend_limit_enforcing,
            "error": result.error,
        },
    )
    return result


@router.post("/{provider_id}/rotate-key", response_model=AIProviderPublic)
async def rotate_ai_provider_key(
    *,
    session: SessionDep,
    current_user: SuperUser,
    provider_id: uuid.UUID,
    data: AIProviderRotateKey,
) -> Any:
    """Replace a ``fixed_key`` provider's key and re-key every member.

    **400 on a ``minted`` provider.** Its secret creates keys, it is not one of
    them, and rotating a minted member's key is a per-member re-mint. Accepting
    the request would store a key nothing reads and leave the admin believing
    they had rolled one.

    The rotation is logged as having happened. Neither the old key nor the new
    one reaches the audit row.
    """
    provider, result = ai_providers_service.rotate_key(
        session, provider_id, data.api_key, current_user
    )
    await _audit(
        session,
        current_user,
        "admin.ai_provider.rotated",
        {
            "provider_id": str(provider_id),
            "members_rekeyed": result.updated_count,
        },
    )
    return ai_providers_service.to_public(session, provider)


@router.post(
    "/{provider_id}/apply-to-existing",
    response_model=ManagedAICredentialApplyResult,
)
async def apply_ai_provider_to_existing(
    session: SessionDep,
    current_user: SuperUser,
    provider_id: uuid.UUID,
    dry_run: bool = Query(
        default=False,
        description=(
            "Return who would receive a key without granting one. Backs the "
            "confirm dialog's preview count."
        ),
    ),
) -> Any:
    """Grant this provider to every active account whose role it covers.

    ``auto_provision_roles`` only fires when an account is created, so this is
    how the people already on the instance are brought in. Add-only: nobody
    loses a credential, and existing members are left alone.

    It is one of the two deliberate admin acts that **do** overwrite a default
    somebody already holds — automatic provisioning never does (§5.5).

    A dry run writes nothing and emits no audit event; there is nothing to audit
    about a question.

    The response model is the credential's apply result rather than the plain
    reconcile result §5.7's table names: ``apply_to_existing`` returns the
    richer shape (it is a subclass), and narrowing it here would drop the
    ``candidates`` / ``defaults_overwrite_count`` fields the confirm dialog's
    preview is built from.
    """
    result = ai_providers_service.apply_to_existing(
        session, provider_id, current_user, dry_run=dry_run
    )
    if not dry_run:
        await _audit(
            session,
            current_user,
            "admin.ai_provider.applied_to_existing",
            {
                "provider_id": str(provider_id),
                "added_count": len(result.added),
                "skipped_count": len(result.skipped),
                "defaults_overwrite_count": result.defaults_overwrite_count,
            },
        )
    return result

