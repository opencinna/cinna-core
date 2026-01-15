"""
Base AI Provider interface for AI functions.

All provider implementations must inherit from BaseAIProvider and implement
the generate_content method.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised when a provider fails to process a request."""

    def __init__(self, message: str, provider_name: str, recoverable: bool = True):
        """
        Initialize ProviderError.

        Args:
            message: Error message
            provider_name: Name of the provider that failed
            recoverable: Whether the error is recoverable (should try next provider)
        """
        super().__init__(message)
        self.provider_name = provider_name
        self.recoverable = recoverable


@dataclass
class ProviderResponse:
    """Response from an AI provider."""

    text: str
    provider_name: str
    model: Optional[str] = None
    usage: Optional[dict] = None


class BaseAIProvider(ABC):
    """
    Abstract base class for AI function providers.

    All provider implementations must:
    1. Implement generate_content() for text generation
    2. Implement is_available() to check if provider is configured
    3. Define a unique PROVIDER_NAME class attribute
    """

    PROVIDER_NAME: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """
        Check if the provider is available and properly configured.

        Returns:
            True if provider can be used, False otherwise
        """
        pass

    @abstractmethod
    def generate_content(self, prompt: str, model: Optional[str] = None) -> ProviderResponse:
        """
        Generate content using the provider's LLM.

        Args:
            prompt: The prompt to send to the LLM
            model: Optional model override (provider-specific)

        Returns:
            ProviderResponse with generated text

        Raises:
            ProviderError: If generation fails
        """
        pass

    def get_name(self) -> str:
        """Get the provider name."""
        return self.PROVIDER_NAME

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(available={self.is_available()})"
