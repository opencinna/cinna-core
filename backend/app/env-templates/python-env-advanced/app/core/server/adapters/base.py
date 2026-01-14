"""
Base SDK Adapter and Unified Event Format

This module defines the common interface for all SDK adapters and the
unified event format that all adapters must produce. This ensures
consistent communication with the backend regardless of the underlying SDK.
"""

import os
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Optional, Any, Type

logger = logging.getLogger(__name__)


class SDKEventType(str, Enum):
    """
    Unified event types for all SDK adapters.

    These types represent the common events that can occur during
    SDK communication, regardless of the underlying SDK implementation.
    """
    # Session lifecycle events
    SESSION_CREATED = "session_created"
    SESSION_RESUMED = "session_resumed"

    # System events
    SYSTEM = "system"
    TOOLS_INIT = "tools_init"

    # Message events
    ASSISTANT = "assistant"
    THINKING = "thinking"
    TOOL_USE = "tool"
    TOOL_RESULT = "tool_result"

    # Completion events
    DONE = "done"
    INTERRUPTED = "interrupted"
    ERROR = "error"


@dataclass
class SDKEvent:
    """
    Unified event structure for SDK communication.

    All SDK adapters must convert their native messages to this format.
    The backend expects this structure for all events from the agent environment.

    Attributes:
        type: Event type from SDKEventType enum
        content: Human-readable content (text response, tool description, error message)
        session_id: SDK session ID for session tracking
        metadata: Additional event-specific data
        tool_name: Name of the tool (only for TOOL_USE events)
        error_type: Type of error (only for ERROR events)
        session_corrupted: Whether the session is corrupted (only for ERROR events)
    """
    type: SDKEventType
    content: str = ""
    session_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    tool_name: Optional[str] = None
    error_type: Optional[str] = None
    session_corrupted: bool = False
    stderr_lines: list[str] = field(default_factory=list)

    # Additional fields for specific event types
    data: Optional[dict] = None  # For TOOLS_INIT event
    subtype: Optional[str] = None  # For SYSTEM events

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "type": self.type.value if isinstance(self.type, SDKEventType) else self.type,
            "content": self.content,
            "metadata": self.metadata,
        }

        if self.session_id is not None:
            result["session_id"] = self.session_id

        if self.tool_name is not None:
            result["tool_name"] = self.tool_name

        if self.error_type is not None:
            result["error_type"] = self.error_type

        if self.session_corrupted:
            result["session_corrupted"] = self.session_corrupted

        if self.stderr_lines:
            result["stderr_lines"] = self.stderr_lines

        if self.data is not None:
            result["data"] = self.data

        if self.subtype is not None:
            result["subtype"] = self.subtype

        return result


@dataclass
class SDKConfig:
    """
    Configuration for SDK adapters.

    Populated from environment variables when the adapter is initialized.
    """
    adapter_id: str  # Full adapter ID (e.g., "claude-code/anthropic", "google-adk-wr/gemini")
    adapter_type: str  # Adapter type prefix (e.g., "claude-code", "google-adk-wr")
    provider: str  # Provider suffix (e.g., "anthropic", "minimax", "gemini")
    workspace_dir: str
    permission_mode: str = "acceptEdits"
    settings_file: Optional[str] = None
    model: Optional[str] = None

    @classmethod
    def from_env(cls, mode: str) -> "SDKConfig":
        """
        Create SDKConfig from environment variables.

        Environment variables:
        - SDK_ADAPTER_BUILDING: Adapter ID for building mode (e.g., "claude-code/anthropic")
        - SDK_ADAPTER_CONVERSATION: Adapter ID for conversation mode
        - CLAUDE_CODE_WORKSPACE: Workspace directory
        - CLAUDE_CODE_PERMISSION_MODE: Permission mode

        Args:
            mode: "building" or "conversation"

        Returns:
            SDKConfig instance
        """
        # Get adapter ID based on mode
        env_key = f"SDK_ADAPTER_{mode.upper()}"
        adapter_id = os.getenv(env_key, "claude-code/anthropic")

        # Parse adapter type and provider
        if "/" in adapter_id:
            adapter_type, provider = adapter_id.split("/", 1)
        else:
            adapter_type = adapter_id
            provider = "default"

        workspace_dir = os.getenv("CLAUDE_CODE_WORKSPACE", "/app/workspace")
        permission_mode = os.getenv("CLAUDE_CODE_PERMISSION_MODE", "acceptEdits")

        return cls(
            adapter_id=adapter_id,
            adapter_type=adapter_type,
            provider=provider,
            workspace_dir=workspace_dir,
            permission_mode=permission_mode,
        )


