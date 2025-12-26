from uuid import UUID
import asyncio
from sqlmodel import Session, select
from app.models import Agent, AgentCreate, AgentUpdate, User, SessionCreate
from app.models.environment import AgentEnvironmentCreate
from app.services.environment_service import EnvironmentService
from app.services.session_service import SessionService
from app.core.config import settings


class AgentService:
    @staticmethod
    async def create_agent(session: Session, user_id: UUID, data: AgentCreate, user: User) -> Agent:
        """Create new agent with default environment"""
        agent = Agent.model_validate(data, update={"owner_id": user_id})
        session.add(agent)
        session.commit()
        session.refresh(agent)

        # Create default environment for the agent
        # auto_start=True means it will automatically start and activate after build completes
        default_env_data = AgentEnvironmentCreate(
            env_name=settings.DEFAULT_AGENT_ENV_NAME,
            env_version=settings.DEFAULT_AGENT_ENV_VERSION,
            instance_name="Default",
            type="docker",
            config={}
        )
        default_env = await EnvironmentService.create_environment(
            session=session,
            agent_id=agent.id,
            data=default_env_data,
            user=user,
            auto_start=True  # Automatically start after build completes
        )

        # Note: Environment will be in "creating" status initially
        # The background task will build it and then auto-start/activate
        # UI can poll GET /environments/{id} to track progress

        # Refresh agent to get updated state
        session.refresh(agent)
        return agent

    @staticmethod
    def get_agent_with_environment(session: Session, agent_id: UUID) -> Agent | None:
        """Get agent with active environment details"""
        statement = select(Agent).where(Agent.id == agent_id)
        return session.exec(statement).first()

    @staticmethod
    def update_agent(session: Session, agent_id: UUID, data: AgentUpdate) -> Agent | None:
        """Update agent"""
        agent = session.get(Agent, agent_id)
        if not agent:
            return None

        update_dict = data.model_dump(exclude_unset=True)
        agent.sqlmodel_update(update_dict)

        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent

    @staticmethod
    def set_active_environment(session: Session, agent_id: UUID, env_id: UUID) -> Agent | None:
        """Set active environment for agent"""
        agent = session.get(Agent, agent_id)
        if not agent:
            return None

        agent.active_environment_id = env_id
        session.add(agent)
        session.commit()
        session.refresh(agent)
        return agent

    @staticmethod
    async def delete_agent(session: Session, agent_id: UUID) -> bool:
        """
        Delete agent and cleanup all associated resources.

        Steps:
        1. Get all environments for the agent
        2. Delete each environment (stops containers, cleans up Docker resources)
           - EnvironmentService.delete_environment handles clearing active_environment_id
        3. Delete agent (DB cascades will handle sessions/messages)
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            return False

        # Get all environments for this agent
        environments = EnvironmentService.list_agent_environments(session, agent_id)

        # Delete each environment (this properly cleans up Docker resources)
        # delete_environment() will automatically clear active_environment_id if needed
        for env in environments:
            try:
                await EnvironmentService.delete_environment(session, env.id)
            except Exception as e:
                # Log error but continue with other environments
                print(f"Warning: Failed to delete environment {env.id}: {e}")

        # Delete agent (DB cascades will handle any remaining records)
        session.delete(agent)
        session.commit()
        return True

    @staticmethod
    async def create_agent_flow(
        session: Session, user: User, description: str, mode: str
    ):
        """
        Create full agent flow: agent + environment + session
        This is an async generator that yields progress updates
        """
        agent = None
        environment = None

        try:
            # Step 1: Create agent from description
            yield {
                "step": "creating_agent",
                "message": "Generating agent configuration...",
                "current_step": "create_agent"
            }

            # Generate agent name and prompts from description
            # For now, use simple logic - in future, can use LLM to generate these
            agent_number = len(session.exec(select(Agent).where(Agent.owner_id == user.id)).all()) + 1
            agent_name = f"Agent #{agent_number}"

            agent_data = AgentCreate(
                name=agent_name,
                description=description,
                workflow_prompt=f"You are an AI agent designed to: {description}",
                entrypoint_prompt=description,
            )

            agent = Agent.model_validate(agent_data, update={"owner_id": user.id})
            session.add(agent)
            session.commit()
            session.refresh(agent)

            yield {
                "step": "agent_created",
                "message": f"Agent '{agent_name}' created successfully",
                "agent_id": str(agent.id),
                "current_step": "create_agent"
            }

            # Step 2: Create and start default environment
            yield {
                "step": "environment_starting",
                "message": "Building default environment...",
                "current_step": "start_environment"
            }

            default_env_data = AgentEnvironmentCreate(
                env_name=settings.DEFAULT_AGENT_ENV_NAME,
                env_version=settings.DEFAULT_AGENT_ENV_VERSION,
                instance_name="Default",
                type="docker",
                config={}
            )

            environment = await EnvironmentService.create_environment(
                session=session,
                agent_id=agent.id,
                data=default_env_data,
                user=user,
                auto_start=True
            )

            # Wait for environment to be ready (poll status)
            max_wait_time = 300  # 5 minutes
            poll_interval = 2  # 2 seconds
            elapsed_time = 0

            while elapsed_time < max_wait_time:
                session.refresh(environment)

                if environment.status == "running":
                    yield {
                        "step": "environment_ready",
                        "message": "Environment is ready",
                        "environment_id": str(environment.id),
                        "current_step": "start_environment"
                    }
                    break
                elif environment.status == "error":
                    raise Exception(f"Environment failed to start: {environment.status_message}")
                else:
                    yield {
                        "step": "environment_starting",
                        "message": f"Environment status: {environment.status}...",
                        "current_step": "start_environment"
                    }

                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval
            else:
                raise Exception("Environment failed to start within timeout")

            # Step 3: Create session
            yield {
                "step": "session_creating",
                "message": "Creating conversation session...",
                "current_step": "create_session"
            }

            # Set agent's active environment
            agent.active_environment_id = environment.id
            session.add(agent)
            session.commit()

            # Create session
            session_data = SessionCreate(
                agent_id=agent.id,
                mode=mode,
                title=None
            )

            new_session = SessionService.create_session(
                db_session=session,
                user_id=user.id,
                data=session_data
            )

            if not new_session:
                raise Exception("Failed to create session")

            yield {
                "step": "session_created",
                "message": "Session created successfully",
                "session_id": str(new_session.id),
                "current_step": "create_session"
            }

            # Step 4: Complete
            yield {
                "step": "completed",
                "message": "Agent creation completed",
                "agent_id": str(agent.id),
                "environment_id": str(environment.id),
                "session_id": str(new_session.id),
                "current_step": "redirect"
            }

        except Exception as e:
            yield {
                "step": "error",
                "message": str(e),
                "current_step": "create_agent" if not agent else ("start_environment" if not environment else "create_session")
            }
