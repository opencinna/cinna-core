"""
Agent Sharing Routes - API endpoints for sharing agents between users.

Owner operations:
- POST /agents/{agent_id}/shares - Share an agent
- GET /agents/{agent_id}/shares - List shares for an agent
- GET /agents/{agent_id}/clones - List clones of an agent
- DELETE /agents/{agent_id}/shares/{share_id} - Revoke a share

Recipient operations:
- GET /shares/pending - List pending shares for current user
- POST /shares/{share_id}/accept - Accept a share
- POST /shares/{share_id}/decline - Decline a share

Clone operations:
- POST /agents/{agent_id}/detach - Detach clone from parent

Update operations (Phase 4):
- POST /agents/{agent_id}/shares/push-updates - Push updates to all clones
- POST /agents/{agent_id}/apply-update - Apply pending update (for clone owners)
- GET /agents/{agent_id}/update-status - Get update status for a clone
- PATCH /agents/{agent_id}/update-mode - Set update mode for a clone
"""
from datetime import datetime
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from app.api.deps import SessionDep, CurrentUser
from app.services.agent_share_service import AgentShareService
from app.services.agent_clone_service import AgentCloneService
from app.models.agent import Agent, AgentPublic
from app.models.agent_share import (
    AgentShare,
    AgentShareCreate,
    AgentSharePublic,
    AgentSharesPublic,
    PendingSharePublic,
    PendingSharesPublic,
    CredentialRequirement,
    AICredentialRequirement,
)
from app.models.clone_update_request import (
    CloneUpdateRequest,
    CloneUpdateRequestPublic,
    CloneUpdateRequestsPublic,
    PushUpdateActionsRequest,
)
from app.models.credential import Credential
from app.models.environment import AgentEnvironment
from app.models.link_models import AgentCredentialLink
from app.models.user import User
from app.services.ai_credentials_service import ai_credentials_service
from app.services.environment_service import SDK_TO_CREDENTIAL_TYPE

router = APIRouter(tags=["agent-shares"])


# Request/Response models
class AICredentialSelections(BaseModel):
    """AI credential selections for accepting a share."""
    conversation_credential_id: UUID | None = None
    building_credential_id: UUID | None = None


class AcceptShareRequest(BaseModel):
    """Request body for accepting a share."""
    credentials: dict | None = None  # {credential_id: {field: value}}
    ai_credential_selections: AICredentialSelections | None = None


class RevokeResponse(BaseModel):
    """Response for revoke action."""
    status: str
    action: str


class DeclineResponse(BaseModel):
    """Response for decline action."""
    status: str


class PushUpdatesResponse(BaseModel):
    """Response for push updates action."""
    clones_queued: int
    clones_auto_updated: int
    clones_pending_manual: int


class UpdateStatusResponse(BaseModel):
    """Response for update status check."""
    has_pending_update: bool
    pending_since: datetime | None
    last_sync_at: datetime | None
    update_mode: str | None
    parent_exists: bool
    parent_name: str | None


class SetUpdateModeRequest(BaseModel):
    """Request body for setting update mode."""
    update_mode: str  # "automatic" | "manual"


# ============ OWNER OPERATIONS ============

