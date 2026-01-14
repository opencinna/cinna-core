"""
SDK Manager - Multi-Adapter Support

This module provides a unified interface for managing different AI SDK adapters.
The adapter selection is based on environment variables that are set when the
environment is created (from the Environment model's SDK configuration).

Environment Variables:
    SDK_ADAPTER_BUILDING: Adapter ID for building mode (e.g., "claude-code/anthropic")
    SDK_ADAPTER_CONVERSATION: Adapter ID for conversation mode (e.g., "claude-code/minimax")

Adapter ID Format:
    <adapter-type>/<provider>
    Examples:
    - claude-code/anthropic: Claude Code SDK with Anthropic backend
    - claude-code/minimax: Claude Code SDK with MiniMax backend
    - google-adk-wr/gemini: Google ADK with Gemini (placeholder)

The manager reads these ENV variables and instantiates the appropriate adapter
for each mode. All adapters produce unified SDKEvent objects that are converted
to dictionaries for backward compatibility with the backend streaming protocol.
"""

import os
import logging
import contextvars
from typing import AsyncIterator, Optional

from .adapters import (
    AdapterRegistry,
    SDKConfig,
    SDKEvent,
    SDKEventType,
    BaseSDKAdapter,
    # Import adapters to register them
    ClaudeCodeAdapter,
    GoogleADKAdapter,
)

# Re-export session tracking functions from claude_code adapter for backward compatibility
from .adapters.claude_code import (
    get_current_sdk_session_id,
    set_current_sdk_session_id,
    clear_current_sdk_session_id,
    get_backend_session_id,
    set_backend_session_id,
    clear_backend_session_id,
)

logger = logging.getLogger(__name__)

# Context variable for session tracking (for SDK operations like interrupts)
current_session_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    'sdk_session_id', default=None
)


