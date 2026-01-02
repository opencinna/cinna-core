"""
AI Functions Service - provides simple LLM processing utilities using Google ADK.

This service encapsulates fast, cheap LLM calls for tasks like:
- Generating agent configurations from descriptions
- Creating conversation titles from messages
- Other text generation tasks
"""
import logging
from typing import Optional

from app.core.config import settings
from app.agents import generate_agent_config, generate_conversation_title, generate_handover_prompt
from app.agents.schedule_generator import generate_agent_schedule

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
