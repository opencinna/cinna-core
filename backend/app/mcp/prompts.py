"""
MCP prompt registration for agent example prompts.

Exposes agent-defined example prompts as MCP prompts so clients (Claude Desktop,
Cursor, etc.) can discover them via the standard MCP prompts/list and prompts/get
protocol operations.

Each prompt line on the agent follows the format:
    slug: prompt text

The slug becomes the MCP prompt `name`, and the text becomes the `description`
and default message content. Lines without a colon are used as both name and text.

Like resources.py, prompts are fetched from the DB on every call so updates take
effect without server restart or cache eviction.
"""
import logging
import uuid

from mcp import types as mcp_types

from app.core.db import create_session
from app.services.mcp.mcp_connector_service import MCPConnectorService
from app.mcp.server import mcp_connector_id_var

logger = logging.getLogger(__name__)


# ── Parsing ──────────────────────────────────────────────────────────────────


def _parse_prompt_line(line: str) -> tuple[str, str] | None:
    """Parse a single prompt line into (slug, prompt_text).

    Format: "slug: prompt text"
    Lines without ':' use the full trimmed line as both slug and text.
    Empty lines or whitespace-only lines return None.

    Returns:
        (slug, prompt_text) or None if the line is empty.
    """
    line = line.strip()
    if not line:
        return None

    if ":" in line:
        slug, _, text = line.partition(":")
        slug = slug.strip()
        text = text.strip()
        if slug and text:
            return slug, text

    # No colon or missing slug/text — use full line as both
    return line, line


# ── Agent Resolution ─────────────────────────────────────────────────────────


def _get_agent_example_prompts() -> list[str]:
    """Resolve connector context var -> connector -> agent -> example_prompts.

    Returns:
        The agent's example_prompts list, or [] if unavailable.
    """
    connector_id_str = mcp_connector_id_var.get(None)
    if not connector_id_str:
        return []

    connector_id = uuid.UUID(connector_id_str)

    with create_session() as db:
        connector, agent, environment = MCPConnectorService.resolve_connector_context(
            db, connector_id,
        )

    return agent.example_prompts or []


# ── Registration ─────────────────────────────────────────────────────────────


def register_mcp_prompts(server) -> None:
    """Register prompt handlers on a FastMCP server instance.

    Patches the low-level server handlers for prompts/list and prompts/get
    to dynamically return prompts from the agent's example_prompts field.
    """

    async def _list_prompts() -> list[mcp_types.Prompt]:
        """Return agent example prompts as MCP Prompt objects."""
        lines = _get_agent_example_prompts()
        prompts = []
        for line in lines:
            parsed = _parse_prompt_line(line)
            if parsed is None:
                continue
            slug, text = parsed
            prompts.append(mcp_types.Prompt(
                name=slug,
                description=text,
            ))
        return prompts

    async def _get_prompt(name: str, arguments: dict[str, str] | None = None) -> mcp_types.GetPromptResult:
        """Return a single prompt by name (slug)."""
        lines = _get_agent_example_prompts()
        for line in lines:
            parsed = _parse_prompt_line(line)
            if parsed is None:
                continue
            slug, text = parsed
            if slug == name:
                return mcp_types.GetPromptResult(
                    description=text,
                    messages=[
                        mcp_types.PromptMessage(
                            role="user",
                            content=mcp_types.TextContent(
                                type="text",
                                text=text,
                            ),
                        )
                    ],
                )

        raise ValueError(f"Prompt not found: {name}")

    # Register handlers on the low-level MCP server using its decorators
    server._mcp_server.list_prompts()(_list_prompts)
    server._mcp_server.get_prompt()(_get_prompt)
