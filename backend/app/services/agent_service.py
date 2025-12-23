from uuid import UUID
from sqlmodel import Session, select
from app.models import Agent, AgentCreate, AgentUpdate
from app.models.environment import AgentEnvironmentCreate
from app.services.environment_service import EnvironmentService


class AgentService:
    @staticmethod
    async def create_agent(session: Session, user_id: UUID, data: AgentCreate) -> Agent:
        """Create new agent with default environment"""
        agent = Agent.model_validate(data, update={"owner_id": user_id})
        session.add(agent)
        session.commit()
        session.refresh(agent)

        # Create default environment for the agent
        default_env_data = AgentEnvironmentCreate(
            env_name="python-env-basic",  # Use actual template name
            env_version="1.0.0",
            instance_name="Default",
            type="docker",
            config={}
        )
        default_env = await EnvironmentService.create_environment(
            session=session,
            agent_id=agent.id,
            data=default_env_data
        )

        # Activate the default environment (starts it)
        await EnvironmentService.activate_environment(
            session=session,
            agent_id=agent.id,
            env_id=default_env.id
        )

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
    def delete_agent(session: Session, agent_id: UUID) -> bool:
        """Delete agent (cascades to environments)"""
        agent = session.get(Agent, agent_id)
        if not agent:
            return False

        session.delete(agent)
        session.commit()
        return True
