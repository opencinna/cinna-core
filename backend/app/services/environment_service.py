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
