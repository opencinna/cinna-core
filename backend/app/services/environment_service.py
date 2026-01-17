from uuid import UUID
from datetime import datetime
import random
import asyncio
import logging
from typing import Any
from sqlmodel import Session, select
from sqlalchemy.orm.attributes import flag_modified
from app.models import AgentEnvironment, AgentEnvironmentCreate, AgentEnvironmentUpdate, Agent, User
from app.models.ai_credential import AICredentialType
from app import crud
from app.core.db import engine
from .environment_lifecycle import EnvironmentLifecycleManager
from .ai_credentials_service import ai_credentials_service

logger = logging.getLogger(__name__)

# SDK Provider Constants
SDK_ANTHROPIC = "claude-code/anthropic"
SDK_MINIMAX = "claude-code/minimax"
SDK_OPENAI_COMPATIBLE = "google-adk-wr/openai-compatible"
DEFAULT_SDK = SDK_ANTHROPIC
VALID_SDK_OPTIONS = [SDK_ANTHROPIC, SDK_MINIMAX, SDK_OPENAI_COMPATIBLE]

# SDK to required API key mapping
SDK_API_KEY_MAP = {
    SDK_ANTHROPIC: "anthropic_api_key",
    SDK_MINIMAX: "minimax_api_key",
    SDK_OPENAI_COMPATIBLE: "openai_compatible_api_key",
}

