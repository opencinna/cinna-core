import os
import shutil
import logging
import asyncio
from pathlib import Path
from uuid import UUID
from sqlmodel import Session, select
from sqlalchemy.orm.attributes import flag_modified
from typing import Optional
from datetime import UTC, datetime, timedelta

from app.models.environments.environment import AgentEnvironment
from app.models.agents.agent import Agent
from app.models import User
from app.models.credentials.ai_credential import AICredentialType
from app.core.config import settings
from app.core import security
from app.utils import detect_anthropic_credential_type
from .adapters.base import EnvironmentAdapter, EnvInitConfig
from .adapters.docker_adapter import DockerEnvironmentAdapter
from .sdk_constants import SDK_TO_CREDENTIAL_TYPE, apply_credential_to_bag, make_empty_credential_bag

logger = logging.getLogger(__name__)

# Files from template root that should be overwritten during rebuild
# These are infrastructure files that may be updated in the template
REBUILD_OVERWRITE_FILES = [
    "uv.lock",
    "pyproject.toml",
    "Dockerfile",
    "docker-compose.template.yml",
]

# Shared app/core directory used by all environment templates
APP_CORE_BASE_DIR_NAME = "app_core_base"


class EnvironmentLifecycleManager:
    """
    Manages environment lifecycle using Docker terminology:

    Container Operations (Docker terminology):
    - UP: Create and start container (docker-compose up)
    - STOP: Stop container but keep it (docker-compose stop)
    - DOWN: Remove container completely (docker-compose down)

    Lifecycle Methods:
    - create_environment_instance: Copy template, build image, prepare instance
    - start_environment: Start/create container (UP), setup if new, sync data
    - stop_environment: Stop container but keep it (STOP)
    - suspend_environment: Stop container to save resources (STOP with status=suspended)
    - activate_suspended_environment: Restart suspended container (UP), sync data only
    - rebuild_environment: Update infrastructure (DOWN + build + UP), full setup
    - delete_environment_instance: Remove all resources (DOWN + cleanup)

    Data Sync Strategy:
    - DYNAMIC DATA (synced every UP): prompts, credentials, plugins, handover config
    - CONTAINER SETUP (only for NEW containers): custom packages, system files
    """

    def __init__(self):
        self.templates_dir = Path(settings.ENV_TEMPLATES_DIR)
        self.instances_dir = Path(settings.ENV_INSTANCES_DIR)
        self.port_range_start = settings.AGENT_PORT_RANGE_START
        self.port_range_end = settings.AGENT_PORT_RANGE_END
        self._allocated_ports = set()

    def get_adapter(self, environment: AgentEnvironment) -> EnvironmentAdapter:
        """
        Get appropriate adapter for environment type.

        Args:
            environment: Environment instance

        Returns:
            Adapter implementation
        """
        if environment.type == "docker":
            env_dir = self.instances_dir / str(environment.id)
            port = environment.config.get("port", self._allocate_port())
            auth_token = environment.config.get("auth_token")

            return DockerEnvironmentAdapter(
                env_id=environment.id,
                env_dir=env_dir,
                port=port,
                container_name=f"agent-{environment.id}",
                auth_token=auth_token
            )
        else:
            raise NotImplementedError(f"Environment type '{environment.type}' not implemented")

    async def _container_exists(self, environment: AgentEnvironment) -> bool:
        """
        Check if container exists (regardless of running state).

        Args:
            environment: Environment instance

        Returns:
            True if container exists (stopped or running)
        """
        adapter = self.get_adapter(environment)
        try:
            container = adapter.get_container()
            return container is not None
        except Exception:
            return False

    async def create_environment_instance(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent,
        anthropic_api_key: str | None = None,
        minimax_api_key: str | None = None,
        openai_compatible_api_key: str | None = None,
        openai_compatible_base_url: str | None = None,
        openai_compatible_model: str | None = None,
        openai_api_key: str | None = None,
        google_api_key: str | None = None,
    ) -> bool:
        """
        Create environment instance:
        1. Copy template files to instance directory
        2. Generate docker-compose.yml from template
        3. Create .env file with environment variables
        4. Build Docker image

        Args:
            db_session: Database session
            environment: Environment model
            agent: Agent model
            anthropic_api_key: User's Anthropic API key (optional)
            minimax_api_key: User's MiniMax API key (optional)
            openai_compatible_api_key: User's OpenAI Compatible API key (optional)
            openai_compatible_base_url: User's OpenAI Compatible base URL (optional)
            openai_compatible_model: User's OpenAI Compatible model (optional)
            openai_api_key: OpenAI API key (for opencode/openai)
            google_api_key: Google API key (for opencode/google)

        Returns:
            True if creation successful
        """
        try:
            # Update status: Creating
            environment.status = "creating"
            environment.status_message = "Preparing environment..."
            db_session.add(environment)
            db_session.commit()

            # 1. Setup directories
            template_dir = self.templates_dir / environment.env_name
            instance_dir = self.instances_dir / str(environment.id)

            logger.info(f"Creating environment instance {environment.id} from template {environment.env_name}")
            logger.debug(f"Template dir: {template_dir} (exists: {template_dir.exists()})")
            logger.debug(f"Instance dir: {instance_dir}")

            if not template_dir.exists():
                raise FileNotFoundError(f"Template not found: {environment.env_name} at {template_dir}")

            # Create instance directory
            instance_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Instance directory created: {instance_dir}")

            # 2. Copy template files
            environment.status_message = "Copying template files..."
            db_session.add(environment)
            db_session.commit()

            await self._copy_template(template_dir, instance_dir)

            # 3. Allocate port (only on first create)
            environment.status_message = "Configuring environment..."
            db_session.add(environment)
            db_session.commit()

            port = self._allocate_port()
            environment.config["port"] = port
            environment.config["container_name"] = f"agent-{environment.id}"
            flag_modified(environment, "config")
            logger.debug(f"Allocated port {port} for environment {environment.id}")

            # 4. Update configuration files (auth token, compose, env)
            self._update_environment_config(
                db_session, instance_dir, environment, agent,
                anthropic_api_key, minimax_api_key,
                openai_compatible_api_key, openai_compatible_base_url, openai_compatible_model,
                openai_api_key=openai_api_key,
                google_api_key=google_api_key,
            )

            # 6. Build image
            environment.status = "building"
            environment.status_message = "Building Docker image (this may take several minutes)..."
            db_session.add(environment)
            db_session.commit()

            logger.info(f"Building Docker image for environment {environment.id}")
            adapter = self.get_adapter(environment)
            await adapter.initialize(
                EnvInitConfig(
                    env_name=environment.env_name,
                    env_version=environment.env_version,
                    agent_id=agent.id,
                    workspace_id=str(environment.id)
                )
            )
            logger.info(f"Environment {environment.id} initialized successfully")

            # Update environment status
            environment.status = "stopped"
            environment.status_message = "Environment ready"
            db_session.add(environment)
            db_session.commit()

            return True

        except Exception as e:
            # Update status to error with detailed message
            environment.status = "error"
            environment.status_message = f"Failed to create environment: {str(e)}"
            db_session.add(environment)
            db_session.commit()
            logger.error(f"Failed to create environment {environment.id}: {e}")
            raise

    async def _sync_dynamic_data(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ):
        """
        Sync dynamic agent data to running container.

        DYNAMIC DATA (synced every time on activation/up):
        - Agent prompts (workflow, entrypoint)
        - Credentials files
        - Plugins configuration
        - Handover configuration

        This should be called every time container becomes running:
        - After container starts (new or existing)
        - After backend updates (even if container was already running)

        Note: Handover config sync is critical for cloned agents to ensure
        they get empty handover config (queried from DB) instead of stale
        parent config that may have been copied during workspace copy.

        Args:
            db_session: Database session
            environment: Environment instance
            agent: Agent instance
        """
        adapter = self.get_adapter(environment)

        # Set prompts in docs files
        environment.status_message = "Syncing agent prompts..."
        db_session.add(environment)
        db_session.commit()

        await adapter.set_agent_prompts(
            workflow_prompt=agent.workflow_prompt,
            entrypoint_prompt=agent.entrypoint_prompt
        )

        # Sync credentials to environment
        environment.status_message = "Syncing credentials..."
        db_session.add(environment)
        db_session.commit()

        from app.services.credentials.credentials_service import CredentialsService
        credentials_data = CredentialsService.prepare_credentials_for_environment(
            session=db_session,
            agent_id=agent.id
        )
        await adapter.set_credentials(credentials_data)

        # Sync plugins to environment
        environment.status_message = "Syncing plugins..."
        db_session.add(environment)
        db_session.commit()

        await self._sync_plugins_to_environment(db_session, environment, agent)

        # Sync handover configuration to environment
        # This ensures cloned agents get empty handover config (not stale parent config)
        # and all agents have current handover state on activation
        environment.status_message = "Syncing handover configuration..."
        db_session.add(environment)
        db_session.commit()

        from app.services.agents.agent_service import AgentService
        await AgentService.sync_agent_handover_config(db_session, agent.id)

    async def _sync_plugins_to_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ):
        """
        Sync installed plugins to the agent environment.

        Args:
            db_session: Database session
            environment: Environment instance
            agent: Agent instance
        """
        from app.services.plugins.llm_plugin_service import LLMPluginService

        adapter = self.get_adapter(environment)

        # Get allowed_tools from agent SDK config
        allowed_tools = None
        if agent.agent_sdk_config:
            allowed_tools = agent.agent_sdk_config.get("allowed_tools", [])

        # Prepare plugin data with allowed_tools
        plugins_data = LLMPluginService.prepare_plugins_for_environment(
            session=db_session,
            agent_id=agent.id,
            allowed_tools=allowed_tools
        )

        # Get plugin files for each installed plugin
        plugin_files = {}
        for plugin_info in plugins_data.get("active_plugins", []):
            # Find the plugin link to get plugin_id
            from app.models.plugins.llm_plugin import AgentPluginLink, LLMPluginMarketplacePlugin
            link = db_session.exec(
                select(AgentPluginLink).where(
                    AgentPluginLink.agent_id == agent.id
                ).join(LLMPluginMarketplacePlugin).where(
                    LLMPluginMarketplacePlugin.name == plugin_info["plugin_name"]
                )
            ).first()

            if link:
                try:
                    files = LLMPluginService.get_plugin_files(
                        session=db_session,
                        plugin_id=link.plugin_id,
                        commit_hash=plugin_info.get("commit_hash"),
                        user_id=agent.owner_id
                    )
                    # Encode files as base64 for JSON transport
                    import base64
                    encoded_files = {
                        path: base64.b64encode(content).decode('utf-8')
                        for path, content in files.items()
                    }
                    plugin_key = f"{plugin_info['marketplace_name']}/{plugin_info['plugin_name']}"
                    plugin_files[plugin_key] = encoded_files
                except Exception as e:
                    logger.warning(f"Failed to get files for plugin {plugin_info['plugin_name']}: {e}")

        # Combine plugins data with files
        full_plugins_data = {
            **plugins_data,
            "plugin_files": plugin_files,
        }

        await adapter.set_plugins(full_plugins_data)

    async def _setup_new_container(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ):
        """
        Setup operations for NEW container only.

        CONTAINER SETUP (only when container is newly created):
        - Installing custom Python dependencies from workspace_requirements.txt
        - Installing system packages from workspace_system_packages.txt
        - Any other one-time setup for new containers

        This should NOT be called when:
        - Restarting an existing stopped container
        - Container already exists and is just being started

        Args:
            db_session: Database session
            environment: Environment instance
            agent: Agent instance
        """
        adapter = self.get_adapter(environment)

        # Install custom packages (only needed for new containers)
        environment.status_message = "Installing custom packages..."
        db_session.add(environment)
        db_session.commit()

        await adapter.install_custom_packages()

        # Install system packages (only needed for new containers)
        environment.status_message = "Installing system packages..."
        db_session.add(environment)
        db_session.commit()

        await adapter.install_system_packages()
        logger.debug(f"New container setup completed for environment {environment.id}")

    async def start_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ) -> bool:
        """
        Start (up) environment container.

        Docker terminology: 'docker-compose up' - creates and starts container

        Process:
        1. Check if container exists
        2. Update configuration files (regenerate auth token, docker-compose.yml, .env)
        3. Start/create container (docker up)
        4. Setup new container if it was just created
        5. Sync dynamic data (always)
        6. Update status to 'running'

        Args:
            db_session: Database session
            environment: Environment instance
            agent: Agent instance
        """
        # Update status
        environment.status = "starting"
        environment.status_message = "Checking container state..."
        db_session.add(environment)
        db_session.commit()

        # Emit ENVIRONMENT_ACTIVATING event so frontend can show loading state
        from app.services.events.event_service import event_service
        from app.models.events.event import EventType
        await event_service.emit_event(
            event_type=EventType.ENVIRONMENT_ACTIVATING,
            model_id=environment.id,
            user_id=agent.owner_id,
            meta={
                "environment_id": str(environment.id),
                "agent_id": str(agent.id),
                "instance_name": environment.instance_name
            }
        )
        logger.info(f"Emitted ENVIRONMENT_ACTIVATING event for environment {environment.id}")

        try:
            # Check if container exists
            container_existed = await self._container_exists(environment)
            logger.info(f"Starting environment {environment.id} (container_existed={container_existed})")

            # Get instance directory
            instance_dir = self.instances_dir / str(environment.id)

            # Update configuration files (generates new auth token, docker-compose.yml, .env)
            # This ensures the environment always has a fresh JWT token before starting
            environment.status_message = "Updating configuration files..."
            db_session.add(environment)
            db_session.commit()

            self._update_environment_config(db_session, instance_dir, environment, agent)
            db_session.add(environment)  # Save updated config with new auth token
            db_session.commit()

            # Get adapter
            adapter = self.get_adapter(environment)

            # Start container (docker-compose up)
            environment.status_message = "Starting container..."
            db_session.add(environment)
            db_session.commit()

            await adapter.start()

            # Setup new container if it was just created
            if not container_existed:
                logger.info(f"Setting up new container for environment {environment.id}")
                await self._setup_new_container(db_session, environment, agent)

            # Always sync dynamic data
            await self._sync_dynamic_data(db_session, environment, agent)

            # Update status
            environment.status = "running"
            environment.status_message = "Environment is running"
            environment.last_health_check = datetime.now(UTC)
            db_session.add(environment)
            db_session.commit()

            # Emit ENVIRONMENT_ACTIVATED event to process any pending sessions
            # This is critical for handovers that occur while environment is building/starting
            from app.services.events.event_service import event_service
            from app.models.events.event import EventType
            await event_service.emit_event(
                event_type=EventType.ENVIRONMENT_ACTIVATED,
                model_id=environment.id,
                user_id=agent.owner_id,
                meta={
                    "environment_id": str(environment.id),
                    "agent_id": str(agent.id),
                    "instance_name": environment.instance_name
                }
            )
            logger.info(f"Emitted ENVIRONMENT_ACTIVATED event for environment {environment.id}")

            return True

        except Exception as e:
            # Update status to error
            environment.status = "error"
            environment.status_message = f"Failed to start environment: {str(e)}"
            environment.config["last_error"] = str(e)
            flag_modified(environment, "config")
            db_session.add(environment)
            db_session.commit()
            raise

    async def stop_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment
    ) -> bool:
        """
        Stop environment container (keeps container).

        Docker terminology: 'docker-compose stop' - stops container but keeps it

        The container can be quickly restarted later without rebuilding.

        Args:
            db_session: Database session
            environment: Environment instance

        Returns:
            True if successful
        """
        try:
            logger.info(f"Stopping environment {environment.id}")
            adapter = self.get_adapter(environment)
            await adapter.stop()

            environment.status = "stopped"
            environment.status_message = "Environment stopped"
            db_session.add(environment)
            db_session.commit()

            logger.info(f"Environment {environment.id} stopped successfully")
            return True
        except Exception as e:
            environment.status = "error"
            environment.status_message = f"Failed to stop environment: {str(e)}"
            environment.config["last_error"] = str(e)
            flag_modified(environment, "config")
            db_session.add(environment)
            db_session.commit()
            logger.error(f"Failed to stop environment {environment.id}: {e}")
            raise

    async def suspend_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment
    ) -> bool:
        """
        Suspend environment container to save resources.

        Docker terminology: 'docker-compose stop' - stops container but keeps it

        This stops the container but keeps the status as 'suspended' instead of 'stopped',
        indicating it will be automatically reactivated when needed.
        The container can be quickly restarted without rebuilding.

        Args:
            db_session: Database session
            environment: Environment instance

        Returns:
            True if suspension successful
        """
        try:
            logger.info(f"Suspending environment {environment.id}")
            adapter = self.get_adapter(environment)
            await adapter.stop()

            environment.status = "suspended"
            environment.status_message = "Environment suspended due to inactivity"
            db_session.add(environment)
            db_session.commit()

            logger.info(f"Environment {environment.id} suspended successfully")
            return True

        except Exception as e:
            environment.status = "error"
            environment.status_message = f"Failed to suspend environment: {str(e)}"
            environment.config["last_error"] = str(e)
            flag_modified(environment, "config")
            db_session.add(environment)
            db_session.commit()
            logger.error(f"Failed to suspend environment {environment.id}: {e}")
            raise

    async def activate_suspended_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent,
        emit_events: bool = True
    ) -> bool:
        """
        Activate a suspended environment.

        Docker terminology: 'docker-compose up' - starts existing stopped container

        When a container is suspended, it exists but is stopped. We just need to:
        1. Start the existing container (docker-compose up)
        2. Sync dynamic data (prompts and credentials)

        NO container setup needed since container already exists and was previously configured.

        Args:
            db_session: Database session
            environment: Environment instance (must be in 'suspended' status)
            agent: Agent instance
            emit_events: If True, emit activation events via event service

        Returns:
            True if activation successful
        """
        try:
            logger.info(f"Activating suspended environment {environment.id}")

            # Emit activating event
            if emit_events:
                from app.services.events.event_service import event_service
                from app.models.events.event import EventType
                await event_service.emit_event(
                    event_type=EventType.ENVIRONMENT_ACTIVATING,
                    model_id=environment.id,
                    user_id=agent.owner_id,
                    meta={
                        "environment_id": str(environment.id),
                        "agent_id": str(agent.id),
                        "instance_name": environment.instance_name
                    }
                )

            # Update status
            environment.status = "activating"
            environment.status_message = "Activating environment..."
            db_session.add(environment)
            db_session.commit()

            # Get instance directory
            instance_dir = self.instances_dir / str(environment.id)

            # Update configuration files (generates new auth token, docker-compose.yml, .env)
            environment.status_message = "Updating configuration files..."
            db_session.add(environment)
            db_session.commit()

            self._update_environment_config(db_session, instance_dir, environment, agent)
            db_session.add(environment)
            db_session.commit()

            # Get adapter
            adapter = self.get_adapter(environment)

            # Start container (docker-compose up on existing stopped container)
            environment.status_message = "Starting container..."
            db_session.add(environment)
            db_session.commit()

            await adapter.start()

            # Container already exists and was previously set up, so skip container setup
            # Only sync dynamic data (prompts and credentials)
            logger.info(f"Syncing dynamic data for suspended environment {environment.id}")
            await self._sync_dynamic_data(db_session, environment, agent)

            # Update status
            environment.status = "running"
            environment.status_message = "Environment activated"
            environment.last_health_check = datetime.now(UTC)
            environment.last_activity_at = datetime.now(UTC)
            db_session.add(environment)
            db_session.commit()

            # Emit activated event
            if emit_events:
                await event_service.emit_event(
                    event_type=EventType.ENVIRONMENT_ACTIVATED,
                    model_id=environment.id,
                    user_id=agent.owner_id,
                    meta={
                        "environment_id": str(environment.id),
                        "agent_id": str(agent.id),
                        "instance_name": environment.instance_name
                    }
                )

            logger.info(f"Environment {environment.id} activated successfully")
            return True

        except Exception as e:
            # Update status to error
            environment.status = "error"
            environment.status_message = f"Failed to activate environment: {str(e)}"
            environment.config["last_error"] = str(e)
            flag_modified(environment, "config")
            db_session.add(environment)
            db_session.commit()

            # Emit activation failed event
            if emit_events:
                from app.services.events.event_service import event_service
                from app.models.events.event import EventType
                await event_service.emit_event(
                    event_type=EventType.ENVIRONMENT_ACTIVATION_FAILED,
                    model_id=environment.id,
                    user_id=agent.owner_id,
                    meta={
                        "environment_id": str(environment.id),
                        "agent_id": str(agent.id),
                        "error": str(e)
                    }
                )

            logger.error(f"Failed to activate environment {environment.id}: {e}")
            raise

    async def restart_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ) -> bool:
        """Restart environment."""
        await self.stop_environment(db_session, environment)
        await self.start_environment(db_session, environment, agent)
        return True

    async def rebuild_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ) -> bool:
        """
        Rebuild environment with updated core files while preserving workspace.

        Docker terminology: 'docker-compose down' + 'docker-compose build' + 'docker-compose up'

        This operation:
        1. Checks if container is running
        2. Stops container if running (docker-compose stop)
        3. Deletes container (docker-compose down) - NEW container will be created
        4. Updates core files from template
        5. Rebuilds Docker image (docker-compose build)
        6. Starts NEW container if it was running before (docker-compose up)
        7. Setup new container (install packages, etc.)
        8. Syncs dynamic data (prompts and credentials)

        Args:
            db_session: Database session
            environment: Environment instance
            agent: Agent instance

        Returns:
            True if rebuild successful
        """
        from app.services.events.event_service import event_service
        from app.models.events.event import EventType

        try:
            # Check current status
            current_status = await self.get_status(environment)
            was_running = current_status == "running"

            logger.info(f"Rebuilding environment {environment.id} (was_running={was_running})")

            # Update status
            environment.status = "rebuilding"
            environment.status_message = "Stopping container for rebuild..."
            db_session.add(environment)
            db_session.commit()

            # Notify frontend immediately so the App icon updates
            await event_service.emit_event(
                event_type=EventType.ENVIRONMENT_STATUS_CHANGED,
                model_id=environment.id,
                user_id=agent.owner_id,
                meta={
                    "environment_id": str(environment.id),
                    "agent_id": str(agent.id),
                    "status": "rebuilding",
                }
            )

            # Stop if running
            if was_running:
                await self.stop_environment(db_session, environment)

            # Get adapter
            adapter = self.get_adapter(environment)

            # Get template directory and shared core directory
            template_dir = self.templates_dir / environment.env_name
            template_core_dir = self.templates_dir / APP_CORE_BASE_DIR_NAME / "core"

            if not template_core_dir.exists():
                raise FileNotFoundError(f"Shared app_core_base directory not found: {template_core_dir}")

            # Get instance directory
            instance_dir = self.instances_dir / str(environment.id)

            # Update configuration files (generates new auth token, docker-compose.yml, .env)
            environment.status_message = "Updating configuration files..."
            db_session.add(environment)
            db_session.commit()

            self._update_environment_config(db_session, instance_dir, environment, agent)
            db_session.add(environment)  # Save updated config with new auth token
            db_session.commit()

            # Update status
            environment.status_message = "Updating core files and rebuilding image..."
            db_session.add(environment)
            db_session.commit()

            # Rebuild via adapter (does: down, update files, build, optionally up)
            await adapter.rebuild(
                template_dir=template_dir,
                template_core_dir=template_core_dir,
                rebuild_overwrite_files=REBUILD_OVERWRITE_FILES,
                was_running=was_running
            )

            # Regenerate SDK settings files after core replacement (MiniMax settings)
            # These files are in /app/core/.claude/ which get replaced during rebuild
            sdk_conversation = environment.agent_sdk_conversation or "claude-code/anthropic"
            sdk_building = environment.agent_sdk_building or "claude-code/anthropic"
            uses_minimax = sdk_conversation == "claude-code/minimax" or sdk_building == "claude-code/minimax"

            # Fetch user credentials for SDK settings regeneration
            # If specific credentials are assigned, use ONLY those (no fallback to user profile)
            user = db_session.get(User, agent.owner_id)
            anthropic_api_key = None
            minimax_api_key = None
            openai_compatible_api_key = None
            openai_compatible_base_url = None
            openai_compatible_model = None

            # Track if specific credentials are assigned (to prevent fallback)
            has_assigned_credentials = (
                environment.conversation_ai_credential_id is not None or
                environment.building_ai_credential_id is not None
            )

            if user:
                from app.services.credentials.ai_credentials_service import ai_credentials_service

                # Initialise credential variables for all supported types
                openai_api_key = None
                google_api_key = None

                def _apply_cred_to_vars(cred_type, cred_data):
                    """Apply credential data to the appropriate local variable."""
                    nonlocal anthropic_api_key, minimax_api_key
                    nonlocal openai_compatible_api_key, openai_compatible_base_url, openai_compatible_model
                    nonlocal openai_api_key, google_api_key
                    if cred_type == AICredentialType.ANTHROPIC and anthropic_api_key is None:
                        anthropic_api_key = cred_data.api_key
                    elif cred_type == AICredentialType.MINIMAX and minimax_api_key is None:
                        minimax_api_key = cred_data.api_key
                    elif cred_type == AICredentialType.OPENAI_COMPATIBLE and openai_compatible_api_key is None:
                        openai_compatible_api_key = cred_data.api_key
                        openai_compatible_base_url = cred_data.base_url
                        openai_compatible_model = cred_data.model
                    elif cred_type == AICredentialType.OPENAI and openai_api_key is None:
                        openai_api_key = cred_data.api_key
                    elif cred_type == AICredentialType.GOOGLE and google_api_key is None:
                        google_api_key = cred_data.api_key

                # Use assigned credentials from environment (handles shared credentials)
                if environment.conversation_ai_credential_id:
                    conv_cred_data = ai_credentials_service.get_credential_for_use(
                        db_session, environment.conversation_ai_credential_id, user.id
                    )
                    if conv_cred_data:
                        cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_conversation)
                        if cred_type:
                            _apply_cred_to_vars(cred_type, conv_cred_data)
                    else:
                        logger.warning(f"Assigned conversation credential {environment.conversation_ai_credential_id} not accessible during rebuild for environment {environment.id}")

                if environment.building_ai_credential_id:
                    build_cred_data = ai_credentials_service.get_credential_for_use(
                        db_session, environment.building_ai_credential_id, user.id
                    )
                    if build_cred_data:
                        cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_building)
                        if cred_type:
                            _apply_cred_to_vars(cred_type, build_cred_data)
                    else:
                        logger.warning(f"Assigned building credential {environment.building_ai_credential_id} not accessible during rebuild for environment {environment.id}")

                # Fall back to user profile credentials ONLY if no specific credentials are assigned
                if not has_assigned_credentials:
                    ai_credentials = ai_credentials_service.get_user_ai_credentials(user=user)
                    if ai_credentials:
                        if anthropic_api_key is None and ai_credentials.anthropic_api_key:
                            anthropic_api_key = ai_credentials.anthropic_api_key
                        if minimax_api_key is None and ai_credentials.minimax_api_key:
                            minimax_api_key = ai_credentials.minimax_api_key
                        if openai_compatible_api_key is None and ai_credentials.openai_compatible_api_key:
                            openai_compatible_api_key = ai_credentials.openai_compatible_api_key
                        if openai_compatible_base_url is None and ai_credentials.openai_compatible_base_url:
                            openai_compatible_base_url = ai_credentials.openai_compatible_base_url
                        if openai_compatible_model is None and ai_credentials.openai_compatible_model:
                            openai_compatible_model = ai_credentials.openai_compatible_model
                    # For OpenCode-specific types (openai, google), try resolving
                    # from the user's named default credentials
                    if openai_api_key is None:
                        openai_default = ai_credentials_service.get_default_for_type(
                            db_session, user.id, AICredentialType.OPENAI
                        )
                        if openai_default:
                            openai_api_key = ai_credentials_service.decrypt_credential(openai_default).api_key
                    if google_api_key is None:
                        google_default = ai_credentials_service.get_default_for_type(
                            db_session, user.id, AICredentialType.GOOGLE
                        )
                        if google_default:
                            google_api_key = ai_credentials_service.decrypt_credential(google_default).api_key

                # Regenerate MiniMax settings if needed
                if uses_minimax and minimax_api_key:
                    self._generate_minimax_settings_files(
                        instance_dir,
                        environment,
                        minimax_api_key,
                        sdk_building,
                        sdk_conversation
                    )
                    logger.info(f"Regenerated MiniMax settings files after rebuild for environment {environment.id}")

                # Regenerate OpenCode config files if needed
                uses_opencode = sdk_conversation.startswith("opencode") or sdk_building.startswith("opencode")
                if uses_opencode:
                    self._generate_opencode_config_files(
                        instance_dir,
                        environment,
                        anthropic_api_key=anthropic_api_key,
                        openai_api_key=openai_api_key,
                        openai_compatible_api_key=openai_compatible_api_key,
                        openai_compatible_base_url=openai_compatible_base_url,
                        openai_compatible_model=openai_compatible_model,
                        google_api_key=google_api_key,
                    )
                    logger.info(f"Regenerated OpenCode config files after rebuild for environment {environment.id}")

            # If container was restarted, setup new container and sync data
            if was_running:
                # Setup new container (install packages, etc.)
                logger.info(f"Setting up new container after rebuild for environment {environment.id}")
                await self._setup_new_container(db_session, environment, agent)

                # Sync dynamic data
                await self._sync_dynamic_data(db_session, environment, agent)

                environment.status = "running"
                environment.status_message = "Environment rebuilt and restarted"
                environment.last_health_check = datetime.now(UTC)
                db_session.add(environment)
                db_session.commit()

                # Emit ENVIRONMENT_ACTIVATED event to process any pending sessions
                await event_service.emit_event(
                    event_type=EventType.ENVIRONMENT_ACTIVATED,
                    model_id=environment.id,
                    user_id=agent.owner_id,
                    meta={
                        "environment_id": str(environment.id),
                        "agent_id": str(agent.id),
                        "instance_name": environment.instance_name
                    }
                )
                logger.info(f"Emitted ENVIRONMENT_ACTIVATED event for rebuilt environment {environment.id}")
            else:
                environment.status = "stopped"
                environment.status_message = "Environment rebuilt successfully"
                db_session.add(environment)
                db_session.commit()

                await event_service.emit_event(
                    event_type=EventType.ENVIRONMENT_STATUS_CHANGED,
                    model_id=environment.id,
                    user_id=agent.owner_id,
                    meta={
                        "environment_id": str(environment.id),
                        "agent_id": str(agent.id),
                        "status": "stopped",
                    }
                )

            logger.info(f"Environment {environment.id} rebuilt successfully")
            return True

        except Exception as e:
            # Update status to error
            environment.status = "error"
            environment.status_message = f"Failed to rebuild environment: {str(e)}"
            environment.config["last_error"] = str(e)
            flag_modified(environment, "config")
            db_session.add(environment)
            db_session.commit()

            await event_service.emit_event(
                event_type=EventType.ENVIRONMENT_STATUS_CHANGED,
                model_id=environment.id,
                user_id=agent.owner_id,
                meta={
                    "environment_id": str(environment.id),
                    "agent_id": str(agent.id),
                    "status": "error",
                    "error": str(e),
                }
            )

            logger.error(f"Failed to rebuild environment {environment.id}: {e}")
            raise

    async def check_health(
        self,
        db_session: Session,
        environment: AgentEnvironment
    ) -> dict:
        """
        Check environment health.

        Returns:
            Health status dict
        """
        adapter = self.get_adapter(environment)
        health = await adapter.health_check()

        # Update last health check
        environment.last_health_check = datetime.now(UTC)
        db_session.add(environment)
        db_session.commit()

        return health.model_dump()

    async def get_status(
        self,
        environment: AgentEnvironment
    ) -> str:
        """
        Get current environment status.

        Returns:
            Status string
        """
        adapter = self.get_adapter(environment)
        return await adapter.get_status()

    async def get_logs(
        self,
        environment: AgentEnvironment,
        lines: int = 100
    ) -> list[str]:
        """Get environment logs."""
        adapter = self.get_adapter(environment)
        return await adapter.get_logs(lines=lines, follow=False)

    async def delete_environment_instance(
        self,
        environment: AgentEnvironment
    ) -> bool:
        """
        Delete environment instance completely.

        Docker terminology: 'docker-compose down' - removes container, volumes, networks

        Process:
        1. Delete container and all associated resources (docker-compose down -v)
        2. Remove instance directory from filesystem
        3. Release port allocation

        Args:
            environment: Environment instance

        Returns:
            True if deletion successful
        """
        # Delete container and all associated resources (docker-compose down -v)
        try:
            adapter = self.get_adapter(environment)
            await adapter.delete()
        except Exception as e:
            # Log error but continue with directory cleanup
            logger.warning(f"Failed to delete container resources for {environment.id}: {e}")

        # Remove instance directory (run in thread pool to avoid blocking)
        instance_dir = self.instances_dir / str(environment.id)
        if instance_dir.exists():
            logger.debug(f"Removing instance directory: {instance_dir}")
            # Run blocking I/O operation in thread pool executor
            await asyncio.to_thread(shutil.rmtree, instance_dir)
            logger.debug(f"Instance directory removed: {instance_dir}")

        # Release port
        if "port" in environment.config:
            self._allocated_ports.discard(environment.config["port"])

        return True

    # === Helper Methods ===

    async def _copy_template(self, template_dir: Path, instance_dir: Path):
        """Copy template files to instance directory, then overlay shared app/core."""
        logger.debug(f"Copying template from {template_dir} to {instance_dir}")

        app_core_base_dir = self.templates_dir / APP_CORE_BASE_DIR_NAME

        def _copy_sync():
            """Synchronous copy operation to run in thread pool."""
            # 1. Copy template-specific files
            for item in template_dir.iterdir():
                if item.name.startswith('.'):
                    logger.debug(f"Skipping hidden file: {item.name}")
                    continue  # Skip hidden files

                dest = instance_dir / item.name

                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

            # 2. Overlay shared app/core from app_core_base
            if app_core_base_dir.exists():
                instance_app_core = instance_dir / "app" / "core"
                shutil.copytree(
                    app_core_base_dir / "core",
                    instance_app_core,
                    dirs_exist_ok=True,
                )
                logger.debug(f"Overlaid shared app/core from {app_core_base_dir}")
            else:
                logger.warning(f"Shared app_core_base not found at {app_core_base_dir}")

            # 3. Create claude_sessions directory for persistent SDK session storage
            # Mounted as /root/.claude inside the container to survive rebuilds
            claude_sessions_dir = instance_dir / "claude_sessions"
            claude_sessions_dir.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Created claude_sessions directory: {claude_sessions_dir}")

        # Run blocking I/O operation in thread pool executor
        await asyncio.to_thread(_copy_sync)
        logger.debug(f"Template copy completed")

    def _update_environment_config(
        self,
        db_session: Session,
        instance_dir: Path,
        environment: AgentEnvironment,
        agent: Agent,
        anthropic_api_key: str | None = None,
        minimax_api_key: str | None = None,
        openai_compatible_api_key: str | None = None,
        openai_compatible_base_url: str | None = None,
        openai_compatible_model: str | None = None,
        openai_api_key: str | None = None,
        google_api_key: str | None = None,
    ):
        """
        Update environment configuration files.

        This method regenerates:
        1. Auth token (JWT)
        2. docker-compose.yml
        3. .env file
        4. SDK-specific settings files (for MiniMax, OpenAI Compatible)

        This should be called:
        - During initial environment creation
        - During environment rebuild
        - Before environment start (to ensure fresh configs)

        Args:
            db_session: Database session
            instance_dir: Path to environment instance directory
            environment: Environment model
            agent: Agent model
            anthropic_api_key: User's Anthropic API key (optional, if not provided will fetch from user settings)
            minimax_api_key: User's MiniMax API key (optional, if not provided will fetch from user settings)
            openai_compatible_api_key: User's OpenAI Compatible API key (optional)
            openai_compatible_base_url: User's OpenAI Compatible base URL (optional)
            openai_compatible_model: User's OpenAI Compatible model (optional)
            openai_api_key: OpenAI API key (for opencode/openai)
            google_api_key: Google API key (for opencode/google)
        """
        # 1. Generate new auth token
        auth_token = self._generate_auth_token(agent.owner_id)
        environment.config["auth_token"] = auth_token
        flag_modified(environment, "config")
        logger.debug(f"Generated new auth token for environment {environment.id}")

        # 2. Get port from config (should already be set)
        port = environment.config.get("port")
        if not port:
            raise ValueError(f"Port not configured for environment {environment.id}")

        # 3. Fetch API keys - use assigned credentials if set, otherwise fall back to user profile
        #    If specific credentials are assigned to the environment, use ONLY those (no fallback)
        #    This is critical for cloned agents that use shared AI credentials
        user = db_session.get(User, agent.owner_id)

        from app.services.credentials.ai_credentials_service import ai_credentials_service

        sdk_conversation = environment.agent_sdk_conversation or "claude-code/anthropic"
        sdk_building = environment.agent_sdk_building or "claude-code/anthropic"

        # Build a credential bag from the caller-supplied values
        bag = {
            "anthropic_api_key": anthropic_api_key,
            "minimax_api_key": minimax_api_key,
            "openai_compatible_api_key": openai_compatible_api_key,
            "openai_compatible_base_url": openai_compatible_base_url,
            "openai_compatible_model": openai_compatible_model,
            "openai_api_key": openai_api_key,
            "google_api_key": google_api_key,
        }

        # Track if specific credentials are assigned (to prevent fallback)
        has_assigned_conversation_credential = environment.conversation_ai_credential_id is not None
        has_assigned_building_credential = environment.building_ai_credential_id is not None

        def _resolve_assigned_credential(credential_id, sdk_id, label):
            """Resolve an assigned credential into the bag (only fills None slots)."""
            if not credential_id or not user:
                return
            cred_data = ai_credentials_service.get_credential_for_use(
                db_session, credential_id, user.id
            )
            if cred_data:
                cred_type = SDK_TO_CREDENTIAL_TYPE.get(sdk_id)
                if cred_type:
                    # Only fill keys that are still None
                    temp_bag = make_empty_credential_bag()
                    apply_credential_to_bag(temp_bag, cred_type, cred_data)
                    for key, val in temp_bag.items():
                        if val is not None and bag[key] is None:
                            bag[key] = val
                    logger.debug(f"Resolved {label} credential for environment {environment.id}")
            else:
                logger.warning(f"Assigned {label} credential {credential_id} not accessible for environment {environment.id}")

        # Resolve conversation and building credentials if stored on environment
        _resolve_assigned_credential(
            environment.conversation_ai_credential_id, sdk_conversation, "conversation"
        )
        _resolve_assigned_credential(
            environment.building_ai_credential_id, sdk_building, "building"
        )

        # Fall back to user's profile credentials ONLY if no specific credentials are assigned
        if user and not has_assigned_conversation_credential and not has_assigned_building_credential:
            ai_credentials = ai_credentials_service.get_user_ai_credentials(user=user)
            if ai_credentials:
                if bag["anthropic_api_key"] is None and ai_credentials.anthropic_api_key:
                    bag["anthropic_api_key"] = ai_credentials.anthropic_api_key
                if bag["minimax_api_key"] is None and ai_credentials.minimax_api_key:
                    bag["minimax_api_key"] = ai_credentials.minimax_api_key
                if bag["openai_compatible_api_key"] is None and ai_credentials.openai_compatible_api_key:
                    bag["openai_compatible_api_key"] = ai_credentials.openai_compatible_api_key
                if bag["openai_compatible_base_url"] is None and ai_credentials.openai_compatible_base_url:
                    bag["openai_compatible_base_url"] = ai_credentials.openai_compatible_base_url
                if bag["openai_compatible_model"] is None and ai_credentials.openai_compatible_model:
                    bag["openai_compatible_model"] = ai_credentials.openai_compatible_model
            # For OpenCode-specific types, try named default credentials
            if bag["openai_api_key"] is None:
                openai_default = ai_credentials_service.get_default_for_type(
                    db_session, user.id, AICredentialType.OPENAI
                )
                if openai_default:
                    bag["openai_api_key"] = ai_credentials_service.decrypt_credential(openai_default).api_key
            if bag["google_api_key"] is None:
                google_default = ai_credentials_service.get_default_for_type(
                    db_session, user.id, AICredentialType.GOOGLE
                )
                if google_default:
                    bag["google_api_key"] = ai_credentials_service.decrypt_credential(google_default).api_key

        # Unpack bag back to local vars for downstream methods that use positional args
        anthropic_api_key = bag["anthropic_api_key"]
        minimax_api_key = bag["minimax_api_key"]
        openai_compatible_api_key = bag["openai_compatible_api_key"]
        openai_compatible_base_url = bag["openai_compatible_base_url"]
        openai_compatible_model = bag["openai_compatible_model"]
        openai_api_key = bag["openai_api_key"]
        google_api_key = bag["google_api_key"]

        # 4. Generate docker-compose.yml
        self._generate_compose_file(instance_dir, environment, agent, port, auth_token)

        # 5. Generate .env file and SDK settings files
        self._generate_env_file(
            instance_dir, environment, agent, port, auth_token,
            anthropic_api_key, minimax_api_key,
            openai_compatible_api_key, openai_compatible_base_url, openai_compatible_model,
            openai_api_key=openai_api_key, google_api_key=google_api_key,
        )

        # 6. Write Claude Code PreToolUse hook settings for credential access detection
        self._write_claude_code_hook_settings(instance_dir, environment)

        # 7. Generate OpenCode config files if any mode uses the opencode SDK
        uses_opencode = sdk_conversation.startswith("opencode") or sdk_building.startswith("opencode")
        if uses_opencode:
            self._generate_opencode_config_files(
                instance_dir,
                environment,
                anthropic_api_key=anthropic_api_key,
                openai_api_key=openai_api_key,
                openai_compatible_api_key=openai_compatible_api_key,
                openai_compatible_base_url=openai_compatible_base_url,
                openai_compatible_model=openai_compatible_model,
                google_api_key=google_api_key,
            )

        logger.info(f"Updated configuration files for environment {environment.id}")

    def _generate_compose_file(
        self,
        instance_dir: Path,
        environment: AgentEnvironment,
        agent: Agent,
        port: int,
        auth_token: str
    ):
        """Generate docker-compose.yml from template."""
        template_path = instance_dir / "docker-compose.template.yml"
        output_path = instance_dir / "docker-compose.yml"

        logger.debug(f"Generating docker-compose.yml from {template_path}")
        if not template_path.exists():
            raise FileNotFoundError(f"Template not found: {template_path}")

        # Read template
        with open(template_path, 'r') as f:
            content = f.read()

        # Determine the host path for volumes
        # If HOST_AGENT_ENVIRONMENTS_DIR is set, use it (for Docker-in-Docker)
        # Otherwise, use the instance_dir as-is (for local dev)
        if settings.HOST_AGENT_ENVIRONMENTS_DIR:
            host_instance_dir = f"{settings.HOST_AGENT_ENVIRONMENTS_DIR}/{environment.id}"
            logger.debug(f"Using host path for volumes: {host_instance_dir}")
        else:
            host_instance_dir = str(instance_dir.absolute())
            logger.debug(f"Using container path for volumes: {host_instance_dir}")

        # Replace variables
        content = content.replace("${ENV_ID}", str(environment.id))
        content = content.replace("${AGENT_ID}", str(agent.id))
        content = content.replace("${ENV_NAME}", environment.env_name)
        content = content.replace("${ENV_VERSION}", environment.env_version)
        content = content.replace("${AGENT_PORT}", str(port))
        content = content.replace("${AGENT_AUTH_TOKEN}", auth_token)
        content = content.replace("${HOST_INSTANCE_DIR}", host_instance_dir)

        # Write output
        with open(output_path, 'w') as f:
            f.write(content)

    def _generate_env_file(
        self,
        instance_dir: Path,
        environment: AgentEnvironment,
        agent: Agent,
        port: int,
        auth_token: str,
        anthropic_api_key: str | None = None,
        minimax_api_key: str | None = None,
        openai_compatible_api_key: str | None = None,
        openai_compatible_base_url: str | None = None,
        openai_compatible_model: str | None = None,
        openai_api_key: str | None = None,
        google_api_key: str | None = None,
    ):
        """Generate .env files for docker-compose and application, and SDK settings files."""
        logger.debug(f"Generating .env files for environment {environment.id}")

        # Determine SDK providers for each mode (default to anthropic for backward compatibility)
        sdk_conversation = environment.agent_sdk_conversation or "claude-code/anthropic"
        sdk_building = environment.agent_sdk_building or "claude-code/anthropic"

        # Check if each SDK is used in any mode
        uses_anthropic = sdk_conversation == "claude-code/anthropic" or sdk_building == "claude-code/anthropic"
        uses_minimax = sdk_conversation == "claude-code/minimax" or sdk_building == "claude-code/minimax"

        # 1. Generate root .env file for docker-compose
        agent_personal_database_url = ''  # To be implemented
        agent_container_log_level = 'INFO'

        # Generate Anthropic credential environment variables based on credential type
        if uses_anthropic and anthropic_api_key:
            env_var_name, key_type = detect_anthropic_credential_type(anthropic_api_key)
            logger.info(f"Detected Anthropic credential: {key_type} -> {env_var_name}")

            if env_var_name == "ANTHROPIC_API_KEY":
                anthropic_api_key_line = f"ANTHROPIC_API_KEY={anthropic_api_key}"
                claude_code_oauth_token_line = "# CLAUDE_CODE_OAUTH_TOKEN not set"
            else:  # CLAUDE_CODE_OAUTH_TOKEN
                anthropic_api_key_line = "# ANTHROPIC_API_KEY not set"
                claude_code_oauth_token_line = f"CLAUDE_CODE_OAUTH_TOKEN={anthropic_api_key}"
        elif uses_anthropic:
            anthropic_api_key_line = "ANTHROPIC_API_KEY="
            claude_code_oauth_token_line = "# CLAUDE_CODE_OAUTH_TOKEN not set"
        else:
            anthropic_api_key_line = "# ANTHROPIC_API_KEY not used (other SDK configured)"
            claude_code_oauth_token_line = "# CLAUDE_CODE_OAUTH_TOKEN not used (other SDK configured)"

        env_content = f"""# Environment Identification
ENV_ID={environment.id}
AGENT_ID={agent.id}
ENV_NAME={environment.env_name}
ENV_VERSION={environment.env_version}

# Network Configuration
AGENT_PORT={port}

# Backend API Configuration
BACKEND_URL=http://backend:8000

# Security
AGENT_AUTH_TOKEN={auth_token}

# Database (private database for the agent)
DATABASE_URL={agent_personal_database_url}

# Resource Limits
CPU_LIMIT={environment.config.get('cpu_limit', settings.AGENT_ENV_CPU_LIMIT)}
MEMORY_LIMIT={environment.config.get('memory_limit', settings.AGENT_ENV_MEMORY_LIMIT)}
CPU_RESERVATION={environment.config.get('cpu_reservation', settings.AGENT_ENV_CPU_RESERVATION)}
MEMORY_RESERVATION={environment.config.get('memory_reservation', settings.AGENT_ENV_MEMORY_RESERVATION)}

# Logging
LOG_LEVEL={agent_container_log_level}

# Claude Code Configuration
CLAUDE_CODE_WORKSPACE=/app/app
CLAUDE_CODE_PERMISSION_MODE=acceptEdits

# AI Service Credentials (passed to container)
{anthropic_api_key_line}
{claude_code_oauth_token_line}
OPENAI_API_KEY={openai_api_key or ""}
GOOGLE_API_KEY={google_api_key or ""}

# SDK Adapter Configuration
# These variables tell the agent-env which adapter to use for each mode
# Format: <adapter-type>/<provider> (e.g., claude-code/anthropic, claude-code/minimax, opencode/anthropic)
SDK_ADAPTER_BUILDING={sdk_building}
SDK_ADAPTER_CONVERSATION={sdk_conversation}
"""

        env_path = instance_dir / ".env"
        with open(env_path, 'w') as f:
            f.write(env_content)

        # 2. Generate app/.env file for application-specific variables (if needed)
        app_env_content = """# Application-specific environment variables can be added here
# Note: API keys are provided via container environment variables or SDK settings files
"""

        app_dir = instance_dir / "app"
        app_dir.mkdir(parents=True, exist_ok=True)
        app_env_path = app_dir / ".env"
        with open(app_env_path, 'w') as f:
            f.write(app_env_content)

        # 3. Generate MiniMax SDK settings files if MiniMax is used
        if uses_minimax and minimax_api_key:
            self._generate_minimax_settings_files(
                instance_dir,
                environment,
                minimax_api_key,
                sdk_building,
                sdk_conversation
            )

    def _generate_minimax_settings_files(
        self,
        instance_dir: Path,
        environment: AgentEnvironment,
        minimax_api_key: str,
        sdk_building: str,
        sdk_conversation: str
    ):
        """
        Generate MiniMax SDK settings files in the core .claude folder.

        These files are used by Claude Code SDK to configure the MiniMax API endpoint.
        Files are placed in /app/core/.claude/ which is part of the core directory.

        Note: Since core files are replaced during rebuild, this method must be called
        AFTER the core files are copied from template.

        Args:
            instance_dir: Environment instance directory
            environment: Environment model
            minimax_api_key: User's MiniMax API key
            sdk_building: SDK for building mode
            sdk_conversation: SDK for conversation mode
        """
        import json

        # Settings content for MiniMax
        minimax_settings = {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
                "ANTHROPIC_AUTH_TOKEN": minimax_api_key,
                "API_TIMEOUT_MS": "3000000",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": 1,
                "ANTHROPIC_MODEL": "MiniMax-M2.1",
                "ANTHROPIC_SMALL_FAST_MODEL": "MiniMax-M2.1-lightning",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "MiniMax-M2.1",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "MiniMax-M2.1",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "MiniMax-M2.1"
            }
        }

        # Create .claude directory in core folder
        # Inside container this will be at /app/core/.claude/
        claude_settings_dir = instance_dir / "app" / "core" / ".claude"
        claude_settings_dir.mkdir(parents=True, exist_ok=True)

        # Generate building settings file if building mode uses MiniMax
        if sdk_building == "claude-code/minimax":
            building_settings_path = claude_settings_dir / "building_settings.json"
            with open(building_settings_path, 'w') as f:
                json.dump(minimax_settings, f, indent=2)
            logger.info(f"Generated MiniMax building settings for environment {environment.id}")

        # Generate conversation settings file if conversation mode uses MiniMax
        if sdk_conversation == "claude-code/minimax":
            conversation_settings_path = claude_settings_dir / "conversation_settings.json"
            with open(conversation_settings_path, 'w') as f:
                json.dump(minimax_settings, f, indent=2)
            logger.info(f"Generated MiniMax conversation settings for environment {environment.id}")

    def _generate_opencode_config_files(
        self,
        instance_dir: Path,
        environment: AgentEnvironment,
        anthropic_api_key: str | None = None,
        openai_api_key: str | None = None,
        openai_compatible_api_key: str | None = None,
        openai_compatible_base_url: str | None = None,
        openai_compatible_model: str | None = None,
        google_api_key: str | None = None,
    ):
        """
        Generate OpenCode config files in the core .opencode folder.

        Creates per-mode config files that are read by the OpenCodeAdapter at
        runtime. Each file combines opencode.json settings with auth credentials
        so the adapter can load everything from a single file.

        Files are placed in /app/core/.opencode/ inside the container
        (host path: instance_dir/app/core/.opencode/).

        Note: Since core files are replaced during rebuild, this method must be
        called AFTER the core files are copied from template.

        Args:
            instance_dir: Environment instance directory
            environment: Environment model
            anthropic_api_key: Anthropic API key
            openai_api_key: OpenAI API key
            openai_compatible_api_key: OpenAI-compatible API key
            openai_compatible_base_url: OpenAI-compatible base URL
            openai_compatible_model: OpenAI-compatible model name
            google_api_key: Google API key
        """
        import json as _json

        sdk_conversation = environment.agent_sdk_conversation or "claude-code/anthropic"
        sdk_building = environment.agent_sdk_building or "claude-code/anthropic"

        # Create .opencode directory inside the instance core folder
        opencode_dir = instance_dir / "app" / "core" / ".opencode"
        opencode_dir.mkdir(parents=True, exist_ok=True)

        # Default models per provider per mode
        _default_models = {
            "anthropic": {
                "building": "anthropic/claude-sonnet-4-5",
                "conversation": "anthropic/claude-haiku-4-5-20251001",
            },
            "openai": {
                "building": "openai/gpt-5.4-mini",
                "conversation": "openai/gpt-5.4-nano",
            },
            "openai_compatible": {
                "building": openai_compatible_model or "gpt-4",
                "conversation": openai_compatible_model or "gpt-4",
            },
            "google": {
                "building": "google/gemini-2.5-pro",
                "conversation": "google/gemini-2.5-flash",
            },
        }

        def _get_provider_from_sdk(sdk_value: str) -> str:
            """Extract the provider from an SDK value like 'opencode/anthropic'."""
            if "/" in sdk_value:
                return sdk_value.split("/", 1)[1]
            return "anthropic"  # default

        def _build_provider_config(provider: str, model: str, api_key: str | None) -> dict:
            """
            Build the provider section that registers the selected model
            and embeds the API key for authentication.

            OpenCode only recognizes models declared in its built-in list
            or explicitly registered in the provider config.  This ensures
            any model (including newer ones like gpt-5.4-nano) is accepted.

            The API key is written directly into the config so it's available
            immediately when opencode serve starts — no env-var indirection.
            Config files live inside the read-only core volume (0o600 perms).
            """
            # Extract the model ID (part after the slash)
            if "/" in model:
                model_id = model.split("/", 1)[1]
            else:
                model_id = model

            # Map our provider names to OpenCode provider IDs.
            # OpenAI-compatible uses a custom provider definition with the
            # @ai-sdk/openai-compatible npm package, registered under a
            # custom key ("custom") rather than the built-in provider name.
            provider_id = {
                "anthropic": "anthropic",
                "openai": "openai",
                "openai_compatible": "custom",
                "google": "google",
            }.get(provider, provider)

            provider_entry: dict = {
                "models": {
                    model_id: {
                        "name": model_id,
                    },
                },
            }

            # OpenAI-compatible endpoints are registered as custom providers
            # with the @ai-sdk/openai-compatible npm adapter and require
            # baseURL so OpenCode knows where to send requests.
            if provider == "openai_compatible":
                provider_entry["npm"] = "@ai-sdk/openai-compatible"
                provider_entry["name"] = "OpenAI Compatible"

            options: dict = {}
            if api_key:
                options["apiKey"] = api_key
            if provider == "openai_compatible" and openai_compatible_base_url:
                options["baseURL"] = openai_compatible_base_url
            if options:
                provider_entry["options"] = options

            return {provider_id: provider_entry}

        def _build_config(mode: str, sdk_value: str) -> dict:
            """Build a complete opencode config dict for a given mode."""
            provider = _get_provider_from_sdk(sdk_value)

            # Determine model: explicit override → provider+mode default
            model_override = (
                environment.model_override_building if mode == "building"
                else environment.model_override_conversation
            )
            provider_defaults = _default_models.get(provider, _default_models.get("anthropic", {}))
            model = model_override or provider_defaults.get(mode, "anthropic/claude-sonnet-4-5")

            # MCP bridge servers expose platform custom tools to OpenCode agents.
            # Server names must match Claude Code's MCP server names so both
            # adapters produce identical tool names (mcp__{server}__{tool}).
            mcp_bridge_servers = {
                "knowledge": {
                    "type": "local",
                    "command": [
                        "python3",
                        "/app/core/server/tools/mcp_bridge/knowledge_server.py",
                    ],
                    "enabled": True,
                },
                "agent_task": {
                    "type": "local",
                    "command": [
                        "python3",
                        "/app/core/server/tools/mcp_bridge/task_server.py",
                    ],
                    "enabled": True,
                },
            }

            # Register the selected model with OpenCode so it's recognized.
            # OpenCode only knows about its built-in models; any model not in
            # its default list must be declared in the provider config section.
            api_key = {
                "anthropic": anthropic_api_key,
                "openai": openai_api_key,
                "openai_compatible": openai_compatible_api_key,
                "google": google_api_key,
            }.get(provider)
            provider_config = _build_provider_config(provider, model, api_key)

            # For custom providers, the top-level model must be prefixed with
            # the provider key so OpenCode routes it to the right adapter.
            # E.g. "granite4:latest" → "custom/granite4:latest"
            if provider == "openai_compatible":
                if "/" in model:
                    # Already has a prefix — replace with the custom provider key
                    config_model = f"custom/{model.split('/', 1)[1]}"
                else:
                    config_model = f"custom/{model}"
            else:
                config_model = model

            config = {
                "$schema": "https://opencode.ai/config.json",
                "model": config_model,
                "provider": provider_config,
                "permission": {
                    "*": "allow",
                    "external_directory": {
                        "/app/workspace/**": "allow",
                        "/app/**": "allow",
                        "/tmp/**": "allow",
                    },
                },
                "tools": {
                    "webfetch": True,
                    "websearch": True,
                    "bash": True,
                    "read": True,
                    "write": True,
                    "edit": True,
                    "glob": True,
                    "grep": True,
                    "list": True,
                    "patch": True,
                },
                "mcp": mcp_bridge_servers,
                "server": {
                    "port": 4096 if mode == "building" else 4097,
                    "hostname": "127.0.0.1",
                },
            }

            return config

        # Generate per-mode config directories.
        # Each mode gets its own subdirectory with an opencode.json that
        # `opencode serve` reads from its cwd.  The adapter runs a separate
        # server instance per mode so there is no config sharing or race.
        for mode_name, sdk_id in [("building", sdk_building), ("conversation", sdk_conversation)]:
            if not sdk_id.startswith("opencode"):
                continue

            mode_dir = opencode_dir / mode_name
            mode_dir.mkdir(parents=True, exist_ok=True)

            config = _build_config(mode_name, sdk_id)

            # Write opencode.json — the main config for this mode's server.
            # Auth is handled via env var references in the provider config
            # (e.g. {env:OPENAI_API_KEY}), not via auth.json.
            opencode_json_path = mode_dir / "opencode.json"
            with open(opencode_json_path, 'w') as f:
                _json.dump(config, f, indent=2)
            # Restrict permissions — config contains the API key
            opencode_json_path.chmod(0o600)

            logger.info(
                f"Generated OpenCode {mode_name} config for environment {environment.id} "
                f"(model={config.get('model')}, port={config.get('server', {}).get('port')})"
            )

    def _write_claude_code_hook_settings(self, instance_dir: Path, environment: AgentEnvironment):
        """
        Write the Claude Code PreToolUse hook configuration for credential access detection.

        Creates or updates /root/.claude/settings.json inside the container
        (host path: instance_dir/claude_sessions/settings.json) to include the
        credential_guard_hook.py PreToolUse hook. This path is where the Claude
        CLI "user" setting source resolves settings from.

        The hook script is already present in the core directory (copied from
        app_core_base/core/hooks/credential_guard_hook.py). This method only
        writes the settings.json that activates it.

        If settings.json already exists (e.g., from a previous build), the
        hooks section is merged in without overwriting existing settings.

        Note: Claude Code hooks run on `Bash|Read|Write|Edit` tool calls. The hook
        script exits code 0 (allow) or 2 (block) based on the backend response.

        Args:
            instance_dir: Environment instance directory
            environment: Environment model
        """
        import json

        # Write to claude_sessions/ which is mounted as /root/.claude in the container.
        # The Claude CLI "user" setting source looks at ~/.claude/settings.json.
        claude_settings_dir = instance_dir / "claude_sessions"
        claude_settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = claude_settings_dir / "settings.json"

        # Load existing settings if present (e.g., MiniMax env config)
        existing_settings: dict = {}
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f:
                    existing_settings = json.load(f)
            except (json.JSONDecodeError, IOError):
                existing_settings = {}

        # Build hook entry
        credential_hook_entry = {
            "matcher": "Bash|Read|Write|Edit",
            "hooks": [
                {
                    "type": "command",
                    "command": "python3 /app/core/hooks/credential_guard_hook.py"
                }
            ]
        }

        # Deep merge: preserve existing hooks, append credential guard if not present
        existing_hooks = existing_settings.setdefault("hooks", {})
        existing_pre_tool = existing_hooks.setdefault("PreToolUse", [])

        # Avoid duplicating the hook if it's already registered
        already_registered = any(
            entry.get("matcher") == credential_hook_entry["matcher"]
            and any(
                h.get("command", "").endswith("credential_guard_hook.py")
                for h in entry.get("hooks", [])
            )
            for entry in existing_pre_tool
        )
        if not already_registered:
            existing_pre_tool.append(credential_hook_entry)

        merged = existing_settings

        with open(settings_path, 'w') as f:
            json.dump(merged, f, indent=2)

        logger.info(f"Wrote Claude Code hook settings for environment {environment.id}")

    def _allocate_port(self) -> int:
        """Allocate available port."""
        for port in range(self.port_range_start, self.port_range_end):
            if port not in self._allocated_ports:
                self._allocated_ports.add(port)
                return port

        raise Exception("No available ports")

    def _generate_auth_token(self, user_id: UUID) -> str:
        """
        Generate JWT authentication token for agent container.

        The token contains the user_id of the agent owner, allowing the agent
        to authenticate as that user when making API calls back to the backend.

        Args:
            user_id: UUID of the agent owner

        Returns:
            JWT token string
        """
        # Create a JWT token that expires in 10 years (agents are long-lived)
        # The token contains the user_id so the agent can authenticate as the owner
        access_token_expires = timedelta(days=365 * 10)
        return security.create_access_token(
            subject=str(user_id),
            expires_delta=access_token_expires
        )

    async def copy_workspace_between_environments(
        self,
        source_env: AgentEnvironment,
        target_env: AgentEnvironment
    ) -> bool:
        """
        Copy workspace from source to target environment.

        Used when switching environments for the same agent to maintain workspace state.
        This ensures the same agent state across environments.

        Copies (all workspace data):
        - app/workspace/scripts/ (agent scripts)
        - app/workspace/docs/ (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md)
        - app/workspace/knowledge/ (integration docs)
        - app/workspace/files/ (reports, caches, CSVs)
        - app/workspace/uploads/ (user-uploaded files)
        - app/workspace/credentials/ (integration credentials)
        - app/workspace/plugins/ (LLM plugins)
        - app/workspace/workspace_requirements.txt (Python packages)
        - app/workspace/workspace_system_packages.txt (OS packages)

        Does NOT copy (Environment Runtime - environment-specific):
        - app/workspace/logs/ (session logs)
        - app/workspace/databases/ (runtime SQLite DBs, session state)

        Args:
            source_env: Source environment to copy from
            target_env: Target environment to copy to

        Returns:
            True if copy successful
        """
        source_dir = self.instances_dir / str(source_env.id)
        target_dir = self.instances_dir / str(target_env.id)

        if not source_dir.exists():
            logger.warning(f"Source environment directory not found: {source_dir}")
            return False

        if not target_dir.exists():
            logger.warning(f"Target environment directory not found: {target_dir}")
            return False

        # Directories to copy (workspace data - synced between environments)
        dirs_to_copy = [
            "app/workspace/scripts",
            "app/workspace/docs",
            "app/workspace/knowledge",
            "app/workspace/files",
            "app/workspace/uploads",
            "app/workspace/credentials",
            "app/workspace/plugins",
            "app/workspace/webapp",
        ]

        # Single files to copy
        files_to_copy = [
            "app/workspace/workspace_requirements.txt",
            "app/workspace/workspace_system_packages.txt",
        ]

        def _copy_sync():
            """Synchronous copy operation to run in thread pool."""
            for dir_rel in dirs_to_copy:
                src = source_dir / dir_rel
                dst = target_dir / dir_rel

                if src.exists():
                    try:
                        if dst.exists():
                            shutil.rmtree(dst)
                        shutil.copytree(src, dst)
                        logger.info(f"Copied {dir_rel} to target environment")
                    except Exception as e:
                        logger.error(f"Failed to copy {dir_rel}: {e}")

            for file_rel in files_to_copy:
                src = source_dir / file_rel
                dst = target_dir / file_rel

                if src.exists():
                    try:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        logger.info(f"Copied {file_rel} to target environment")
                    except Exception as e:
                        logger.error(f"Failed to copy {file_rel}: {e}")

        # Run blocking I/O operation in thread pool executor
        await asyncio.to_thread(_copy_sync)
        logger.info(f"Workspace copied from environment {source_env.id} to {target_env.id}")
        return True
