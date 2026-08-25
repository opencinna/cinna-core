"""
App MCP Server prompts — exposes the caller's routable targets as MCP prompts.

This enables external AI clients (Claude Desktop, Cursor) to discover
available agents via MCP prompts/list without guessing.
"""
import logging
import re
import uuid

from app.mcp.context_vars import mcp_authenticated_user_id_var

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a candidate name to a slug suitable for use as a prompt name."""
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
        ballot — the agents this user owns **and** the identity owners they can
        address — because a discovery list that omits half the routable targets
        teaches the client the wrong vocabulary. The two providers are composed
        here in the same order, behind the same
        ``policy.allow_identity_routing`` gate, for exactly that reason: a
        prompt list built from a different question than the router asks is a
        vocabulary the router will refuse.

        Examples come from ``Agent.example_prompts`` (via
        ``ChannelCandidateProvider``) and ``IdentityAgentBinding.prompt_examples``
        (via ``IdentityCandidateProvider``, which keeps the owner-name
        prefixing). Neither is re-derived here.
        """
        from app.core.db import create_session
        from app.services.routing.channel_candidate_provider import (
            ChannelCandidateProvider,
        )
        from app.services.routing.identity_candidate_provider import (
            IdentityCandidateProvider,
        )
        from app.services.server_channels.adapters.app_mcp import AppMCPChannelAdapter
        from app.services.server_channels.channel_policy_service import (
            ChannelPolicyService,
        )
        from app.services.server_channels.server_channel_service import (
            ServerChannelService,
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
                channel = ServerChannelService.get_or_create_singleton(
                    db, AppMCPChannelAdapter.channel_type
                )
                policy = ChannelPolicyService.resolve(db, channel, user_id)
                candidates = ChannelCandidateProvider.build(db, user_id, policy=policy)
                if policy.allow_identity_routing:
                    candidates += IdentityCandidateProvider.build(db, user_id)
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
        for candidate in candidates:
            prompts.extend(_entry(candidate.trigger_prompt, candidate.prompt_examples))
        return prompts