# SDK to AICredentialType mapping
SDK_TO_CREDENTIAL_TYPE = {
    SDK_ANTHROPIC: AICredentialType.ANTHROPIC,
    SDK_MINIMAX: AICredentialType.MINIMAX,
    SDK_OPENAI_COMPATIBLE: AICredentialType.OPENAI_COMPATIBLE,
}


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
        auto_start: bool = False,
        minimax_api_key: str | None = None,
        openai_compatible_api_key: str | None = None,
        openai_compatible_base_url: str | None = None,
        openai_compatible_model: str | None = None
    ):
        """
        Background task to create environment instance.
        Uses its own database session to avoid conflicts.

        Args:
            env_id: Environment ID
            agent_id: Agent ID
            anthropic_api_key: User's Anthropic API key
            auto_start: If True, automatically start and activate after build
            minimax_api_key: User's MiniMax API key
            openai_compatible_api_key: User's OpenAI Compatible API key
            openai_compatible_base_url: User's OpenAI Compatible base URL
            openai_compatible_model: User's OpenAI Compatible model
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
                    session, environment, agent,
                    anthropic_api_key, minimax_api_key,
                    openai_compatible_api_key, openai_compatible_base_url, openai_compatible_model
                )

                # Auto-start if requested (typically for default agent environments)
                if auto_start and environment.status == "stopped":
                    # Start the environment
                    await lifecycle_manager.start_environment(session, environment, agent)

                    # Set as active environment
                    environment.is_active = True
                    session.add(environment)

                    # Update agent's active_environment_id
                    agent.active_environment_id = environment.id
                    session.add(agent)

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

                # Update agent's active_environment_id
                agent.active_environment_id = env_id
                session.add(agent)

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
        1. Validate SDK values and required API keys
        2. Create DB record with status "creating"
        3. Spawn background task to build Docker instance (non-blocking)
        4. Return immediately - client can poll status endpoint

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

        # Normalize SDK values (use user's defaults, then fall back to global default)
        # Conversation SDK is always required
        sdk_conversation = data.agent_sdk_conversation or user.default_sdk_conversation or DEFAULT_SDK
        # Building SDK can be None (e.g., for user-mode clones that don't need building)
        # Only apply defaults if not explicitly set to None
        if data.agent_sdk_building is None:
            # Explicitly None means "not needed" (e.g., user-mode clones)
            sdk_building = None
        else:
            sdk_building = data.agent_sdk_building or user.default_sdk_building or DEFAULT_SDK

        # Validate SDK values
        if sdk_conversation not in VALID_SDK_OPTIONS:
            raise ValueError(f"Invalid agent_sdk_conversation: {sdk_conversation}. Valid options: {VALID_SDK_OPTIONS}")
        if sdk_building is not None and sdk_building not in VALID_SDK_OPTIONS:
            raise ValueError(f"Invalid agent_sdk_building: {sdk_building}. Valid options: {VALID_SDK_OPTIONS}")

        # Initialize API key variables
        anthropic_api_key = None
        minimax_api_key = None
        openai_compatible_api_key = None
        openai_compatible_base_url = None
        openai_compatible_model = None

        # Credential IDs for storing on the environment
        conversation_ai_credential_id = None
        building_ai_credential_id = None

        if data.use_default_ai_credentials:
            # Use user's default AI credentials from profile (existing behavior)
            ai_credentials = crud.get_user_ai_credentials(user=user)

            # Collect required SDKs and validate user has API keys
            required_sdks = set([sdk_conversation, sdk_building])
            for sdk in required_sdks:
                required_key = SDK_API_KEY_MAP.get(sdk)
                if required_key and ai_credentials:
                    key_value = getattr(ai_credentials, required_key, None)
                    if not key_value:
                        raise ValueError(f"Missing required API key '{required_key}' for SDK '{sdk}'. Please add it in user settings.")
                elif required_key and not ai_credentials:
                    raise ValueError(f"Missing required API key '{required_key}' for SDK '{sdk}'. Please add it in user settings.")

            # Get API keys from user's default credentials
            anthropic_api_key = ai_credentials.anthropic_api_key if ai_credentials else None
            minimax_api_key = ai_credentials.minimax_api_key if ai_credentials else None
            openai_compatible_api_key = ai_credentials.openai_compatible_api_key if ai_credentials else None
            openai_compatible_base_url = ai_credentials.openai_compatible_base_url if ai_credentials else None
            openai_compatible_model = ai_credentials.openai_compatible_model if ai_credentials else None
        else:
            # Resolve conversation credential
            if data.conversation_ai_credential_id:
                conv_cred_data = ai_credentials_service.get_credential_for_use(
                    session, data.conversation_ai_credential_id, user.id
                )
                if not conv_cred_data:
                    raise ValueError("Cannot access the specified conversation AI credential")
                conversation_ai_credential_id = data.conversation_ai_credential_id
                # Map to appropriate API key based on SDK
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_conversation)
                if cred_type == AICredentialType.ANTHROPIC:
                    anthropic_api_key = conv_cred_data.api_key
                elif cred_type == AICredentialType.MINIMAX:
                    minimax_api_key = conv_cred_data.api_key
                elif cred_type == AICredentialType.OPENAI_COMPATIBLE:
                    openai_compatible_api_key = conv_cred_data.api_key
                    openai_compatible_base_url = conv_cred_data.base_url
                    openai_compatible_model = conv_cred_data.model
            else:
                # Fall back to user's default for conversation SDK
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_conversation)
                if cred_type:
                    default_cred = ai_credentials_service.get_default_for_type(session, user.id, cred_type)
                    if default_cred:
                        conv_cred_data = ai_credentials_service._decrypt_credential(default_cred)
                        if cred_type == AICredentialType.ANTHROPIC:
                            anthropic_api_key = conv_cred_data.api_key
                        elif cred_type == AICredentialType.MINIMAX:
                            minimax_api_key = conv_cred_data.api_key
                        elif cred_type == AICredentialType.OPENAI_COMPATIBLE:
                            openai_compatible_api_key = conv_cred_data.api_key
                            openai_compatible_base_url = conv_cred_data.base_url
                            openai_compatible_model = conv_cred_data.model
                    else:
                        logger.info(f"DEBUG env_service - No default credential found for type {cred_type}")

            # Resolve building credential
            if data.building_ai_credential_id:
                build_cred_data = ai_credentials_service.get_credential_for_use(
                    session, data.building_ai_credential_id, user.id
                )
                if not build_cred_data:
                    raise ValueError("Cannot access the specified building AI credential")
                building_ai_credential_id = data.building_ai_credential_id
                # Map to appropriate API key based on SDK
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_building)
                if cred_type == AICredentialType.ANTHROPIC:
                    anthropic_api_key = build_cred_data.api_key
                elif cred_type == AICredentialType.MINIMAX:
                    minimax_api_key = build_cred_data.api_key
                elif cred_type == AICredentialType.OPENAI_COMPATIBLE:
                    openai_compatible_api_key = build_cred_data.api_key
                    openai_compatible_base_url = build_cred_data.base_url
                    openai_compatible_model = build_cred_data.model
            else:
                # Fall back to user's default for building SDK if not same as conversation
                if sdk_building != sdk_conversation or not data.conversation_ai_credential_id:
                    cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_building)
                    if cred_type:
                        default_cred = ai_credentials_service.get_default_for_type(session, user.id, cred_type)
                        if default_cred:
                            build_cred_data = ai_credentials_service._decrypt_credential(default_cred)
                            if cred_type == AICredentialType.ANTHROPIC:
                                anthropic_api_key = build_cred_data.api_key
                            elif cred_type == AICredentialType.MINIMAX:
                                minimax_api_key = build_cred_data.api_key
                            elif cred_type == AICredentialType.OPENAI_COMPATIBLE:
                                openai_compatible_api_key = build_cred_data.api_key
                                openai_compatible_base_url = build_cred_data.base_url
                                openai_compatible_model = build_cred_data.model

            # Validate that we have the required API keys
            required_sdks = set([sdk_conversation, sdk_building])
            for sdk in required_sdks:
                required_key = SDK_API_KEY_MAP.get(sdk)
                if required_key:
                    key_value = None
                    if required_key == "anthropic_api_key":
                        key_value = anthropic_api_key
                    elif required_key == "minimax_api_key":
                        key_value = minimax_api_key
                    elif required_key == "openai_compatible_api_key":
                        key_value = openai_compatible_api_key
                    if not key_value:
                        raise ValueError(f"Missing required API key for SDK '{sdk}'. Please select an AI credential or set a default.")

        # Generate a memorable instance name if not provided
        instance_name = data.instance_name if data.instance_name != "Instance" else generate_environment_name()

        # Create DB record with initial status
        environment = AgentEnvironment.model_validate(
            data, update={
                "agent_id": agent_id,
                "instance_name": instance_name,
                "status": "creating",
                "status_message": "Initializing environment creation...",
                "agent_sdk_conversation": sdk_conversation,
                "agent_sdk_building": sdk_building,
                "use_default_ai_credentials": data.use_default_ai_credentials,
                "conversation_ai_credential_id": conversation_ai_credential_id,
                "building_ai_credential_id": building_ai_credential_id,
            }
        )
        session.add(environment)
        session.commit()
        session.refresh(environment)

        # Spawn background task to create Docker instance
        # This allows the API to return immediately while the build happens asynchronously
        asyncio.create_task(
            EnvironmentService._create_environment_background(
                environment.id, agent_id, anthropic_api_key, auto_start, minimax_api_key,
                openai_compatible_api_key, openai_compatible_base_url, openai_compatible_model
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
        1. Clear agent's active_environment_id if this is the active environment
        2. Stop container if running
        3. Delete Docker instance (remove directory, cleanup network)
        4. Delete DB record
        """
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            return False

        # Clear agent's active_environment_id if this is the active environment
        # This prevents FK constraint violations
        agent = session.get(Agent, environment.agent_id)
        if agent and agent.active_environment_id == env_id:
            agent.active_environment_id = None
            session.add(agent)
            session.commit()

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
    async def suspend_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """
        Suspend environment container to save resources.

        This stops the container but keeps the status as 'suspended' instead of 'stopped',
        indicating it can be quickly reactivated when needed.
        """
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise ValueError(f"Environment {env_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        await lifecycle_manager.suspend_environment(session, environment)

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
    async def rebuild_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """
        Rebuild environment with updated core files while preserving workspace.

        This operation:
        - Checks if container is running
        - Stops container if running
        - Updates core files from template
        - Rebuilds Docker image
        - Starts container if it was running before
        - Preserves workspace data (scripts, files, docs, credentials, databases)
        """
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise ValueError(f"Environment {env_id} not found")

        agent = session.get(Agent, environment.agent_id)
        if not agent:
            raise ValueError(f"Agent {environment.agent_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        await lifecycle_manager.rebuild_environment(session, environment, agent)

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

    # === Prompt Sync Operations ===

    @staticmethod
    async def sync_agent_prompts_from_environment(
        session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ) -> bool:
        """
        Sync agent prompts from environment docs files back to agent model.

        This should be called after a building mode session completes to capture
        any updates the agent made to WORKFLOW_PROMPT.md and ENTRYPOINT_PROMPT.md.

        Args:
            session: Database session
            environment: Agent environment instance
            agent: Agent instance to update

        Returns:
            True if sync successful
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            lifecycle_manager = EnvironmentService.get_lifecycle_manager()
            adapter = lifecycle_manager.get_adapter(environment)

            # Fetch current prompts from environment
            prompts = await adapter.get_agent_prompts()

            workflow_prompt = prompts.get("workflow_prompt")
            entrypoint_prompt = prompts.get("entrypoint_prompt")

            # Update agent if prompts have changed
            updated = False
            if workflow_prompt and workflow_prompt != agent.workflow_prompt:
                agent.workflow_prompt = workflow_prompt
                updated = True
                logger.info(f"Updated agent {agent.id} workflow_prompt from environment ({len(workflow_prompt)} chars)")

            if entrypoint_prompt and entrypoint_prompt != agent.entrypoint_prompt:
                agent.entrypoint_prompt = entrypoint_prompt
                updated = True
                logger.info(f"Updated agent {agent.id} entrypoint_prompt from environment ({len(entrypoint_prompt)} chars)")

            if updated:
                agent.updated_at = datetime.utcnow()
                session.add(agent)
                session.commit()
                session.refresh(agent)
                logger.info(f"Synced agent prompts from environment {environment.id} to agent {agent.id}")

            return True

        except Exception as e:
            logger.error(f"Failed to sync agent prompts from environment: {e}", exc_info=True)
            return False

    @staticmethod
    async def sync_agent_prompts_to_environment(
        environment: AgentEnvironment,
        workflow_prompt: str | None = None,
        entrypoint_prompt: str | None = None
    ) -> bool:
        """
        Sync agent prompts from backend to environment docs files.

        This should be called when user manually edits prompts in the backend UI
        to ensure the environment has the latest versions.

        Args:
            environment: Agent environment instance
            workflow_prompt: Updated workflow prompt content (None to skip)
            entrypoint_prompt: Updated entrypoint prompt content (None to skip)

        Returns:
            True if sync successful
        """
        try:
            lifecycle_manager = EnvironmentService.get_lifecycle_manager()
            adapter = lifecycle_manager.get_adapter(environment)

            # Push prompts to environment
            await adapter.set_agent_prompts(
                workflow_prompt=workflow_prompt,
                entrypoint_prompt=entrypoint_prompt
            )

            logger.info(f"Synced agent prompts to environment {environment.id}")
            return True

        except Exception as e:
            logger.error(f"Failed to sync agent prompts to environment: {e}", exc_info=True)
            return False

    @staticmethod
    async def handle_stream_completed_event(event_data: dict[str, Any]):
        """
        Event handler for stream_completed events.

        This handler is called automatically when a stream completes.
        If the session was in "building" mode, it syncs agent prompts
        from the environment back to the agent model.

        Args:
            event_data: Event data containing session_id, environment_id, agent_id, etc.
        """
        try:
            meta = event_data.get("meta", {})
            session_mode = meta.get("session_mode")
            environment_id = meta.get("environment_id")
            agent_id = meta.get("agent_id")

            # Only proceed if this was a building session
            if session_mode != "building":
                logger.debug(f"Skipping prompt sync for non-building session (mode: {session_mode})")
                return

            if not environment_id or not agent_id:
                logger.warning("stream_completed event missing environment_id or agent_id in metadata")
                return

            logger.info(f"Handling stream_completed event: syncing prompts from environment {environment_id}")

            # Use a fresh database session for this background task
            with Session(engine) as session:
                environment = session.get(AgentEnvironment, UUID(environment_id))
                agent = session.get(Agent, UUID(agent_id))

                if not environment or not agent:
                    logger.warning(f"Environment {environment_id} or agent {agent_id} not found")
                    return

                # Sync prompts from environment to agent
                success = await EnvironmentService.sync_agent_prompts_from_environment(
                    session=session,
                    environment=environment,
                    agent=agent
                )

                if success:
                    logger.info(f"Successfully synced agent prompts after building session")
                else:
                    logger.warning(f"Failed to sync agent prompts after building session")

        except Exception as e:
            logger.error(f"Error in handle_stream_completed_event: {e}", exc_info=True)
