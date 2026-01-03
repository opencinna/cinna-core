import uuid
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import func, select

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
    ExecuteHandoverRequest,
    ExecuteHandoverResponse,
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


@router.get("/", response_model=AgentsPublic)
def read_agents(
    session: SessionDep,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 100,
    user_workspace_id: uuid.UUID | None = None,
) -> Any:
    """
    Retrieve agents. Optionally filter by workspace.
    If user_workspace_id is not provided, returns all agents.
    If user_workspace_id is provided (including null for default workspace), filters by that workspace.
    """

    if current_user.is_superuser:
        count_statement = select(func.count()).select_from(Agent)
        statement = select(Agent)

        # Apply workspace filter for superuser too
        if user_workspace_id is not None:
            count_statement = count_statement.where(Agent.user_workspace_id == user_workspace_id)
            statement = statement.where(Agent.user_workspace_id == user_workspace_id)

        count = session.exec(count_statement).one()
        agents = session.exec(statement.offset(skip).limit(limit)).all()
    else:
        count_statement = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.owner_id == current_user.id)
        )
        statement = (
            select(Agent)
            .where(Agent.owner_id == current_user.id)
        )

        # Apply workspace filter
        if user_workspace_id is not None:
            count_statement = count_statement.where(Agent.user_workspace_id == user_workspace_id)
            statement = statement.where(Agent.user_workspace_id == user_workspace_id)

        count = session.exec(count_statement).one()
        agents = session.exec(statement.offset(skip).limit(limit)).all()

    return AgentsPublic(data=agents, count=count)


@router.get("/{id}", response_model=AgentPublic)
def read_agent(session: SessionDep, current_user: CurrentUser, id: uuid.UUID) -> Any:
    """
    Get agent by ID with environment details.
    """
    agent = AgentService.get_agent_with_environment(session=session, agent_id=id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")
    return agent


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
            auto_create_session=request.auto_create_session
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
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    updated_agent = AgentService.update_agent(
        session=session, agent_id=id, data=agent_in
    )
    if not updated_agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return updated_agent


@router.post("/{id}/sync-prompts", response_model=Message)
async def sync_agent_prompts(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """
    Sync agent prompts to active environment.

    When user manually edits workflow_prompt or entrypoint_prompt in the backend,
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
        await MessageService.sync_agent_prompts_to_environment(
            environment=environment,
            workflow_prompt=agent.workflow_prompt,
            entrypoint_prompt=agent.entrypoint_prompt
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
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

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

    config.updated_at = datetime.utcnow()
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


@router.post("/handover/execute", response_model=ExecuteHandoverResponse)
async def execute_handover(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    data: ExecuteHandoverRequest
) -> Any:
    """
    Execute a handover by creating a new session for target agent and sending the handover message.
    This endpoint is called by agent-env tools to trigger another agent.

    The handover process:
    1. Creates new conversation session for target agent
    2. Posts handover message to new session
    3. Logs system message in source session with link to new session
    """
    logger.info(f"Handover request from user {current_user.id}: target_agent_id={data.target_agent_id}, source_session_id={data.source_session_id}")
    success, new_session_id, error = await AgentService.execute_handover(
        session=session,
        user_id=current_user.id,
        target_agent_id=data.target_agent_id,
        target_agent_name=data.target_agent_name,
        handover_message=data.handover_message,
        source_session_id=data.source_session_id
    )

    return ExecuteHandoverResponse(
        success=success,
        session_id=new_session_id,
        error=error
    )
