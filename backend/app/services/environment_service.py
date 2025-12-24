from uuid import UUID
from datetime import datetime
import random
import asyncio
from sqlmodel import Session, select
from sqlalchemy.orm.attributes import flag_modified
from app.models import AgentEnvironment, AgentEnvironmentCreate, AgentEnvironmentUpdate, Agent, User
from app import crud
from app.core.db import engine
from .environment_lifecycle import EnvironmentLifecycleManager


def generate_environment_name() -> str:
    """Generate a memorable 3-word name with dashes (e.g., 'brave-blue-dragon')"""
    adjectives = [
        "brave", "clever", "bright", "swift", "gentle", "noble", "wise", "calm",
        "bold", "keen", "sharp", "quick", "fierce", "proud", "strong", "silent",
        "agile", "mighty", "golden", "silver", "cosmic", "stellar", "lunar", "solar",
        "ancient", "mystic", "radiant", "serene", "vivid", "royal", "divine", "eternal",
        "wild", "free", "pure", "grand", "epic", "vital", "dynamic", "blessed",
        "elegant", "graceful", "daring", "fearless", "infinite", "supreme", "perfect", "pristine"
    ]

    colors = [
        "red", "blue", "green", "yellow", "purple", "orange", "pink", "teal",
        "amber", "jade", "ruby", "azure", "violet", "crimson", "emerald", "indigo",
        "coral", "pearl", "onyx", "ivory", "cobalt", "bronze", "copper", "chrome",
        "sapphire", "topaz", "turquoise", "magenta", "scarlet", "lime", "mint", "lavender",
        "maroon", "navy", "olive", "platinum", "slate", "steel", "graphite", "charcoal",
        "frost", "snow", "cloud", "smoke", "shadow", "obsidian", "diamond", "crystal"
    ]

    nouns = [
        "tiger", "dragon", "eagle", "falcon", "phoenix", "wolf", "lion", "bear",
        "hawk", "raven", "fox", "puma", "lynx", "cobra", "viper", "panther",
        "orca", "shark", "whale", "dolphin", "owl", "sparrow", "swan", "crane",
        "mountain", "river", "ocean", "forest", "canyon", "valley", "summit", "peak",
        "leopard", "jaguar", "cheetah", "gryphon", "unicorn", "pegasus", "sphinx", "hydra",
        "thunder", "lightning", "storm", "tempest", "blizzard", "aurora", "comet", "meteor",
        "warrior", "guardian", "sentinel", "champion", "ranger", "scout", "voyager", "explorer",
        "glacier", "waterfall", "volcano", "desert", "prairie", "tundra", "reef", "archipelago"
    ]

    adjective = random.choice(adjectives)
    color = random.choice(colors)
    noun = random.choice(nouns)

    return f"{adjective}-{color}-{noun}"


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
    async def _create_environment_background(
        env_id: UUID,
        agent_id: UUID,
        anthropic_api_key: str | None = None,
        auto_start: bool = False
    ):
        """
        Background task to create environment instance.
        Uses its own database session to avoid conflicts.

        Args:
            env_id: Environment ID
            agent_id: Agent ID
            anthropic_api_key: User's Anthropic API key
            auto_start: If True, automatically start and activate after build
        """
        # Create new database session for background task
        with Session(engine) as session:
            try:
                # Get environment and agent
                environment = session.get(AgentEnvironment, env_id)
                agent = session.get(Agent, agent_id)

                if not environment or not agent:
                    raise ValueError("Environment or Agent not found")

                # Run creation process
                lifecycle_manager = EnvironmentService.get_lifecycle_manager()
                await lifecycle_manager.create_environment_instance(
                    session, environment, agent, anthropic_api_key
                )

                # Auto-start if requested (typically for default agent environments)
                if auto_start and environment.status == "stopped":
                    # Start the environment
                    await lifecycle_manager.start_environment(session, environment, agent)

                    # Set as active
                    environment.is_active = True
                    session.add(environment)
                    session.commit()

            except Exception as e:
                # Error is already logged and status updated in create_environment_instance
                # Just ensure we don't leave the background task hanging
                pass

    @staticmethod
    async def _activate_environment_background(
        agent_id: UUID,
        env_id: UUID
    ):
        """
        Background task to activate environment.
        Uses its own database session to avoid conflicts.

        Steps:
        1. Stop all other running environments
        2. Start target environment
        3. Update is_active flags

        Args:
            agent_id: Agent ID
            env_id: Environment ID to activate
        """
        # Create new database session for background task
        with Session(engine) as session:
            try:
                # Get agent and target environment
                agent = session.get(Agent, agent_id)
                target_env = session.get(AgentEnvironment, env_id)

                if not agent or not target_env:
                    raise ValueError("Agent or Environment not found")

                # Get all environments for this agent
                all_envs = EnvironmentService.list_agent_environments(session, agent_id)
                lifecycle_manager = EnvironmentService.get_lifecycle_manager()

                # Stop all other environments first
                for env in all_envs:
                    if env.id != env_id and env.status == "running":
                        try:
                            await lifecycle_manager.stop_environment(session, env)
                            env.is_active = False
                            session.add(env)
                        except Exception as e:
                            # Log but continue
                            print(f"Warning: Failed to stop environment {env.id}: {e}")

                # Start target environment (this updates status internally)
                await lifecycle_manager.start_environment(session, target_env, agent)

                # Update is_active flags for all environments
                for env in all_envs:
                    env.is_active = (env.id == env_id)
                    env.updated_at = datetime.utcnow()
                    session.add(env)

                session.commit()

            except Exception as e:
                # Error is already logged in start_environment
                # Update target environment status to error if not already done
                with Session(engine) as error_session:
                    target_env = error_session.get(AgentEnvironment, env_id)
                    if target_env and target_env.status != "error":
                        target_env.status = "error"
                        target_env.status_message = f"Failed to activate environment: {str(e)}"
                        error_session.add(target_env)
                        error_session.commit()
    @staticmethod
    async def create_environment(
        session: Session,
        agent_id: UUID,
        data: AgentEnvironmentCreate,
        user: User,
        auto_start: bool = False
    ) -> AgentEnvironment:
        """
        Create environment for agent.

        Steps:
        1. Create DB record with status "creating"
        2. Spawn background task to build Docker instance (non-blocking)
        3. Return immediately - client can poll status endpoint

        Args:
            session: Database session
            agent_id: Agent ID
            data: Environment creation data
            user: User creating the environment
            auto_start: If True, automatically start and activate after build completes

        Note: The actual Docker build happens asynchronously.
        Use GET /environments/{id}/status to track progress.
        """
        # Get agent
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Get user's AI credentials
        ai_credentials = crud.get_user_ai_credentials(user=user)
        anthropic_api_key = ai_credentials.anthropic_api_key if ai_credentials else None

        # Generate a memorable instance name if not provided
        instance_name = data.instance_name if data.instance_name != "Instance" else generate_environment_name()

        # Create DB record with initial status
        environment = AgentEnvironment.model_validate(
            data, update={
                "agent_id": agent_id,
                "instance_name": instance_name,
                "status": "creating",
                "status_message": "Initializing environment creation..."
            }
        )
        session.add(environment)
        session.commit()
        session.refresh(environment)

        # Spawn background task to create Docker instance
        # This allows the API to return immediately while the build happens asynchronously
        asyncio.create_task(
            EnvironmentService._create_environment_background(
                environment.id, agent_id, anthropic_api_key, auto_start
            )
        )

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
        1. Validate environment exists and belongs to agent
        2. Set target environment status to "starting" immediately
        3. Spawn background task to:
           - Stop all other running environments
           - Start target environment
           - Update is_active flags
        4. Return immediately (non-blocking)

        Note: The actual start/stop happens asynchronously.
        Use GET /environments/{id}/status to track progress.
        """
        # Get agent
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Get target environment
        target_env = session.get(AgentEnvironment, env_id)
        if not target_env or target_env.agent_id != agent_id:
            raise ValueError("Environment not found for this agent")

        # Update target environment status immediately
        target_env.status = "starting"
        target_env.status_message = "Preparing to activate environment..."
        session.add(target_env)
        session.commit()
        session.refresh(target_env)

        # Spawn background task to activate environment
        # This allows the API to return immediately while activation happens asynchronously
        asyncio.create_task(
            EnvironmentService._activate_environment_background(agent_id, env_id)
        )

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
