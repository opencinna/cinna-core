"""
Claude Code SDK Adapter

This adapter handles all claude-code/* variants:
- claude-code/anthropic: Default Anthropic Claude
- claude-code/minimax: MiniMax M2 (Anthropic-compatible API)

The adapter converts Claude SDK messages to the unified SDKEvent format.
"""

import os
import logging
import asyncio
import json
from typing import AsyncIterator, Optional
from pathlib import Path
from collections import deque

from .base import (
    BaseSDKAdapter,
    SDKEvent,
    SDKEventType,
    SDKConfig,
    AdapterRegistry,
)
from ..prompt_generator import PromptGenerator
from ..sdk_utils import SessionLogger, format_message_for_debug
from ..active_session_manager import active_session_manager
from ..agent_env_service import AgentEnvService

logger = logging.getLogger(__name__)

# Separate logger for CLI stderr
cli_stderr_logger = logging.getLogger(f"{__name__}.cli_stderr")

# Lock to serialize SDK sessions
_sdk_session_lock = asyncio.Lock()

# Global session tracking
_current_sdk_session_id: str | None = None
_backend_session_map: dict[str, str] = {}


def get_current_sdk_session_id() -> str | None:
    """Get the current SDK session ID."""
    return _current_sdk_session_id


def set_current_sdk_session_id(sdk_session_id: str) -> None:
    """Store the current SDK session ID globally."""
    global _current_sdk_session_id
    _current_sdk_session_id = sdk_session_id
    logger.info(f"Set current SDK session ID: {sdk_session_id}")


def clear_current_sdk_session_id() -> None:
    """Clear the current SDK session ID."""
    global _current_sdk_session_id
    _current_sdk_session_id = None
    logger.info("Cleared current SDK session ID")


def get_backend_session_id(sdk_session_id: str | None = None) -> str | None:
    """Get the backend session ID for a given SDK session ID."""
    if not sdk_session_id:
        sdk_session_id = get_current_sdk_session_id()
    if not sdk_session_id:
        return None
    return _backend_session_map.get(sdk_session_id)


def set_backend_session_id(sdk_session_id: str, backend_session_id: str) -> None:
    """Store the backend session ID for a given SDK session ID."""
    _backend_session_map[sdk_session_id] = backend_session_id
    logger.info(f"Mapped SDK session {sdk_session_id} -> backend session {backend_session_id}")


def clear_backend_session_id(sdk_session_id: str) -> None:
    """Remove the backend session ID mapping."""
    if sdk_session_id in _backend_session_map:
        del _backend_session_map[sdk_session_id]
        logger.info(f"Cleared backend session mapping for SDK session {sdk_session_id}")


class CLIStderrCapture:
    """Captures stderr output from the Claude CLI subprocess."""

    def __init__(self, max_lines: int = 100):
        self._buffer: deque[str] = deque(maxlen=max_lines)
        self._line_count = 0

    def __call__(self, line: str) -> None:
        """Callback invoked by SDK for each stderr line."""
        self._line_count += 1
        self._buffer.append(line)
        cli_stderr_logger.error(f"[Claude CLI] {line}")

    def get_recent_lines(self, count: int = 50) -> list[str]:
        """Get the most recent stderr lines."""
        lines = list(self._buffer)
        return lines[-count:] if len(lines) > count else lines

    def get_all_lines(self) -> list[str]:
        """Get all captured stderr lines."""
        return list(self._buffer)

    def get_summary(self) -> str:
        """Get a summary string of captured stderr for error messages."""
        lines = self.get_recent_lines(20)
        if not lines:
            return "No stderr output captured"

        summary = f"Last {len(lines)} stderr lines (total: {self._line_count}):\n"
        summary += "\n".join(f"  {line}" for line in lines)
        return summary

    @property
    def line_count(self) -> int:
        """Total number of stderr lines received."""
        return self._line_count


