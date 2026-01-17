"""
A2A Service - Agent Card generation and A2A protocol coordination.

This module provides services for generating A2A-compliant AgentCards
from internal Agent models, enabling agent discovery via the A2A protocol.
"""
import logging
from uuid import UUID

from a2a.types import (
    AgentCard,
    AgentSkill,
    AgentCapabilities,
    AgentExtension,
    HTTPAuthSecurityScheme,
    SecurityScheme,
)

from app.models import Agent
from app.models.environment import AgentEnvironment

logger = logging.getLogger(__name__)


class A2AService:
    """Service for A2A protocol operations."""

    @staticmethod
    def _build_security_schemes() -> tuple[dict[str, SecurityScheme], list[dict[str, list[str]]]]:
        """
        Build security schemes for the AgentCard.

        Returns:
            Tuple of (securitySchemes dict, security list)
        """
        # Define Bearer JWT authentication scheme
        bearer_scheme = HTTPAuthSecurityScheme(
            scheme="Bearer",
            bearerFormat="JWT",
            description="JWT Bearer token for authentication. Use either a user JWT token or an A2A access token.",
        )

        security_schemes = {
            "bearerAuth": SecurityScheme(root=bearer_scheme),
        }

        # Security requirements - bearerAuth is required
        security = [{"bearerAuth": []}]

        return security_schemes, security

    @staticmethod
    def build_public_agent_card(
        agent: Agent,
        base_url: str
    ) -> AgentCard:
        """
        Build a minimal public A2A AgentCard with limited information.

        This card is returned for unauthenticated requests when A2A is enabled.
        It exposes only the agent name and URL, with extendedAgentCard=true
        to indicate that more details are available with authentication.

        Args:
            agent: The Agent model instance
            base_url: The base URL for the A2A endpoints

        Returns:
            Minimal AgentCard with name only
        """
        # Build capabilities
        capabilities = AgentCapabilities(
            streaming=True,
            pushNotifications=False,
            stateTransitionHistory=False,
        )

        # Build security schemes
        security_schemes, security = A2AService._build_security_schemes()

        # Return minimal public card
        return AgentCard(
            name=agent.name,
            description="AI Agent",  # Minimal description required by schema
            url=f"{base_url}/api/v1/a2a/{agent.id}/",
            version="1.0.0",
            protocolVersion="0.3.0",  # Library default version for stable mode
            defaultInputModes=["text/plain"],
            defaultOutputModes=["text/plain"],
            capabilities=capabilities,
            skills=[],  # No skills exposed in public card
            securitySchemes=security_schemes,
            security=security,
            supportsAuthenticatedExtendedCard=True,  # Indicates full card available with auth
        )

    @staticmethod
    def build_agent_card(
        agent: Agent,
        environment: AgentEnvironment | None,
        base_url: str
    ) -> AgentCard:
        """
        Build a full (extended) A2A AgentCard from internal Agent model.

        This card is returned for authenticated requests and includes
        all details: description, skills, extensions, etc.

        Args:
            agent: The Agent model instance
            environment: The agent's active environment (optional)
            base_url: The base URL for the A2A endpoints

        Returns:
            Full AgentCard compliant with A2A protocol
        """
        # Build skills from cached a2a_config
        skills = []
        if agent.a2a_config:
            for skill_data in agent.a2a_config.get("skills", []):
                try:
                    skills.append(AgentSkill(
                        id=skill_data.get("id", ""),
                        name=skill_data.get("name", ""),
                        description=skill_data.get("description", ""),
                        tags=skill_data.get("tags", []),
                        examples=skill_data.get("examples", []),
                    ))
                except Exception as e:
                    logger.warning(f"Failed to parse skill: {e}")

        # Build extensions from environment SDK type
        extensions = []
        if environment:
            sdk_type = environment.agent_sdk_conversation or environment.agent_sdk_building or "default"
            extensions.append(AgentExtension(
                uri=f"urn:workflow-runner:sdk:{sdk_type}",
                description=f"Powered by {sdk_type} SDK",
                required=False,
            ))

        # Get version from a2a_config
        version = "1.0.0"
        if agent.a2a_config:
            version = agent.a2a_config.get("version", "1.0.0")

        # Check if A2A is enabled (determines if extended card feature is available)
        a2a_enabled = agent.a2a_config.get("enabled", False) if agent.a2a_config else False

        # Build capabilities
        capabilities = AgentCapabilities(
            streaming=True,
            pushNotifications=False,  # Phase 2
            stateTransitionHistory=True,
            extensions=extensions if extensions else None,
        )

        # Build security schemes
        security_schemes, security = A2AService._build_security_schemes()

        # Build and return AgentCard
        return AgentCard(
            name=agent.name,
            description=agent.description or "AI Agent",
            url=f"{base_url}/api/v1/a2a/{agent.id}/",
            version=version,
            protocolVersion="0.3.0",  # Library default version for stable mode
            defaultInputModes=["text/plain"],
            defaultOutputModes=["text/plain"],
            capabilities=capabilities,
            skills=skills,
            securitySchemes=security_schemes,
            security=security,
            supportsAuthenticatedExtendedCard=a2a_enabled,  # Indicates extended card available
        )

    @staticmethod
    def get_agent_card_dict(
        agent: Agent,
        environment: AgentEnvironment | None,
        base_url: str
    ) -> dict:
        """
        Get full (extended) AgentCard as a dictionary for JSON serialization.

        Args:
            agent: The Agent model instance
            environment: The agent's active environment (optional)
            base_url: The base URL for the A2A endpoints

        Returns:
            Dictionary representation of full AgentCard
        """
        card = A2AService.build_agent_card(agent, environment, base_url)
        return card.model_dump(by_alias=True, exclude_none=True)

    @staticmethod
    def get_public_agent_card_dict(
        agent: Agent,
        base_url: str
    ) -> dict:
        """
        Get minimal public AgentCard as a dictionary for JSON serialization.

        This is returned for unauthenticated requests when A2A is enabled.
        Contains only the agent name and basic capabilities.

        Args:
            agent: The Agent model instance
            base_url: The base URL for the A2A endpoints

        Returns:
            Dictionary representation of minimal public AgentCard
        """
        card = A2AService.build_public_agent_card(agent, base_url)
        return card.model_dump(by_alias=True, exclude_none=True)
