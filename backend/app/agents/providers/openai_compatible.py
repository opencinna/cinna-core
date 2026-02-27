"""
OpenAI-Compatible Provider - supports any OpenAI-compatible endpoint via litellm.

This provider uses litellm to connect to OpenAI-compatible endpoints like:
- OpenAI API
- Azure OpenAI
- Ollama
- vLLM
- Local LLM deployments
- Any other OpenAI-compatible API
"""
import logging
from typing import Optional

from app.core.config import settings

from .base import BaseAIProvider, ProviderResponse, ProviderError


logger = logging.getLogger(__name__)


class OpenAICompatibleProvider(BaseAIProvider):
    """
    OpenAI-compatible provider using litellm.

    Configuration:
        OPENAI_COMPATIBLE_BASE_URL: Base URL for the API endpoint
        OPENAI_COMPATIBLE_API_KEY: API key for authentication
        OPENAI_COMPATIBLE_MODEL: Model to use (default: gpt-4o-mini)
    """

    PROVIDER_NAME = "openai-compatible"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """
        Initialize OpenAI-compatible provider.

        Args:
            base_url: Optional base URL override
            api_key: Optional API key override
            model: Optional model override
        """
        self._base_url = base_url or settings.OPENAI_COMPATIBLE_BASE_URL
        self._api_key = api_key or settings.OPENAI_COMPATIBLE_API_KEY
        self._model = model or settings.OPENAI_COMPATIBLE_MODEL

    def is_available(self) -> bool:
        """
        Check if OpenAI-compatible provider is available.

        Requires at least base_url to be configured.
        API key may be optional for some local deployments.
        """
        return bool(self._base_url)

    def generate_content(self, prompt: str, model: Optional[str] = None) -> ProviderResponse:
        """
        Generate content using OpenAI-compatible endpoint via litellm.

        Args:
            prompt: The prompt to send to the LLM
            model: Optional model override

        Returns:
            ProviderResponse with generated text

        Raises:
            ProviderError: If generation fails
        """
        if not self.is_available():
            raise ProviderError(
                "OPENAI_COMPATIBLE_BASE_URL is not configured",
                self.PROVIDER_NAME,
                recoverable=True,
            )

        model_name = model or self._model

        try:
            import litellm

            # Configure litellm for the OpenAI-compatible endpoint
            # The model format for litellm is "openai/<model_name>" when using custom base_url
            litellm_model = f"openai/{model_name}"

            logger.debug(
                f"OpenAI-compatible request: base_url={self._base_url}, model={model_name}"
            )

            response = litellm.completion(
                model=litellm_model,
                messages=[{"role": "user", "content": prompt}],
                api_base=self._base_url,
                api_key=self._api_key or "dummy-key",  # Some local endpoints don't require key
            )

            text = ""
            if response.choices and len(response.choices) > 0:
                text = response.choices[0].message.content or ""
                text = text.strip()

            usage = None
            if hasattr(response, "usage") and response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            logger.debug(f"OpenAI-compatible generated {len(text)} chars using {model_name}")

            return ProviderResponse(
                text=text,
                provider_name=self.PROVIDER_NAME,
                model=model_name,
                usage=usage,
            )

        except ImportError as e:
            raise ProviderError(
                f"litellm package not installed: {e}",
                self.PROVIDER_NAME,
                recoverable=False,
            )
        except Exception as e:
            logger.error(f"OpenAI-compatible generation failed: {e}")
            raise ProviderError(
                f"Failed to generate content: {str(e)}",
                self.PROVIDER_NAME,
                recoverable=True,
            )
