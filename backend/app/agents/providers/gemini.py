"""
Gemini Provider - Google Gemini via google-genai SDK.

This provider uses the official Google GenAI SDK for text generation.
"""
import logging
from typing import Optional

from app.core.config import settings

from .base import BaseAIProvider, ProviderResponse, ProviderError


logger = logging.getLogger(__name__)


class GeminiProvider(BaseAIProvider):
    """
    Google Gemini provider using google-genai SDK.

    Configuration:
        GOOGLE_API_KEY: Required API key for Gemini

    Default model: gemini-2.5-flash-lite (fast, cheap)
    """

    PROVIDER_NAME = "gemini"
    DEFAULT_MODEL = "gemini-2.5-flash-lite"
    # Timeout in ms for the SDK's internal retry loop (prevents long hangs on 429s)
    REQUEST_TIMEOUT_MS = 10000

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Gemini provider.

        Args:
            api_key: Optional API key override. If not provided, uses settings.GOOGLE_API_KEY
        """
        self._api_key = api_key or settings.GOOGLE_API_KEY
        self._client = None

    def is_available(self) -> bool:
        """Check if Gemini is available (API key configured)."""
        return bool(self._api_key)

    def _get_client(self):
        """Lazy initialization of Google GenAI client."""
        if self._client is None:
            if not self._api_key:
                raise ProviderError(
                    "GOOGLE_API_KEY is not configured",
                    self.PROVIDER_NAME,
                    recoverable=False,
                )
            try:
                from google.genai import Client
                self._client = Client(
                    api_key=self._api_key,
                    http_options={"timeout": self.REQUEST_TIMEOUT_MS},
                )
            except ImportError as e:
                raise ProviderError(
                    f"google-genai package not installed: {e}",
                    self.PROVIDER_NAME,
                    recoverable=False,
                )
        return self._client

    def generate_content(self, prompt: str, model: Optional[str] = None) -> ProviderResponse:
        """
        Generate content using Google Gemini.

        Args:
            prompt: The prompt to send to Gemini
            model: Optional model override (default: gemini-2.5-flash-lite)

        Returns:
            ProviderResponse with generated text

        Raises:
            ProviderError: If generation fails
        """
        model_name = model or self.DEFAULT_MODEL

        try:
            client = self._get_client()
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )

            text = response.text.strip() if response.text else ""

            logger.debug(f"Gemini generated {len(text)} chars using {model_name}")

            return ProviderResponse(
                text=text,
                provider_name=self.PROVIDER_NAME,
                model=model_name,
            )

        except ProviderError:
            raise
        except Exception as e:
            error_str = str(e)
            is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
            if is_rate_limit:
                logger.warning(
                    f"Gemini rate limit hit for {model_name}: {error_str}"
                )
            else:
                logger.error(f"Gemini generation failed: {e}", exc_info=True)
            raise ProviderError(
                f"Failed to generate content: {error_str}",
                self.PROVIDER_NAME,
                recoverable=True,
            )
