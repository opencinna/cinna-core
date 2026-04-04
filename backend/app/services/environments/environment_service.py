from uuid import UUID
from datetime import UTC, datetime
import random
import asyncio
import logging
from typing import Any
from sqlmodel import Session, select
from sqlalchemy import or_
from sqlalchemy.orm.attributes import flag_modified
from app.models import AgentEnvironment, AgentEnvironmentCreate, AgentEnvironmentUpdate, Agent, User
from app.models.credentials.ai_credential import AICredential, AICredentialType
from app.core.db import engine, create_session
from app.utils import create_task_with_error_logging
from .environment_lifecycle import EnvironmentLifecycleManager
from app.services.credentials.ai_credentials_service import ai_credentials_service
from .sdk_constants import (
    SDK_ANTHROPIC,
    SDK_MINIMAX,
    DEFAULT_SDK,
    VALID_SDK_OPTIONS,
    SDK_TO_CREDENTIAL_TYPE,
    SDK_ENGINE_CLAUDE_CODE,
    SDK_ENGINE_OPENCODE,
    VALID_SDK_ENGINES,
    SDK_CREDENTIAL_COMPATIBILITY,
    CREDENTIAL_TYPE_TO_BAG_KEY,
    is_valid_sdk,
    make_empty_credential_bag,
    apply_credential_to_bag,
)

logger = logging.getLogger(__name__)


class AgentEnvironmentError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class EnvironmentNotFoundError(AgentEnvironmentError):
    def __init__(self, message: str = "Environment not found"):
        super().__init__(message, status_code=404)


class AgentNotFoundError(AgentEnvironmentError):
    def __init__(self, message: str = "Agent not found"):
        super().__init__(message, status_code=404)


class EnvironmentPermissionDeniedError(AgentEnvironmentError):
    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=403)


class EnvironmentCredentialError(AgentEnvironmentError):
    def __init__(self, message: str):
        super().__init__(message, status_code=400)