@router.post("/agents/{agent_id}/shares", response_model=AgentSharePublic)
async def share_agent(
    agent_id: UUID,
    share_data: AgentShareCreate,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Share an agent with another user (by email).

    Creates a pending share that the recipient can accept or decline.

    - Agent must not be a clone
    - Target user must exist
    - Cannot share with yourself
    - If providing AI credentials, they must be owned by you
    """
    share = await AgentShareService.share_agent(
        session=session,
        agent_id=agent_id,
        owner_id=current_user.id,
        shared_with_email=share_data.shared_with_email,
        share_mode=share_data.share_mode,
        provide_ai_credentials=share_data.provide_ai_credentials,
        conversation_ai_credential_id=share_data.conversation_ai_credential_id,
        building_ai_credential_id=share_data.building_ai_credential_id
    )
    return _share_to_public(session, share)


@router.get("/agents/{agent_id}/shares", response_model=AgentSharesPublic)
async def get_agent_shares(
    agent_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Get all shares for an agent you own.
    """
    shares = await AgentShareService.get_agent_shares(
        session=session,
        agent_id=agent_id,
        owner_id=current_user.id
    )
    return AgentSharesPublic(
        data=[_share_to_public(session, s) for s in shares],
        count=len(shares)
    )


@router.get("/agents/{agent_id}/clones", response_model=list[AgentPublic])
async def get_agent_clones(
    agent_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Get all clones of an agent you own.
    """
    clones = await AgentShareService.get_agent_clones(
        session=session,
        agent_id=agent_id,
        owner_id=current_user.id
    )
    return [_agent_to_public(session, c) for c in clones]


@router.delete("/agents/{agent_id}/shares/{share_id}")
async def revoke_share(
    agent_id: UUID,
    share_id: UUID,
    action: Literal["delete", "detach", "remove"] = Query(..., description="What to do with the clone"),
    session: SessionDep = None,
    current_user: CurrentUser = None
) -> RevokeResponse:
    """
    Revoke a share or remove a share record.

    action=delete: Delete the clone and all its data
    action=detach: Clone becomes independent (user keeps it)
    action=remove: Remove share record from database (only for terminal states: deleted, declined, revoked)
    """
    if action == "remove":
        await AgentShareService.delete_share_record(
            session=session,
            share_id=share_id,
            owner_id=current_user.id
        )
    else:
        await AgentShareService.revoke_share(
            session=session,
            share_id=share_id,
            owner_id=current_user.id,
            action=action
        )
    return RevokeResponse(status="ok", action=action)


# ============ RECIPIENT OPERATIONS ============

@router.get("/shares/pending", response_model=PendingSharesPublic)
async def get_pending_shares(
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Get all pending shares for the current user.

    These are agents shared with you that you haven't accepted yet.
    """
    shares = await AgentShareService.get_pending_shares(
        session=session,
        user_id=current_user.id
    )
    return PendingSharesPublic(
        data=[_share_to_pending_public(session, s) for s in shares],
        count=len(shares)
    )


@router.post("/shares/{share_id}/accept", response_model=AgentPublic)
async def accept_share(
    share_id: UUID,
    request: AcceptShareRequest | None = None,
    session: SessionDep = None,
    current_user: CurrentUser = None
):
    """
    Accept a pending share and create your clone.

    Optionally provide:
    - credential values for non-shareable credentials
    - ai_credential_selections if the share doesn't provide AI credentials
    """
    credentials = request.credentials if request else None
    ai_selections = None
    if request and request.ai_credential_selections:
        ai_selections = {
            "conversation_credential_id": request.ai_credential_selections.conversation_credential_id,
            "building_credential_id": request.ai_credential_selections.building_credential_id
        }
    clone = await AgentShareService.accept_share(
        session=session,
        share_id=share_id,
        recipient_id=current_user.id,
        credentials_data=credentials,
        ai_credential_selections=ai_selections
    )
    return _agent_to_public(session, clone)


@router.post("/shares/{share_id}/decline")
async def decline_share(
    share_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
) -> DeclineResponse:
    """
    Decline a pending share.
    """
    await AgentShareService.decline_share(
        session=session,
        share_id=share_id,
        recipient_id=current_user.id
    )
    return DeclineResponse(status="declined")


# ============ CLONE OPERATIONS ============

@router.post("/agents/{agent_id}/detach", response_model=AgentPublic)
async def detach_clone(
    agent_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Detach your clone from its parent.

    The clone becomes an independent agent that you fully own.
    You can then modify it freely and even share it with others.
    """
    agent = await AgentCloneService.detach_clone(
        session=session,
        clone_id=agent_id,
        clone_owner_id=current_user.id
    )
    return _agent_to_public(session, agent)


# ============ HELPERS ============

def _share_to_public(session, share: AgentShare) -> AgentSharePublic:
    """Convert AgentShare to public representation with resolved emails."""
    # Resolve agent name
    original_agent = session.get(Agent, share.original_agent_id)
    original_agent_name = original_agent.name if original_agent else "Unknown"

    # Resolve user emails
    shared_with_user = session.get(User, share.shared_with_user_id)
    shared_by_user = session.get(User, share.shared_by_user_id)

    return AgentSharePublic(
        id=share.id,
        original_agent_id=share.original_agent_id,
        original_agent_name=original_agent_name,
        share_mode=share.share_mode,
        status=share.status,
        shared_at=share.shared_at,
        accepted_at=share.accepted_at,
        shared_with_email=shared_with_user.email if shared_with_user else "Unknown",
        shared_by_email=shared_by_user.email if shared_by_user else "Unknown",
        cloned_agent_id=share.cloned_agent_id
    )


def _share_to_pending_public(session, share: AgentShare) -> PendingSharePublic:
    """Convert to pending share with agent details and credential requirements."""
    # Get original agent
    original_agent = session.get(Agent, share.original_agent_id)

    # Get shared_by user
    shared_by_user = session.get(User, share.shared_by_user_id)

    # Get credentials info for acceptance wizard
    credentials_required = []
    if original_agent:
        # Get credentials linked to the original agent
        stmt = select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == original_agent.id
        )
        links = session.exec(stmt).all()

        for link in links:
            cred = session.get(Credential, link.credential_id)
            if cred:
                credentials_required.append(
                    CredentialRequirement(
                        name=cred.name,
                        type=cred.type.value if hasattr(cred.type, 'value') else str(cred.type),
                        allow_sharing=cred.allow_sharing
                    )
                )

    # Get AI credential info
    ai_credentials_provided = share.provide_ai_credentials
    conversation_ai_credential_name = None
    building_ai_credential_name = None
    required_ai_credential_types = []

    # Get provided credential names
    if share.conversation_ai_credential_id:
        info = ai_credentials_service.get_credential_public_info(session, share.conversation_ai_credential_id)
        if info:
            conversation_ai_credential_name = info[0]

    if share.building_ai_credential_id:
        info = ai_credentials_service.get_credential_public_info(session, share.building_ai_credential_id)
        if info:
            building_ai_credential_name = info[0]

    # Determine required AI credential types based on agent's active environment SDKs
    if original_agent and original_agent.active_environment_id:
        env = session.get(AgentEnvironment, original_agent.active_environment_id)
        if env:
            sdk_conversation = env.agent_sdk_conversation
            sdk_building = env.agent_sdk_building

            # Conversation SDK type
            if sdk_conversation and sdk_conversation in SDK_TO_CREDENTIAL_TYPE:
                cred_type = SDK_TO_CREDENTIAL_TYPE[sdk_conversation]
                required_ai_credential_types.append(
                    AICredentialRequirement(
                        sdk_type=cred_type.value,
                        purpose="conversation"
                    )
                )

            # Building SDK type (only if share_mode is "builder" and different from conversation)
            if share.share_mode == "builder" and sdk_building and sdk_building in SDK_TO_CREDENTIAL_TYPE:
                cred_type = SDK_TO_CREDENTIAL_TYPE[sdk_building]
                # Check if we already added this type for conversation
                existing_types = [r.sdk_type for r in required_ai_credential_types]
                if cred_type.value not in existing_types:
                    required_ai_credential_types.append(
                        AICredentialRequirement(
                            sdk_type=cred_type.value,
                            purpose="building"
                        )
                    )

    return PendingSharePublic(
        id=share.id,
        original_agent_id=share.original_agent_id,
        original_agent_name=original_agent.name if original_agent else "Unknown",
        original_agent_description=original_agent.description if original_agent else None,
        share_mode=share.share_mode,
        shared_at=share.shared_at,
        shared_by_email=shared_by_user.email if shared_by_user else "Unknown",
        shared_by_name=shared_by_user.full_name if shared_by_user else None,
        credentials_required=credentials_required,
        ai_credentials_provided=ai_credentials_provided,
        conversation_ai_credential_name=conversation_ai_credential_name,
        building_ai_credential_name=building_ai_credential_name,
        required_ai_credential_types=required_ai_credential_types
    )


def _agent_to_public(session, agent: Agent) -> AgentPublic:
    """Convert Agent to public representation with clone info resolved."""
    # Resolve parent agent name and shared_by email if this is a clone
    parent_agent_name = None
    shared_by_email = None

    if agent.is_clone and agent.parent_agent_id:
        parent = session.get(Agent, agent.parent_agent_id)
        if parent:
            parent_agent_name = parent.name

            # Get the share record to find who shared it
            stmt = select(AgentShare).where(
                AgentShare.cloned_agent_id == agent.id
            )
            share = session.exec(stmt).first()
            if share:
                shared_by_user = session.get(User, share.shared_by_user_id)
                if shared_by_user:
                    shared_by_email = shared_by_user.email

    return AgentPublic(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        workflow_prompt=agent.workflow_prompt,
        entrypoint_prompt=agent.entrypoint_prompt,
        refiner_prompt=agent.refiner_prompt,
        is_active=agent.is_active,
        active_environment_id=agent.active_environment_id,
        ui_color_preset=agent.ui_color_preset,
        show_on_dashboard=agent.show_on_dashboard,
        conversation_mode_ui=agent.conversation_mode_ui,
        agent_sdk_config=agent.agent_sdk_config,
        a2a_config=agent.a2a_config,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        owner_id=agent.owner_id,
        user_workspace_id=agent.user_workspace_id,
        # Clone info
        is_clone=agent.is_clone,
        clone_mode=agent.clone_mode,
        update_mode=agent.update_mode,
        pending_update=agent.pending_update,
        parent_agent_id=agent.parent_agent_id,
        parent_agent_name=parent_agent_name,
        shared_by_email=shared_by_email
    )


# ============ UPDATE OPERATIONS (PHASE 4) ============

@router.post("/agents/{agent_id}/shares/push-updates", response_model=PushUpdatesResponse)
async def push_updates_to_clones(
    agent_id: UUID,
    request: PushUpdateActionsRequest | None = None,
    session: SessionDep = None,
    current_user: CurrentUser = None
):
    """
    Push updates to all clones of an agent.

    Optionally specify actions to perform:
    - copy_files_folder: Copy the files folder to clones
    - rebuild_environment: Rebuild the environment for clones

    Queues updates for all clones. Automatic mode clones will receive
    updates immediately. Manual mode clones will show "Update Available".

    Returns count of clones updated.
    """
    copy_files_folder = request.copy_files_folder if request else False
    rebuild_environment = request.rebuild_environment if request else False

    result = await AgentCloneService.push_updates(
        session=session,
        original_agent_id=agent_id,
        owner_id=current_user.id,
        copy_files_folder=copy_files_folder,
        rebuild_environment=rebuild_environment
    )
    return PushUpdatesResponse(**result)


@router.post("/agents/{agent_id}/apply-update", response_model=AgentPublic)
async def apply_update(
    agent_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Apply pending update from parent to your clone.

    Syncs scripts, prompts, and knowledge from parent.
    Your sessions and files are preserved.
    """
    clone = await AgentCloneService.apply_update(
        session=session,
        clone_id=agent_id,
        clone_owner_id=current_user.id
    )
    return _agent_to_public(session, clone)


@router.get("/agents/{agent_id}/update-status", response_model=UpdateStatusResponse)
async def get_update_status(
    agent_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Get update status for a clone.

    Returns information about pending updates and last sync time.
    """
    result = await AgentCloneService.get_update_status(
        session=session,
        clone_id=agent_id,
        clone_owner_id=current_user.id
    )
    return UpdateStatusResponse(**result)


@router.patch("/agents/{agent_id}/update-mode", response_model=AgentPublic)
async def set_update_mode(
    agent_id: UUID,
    request: SetUpdateModeRequest,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Set update mode for your clone.

    automatic: Updates applied automatically
    manual: Updates require manual approval
    """
    clone = await AgentCloneService.set_update_mode(
        session=session,
        clone_id=agent_id,
        clone_owner_id=current_user.id,
        update_mode=request.update_mode
    )
    return _agent_to_public(session, clone)


# ============ UPDATE REQUEST OPERATIONS ============

@router.get("/agents/{agent_id}/update-requests", response_model=CloneUpdateRequestsPublic)
async def get_pending_update_requests(
    agent_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Get pending update requests for a clone.

    Returns list of update requests that have been pushed from the parent
    and are waiting to be applied or dismissed.
    """
    requests = await AgentCloneService.get_pending_update_requests(
        session=session,
        clone_id=agent_id,
        clone_owner_id=current_user.id
    )
    return CloneUpdateRequestsPublic(
        data=[_update_request_to_public(session, r) for r in requests],
        count=len(requests)
    )


@router.post("/update-requests/{request_id}/dismiss", response_model=CloneUpdateRequestPublic)
async def dismiss_update_request(
    request_id: UUID,
    session: SessionDep,
    current_user: CurrentUser
):
    """
    Dismiss an update request.

    The update request will be marked as dismissed and the pending update
    flag on the clone will be cleared if there are no other pending requests.
    """
    request = await AgentCloneService.dismiss_update_request(
        session=session,
        request_id=request_id,
        clone_owner_id=current_user.id
    )
    return _update_request_to_public(session, request)


def _update_request_to_public(session, request: CloneUpdateRequest) -> CloneUpdateRequestPublic:
    """Convert CloneUpdateRequest to public representation."""
    # Resolve parent agent name
    parent_agent = session.get(Agent, request.parent_agent_id)
    parent_agent_name = parent_agent.name if parent_agent else None

    # Resolve pushed_by email
    pushed_by_user = session.get(User, request.pushed_by_user_id)
    pushed_by_email = pushed_by_user.email if pushed_by_user else None

    return CloneUpdateRequestPublic(
        id=request.id,
        clone_agent_id=request.clone_agent_id,
        parent_agent_id=request.parent_agent_id,
        parent_agent_name=parent_agent_name,
        pushed_by_email=pushed_by_email,
        copy_files_folder=request.copy_files_folder,
        rebuild_environment=request.rebuild_environment,
        status=request.status,
        created_at=request.created_at,
        applied_at=request.applied_at,
        dismissed_at=request.dismissed_at
    )
