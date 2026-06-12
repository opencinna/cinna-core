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
from dataclasses import dataclass
from typing import Any, Optional

from sqlmodel import Session as DBSession, select

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.environments.environment import AgentEnvironment
from app.models.external.external_agents import (
    BundleVersionInfo,
    ExternalAgentListResponse,
    ExternalTargetPublic,
)
from app.models.users.user import User
from app.services.a2a.a2a_service import A2AService
from app.services.app_mcp.app_agent_route_service import (
    AppAgentRouteService,
    EffectiveRoute,
)
from app.services.identity.identity_service import IdentityService

logger = logging.getLogger(__name__)


@dataclass
class _DescriptorContext:
    """Everything needed to build a cinna.mcp descriptor for one target.

    Collected during the per-section passes (which already resolve the
    underlying Agent) and consumed by the deconfliction post-pass in
    ``list_targets``, where the full set of names is visible.
    """

    agent: Agent
    environment: AgentEnvironment | None
    # Name to slugify (agent name for personal targets, route name for routes).
    slug_source: str
    # Display name / description overrides for the descriptor (route identity).
    display_name: str | None = None
    description: str | None = None

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
        # Descriptor contexts keyed by target_id — populated by the section
        # helpers, consumed by the slug deconfliction post-pass below.
        descriptor_contexts: dict[uuid.UUID, _DescriptorContext] = {}

        personal = ExternalAgentCatalogService._list_personal_agents(
            db, user, request_base_url, workspace_id=workspace_id,
            descriptor_contexts=descriptor_contexts,
        )
        # Track agents already surfaced so the same underlying agent is never
        # returned twice (e.g. an agent exposed both as personal and via an
        # AppAgentRoute shared to the same user, or two routes pointing at the
        # same agent).
        seen_agent_ids: set[uuid.UUID] = {t.target_id for t in personal}
        shared = ExternalAgentCatalogService._list_mcp_shared_agents(
            db, user, request_base_url, seen_agent_ids=seen_agent_ids,
            descriptor_contexts=descriptor_contexts,
        )
        identity = ExternalAgentCatalogService._list_identity_contacts(
            db, user, request_base_url
        )

        targets = personal + shared + identity

        # Compute deconflicted slugs across the full reachable set and attach the
        # cinna.mcp descriptor to each target that has a context (identity
        # contacts are person-level and carry no single-tool descriptor).
        ExternalAgentCatalogService._attach_mcp_descriptors(
            targets, descriptor_contexts
        )

        return ExternalAgentListResponse(targets=targets)

    @staticmethod
    def _attach_mcp_descriptors(
        targets: list[ExternalTargetPublic],
        contexts: dict[uuid.UUID, _DescriptorContext],
    ) -> None:
        """Compute collision-free tool slugs and set ``target.mcp`` in place.

        Base slugs are derived from each target's name. When two or more targets
        slugify to the same base, every colliding target is suffixed with a
        deterministic discriminator derived from its agent id (not its position),
        so the result is stable across requests.
        """
        # First pass: base slug per target_id.
        base_slugs: dict[uuid.UUID, str] = {}
        slug_counts: dict[str, int] = {}
        for target in targets:
            ctx = contexts.get(target.target_id)
            if ctx is None:
                continue
            base = A2AService.slugify_tool_name(ctx.slug_source)
            base_slugs[target.target_id] = base
            slug_counts[base] = slug_counts.get(base, 0) + 1

        # Second pass: deconflict collisions and build the descriptor.
        for target in targets:
            ctx = contexts.get(target.target_id)
            if ctx is None:
                continue
            base = base_slugs[target.target_id]
            if slug_counts[base] > 1:
                tool_name = A2AService.deconflict_tool_name(base, ctx.agent.id)
            else:
                tool_name = base
            target.mcp = A2AService.build_cinna_mcp_descriptor(
                ctx.agent,
                ctx.environment,
                tool_name=tool_name,
                display_name=ctx.display_name,
                description=ctx.description,
            )

    # ------------------------------------------------------------------
    # Section helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _list_personal_agents(
        db: DBSession,
        user: User,
        base_url: str,
        workspace_id: Optional[uuid.UUID] = None,
        descriptor_contexts: dict[uuid.UUID, _DescriptorContext] | None = None,
    ) -> list[ExternalTargetPublic]:
        """Return active agents owned by (or cloned to) the user.

        When ``workspace_id`` is provided, only agents in that workspace are
        returned.  The workspace field on the Agent model is ``user_workspace_id``.

        When ``descriptor_contexts`` is provided, a ``_DescriptorContext`` is
        recorded per target so ``list_targets`` can build the cinna.mcp
        descriptor after computing collision-free slugs.
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
                    bundle_version=(
                        ExternalAgentCatalogService.build_bundle_version_info(
                            db, agent
                        )
                    ),
                )
            )
            if descriptor_contexts is not None:
                descriptor_contexts[agent.id] = _DescriptorContext(
                    agent=agent,
                    environment=ExternalAgentCatalogService._resolve_environment(
                        db, agent
                    ),
                    slug_source=agent.name,
                )
        return results

    @staticmethod
    def _resolve_environment(
        db: DBSession, agent: Agent
    ) -> AgentEnvironment | None:
        """Load the agent's active environment (or None when unset/missing)."""
        if not agent.active_environment_id:
            return None
        return db.get(AgentEnvironment, agent.active_environment_id)

    @staticmethod
    def build_bundle_version_info(
        db: DBSession, agent: Agent
    ) -> BundleVersionInfo | None:
        """Build installed-vs-latest version state for a consumer install.

        Returns ``None`` for anything that is not the caller's own consumer
        install: agents with no ``bundle_uuid`` (never installed from a
        bundle) and publisher working copies (``is_publisher_install=True``,
        which are the source of a bundle, not an install of it).

        Read-only — never mutates ``Agent.pending_update``. ``update_available``
        is derived from the monotonic ``revision_number`` comparison so it does
        not depend on the (separately reconciled) ``pending_update`` flag. The
        shared resolution also powers the external apply-update / check-updates
        routes so the discovery list and the action responses never diverge.
        """
        if not agent.bundle_uuid or agent.is_publisher_install:
            return None

        installed_number: int | None = None
        installed_version: str | None = None
        if agent.installed_revision_id:
            rev = db.get(AgentBundleRevision, agent.installed_revision_id)
            if rev:
                installed_number = rev.revision_number
                installed_version = rev.version

        latest_number: int | None = None
        latest_version: str | None = None
        bundle = db.get(AgentBundle, agent.bundle_uuid)
        if bundle and bundle.latest_revision_id:
            latest_rev = db.get(AgentBundleRevision, bundle.latest_revision_id)
            if latest_rev:
                latest_number = latest_rev.revision_number
                latest_version = latest_rev.version

        update_available = (
            latest_number is not None
            and installed_number is not None
            and latest_number > installed_number
        )

        return BundleVersionInfo(
            installed_revision_number=installed_number,
            installed_version=installed_version,
            latest_revision_number=latest_number,
            latest_version=latest_version,
            update_available=update_available,
            update_mode=agent.update_mode,
            last_update_status=agent.last_update_status,
        )

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
        descriptor_contexts: dict[uuid.UUID, _DescriptorContext] | None = None,
    ) -> list[ExternalTargetPublic]:
        """Return agents shared with the user via active AppAgentRoute assignments.

        Excludes identity-source routes (those are handled by the identity
        section) and any route whose underlying agent is already present in
        ``seen_agent_ids`` — this de-duplicates against the personal section
        and collapses multiple routes pointing at the same agent into a single
        entry.

        When ``descriptor_contexts`` is provided, a ``_DescriptorContext`` keyed
        by the route id is recorded for each surfaced route so ``list_targets``
        can build the cinna.mcp descriptor with the route's identity (route name
        + trigger prompt).
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
            if descriptor_contexts is not None and agent is not None:
                # Slug + identity come from the route the caller sees, not the
                # raw underlying agent. Use the route's own display name
                # (AppAgentRoute.name) so the descriptor matches the card path
                # (external_a2a_service passes route.name); fall back to the
                # agent name for routes without their own name (personal /
                # identity sources).
                route_display_name = route.name or route.agent_name
                descriptor_contexts[route.route_id] = _DescriptorContext(
                    agent=agent,
                    environment=ExternalAgentCatalogService._resolve_environment(
                        db, agent
                    ),
                    slug_source=route_display_name,
                    display_name=route_display_name,
                    description=route.trigger_prompt,
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
