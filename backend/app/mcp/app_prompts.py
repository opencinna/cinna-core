"""
App MCP Server prompts — exposes user's active agent routes as MCP prompts.

This enables external AI clients (Claude Desktop, Cursor) to discover
available agents via MCP prompts/list without guessing.
"""
import logging
import re
import uuid

from app.mcp.context_vars import mcp_authenticated_user_id_var

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a route name to a slug suitable for use as a prompt name."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug or "agent"


def register_app_mcp_prompts(server) -> None:
    """Register dynamic per-user prompts on the App MCP FastMCP instance."""

    @server.prompt()
    async def list_available_agents() -> list:
        """List available agents as MCP prompts for the authenticated user.

        Returns prompts representing each addressable target's trigger
        description. The set mirrors what App MCP Stage 1 would put on the
        ballot — the user's effective routes **and** the identity owners they
        can address — because a discovery list that omits half the routable
        targets teaches the client the wrong vocabulary. The two are composed
        here for the same reason ``AppMCPRoutingService.route_message``
        composes them: identity stopped being an arm inside the route service
        in phase 1 of the channels/identity unification.
        """
        from app.core.db import create_session
        from app.services.app_mcp.app_agent_route_service import AppAgentRouteService
        from app.services.routing.identity_candidate_provider import (
            IdentityCandidateProvider,
        )

        auth_user_id_str = mcp_authenticated_user_id_var.get(None)
        if not auth_user_id_str:
            return []

        try:
            user_id = uuid.UUID(auth_user_id_str)
        except ValueError:
            return []

        try:
            with create_session() as db:
                effective_routes = AppAgentRouteService.get_effective_routes_for_user(
                    db_session=db,
                    user_id=user_id,
                    channel="app_mcp",
                )
                identity_candidates = IdentityCandidateProvider.build(db, user_id)
        except Exception as e:
            logger.error("[AppMCP] Failed to load prompts for user %s: %s", user_id, e)
            return []

        from mcp.types import TextContent, PromptMessage

        def _entry(trigger_prompt: str, prompt_examples: str | None) -> list:
            messages = [
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=trigger_prompt),
                )
            ]
            for raw_line in (prompt_examples or "").splitlines():
                line = raw_line.strip()
                if line:
                    messages.append(
                        PromptMessage(
                            role="user",
                            content=TextContent(type="text", text=line),
                        )
                    )
            return messages

        prompts = []
        for route in effective_routes:
            prompts.extend(_entry(route.trigger_prompt, route.prompt_examples))
        for candidate in identity_candidates:
            prompts.extend(_entry(candidate.trigger_prompt, candidate.prompt_examples))
        return prompts
