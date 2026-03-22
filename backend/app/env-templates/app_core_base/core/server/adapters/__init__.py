"""
SDK Adapters Package

This package provides a unified interface for different AI SDK providers.

Two distinct abstractions live here:

- **SDK Adapters** (``*_sdk_adapter.py``) — orchestrate an SDK's lifecycle:
  subprocess management, session creation/resumption, streaming, interrupts.
  Each adapter converts its SDK-specific events into unified ``SDKEvent``
  objects via the corresponding event transformer.

- **Event Transformers** (``*_event_transformer.py``) — stateless or
  stateful translators that normalise raw SDK messages/events into the
  common ``SDKEvent`` format consumed by the backend streaming pipeline.
  Tool names are lowercased via ``tool_name_registry.py``.

Supported SDK adapters:
- claude-code/*: Claude Code SDK (Anthropic, MiniMax)
- google-adk-wr/*: Google ADK Wrapper (OpenAI-compatible, Gemini, Vertex)
- opencode/*: OpenCode multi-provider agent (Anthropic, OpenAI, Google, etc.)
"""

from .base import (
    BaseSDKAdapter,
    SDKEvent,
    SDKEventType,
    SDKConfig,
    AdapterRegistry,
)

# SDK Adapters (canonical locations)
from .claude_code_sdk_adapter import ClaudeCodeAdapter
from .google_adk_sdk_adapter import GoogleADKAdapter
from .opencode_sdk_adapter import OpenCodeAdapter

# Event Transformers
from .claude_code_event_transformer import ClaudeCodeEventTransformer
from .opencode_event_transformer import OpenCodeEventTransformer
from .google_adk_event_transformer import GoogleADKEventTransformer

__all__ = [
    # Base
    "BaseSDKAdapter",
    "SDKEvent",
    "SDKEventType",
    "SDKConfig",
    "AdapterRegistry",
    # SDK Adapters
    "ClaudeCodeAdapter",
    "GoogleADKAdapter",
    "OpenCodeAdapter",
    # Event Transformers
    "ClaudeCodeEventTransformer",
    "OpenCodeEventTransformer",
    "GoogleADKEventTransformer",
]
