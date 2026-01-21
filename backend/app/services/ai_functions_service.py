"""
AI Functions Service - provides simple LLM processing utilities.

This service encapsulates fast, cheap LLM calls for tasks like:
- Generating agent configurations from descriptions
- Creating conversation titles from messages
- Other text generation tasks

Supports multiple providers with cascade fallback via AI_FUNCTIONS_PROVIDERS env variable.
"""
import logging
from uuid import UUID

from sqlmodel import Session

from app.agents import (
    generate_agent_config,
    generate_agent_description,
    generate_conversation_title,
    generate_handover_prompt as generate_handover_prompt_from_agents,
    generate_sql_query,
    refine_prompt,
    refine_task as refine_task_from_agents,
)
from app.agents.schedule_generator import generate_agent_schedule
from app.agents.provider_manager import get_provider_manager
from app.models.agent import Agent

logger = logging.getLogger(__name__)


class AIFunctionsService:
    """Service for simple AI-powered text generation tasks."""

    @staticmethod
    def generate_agent_configuration(description: str) -> dict:
        """
        Generate agent configuration from user description.

        This generates:
        1. Agent name (concise, descriptive)
        2. Entrypoint prompt (human-like trigger message)
        3. Workflow prompt (system prompt for conversation mode)

        Args:
            description: User's description of what the agent should do

        Returns:
            dict with keys:
                - name: Agent name (str)
                - entrypoint_prompt: Natural trigger message (str)
                - workflow_prompt: Detailed system prompt (str)

        Raises:
            Exception: If agent generation fails and no fallback is available
        """
        try:
            config = generate_agent_config(description)
            logger.info(
                f"Generated agent config: {config.get('name', 'Unknown')} "
                f"(entrypoint: {len(config.get('entrypoint_prompt', ''))} chars, "
                f"workflow: {len(config.get('workflow_prompt', ''))} chars)"
            )
            return config
        except Exception as e:
            logger.error(f"Failed to generate agent config: {e}", exc_info=True)
            # Return fallback configuration
            return {
                "name": f"Agent: {description[:30]}...",
                "entrypoint_prompt": description,
                "workflow_prompt": f"You are an assistant that helps with: {description}",
            }

    @staticmethod
    def generate_description_from_workflow(
        workflow_prompt: str,
        agent_name: str | None = None
    ) -> str:
        """
        Generate a short description from a workflow prompt.

        Args:
            workflow_prompt: The agent's workflow/system prompt
            agent_name: Optional agent name for context

        Returns:
            str: A concise 1-2 sentence description of what the agent does
        """
        try:
            description = generate_agent_description(workflow_prompt, agent_name)
            logger.info(f"Generated agent description: {description[:50]}...")
            return description
        except Exception as e:
            logger.error(f"Failed to generate agent description: {e}", exc_info=True)
            # Return a generic fallback
            return "AI agent configured with custom workflow."

    @staticmethod
    def generate_session_title(message_content: str) -> str:
        """
        Generate a concise title for a conversation session.

        Args:
            message_content: First message from the user

        Returns:
            str: Concise title (max 100 chars)
        """
        try:
            title = generate_conversation_title(message_content)
            logger.info(f"Generated session title: {title}")
            return title
        except Exception as e:
            logger.error(f"Failed to generate session title: {e}", exc_info=True)
            # Return fallback title (truncated message)
            title = message_content[:100]
            if len(message_content) > 100:
                title += "..."
            return title

    @staticmethod
    def generate_schedule(natural_language: str, timezone: str) -> dict:
        """
        Generate CRON schedule from natural language.

        Args:
            natural_language: User's input (e.g., "every workday at 7 AM")
            timezone: IANA timezone (e.g., "Europe/Berlin")

        Returns:
            dict with keys:
                - success: bool
                - description: Human-readable schedule (if success)
                - cron_string: CRON expression in local time (if success)
                - error: Error message (if not success)

        Note:
            The CRON string is in local time. The caller must convert to UTC
            using AgentSchedulerService.convert_local_cron_to_utc().
        """
        try:
            result = generate_agent_schedule(natural_language, timezone)
            logger.info(
                f"Generated schedule: {result.get('success')} - "
                f"{result.get('description') or result.get('error')}"
            )
            return result
        except Exception as e:
            logger.error(f"Failed to generate schedule: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to generate schedule: {str(e)}"
            }

    @staticmethod
    def generate_handover_prompt(
        source_agent_name: str,
        source_entrypoint: str | None,
        source_workflow: str | None,
        target_agent_name: str,
        target_entrypoint: str | None,
        target_workflow: str | None
    ) -> dict:
        """
        Generate handover prompt between two agents using AI.

        Args:
            source_agent_name: Name of source agent
            source_entrypoint: Source agent's entrypoint prompt
            source_workflow: Source agent's workflow prompt
            target_agent_name: Name of target agent
            target_entrypoint: Target agent's entrypoint prompt
            target_workflow: Target agent's workflow prompt

        Returns:
            dict with keys:
                - success: bool
                - handover_prompt: Generated prompt (if success)
                - error: Error message (if not success)
        """
        try:
            result = generate_handover_prompt_from_agents(
                source_agent_name=source_agent_name,
                source_entrypoint=source_entrypoint,
                source_workflow=source_workflow,
                target_agent_name=target_agent_name,
                target_entrypoint=target_entrypoint,
                target_workflow=target_workflow,
            )
            logger.info(
                f"Generated handover prompt: {result.get('success')} - "
                f"{result.get('handover_prompt', '')[:50] if result.get('success') else result.get('error')}"
            )
            return result
        except Exception as e:
            logger.error(f"Failed to generate handover prompt: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to generate handover prompt: {str(e)}"
            }

    @staticmethod
    def is_available() -> bool:
        """
        Check if AI functions are available (at least one provider is configured).

        Returns:
            bool: True if AI functions can be used, False otherwise
        """
        return get_provider_manager().is_available()

    @staticmethod
    def get_available_providers() -> list[str]:
        """
        Get list of available AI function providers.

        Returns:
            list[str]: Names of available providers in priority order
        """
        return get_provider_manager().get_available_providers()

    @staticmethod
    def generate_sql(
        user_request: str,
        database_schema: dict,
        current_query: str | None = None
    ) -> dict:
        """
        Generate SQL query from natural language description.

        Args:
            user_request: User's natural language request
            database_schema: Database schema with tables, views, and columns
            current_query: Current SQL query in the editor (optional)

        Returns:
            dict with keys:
                - success: bool
                - sql: Generated SQL query (if success)
                - error: Error message or clarifying questions (if not success)
        """
        try:
            result = generate_sql_query(
                user_request=user_request,
                database_schema=database_schema,
                current_query=current_query,
            )
            logger.info(
                f"Generated SQL: {result.get('success')} - "
                f"{result.get('sql', '')[:50] if result.get('success') else result.get('error')}"
            )
            return result
        except Exception as e:
            logger.error(f"Failed to generate SQL: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to generate SQL query: {str(e)}"
            }

    @staticmethod
    def refine_user_prompt(
        db: Session,
        user_input: str,
        has_files_attached: bool,
        agent_id: UUID | None,
        owner_id: UUID,
        mode: str,
        is_new_agent: bool,
    ) -> dict:
        """
        Refine a user's prompt to make it more effective.

        Args:
            db: Database session
            user_input: The user's current input text
            has_files_attached: Whether files are attached to the message
            agent_id: ID of the agent (if any) - will be fetched from DB
            owner_id: ID of the user (to verify agent ownership)
            mode: Session mode - "building" or "conversation"
            is_new_agent: Whether this is a new agent being created

        Returns:
            dict with keys:
                - success: bool
                - refined_prompt: The improved prompt text (if success)
                - error: Error message (if not success)
        """
        try:
            # Fetch agent details if agent_id is provided
            agent_name = None
            entrypoint_prompt = None
            workflow_prompt = None

            if agent_id:
                agent = db.get(Agent, agent_id)
                if agent and agent.owner_id == owner_id:
                    agent_name = agent.name
                    entrypoint_prompt = agent.entrypoint_prompt
                    workflow_prompt = agent.workflow_prompt

            result = refine_prompt(
                user_input=user_input,
                has_files_attached=has_files_attached,
                agent_name=agent_name,
                entrypoint_prompt=entrypoint_prompt,
                workflow_prompt=workflow_prompt,
                mode=mode,
                is_new_agent=is_new_agent,
            )
            logger.info(
                f"Refined prompt: {result.get('success')} - "
                f"{result.get('refined_prompt', '')[:50] if result.get('success') else result.get('error')}"
            )
            return result
        except Exception as e:
            logger.error(f"Failed to refine prompt: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to refine prompt: {str(e)}"
            }

    @staticmethod
    def refine_task(
        db: Session,
        current_description: str,
        user_comment: str,
        agent_id: UUID | None,
        owner_id: UUID,
        refinement_history: list[dict] | None = None,
        user_selected_text: str | None = None,
    ) -> dict:
        """
        Refine a task description based on user feedback.

        Args:
            db: Database session
            current_description: The current task description
            user_comment: User's refinement request or feedback
            agent_id: ID of the selected agent (if any)
            owner_id: ID of the user (to verify agent ownership)
            refinement_history: Previous refinement conversation history
            user_selected_text: Optional text selected by user from the task body

        Returns:
            dict with keys:
                - success: bool
                - refined_description: The improved description (if success)
                - feedback_message: Brief message about changes or questions
                - error: Error message (if not success)
        """
        try:
            # Fetch agent workflow prompt and refiner prompt if agent_id is provided
            agent_workflow_prompt = None
            agent_refiner_prompt = None

            if agent_id:
                agent = db.get(Agent, agent_id)
                if agent and agent.owner_id == owner_id:
                    agent_workflow_prompt = agent.workflow_prompt
                    agent_refiner_prompt = agent.refiner_prompt

            result = refine_task_from_agents(
                current_description=current_description,
                agent_workflow_prompt=agent_workflow_prompt,
                agent_refiner_prompt=agent_refiner_prompt,
                user_comment=user_comment,
                refinement_history=refinement_history,
                user_selected_text=user_selected_text,
            )
            logger.info(
                f"Refined task: {result.get('success')} - "
                f"{result.get('refined_description', '')[:50] if result.get('success') else result.get('error')}"
            )
            return result
        except Exception as e:
            logger.error(f"Failed to refine task: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to refine task: {str(e)}"
            }
