"""
SDK Adapters Package

This package provides a unified interface for different AI SDK providers.
Each adapter converts SDK-specific messages to a common event format
that the backend can process uniformly.

Supported adapters:
- claude-code/*: Claude Code SDK (Anthropic, MiniMax)
- google-adk-wr/*: Google ADK Wrapper (placeholder)
"""

from .base import (
    BaseSDKAdapter,
    SDKEvent,
    SDKEventType,
    SDKConfig,
    AdapterRegistry,
)
from .claude_code import ClaudeCodeAdapter
from .google_adk import GoogleADKAdapter

__all__ = [
    "BaseSDKAdapter",
    "SDKEvent",
    "SDKEventType",
    "SDKConfig",
    "AdapterRegistry",
    "ClaudeCodeAdapter",
    "GoogleADKAdapter",
]