@AdapterRegistry.register
class ClaudeCodeAdapter(BaseSDKAdapter):
    """
    Claude Code SDK adapter for all claude-code/* variants.

    Supports:
    - claude-code/anthropic: Default Anthropic Claude
    - claude-code/minimax: MiniMax M2 (Anthropic-compatible API)
    """

    ADAPTER_TYPE = "claude-code"
    SUPPORTED_PROVIDERS = ["anthropic", "minimax"]

    def __init__(self, config: SDKConfig):
        super().__init__(config)

        # Initialize prompt generator
        self.prompt_generator = PromptGenerator(self.workspace_dir)

        # Initialize session logger
        dump_llm_session = os.getenv("DUMP_LLM_SESSION", "false").lower() == "true"
        logs_dir = Path(self.workspace_dir) / "logs"
        self.session_logger = SessionLogger(logs_dir, dump_enabled=dump_llm_session)

        # Initialize agent env service for plugin management
        self.agent_env_service = AgentEnvService(self.workspace_dir)

    async def send_message_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
        backend_session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        mode: str = "conversation",
    ) -> AsyncIterator[SDKEvent]:
        """
        Send message to Claude SDK and stream responses as SDKEvents.

        Args:
            message: User message
            session_id: External SDK session ID to resume
            backend_session_id: Backend session ID for tracking
            system_prompt: Custom system prompt
            mode: "building" or "conversation"

        Yields:
            SDKEvent objects
        """
        async with _sdk_session_lock:
            logger.info(f"Acquired SDK session lock for message: {message[:50]}...")

            session_log_file = self.session_logger.init_session_log(message, session_id)

            try:
                # Import SDK
                from claude_agent_sdk import (
                    ClaudeSDKClient,
                    ClaudeAgentOptions,
                    ResultMessage,
                    create_sdk_mcp_server,
                )

                # Build pre-allowed tools
                pre_allowed_tools = [
                    "Read", "Edit", "Glob", "Grep", "Bash", "Write",
                    "WebFetch", "WebSearch", "TodoWrite"
                ]

                # Get user-approved allowed tools
                user_allowed_tools = self.agent_env_service.get_allowed_tools()
                logger.info(f"User-approved allowed tools: {user_allowed_tools}")

                # Merge tools
                all_allowed_tools = list(set(pre_allowed_tools + user_allowed_tools))
                logger.info(f"Merged allowed tools: {len(all_allowed_tools)} tools")

                # Create stderr capture
                stderr_capture = CLIStderrCapture(max_lines=200)

                # Build options
                options = ClaudeAgentOptions(
                    allowed_tools=all_allowed_tools,
                    permission_mode=self.permission_mode,
                    cwd=self.workspace_dir,
                    stderr=stderr_capture,
                )

                # Check for SDK settings file (MiniMax, etc.)
                settings_file_name = "building_settings.json" if mode == "building" else "conversation_settings.json"
                settings_file_path = Path("/app/core/.claude") / settings_file_name
                if settings_file_path.exists():
                    options.settings = str(settings_file_path)
                    logger.info(f"Using SDK settings file: {settings_file_path}")

                # Add custom tools for building mode
                if mode == "building":
                    try:
                        from ..tools.knowledge_query import query_integration_knowledge

                        knowledge_server = create_sdk_mcp_server(
                            name="knowledge",
                            version="1.0.0",
                            tools=[query_integration_knowledge]
                        )
                        options.mcp_servers = {"knowledge": knowledge_server}
                        options.allowed_tools.append("mcp__knowledge__query_integration_knowledge")
                        logger.info("Added knowledge query tool for building mode")
                    except ImportError as e:
                        logger.warning(f"Could not import knowledge query tool: {e}")
                    except Exception as e:
                        logger.warning(f"Could not setup knowledge query tool: {e}")

                # Add custom tools for conversation mode
                if mode == "conversation":
                    try:
                        from ..tools.agent_handover import agent_handover

                        handover_server = create_sdk_mcp_server(
                            name="handover",
                            version="1.0.0",
                            tools=[agent_handover]
                        )
                        if options.mcp_servers:
                            options.mcp_servers["handover"] = handover_server
                        else:
                            options.mcp_servers = {"handover": handover_server}
                        options.allowed_tools.append("mcp__handover__agent_handover")
                        logger.info("Added agent handover tool for conversation mode")
                    except ImportError as e:
                        logger.warning(f"Could not import agent handover tool: {e}")
                    except Exception as e:
                        logger.warning(f"Could not setup agent handover tool: {e}")

                # Load plugins for current mode
                try:
                    active_plugins = self.agent_env_service.get_active_plugins_for_mode(mode)
                    if active_plugins:
                        plugins = [
                            {"type": "local", "path": plugin["path"]}
                            for plugin in active_plugins
                        ]
                        options.plugins = plugins
                        logger.info(f"Loaded {len(plugins)} plugins for {mode} mode")
                except Exception as e:
                    logger.warning(f"Could not load plugins: {e}")

                # Set model based on mode
                if mode == "conversation":
                    options.model = "haiku"
                    logger.info("Using Haiku model for conversation mode")

                # Set system prompt
                if system_prompt:
                    options.system_prompt = system_prompt
                    logger.info("Using explicit system_prompt override")
                else:
                    try:
                        options.system_prompt = self.prompt_generator.generate_prompt(mode)
                        logger.info(f"Generated system prompt for mode: {mode}")
                    except ValueError as e:
                        yield SDKEvent(
                            type=SDKEventType.ERROR,
                            content=str(e),
                            error_type="ValueError",
                        )
                        return

                # Set resume parameter
                if session_id:
                    logger.info(f"Creating SDK client to resume session: {session_id}")
                    options.resume = session_id
                else:
                    logger.info("Creating new SDK client session")

                # Create client and connect
                client = ClaudeSDKClient(options=options)
                await client.connect()

                current_session_id = session_id

                # Register session for interrupt support
                if current_session_id:
                    await active_session_manager.register_session(current_session_id, client)
                    set_current_sdk_session_id(current_session_id)
                    logger.info(f"Registered session {current_session_id} for interrupt support")

                    if backend_session_id:
                        set_backend_session_id(current_session_id, backend_session_id)

                pending_backend_session_id = backend_session_id if not current_session_id else None

                # Emit session_created for new sessions
                if not session_id:
                    yield SDKEvent(
                        type=SDKEventType.SESSION_CREATED,
                        session_id=None,
                        content="",
                    )

                # Emit tools_init event
                yield SDKEvent(
                    type=SDKEventType.SYSTEM,
                    subtype="tools_init",
                    content="",
                    data={"tools": options.allowed_tools.copy() if options.allowed_tools else []},
                )

                # Send the message
                logger.info(f"Sending query to SDK client: {message[:50]}...")
                try:
                    await client.query(message)
                    logger.info("Query sent successfully")
                except Exception as e:
                    error_msg = str(e)
                    if "No conversation found" in error_msg or "Cannot write to terminated process" in error_msg:
                        logger.warning(f"Session {current_session_id} appears corrupted")
                        if stderr_capture.line_count > 0:
                            logger.warning(f"CLI stderr:\n{stderr_capture.get_summary()}")

                        try:
                            await client.disconnect()
                        except Exception:
                            pass

                        if current_session_id:
                            await active_session_manager.unregister_session(current_session_id)

                        yield SDKEvent(
                            type=SDKEventType.ERROR,
                            content="Previous session was corrupted. Please send your message again.",
                            error_type="CorruptedSession",
                            session_corrupted=True,
                            stderr_lines=stderr_capture.get_recent_lines(10),
                        )
                        return

                    logger.error(f"Error sending query: {e}", exc_info=True)
                    raise

                # Stream responses
                logger.info("Starting to iterate over receive_messages()...")
                message_count = 0
                interrupt_initiated = False
                interrupt_event_yielded = False

                try:
                    async for message_obj in client.receive_messages():
                        message_count += 1

                        # Extract session_id if not yet captured
                        if not current_session_id:
                            extracted_session_id = self._extract_session_id(message_obj)
                            if extracted_session_id:
                                current_session_id = extracted_session_id
                                logger.info(f"✅ Captured session_id: {current_session_id}")

                                await active_session_manager.register_session(current_session_id, client)
                                set_current_sdk_session_id(current_session_id)

                                if pending_backend_session_id:
                                    set_backend_session_id(current_session_id, pending_backend_session_id)

                                yield SDKEvent(
                                    type=SDKEventType.SESSION_CREATED,
                                    session_id=current_session_id,
                                    content="",
                                )

                        # Check for interrupt requests
                        if current_session_id and not interrupt_initiated:
                            if await active_session_manager.check_interrupt_requested(current_session_id):
                                logger.info(f"Interrupt detected for session {current_session_id}")
                                try:
                                    await client.interrupt()
                                    interrupt_initiated = True
                                    logger.info("SDK interrupt() called successfully")
                                except Exception as e:
                                    logger.error(f"Failed to interrupt: {e}", exc_info=True)
                                    yield SDKEvent(
                                        type=SDKEventType.ERROR,
                                        content=f"Failed to interrupt session: {str(e)}",
                                        error_type="InterruptError",
                                        session_id=current_session_id,
                                    )
                                    break

                        # Dump message to log
                        self.session_logger.dump_message(session_log_file, message_obj, message_count)

                        # Format message to SDKEvent
                        event = self._format_message(message_obj, current_session_id, interrupt_initiated)

                        if event is not None:
                            yield event

                        # Stop at ResultMessage
                        if isinstance(message_obj, ResultMessage):
                            logger.info("Received ResultMessage, stopping")
                            break

                    logger.info(f"Finished. Total messages: {message_count}")
                    self.session_logger.complete_session_log(session_log_file, message_count)

                except Exception as e:
                    error_msg = str(e)
                    if "exit code -9" in error_msg or "exit code: -9" in error_msg:
                        if interrupt_initiated:
                            logger.info("Expected exit code -9 after interrupt()")
                            if not interrupt_event_yielded:
                                yield SDKEvent(
                                    type=SDKEventType.INTERRUPTED,
                                    content="Message interrupted by user",
                                    session_id=current_session_id,
                                )
                                interrupt_event_yielded = True
                        else:
                            logger.error(f"Session died with SIGKILL")
                            logger.error(f"CLI stderr:\n{stderr_capture.get_summary()}")
                            yield SDKEvent(
                                type=SDKEventType.ERROR,
                                content="Session was corrupted. Please try again.",
                                error_type="CorruptedSession",
                                session_corrupted=True,
                                stderr_lines=stderr_capture.get_recent_lines(10),
                            )
                    else:
                        logger.error(f"Error during receive_messages: {e}", exc_info=True)
                        if stderr_capture.line_count > 0:
                            logger.error(f"CLI stderr:\n{stderr_capture.get_summary()}")
                        raise

                finally:
                    # Cleanup
                    if current_session_id:
                        await active_session_manager.unregister_session(current_session_id)
                        clear_backend_session_id(current_session_id)

                    clear_current_sdk_session_id()

                    try:
                        await client.disconnect()
                        logger.info("SDK client disconnected")
                    except Exception as e:
                        logger.error(f"Error disconnecting: {e}", exc_info=True)

            except ImportError as e:
                logger.error(f"Claude SDK not available: {e}")
                yield SDKEvent(
                    type=SDKEventType.ERROR,
                    content="Claude Code SDK is not installed",
                    error_type="ImportError",
                )

            except Exception as e:
                logger.error(f"SDK error: {type(e).__name__}: {e}", exc_info=True)
                if 'stderr_capture' in locals() and stderr_capture.line_count > 0:
                    logger.error(f"CLI stderr:\n{stderr_capture.get_summary()}")
                yield SDKEvent(
                    type=SDKEventType.ERROR,
                    content=f"SDK error: {str(e)}",
                    error_type=type(e).__name__,
                    stderr_lines=stderr_capture.get_recent_lines(10) if 'stderr_capture' in locals() else [],
                )

    async def interrupt_session(self, session_id: str) -> bool:
        """Interrupt an active session."""
        return await active_session_manager.request_interrupt(session_id)

    def _extract_session_id(self, message_obj) -> Optional[str]:
        """Extract session_id from SDK message object."""
        extracted_session_id = None

        # Try data.session_id
        if hasattr(message_obj, 'data'):
            data = message_obj.data
            if hasattr(data, 'session_id'):
                extracted_session_id = data.session_id
            elif isinstance(data, dict) and data.get('session_id'):
                extracted_session_id = data['session_id']

        # Try session_id attribute
        elif hasattr(message_obj, 'session_id'):
            extracted_session_id = message_obj.session_id

        return extracted_session_id

    def _format_message(
        self,
        message_obj,
        session_id: str,
        interrupt_initiated: bool = False
    ) -> Optional[SDKEvent]:
        """
        Format Claude SDK message to unified SDKEvent.

        Args:
            message_obj: Claude SDK message object
            session_id: Current session ID
            interrupt_initiated: Whether interrupt was called

        Returns:
            SDKEvent or None to skip the message
        """
        from claude_agent_sdk import (
            AssistantMessage,
            TextBlock,
            ToolUseBlock,
            ToolResultBlock,
            ResultMessage,
            ThinkingBlock,
            SystemMessage,
            UserMessage,
        )

        # Handle SystemMessage
        if isinstance(message_obj, SystemMessage):
            if message_obj.subtype == "init":
                # Skip init messages
                return None
            else:
                return SDKEvent(
                    type=SDKEventType.SYSTEM,
                    content=f"System: {message_obj.subtype}",
                    session_id=session_id,
                    metadata={"subtype": message_obj.subtype},
                )

        # Handle AssistantMessage
        elif isinstance(message_obj, AssistantMessage):
            content_parts = []
            event_type = SDKEventType.ASSISTANT

            for block in message_obj.content:
                if isinstance(block, TextBlock):
                    content_parts.append(block.text)

                elif isinstance(block, ThinkingBlock):
                    event_type = SDKEventType.THINKING
                    content_parts.append(f"[Thinking] {block.thinking}")

                elif isinstance(block, ToolUseBlock):
                    # Return tool use as separate event
                    tool_input_str = ""
                    if block.input:
                        try:
                            input_json = json.dumps(block.input, indent=2)
                            if len(input_json) > 200:
                                input_json = input_json[:200] + "..."
                            tool_input_str = f"\nInput: {input_json}"
                        except Exception:
                            tool_input_str = f"\nInput: {str(block.input)[:200]}"

                    return SDKEvent(
                        type=SDKEventType.TOOL_USE,
                        tool_name=block.name,
                        content=f"🔧 Using tool: {block.name}{tool_input_str}",
                        session_id=session_id,
                        metadata={
                            "tool_id": block.id,
                            "tool_input": block.input,
                        },
                    )

                elif isinstance(block, ToolResultBlock):
                    # Skip tool results
                    continue

            content = "\n".join(content_parts) if content_parts else ""

            metadata = {}
            if hasattr(message_obj, "model"):
                metadata["model"] = message_obj.model

            return SDKEvent(
                type=event_type,
                content=content,
                session_id=session_id,
                metadata=metadata,
            )

        # Handle ResultMessage
        elif isinstance(message_obj, ResultMessage):
            is_interrupted = interrupt_initiated and message_obj.subtype == "error_during_execution"

            if is_interrupted:
                logger.info(f"Detected interrupted session from ResultMessage")

            return SDKEvent(
                type=SDKEventType.INTERRUPTED if is_interrupted else SDKEventType.DONE,
                content="Request interrupted by user" if is_interrupted else "",
                session_id=session_id,
                metadata={
                    "subtype": message_obj.subtype,
                    "duration_ms": message_obj.duration_ms,
                    "is_error": message_obj.is_error,
                    "num_turns": message_obj.num_turns,
                    "total_cost_usd": message_obj.total_cost_usd,
                    "session_id": message_obj.session_id,
                },
            )

        # Handle UserMessage (interrupt notifications)
        elif isinstance(message_obj, UserMessage):
            content_str = str(message_obj.content)
            if "[Request interrupted by user" in content_str:
                return SDKEvent(
                    type=SDKEventType.SYSTEM,
                    content="⚠️ Request interrupted by user",
                    session_id=session_id,
                    metadata={"interrupt_notification": True},
                )
            else:
                # Skip other user messages
                return None

        else:
            logger.warning(f"Unknown message type: {type(message_obj)}")
            return None
