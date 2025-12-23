from uuid import UUID
from datetime import datetime
from sqlmodel import Session, select
from app.models import AgentEnvironment, AgentEnvironmentCreate, AgentEnvironmentUpdate, Agent
from .environment_lifecycle import EnvironmentLifecycleManager


class EnvironmentService:
    """
    Service layer for environment operations.

    Responsibilities:
    - Database operations (CRUD)
    - Business logic validation
    - Delegates lifecycle operations to EnvironmentLifecycleManager
    """

    # Singleton lifecycle manager instance
    _lifecycle_manager = None

    @classmethod
    def get_lifecycle_manager(cls) -> EnvironmentLifecycleManager:
        """Get or create lifecycle manager singleton"""
        if cls._lifecycle_manager is None:
            cls._lifecycle_manager = EnvironmentLifecycleManager()
        return cls._lifecycle_manager
    @staticmethod
    async def create_environment(
        session: Session, agent_id: UUID, data: AgentEnvironmentCreate
    ) -> AgentEnvironment:
        """
        Create environment for agent.

        Steps:
        1. Create DB record
        2. Create Docker instance from template (copy files, build image)
        """
        # Get agent
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Create DB record
        environment = AgentEnvironment.model_validate(data, update={"agent_id": agent_id})
        session.add(environment)
        session.commit()
        session.refresh(environment)

        # Create Docker instance (copy template, build image)
        try:
            lifecycle_manager = EnvironmentService.get_lifecycle_manager()
            await lifecycle_manager.create_environment_instance(session, environment, agent)
        except Exception as e:
            # Rollback DB record if Docker creation fails
            session.delete(environment)
            session.commit()
            raise Exception(f"Failed to create environment instance: {str(e)}") from e

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
    async def delete_environment(session: Session, env_id: UUID) -> bool:
        """
        Delete environment.

        Steps:
        1. Stop container if running
        2. Delete Docker instance (remove directory, cleanup network)
        3. Delete DB record
        """
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            return False

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()

        # Stop container if running
        if environment.status == "running":
            try:
                await lifecycle_manager.stop_environment(session, environment)
            except Exception as e:
                # Log error but continue with deletion
                print(f"Warning: Failed to stop environment: {e}")

        # Delete Docker instance (directory, volumes, networks, etc.)
        try:
            await lifecycle_manager.delete_environment_instance(environment)
        except Exception as e:
            # Log error but continue with DB deletion
            print(f"Warning: Failed to delete environment instance: {e}")

        # Delete DB record
        session.delete(environment)
        session.commit()
        return True

    @staticmethod
    def list_agent_environments(session: Session, agent_id: UUID) -> list[AgentEnvironment]:
        """List all environments for an agent"""
        statement = select(AgentEnvironment).where(AgentEnvironment.agent_id == agent_id)
        return list(session.exec(statement).all())

    @staticmethod
    async def activate_environment(session: Session, agent_id: UUID, env_id: UUID) -> AgentEnvironment:
        """
        Activate environment: starts it, sets as active, stops other environments.

        Business logic:
        1. Get all environments for the agent
        2. Start target environment (Docker)
        3. Set target environment is_active to True
        4. Stop all other environments (Docker)
        5. Set all other environments is_active to False
        """
        # Get agent
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Get target environment
        target_env = session.get(AgentEnvironment, env_id)
        if not target_env or target_env.agent_id != agent_id:
            raise ValueError("Environment not found for this agent")

        # Get all environments for this agent
        all_envs = EnvironmentService.list_agent_environments(session, agent_id)

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()

        # Stop all other environments first
        for env in all_envs:
            if env.id != env_id and env.status == "running":
                try:
                    await lifecycle_manager.stop_environment(session, env)
                    env.is_active = False
                except Exception as e:
                    print(f"Warning: Failed to stop environment {env.id}: {e}")

        # Start target environment
        await lifecycle_manager.start_environment(session, target_env, agent)
        target_env.is_active = True

        # Update all environment records
        for env in all_envs:
            if env.id == env_id:
                env.is_active = True
            else:
                env.is_active = False
            env.updated_at = datetime.utcnow()
            session.add(env)

        session.commit()
        session.refresh(target_env)
        return target_env

    # === Lifecycle Operations ===

    @staticmethod
    async def start_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """Start environment container"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise ValueError(f"Environment {env_id} not found")

        agent = session.get(Agent, environment.agent_id)
        if not agent:
            raise ValueError(f"Agent {environment.agent_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        await lifecycle_manager.start_environment(session, environment, agent)

        session.refresh(environment)
        return environment

    @staticmethod
    async def stop_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """Stop environment container"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise ValueError(f"Environment {env_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        await lifecycle_manager.stop_environment(session, environment)

        session.refresh(environment)
        return environment

    @staticmethod
    async def restart_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """Restart environment container"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise ValueError(f"Environment {env_id} not found")

        agent = session.get(Agent, environment.agent_id)
        if not agent:
            raise ValueError(f"Agent {environment.agent_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        await lifecycle_manager.restart_environment(session, environment, agent)

        session.refresh(environment)
        return environment

    @staticmethod
    async def get_environment_status(session: Session, env_id: UUID) -> dict:
        """Get environment status"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise ValueError(f"Environment {env_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        status = await lifecycle_manager.get_status(environment)

        return {
            "environment_id": env_id,
            "status": status,
            "last_health_check": environment.last_health_check
        }

    @staticmethod
    async def check_environment_health(session: Session, env_id: UUID) -> dict:
        """Check environment health"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise ValueError(f"Environment {env_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        health = await lifecycle_manager.check_health(session, environment)

        return health

    @staticmethod
    async def get_environment_logs(session: Session, env_id: UUID, lines: int = 100) -> list[str]:
        """Get environment logs"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise ValueError(f"Environment {env_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        logs = await lifecycle_manager.get_logs(environment, lines=lines)

        return logs
