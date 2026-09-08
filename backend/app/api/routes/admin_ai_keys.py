"""
Admin "AI Keys" API — one resource per real API key.

WHY THIS EXISTS BESIDE ``admin_llm_providers``
----------------------------------------------
That router administers **records**: a managed AI credential, its wiring and its
member set. This one administers **keys**, and for a ``minted`` provider those
are not the same thing — one record holds one key per member, each with its own
handles at the vendor and its own child credential.

Until this existed, the only way to act on one person's key was to PATCH the
record with the entire desired member set. That is why the blast-radius gate
answers with a *list* of blocked members: no caller could ever remove fewer than
a set. Here the resource is the **membership row** — it carries the vendor
handles, names the child credential and holds the provisioning lifecycle — and
every verb addresses one by id.

WHAT A ROW IS
-------------
* a **minted** record contributes one row per member;
* a **shared** record (manual, or owned by a ``fixed_key`` provider) contributes
  exactly one row, because ``_resolve_key`` copies one secret onto every
  member's child credential — those N children are copies, not keys.

So the row count follows the number of secrets that exist at the provider, which
is the property that makes this list mean anything at company size.

NO KEY MATERIAL, EVER
---------------------
``key_reference`` publishes the project and service-account handles so a row can
be matched against the vendor's own console — the same job the service account's
``email (membership id)`` name does from the other side. The secret is never
projected, and neither is the half of an ``api_key_id`` that could be replayed.
"""
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import Message, User
from app.models.credentials.managed_ai_credential import (
    AdminAIKeyKind,
    AdminAIKeysPublic,
)
from app.models.credentials.managed_ai_credential_membership import (
    MembershipProvisioningStatus,
)
from app.models.events.security_event import SecurityEventCreate
from app.services.credentials.key_provisioning_service import (
    key_provisioning_service,
)
from app.services.credentials.managed_ai_credentials_service import (
    managed_ai_credentials_service,
)
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/ai-credentials/keys", tags=["admin-ai-keys"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.get("/", response_model=AdminAIKeysPublic)
def list_ai_keys(
    session: SessionDep,
    current_user: SuperUser,
    q: str | None = Query(
        default=None,
        description=(
            "Match the holder's email or name, or the credential's name"
        ),
    ),
    status: list[MembershipProvisioningStatus] | None = Query(default=None),
    kind: AdminAIKeyKind | None = Query(default=None),
    provider_id: uuid.UUID | None = Query(default=None),
    skip: int = 0,
    limit: int = 50,
) -> Any:
    """One page of keys, newest filters applied server-side.

    Paginated at the server because the count that grows here is **headcount**,
    not the number of records: the record list this replaces embedded every
    member of every record in one response, so its payload grew with the size of
    the company on every load.

    ``status`` is a provisioning filter and therefore excludes every shared row
    by construction — a shared key has no provisioning lifecycle, and inventing
    one for it so it could pass the filter is exactly the synthetic value the
    row DTO refuses to publish.
    """
    return managed_ai_credentials_service.list_keys(
        session,
        q=q,
        statuses=list(status) if status else None,
        kind=kind,
        provider_id=provider_id,
        skip=skip,
        limit=limit,
    )


@router.delete("/{membership_id}")
async def revoke_ai_key(
    session: SessionDep,
    current_user: SuperUser,
    membership_id: uuid.UUID,
    force: bool = False,
) -> Message:
    """Take one key away: delete the credential, then revoke it at the provider.

    The same removal a PATCH of the record's member set performs, addressed at
    one person — including the Tier-2 blast-radius gate (409 with the impact,
    unless ``force``) and the refusal while a mint is in flight.

    On a **shared** record this removes that person's copy and leaves the key
    working for everyone else, which is what removing a member has always meant
    there.
    """
    # Imported from the record router rather than reimplemented: the audit trail
    # for "an admin removed somebody's key" must not depend on which surface the
    # admin used to do it.
    from app.api.routes.admin_llm_providers import _emit_reconcile_events

    result = managed_ai_credentials_service.revoke_key(
        session, current_user, membership_id, force=force
    )
    # **On ``result.blocked`` alone, never on ``blocked and not force``.**
    # ``force`` is consumed *inside* the removal, where it lifts the Tier-2
    # blast-radius gate; a block that comes back out means the row was left
    # intact whatever was passed. The `and not force` form answers 200 "Key
    # revoked successfully" for the one block force cannot lift — an in-flight
    # mint — which is the worst of both: the key is still live and the admin
    # has been told it is gone.
    if result.blocked:
        distinct = list(dict.fromkeys(b.message for b in result.blocked))
        raise HTTPException(
            status_code=409,
            detail={
                "message": " ".join(["This key could not be removed."] + distinct),
                "blocked": [b.model_dump(mode="json") for b in result.blocked],
            },
        )
    await _emit_reconcile_events(
        session, current_user, result, "admin.managed_ai_credential.update"
    )
    return Message(message="Key revoked successfully")


