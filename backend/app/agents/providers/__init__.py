"""
AI Functions Provider abstraction layer.

This module provides a unified interface for different LLM providers
used by AI functions (title generation, SQL generation, etc.).

Supported providers:
- gemini: Google Gemini via google-genai SDK
- openai-compatible: OpenAI-compatible endpoints via litellm
- openai: Direct OpenAI Chat Completions API via httpx
- anthropic: Direct Anthropic Messages API via httpx (personal key routing)
"""
from .base import BaseAIProvider, ProviderResponse, ProviderError
from .gemini import GeminiProvider
from .openai_compatible import OpenAICompatibleProvider
from .anthropic_provider import AnthropicProvider
from .openai_provider import OpenAIProvider

__all__ = [
    "BaseAIProvider",
    "ProviderResponse",
    "ProviderError",
    "GeminiProvider",
    "OpenAICompatibleProvider",
    "AnthropicProvider",
    "OpenAIProvider",
]
