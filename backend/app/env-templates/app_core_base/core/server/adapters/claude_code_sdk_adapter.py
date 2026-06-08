"""
Claude Code SDK Adapter

This adapter handles all claude-code/* variants:
- claude-code/anthropic: Default Anthropic Claude
- claude-code/minimax: MiniMax M2 (Anthropic-compatible API)

The adapter orchestrates the Claude Agent SDK subprocess lifecycle —
session creation, message sending, interrupt handling, and cleanup.
Event translation is delegated to ClaudeCodeEventTransformer.
"""

import os
import logging
import asyncio
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
from .claude_code_event_transformer import ClaudeCodeEventTransformer
from ..prompt_generator import PromptGenerator
from ..sdk_utils import SessionEventLogger, format_message_for_debug
from ..active_session_manager import active_session_manager
from ..agent_env_service import AgentEnvService
from .tool_name_registry import normalize_tool_name

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

        # Initialize event logger (shared JSONL format, same as OpenCode)
        dump_llm_session = os.getenv("DUMP_LLM_SESSION", "false").lower() == "true"
        logs_dir = Path(self.workspace_dir) / "logs"
        self.event_logger = SessionEventLogger(
            logs_dir, prefix="claude_code_session", enabled=dump_llm_session,
        )

        # Initialize agent env service for plugin management
        self.agent_env_service = AgentEnvService(self.workspace_dir)

        # Event transformer — translates raw Claude SDK messages to SDKEvents
        self._event_transformer = ClaudeCodeEventTransformer()

    async def send_message_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
        backend_session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        mode: str = "conversation",
        session_state: Optional[dict] = None,
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

            # Rotate log file for new sessions so each session gets its own file
            if not session_id:
                self.event_logger.rotate()

            self.event_logger.log_send("query", {
                "session_id": session_id,
                "message": message,
                "mode": mode,
            })

            try:
                # Import SDK
                from claude_agent_sdk import (
                    ClaudeSDKClient,
                    ClaudeAgentOptions,
                    ResultMessage,
                    create_sdk_mcp_server,
                )

                # Build pre-allowed tools — PascalCase is required by the Claude
                # SDK config. Normalized to lowercase at tools_init emission.
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
                    setting_sources=["user", "project", "local"]  # Load all settings
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
                        from ..tools.agent_task_add_comment import agent_task_add_comment
                        from ..tools.agent_task_update_status import agent_task_update_status
                        from ..tools.agent_task_create_task import agent_task_create_task
                        from ..tools.agent_task_create_subtask import agent_task_create_subtask
                        from ..tools.agent_task_get_details import agent_task_get_details
                        from ..tools.agent_task_list_tasks import agent_task_list_tasks

                        agent_task_tools = [
                            agent_task_add_comment,
                            agent_task_update_status,
                            agent_task_create_task,
                            agent_task_create_subtask,
                            agent_task_get_details,
                            agent_task_list_tasks,
                        ]

                        agent_task_server = create_sdk_mcp_server(
                            name="agent_task",
                            version="1.0.0",
                            tools=agent_task_tools
                        )
                        if options.mcp_servers:
                            options.mcp_servers["agent_task"] = agent_task_server
                        else:
                            options.mcp_servers = {"agent_task": agent_task_server}
                        options.allowed_tools.append("mcp__agent_task__add_comment")
                        options.allowed_tools.append("mcp__agent_task__update_status")
                        options.allowed_tools.append("mcp__agent_task__create_task")
                        options.allowed_tools.append("mcp__agent_task__create_subtask")
                        options.allowed_tools.append("mcp__agent_task__get_details")
                        options.allowed_tools.append("mcp__agent_task__list_tasks")
                        logger.info(
                            "Added agent task tools (add_comment, update_status, "
                            "create_task, create_subtask, get_details, list_tasks) "
                            "for conversation mode"
                        )
                    except ImportError as e:
                        logger.warning(f"Could not import agent task tools: {e}")
                    except Exception as e:
                        logger.warning(f"Could not setup agent task tools: {e}")

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

                # Merge credential-derived MCP-provider servers (RD-5). Each is
                # a remote http/sse MCP server keyed cinna_mcp_<credential_id>,
                # so it never collides with the knowledge / agent_task SDK
                # servers above. The bearer token (if any) rides in headers — the
                # only place an mcp_provider token reaches the container.
                try:
                    user_mcp = self.agent_env_service.get_user_mcp_servers_for_mode(
                        mode, engine="claude_code"
                    )
                    if user_mcp:
                        if options.mcp_servers:
                            options.mcp_servers.update(user_mcp)
                        else:
                            options.mcp_servers = dict(user_mcp)
                        # Allow the agent to call every tool these servers expose.
                        for server_key in user_mcp:
                            options.allowed_tools.append(f"mcp__{server_key}")
                        logger.info(
                            f"Merged {len(user_mcp)} MCP-provider server(s) "
                            f"for {mode} mode"
                        )
                except Exception as e:
                    logger.warning(f"Could not load MCP-provider servers: {e}")

                # Set model from the per-mode MODEL_<MODE> env var, which the
                # backend computes from the central model catalog (honoring any
                # model_override_*). For claude-code/anthropic with no override
                # this is a tier word (haiku/sonnet) the CLI auto-resolves; for
                # minimax or an explicit override it's a concrete model id.
                #
                # Fallbacks when the env var is unset (e.g. an old environment
                # generated before this var existed):
                #   - conversation: fall back to "haiku" (prior behavior)
                #   - building: leave unset so the SDK uses its default
                model_env_var = f"MODEL_{mode.upper()}"
                model_value = (os.getenv(model_env_var) or "").strip()
                if model_value:
                    options.model = model_value
                    logger.info(f"Using model '{model_value}' for {mode} mode (from {model_env_var})")
                elif mode == "conversation":
                    options.model = "haiku"
                    logger.info("Using fallback Haiku model for conversation mode (no MODEL_CONVERSATION set)")
                else:
                    logger.info("No MODEL_BUILDING set; using SDK default model for building mode")

                # Set system prompt
                if system_prompt:
                    options.system_prompt = system_prompt
                    logger.info("Using explicit system_prompt override")
                else:
                    try:
                        options.system_prompt = self.prompt_generator.generate_prompt(
                            mode, session_state=session_state
                        )
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

                # Emit tools_init event with unified lowercase tool names
                unified_tools = [
                    normalize_tool_name(t, sdk="claude-code")
                    for t in (options.allowed_tools or [])
                ]
                yield SDKEvent(
                    type=SDKEventType.SYSTEM,
                    subtype="tools_init",
                    content="",
                    data={"tools": unified_tools},
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

                        # Log message as JSONL recv event
                        self.event_logger.log_recv({
                            "type": type(message_obj).__name__,
                            "data": format_message_for_debug(message_obj),
                        })

                        # Translate message via event transformer
                        event = self._event_transformer.translate(
                            message_obj, current_session_id, interrupt_initiated
                        )

                        if event is not None:
                            yield event

                        # Stop at ResultMessage
                        if isinstance(message_obj, ResultMessage):
                            logger.info("Received ResultMessage, stopping")
                            break

                    logger.info(f"Finished. Total messages: {message_count}")
                    self.event_logger.log_send("session_complete", {
                        "session_id": current_session_id,
                        "message_count": message_count,
                    })

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