@router.post("/{membership_id}/rotate")
async def rotate_ai_key(
    session: SessionDep,
    current_user: SuperUser,
    membership_id: uuid.UUID,
) -> Message:
    """Destroy this key and queue a fresh one.

    The holder is without a key until the next converge tick — at most a minute,
    the same window a deactivation followed by a reactivation already produces.
    That is stated in the surface's copy rather than hidden, because an admin who
    rotates a key during a working day should know what they are spending.

    400 for a shared key (replace it on the provider instead), 409 while a mint
    is in flight or when the credential is held by a published bundle.
    """
    membership, parent = managed_ai_credentials_service.membership_or_404(
        session, membership_id
    )
    user_id = membership.user_id
    parent_id = parent.id
    previous_ref = dict(membership.external_key_ref or {})
    key_provisioning_service.rotate_member_key(session, membership, parent)
    await SecurityEventService.create_event(
        session=session,
        user_id=user_id,
        data=SecurityEventCreate(
            event_type="admin.ai_credential.mint_requested",
            severity="medium",
            details={
                "managed_credential_id": str(parent_id),
                "target_user_id": str(user_id),
                "managed_by_id": str(current_user.id),
                # What was destroyed, named. A rotation that leaves no trace of
                # the key it replaced makes the revocation that follows
                # unattributable in the feed.
                "rotated_from": previous_ref or None,
            },
        ),
    )
    return Message(message="Key rotation queued")


@router.post("/{membership_id}/default")
def set_ai_key_as_default(
    session: SessionDep,
    current_user: SuperUser,
    membership_id: uuid.UUID,
) -> Message:
    """Make this key its holder's default for its type.

    The per-person counterpart of the record's *Set default for all*: that one
    overwrites everybody's choice, this one fixes the single person whose grant
    declined an occupied slot under the incumbent-wins rule.
    """
    managed_ai_credentials_service.set_key_as_holder_default(
        session, membership_id
    )
    return Message(message="Default updated")


@router.post("/{membership_id}/retry")
async def retry_ai_key(
    session: SessionDep,
    current_user: SuperUser,
    membership_id: uuid.UUID,
) -> Message:
    """Put a key whose minting failed back in the queue.

    ``failed`` is terminal on purpose — bounded retries that converge are what
    makes the status mean "somebody has to look at this" — but terminal must not
    mean unreachable. 400 if this key's provisioning has not failed.

    Re-homed from ``/admin/llm-providers/{id}/members/{user_id}/retry``, which is
    gone: the verb acts on one key and now addresses one, and two paths to a
    single verb is how a surface ends up calling the one nobody maintains.
    """
    membership, parent = managed_ai_credentials_service.membership_or_404(
        session, membership_id
    )
    user_id = membership.user_id
    parent_id = parent.id
    requeued = key_provisioning_service.requeue_failed_member(
        session, parent_id=parent_id, user_id=user_id
    )
    await SecurityEventService.create_event(
        session=session,
        user_id=user_id,
        data=SecurityEventCreate(
            event_type="admin.ai_credential.mint_requested",
            severity="medium",
            details={
                "managed_credential_id": str(parent_id),
                "target_user_id": str(user_id),
                "managed_by_id": str(current_user.id),
                "retry_of": requeued.last_error,
            },
        ),
    )
    return Message(message="Key creation queued again")
