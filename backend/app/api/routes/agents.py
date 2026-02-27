import uuid
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import select

from app.api.deps import CurrentUser, SessionDep

logger = logging.getLogger(__name__)
from app.models import (
    Agent,
    AgentCreate,
    AgentPublic,
    AgentsPublic,
    AgentUpdate,
    AgentCredentialLinkRequest,
    AgentCreateFlowRequest,
    AgentCreateFlowResponse,
    AgentSdkConfig,
    AllowedToolsUpdate,
    PendingToolsResponse,
    Message,
    Credential,
    CredentialPublic,
    CredentialsPublic,
    AgentEnvironment,
    AgentEnvironmentCreate,
    AgentEnvironmentPublic,
    AgentEnvironmentsPublic,
    ScheduleRequest,
    ScheduleResponse,
    SaveScheduleRequest,
    AgentSchedulePublic,
    AgentHandoverConfig,
    HandoverConfigCreate,
    HandoverConfigUpdate,
    HandoverConfigPublic,
    HandoverConfigsPublic,
    GenerateHandoverPromptRequest,
    GenerateHandoverPromptResponse,
    CreateAgentTaskRequest,
    CreateAgentTaskResponse,
    ExecuteHandoverRequest,
    ExecuteHandoverResponse,
    UpdateSessionStateRequest,
    UpdateSessionStateResponse,
    RespondToTaskRequest,
    AgentShare,
    User,
    InputTask,
    Session as ChatSession,
)
from app.services.environment_service import EnvironmentService
from app.services.agent_service import AgentService
from app.services.credentials_service import CredentialsService
from app.services.message_service import MessageService
from app.services.ai_functions_service import AIFunctionsService
from app.services.agent_scheduler_service import AgentSchedulerService
from app.services.session_service import SessionService
from app.models.session import SessionCreate

router = APIRouter(prefix="/agents", tags=["agents"])


def _agent_to_public_with_clone_info(session, agent: Agent) -> AgentPublic:
    """Convert Agent to AgentPublic with resolved clone information."""
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
        example_prompts=agent.example_prompts,
        inactivity_period_limit=agent.inactivity_period_limit,
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


@router.get("/", response_model=AgentsPublic)
def read_agents(
    session: SessionDep,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 100,
    user_workspace_id: str | None = None,
) -> Any:
    """
    Retrieve agents. Optionally filter by workspace.
    - If user_workspace_id is not provided (None): returns all agents
    - If user_workspace_id is empty string (""): filters for default workspace (NULL)
    - If user_workspace_id is a UUID string: filters for that workspace
    """
    # Parse workspace filter
    workspace_filter: uuid.UUID | None = None
    apply_filter = False

    if user_workspace_id is None:
        # Parameter not provided - return all agents
        apply_filter = False
    elif user_workspace_id == "":
        # Empty string means default workspace (NULL in database)
        workspace_filter = None
        apply_filter = True
    else:
        # Parse as UUID
        try:
            workspace_filter = uuid.UUID(user_workspace_id)
            apply_filter = True
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid workspace ID format")

    # Use service to list agents
    agents, count = AgentService.list_agents(
        session=session,
        user_id=current_user.id,
        skip=skip,
        limit=limit,
        workspace_filter=workspace_filter,
        apply_workspace_filter=apply_filter,
    )

    # Convert to public format with clone info
    agents_public = [_agent_to_public_with_clone_info(session, a) for a in agents]
    return AgentsPublic(data=agents_public, count=count)


