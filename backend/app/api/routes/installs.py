"""Install-side endpoints — publish, uninstall, apply-update, etc.

Mounted with prefix ``/agents/{agent_id}/...`` because every Agent row IS
an Install in the new model. Routes that mutate an install always validate
ownership against the current user.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep, require_developer
from app.models.agents.agent import Agent, AgentPublic
from app.models.bundles.agent_bundle_revision import (
    AgentBundleRevisionPublic,
    PublishRequest,
)
from app.models.bundles.catalog import (
    BundleCredentialDrift,
    CheckUpdatesResponse,
    EditBundleIdRequest,
    SetUpdateModeRequest,
    SetupCredentialSummary,
    SetupStatusMissingItem,
    SetupStatusResponse,
)
from app.models.credentials.credential import (
    Credential,
    CredentialPublic,
    CredentialUpdate,
)
from app.models.credentials.link_models import AgentCredentialLink
from app.models.events.event import EventType
from app.services.agents.agent_service import AgentService
from app.services.bundles.install_readiness_gate import InstallReadinessGate
from app.services.bundles.install_service import InstallError, InstallService
from app.services.bundles.publish_service import PublishService
from app.services.credentials.credentials_service import CredentialsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["installs"])


def _resolve_install_owned(session, agent_id: uuid.UUID, current_user) -> Agent:
    install = session.get(Agent, agent_id)
    if not install:
        raise HTTPException(status_code=404, detail="Agent not found")
    if (
        install.owner_id != current_user.id
        and not current_user.is_superuser
    ):
        raise HTTPException(status_code=403, detail="Not your agent")
    return install


# Phase 3 — publish, edit-bundle-id, and update-mode toggles are
# developer-only.  Uninstall, apply-update, and check-updates remain
# open to install owners regardless of role so an agent-user can keep
# their installs current after a downgrade.


# ── Publish ────────────────────────────────────────────────────


@router.post(
    "/{agent_id}/publish",
    response_model=AgentBundleRevisionPublic,
    dependencies=[Depends(require_developer)],
)
async def publish_agent(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    request: PublishRequest | None = None,
) -> AgentBundleRevisionPublic:
    """Snapshot the install workspace into a new bundle revision.

    First publish creates the ``AgentBundle`` row and promotes the install
    to ``is_publisher_install=True``. Subsequent publishes append a new
    ``AgentBundleRevision``.

    Phase 3 — restricted to ``agent-developer`` and ``admin`` roles.
    """
    install = _resolve_install_owned(session, agent_id, current_user)
    try:
        revision = await PublishService.publish(
            session=session,
            install=install,
            publisher_user_id=current_user.id,
            release_notes=request.release_notes if request else None,
            display_name=request.display_name if request else None,
            description=request.description if request else None,
            bundle_id_override=request.bundle_id if request else None,
            version=request.version if request else None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Wire response with install_count (just-published revision = 0+ installs).
    from app.api.routes.bundles import _revision_to_public
    from app.services.bundles.bundle_service import BundleService

    install_count = BundleService.revision_install_count(session, revision.id)
    return _revision_to_public(revision, install_count)


# ── Uninstall ─────────────────────────────────────────────────


@router.post("/{agent_id}/uninstall")
async def uninstall_install(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> dict:
    """Delete the install + env; mark per-user app-data orphaned (preserved)."""
    install = _resolve_install_owned(session, agent_id, current_user)
    if install.is_publisher_install:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot uninstall the publisher install. Delete the agent + "
                "bundle from the bundle management UI instead."
            ),
        )
    try:
        await InstallService.uninstall(session, install)
    except InstallError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "uninstalled"}


# ── Apply update ───────────────────────────────────────────────


@router.post("/{agent_id}/apply-update", response_model=AgentPublic)
async def apply_update(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentPublic:
    install = _resolve_install_owned(session, agent_id, current_user)
    try:
        install = await InstallService.apply_update(session, install)
    except InstallError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AgentService.to_public_with_clone_info(session, install)


@router.post("/{agent_id}/check-updates", response_model=CheckUpdatesResponse)
def check_updates(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> CheckUpdatesResponse:
    install = _resolve_install_owned(session, agent_id, current_user)
    result = InstallService.check_for_updates(session, install)
    return CheckUpdatesResponse(**result)


@router.patch("/{agent_id}/update-mode", response_model=AgentPublic)
def set_update_mode(
    agent_id: uuid.UUID,
    request: SetUpdateModeRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentPublic:
    install = _resolve_install_owned(session, agent_id, current_user)
    try:
        install = InstallService.set_update_mode(
            session=session, install=install, mode=request.update_mode
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AgentService.to_public_with_clone_info(session, install)


@router.patch(
    "/{agent_id}/bundle-id",
    response_model=AgentPublic,
    dependencies=[Depends(require_developer)],
)
def edit_bundle_id(
    agent_id: uuid.UUID,
    request: EditBundleIdRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentPublic:
    """Phase 3 — restricted to ``agent-developer`` and ``admin`` roles."""
    install = _resolve_install_owned(session, agent_id, current_user)
    install = InstallService.edit_bundle_id(
        session=session, install=install, new_bundle_id=request.bundle_id
    )
    return AgentService.to_public_with_clone_info(session, install)


# ── Phase 5 — publisher override map for credential provisioning ───


class _CredentialOverride(BaseModel):
    """Per-spec ``provided_by`` choice persisted on the publisher install."""

    provided_by: str  # "user" | "publisher" | "template"


class _AICredentialDraft(BaseModel):
    """Pre-publish draft of the bundle's publisher AI credentials.

    Stored on the publisher install while the ``AgentBundle`` row does
    not yet exist (i.e. the agent has never been published). At first
    publish, ``PublishService`` copies these UUIDs onto the new bundle
    row and they become the source of truth via the FK columns. After
    that, the picker writes directly to ``AgentBundle`` via
    ``PATCH /bundles/{uuid}`` and these draft fields stop being read.
    ``null`` explicitly means "no publisher-provided AI for this mode";
    omitting the field on update leaves the existing draft unchanged.
    """

    conversation_credential_id: uuid.UUID | None = None
    building_credential_id: uuid.UUID | None = None


class PublishSettingsUpdate(BaseModel):
    """Body of ``PATCH /agents/{agent_id}/publish-settings``.

    Only the publisher install (``is_publisher_install=True``) can hold
    publish settings. Fields are partial — omitting a top-level key
    preserves the existing value; sending it (even as empty) replaces it.

    - ``credential_overrides``: per-spec ``provided_by`` map. Keys must
      match the names of credentials currently linked to the install;
      values must use ``"user"``, ``"publisher"``, or ``"template"``.
    - ``ai_credentials``: pre-publish AI credential draft (see
      ``_AICredentialDraft``). Each id, when non-null, must reference an
      ``AICredential`` owned by the publisher.
    """

    credential_overrides: dict[str, _CredentialOverride] | None = None
    ai_credentials: _AICredentialDraft | None = None


@router.patch(
    "/{agent_id}/publish-settings",
    response_model=AgentPublic,
    dependencies=[Depends(require_developer)],
)
def update_publish_settings(
    agent_id: uuid.UUID,
    request: PublishSettingsUpdate,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentPublic:
    """Persist the publisher overrides for credential / AI provisioning.

    Lets the publisher explicitly mark each linked credential as
    ``user``, ``publisher``, or ``template``-provided, and ship a
    pre-publish AI credential draft. The publish-time spec collector
    reads this map before falling back to inference from
    ``Credential.allow_sharing`` / ``allow_template_sharing``.

    Both top-level fields are partial: omitting one preserves it,
    sending it (even as empty) replaces it.
    """
    install = _resolve_install_owned(session, agent_id, current_user)
    try:
        install = InstallService.update_publish_settings(
            session=session,
            install=install,
            credential_overrides=(
                {k: v.model_dump() for k, v in request.credential_overrides.items()}
                if request.credential_overrides is not None
                else None
            ),
            ai_credentials=(
                request.ai_credentials.model_dump()
                if request.ai_credentials is not None
                else None
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return AgentService.to_public_with_clone_info(session, install)


# ── Phase 4 — install setup gate (status + per-credential setup) ───


def _gate_result_to_response(result) -> SetupStatusResponse:
    """Marshal an internal ``GateResult`` into the public response model."""
    return SetupStatusResponse(
        status=result.status,
        missing=[
            SetupStatusMissingItem(
                spec_name=m.spec_name,
                spec_type=m.spec_type,
                reason=m.reason,
                is_ai=m.is_ai,
            )
            for m in result.missing
        ],
        setup_url=result.setup_url,
    )


@router.get("/{agent_id}/setup-status", response_model=SetupStatusResponse)
def get_setup_status(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> SetupStatusResponse:
    """Return the current install readiness verdict.

    Powers the "setup needed" banner on the install detail page and the
    setup-credentials route. Same scan as the runtime gate, minus the
    pre-rendered ``user_message`` (the frontend renders its own copy).
    """
    install = _resolve_install_owned(session, agent_id, current_user)
    result = InstallReadinessGate.check(session, install)
    return _gate_result_to_response(result)


@router.get(
    "/{agent_id}/setup-credentials",
    response_model=list[SetupCredentialSummary],
)
def list_setup_credentials(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> list[SetupCredentialSummary]:
    """List the install's user-fillable placeholder credentials.

    Returns only rows owned by the install owner AND linked to the
    install AND ``is_placeholder=True``. Publisher-shared rows are not
    listed because the installer can't fill them — those surface in
    ``setup-status`` as ``publisher_credential_*`` items instead.
    """
    install = _resolve_install_owned(session, agent_id, current_user)
    return InstallService.list_setup_credentials(session=session, install=install)


@router.get(
    "/{agent_id}/bundle-credential-drift",
    response_model=BundleCredentialDrift,
    dependencies=[Depends(require_developer)],
)
def get_bundle_credential_drift(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> BundleCredentialDrift:
    """Report live-vs-published ``provided_by`` drift for the publisher install.

    Powers the "republish to apply" hint on the bundle tab: when the
    publisher changes a credential's sharing mode after the last publish,
    installers keep receiving the previously published setting until the
    bundle is republished. Returns ``stale=False`` with an empty list when
    nothing has drifted (or the install has never published).

    Publisher-install owner-only. Returns 404 (not 403) for non-owners and
    for installs that are not publisher installs, to avoid leaking the
    existence of a bundle to non-publishers.
    """
    install = session.get(Agent, agent_id)
    if (
        install is None
        or (install.owner_id != current_user.id and not current_user.is_superuser)
        or not install.is_publisher_install
    ):
        raise HTTPException(status_code=404, detail="Agent not found")
    return PublishService.compute_credential_spec_drift(session, install)


@router.put(
    "/{agent_id}/setup-credentials/{credential_id}",
    response_model=CredentialPublic,
)
async def update_setup_credential(
    agent_id: uuid.UUID,
    credential_id: uuid.UUID,
    credential_in: CredentialUpdate,
    session: SessionDep,
    current_user: CurrentUser,
) -> CredentialPublic:
    """Fill in a placeholder credential for this install.

    Validates that the credential is:
      1. Owned by the install owner.
      2. Linked to this install via ``AgentCredentialLink``.
      3. Currently ``is_placeholder=True`` (gate will refuse to flip an
         already-real credential because there's no setup flow for it).

    On success: persists the new data via ``CredentialsService.update_credential``
    (which also flips ``is_placeholder=False`` when the saved data is
    non-empty), then re-runs the gate; if the install is now ready, emits
    ``INSTALL_SETUP_COMPLETED`` so the frontend can hide the banner.
    """
    install = _resolve_install_owned(session, agent_id, current_user)

    cred = session.get(Credential, credential_id)
    if cred is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    if cred.owner_id != install.owner_id:
        raise HTTPException(status_code=403, detail="Not your credential")

    link = session.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install.id,
            AgentCredentialLink.credential_id == credential_id,
        )
    ).first()
    if link is None:
        raise HTTPException(
            status_code=404,
            detail="Credential is not linked to this install",
        )

    if not cred.is_placeholder:
        raise HTTPException(
            status_code=409,
            detail=(
                "Credential is already set up. Edit it from the credentials "
                "page instead."
            ),
        )

    try:
        updated = await CredentialsService.update_credential(
            session=session,
            credential_id=credential_id,
            credential_in=credential_in,
            owner_id=current_user.id,
            is_superuser=current_user.is_superuser,
        )
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))

    # Re-run the gate. If we just made the install ready, emit a WS
    # notice so the install detail page can hide its setup banner
    # without polling. Best-effort — failures are logged, not raised.
    try:
        result = InstallReadinessGate.check(session, install)
        if result.status == "ready":
            from app.services.events.event_service import event_service

            await event_service.emit_event(
                event_type=EventType.INSTALL_SETUP_COMPLETED,
                model_id=install.id,
                user_id=install.owner_id,
                meta={
                    "agent_id": str(install.id),
                    "credential_id": str(credential_id),
                },
            )
    except Exception as e:  # pragma: no cover — diagnostic-only path
        logger.warning(
            "Failed to emit INSTALL_SETUP_COMPLETED for install %s: %s",
            install.id, e,
        )

    # Build the same CredentialPublic payload the credentials PUT route
    # returns so frontend code can reuse the existing type.
    credential_data = CredentialsService.decrypt_credential_data(
        session=session, credential=updated
    )
    status = CredentialsService.check_credential_completeness(
        credential_type=updated.type.value,
        credential_data=credential_data,
    )
    return CredentialPublic(
        id=updated.id,
        name=updated.name,
        type=updated.type,
        notes=updated.notes,
        allow_sharing=updated.allow_sharing,
        allow_template_sharing=updated.allow_template_sharing,
        template_private_fields=list(updated.template_private_fields or []),
        owner_id=updated.owner_id,
        user_workspace_id=updated.user_workspace_id,
        share_count=0,
        is_shared=False,
        owner_email=None,
        is_placeholder=updated.is_placeholder,
        placeholder_source_id=updated.placeholder_source_id,
        status=status,
    )
