"""
Description generator - creates short agent descriptions from workflow prompts.

This module generates a concise 1-2 sentence description of what an agent does
based on its workflow prompt. Used to auto-update agent descriptions when
workflow prompts change.

Uses the provider manager for cascade provider selection.
"""
import logging

from .provider_manager import get_provider_manager

logger = logging.getLogger(__name__)

FALLBACK_DESCRIPTION = "AI agent configured with custom workflow."


def generate_agent_description(workflow_prompt: str, agent_name: str | None = None) -> str:
    """
    Generate a short description from a workflow prompt.

    Args:
        workflow_prompt: The agent's workflow/system prompt
        agent_name: Optional agent name for context

    Returns:
        str: A concise 1-2 sentence description of what the agent does
    """
    try:
        manager = get_provider_manager()

        name_context = f"Agent name: {agent_name}\n\n" if agent_name else ""

        prompt = f"""Generate a concise description of what this AI agent does based on its workflow prompt.

{name_context}Workflow prompt:
---
{workflow_prompt}
---

Requirements:
- Write exactly 1-2 sentences
- Focus on the agent's primary purpose and capabilities
- Be specific about what tasks the agent can help with
- Write in third person (e.g., "This agent helps with..." or "Assists users in...")
- No quotes or formatting markers

Return ONLY the description, nothing else."""

        response = manager.generate_content(prompt)

        # Clean up response
        description = response.text.strip()

        # Remove any quotes if present
        description = description.strip('"').strip("'")

        return description

    except Exception as e:
        logger.warning(f"Failed to generate agent description: {e}")
        return FALLBACK_DESCRIPTION
