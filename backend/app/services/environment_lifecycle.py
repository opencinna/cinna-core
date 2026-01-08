import os
import shutil
import logging
import asyncio
from pathlib import Path
from uuid import UUID
from sqlmodel import Session
from sqlalchemy.orm.attributes import flag_modified
from typing import Optional
from datetime import datetime, timedelta

from app.models.environment import AgentEnvironment
from app.models.agent import Agent
from app.models import User
from app.core.config import settings
from app.core import security
import app.crud as crud
from .adapters.base import EnvironmentAdapter, EnvInitConfig
from .adapters.docker_adapter import DockerEnvironmentAdapter

logger = logging.getLogger(__name__)


class EnvironmentLifecycleManager:
    """
    Manages environment lifecycle:
    - Creation (copy template → instance directory)
    - Initialization (build image, create .env)
    - Start/Stop/Restart
    - Status monitoring
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

    async def create_environment_instance(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent,
        anthropic_api_key: str | None = None
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
            self._update_environment_config(db_session, instance_dir, environment, agent, anthropic_api_key)

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

    async def _sync_agent_data(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ):
        """
        Sync agent data (prompts and credentials) to running environment.

        This is called after:
        - Environment starts
        - Environment rebuilds (if it was running)
        - Environment restarts

        Args:
            db_session: Database session
            environment: Environment instance
            agent: Agent instance
        """
        adapter = self.get_adapter(environment)

        # Set prompts in docs files
        environment.status_message = "Configuring agent prompts..."
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

        from app.services.credentials_service import CredentialsService
        credentials_data = CredentialsService.prepare_credentials_for_environment(
            session=db_session,
            agent_id=agent.id
        )
        await adapter.set_credentials(credentials_data)

    async def start_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment,
        agent: Agent
    ) -> bool:
        """
        Start environment:
        1. Update configuration files (regenerate auth token, docker-compose.yml, .env)
        2. Update status to 'starting'
        3. Start container via adapter
        4. Wait for health check
        5. Sync agent data (prompts and credentials)
        6. Update status to 'running'

        Args:
            db_session: Database session
            environment: Environment instance
            agent: Agent instance
        """
        # Update status
        environment.status = "starting"
        environment.status_message = "Updating configuration files..."
        db_session.add(environment)
        db_session.commit()

        try:
            # Get instance directory
            instance_dir = self.instances_dir / str(environment.id)

            # Update configuration files (generates new auth token, docker-compose.yml, .env)
            # This ensures the environment always has a fresh JWT token before starting
            self._update_environment_config(db_session, instance_dir, environment, agent)
            db_session.add(environment)  # Save updated config with new auth token
            db_session.commit()

            # Get adapter
            adapter = self.get_adapter(environment)

            # Start container
            environment.status_message = "Starting container..."
            db_session.add(environment)
            db_session.commit()

            await adapter.start()

            # Sync agent data (prompts and credentials)
            await self._sync_agent_data(db_session, environment, agent)

            # Update status
            environment.status = "running"
            environment.status_message = "Environment is running"
            environment.last_health_check = datetime.utcnow()
            db_session.add(environment)
            db_session.commit()

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
        """Stop environment container."""
        try:
            adapter = self.get_adapter(environment)
            await adapter.stop()

            environment.status = "stopped"
            db_session.add(environment)
            db_session.commit()

            # Release port
            if "port" in environment.config:
                self._allocated_ports.discard(environment.config["port"])

            return True
        except Exception as e:
            environment.status = "error"
            environment.config["last_error"] = str(e)
            flag_modified(environment, "config")
            db_session.add(environment)
            db_session.commit()
            raise

    async def suspend_environment(
        self,
        db_session: Session,
        environment: AgentEnvironment
    ) -> bool:
        """
        Suspend environment container to save resources.

        This stops the container but keeps the status as 'suspended' instead of 'stopped',
        indicating it can be quickly reactivated when needed.

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

        This starts the container and sets status to 'running'.
        Similar to start_environment but optimized for suspended environments.

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
                from app.services.event_service import event_service
                from app.models.event import EventType
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

            # Start container
            environment.status_message = "Starting container..."
            db_session.add(environment)
            db_session.commit()

            await adapter.start()

            # Sync agent data (prompts and credentials)
            await self._sync_agent_data(db_session, environment, agent)

            # Update status
            environment.status = "running"
            environment.status_message = "Environment activated"
            environment.last_health_check = datetime.utcnow()
            environment.last_activity_at = datetime.utcnow()
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
                from app.services.event_service import event_service
                from app.models.event import EventType
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

        This operation:
        1. Checks if container is running
        2. Stops container if running
        3. Updates core files from template
        4. Rebuilds Docker image
        5. Starts container if it was running before
        6. Syncs agent data (prompts and credentials) if container is running

        Args:
            db_session: Database session
            environment: Environment instance
            agent: Agent instance

        Returns:
            True if rebuild successful
        """
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

            # Stop if running
            if was_running:
                await self.stop_environment(db_session, environment)

            # Get adapter
            adapter = self.get_adapter(environment)

            # Get template core directory
            template_dir = self.templates_dir / environment.env_name
            template_core_dir = template_dir / "app" / "core"

            if not template_core_dir.exists():
                raise FileNotFoundError(f"Template core directory not found: {template_core_dir}")

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

            # Rebuild via adapter
            await adapter.rebuild(
                template_core_dir=template_core_dir,
                was_running=was_running
            )

            # Sync agent data if environment is now running
            if was_running:
                await self._sync_agent_data(db_session, environment, agent)

                environment.status = "running"
                environment.status_message = "Environment rebuilt and restarted"
                environment.last_health_check = datetime.utcnow()
            else:
                environment.status = "stopped"
                environment.status_message = "Environment rebuilt successfully"

            db_session.add(environment)
            db_session.commit()

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
        environment.last_health_check = datetime.utcnow()
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
        Delete environment instance:
        1. Delete container, volumes, and networks via adapter
        2. Remove instance directory
        3. Release port
        """
        # Delete container and all associated resources
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
        """Copy template files to instance directory."""
        logger.debug(f"Copying template from {template_dir} to {instance_dir}")

        def _copy_sync():
            """Synchronous copy operation to run in thread pool."""
            for item in template_dir.iterdir():
                if item.name.startswith('.'):
                    logger.debug(f"Skipping hidden file: {item.name}")
                    continue  # Skip hidden files

                dest = instance_dir / item.name

                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

        # Run blocking I/O operation in thread pool executor
        await asyncio.to_thread(_copy_sync)
        logger.debug(f"Template copy completed")

    def _update_environment_config(
        self,
        db_session: Session,
        instance_dir: Path,
        environment: AgentEnvironment,
        agent: Agent,
        anthropic_api_key: str | None = None
    ):
        """
        Update environment configuration files.

        This method regenerates:
        1. Auth token (JWT)
        2. docker-compose.yml
        3. .env file

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

        # 3. Fetch ANTHROPIC_API_KEY from user AI credentials if not explicitly provided
        if anthropic_api_key is None:
            user = db_session.get(User, agent.owner_id)
            if user:
                ai_credentials = crud.get_user_ai_credentials(user=user)
                anthropic_api_key = ai_credentials.anthropic_api_key if ai_credentials else None
                if anthropic_api_key:
                    logger.debug(f"Fetched ANTHROPIC_API_KEY from user settings for environment {environment.id}")

        # 4. Generate docker-compose.yml
        self._generate_compose_file(instance_dir, environment, agent, port, auth_token)

        # 5. Generate .env file
        self._generate_env_file(instance_dir, environment, agent, port, auth_token, anthropic_api_key)

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
        anthropic_api_key: str | None = None
    ):
        """Generate .env files for docker-compose and application."""
        logger.debug(f"Generating .env files for environment {environment.id}")

        # 1. Generate root .env file for docker-compose (without ANTHROPIC_API_KEY)
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

# Database (optional - for agent to access main DB)
DATABASE_URL={settings.SQLALCHEMY_DATABASE_URI}

# Resource Limits
CPU_LIMIT={environment.config.get('cpu_limit', '1.0')}
MEMORY_LIMIT={environment.config.get('memory_limit', '512M')}
CPU_RESERVATION={environment.config.get('cpu_reservation', '0.25')}
MEMORY_RESERVATION={environment.config.get('memory_reservation', '128M')}

# Logging
LOG_LEVEL=INFO

# Claude Code Configuration
CLAUDE_CODE_WORKSPACE=/app/app
CLAUDE_CODE_PERMISSION_MODE=acceptEdits

# AI Service Credentials (passed to container)
ANTHROPIC_API_KEY={anthropic_api_key or ''}
"""

        env_path = instance_dir / ".env"
        with open(env_path, 'w') as f:
            f.write(env_content)

        # 2. Generate app/.env file for application-specific variables (if needed)
        # Note: ANTHROPIC_API_KEY is now provided via container environment variables
        app_env_content = """# Application-specific environment variables can be added here
# Note: ANTHROPIC_API_KEY is provided via container environment variables
"""

        app_dir = instance_dir / "app"
        app_dir.mkdir(parents=True, exist_ok=True)
        app_env_path = app_dir / ".env"
        with open(app_env_path, 'w') as f:
            f.write(app_env_content)

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