class BaseSDKAdapter(ABC):
    """
    Abstract base class for SDK adapters.

    All SDK adapters must implement this interface to ensure
    consistent behavior and event format.
    """

    # Class-level adapter info
    ADAPTER_TYPE: str = "base"  # Override in subclasses (e.g., "claude-code", "google-adk-wr")
    SUPPORTED_PROVIDERS: list[str] = []  # Override in subclasses

    def __init__(self, config: SDKConfig):
        """
        Initialize the adapter with configuration.

        Args:
            config: SDKConfig instance with adapter settings
        """
        self.config = config
        self.workspace_dir = config.workspace_dir
        self.permission_mode = config.permission_mode

    @abstractmethod
    async def send_message_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
        backend_session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        mode: str = "conversation",
    ) -> AsyncIterator[SDKEvent]:
        """
        Send a message and stream responses as SDKEvents.

        All adapters must yield SDKEvent objects that follow the
        unified event format.

        Args:
            message: User message to send
            session_id: External session ID for resumption
            backend_session_id: Backend session ID for tracking
            system_prompt: Optional custom system prompt
            mode: "building" or "conversation"

        Yields:
            SDKEvent objects representing adapter responses
        """
        pass

    @abstractmethod
    async def interrupt_session(self, session_id: str) -> bool:
        """
        Interrupt an active session.

        Args:
            session_id: Session ID to interrupt

        Returns:
            True if interrupt was successful
        """
        pass

    @classmethod
    def get_adapter_id(cls, provider: str) -> str:
        """Get full adapter ID for a provider."""
        return f"{cls.ADAPTER_TYPE}/{provider}"

    @classmethod
    def supports_provider(cls, provider: str) -> bool:
        """Check if this adapter supports a given provider."""
        return provider in cls.SUPPORTED_PROVIDERS


class AdapterRegistry:
    """
    Registry for SDK adapters.

    Maps adapter type prefixes to adapter classes for dynamic instantiation.
    """

    _adapters: dict[str, Type[BaseSDKAdapter]] = {}

    @classmethod
    def register(cls, adapter_class: Type[BaseSDKAdapter]) -> Type[BaseSDKAdapter]:
        """
        Register an adapter class.

        Can be used as a decorator:

        @AdapterRegistry.register
        class MyAdapter(BaseSDKAdapter):
            ADAPTER_TYPE = "my-adapter"
            ...
        """
        cls._adapters[adapter_class.ADAPTER_TYPE] = adapter_class
        logger.info(f"Registered SDK adapter: {adapter_class.ADAPTER_TYPE}")
        return adapter_class

    @classmethod
    def get_adapter_class(cls, adapter_type: str) -> Optional[Type[BaseSDKAdapter]]:
        """
        Get adapter class by type prefix.

        Args:
            adapter_type: Adapter type (e.g., "claude-code", "google-adk-wr")

        Returns:
            Adapter class or None if not found
        """
        return cls._adapters.get(adapter_type)

    @classmethod
    def create_adapter(cls, config: SDKConfig) -> Optional[BaseSDKAdapter]:
        """
        Create an adapter instance from config.

        Args:
            config: SDKConfig with adapter settings

        Returns:
            Adapter instance or None if adapter type not found
        """
        adapter_class = cls.get_adapter_class(config.adapter_type)
        if adapter_class is None:
            logger.error(f"Unknown adapter type: {config.adapter_type}")
            return None

        if not adapter_class.supports_provider(config.provider):
            logger.warning(
                f"Adapter {config.adapter_type} may not fully support provider {config.provider}. "
                f"Supported providers: {adapter_class.SUPPORTED_PROVIDERS}"
            )

        return adapter_class(config)

    @classmethod
    def list_adapters(cls) -> list[str]:
        """List all registered adapter types."""
        return list(cls._adapters.keys())
