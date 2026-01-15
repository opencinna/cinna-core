"""
AI Functions Provider Manager - handles cascade provider selection.

This module manages the selection and fallback logic for AI function providers.
It tries providers in the order specified by AI_FUNCTIONS_PROVIDERS env variable
and falls back to the next provider if one fails.

Example:
    AI_FUNCTIONS_PROVIDERS=openai-compatible,gemini
    - First tries openai-compatible endpoint
    - If that fails, falls back to gemini

Usage:
    from app.agents.provider_manager import get_provider_manager

    manager = get_provider_manager()
    response = manager.generate_content("Your prompt here")
"""
import logging
from typing import Optional

from app.core.config import settings

from .providers import (
    BaseAIProvider,
    ProviderResponse,
    ProviderError,
    GeminiProvider,
    OpenAICompatibleProvider,
)


logger = logging.getLogger(__name__)


# Registry of available providers
PROVIDER_REGISTRY: dict[str, type[BaseAIProvider]] = {
    "gemini": GeminiProvider,
    "openai-compatible": OpenAICompatibleProvider,
}


class ProviderManager:
    """
    Manages AI function providers with cascade fallback support.

    The manager maintains an ordered list of providers based on configuration
    and handles automatic fallback when a provider fails.
    """

    def __init__(self, provider_order: Optional[list[str]] = None):
        """
        Initialize the provider manager.

        Args:
            provider_order: Optional list of provider names in priority order.
                          If not provided, uses settings.ai_functions_provider_list
        """
        self._provider_order = provider_order or settings.ai_functions_provider_list
        self._providers: dict[str, BaseAIProvider] = {}
        self._initialize_providers()

    def _initialize_providers(self):
        """Initialize all configured providers."""
        for provider_name in self._provider_order:
            provider_name = provider_name.lower()
            if provider_name in PROVIDER_REGISTRY:
                try:
                    provider_class = PROVIDER_REGISTRY[provider_name]
                    provider = provider_class()
                    self._providers[provider_name] = provider
                    logger.debug(
                        f"Initialized provider: {provider_name} "
                        f"(available: {provider.is_available()})"
                    )
                except Exception as e:
                    logger.warning(f"Failed to initialize provider {provider_name}: {e}")
            else:
                logger.warning(
                    f"Unknown provider '{provider_name}' in AI_FUNCTIONS_PROVIDERS. "
                    f"Available: {list(PROVIDER_REGISTRY.keys())}"
                )

    def get_available_providers(self) -> list[str]:
        """
        Get list of available (configured) providers in priority order.

        Returns:
            List of provider names that are available
        """
        return [
            name for name in self._provider_order
            if name in self._providers and self._providers[name].is_available()
        ]

    def is_available(self) -> bool:
        """
        Check if at least one provider is available.

        Returns:
            True if at least one provider can be used
        """
        return len(self.get_available_providers()) > 0

    def generate_content(
        self,
        prompt: str,
        model: Optional[str] = None,
        preferred_provider: Optional[str] = None,
    ) -> ProviderResponse:
        """
        Generate content using the cascade of providers.

        Tries providers in order and falls back to the next one if a provider fails.

        Args:
            prompt: The prompt to send to the LLM
            model: Optional model override (provider-specific)
            preferred_provider: Optional preferred provider to try first

        Returns:
            ProviderResponse with generated text

        Raises:
            ProviderError: If all providers fail
        """
        # Build provider order with preferred provider first
        providers_to_try = []
        if preferred_provider and preferred_provider in self._providers:
            providers_to_try.append(preferred_provider)

        for name in self._provider_order:
            if name not in providers_to_try and name in self._providers:
                providers_to_try.append(name)

        if not providers_to_try:
            raise ProviderError(
                "No providers configured. Set AI_FUNCTIONS_PROVIDERS in .env",
                "none",
                recoverable=False,
            )

        # Track errors for detailed error message
        errors: list[tuple[str, str]] = []

        for provider_name in providers_to_try:
            provider = self._providers[provider_name]

            if not provider.is_available():
                logger.debug(f"Skipping unavailable provider: {provider_name}")
                errors.append((provider_name, "Not configured/available"))
                continue

            try:
                logger.info(f"Trying provider: {provider_name}")
                response = provider.generate_content(prompt, model)
                logger.info(
                    f"Successfully generated content using {provider_name} "
                    f"({len(response.text)} chars)"
                )
                return response

            except ProviderError as e:
                logger.warning(f"Provider {provider_name} failed: {e}")
                errors.append((provider_name, str(e)))

                if not e.recoverable:
                    logger.error(f"Non-recoverable error from {provider_name}, stopping cascade")
                    raise

                # Continue to next provider
                continue

            except Exception as e:
                logger.warning(f"Unexpected error from {provider_name}: {e}")
                errors.append((provider_name, str(e)))
                # Continue to next provider
                continue

        # All providers failed
        error_details = "; ".join([f"{name}: {err}" for name, err in errors])
        raise ProviderError(
            f"All providers failed. Errors: {error_details}",
            "cascade",
            recoverable=False,
        )

    def get_provider(self, name: str) -> Optional[BaseAIProvider]:
        """
        Get a specific provider by name.

        Args:
            name: Provider name

        Returns:
            Provider instance or None if not found
        """
        return self._providers.get(name)


# Singleton instance
_provider_manager: Optional[ProviderManager] = None


def get_provider_manager() -> ProviderManager:
    """
    Get the global provider manager instance.

    Returns:
        ProviderManager singleton
    """
    global _provider_manager
    if _provider_manager is None:
        _provider_manager = ProviderManager()
    return _provider_manager


def reset_provider_manager():
    """
    Reset the provider manager singleton.

    Useful for testing or when configuration changes.
    """
    global _provider_manager
    _provider_manager = None