@router.get("/{id}", response_model=AgentPublic)
def read_agent(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """
    Get agent by ID with environment details.

    For clones: includes parent agent info and update status.
    """
    agent = AgentService.get_agent_with_environment(session=session, agent_id=id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Return with clone info resolved
    return _agent_to_public_with_clone_info(session, agent)


@router.post("/", response_model=AgentPublic)
async def create_agent(
    *, session: SessionDep, current_user: CurrentUser, agent_in: AgentCreate
) -> Any:
    """
    Create new agent with default environment.
    """
    agent = await AgentService.create_agent(
        session=session, user_id=current_user.id, data=agent_in, user=current_user
    )
    return agent


@router.post("/create-flow", response_model=AgentCreateFlowResponse)
async def create_agent_with_flow(
    *, session: SessionDep, current_user: CurrentUser, request: AgentCreateFlowRequest
) -> Any:
    """
    Initiate agent creation flow (agent + environment + session).
    This endpoint starts the process and returns immediately.
    Use the /create-flow-stream endpoint to monitor progress.
    """
    async def event_generator():
        async for event in AgentService.create_agent_flow(
            session=session,
            user=current_user,
            description=request.description,
            mode=request.mode,
            auto_create_session=request.auto_create_session,
            user_workspace_id=request.user_workspace_id,
            agent_sdk_conversation=request.agent_sdk_conversation,
            agent_sdk_building=request.agent_sdk_building,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@router.put("/{id}", response_model=AgentPublic)
def update_agent(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    agent_in: AgentUpdate,
) -> Any:
    """
    Update an agent.

    For "user" mode clones: Only interface settings can be modified
    (ui_color_preset, show_on_dashboard, conversation_mode_ui, update_mode).
    For "builder" mode clones: Full modification allowed.
    For non-clones: Normal owner access.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Clone restrictions for "user" mode
    if agent.is_clone and agent.clone_mode == "user":
        # Only allow interface settings and update_mode for user mode clones
        allowed_fields = {
            "ui_color_preset",
            "show_on_dashboard",
            "conversation_mode_ui",
            "update_mode",
            "is_active"
        }
        update_dict = agent_in.model_dump(exclude_unset=True)

        for field in update_dict.keys():
            if field not in allowed_fields:
                raise HTTPException(
                    status_code=403,
                    detail=f"User mode clones cannot modify '{field}'. Only interface settings allowed."
                )

    updated_agent = AgentService.update_agent(
        session=session, agent_id=id, data=agent_in
    )
    if not updated_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _agent_to_public_with_clone_info(session, updated_agent)


@router.post("/{id}/sync-prompts", response_model=Message)
async def sync_agent_prompts(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """
    Sync agent prompts to active environment.

    When user manually edits workflow_prompt, entrypoint_prompt, or refiner_prompt in the backend,
    this endpoint pushes those changes to the active environment's docs files.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Check if agent has active environment
    if not agent.active_environment_id:
        raise HTTPException(
            status_code=400,
            detail="Agent has no active environment. Cannot sync prompts."
        )

    # Get active environment
    environment = session.get(AgentEnvironment, agent.active_environment_id)
    if not environment:
        raise HTTPException(status_code=404, detail="Active environment not found")

    # Check environment is running
    if environment.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Environment is not running (status: {environment.status}). Start the environment before syncing prompts."
        )

    # Sync prompts to environment
    try:
        await EnvironmentService.sync_agent_prompts_to_environment(
            environment=environment,
            workflow_prompt=agent.workflow_prompt,
            entrypoint_prompt=agent.entrypoint_prompt,
            refiner_prompt=agent.refiner_prompt
        )
        return Message(message="Agent prompts synced to environment successfully")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to sync prompts to environment: {str(e)}"
        )


@router.delete("/{id}")
async def delete_agent(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Delete an agent and cleanup all associated resources (environments, containers).

    If this agent has clones (is a parent), all clones are detached first
    (they become independent agents).
    Clone owners can delete their own clones.
    If deleting a clone, the corresponding share record is updated to 'deleted' status.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # If this agent is a clone, update the share record status to 'deleted'
    if agent.is_clone:
        stmt = select(AgentShare).where(AgentShare.cloned_agent_id == id)
        share = session.exec(stmt).first()
        if share:
            share.status = "deleted"
            share.cloned_agent_id = None  # Clear the reference since clone is being deleted
            session.add(share)
            logger.info(f"Updated share {share.id} status to 'deleted' for deleted clone {id}")
            session.commit()

    # If this agent has clones (is a parent), detach them first
    if not agent.is_clone:
        stmt = select(Agent).where(Agent.parent_agent_id == id)
        clones = session.exec(stmt).all()
        for clone in clones:
            clone.is_clone = False
            clone.parent_agent_id = None
            clone.clone_mode = None
            clone.pending_update = False
            clone.pending_update_at = None
            session.add(clone)
            logger.info(f"Detached clone {clone.id} from deleted parent {id}")

        if clones:
            session.commit()

    success = await AgentService.delete_agent(session=session, agent_id=id)
    if not success:
        raise HTTPException(status_code=404, detail="Agent not found")
    return Message(message="Agent deleted successfully")


@router.get("/{id}/credentials", response_model=CredentialsPublic)
def read_agent_credentials(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Get all credentials linked to an agent.
    """
    # Authorization check
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Get credentials via service
    credentials = CredentialsService.get_agent_credentials(session=session, agent_id=id)
    return CredentialsPublic(data=credentials, count=len(credentials))


@router.post("/{id}/credentials", response_model=Message)
async def add_credential_to_agent(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    link_request: AgentCredentialLinkRequest,
) -> Any:
    """
    Link a credential to an agent.

    This will trigger automatic sync to all running environments of this agent.
    """
    try:
        await CredentialsService.link_credential_to_agent(
            session=session,
            agent_id=id,
            credential_id=link_request.credential_id,
            owner_id=current_user.id,
            is_superuser=current_user.is_superuser
        )
        return Message(message="Credential linked successfully")
    except ValueError as e:
        # Service raises ValueError for not found or permission errors
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.delete("/{id}/credentials/{credential_id}", response_model=Message)
async def remove_credential_from_agent(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    credential_id: uuid.UUID
) -> Any:
    """
    Unlink a credential from an agent.

    This will trigger automatic sync to all running environments of this agent.
    """
    try:
        await CredentialsService.unlink_credential_from_agent(
            session=session,
            agent_id=id,
            credential_id=credential_id,
            owner_id=current_user.id,
            is_superuser=current_user.is_superuser
        )
        return Message(message="Credential unlinked successfully")
    except ValueError as e:
        # Service raises ValueError for not found or permission errors
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


# Environment management routes
@router.post("/{id}/environments", response_model=AgentEnvironmentPublic)
async def create_agent_environment(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    environment_in: AgentEnvironmentCreate,
) -> Any:
    """
    Create new environment for agent.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    environment = await EnvironmentService.create_environment(
        session=session, agent_id=id, data=environment_in, user=current_user
    )
    return environment


@router.get("/{id}/environments", response_model=AgentEnvironmentsPublic)
def list_agent_environments(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    List all environments for an agent.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    environments = EnvironmentService.list_agent_environments(session=session, agent_id=id)
    return AgentEnvironmentsPublic(data=environments, count=len(environments))


@router.post("/{id}/environments/{env_id}/activate", response_model=AgentPublic)
async def activate_environment(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    env_id: uuid.UUID,
) -> Any:
    """
    Activate environment: starts it, sets as active for agent, stops other environments.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Verify environment belongs to this agent
    environment = session.get(AgentEnvironment, env_id)
    if not environment or environment.agent_id != id:
        raise HTTPException(status_code=404, detail="Environment not found for this agent")

    # Activate the environment (starts it, sets as active, stops others)
    try:
        await EnvironmentService.activate_environment(
            session=session, agent_id=id, env_id=env_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Set as agent's active environment
    updated_agent = AgentService.set_active_environment(
        session=session, agent_id=id, env_id=env_id
    )
    return updated_agent


# Schedule management routes
@router.post("/{id}/schedule", response_model=ScheduleResponse)
def generate_schedule(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    data: ScheduleRequest
) -> ScheduleResponse:
    """Generate CRON schedule from natural language using AI."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Call AI function to generate CRON string (in local time)
    ai_result = AIFunctionsService.generate_schedule(
        natural_language=data.natural_language,
        timezone=data.timezone
    )

    # If successful, calculate next execution for preview
    if ai_result.get("success"):
        try:
            # Convert CRON from local time to UTC
            cron_utc = AgentSchedulerService.convert_local_cron_to_utc(
                ai_result["cron_string"],
                data.timezone
            )

            # Calculate next execution
            next_exec = AgentSchedulerService.calculate_next_execution(
                cron_utc,
                data.timezone
            )

            # Keep CRON in local time (don't update it)
            # The save_schedule endpoint will do the conversion
            ai_result["next_execution"] = next_exec.isoformat()
        except Exception as e:
            return ScheduleResponse(
                success=False,
                error=f"Failed to calculate next execution: {str(e)}"
            )

    return ScheduleResponse(**ai_result)


@router.put("/{id}/schedule", response_model=AgentSchedulePublic)
def save_schedule(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    data: SaveScheduleRequest
) -> Any:
    """Save schedule configuration for agent."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Create or update schedule using service
    try:
        schedule = AgentSchedulerService.create_or_update_schedule(
            session=session,
            agent_id=id,
            cron_string=data.cron_string,
            timezone=data.timezone,
            description=data.description,
            enabled=data.enabled
        )
        return schedule
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{id}/schedule", response_model=AgentSchedulePublic | None)
def get_schedule(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """Get current schedule for agent."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    return AgentSchedulerService.get_agent_schedule(session, id)


@router.delete("/{id}/schedule")
def delete_schedule(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Message:
    """Delete agent schedule."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    deleted = AgentSchedulerService.delete_schedule(session, id)
    if deleted:
        return Message(message="Schedule deleted successfully")
    else:
        return Message(message="No schedule found")


# Handover configuration routes
@router.get("/{id}/handovers", response_model=HandoverConfigsPublic)
def list_handover_configs(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """Get all handover configurations for an agent."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Query handover configs where this agent is the source
    statement = select(AgentHandoverConfig).where(
        AgentHandoverConfig.source_agent_id == id
    )
    configs = session.exec(statement).all()

    # Build public response with target agent names
    public_configs = []
    for config in configs:
        target_agent = session.get(Agent, config.target_agent_id)
        public_configs.append(
            HandoverConfigPublic(
                id=config.id,
                source_agent_id=config.source_agent_id,
                target_agent_id=config.target_agent_id,
                target_agent_name=target_agent.name if target_agent else "Unknown",
                handover_prompt=config.handover_prompt,
                enabled=config.enabled,
                auto_feedback=config.auto_feedback,
                created_at=config.created_at,
                updated_at=config.updated_at,
            )
        )

    return HandoverConfigsPublic(data=public_configs, count=len(public_configs))


@router.post("/{id}/handovers", response_model=HandoverConfigPublic)
async def create_handover_config(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    data: HandoverConfigCreate,
) -> Any:
    """Create a new handover configuration."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Verify target agent exists and user has access
    target_agent = session.get(Agent, data.target_agent_id)
    if not target_agent:
        raise HTTPException(status_code=404, detail="Target agent not found")
    if not current_user.is_superuser and (target_agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions to access target agent")

    # Prevent self-handover
    if id == data.target_agent_id:
        raise HTTPException(status_code=400, detail="Cannot create handover to the same agent")

    # Create handover config
    config = AgentHandoverConfig(
        source_agent_id=id,
        target_agent_id=data.target_agent_id,
        handover_prompt=data.handover_prompt,
        enabled=True,
    )
    session.add(config)
    session.commit()
    session.refresh(config)

    # Sync handover config to agent-env
    await AgentService.sync_agent_handover_config(session, id)

    return HandoverConfigPublic(
        id=config.id,
        source_agent_id=config.source_agent_id,
        target_agent_id=config.target_agent_id,
        target_agent_name=target_agent.name,
        handover_prompt=config.handover_prompt,
        enabled=config.enabled,
        auto_feedback=config.auto_feedback,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.put("/{id}/handovers/{handover_id}", response_model=HandoverConfigPublic)
async def update_handover_config(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    handover_id: uuid.UUID,
    data: HandoverConfigUpdate,
) -> Any:
    """Update a handover configuration."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    config = session.get(AgentHandoverConfig, handover_id)
    if not config or config.source_agent_id != id:
        raise HTTPException(status_code=404, detail="Handover configuration not found")

    # Update fields
    if data.handover_prompt is not None:
        config.handover_prompt = data.handover_prompt
    if data.enabled is not None:
        config.enabled = data.enabled
    if data.auto_feedback is not None:
        config.auto_feedback = data.auto_feedback

    config.updated_at = datetime.now(UTC)
    session.add(config)
    session.commit()
    session.refresh(config)

    # Sync handover config to agent-env
    await AgentService.sync_agent_handover_config(session, id)

    target_agent = session.get(Agent, config.target_agent_id)
    return HandoverConfigPublic(
        id=config.id,
        source_agent_id=config.source_agent_id,
        target_agent_id=config.target_agent_id,
        target_agent_name=target_agent.name if target_agent else "Unknown",
        handover_prompt=config.handover_prompt,
        enabled=config.enabled,
        auto_feedback=config.auto_feedback,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.delete("/{id}/handovers/{handover_id}")
async def delete_handover_config(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    handover_id: uuid.UUID,
) -> Message:
    """Delete a handover configuration."""
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    config = session.get(AgentHandoverConfig, handover_id)
    if not config or config.source_agent_id != id:
        raise HTTPException(status_code=404, detail="Handover configuration not found")

    session.delete(config)
    session.commit()

    # Sync handover config to agent-env
    await AgentService.sync_agent_handover_config(session, id)

    return Message(message="Handover configuration deleted successfully")


@router.post("/{id}/handovers/generate", response_model=GenerateHandoverPromptResponse)
def generate_handover_prompt_endpoint(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    data: GenerateHandoverPromptRequest,
) -> Any:
    """Generate handover prompt using AI."""
    # Verify source agent
    source_agent = session.get(Agent, id)
    if not source_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (source_agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Verify target agent
    target_agent = session.get(Agent, data.target_agent_id)
    if not target_agent:
        raise HTTPException(status_code=404, detail="Target agent not found")
    if not current_user.is_superuser and (target_agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions to access target agent")

    # Prevent self-handover
    if id == data.target_agent_id:
        raise HTTPException(status_code=400, detail="Cannot create handover to the same agent")

    # Generate handover prompt
    result = AIFunctionsService.generate_handover_prompt(
        source_agent_name=source_agent.name,
        source_entrypoint=source_agent.entrypoint_prompt,
        source_workflow=source_agent.workflow_prompt,
        target_agent_name=target_agent.name,
        target_entrypoint=target_agent.entrypoint_prompt,
        target_workflow=target_agent.workflow_prompt,
    )

    return GenerateHandoverPromptResponse(**result)


@router.post("/tasks/create", response_model=CreateAgentTaskResponse)
async def create_agent_task(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    data: CreateAgentTaskRequest
) -> Any:
    """
    Create a task from an agent.

    If target_agent_id is provided: Creates task with auto_execute=true (direct handover)
    - Validates target agent exists and user has access
    - Creates InputTask (agent_initiated=True, auto_execute=True)
    - If target agent has refiner_prompt, runs auto-refine
    - Creates session for target agent and sends message
    - Logs system message in source session about task creation

    If target_agent_id is None: Creates task with auto_execute=false (inbox task)
    - Creates InputTask (agent_initiated=True, auto_execute=False)
    - Does NOT auto-refine (user will refine manually)
    - Does NOT execute (user will select agent and execute)
    - Logs system message in source session about task creation
    """
    logger.info(
        f"Create agent task from user {current_user.id}: "
        f"target_agent_id={data.target_agent_id}, source_session_id={data.source_session_id}"
    )
    success, task_id, session_id, error = await AgentService.create_agent_task(
        session=session,
        user=current_user,
        task_message=data.task_message,
        source_session_id=data.source_session_id,
        target_agent_id=data.target_agent_id,
        target_agent_name=data.target_agent_name,
    )

    if data.target_agent_id:
        message = f"Task created for handover to '{data.target_agent_name}'" if success else None
    else:
        message = "Task created in user's inbox" if success else None

    return CreateAgentTaskResponse(
        success=success,
        task_id=task_id,
        session_id=session_id,
        message=message,
        error=error
    )


@router.post("/handover/execute", response_model=ExecuteHandoverResponse)
async def execute_handover(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    data: ExecuteHandoverRequest
) -> Any:
    """
    Deprecated: Use /tasks/create instead.

    Execute a handover by creating a task for target agent, optionally refining it,
    and auto-executing. This endpoint is called by agent-env tools to trigger another agent.
    """
    logger.warning("Deprecated endpoint /handover/execute called, use /tasks/create instead")
    logger.info(f"Handover request from user {current_user.id}: target_agent_id={data.target_agent_id}, source_session_id={data.source_session_id}")

    # Call the new unified service method
    success, task_id, session_id, error = await AgentService.create_agent_task(
        session=session,
        user=current_user,
        task_message=data.task_message,
        source_session_id=data.source_session_id,
        target_agent_id=data.target_agent_id,
        target_agent_name=data.target_agent_name,
    )

    return ExecuteHandoverResponse(
        success=success,
        task_id=task_id,
        session_id=session_id,
        message=f"Task created for handover to '{data.target_agent_name}'" if success else None,
        error=error
    )


# SDK Config and Tools Management routes
@router.get("/{id}/sdk-config", response_model=AgentSdkConfig)
def get_sdk_config(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """
    Get SDK configuration for an agent.

    Returns the agent's SDK config including:
    - sdk_tools: All tools discovered from agent-env
    - allowed_tools: Tools approved by user for automatic permission grant
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    return AgentService.get_sdk_config(session=session, agent_id=id)


@router.patch("/{id}/allowed-tools", response_model=AgentSdkConfig)
async def add_allowed_tools(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    data: AllowedToolsUpdate,
) -> Any:
    """
    Add tools to the allowed_tools list.

    Merges the provided tools with existing allowed_tools (no duplicates).
    Syncs the updated allowed_tools to agent's active environment.
    Returns updated SDK config.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    try:
        sdk_config = AgentService.add_allowed_tools(
            session=session,
            agent_id=id,
            tools=data.tools
        )

        # Sync allowed_tools to agent's active environment
        await AgentService.sync_allowed_tools_to_environment(
            session=session,
            agent_id=id
        )

        return sdk_config
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{id}/pending-tools", response_model=PendingToolsResponse)
def get_pending_tools(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """
    Get tools that need approval.

    Returns tools that are in sdk_tools but not in allowed_tools.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    pending = AgentService.get_pending_tools(session=session, agent_id=id)
    return PendingToolsResponse(pending_tools=pending)


# Session State Management routes
@router.post("/sessions/update-state", response_model=UpdateSessionStateResponse)
async def update_session_state(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    data: UpdateSessionStateRequest,
) -> Any:
    """
    Update session state from agent-env.

    Called by the update_session_state tool to declare session outcomes.
    Creates activities and optionally propagates feedback to source agent.
    """
    from app.services.event_service import event_service
    from app.models.event import EventType

    # Validate state
    allowed_states = ("completed", "needs_input", "error")
    if data.state not in allowed_states:
        return UpdateSessionStateResponse(
            success=False,
            error=f"state must be one of: {', '.join(allowed_states)}"
        )

    if not data.summary.strip():
        return UpdateSessionStateResponse(
            success=False,
            error="summary is required"
        )

    # Look up session
    try:
        session_id = uuid.UUID(data.session_id)
    except ValueError:
        return UpdateSessionStateResponse(
            success=False,
            error="Invalid session_id format"
        )

    chat_session = session.get(ChatSession, session_id)
    if not chat_session:
        return UpdateSessionStateResponse(
            success=False,
            error="Session not found"
        )

    # Update session result state
    chat_session.result_state = data.state
    chat_session.result_summary = data.summary.strip()
    chat_session.updated_at = datetime.now(UTC)
    session.add(chat_session)
    session.commit()

    # Emit SESSION_STATE_UPDATED event
    await event_service.emit_event(
        event_type=EventType.SESSION_STATE_UPDATED,
        model_id=session_id,
        user_id=chat_session.user_id,
        meta={
            "session_id": str(session_id),
            "state": data.state,
            "summary": data.summary.strip(),
            "environment_id": str(chat_session.environment_id),
        }
    )

    return UpdateSessionStateResponse(
        success=True,
        message=f"Session state updated to '{data.state}'"
    )


@router.post("/tasks/respond", response_model=UpdateSessionStateResponse)
async def respond_to_task(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    data: RespondToTaskRequest,
) -> Any:
    """
    Send a message to a sub-task's session from the source agent.

    Used by source agents to answer clarification requests from sub-tasks.
    """
    # Validate inputs
    try:
        task_id = uuid.UUID(data.task_id)
        source_session_id = uuid.UUID(data.source_session_id)
    except ValueError:
        return UpdateSessionStateResponse(
            success=False,
            error="Invalid task_id or source_session_id format"
        )

    if not data.message.strip():
        return UpdateSessionStateResponse(
            success=False,
            error="message is required"
        )

    # Look up task
    task = session.get(InputTask, task_id)
    if not task:
        return UpdateSessionStateResponse(
            success=False,
            error="Task not found"
        )

    # Verify source_session_id matches task's source
    if task.source_session_id != source_session_id:
        return UpdateSessionStateResponse(
            success=False,
            error="Source session does not match task"
        )

    # Get task's active session
    if not task.session_id:
        return UpdateSessionStateResponse(
            success=False,
            error="Task has no active session"
        )

    task_session = session.get(ChatSession, task.session_id)
    if not task_session:
        return UpdateSessionStateResponse(
            success=False,
            error="Task session not found"
        )

    # Reset session result_state (session back in progress)
    task_session.result_state = None
    task_session.result_summary = None
    task_session.updated_at = datetime.now(UTC)
    session.add(task_session)

    # Reset feedback_delivered
    task.feedback_delivered = False
    session.add(task)
    session.commit()

    # Send message to task's session
    result = await SessionService.send_session_message(
        session_id=task.session_id,
        user_id=current_user.id,
        content=data.message.strip(),
    )

    if result.get("action") == "error":
        return UpdateSessionStateResponse(
            success=False,
            error=result.get("message", "Failed to send message")
        )

    return UpdateSessionStateResponse(
        success=True,
        message="Message sent to task session"
    )
