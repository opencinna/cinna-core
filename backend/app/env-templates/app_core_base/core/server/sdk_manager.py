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
    - opencode/anthropic: OpenCode with Anthropic backend

The manager reads these ENV variables and instantiates the appropriate adapter
for each mode. All adapters produce unified SDKEvent objects that are converted
to dictionaries for backward compatibility with the backend streaming protocol.
"""

import os
import asyncio
import logging
import contextvars
from typing import AsyncIterator, Optional

from . import skills_projection
from .adapters import (
    AdapterRegistry,
    SDKConfig,
    SDKEvent,
    SDKEventType,
    BaseSDKAdapter,
    # Import adapters to register them
    ClaudeCodeAdapter,
    OpenCodeAdapter,
)

# Re-export session tracking functions from claude_code SDK adapter
from .adapters.claude_code_sdk_adapter import (
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

        # Last skills-projection identity each MODE has been told about.
        #
        # Per mode, not global: building and conversation run separate engine
        # processes with separate memoized skill lists, so a skill projected
        # during a building message must still register as a change for the
        # conversation adapter. A single shared flag would lose exactly the
        # feature's headline flow — build a skill in building mode, use it in
        # conversation.
        #
        # The identity, not the tree hash: it also moves when a previously
        # failed skill finally lands (which the hash cannot see), and it holds
        # still while a durable failure keeps retrying (which a "did this run
        # report errors" gate could not, and which would otherwise dispose the
        # OpenCode instance on every single turn).
        self._skills_state_by_mode: dict[str, str] = {}

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
        session_state: Optional[dict] = None,
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
            session_state: Backend-managed state context (e.g., previous_result_state)

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

            # Project the workspace's skills into the engine's skill root
            # BEFORE the adapter runs, so this message already sees a skill the
            # agent wrote during the previous one. Engine-agnostic and
            # hash-short-circuited: unchanged trees cost one stat walk. Never
            # raises — a projection failure degrades to "the model does not see
            # the new skill", never to a failed turn.
            #
            # Off the event loop: the fast path is a stat walk, but a real
            # re-projection copies up to the 16 MB skills budget, and this
            # process also serves every other session's SSE stream.
            projection = await asyncio.to_thread(
                skills_projection.refresh, adapter.workspace_dir
            )

            # A mode is "up to date" only once IT has been told about an
            # identity. An empty identity means the projection could not
            # determine anything at all — say nothing changed rather than
            # asking the engine to rebuild on no evidence.
            skills_changed = bool(projection.identity) and (
                projection.identity != self._skills_state_by_mode.get(mode)
            )
            if projection.identity:
                self._skills_state_by_mode[mode] = projection.identity
            # Latched before the adapter runs, so an engine that cannot act on
            # the signal (an older OpenCode with no /instance/dispose) is not
            # told again for the life of this process. That is the documented
            # degradation — the skills appear at the next server start, and
            # /rebuild-env is the user-facing fix — not a silent loss.

            # Stream events from adapter and convert to dicts
            async for event in adapter.send_message_stream(
                message=message,
                session_id=session_id,
                backend_session_id=backend_session_id,
                system_prompt=system_prompt,
                mode=mode,
                session_state=session_state,
                skills_changed=skills_changed,
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

    async def stop_opencode_servers(self) -> list[str]:
        """Terminate every running OpenCode server so it relaunches lazily.

        OpenCode reads its plugin-derived config (MCP servers, plugin ``skills``
        paths, slash commands) once, at ``opencode serve`` start. A plugin
        install/uninstall/toggle therefore has no effect until the server
        restarts — so ``routes.install_plugins`` calls this after a successful
        manifest install and the next message pays the ~30s relaunch once,
        instead of the agent silently running with the previous plugin set.

        Claude Code adapters are untouched: they spawn a fresh CLI per message
        and pick up plugin changes on their own.

        Returns the modes whose server was actually stopped.
        """
        stopped: list[str] = []
        for mode, adapter in self._adapters.items():
            stop = getattr(adapter, "stop", None)
            if stop is None:
                continue
            try:
                if await stop():
                    stopped.append(mode)
            except Exception as e:  # noqa: BLE001 — never break plugin install
                logger.warning(f"Failed to stop {mode} adapter server: {e}")
        if stopped:
            logger.info(f"Stopped OpenCode server(s) for mode(s): {stopped}")
        return stopped

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
