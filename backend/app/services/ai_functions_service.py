"""
AI Functions Service - provides simple LLM processing utilities using Google ADK.

This service encapsulates fast, cheap LLM calls for tasks like:
- Generating agent configurations from descriptions
- Creating conversation titles from messages
- Other text generation tasks
"""
import logging
from typing import Optional
from uuid import UUID

from sqlmodel import Session

from app.core.config import settings
from app.agents import generate_agent_config, generate_conversation_title, generate_handover_prompt, generate_sql_query, refine_prompt
from app.agents.schedule_generator import generate_agent_schedule
from app.models.agent import Agent

logger = logging.getLogger(__name__)


class AIFunctionsService:
    """Service for simple AI-powered text generation tasks."""

    @staticmethod
    def _get_api_key() -> str:
        """
        Get Google API key from settings.

        Raises:
            ValueError: If GOOGLE_API_KEY is not configured
        """
        if not settings.GOOGLE_API_KEY:
            raise ValueError(
                "GOOGLE_API_KEY is not configured. "
                "Please set it in your .env file to use AI functions."
            )
        return settings.GOOGLE_API_KEY

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
            ValueError: If GOOGLE_API_KEY is not configured
            Exception: If agent generation fails
        """
        try:
            api_key = AIFunctionsService._get_api_key()
            config = generate_agent_config(description, api_key)
            logger.info(
                f"Generated agent config: {config.get('name', 'Unknown')} "
                f"(entrypoint: {len(config.get('entrypoint_prompt', ''))} chars, "
                f"workflow: {len(config.get('workflow_prompt', ''))} chars)"
            )
            return config
        except ValueError:
            # Re-raise configuration errors
            raise
        except Exception as e:
            logger.error(f"Failed to generate agent config: {e}", exc_info=True)
            # Return fallback configuration
            return {
                "name": f"Agent: {description[:30]}...",
                "entrypoint_prompt": description,
                "workflow_prompt": f"You are an assistant that helps with: {description}",
            }

    @staticmethod
    def generate_session_title(message_content: str) -> str:
        """
        Generate a concise title for a conversation session.

        Args:
            message_content: First message from the user

        Returns:
            str: Concise title (max 100 chars)

        Raises:
            ValueError: If GOOGLE_API_KEY is not configured
        """
        try:
            api_key = AIFunctionsService._get_api_key()
            title = generate_conversation_title(message_content, api_key)
            logger.info(f"Generated session title: {title}")
            return title
        except ValueError:
            # Re-raise configuration errors
            raise
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

        Raises:
            ValueError: If GOOGLE_API_KEY is not configured
        """
        try:
            api_key = AIFunctionsService._get_api_key()
            result = generate_agent_schedule(natural_language, timezone, api_key)
            logger.info(
                f"Generated schedule: {result.get('success')} - "
                f"{result.get('description') or result.get('error')}"
            )
            return result
        except ValueError:
            # Re-raise configuration errors
            raise
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

        Raises:
            ValueError: If GOOGLE_API_KEY is not configured
        """
        try:
            api_key = AIFunctionsService._get_api_key()
            result = generate_handover_prompt(
                source_agent_name=source_agent_name,
                source_entrypoint=source_entrypoint,
                source_workflow=source_workflow,
                target_agent_name=target_agent_name,
                target_entrypoint=target_entrypoint,
                target_workflow=target_workflow,
                api_key=api_key
            )
            logger.info(
                f"Generated handover prompt: {result.get('success')} - "
                f"{result.get('handover_prompt', '')[:50] if result.get('success') else result.get('error')}"
            )
            return result
        except ValueError:
            # Re-raise configuration errors
            raise
        except Exception as e:
            logger.error(f"Failed to generate handover prompt: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to generate handover prompt: {str(e)}"
            }

    @staticmethod
    def is_available() -> bool:
        """
        Check if AI functions are available (GOOGLE_API_KEY is configured).

        Returns:
            bool: True if AI functions can be used, False otherwise
        """
        return bool(settings.GOOGLE_API_KEY)

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

        Raises:
            ValueError: If GOOGLE_API_KEY is not configured
        """
        try:
            api_key = AIFunctionsService._get_api_key()
            result = generate_sql_query(
                user_request=user_request,
                database_schema=database_schema,
                current_query=current_query,
                api_key=api_key
            )
            logger.info(
                f"Generated SQL: {result.get('success')} - "
                f"{result.get('sql', '')[:50] if result.get('success') else result.get('error')}"
            )
            return result
        except ValueError:
            # Re-raise configuration errors
            raise
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

        Raises:
            ValueError: If GOOGLE_API_KEY is not configured
        """
        try:
            api_key = AIFunctionsService._get_api_key()

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
                api_key=api_key,
            )
            logger.info(
                f"Refined prompt: {result.get('success')} - "
                f"{result.get('refined_prompt', '')[:50] if result.get('success') else result.get('error')}"
            )
            return result
        except ValueError:
            # Re-raise configuration errors
            raise
        except Exception as e:
            logger.error(f"Failed to refine prompt: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Failed to refine prompt: {str(e)}"
            }
