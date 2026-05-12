"""
Router trigger prompt generator — turns an agent's short description into
a natural-language trigger prompt used by the App MCP router for AI
classification.

Output target: ~120-150 chars, single sentence, capability-verb-focused
(e.g., "Plans meetings, finds free slots in my calendar, and books
events on my behalf").

Lets each provider pick its own default model — matches how
``app_agent_router.route_to_agent`` and the other AI function generators
call ``generate_content`` so personal-provider routing (Anthropic /
OpenAI api_key kwargs) works correctly.
"""
import logging

from .provider_manager import get_provider_manager

logger = logging.getLogger(__name__)

FALLBACK_TRIGGER_PROMPT = "Handles tasks related to: "


def generate_router_trigger_prompt(
    agent_name: str,
    description: str,
    provider_kwargs: dict | None = None,
) -> str:
    """Generate a router trigger prompt from an agent name + description.

    Args:
        agent_name: The agent's display name. Provides hints about the
            domain (e.g., "Calendar Planner").
        description: The agent's short, user-facing description. Should
            describe what the agent does in natural language.
        provider_kwargs: Optional kwargs passed to ``generate_content``
            (e.g., ``api_key`` for personal Anthropic / OpenAI routing).

    Returns:
        A short single-sentence string (~120-150 chars) describing the
        kinds of requests this agent should handle. On any failure,
        returns a fallback prefix + the description (truncated) so the
        caller always gets a non-empty hint.
    """
    if not description or not description.strip():
        return ""

    try:
        manager = get_provider_manager()

        prompt = f"""You are writing a short routing description for an AI agent
in a multi-agent router. The router will read this description plus the
user's incoming message and pick the right agent.

Agent name: {agent_name}
Agent description (what it does):
---
{description.strip()}
---

Write ONE sentence that tells the router when to pick this agent. Requirements:
- Single sentence, ~120-150 characters
- Start with a capability verb (Plans, Books, Drafts, Summarises, Tracks, ...)
- Focus on the kinds of requests this agent should handle, not how it works
- No quotes, no markdown, no trailing punctuation lists
- Third person ("Handles ...", "Plans ...")

Return ONLY the sentence, nothing else."""

        response = manager.generate_content(
            prompt,
            **(provider_kwargs or {}),
        )
        text = response.text.strip()

        # Strip any surrounding quotes / markdown the model may sneak in.
        text = text.strip().strip('"').strip("'").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(line for line in lines if not line.startswith("```")).strip()

        # Collapse newlines — the router consumes a single line.
        text = " ".join(text.split())

        if not text:
            raise ValueError("Empty trigger prompt returned by provider")

        return text
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.warning(
            "router_trigger_prompt generation failed for agent_name=%r: %s",
            agent_name, exc,
        )
        # Fall back to the description itself, truncated, so the router
        # still has something usable until the user regenerates.
        snippet = description.strip().splitlines()[0][:140]
        return f"{FALLBACK_TRIGGER_PROMPT}{snippet}".strip()
