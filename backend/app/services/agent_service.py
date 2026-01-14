from uuid import UUID
import asyncio
import logging
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import Session, select
from app.models import Agent, AgentCreate, AgentUpdate, User, SessionCreate, AgentHandoverConfig, AgentEnvironment, Session as ChatSession, AgentSdkConfig
from app.models.environment import AgentEnvironmentCreate
from app.services.environment_service import EnvironmentService
from app.services.environment_lifecycle import EnvironmentLifecycleManager
from app.services.session_service import SessionService
from app.services.ai_functions_service import AIFunctionsService
from app.core.config import settings
from app.core.db import engine

logger = logging.getLogger(__name__)


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
        session: Session, user: User, description: str, mode: str, auto_create_session: bool = False, user_workspace_id: UUID | None = None
    ):
        """
        Create full agent flow: agent + environment + (optionally) session
        This is an async generator that yields progress updates

        Args:
            auto_create_session: If True, automatically create session after environment is ready.
                               If False, stop after environment is ready (for credential sharing).
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

            # Generate agent name, entrypoint_prompt, and workflow_prompt from description using LLM
            if AIFunctionsService.is_available():
                try:
                    config = AIFunctionsService.generate_agent_configuration(description)
                    agent_name = config.get("name", f"Agent: {description[:30]}...")
                    entrypoint_prompt = config.get("entrypoint_prompt", description)
                    workflow_prompt = config.get("workflow_prompt", f"You are an AI agent designed to: {description}")
                except Exception as e:
                    # Fallback to simple logic if LLM fails
                    agent_number = len(session.exec(select(Agent).where(Agent.owner_id == user.id)).all()) + 1
                    agent_name = f"Agent #{agent_number}"
                    entrypoint_prompt = description
                    workflow_prompt = f"You are an AI agent designed to: {description}"
            else:
                # Use simple logic when AI functions not available
                agent_number = len(session.exec(select(Agent).where(Agent.owner_id == user.id)).all()) + 1
                agent_name = f"Agent #{agent_number}"
                entrypoint_prompt = description
                workflow_prompt = f"You are an AI agent designed to: {description}"

            agent_data = AgentCreate(
                name=agent_name,
                description=description,
                workflow_prompt=workflow_prompt,
                entrypoint_prompt=entrypoint_prompt,
                user_workspace_id=user_workspace_id,
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
                    # Set agent's active environment
                    agent.active_environment_id = environment.id
                    session.add(agent)
                    session.commit()

                    yield {
                        "step": "environment_ready",
                        "message": "Environment is ready",
                        "agent_id": str(agent.id),
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

            # If auto_create_session is False, stop here (for credential sharing)
            if not auto_create_session:
                yield {
                    "step": "completed",
                    "message": "Agent and environment created successfully. Ready for credential sharing.",
                    "agent_id": str(agent.id),
                    "environment_id": str(environment.id),
                    "current_step": "redirect"
                }
                return

            # Step 3: Create session (only if auto_create_session is True)
            yield {
                "step": "session_creating",
                "message": "Creating conversation session...",
                "current_step": "create_session"
            }

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

    @staticmethod
    async def sync_agent_handover_config(session: Session, agent_id: UUID) -> None:
        """
        Sync handover configuration to agent-env.

        Called after creating, updating, or deleting handover configs.
        Queries all enabled handovers for the agent, formats them, and pushes
        the configuration to the agent's active environment.

        Args:
            session: Database session
            agent_id: UUID of the agent
        """
        # Get agent with active environment
        agent = AgentService.get_agent_with_environment(session=session, agent_id=agent_id)
        if not agent or not agent.active_environment_id:
            logger.warning(f"Agent {agent_id} has no active environment, skipping handover sync")
            return

        environment_id = agent.active_environment_id

        # Get all enabled handovers for this agent
        handover_configs = session.exec(
            select(AgentHandoverConfig)
            .where(AgentHandoverConfig.source_agent_id == agent_id)
            .where(AgentHandoverConfig.enabled == True)
        ).all()

        # Format handovers for agent-env
        handovers_list = []
        for config in handover_configs:
            handovers_list.append({
                "id": str(config.target_agent_id),
                "name": config.target_agent.name,
                "prompt": config.handover_prompt
            })

        # Generate overall handover prompt
        if handovers_list:
            handover_prompt = (
                "## AGENT HANDOVER INSTRUCTIONS FOR THIS CONVERSATION\n\n"
                "CRITICAL: You have the `agent_handover` tool available RIGHT NOW in this conversation. "
                "When you complete a task that matches the trigger conditions below IN THIS CONVERSATION, "
                "you MUST immediately call the tool IN THE SAME RESPONSE - do not just describe what you would do, "
                "do not wait for the next message, do not ask for permission. "
                "Execute the handover IMMEDIATELY when conditions are met.\n\n"
                "**CONFIGURED HANDOVERS (these apply to the current conversation):**\n"
            )

            for h in handovers_list:
                handover_prompt += f"\n**→ {h['name']}** (ID: {h['id']})\n{h['prompt']}\n"

            handover_prompt += (
                "\n**HOW TO EXECUTE THE HANDOVER RIGHT NOW:**\n"
                "As soon as conditions are met in this conversation, call `agent_handover` with:\n"
                "- `target_agent_id`: UUID of the target agent (shown above)\n"
                "- `target_agent_name`: Name of the target agent (shown above)\n"
                "- `handover_message`: The context message as specified in the instructions above\n\n"
                "CRITICAL: This happens in the CURRENT conversation. When you complete the triggering task, "
                "call the tool in that same response. Do not wait, do not ask, do not describe - just execute.\n"
            )
        else:
            handover_prompt = ""

        # Get environment adapter and sync config
        try:
            environment = session.get(AgentEnvironment, environment_id)
            if environment:
                lifecycle_manager = EnvironmentLifecycleManager()
                adapter = lifecycle_manager.get_adapter(environment)
                await adapter.set_agent_handover_config(
                    handovers=handovers_list,
                    handover_prompt=handover_prompt
                )
                logger.info(f"Synced {len(handovers_list)} handover(s) to environment {environment_id}")
        except Exception as e:
            logger.error(f"Failed to sync handover config to environment: {e}")

    @staticmethod
    async def execute_handover(
        session: Session,
        user_id: UUID,
        target_agent_id: UUID,
        target_agent_name: str,
        handover_message: str,
        source_session_id: UUID
    ) -> tuple[bool, UUID | None, str | None]:
        """
        Execute agent handover by creating new session and posting handover message.

        This method:
        1. Validates target agent exists and user has access
        2. Creates new conversation session for target agent
        3. Posts handover message to new session
        4. Logs system message in source session with link to new session

        Args:
            session: Database session
            user_id: User executing the handover
            target_agent_id: Target agent UUID
            target_agent_name: Target agent name
            handover_message: Message to send to target agent
            source_session_id: Source session UUID (for logging handover)

        Returns:
            Tuple of (success: bool, session_id: UUID | None, error: str | None)
        """
        from app.services.message_service import MessageService

        try:
            # Get target agent
            target_agent = session.get(Agent, target_agent_id)
            if not target_agent:
                return False, None, "Target agent not found"

            # Check permissions
            if target_agent.owner_id != user_id:
                return False, None, "Not enough permissions to access target agent"

            # Verify target agent has active environment
            if not target_agent.active_environment_id:
                return False, None, "Target agent has no active environment"

            # Create session for target agent (conversation mode by default)
            session_create = SessionCreate(
                agent_id=target_agent_id,
                title=f"Handover from {target_agent_name}",
                mode="conversation",
            )

            new_session = SessionService.create_session(
                db_session=session,
                user_id=user_id,
                data=session_create
            )

            if not new_session:
                return False, None, "Failed to create session for target agent"

            # Send handover message using centralized service method
            # This handles message creation and stream initiation
            result = await SessionService.send_session_message(
                session_id=new_session.id,
                user_id=user_id,
                content=handover_message,
                file_ids=None,
                answers_to_message_id=None,
                get_fresh_db_session=lambda: Session(engine)
            )

            # Log the result for debugging
            if result["action"] == "error":
                logger.error(f"Failed to send handover message: {result['message']}")
                return False, None, f"Failed to send handover message: {result['message']}"

            logger.info(f"Handover message sent to session {new_session.id}, action: {result['action']}")

            # Log system message in source session about the handover
            source_session = session.get(ChatSession, source_session_id)
            if source_session:
                MessageService.create_message(
                    session=session,
                    session_id=source_session_id,
                    role="system",
                    content=f"🔀 Agent handed over to '{target_agent.name}'",
                    message_metadata={
                        "handover_type": "agent_handover",
                        "forwarded_to_session_id": str(new_session.id),
                        "target_agent_id": str(target_agent_id),
                        "target_agent_name": target_agent.name
                    }
                )

            logger.info(
                f"Handover executed: Created session {new_session.id} for agent {target_agent_id}, "
                f"source session: {source_session_id}"
            )

            return True, new_session.id, None

        except Exception as e:
            logger.error(f"Error executing handover: {str(e)}")
            return False, None, str(e)

    # SDK Config Methods

    @staticmethod
    def get_sdk_config(session: Session, agent_id: UUID) -> AgentSdkConfig:
        """
        Get SDK configuration for an agent.

        Returns AgentSdkConfig with sdk_tools and allowed_tools lists.
        If agent_sdk_config is empty or None, returns empty lists.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            return AgentSdkConfig(sdk_tools=[], allowed_tools=[])

        config = agent.agent_sdk_config or {}
        return AgentSdkConfig(
            sdk_tools=config.get("sdk_tools", []),
            allowed_tools=config.get("allowed_tools", [])
        )

    @staticmethod
    def add_allowed_tools(session: Session, agent_id: UUID, tools: list[str]) -> AgentSdkConfig:
        """
        Add tools to the allowed_tools list.

        Merges new tools with existing allowed_tools (no duplicates).
        Returns updated AgentSdkConfig.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Get current config or initialize
        if not agent.agent_sdk_config:
            agent.agent_sdk_config = {"sdk_tools": [], "allowed_tools": []}

        # Get current allowed tools
        current_allowed = set(agent.agent_sdk_config.get("allowed_tools", []))

        # Add new tools
        current_allowed.update(tools)

        # Update config
        agent.agent_sdk_config["allowed_tools"] = list(current_allowed)

        # Mark as modified for SQLAlchemy to detect the change
        flag_modified(agent, "agent_sdk_config")

        session.add(agent)
        session.commit()
        session.refresh(agent)

        return AgentSdkConfig(
            sdk_tools=agent.agent_sdk_config.get("sdk_tools", []),
            allowed_tools=agent.agent_sdk_config.get("allowed_tools", [])
        )

    @staticmethod
    def get_pending_tools(session: Session, agent_id: UUID) -> list[str]:
        """
        Get tools that need approval.

        Returns tools that are in sdk_tools but not in allowed_tools.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            return []

        config = agent.agent_sdk_config or {}
        sdk_tools = set(config.get("sdk_tools", []))
        allowed_tools = set(config.get("allowed_tools", []))

        # Pending = sdk_tools - allowed_tools
        pending = sdk_tools - allowed_tools
        return list(pending)

    @staticmethod
    def update_sdk_tools(session: Session, agent_id: UUID, tools: list[str]) -> AgentSdkConfig:
        """
        Update the sdk_tools list (incrementally - adds new tools, keeps existing).

        Called when init message is received from agent-env to update discovered tools.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise ValueError(f"Agent {agent_id} not found")

        # Get current config or initialize
        if not agent.agent_sdk_config:
            agent.agent_sdk_config = {"sdk_tools": [], "allowed_tools": []}

        # Get current sdk_tools
        current_sdk_tools = set(agent.agent_sdk_config.get("sdk_tools", []))

        # Add new tools (incremental)
        current_sdk_tools.update(tools)

        # Update config
        agent.agent_sdk_config["sdk_tools"] = list(current_sdk_tools)

        # Mark as modified for SQLAlchemy to detect the change
        flag_modified(agent, "agent_sdk_config")

        session.add(agent)
        session.commit()
        session.refresh(agent)

        return AgentSdkConfig(
            sdk_tools=agent.agent_sdk_config.get("sdk_tools", []),
            allowed_tools=agent.agent_sdk_config.get("allowed_tools", [])
        )

    @staticmethod
    async def sync_allowed_tools_to_environment(session: Session, agent_id: UUID) -> bool:
        """
        Sync allowed_tools to agent's active environment.

        This syncs only the settings.json (not plugin files) to update allowed_tools.
        Called after approving tools via /allowed-tools endpoint.

        Args:
            session: Database session
            agent_id: Agent UUID

        Returns:
            True if sync was successful, False otherwise
        """
        from app.services.llm_plugin_service import LLMPluginService

        agent = session.get(Agent, agent_id)
        if not agent:
            logger.warning(f"Agent {agent_id} not found for allowed_tools sync")
            return False

        if not agent.active_environment_id:
            logger.warning(f"Agent {agent_id} has no active environment, skipping allowed_tools sync")
            return False

        environment = session.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            logger.warning(f"Active environment {agent.active_environment_id} not found")
            return False

        if environment.status != "running":
            logger.warning(f"Environment {environment.id} is not running (status: {environment.status})")
            return False

        try:
            # Get allowed_tools from agent SDK config
            allowed_tools = []
            if agent.agent_sdk_config:
                allowed_tools = agent.agent_sdk_config.get("allowed_tools", [])

            # Prepare plugin data with allowed_tools
            plugins_data = LLMPluginService.prepare_plugins_for_environment(
                session=session,
                agent_id=agent_id,
                allowed_tools=allowed_tools
            )

            # Get lifecycle manager and adapter
            lifecycle_manager = EnvironmentLifecycleManager()
            adapter = lifecycle_manager.get_adapter(environment)

            # Sync only settings (no plugin files needed for tool approval)
            # We just send the settings_json update
            await adapter.set_plugins({
                "active_plugins": plugins_data.get("active_plugins", []),
                "settings_json": plugins_data.get("settings_json", {}),
                "plugin_files": {},  # No need to re-sync plugin files
            })

            logger.info(f"Synced allowed_tools to environment {environment.id} for agent {agent_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to sync allowed_tools to environment: {e}")
            return False