def _validate_sdk_credential_compatibility(sdk_id: str, credential: AICredential) -> None:
    """
    Validate that the SDK engine is compatible with the given credential type.

    Args:
        sdk_id: Full SDK ID (e.g., "claude-code/anthropic") or engine prefix
        credential: The AICredential instance to validate against

    Raises:
        EnvironmentCredentialError: If the SDK and credential type are incompatible
    """
    engine = sdk_id.split("/")[0] if "/" in sdk_id else sdk_id
    compatible_types = SDK_CREDENTIAL_COMPATIBILITY.get(engine, [])
    cred_type_value = credential.type.value if hasattr(credential.type, 'value') else str(credential.type)
    if cred_type_value not in compatible_types:
        raise EnvironmentCredentialError(
            f"SDK '{engine}' is not compatible with credential type '{cred_type_value}'. "
            f"Compatible types: {compatible_types}"
        )


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
    def _apply_credential_keys(cred_data, cred_type) -> dict:
        """Legacy helper — delegates to apply_credential_to_bag and returns a dict."""
        bag = make_empty_credential_bag()
        apply_credential_to_bag(bag, cred_type, cred_data)
        # Return only non-None entries
        return {k: v for k, v in bag.items() if v is not None}

    @staticmethod
    def get_environment_with_access_check(
        session: Session, env_id: UUID, user_id: UUID, is_superuser: bool = False
    ) -> tuple["AgentEnvironment", "Agent"]:
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise EnvironmentNotFoundError()
        agent = session.get(Agent, environment.agent_id)
        if not agent:
            raise AgentNotFoundError()
        if not is_superuser and agent.owner_id != user_id:
            raise EnvironmentPermissionDeniedError()
        return environment, agent

    @staticmethod
    async def _create_environment_background(
        env_id: UUID,
        agent_id: UUID,
        credential_bag: dict[str, str | None],
        auto_start: bool = False,
        source_environment_id: UUID | None = None,
    ):
        """
        Background task to create environment instance.
        Uses its own database session to avoid conflicts.
        """
        # Create new database session for background task
        with create_session() as session:
            try:
                # Get environment and agent
                environment = session.get(AgentEnvironment, env_id)
                agent = session.get(Agent, agent_id)

                if not environment or not agent:
                    raise AgentEnvironmentError("Environment or Agent not found")

                # Run creation process
                lifecycle_manager = EnvironmentService.get_lifecycle_manager()
                await lifecycle_manager.create_environment_instance(
                    session, environment, agent,
                    anthropic_api_key=credential_bag.get("anthropic_api_key"),
                    minimax_api_key=credential_bag.get("minimax_api_key"),
                    openai_compatible_api_key=credential_bag.get("openai_compatible_api_key"),
                    openai_compatible_base_url=credential_bag.get("openai_compatible_base_url"),
                    openai_compatible_model=credential_bag.get("openai_compatible_model"),
                    openai_api_key=credential_bag.get("openai_api_key"),
                    google_api_key=credential_bag.get("google_api_key"),
                )

                # Copy workspace from source environment if provided (for clones)
                if source_environment_id:
                    from app.services.sharing.agent_clone_service import AgentCloneService
                    logger.info(f"Copying workspace from {source_environment_id} to {env_id}")
                    await AgentCloneService.copy_workspace(
                        original_env_id=source_environment_id,
                        clone_env_id=env_id
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
                logger.error(f"Error in _create_environment_background: {e}")
                pass

    @staticmethod
    def _find_source_environment_for_workspace_copy(
        session: "Session",
        agent_id: UUID,
        target_env_id: UUID,
        all_envs: list[AgentEnvironment]
    ) -> AgentEnvironment | None:
        """
        Find the best source environment to copy workspace from.

        Priority:
        1. Current active environment (if set and different from target)
        2. Most recently used suspended environment
        3. Environment from the most recent session for this agent

        Args:
            session: Database session
            agent_id: Agent ID
            target_env_id: Target environment ID (to exclude from search)
            all_envs: List of all environments for this agent

        Returns:
            Source environment to copy from, or None if no suitable source found
        """
        from app.models.sessions.session import Session as SessionModel

        agent = session.get(Agent, agent_id)
        if not agent:
            return None

        # 1. Check current active environment
        if agent.active_environment_id and agent.active_environment_id != target_env_id:
            active_env = session.get(AgentEnvironment, agent.active_environment_id)
            if active_env:
                logger.info(f"Using active environment {active_env.id} as workspace source")
                return active_env

        # 2. Look for suspended environments (most recently updated)
        suspended_envs = [
            env for env in all_envs
            if env.status == "suspended" and env.id != target_env_id
        ]
        if suspended_envs:
            # Sort by updated_at descending to get most recent
            suspended_envs.sort(key=lambda e: e.updated_at or e.created_at, reverse=True)
            source_env = suspended_envs[0]
            logger.info(f"Using most recent suspended environment {source_env.id} as workspace source")
            return source_env

        # 3. Find the most recent session for this agent and use its environment
        # Get all environment IDs for this agent (excluding target)
        env_ids = [env.id for env in all_envs if env.id != target_env_id]
        if env_ids:
            # Query for most recent session across all agent's environments
            stmt = (
                select(SessionModel)
                .where(SessionModel.environment_id.in_(env_ids))
                .order_by(SessionModel.updated_at.desc())
                .limit(1)
            )
            recent_session = session.exec(stmt).first()
            if recent_session:
                source_env = session.get(AgentEnvironment, recent_session.environment_id)
                if source_env:
                    logger.info(
                        f"Using environment {source_env.id} from most recent session as workspace source"
                    )
                    return source_env

        logger.info("No suitable source environment found for workspace copy")
        return None

    @staticmethod
    async def _activate_environment_background(
        agent_id: UUID,
        env_id: UUID
    ):
        """
        Background task to activate environment.
        Uses its own database session to avoid conflicts.

        Steps:
        1. Find best source environment for workspace copy
        2. Copy workspace from source to target (if found)
        3. Stop all other running environments
        4. Start target environment
        5. Update is_active flags

        Args:
            agent_id: Agent ID
            env_id: Environment ID to activate
        """
        # Create new database session for background task
        with create_session() as session:
            try:
                # Get agent and target environment
                agent = session.get(Agent, agent_id)
                target_env = session.get(AgentEnvironment, env_id)

                if not agent or not target_env:
                    raise AgentEnvironmentError("Agent or Environment not found")

                # Get all environments for this agent
                all_envs = EnvironmentService.list_agent_environments(session, agent_id)
                lifecycle_manager = EnvironmentService.get_lifecycle_manager()

                # Find best source environment for workspace copy
                source_env = EnvironmentService._find_source_environment_for_workspace_copy(
                    session, agent_id, env_id, all_envs
                )

                # Copy workspace from source to target (if found)
                if source_env:
                    logger.info(f"Copying workspace from environment {source_env.id} to {target_env.id}")
                    try:
                        await lifecycle_manager.copy_workspace_between_environments(
                            source_env, target_env
                        )
                    except Exception as e:
                        logger.warning(f"Failed to copy workspace between environments: {e}")
                        # Continue with activation even if copy fails

                # Stop all other environments first
                for env in all_envs:
                    if env.id != env_id and env.status == "running":
                        try:
                            await lifecycle_manager.stop_environment(session, env)
                            env.is_active = False
                            session.add(env)
                        except Exception as e:
                            # Log but continue
                            logger.warning(f"Failed to stop environment {env.id}: {e}")

                # Start target environment (this updates status internally)
                await lifecycle_manager.start_environment(session, target_env, agent)

                # Update is_active flags for all environments
                for env in all_envs:
                    env.is_active = (env.id == env_id)
                    env.updated_at = datetime.now(UTC)
                    session.add(env)

                # Update agent's active_environment_id
                agent.active_environment_id = env_id
                session.add(agent)

                session.commit()

            except Exception as e:
                # Error is already logged in start_environment
                # Update target environment status to error if not already done
                with create_session() as error_session:
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
        auto_start: bool = False,
        source_environment_id: UUID | None = None
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
            source_environment_id: If provided, copy workspace from this environment after build (for clones)

        Note: The actual Docker build happens asynchronously.
        Use GET /environments/{id}/status to track progress.
        """
        # Get agent
        agent = session.get(Agent, agent_id)
        if not agent:
            raise AgentNotFoundError(f"Agent {agent_id} not found")

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
        if not is_valid_sdk(sdk_conversation):
            raise AgentEnvironmentError(
                f"Invalid agent_sdk_conversation: '{sdk_conversation}'. "
                f"Must be a known SDK or engine/provider format. Valid engines: {VALID_SDK_ENGINES}"
            )
        if sdk_building is not None and not is_valid_sdk(sdk_building):
            raise AgentEnvironmentError(
                f"Invalid agent_sdk_building: '{sdk_building}'. "
                f"Must be a known SDK or engine/provider format. Valid engines: {VALID_SDK_ENGINES}"
            )

        # Credential bag — single dict holding all resolved API keys
        bag = make_empty_credential_bag()

        # Credential IDs for storing on the environment
        conversation_ai_credential_id = None
        building_ai_credential_id = None

        if data.use_default_ai_credentials:
            # Seed bag from user's legacy profile credentials
            ai_credentials = ai_credentials_service.get_user_ai_credentials(user=user)
            if ai_credentials:
                bag["anthropic_api_key"] = ai_credentials.anthropic_api_key or bag["anthropic_api_key"]
                bag["minimax_api_key"] = ai_credentials.minimax_api_key or bag["minimax_api_key"]
                bag["openai_compatible_api_key"] = ai_credentials.openai_compatible_api_key or bag["openai_compatible_api_key"]
                bag["openai_compatible_base_url"] = ai_credentials.openai_compatible_base_url or bag["openai_compatible_base_url"]
                bag["openai_compatible_model"] = ai_credentials.openai_compatible_model or bag["openai_compatible_model"]

            # Override with named default credentials if configured per-mode
            if user.default_ai_credential_conversation_id:
                conv_default_cred = ai_credentials_service.get_credential_for_use(
                    session, user.default_ai_credential_conversation_id, user.id
                )
                if conv_default_cred:
                    cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_conversation)
                    if cred_type:
                        apply_credential_to_bag(bag, cred_type, conv_default_cred)
                        conversation_ai_credential_id = user.default_ai_credential_conversation_id

            if user.default_ai_credential_building_id and sdk_building:
                build_default_cred = ai_credentials_service.get_credential_for_use(
                    session, user.default_ai_credential_building_id, user.id
                )
                if build_default_cred:
                    cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_building)
                    if cred_type:
                        apply_credential_to_bag(bag, cred_type, build_default_cred)
                        building_ai_credential_id = user.default_ai_credential_building_id

            # Validate required credentials for each SDK
            for sdk in {sdk_conversation, sdk_building}:
                if not sdk:
                    continue
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk)
                if cred_type:
                    required_key = CREDENTIAL_TYPE_TO_BAG_KEY.get(cred_type)
                    if required_key and not bag.get(required_key):
                        raise EnvironmentCredentialError(
                            f"Missing required API key for SDK '{sdk}'. "
                            f"Please add it in user settings."
                        )
        else:
            # Resolve conversation credential
            if data.conversation_ai_credential_id:
                conv_cred_obj = session.get(AICredential, data.conversation_ai_credential_id)
                if not conv_cred_obj or conv_cred_obj.owner_id != user.id:
                    raise EnvironmentCredentialError("Cannot access the specified conversation AI credential")
                _validate_sdk_credential_compatibility(sdk_conversation, conv_cred_obj)
                conv_cred_data = ai_credentials_service.get_credential_for_use(
                    session, data.conversation_ai_credential_id, user.id
                )
                if not conv_cred_data:
                    raise EnvironmentCredentialError("Cannot access the specified conversation AI credential")
                conversation_ai_credential_id = data.conversation_ai_credential_id
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_conversation)
                if cred_type:
                    apply_credential_to_bag(bag, cred_type, conv_cred_data)
            else:
                # Fall back to user's default for conversation SDK
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_conversation)
                if cred_type:
                    default_cred = ai_credentials_service.get_default_for_type(session, user.id, cred_type)
                    if default_cred:
                        apply_credential_to_bag(bag, cred_type, ai_credentials_service.decrypt_credential(default_cred))
                    else:
                        logger.debug(f"No default credential found for type {cred_type}")

            # Resolve building credential
            if data.building_ai_credential_id:
                build_cred_obj = session.get(AICredential, data.building_ai_credential_id)
                if not build_cred_obj or build_cred_obj.owner_id != user.id:
                    raise EnvironmentCredentialError("Cannot access the specified building AI credential")
                if sdk_building:
                    _validate_sdk_credential_compatibility(sdk_building, build_cred_obj)
                build_cred_data = ai_credentials_service.get_credential_for_use(
                    session, data.building_ai_credential_id, user.id
                )
                if not build_cred_data:
                    raise EnvironmentCredentialError("Cannot access the specified building AI credential")
                building_ai_credential_id = data.building_ai_credential_id
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_building)
                if cred_type:
                    apply_credential_to_bag(bag, cred_type, build_cred_data)
            else:
                # Fall back to user's default for building SDK if not same as conversation
                if sdk_building != sdk_conversation or not data.conversation_ai_credential_id:
                    cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_building)
                    if cred_type:
                        default_cred = ai_credentials_service.get_default_for_type(session, user.id, cred_type)
                        if default_cred:
                            apply_credential_to_bag(bag, cred_type, ai_credentials_service.decrypt_credential(default_cred))

            # Validate that we have the required API keys for all SDK types
            for sdk in {sdk_conversation, sdk_building}:
                if not sdk:
                    continue
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk)
                if cred_type:
                    required_key = CREDENTIAL_TYPE_TO_BAG_KEY.get(cred_type)
                    if required_key and not bag.get(required_key):
                        raise EnvironmentCredentialError(
                            f"Missing required API key for SDK '{sdk}'. "
                            f"Please select an AI credential or set a default."
                        )

        # Generate a memorable instance name if not provided
        instance_name = data.instance_name if data.instance_name != "Instance" else generate_environment_name()

        # Resolve model overrides: explicit request data takes priority, then user's saved defaults
        model_override_conversation = (
            data.model_override_conversation
            or getattr(user, "default_model_override_conversation", None)
            or None
        )
        model_override_building = (
            data.model_override_building
            or getattr(user, "default_model_override_building", None)
            or None
        )

        # Create DB record with initial status
        environment = AgentEnvironment.model_validate(
            data, update={
                "agent_id": agent_id,
                "instance_name": instance_name,
                "status": "creating",
                "status_message": "Initializing environment creation...",
                "agent_sdk_conversation": sdk_conversation,
                "agent_sdk_building": sdk_building,
                "model_override_conversation": model_override_conversation,
                "model_override_building": model_override_building,
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
        create_task_with_error_logging(
            EnvironmentService._create_environment_background(
                environment.id, agent_id, bag, auto_start,
                source_environment_id,
            ),
            "create_environment",
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
        environment.updated_at = datetime.now(UTC)

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
                logger.warning(f"Failed to stop environment: {e}")

        # Delete Docker instance (directory, volumes, networks, etc.)
        try:
            await lifecycle_manager.delete_environment_instance(environment)
        except Exception as e:
            # Log error but continue with DB deletion
            logger.warning(f"Failed to delete environment instance: {e}")

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
            raise AgentNotFoundError(f"Agent {agent_id} not found")

        # Get target environment
        target_env = session.get(AgentEnvironment, env_id)
        if not target_env or target_env.agent_id != agent_id:
            raise EnvironmentNotFoundError("Environment not found for this agent")

        # Update target environment status immediately
        target_env.status = "starting"
        target_env.status_message = "Preparing to activate environment..."
        session.add(target_env)
        session.commit()
        session.refresh(target_env)

        # Spawn background task to activate environment
        # This allows the API to return immediately while activation happens asynchronously
        create_task_with_error_logging(
            EnvironmentService._activate_environment_background(agent_id, env_id),
            "activate_environment",
        )

        return target_env

    # === Lifecycle Operations ===

    @staticmethod
    async def start_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """Start environment container"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise EnvironmentNotFoundError(f"Environment {env_id} not found")

        agent = session.get(Agent, environment.agent_id)
        if not agent:
            raise AgentNotFoundError(f"Agent {environment.agent_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        await lifecycle_manager.start_environment(session, environment, agent)

        session.refresh(environment)
        return environment

    @staticmethod
    async def stop_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """Stop environment container"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise EnvironmentNotFoundError(f"Environment {env_id} not found")

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
            raise EnvironmentNotFoundError(f"Environment {env_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        await lifecycle_manager.suspend_environment(session, environment)

        session.refresh(environment)
        return environment

    @staticmethod
    async def restart_environment(session: Session, env_id: UUID) -> AgentEnvironment:
        """Restart environment container"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise EnvironmentNotFoundError(f"Environment {env_id} not found")

        agent = session.get(Agent, environment.agent_id)
        if not agent:
            raise AgentNotFoundError(f"Agent {environment.agent_id} not found")

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
            raise EnvironmentNotFoundError(f"Environment {env_id} not found")

        agent = session.get(Agent, environment.agent_id)
        if not agent:
            raise AgentNotFoundError(f"Agent {environment.agent_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        await lifecycle_manager.rebuild_environment(session, environment, agent)

        session.refresh(environment)
        return environment

    @staticmethod
    async def get_environment_status(session: Session, env_id: UUID) -> dict:
        """Get environment status"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise EnvironmentNotFoundError(f"Environment {env_id} not found")

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
            raise EnvironmentNotFoundError(f"Environment {env_id} not found")

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        health = await lifecycle_manager.check_health(session, environment)

        return health

    @staticmethod
    async def get_environment_logs(session: Session, env_id: UUID, lines: int = 100) -> list[str]:
        """Get environment logs"""
        environment = session.get(AgentEnvironment, env_id)
        if not environment:
            raise EnvironmentNotFoundError(f"Environment {env_id} not found")

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
        any updates the agent made to WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md, and REFINER_PROMPT.md.

        When workflow_prompt changes, this also triggers:
        - A2A skills regeneration
        - Background description generation

        Args:
            session: Database session
            environment: Agent environment instance
            agent: Agent instance to update

        Returns:
            True if sync successful
        """
        from app.services.agents.agent_service import AgentService

        try:
            lifecycle_manager = EnvironmentService.get_lifecycle_manager()
            adapter = lifecycle_manager.get_adapter(environment)

            # Fetch current prompts from environment
            prompts = await adapter.get_agent_prompts()

            workflow_prompt = prompts.get("workflow_prompt")
            entrypoint_prompt = prompts.get("entrypoint_prompt")
            refiner_prompt = prompts.get("refiner_prompt")

            # Update agent if prompts have changed
            updated = False

            if workflow_prompt and workflow_prompt != agent.workflow_prompt:
                # Use unified handler for workflow_prompt changes
                # This regenerates A2A skills and triggers background description update
                AgentService.handle_workflow_prompt_change(
                    agent=agent,
                    new_workflow_prompt=workflow_prompt,
                    trigger_description_update=True
                )
                agent.workflow_prompt = workflow_prompt
                updated = True
                logger.info(f"Updated agent {agent.id} workflow_prompt from environment ({len(workflow_prompt)} chars)")

            if entrypoint_prompt and entrypoint_prompt != agent.entrypoint_prompt:
                agent.entrypoint_prompt = entrypoint_prompt
                updated = True
                logger.info(f"Updated agent {agent.id} entrypoint_prompt from environment ({len(entrypoint_prompt)} chars)")

            if refiner_prompt and refiner_prompt != agent.refiner_prompt:
                agent.refiner_prompt = refiner_prompt
                updated = True
                logger.info(f"Updated agent {agent.id} refiner_prompt from environment ({len(refiner_prompt)} chars)")

            if updated:
                agent.updated_at = datetime.now(UTC)
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
        entrypoint_prompt: str | None = None,
        refiner_prompt: str | None = None
    ) -> bool:
        """
        Sync agent prompts from backend to environment docs files.

        This should be called when user manually edits prompts in the backend UI
        to ensure the environment has the latest versions.

        Args:
            environment: Agent environment instance
            workflow_prompt: Updated workflow prompt content (None to skip)
            entrypoint_prompt: Updated entrypoint prompt content (None to skip)
            refiner_prompt: Updated refiner prompt content (None to skip)

        Returns:
            True if sync successful
        """
        try:
            lifecycle_manager = EnvironmentService.get_lifecycle_manager()
            adapter = lifecycle_manager.get_adapter(environment)

            # Push prompts to environment
            await adapter.set_agent_prompts(
                workflow_prompt=workflow_prompt,
                entrypoint_prompt=entrypoint_prompt,
                refiner_prompt=refiner_prompt
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
            with create_session() as session:
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

    @staticmethod
    def get_environments_for_credential(
        session: Session,
        credential: AICredential,
    ) -> list[dict]:
        """
        Find all environments that use a given AI credential, either explicitly
        (via conversation/building_ai_credential_id) or implicitly (via
        use_default_ai_credentials when the credential is the user's default).

        Returns a list of dicts with keys: environment, agent, owner, usage.
        """
        credential_id = credential.id

        # 1. Environments explicitly linked to this credential
        explicit_stmt = (
            select(AgentEnvironment, Agent, User)
            .join(Agent, AgentEnvironment.agent_id == Agent.id)
            .join(User, Agent.owner_id == User.id)
            .where(
                or_(
                    AgentEnvironment.conversation_ai_credential_id == credential_id,
                    AgentEnvironment.building_ai_credential_id == credential_id,
                )
            )
        )
        explicit_results = session.exec(explicit_stmt).all()

        results: list[dict] = []
        seen_env_ids: set[UUID] = set()

        for env, agent, owner in explicit_results:
            usage = []
            if env.conversation_ai_credential_id == credential_id:
                usage.append("conversation")
            if env.building_ai_credential_id == credential_id:
                usage.append("building")

            results.append({
                "environment": env,
                "agent": agent,
                "owner": owner,
                "usage": " & ".join(usage),
            })
            seen_env_ids.add(env.id)

        # 2. Environments using default credentials where this credential is the default
        if not credential.is_default:
            return results

        # Find SDK IDs that map to this credential type
        matching_sdks = [
            sdk for sdk, cred_type in SDK_TO_CREDENTIAL_TYPE.items()
            if cred_type.value == credential.type
        ]
        if not matching_sdks:
            return results

        # Query environments owned by the credential owner that use defaults
        # and have a matching SDK
        sdk_filters = []
        for sdk in matching_sdks:
            sdk_filters.append(AgentEnvironment.agent_sdk_conversation == sdk)
            sdk_filters.append(AgentEnvironment.agent_sdk_building == sdk)

        default_stmt = (
            select(AgentEnvironment, Agent, User)
            .join(Agent, AgentEnvironment.agent_id == Agent.id)
            .join(User, Agent.owner_id == User.id)
            .where(
                Agent.owner_id == credential.owner_id,
                AgentEnvironment.use_default_ai_credentials.is_(True),
                or_(*sdk_filters),
            )
        )
        default_results = session.exec(default_stmt).all()

        for env, agent, owner in default_results:
            if env.id in seen_env_ids:
                continue

            usage = []
            if env.agent_sdk_conversation in matching_sdks:
                usage.append("conversation")
            if env.agent_sdk_building in matching_sdks:
                usage.append("building")

            if usage:
                results.append({
                    "environment": env,
                    "agent": agent,
                    "owner": owner,
                    "usage": " & ".join(usage),
                })

        return results
