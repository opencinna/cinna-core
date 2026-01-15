"""
AI Functions Provider abstraction layer.

This module provides a unified interface for different LLM providers
used by AI functions (title generation, SQL generation, etc.).

Supported providers:
- gemini: Google Gemini via google-genai SDK
- openai-compatible: OpenAI-compatible endpoints via litellm
"""
from .base import BaseAIProvider, ProviderResponse, ProviderError
from .gemini import GeminiProvider
from .openai_compatible import OpenAICompatibleProvider

__all__ = [
    "BaseAIProvider",
    "ProviderResponse",
    "ProviderError",
    "GeminiProvider",
    "OpenAICompatibleProvider",
]
