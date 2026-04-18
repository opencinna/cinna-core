"""
ExternalA2AService — AgentCard builder for the External A2A surface.

Phase 2: builds cards for target_type="agent".
Phase 3: adds target_type="app_mcp_route" — cards are built from the underlying
agent but the route's name/description are used and URLs are rewritten to the
route-scoped external namespace.
Phase 4: adds target_type="identity" — person-level card synthesized from the
identity owner + their caller-accessible bindings.  Each binding contributes
one AgentSkill whose id is the binding id (opaque to the caller).
"""
from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
)
from sqlmodel import Session as DBSession

from app.models import Agent, User
from app.models.app_mcp.app_agent_route import AppAgentRoute
from app.models.environments.environment import AgentEnvironment
from app.models.identity.identity_models import IdentityAgentBinding
from app.services.a2a.a2a_service import A2AService
from app.services.external.errors import InvalidExternalParamsError
from app.services.external.external_access_policy import ExternalAccessPolicy

logger = logging.getLogger(__name__)


class ExternalA2AService:
    """Builds A2A AgentCards for external targets."""

    @staticmethod
    def build_card(
        db: DBSession,
        user: User,
        target_type: str,
        target_id: UUID,
        request_base_url: str,
        protocol: Literal["v1.0", "v0.3"] = "v1.0",
    ) -> dict:
        """Build an AgentCard for the given target.

        Supported target types:
          - "agent"          (Phase 2)
          - "app_mcp_route"  (Phase 3)
          - "identity"       (Phase 4) — target_id is the identity owner's user id

        Args:
            db: Database session.
            user: Authenticated caller.
            target_type: "agent", "app_mcp_route", or "identity".
            target_id: UUID of the target (agent.id, route.id, or owner_id).
            request_base_url: Base URL of the request (no trailing slash).
            protocol: "v1.0" (default) or "v0.3".

        Returns:
            JSON-serializable AgentCard dict.

        Raises:
            TargetNotAccessibleError: Target not found or access denied.
            NoActiveEnvironmentError: Target agent has no active environment
                (only when env resolution is required).
            InvalidExternalParamsError: Unknown target_type.
        """
        if target_type == "agent":
            return ExternalA2AService._build_agent_card(
                db, user, target_id, request_base_url, protocol
            )
        if target_type == "app_mcp_route":
            return ExternalA2AService._build_route_card(
                db, user, target_id, request_base_url, protocol
            )
        if target_type == "identity":
            return ExternalA2AService._build_identity_card(
                db, user, target_id, request_base_url, protocol
            )
        raise InvalidExternalParamsError(
            f"Unsupported target_type: {target_type!r}"
        )

    @staticmethod
    def _build_agent_card(
        db: DBSession,
        user: User,
        agent_id: UUID,
        request_base_url: str,
        protocol: Literal["v1.0", "v0.3"],
    ) -> dict:
        """Build card for target_type="agent".

        - Verifies agent.owner_id == user.id (regardless of a2a_config.enabled)
        - Uses A2AService.build_agent_card() for the full card structure
        - Sets url to the external-namespace path
        - For v1.0: applies A2AV1Adapter then overwrites supportedInterfaces with
          correct external URLs (the adapter inserts standard /a2a/ paths which are
          wrong for the external namespace)
        - For v0.3: returns the card as-is with url pointing at the external path
        """
        agent, _ = ExternalAccessPolicy.resolve_agent(db, user, agent_id)

        environment = (
            db.get(AgentEnvironment, agent.active_environment_id)
            if agent.active_environment_id
            else None
        )

        external_url = f"{request_base_url}/api/v1/external/a2a/agent/{agent_id}/"
        card_dict = A2AService.get_agent_card_dict(
            agent, environment, request_base_url,
            url_override=external_url, protocol=protocol,
        )
        return ExternalA2AService._finalize_card(card_dict, external_url, protocol)

    @staticmethod
    def _build_route_card(
        db: DBSession,
        user: User,
        route_id: UUID,
        request_base_url: str,
        protocol: Literal["v1.0", "v0.3"],
    ) -> dict:
        """Build card for target_type="app_mcp_route".

        - Re-verifies the route is effective for the caller
        - Builds card from the underlying agent (route.agent_id)
        - Overrides name with route.name and description with route.trigger_prompt
        - Rewrites URL to the route-scoped external path
        """
        route, _effective = ExternalAccessPolicy.resolve_route(db, user, route_id)

        agent = db.get(Agent, route.agent_id)
        if not agent:
            from app.services.external.errors import TargetNotAccessibleError
            raise TargetNotAccessibleError("Route agent not found")

        environment = (
            db.get(AgentEnvironment, agent.active_environment_id)
            if agent.active_environment_id
            else None
        )

        external_url = f"{request_base_url}/api/v1/external/a2a/route/{route_id}/"
        card_dict = A2AService.get_agent_card_dict(
            agent, environment, request_base_url,
            url_override=external_url, protocol=protocol,
        )

        # Override name / description with route-specific values so the caller
        # sees the shared-route identity rather than the raw underlying agent.
        card_dict["name"] = route.name
        card_dict["description"] = route.trigger_prompt

        return ExternalA2AService._finalize_card(card_dict, external_url, protocol)

    @staticmethod
    def _build_identity_card(
        db: DBSession,
        user: User,
        owner_id: UUID,
        request_base_url: str,
        protocol: Literal["v1.0", "v0.3"],
    ) -> dict:
        """Build card for target_type="identity".

        - Verifies the caller has at least one active+enabled binding from the
          owner (via IdentityService.get_active_bindings_for_user).
        - Synthesizes a person-level card: name=owner.full_name,
          description=owner.email, one AgentSkill per accessible binding.
        - Skill ``id`` is the binding id (opaque UUID — the caller never sees
          internal agent ids).
        - URL / supportedInterfaces point at the identity-scoped external path.
        """
        bindings = ExternalAccessPolicy.require_identity_access(db, user, owner_id)
        owner = db.get(User, owner_id)
        # owner is guaranteed non-None by require_identity_access

        skills = ExternalA2AService._synth_identity_skills(db, bindings)
        external_url = f"{request_base_url}/api/v1/external/a2a/identity/{owner_id}/"

        # Person-level card — we synthesize it directly rather than going through
        # A2AService.build_agent_card() because the "agent" being represented is
        # the person, not any single Agent row.
        card = AgentCard(
            name=owner.full_name or owner.email or str(owner_id),
            description=owner.email or "",
            url=external_url,
            version="1.0.0",
            protocolVersion="0.3.0",
            defaultInputModes=["text/plain"],
            defaultOutputModes=["text/plain"],
            capabilities=AgentCapabilities(
                streaming=True,
                pushNotifications=False,
                stateTransitionHistory=True,
            ),
            skills=skills,
            supportsAuthenticatedExtendedCard=True,
        )
        card_dict = card.model_dump(by_alias=True, exclude_none=True)
        card_dict = A2AService.apply_protocol(card_dict, protocol)

        return ExternalA2AService._finalize_card(card_dict, external_url, protocol)

    @staticmethod
    def _synth_identity_skills(
        db: DBSession,
        bindings: list[IdentityAgentBinding],
    ) -> list[AgentSkill]:
        """One AgentSkill per accessible binding.

        - ``id``          — binding.id (UUID string, opaque to the caller)
        - ``name``        — the underlying agent's name (fallback: trigger prompt)
        - ``description`` — binding.trigger_prompt
        - ``examples``    — binding.prompt_examples split on newlines (empty if None)
        - ``tags``        — empty (no tagging yet)
        """
        skills: list[AgentSkill] = []
        for binding in bindings:
            agent = db.get(Agent, binding.agent_id)
            examples = [
                line.strip()
                for line in (binding.prompt_examples or "").splitlines()
                if line.strip()
            ]
            skills.append(
                AgentSkill(
                    id=str(binding.id),
                    name=(agent.name if agent else binding.trigger_prompt),
                    description=binding.trigger_prompt,
                    tags=[],
                    examples=examples,
                )
            )
        return skills

    @staticmethod
    def _finalize_card(
        card_dict: dict,
        external_url: str,
        protocol: Literal["v1.0", "v0.3"],
    ) -> dict:
        """Overwrite v1.0 ``supportedInterfaces`` with external URL variants.

        The v1.0 protocol adapter is applied by ``A2AService`` (agent / route
        paths) or explicitly by the caller (identity path). This helper only
        replaces the adapter's default ``/api/v1/a2a/...`` interfaces with the
        external-namespace URLs. For v0.3 it returns the card as-is.
        """
        if protocol == "v1.0":
            card_dict["supportedInterfaces"] = [
                {
                    "url": external_url,
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                },
                {
                    "url": f"{external_url}?protocol=v0.3",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "0.3.0",
                },
            ]
        return card_dict