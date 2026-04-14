"""
OpenAI Provider - direct HTTP calls to OpenAI Chat Completions API.

Makes direct httpx calls to https://api.openai.com/v1/chat/completions.
No OpenAI SDK dependency — only httpx (already a project dependency).

Can be used in two modes:
- System provider: instantiated with no api_key, reads from settings.OPENAI_API_KEY.
  Errors are recoverable so the cascade continues to the next provider.
- Personal provider: instantiated with an explicit api_key (per-user personal key routing).
  Errors are non-recoverable — no fallback to system providers.
"""
import logging
from typing import Optional

import httpx

from app.core.config import settings
from .base import BaseAIProvider, ProviderError, ProviderResponse

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseAIProvider):
    """
    OpenAI provider using direct HTTP calls to the Chat Completions API.

    Accepts an optional explicit api_key. When not provided, falls back to
    settings.OPENAI_API_KEY for system-level usage.

    Default model: gpt-4o-mini (fast, cheap)
    """

    PROVIDER_NAME = "openai"
    DEFAULT_MODEL = "gpt-4o-mini"
    API_URL = "https://api.openai.com/v1/chat/completions"
    REQUEST_TIMEOUT = 30.0

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the OpenAI provider.

        Args:
            api_key: Optional OpenAI API key. When provided, this is a personal
                     key and errors will be non-recoverable. When None, falls
                     back to settings.OPENAI_API_KEY and errors are recoverable
                     (allowing cascade to the next system provider).
        """
        if api_key is not None:
            self._api_key = api_key
            self._personal_key = True
        else:
            self._api_key = settings.OPENAI_API_KEY
            self._personal_key = False

    def is_available(self) -> bool:
        """Check if the provider is available (API key is set)."""
        return bool(self._api_key)

    def generate_content(self, prompt: str, model: Optional[str] = None) -> ProviderResponse:
        """
        Generate content by calling the OpenAI Chat Completions API.

        Args:
            prompt: The prompt to send
            model: Optional model override (default: gpt-4o-mini)

        Returns:
            ProviderResponse with generated text

        Raises:
            ProviderError: On any failure. recoverable=False for personal key usage
                          (no cascade intended), recoverable=True for system usage
                          (allows cascade to next provider).
        """
        recoverable = not self._personal_key

        if not self.is_available():
            raise ProviderError(
                "OpenAI API key not provided",
                self.PROVIDER_NAME,
                recoverable=recoverable,
            )

        model_name = model or self.DEFAULT_MODEL

        try:
            response = httpx.post(
                self.API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_name,
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=self.REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                error_body = response.text[:300]
                logger.warning(
                    f"OpenAI API returned {response.status_code}: {error_body}"
                )
                raise ProviderError(
                    f"OpenAI API returned HTTP {response.status_code}: {error_body}",
                    self.PROVIDER_NAME,
                    recoverable=recoverable,
                )

            data = response.json()
            text = data["choices"][0]["message"]["content"].strip()

            logger.debug(
                f"OpenAI generated {len(text)} chars using {model_name}"
            )

            return ProviderResponse(
                text=text,
                provider_name=self.PROVIDER_NAME,
                model=model_name,
            )

        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError(
                "OpenAI API request timed out",
                self.PROVIDER_NAME,
                recoverable=recoverable,
            )
        except Exception as e:
            raise ProviderError(
                f"OpenAI API call failed: {e}",
                self.PROVIDER_NAME,
                recoverable=recoverable,
            )
