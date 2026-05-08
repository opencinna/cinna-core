"""Bundle CRUD + revisions + grants — publisher-facing endpoints.

Phase 3 — every mutating endpoint here is gated on the
``require_developer`` dependency.  Read endpoints (``GET /bundles/...``)
keep visibility-aware checks inline so granted catalog viewers can see
bundles they have access to without needing the developer role.

Routes are thin wrappers around ``BundleService``: they extract request
parameters, call the service, translate ``BundleError`` subclasses to
HTTP exceptions via ``_handle_bundle_error``, and return response models.
Ownership / 403 enforcement lives in ``BundleService.get_for_publisher``,
not here.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep, require_developer
from app.models.bundles.agent_bundle import (
    AgentBundle,
    AgentBundlePublic,
    AgentBundlesPublic,
    AgentBundleUpdate,
)
from app.models.bundles.agent_bundle_revision import (
    AgentBundleRevision,
    AgentBundleRevisionPublic,
    AgentBundleRevisionsPublic,
)
from app.models.bundles.bundle_access_grant import (
    BundleAccessGrant,
    BundleAccessGrantCreate,
    BundleAccessGrantPublic,
    BundleAccessGrantsPublic,
)
from app.models.users.user import User
from app.services.bundles.bundle_service import BundleService
from app.services.bundles.catalog_service import CatalogService
from app.services.bundles.exceptions import BundleError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bundles", tags=["bundles"])


def _handle_bundle_error(e: BundleError) -> HTTPException:
    """Translate a domain exception to its HTTP equivalent."""
    return HTTPException(status_code=e.http_status, detail=str(e))


def _bundle_to_public(session, bundle: AgentBundle) -> AgentBundlePublic:
    install_count = BundleService.install_count(session, bundle.id)
    latest_rev_number = None
    if bundle.latest_revision_id:
        rev = session.get(AgentBundleRevision, bundle.latest_revision_id)
        if rev:
            latest_rev_number = rev.revision_number
    publisher_handle = (
        f"{str(bundle.publisher_user_id)[:8]}…"
        if bundle.publisher_user_id else None
    )
    return AgentBundlePublic(
        id=bundle.id,
        bundle_id=bundle.bundle_id,
        display_name=bundle.display_name,
        description=bundle.description,
        publisher_user_id=bundle.publisher_user_id,
        publisher_handle=publisher_handle,
        latest_revision_id=bundle.latest_revision_id,
        latest_revision_number=latest_rev_number,
        is_listed=bundle.is_listed,
        visibility=bundle.visibility,
        default_install_mode=bundle.default_install_mode,
        install_count=install_count,
        publisher_ai_credential_conversation_id=(
            bundle.publisher_ai_credential_conversation_id
        ),
        publisher_ai_credential_building_id=(
            bundle.publisher_ai_credential_building_id
        ),
        created_at=bundle.created_at,
        updated_at=bundle.updated_at,
    )


def _revision_to_public(
    revision: AgentBundleRevision, install_count: int
) -> AgentBundleRevisionPublic:
    return AgentBundleRevisionPublic(
        id=revision.id,
        bundle_id=revision.bundle_id,
        revision_number=revision.revision_number,
        version=revision.version,
        manifest=revision.manifest,
        content_hash=revision.content_hash,
        workflow_prompt=revision.workflow_prompt,
        entrypoint_prompt=revision.entrypoint_prompt,
        refiner_prompt=revision.refiner_prompt,
        agent_sdk_building=revision.agent_sdk_building,
        agent_sdk_conversation=revision.agent_sdk_conversation,
        model_override_building=revision.model_override_building,
        model_override_conversation=revision.model_override_conversation,
        required_credential_specs=revision.required_credential_specs or [],
        published_by_user_id=revision.published_by_user_id,
        published_at=revision.published_at,
        release_notes=revision.release_notes,
        install_count=install_count,
    )


def _grant_to_public(session, grant: BundleAccessGrant) -> BundleAccessGrantPublic:
    user = session.get(User, grant.user_id)
    return BundleAccessGrantPublic(
        id=grant.id,
        bundle_id=grant.bundle_id,
        user_id=grant.user_id,
        user_email=user.email if user else None,
        granted_by_user_id=grant.granted_by_user_id,
        created_at=grant.created_at,
    )


# ── Bundle CRUD ─────────────────────────────────────────────────


@router.get(
    "/",
    response_model=AgentBundlesPublic,
    dependencies=[Depends(require_developer)],
)
def list_bundles(
    session: SessionDep, current_user: CurrentUser
) -> AgentBundlesPublic:
    """List bundles owned by the current user.

    Phase 3 — developer-only.  ``agent-user`` accounts have no
    bundles to list; this endpoint is part of the publisher UI.
    """
    bundles = BundleService.list_publisher_bundles(session, current_user.id)
    return AgentBundlesPublic(
        data=[_bundle_to_public(session, b) for b in bundles],
        count=len(bundles),
    )


@router.get("/{bundle_uuid}", response_model=AgentBundlePublic)
def get_bundle(
    bundle_uuid: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentBundlePublic:
    """Bundle detail (publisher only for unlisted; visibility-aware otherwise)."""
    bundle = BundleService.get_bundle_by_uuid(session, bundle_uuid)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Bundle not found")
    is_publisher = bundle.publisher_user_id == current_user.id
    if not is_publisher and not CatalogService.user_can_see(session, bundle, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to view this bundle")
    return _bundle_to_public(session, bundle)


@router.patch(
    "/{bundle_uuid}",
    response_model=AgentBundlePublic,
    dependencies=[Depends(require_developer)],
)
def update_bundle(
    bundle_uuid: uuid.UUID,
    data: AgentBundleUpdate,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentBundlePublic:
    """Phase 3 — developer-only."""
    try:
        bundle = BundleService.get_for_publisher(session, bundle_uuid, current_user)
        bundle = BundleService.update_bundle(session, bundle, data)
    except BundleError as e:
        raise _handle_bundle_error(e)
    return _bundle_to_public(session, bundle)


@router.delete("/{bundle_uuid}", dependencies=[Depends(require_developer)])
def delete_bundle(
    bundle_uuid: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> dict:
    """Phase 3 — developer-only."""
    try:
        bundle = BundleService.get_for_publisher(session, bundle_uuid, current_user)
        BundleService.delete_bundle(session, bundle)
    except BundleError as e:
        raise _handle_bundle_error(e)
    return {"status": "deleted"}


# ── Revisions ───────────────────────────────────────────────────


@router.get(
    "/{bundle_uuid}/revisions",
    response_model=AgentBundleRevisionsPublic,
    dependencies=[Depends(require_developer)],
)
def list_revisions(
    bundle_uuid: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentBundleRevisionsPublic:
    try:
        bundle = BundleService.get_for_publisher(session, bundle_uuid, current_user)
    except BundleError as e:
        raise _handle_bundle_error(e)
    pairs = BundleService.list_revisions_with_install_counts(session, bundle)
    return AgentBundleRevisionsPublic(
        data=[_revision_to_public(rev, count) for rev, count in pairs],
        count=len(pairs),
    )


@router.delete(
    "/{bundle_uuid}/revisions/{revision_id}",
    dependencies=[Depends(require_developer)],
)
def delete_revision(
    bundle_uuid: uuid.UUID,
    revision_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> dict:
    """Delete one revision.

    Allowed only when no foreign install references the revision. The
    publisher's own working install is automatically detached and the
    bundle's ``latest_revision_id`` is rewired to the previous revision when
    needed. The on-disk snapshot tree is cleaned up best-effort.
    """
    try:
        bundle = BundleService.get_for_publisher(session, bundle_uuid, current_user)
        BundleService.delete_revision(session, bundle, revision_id)
    except BundleError as e:
        raise _handle_bundle_error(e)
    return {"status": "deleted"}


# ── Grants ──────────────────────────────────────────────────────


@router.get(
    "/{bundle_uuid}/grants",
    response_model=BundleAccessGrantsPublic,
    dependencies=[Depends(require_developer)],
)
def list_grants(
    bundle_uuid: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> BundleAccessGrantsPublic:
    try:
        bundle = BundleService.get_for_publisher(session, bundle_uuid, current_user)
    except BundleError as e:
        raise _handle_bundle_error(e)
    grants = BundleService.list_grants(session, bundle)
    return BundleAccessGrantsPublic(
        data=[_grant_to_public(session, g) for g in grants],
        count=len(grants),
    )


@router.post(
    "/{bundle_uuid}/grants",
    response_model=BundleAccessGrantPublic,
    dependencies=[Depends(require_developer)],
)
def add_grant(
    bundle_uuid: uuid.UUID,
    data: BundleAccessGrantCreate,
    session: SessionDep,
    current_user: CurrentUser,
) -> BundleAccessGrantPublic:
    """Phase 3 — developer-only."""
    try:
        bundle = BundleService.get_for_publisher(session, bundle_uuid, current_user)
    except BundleError as e:
        raise _handle_bundle_error(e)
    target_stmt = select(User).where(User.email == data.email.strip().lower())
    target = session.exec(target_stmt).first()
    if not target:
        raise HTTPException(
            status_code=404,
            detail="No user with that email exists on this instance",
        )
    if target.id == bundle.publisher_user_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot grant access to the bundle's own publisher",
        )
    grant = BundleService.grant_access(
        session=session,
        bundle=bundle,
        target_user=target,
        granted_by_user_id=current_user.id,
    )
    return _grant_to_public(session, grant)


@router.delete(
    "/{bundle_uuid}/grants/{grant_id}",
    dependencies=[Depends(require_developer)],
)
def revoke_grant(
    bundle_uuid: uuid.UUID,
    grant_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> dict:
    """Phase 3 — developer-only."""
    try:
        bundle = BundleService.get_for_publisher(session, bundle_uuid, current_user)
        BundleService.revoke_grant(session, bundle, grant_id)
    except BundleError as e:
        raise _handle_bundle_error(e)
    return {"status": "revoked"}
