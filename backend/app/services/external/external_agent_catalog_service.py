"""
ExternalAgentCatalogService — Discovery layer for the External Agent Access API.

Builds the unified target list (GET /api/v1/external/agents) from three sources:
  1. Personal agents owned by the user
  2. MCP Shared Agents (AppAgentRoute assignments)
  3. Identity Contacts (IdentityAgentBinding-based)

No database writes are performed — this is a read-only service.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from sqlmodel import Session as DBSession, select

from app.models.agents.agent import Agent
from app.models.external.external_agents import (
    ExternalAgentListResponse,
    ExternalTargetPublic,
)
from app.models.users.user import User
from app.services.app_mcp.app_agent_route_service import (
    AppAgentRouteService,
    EffectiveRoute,
)
from app.services.identity.identity_service import IdentityService

logger = logging.getLogger(__name__)

# Maximum number of prompt examples to include per identity contact
# (aggregated across all their bindings)
_MAX_EXAMPLE_PROMPTS = 10


def _parse_prompt_examples(raw: str | None) -> list[str]:
    """Split a newline-separated prompt_examples string into a clean list.

    - Splits on newlines
    - Strips whitespace from each entry
    - Drops empty strings
    - Caps the result at _MAX_EXAMPLE_PROMPTS entries
    """
    if not raw:
        return []
    lines = [line.strip() for line in raw.split("\n")]
    return [line for line in lines if line][:_MAX_EXAMPLE_PROMPTS]


class ExternalAgentCatalogService:
    """Read-only service that aggregates addressable targets for a given user."""

    @staticmethod
    def list_targets(
        db: DBSession,
        user: User,
        request_base_url: str,
        workspace_id: Optional[uuid.UUID] = None,
    ) -> ExternalAgentListResponse:
        """Return the unified list of targets for `user`.

        Sections are assembled sequentially and concatenated:
          1. Personal agents (sorted by name)
          2. MCP Shared Agents (sorted by agent_name)
          3. Identity Contacts (sorted by owner_name)

        Args:
            db: Active database session.
            user: The authenticated user making the request.
            request_base_url: Base URL of the request (e.g. ``https://example.com``),
                used to build absolute ``agent_card_url`` values. Trailing slash must
                be stripped by the caller.
            workspace_id: Optional workspace filter. When provided, the personal
                agents section is limited to agents in this workspace. MCP shared
                agents and identity contacts are not filtered.

        Returns:
            ExternalAgentListResponse containing all three sections.
        """
        personal = ExternalAgentCatalogService._list_personal_agents(
            db, user, request_base_url, workspace_id=workspace_id
        )
        # Track agents already surfaced so the same underlying agent is never
        # returned twice (e.g. an agent exposed both as personal and via an
        # AppAgentRoute shared to the same user, or two routes pointing at the
        # same agent).
        seen_agent_ids: set[uuid.UUID] = {t.target_id for t in personal}
        shared = ExternalAgentCatalogService._list_mcp_shared_agents(
            db, user, request_base_url, seen_agent_ids=seen_agent_ids
        )
        identity = ExternalAgentCatalogService._list_identity_contacts(
            db, user, request_base_url
        )

        return ExternalAgentListResponse(targets=personal + shared + identity)

    # ------------------------------------------------------------------
    # Section helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _list_personal_agents(
        db: DBSession,
        user: User,
        base_url: str,
        workspace_id: Optional[uuid.UUID] = None,
    ) -> list[ExternalTargetPublic]:
        """Return active agents owned by (or cloned to) the user.

        When ``workspace_id`` is provided, only agents in that workspace are
        returned.  The workspace field on the Agent model is ``user_workspace_id``.
        """
        stmt = (
            select(Agent)
            .where(
                Agent.owner_id == user.id,
                Agent.is_active == True,  # noqa: E712
            )
            .order_by(Agent.name)
        )
        agents = db.exec(stmt).all()

        if workspace_id is not None:
            agents = [a for a in agents if a.user_workspace_id == workspace_id]

        results: list[ExternalTargetPublic] = []
        for agent in agents:
            results.append(
                ExternalTargetPublic(
                    target_type="agent",
                    target_id=agent.id,
                    name=agent.name,
                    description=agent.description,
                    entrypoint_prompt=agent.entrypoint_prompt,
                    example_prompts=(
                        list(agent.example_prompts)
                        if agent.example_prompts
                        else []
                    ),
                    session_mode=None,  # Agent model has no session_mode field
                    ui_color_preset=agent.ui_color_preset,
                    agent_card_url=(
                        f"{base_url}/api/v1/external/a2a/agent/{agent.id}/"
                    ),
                    metadata=ExternalAgentCatalogService._agent_metadata(agent),
                )
            )
        return results

    @staticmethod
    def _agent_metadata(agent: Agent) -> dict[str, Any]:
        return {
            "agent_id": str(agent.id),
            "bundle_id": agent.bundle_id,
            "bundle_uuid": str(agent.bundle_uuid) if agent.bundle_uuid else None,
            "is_publisher_install": agent.is_publisher_install,
            "active_environment_id": (
                str(agent.active_environment_id)
                if agent.active_environment_id
                else None
            ),
            "workspace_id": (
                str(agent.user_workspace_id) if agent.user_workspace_id else None
            ),
        }

    @staticmethod
    def _list_mcp_shared_agents(
        db: DBSession,
        user: User,
        base_url: str,
        seen_agent_ids: set[uuid.UUID] | None = None,
    ) -> list[ExternalTargetPublic]:
        """Return agents shared with the user via active AppAgentRoute assignments.

        Excludes identity-source routes (those are handled by the identity
        section) and any route whose underlying agent is already present in
        ``seen_agent_ids`` — this de-duplicates against the personal section
        and collapses multiple routes pointing at the same agent into a single
        entry.
        """
        routes = AppAgentRouteService.get_effective_routes_for_user(
            db_session=db,
            user_id=user.id,
            channel="app_mcp",
        )
        # Keep only direct agent routes; identity routes are in the identity section
        routes = [r for r in routes if r.source != "identity"]
        # Sort by agent name ascending
        routes.sort(key=lambda r: r.agent_name.lower())

        if seen_agent_ids is None:
            seen_agent_ids = set()

        results: list[ExternalTargetPublic] = []
        for route in routes:
            # Skip if we've already surfaced this agent (personal section or
            # an earlier route in this loop).
            if route.agent_id in seen_agent_ids:
                continue
            seen_agent_ids.add(route.agent_id)

            # Resolve agent to get entrypoint_prompt
            agent = db.get(Agent, route.agent_id)

            entrypoint_prompt = (
                agent.entrypoint_prompt
                if agent and agent.entrypoint_prompt
                else None
            )

            # Resolve agent owner details
            agent_owner_id: uuid.UUID | None = None
            agent_owner_name: str | None = None
            agent_owner_email: str | None = None
            if agent:
                owner = db.get(User, agent.owner_id)
                if owner:
                    agent_owner_id = owner.id
                    agent_owner_name = owner.full_name or ""
                    agent_owner_email = owner.email or ""

            results.append(
                ExternalTargetPublic(
                    target_type="app_mcp_route",
                    target_id=route.route_id,
                    name=route.agent_name,
                    description=route.trigger_prompt,
                    entrypoint_prompt=entrypoint_prompt,
                    example_prompts=_parse_prompt_examples(route.prompt_examples),
                    session_mode=(
                        route.session_mode
                        if route.session_mode in ("conversation", "building")
                        else None
                    ),
                    ui_color_preset=None,
                    agent_card_url=(
                        f"{base_url}/api/v1/external/a2a/route/{route.route_id}/"
                    ),
                    metadata=ExternalAgentCatalogService._route_metadata(
                        route,
                        agent_owner_id=agent_owner_id,
                        agent_owner_name=agent_owner_name,
                        agent_owner_email=agent_owner_email,
                    ),
                )
            )
        return results

    @staticmethod
    def _route_metadata(
        route: EffectiveRoute,
        *,
        agent_owner_id: uuid.UUID | None,
        agent_owner_name: str | None,
        agent_owner_email: str | None,
    ) -> dict[str, Any]:
        return {
            "route_id": str(route.route_id),
            "agent_id": str(route.agent_id),
            "agent_name": route.agent_name,
            "agent_owner_id": str(agent_owner_id) if agent_owner_id else None,
            "agent_owner_name": agent_owner_name,
            "agent_owner_email": agent_owner_email,
            "trigger_prompt": route.trigger_prompt,
        }

    @staticmethod
    def _list_identity_contacts(
        db: DBSession,
        user: User,
        base_url: str,
    ) -> list[ExternalTargetPublic]:
        """Return identity contacts that the user has enabled.

        Each entry represents one identity owner (person), not their individual agents.
        Prompt examples are aggregated from all accessible bindings and prefixed with
        the owner's name (e.g. "ask Alice to generate a report").
        """
        contacts = IdentityService.get_identity_contacts(
            db_session=db,
            user_id=user.id,
        )
        # Filter to contacts the user has enabled
        contacts = [c for c in contacts if c.is_enabled]
        # Sort by owner name ascending
        contacts.sort(key=lambda c: (c.owner_name or "").lower())

        results: list[ExternalTargetPublic] = []
        for contact in contacts:
            example_prompts = ExternalAgentCatalogService._aggregate_identity_examples(
                db, contact.owner_id, user.id, contact.owner_name
            )
            results.append(
                ExternalTargetPublic(
                    target_type="identity",
                    target_id=contact.owner_id,
                    name=contact.owner_name,
                    description=contact.owner_email,
                    entrypoint_prompt=None,
                    example_prompts=example_prompts,
                    session_mode=None,
                    ui_color_preset=None,
                    agent_card_url=(
                        f"{base_url}/api/v1/external/a2a/identity/{contact.owner_id}/"
                    ),
                    metadata={
                        "owner_id": str(contact.owner_id),
                        "owner_name": contact.owner_name,
                        "owner_email": contact.owner_email,
                        "agent_count": contact.agent_count,
                        "assignment_ids": [
                            str(aid) for aid in contact.assignment_ids
                        ],
                    },
                )
            )
        return results

    @staticmethod
    def _aggregate_identity_examples(
        db: DBSession,
        owner_id: uuid.UUID,
        caller_id: uuid.UUID,
        owner_name: str,
    ) -> list[str]:
        """Collect prompt examples from all accessible bindings for this identity owner.

        Each example is prefixed with "ask {owner_name} to " so that clients can
        use them as-is in the identity's A2A endpoint.

        Capped at _MAX_EXAMPLE_PROMPTS total entries.
        """
        bindings = IdentityService.get_active_bindings_for_user(
            db_session=db,
            owner_id=owner_id,
            target_user_id=caller_id,
        )

        all_examples: list[str] = []
        prefix = f"ask {owner_name} to "

        for binding in bindings:
            raw_examples = _parse_prompt_examples(binding.prompt_examples)
            for example in raw_examples:
                # Avoid double-prefixing if the example already starts with the prefix
                if example.lower().startswith(prefix.lower()):
                    all_examples.append(example)
                else:
                    all_examples.append(f"{prefix}{example}")
                if len(all_examples) >= _MAX_EXAMPLE_PROMPTS:
                    return all_examples

        return all_examples
