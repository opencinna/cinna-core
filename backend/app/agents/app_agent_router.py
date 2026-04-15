"""
App Agent Router — classifies a user message and selects the best matching agent.

Uses the provider manager for cascade provider selection.
"""
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .provider_manager import get_provider_manager

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"
APP_AGENT_ROUTER_PROMPT = PROMPTS_DIR / "app_agent_router_prompt.md"


@dataclass
class RouteToAgentResult:
    """Result of routing a user message to an agent."""

    agent_id: str
    transformed_message: str | None = None  # None means "use original message"


def _load_prompt_template(file_path: Path) -> str:
    """Load prompt template from file."""
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to load prompt template from {file_path}: {e}")


def route_to_agent(
    message: str,
    available_agents: list[dict],
    provider_kwargs: dict | None = None,
) -> RouteToAgentResult | None:
    """Classify a user message and pick the best matching agent.

    Args:
        message: The user's message to classify.
        available_agents: List of dicts with keys: id, name, trigger_prompt.
        provider_kwargs: Optional kwargs passed to the provider manager.

    Returns:
        RouteToAgentResult with agent_id and optional transformed_message,
        or None if no agent fits or on parse failure.
    """
    if not available_agents:
        return None

    try:
        template = _load_prompt_template(APP_AGENT_ROUTER_PROMPT)

        # Build agent list section
        agents_section = "\n".join(
            f"- **ID**: {agent['id']}\n  **Name**: {agent['name']}\n  **Description**: {agent['trigger_prompt']}"
            for agent in available_agents
        )

        prompt = f"""{template}

---

## Available Agents

{agents_section}

---

## User Message

{message}

---

Return JSON only:
"""

        manager = get_provider_manager()
        response = manager.generate_content(prompt, **(provider_kwargs or {}))

        raw = response.text.strip()
        logger.debug("App agent router response: %r", raw)

        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.splitlines()
            raw = "\n".join(
                line for line in lines
                if not line.startswith("```")
            ).strip()

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("App agent router returned non-JSON response: %r", raw)
            return None

        agent_id = data.get("agent_id", "")
        if not agent_id or agent_id == "NONE":
            return None

        # Validate it looks like a UUID (basic check)
        if not (len(agent_id) == 36 and agent_id.count("-") == 4):
            logger.warning("App agent router returned unexpected agent_id: %r", agent_id)
            return None

        # Extract and validate transformed_message
        raw_transformed = data.get("message")
        transformed_message: str | None = None
        if raw_transformed and isinstance(raw_transformed, str):
            stripped = raw_transformed.strip()
            if (
                stripped
                and stripped != message
                and len(stripped) <= 2 * len(message)
            ):
                transformed_message = stripped

        return RouteToAgentResult(
            agent_id=agent_id,
            transformed_message=transformed_message,
        )

    except Exception as e:
        logger.error("App agent routing failed: %s", e, exc_info=True)
        return None
