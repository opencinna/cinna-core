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
import time
from typing import Optional

from app.core.config import settings
from app.services.routing import routing_trace

from .providers import (
    BaseAIProvider,
    ProviderAttempt,
    ProviderResponse,
    ProviderError,
    GeminiProvider,
    OpenAICompatibleProvider,
    AnthropicProvider,
    OpenAIProvider,
)


logger = logging.getLogger(__name__)


def _note_attempt(
    attempts: list[ProviderAttempt],
    *,
    provider: str,
    model: Optional[str],
    ok: bool,
    error: Optional[str] = None,
    exc: Optional[BaseException] = None,
    latency_ms: int = 0,
) -> None:
    """Record one cascade attempt, both on the response and on any routing trace.

    The trace side no-ops when there is no active capture, which is the common
    case: most AI functions never open one.

    ``exc`` is the failure itself, and it is what the **routing trace** records —
    de-tainted to an exception type, this provider's name, and an integer HTTP
    status when one is available. ``str(exc)`` must not reach the trace: provider
    SDK exceptions routinely echo the request payload back in their message, and
    at the router's call site that payload is the rendered classifier prompt,
    which contains the sender's message. That put external senders' words into
    ``stages[].llm_attempts[].error`` — outside the
    ``ROUTING_TRACE_STORE_MESSAGE_TEXT`` gate entirely.

    De-tainted here rather than gated downstream on purpose: gating would hide
    outage diagnostics behind a *text* flag and blunt ``?outcome=error``, which
    is the filter an operator under privacy pressure needs most. A field made
    safe beats a field made invisible.

    ``error`` is unchanged and still carries ``str(exc)`` onto the
    ``ProviderAttempt``, which stays in memory: it is what
    ``generate_content`` builds its "All providers failed" message from, and
    every other consumer of that message is out of scope here.
    """
    attempts.append(
        ProviderAttempt(
            provider=provider,
            model=model,
            ok=ok,
            error=error,
            latency_ms=latency_ms,
        )
    )
    routing_trace.record_llm_attempt(
        provider=provider,
        model=model,
        ok=ok,
        # Total by contract (like ``clamp``), so this argument expression cannot
        # raise into the cascade — §11a Rule 2.
        error=routing_trace.describe_exception(exc, provider=provider)
        if exc is not None
        else error,
        latency_ms=latency_ms,
    )


# Registry of available providers
PROVIDER_REGISTRY: dict[str, type[BaseAIProvider]] = {
    "gemini": GeminiProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
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
        api_key: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> ProviderResponse:
        """
        Generate content using the cascade of providers.

        Tries providers in order and falls back to the next one if a provider fails.

        When api_key is provided, bypasses the cascade entirely and calls the
        appropriate personal provider directly with that key. No fallback occurs —
        if it fails, the error propagates to the caller. This is used for per-user
        personal API key routing.

        Args:
            prompt: The prompt to send to the LLM
            model: Optional model override (provider-specific)
            preferred_provider: Optional preferred provider to try first
            api_key: Optional personal API key. When set, bypasses cascade and
                     uses the specified personal provider directly with no fallback.
            provider: Optional provider name to use with personal api_key.
                      Supported: "openai", "anthropic" (default when not specified).

        Returns:
            ProviderResponse with generated text

        Raises:
            ProviderError: If all providers fail (or if personal key call fails)
        """
        # Personal API key path: bypass cascade, no fallback
        if api_key:
            if provider == "openai":
                logger.info("Using personal OpenAI API key for AI function call")
                provider_instance = OpenAIProvider(api_key=api_key)
                personal_name = "openai (personal key)"
            else:
                logger.info("Using personal Anthropic API key for AI function call")
                provider_instance = AnthropicProvider(api_key=api_key)
                personal_name = "anthropic (personal key)"

            personal_attempts: list[ProviderAttempt] = []
            started = time.monotonic()
            try:
                response = provider_instance.generate_content(prompt, model)
            except Exception as e:
                _note_attempt(
                    personal_attempts,
                    provider=personal_name,
                    model=model,
                    ok=False,
                    error=str(e),
                    exc=e,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            _note_attempt(
                personal_attempts,
                provider=personal_name,
                model=response.model or model,
                ok=True,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            response.attempts = personal_attempts
            return response

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

        # Every provider tried, in order — the successful one included. This
        # used to be an error-only list discarded on success; it now rides back
        # on the response so callers can see which models were reached.
        attempts: list[ProviderAttempt] = []

        for provider_name in providers_to_try:
            provider = self._providers[provider_name]

            if not provider.is_available():
                logger.debug(f"Skipping unavailable provider: {provider_name}")
                _note_attempt(
                    attempts,
                    provider=provider_name,
                    model=model,
                    ok=False,
                    error="Not configured/available",
                )
                continue

            started = time.monotonic()
            try:
                logger.info(f"Trying provider: {provider_name}")
                response = provider.generate_content(prompt, model)
                _note_attempt(
                    attempts,
                    provider=provider_name,
                    model=response.model or model,
                    ok=True,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                logger.info(
                    f"Successfully generated content using {provider_name} "
                    f"({len(response.text)} chars)"
                )
                response.attempts = attempts
                return response

            except ProviderError as e:
                logger.warning(f"Provider {provider_name} failed: {e}")
                _note_attempt(
                    attempts,
                    provider=provider_name,
                    model=model,
                    ok=False,
                    error=str(e),
                    exc=e,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )

                if not e.recoverable:
                    logger.error(f"Non-recoverable error from {provider_name}, stopping cascade")
                    raise

                # Continue to next provider
                continue

            except Exception as e:
                logger.warning(f"Unexpected error from {provider_name}: {e}")
                _note_attempt(
                    attempts,
                    provider=provider_name,
                    model=model,
                    ok=False,
                    error=str(e),
                    exc=e,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                # Continue to next provider
                continue

        # All providers failed
        error_details = "; ".join(
            f"{a.provider}: {a.error}" for a in attempts if not a.ok
        )
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
