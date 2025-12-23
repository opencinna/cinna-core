from uuid import UUID
from datetime import datetime
from sqlmodel import Session, select
from app.models import AgentEnvironment, AgentEnvironmentCreate, AgentEnvironmentUpdate


class EnvironmentService:
    @staticmethod
    def create_environment(
        session: Session, agent_id: UUID, data: AgentEnvironmentCreate
    ) -> AgentEnvironment:
        """Create environment for agent"""
        environment = AgentEnvironment.model_validate(data, update={"agent_id": agent_id})
        session.add(environment)
        session.commit()
        session.refresh(environment)
        return environment

    @staticmethod
    def get_environment(session: Session, env_id: UUID) -> AgentEnvironment | None:
        """Get environment by ID"""
        return session.get(AgentEnvironment, env_id)

    @staticmethod
    def update_environment(
        session: Session, env_id: UUID, data: AgentEnvironmentUpdate
    ) -> AgentEnvironment | None:
        """Update environment config"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            return None

        update_dict = data.model_dump(exclude_unset=True)
        environment.sqlmodel_update(update_dict)
        environment.updated_at = datetime.utcnow()

        session.add(environment)
        session.commit()
        session.refresh(environment)
        return environment

    @staticmethod
    def delete_environment(session: Session, env_id: UUID) -> bool:
        """Delete environment"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            return False

        session.delete(environment)
        session.commit()
        return True

    @staticmethod
    def list_agent_environments(session: Session, agent_id: UUID) -> list[AgentEnvironment]:
        """List all environments for an agent"""
        statement = select(AgentEnvironment).where(AgentEnvironment.agent_id == agent_id)
        return list(session.exec(statement).all())

    @staticmethod
    def activate_environment(session: Session, agent_id: UUID, env_id: UUID) -> AgentEnvironment:
        """
        Activate environment: starts it, sets as active, stops other environments.

        Business logic:
        1. Get all environments for the agent
        2. Set target environment status to 'running' (stub - will implement Docker start later)
        3. Set target environment is_active to True
        4. Set all other environments is_active to False
        5. Set all other environments status to 'stopped' (stub - will implement Docker stop later)
        """
        # Get target environment
        target_env = session.get(AgentEnvironment, env_id)
        if not target_env or target_env.agent_id != agent_id:
            raise ValueError("Environment not found for this agent")

        # Get all environments for this agent
        all_envs = EnvironmentService.list_agent_environments(session, agent_id)

        # Update all environments
        for env in all_envs:
            if env.id == env_id:
                # Activate target environment
                env.is_active = True
                env.status = "running"  # Stub: will actually start Docker container later
                env.updated_at = datetime.utcnow()
            else:
                # Deactivate and stop other environments
                env.is_active = False
                env.status = "stopped"  # Stub: will actually stop Docker container later
                env.updated_at = datetime.utcnow()

            session.add(env)

        session.commit()
        session.refresh(target_env)
        return target_env
