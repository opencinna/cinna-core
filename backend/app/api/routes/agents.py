import uuid
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlmodel import func, select

from app.api.deps import CurrentUser, SessionDep
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
)
from app import crud
from app.services.environment_service import EnvironmentService
from app.services.agent_service import AgentService
from app.services.message_service import MessageService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/", response_model=AgentsPublic)
def read_agents(
    session: SessionDep, current_user: CurrentUser, skip: int = 0, limit: int = 100
) -> Any:
    """
    Retrieve agents.
    """

    if current_user.is_superuser:
        count_statement = select(func.count()).select_from(Agent)
        count = session.exec(count_statement).one()
        statement = select(Agent).offset(skip).limit(limit)
        agents = session.exec(statement).all()
    else:
        count_statement = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.owner_id == current_user.id)
        )
        count = session.exec(count_statement).one()
        statement = (
            select(Agent)
            .where(Agent.owner_id == current_user.id)
            .offset(skip)
            .limit(limit)
        )
        agents = session.exec(statement).all()

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
            mode=request.mode
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
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    credentials = crud.get_agent_credentials(session=session, agent_id=id)
    return CredentialsPublic(data=credentials, count=len(credentials))


@router.post("/{id}/credentials", response_model=Message)
def add_credential_to_agent(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    link_request: AgentCredentialLinkRequest,
) -> Any:
    """
    Link a credential to an agent.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Verify credential exists and user owns it
    credential = session.get(Credential, link_request.credential_id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    if not current_user.is_superuser and (credential.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    crud.add_credential_to_agent(
        session=session, agent_id=id, credential_id=link_request.credential_id
    )
    return Message(message="Credential linked successfully")


@router.delete("/{id}/credentials/{credential_id}", response_model=Message)
def remove_credential_from_agent(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID, credential_id: uuid.UUID
) -> Any:
    """
    Unlink a credential from an agent.
    """
    agent = session.get(Agent, id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not current_user.is_superuser and (agent.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    crud.remove_credential_from_agent(
        session=session, agent_id=id, credential_id=credential_id
    )
    return Message(message="Credential unlinked successfully")


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