class SDKManager:
    """
    Multi-adapter SDK Manager.

    This manager handles adapter selection based on environment configuration
    and delegates message handling to the appropriate adapter.

    The adapter selection is determined by environment variables:
    - SDK_ADAPTER_BUILDING: Adapter for building mode
    - SDK_ADAPTER_CONVERSATION: Adapter for conversation mode

    These variables are set when the environment is created, based on the
    agent_sdk_building and agent_sdk_conversation fields in the Environment model.
    """

    # Default adapter if not specified in ENV
    DEFAULT_ADAPTER = "claude-code/anthropic"

    def __init__(self):
        """Initialize the SDK Manager."""
        self._adapters: dict[str, BaseSDKAdapter] = {}

        # Log available adapters
        available = AdapterRegistry.list_adapters()
        logger.info(f"Available SDK adapters: {available}")

        # Log configured adapters from ENV
        building_adapter = os.getenv("SDK_ADAPTER_BUILDING", self.DEFAULT_ADAPTER)
        conversation_adapter = os.getenv("SDK_ADAPTER_CONVERSATION", self.DEFAULT_ADAPTER)
        logger.info(f"Configured adapters - building: {building_adapter}, conversation: {conversation_adapter}")

    def _get_adapter(self, mode: str) -> BaseSDKAdapter:
        """
        Get or create the adapter for a given mode.

        Args:
            mode: "building" or "conversation"

        Returns:
            BaseSDKAdapter instance

        Raises:
            ValueError: If adapter type is unknown or unsupported
        """
        # Check cache first
        if mode in self._adapters:
            return self._adapters[mode]

        # Get adapter config from ENV
        config = SDKConfig.from_env(mode)

        logger.info(
            f"Creating adapter for mode '{mode}': "
            f"type={config.adapter_type}, provider={config.provider}"
        )

        # Create adapter via registry
        adapter = AdapterRegistry.create_adapter(config)

        if adapter is None:
            # Fall back to default adapter
            logger.warning(
                f"Unknown adapter type '{config.adapter_type}', "
                f"falling back to default: {self.DEFAULT_ADAPTER}"
            )
            fallback_config = SDKConfig(
                adapter_id=self.DEFAULT_ADAPTER,
                adapter_type="claude-code",
                provider="anthropic",
                workspace_dir=config.workspace_dir,
                permission_mode=config.permission_mode,
            )
            adapter = AdapterRegistry.create_adapter(fallback_config)

            if adapter is None:
                raise ValueError(
                    f"Could not create adapter for mode '{mode}'. "
                    f"Requested: {config.adapter_id}, Fallback: {self.DEFAULT_ADAPTER}"
                )

        # Cache the adapter
        self._adapters[mode] = adapter

        return adapter

    async def send_message_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
        backend_session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        mode: str = "conversation",
    ) -> AsyncIterator[dict]:
        """
        Send message to SDK and stream responses.

        Delegates to the appropriate adapter based on mode configuration.
        Converts SDKEvent objects to dictionaries for backward compatibility.

        Args:
            message: User message
            session_id: External SDK session ID to resume (None = create new)
            backend_session_id: Backend session ID for tracking
            system_prompt: Custom system prompt (overrides mode-based prompt)
            mode: "building" or "conversation" - determines adapter selection

        Yields:
            Dictionaries with message data (backward compatible format):
            {
                "type": "assistant" | "tool" | "result" | "error" | "session_created" | ...,
                "content": str,
                "session_id": str,
                "tool_name": str (only in tool events),
                "metadata": dict,
            }
        """
        try:
            # Get adapter for this mode
            adapter = self._get_adapter(mode)

            logger.info(
                f"Using adapter {adapter.__class__.__name__} "
                f"(type={adapter.ADAPTER_TYPE}) for mode '{mode}'"
            )

            # Stream events from adapter and convert to dicts
            async for event in adapter.send_message_stream(
                message=message,
                session_id=session_id,
                backend_session_id=backend_session_id,
                system_prompt=system_prompt,
                mode=mode,
            ):
                # Convert SDKEvent to dict for backward compatibility
                yield self._event_to_dict(event)

        except ValueError as e:
            logger.error(f"Adapter error: {e}")
            yield {
                "type": "error",
                "content": str(e),
                "error_type": "ValueError",
            }

        except Exception as e:
            logger.error(f"Unexpected error in send_message_stream: {e}", exc_info=True)
            yield {
                "type": "error",
                "content": f"Unexpected error: {str(e)}",
                "error_type": type(e).__name__,
            }

    def _event_to_dict(self, event: SDKEvent) -> dict:
        """
        Convert SDKEvent to dictionary format.

        This maintains backward compatibility with the existing backend
        streaming protocol.

        Args:
            event: SDKEvent object

        Returns:
            Dictionary representation
        """
        return event.to_dict()

    def get_adapter_info(self, mode: str) -> dict:
        """
        Get information about the adapter configured for a mode.

        Args:
            mode: "building" or "conversation"

        Returns:
            Dict with adapter information
        """
        config = SDKConfig.from_env(mode)
        return {
            "adapter_id": config.adapter_id,
            "adapter_type": config.adapter_type,
            "provider": config.provider,
            "is_registered": AdapterRegistry.get_adapter_class(config.adapter_type) is not None,
        }


# ==============================================================================
# Backward Compatibility Layer
# ==============================================================================

# Create a compatibility alias for the old class name
class ClaudeCodeSDKManager(SDKManager):
    """
    Backward compatibility alias for SDKManager.

    This class is deprecated. Use SDKManager instead.
    """

    def __init__(self):
        logger.warning(
            "ClaudeCodeSDKManager is deprecated. Use SDKManager instead. "
            "The new SDKManager supports multiple adapters via ENV configuration."
        )
        super().__init__()


# Global SDK manager instance (backward compatible)
sdk_manager = SDKManager()


# ==============================================================================
# Event Type Constants (for external use)
# ==============================================================================

# Re-export event types for consumers that need to check event types
EVENT_TYPE_SESSION_CREATED = SDKEventType.SESSION_CREATED.value
EVENT_TYPE_SYSTEM = SDKEventType.SYSTEM.value
EVENT_TYPE_ASSISTANT = SDKEventType.ASSISTANT.value
EVENT_TYPE_THINKING = SDKEventType.THINKING.value
EVENT_TYPE_TOOL = SDKEventType.TOOL_USE.value
EVENT_TYPE_DONE = SDKEventType.DONE.value
EVENT_TYPE_INTERRUPTED = SDKEventType.INTERRUPTED.value
EVENT_TYPE_ERROR = SDKEventType.ERROR.value
