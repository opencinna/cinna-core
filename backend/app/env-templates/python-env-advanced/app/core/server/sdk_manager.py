import os
import logging
import asyncio
import contextvars
from typing import AsyncIterator, Optional
from pathlib import Path
from collections import deque

from .prompt_generator import PromptGenerator
from .sdk_utils import SessionLogger, format_message_for_debug, format_sdk_message
from .active_session_manager import active_session_manager
from .agent_env_service import AgentEnvService

logger = logging.getLogger(__name__)

# Separate logger for CLI stderr to allow different log levels/handlers
cli_stderr_logger = logging.getLogger(f"{__name__}.cli_stderr")


class CLIStderrCapture:
    """
    Captures stderr output from the Claude CLI subprocess.

    Logs each line to the app logger and maintains a rolling buffer
    of recent lines for inclusion in error messages.
    """

    def __init__(self, max_lines: int = 100):
        self._buffer: deque[str] = deque(maxlen=max_lines)
        self._line_count = 0

    def __call__(self, line: str) -> None:
        """Callback invoked by SDK for each stderr line."""
        self._line_count += 1
        self._buffer.append(line)
        # Log each stderr line - use error level to ensure visibility
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

# Context variable to track current SDK session ID (for SDK operations like interrupts)
current_session_context: contextvars.ContextVar[str | None] = contextvars.ContextVar('sdk_session_id', default=None)

# Lock to serialize SDK sessions in this agent environment
# This prevents race conditions when multiple concurrent requests arrive
_sdk_session_lock = asyncio.Lock()

# Global variable to track the current SDK session ID
# This is used by tools since they execute in new async contexts that don't inherit context vars
# Protected by _sdk_session_lock to prevent race conditions from concurrent requests
_current_sdk_session_id: str | None = None

# Global dictionary to track backend session IDs by SDK session ID
# This is used instead of a context var because tools execute in new async contexts
# that don't inherit context vars from the parent
# Protected by _sdk_session_lock to prevent race conditions from concurrent requests
_backend_session_map: dict[str, str] = {}


def get_current_sdk_session_id() -> str | None:
    """
    Get the current SDK session ID.

    This is used by tools to retrieve the SDK session ID even when executing
    in a different async context where context vars are not available.

    Safe because each agent environment handles only one SDK session at a time.
    """
    return _current_sdk_session_id


def set_current_sdk_session_id(sdk_session_id: str) -> None:
    """
    Store the current SDK session ID globally.

    This allows tools to access the SDK session ID even when executing
    in a different async context.
    """
    global _current_sdk_session_id
    _current_sdk_session_id = sdk_session_id
    logger.info(f"Set current SDK session ID: {sdk_session_id}")


def clear_current_sdk_session_id() -> None:
    """
    Clear the current SDK session ID when a session ends.
    """
    global _current_sdk_session_id
    _current_sdk_session_id = None
    logger.info("Cleared current SDK session ID")


def get_backend_session_id(sdk_session_id: str | None = None) -> str | None:
    """
    Get the backend session ID for a given SDK session ID.

    If sdk_session_id is not provided, uses the current SDK session ID.

    This is used by tools (like agent_handover) to retrieve the backend session ID.
    """
    if not sdk_session_id:
        sdk_session_id = get_current_sdk_session_id()
    if not sdk_session_id:
        return None
    return _backend_session_map.get(sdk_session_id)


def set_backend_session_id(sdk_session_id: str, backend_session_id: str) -> None:
    """
    Store the backend session ID for a given SDK session ID.

    This allows tools to look up the backend session ID even when executing
    in a different async context.
    """
    _backend_session_map[sdk_session_id] = backend_session_id
    logger.info(f"Mapped SDK session {sdk_session_id} -> backend session {backend_session_id}")


def clear_backend_session_id(sdk_session_id: str) -> None:
    """
    Remove the backend session ID mapping when a session ends.
    """
    if sdk_session_id in _backend_session_map:
        del _backend_session_map[sdk_session_id]
        logger.info(f"Cleared backend session mapping for SDK session {sdk_session_id}")


class ClaudeCodeSDKManager:
    """
    Manages Claude Code SDK sessions for both building and conversation modes.

    Responsibilities:
    - Initialize SDK with workspace and API key
    - Create and resume SDK sessions using ClaudeSDKClient
    - Stream responses from SDK
    - Handle SDK errors
    - Coordinate with PromptGenerator for system prompts
    - Coordinate with SessionLogger for debugging
    """

    def __init__(self):
        self.workspace_dir = os.getenv("CLAUDE_CODE_WORKSPACE", "/app/app")
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.permission_mode = os.getenv("CLAUDE_CODE_PERMISSION_MODE", "acceptEdits")

        # Validate configuration
        if not self.api_key:
            logger.warning("ANTHROPIC_API_KEY not set. SDK will not work.")

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
        agent_sdk: str = "claude",
    ) -> AsyncIterator[dict]:
        """
        Send message to SDK and stream responses.

        Uses ClaudeSDKClient with proper session resumption via ClaudeAgentOptions.

        Args:
            message: User message
            session_id: External SDK session ID to resume (None = create new)
            system_prompt: Custom system prompt for this session (overrides mode-based prompt)
            mode: "building" or "conversation" - determines system prompt structure
            agent_sdk: SDK to use ("claude" is currently the only option)

        Yields:
            Dictionaries with message data:
            {
                "type": "assistant" | "tool" | "result" | "error" | "session_created",
                "content": str,
                "session_id": str (included in all events),
                "tool_name": str (only in tool events),
                "metadata": dict,
            }
        """
        # Validate agent_sdk parameter
        if agent_sdk != "claude":
            yield {
                "type": "error",
                "content": f"Unsupported agent_sdk: {agent_sdk}. Only 'claude' is supported.",
                "error_type": "ValueError",
            }
            return

        # Acquire lock to serialize SDK sessions and prevent race conditions
        # This ensures only one SDK session is active at a time in this agent environment
        async with _sdk_session_lock:
            logger.info(f"Acquired SDK session lock for message: {message[:50]}...")

            # Initialize session log file if dumping is enabled
            session_log_file = self.session_logger.init_session_log(message, session_id)

            try:
                # Import SDK here to avoid import errors if SDK not installed
                from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server

                # Build pre-allowed tools list (always allowed, no user approval needed)
                pre_allowed_tools = ["Read", "Edit", "Glob", "Grep", "Bash", "Write", "WebFetch", "WebSearch", "TodoWrite"]

                # Get user-approved allowed tools from settings.json
                user_allowed_tools = self.agent_env_service.get_allowed_tools()
                logger.info(f"User-approved allowed tools from settings: {user_allowed_tools}")

                # Merge pre-allowed tools with user-approved tools (no duplicates)
                all_allowed_tools = list(set(pre_allowed_tools + user_allowed_tools))
                logger.info(f"Merged allowed tools: {len(all_allowed_tools)} tools")

                # Create stderr capture to log CLI output and collect for error reporting
                stderr_capture = CLIStderrCapture(max_lines=200)

                # Build options
                options = ClaudeAgentOptions(
                    allowed_tools=all_allowed_tools,
                    permission_mode=self.permission_mode,
                    cwd=self.workspace_dir,
                    stderr=stderr_capture,  # Capture CLI stderr output
                )

                # Check for SDK settings file (used for MiniMax and other custom SDK configurations)
                # Settings files are created by backend when environment uses a non-default SDK
                # Files are stored in /app/core/.claude/ which is part of the core directory
                settings_file_name = "building_settings.json" if mode == "building" else "conversation_settings.json"
                settings_file_path = Path("/app/core/.claude") / settings_file_name
                if settings_file_path.exists():
                    options.settings = str(settings_file_path)
                    logger.info(f"Using SDK settings file: {settings_file_path}")

                # Add custom tools for building mode
                if mode == "building":
                    try:
                        # Import the knowledge query tool
                        from .tools.knowledge_query import query_integration_knowledge

                        # Create MCP server with custom tools
                        knowledge_server = create_sdk_mcp_server(
                            name="knowledge",
                            version="1.0.0",
                            tools=[query_integration_knowledge]
                        )

                        # Add to options
                        options.mcp_servers = {"knowledge": knowledge_server}
                        options.allowed_tools.append("mcp__knowledge__query_integration_knowledge")
                        logger.info("Added knowledge query tool for building mode")
                    except ImportError as tool_import_error:
                        logger.warning(f"Could not import knowledge query tool: {tool_import_error}")
                    except Exception as tool_error:
                        logger.warning(f"Could not setup knowledge query tool: {tool_error}")

                # Add custom tools for conversation mode
                if mode == "conversation":
                    try:
                        # Import the agent handover tool
                        from .tools.agent_handover import agent_handover

                        # Create MCP server with custom tools
                        handover_server = create_sdk_mcp_server(
                            name="handover",
                            version="1.0.0",
                            tools=[agent_handover]
                        )

                        # Add to options
                        if options.mcp_servers:
                            options.mcp_servers["handover"] = handover_server
                        else:
                            options.mcp_servers = {"handover": handover_server}
                        options.allowed_tools.append("mcp__handover__agent_handover")
                        logger.info("Added agent handover tool for conversation mode")
                    except ImportError as tool_import_error:
                        logger.warning(f"Could not import agent handover tool: {tool_import_error}")
                    except Exception as tool_error:
                        logger.warning(f"Could not setup agent handover tool: {tool_error}")

                # Load plugins for current mode
                try:
                    active_plugins = self.agent_env_service.get_active_plugins_for_mode(mode)
                    if active_plugins:
                        # Build plugins array for SDK options
                        plugins = [
                            {"type": "local", "path": plugin["path"]}
                            for plugin in active_plugins
                        ]
                        options.plugins = plugins
                        logger.info(f"Loaded {len(plugins)} plugins for {mode} mode: {[p['path'] for p in plugins]}")
                    else:
                        logger.debug(f"No plugins configured for {mode} mode")
                except Exception as plugin_error:
                    logger.warning(f"Could not load plugins for {mode} mode: {plugin_error}")

                # Set model based on mode
                # Conversation mode: use Haiku for faster, cheaper responses
                # Building mode: use default model (Sonnet) for better code generation
                if mode == "conversation":
                    options.model = "haiku"
                    logger.info("Using Haiku model for conversation mode")
                # For building mode, don't set model parameter to use default (Sonnet)

                # Set system prompt based on mode
                if system_prompt:
                    # Explicit system_prompt overrides everything
                    options.system_prompt = system_prompt
                    logger.info("Using explicit system_prompt override")
                else:
                    # Generate prompt based on mode using PromptGenerator
                    try:
                        options.system_prompt = self.prompt_generator.generate_prompt(mode)
                        logger.info(f"Generated system prompt for mode: {mode}")
                    except ValueError as e:
                        yield {
                            "type": "error",
                            "content": str(e),
                            "error_type": "ValueError",
                        }
                        return

                # Set resume parameter if we have a session_id
                if session_id:
                    logger.info(f"Creating SDK client to resume session: {session_id}")
                    options.resume = session_id
                else:
                    logger.info("Creating new SDK client session")

                # Create new client (even when resuming - the SDK handles session continuation)
                client = ClaudeSDKClient(options=options)
                await client.connect()

                # If this is a new session, we'll get the session_id from ResultMessage
                # For now, use a placeholder for tracking
                current_session_id = session_id

                # Register session EARLY for interrupt support
                # This allows interrupts to work even during the slow connect/query phase
                # For resumed sessions, we know the ID upfront and can register immediately
                # For new sessions, we'll register again when we get the real session_id from ResultMessage
                if current_session_id:
                    await active_session_manager.register_session(current_session_id, client)
                    # Set context variable so tools can access the SDK session_id
                    current_session_context.set(current_session_id)
                    # Set global SDK session ID for tools to access
                    set_current_sdk_session_id(current_session_id)
                    logger.info(f"Registered session {current_session_id} for interrupt support")

                    # For resumed sessions, map backend session ID immediately
                    if backend_session_id:
                        set_backend_session_id(current_session_id, backend_session_id)
                        logger.info(f"Mapped backend session {backend_session_id} to resumed SDK session {current_session_id}")

                # Store backend session ID mapping for new sessions (will be set once we extract SDK session ID)
                pending_backend_session_id = backend_session_id if not current_session_id else None

                # Emit session_created event only for new sessions
                if not session_id:
                    # We don't know the real session_id yet - will get it from ResultMessage
                    yield {
                        "type": "session_created",
                        "session_id": None,  # Will be set from ResultMessage
                        "content": "",
                    }

                # Emit tools_init event with all available tools for backend tracking
                # This allows backend to track which tools are available and manage approvals
                yield {
                    "type": "system",
                    "subtype": "tools_init",
                    "content": "",
                    "data": {
                        "tools": options.allowed_tools.copy() if options.allowed_tools else [],
                    },
                }
                logger.info(f"Yielded tools_init event with {len(options.allowed_tools or [])} tools")

                # Send the message
                logger.info(f"Sending query to SDK client: {message[:50]}...")
                try:
                    await client.query(message)
                    logger.info("Query sent successfully, starting to receive responses...")
                except Exception as e:
                    # Check if this is a "No conversation found" error from corrupted session
                    error_msg = str(e)
                    if "No conversation found" in error_msg or "Cannot write to terminated process" in error_msg:
                        logger.warning(f"Session {current_session_id} appears corrupted, will retry without resuming")
                        if stderr_capture.line_count > 0:
                            logger.warning(f"CLI stderr output:\n{stderr_capture.get_summary()}")

                        # Clean up the corrupted client
                        try:
                            await client.disconnect()
                        except Exception:
                            pass  # Ignore disconnect errors for corrupted clients

                        # Unregister the corrupted session
                        if current_session_id:
                            await active_session_manager.unregister_session(current_session_id)

                        # Yield error event indicating session was corrupted
                        yield {
                            "type": "error",
                            "content": "Previous session was corrupted or interrupted. Please send your message again to start a new session.",
                            "error_type": "CorruptedSession",
                            "session_corrupted": True,  # Signal to backend to clear external_session_id
                            "stderr_lines": stderr_capture.get_recent_lines(10),
                        }
                        return

                    logger.error(f"Error sending query to SDK client: {e}", exc_info=True)
                    if stderr_capture.line_count > 0:
                        logger.error(f"CLI stderr output:\n{stderr_capture.get_summary()}")
                    raise

                # Stream responses using receive_messages()
                logger.info("Starting to iterate over receive_messages()...")
                message_count = 0
                interrupt_initiated = False  # Track if we called interrupt()
                interrupt_event_yielded = False  # Track if we've sent interrupted event to backend

                try:
                    async for message_obj in client.receive_messages():
                        message_count += 1

                        # Extract session_id from SystemMessage (first message) if we don't have it yet
                        # Check multiple ways since SDK structure can vary
                        if not current_session_id:
                            extracted_session_id = None

                            # Try data.session_id (SystemMessage - data could be object or dict)
                            if hasattr(message_obj, 'data'):
                                data = message_obj.data
                                # Try as attribute (dataclass/object)
                                if hasattr(data, 'session_id'):
                                    extracted_session_id = data.session_id
                                    logger.info(f"Extracted session_id from message_obj.data.session_id: {extracted_session_id}")
                                # Try as dict key
                                elif isinstance(data, dict) and data.get('session_id'):
                                    extracted_session_id = data['session_id']
                                    logger.info(f"Extracted session_id from message_obj.data['session_id']: {extracted_session_id}")
                            # Try session_id attribute directly (ResultMessage)
                            elif hasattr(message_obj, 'session_id'):
                                extracted_session_id = message_obj.session_id
                                logger.info(f"Extracted session_id from message_obj.session_id: {extracted_session_id}")

                            if extracted_session_id:
                                current_session_id = extracted_session_id
                                logger.info(f"✅ Captured session_id: {current_session_id}")
                                # Register session EARLY so interrupts can be processed
                                await active_session_manager.register_session(current_session_id, client)
                                # Set context variable so tools can access the SDK session_id
                                current_session_context.set(current_session_id)
                                # Set global SDK session ID for tools to access
                                set_current_sdk_session_id(current_session_id)
                                logger.info(f"✅ Registered session {current_session_id} for interrupt support (early)")

                                # Map SDK session ID to backend session ID for tools to access
                                if pending_backend_session_id:
                                    set_backend_session_id(current_session_id, pending_backend_session_id)
                                    logger.info(f"✅ Mapped backend session {pending_backend_session_id} to SDK session {current_session_id}")

                                # Immediately yield session_created event with session_id
                                # This allows backend to forward pending interrupts early
                                yield {
                                    "type": "session_created",
                                    "session_id": current_session_id,
                                    "content": "",
                                }
                                logger.info(f"✅ Yielded session_created event with session_id: {current_session_id}")

                        # Check for interrupt requests (if we have a session_id)
                        # Only check if we haven't already initiated an interrupt
                        if current_session_id and not interrupt_initiated:
                            if await active_session_manager.check_interrupt_requested(current_session_id):
                                logger.info(f"Interrupt detected for session {current_session_id}, calling SDK interrupt()")
                                try:
                                    await client.interrupt()
                                    interrupt_initiated = True  # Mark that we initiated the interrupt
                                    logger.info("SDK interrupt() called successfully - continuing to receive final messages")
                                    # DON'T break here - let SDK send final messages and naturally end
                                    # The SDK will raise exit code -9 when it's done cleaning up
                                except Exception as int_error:
                                    logger.error(f"Failed to interrupt SDK: {int_error}", exc_info=True)
                                    # Yield error event to inform backend
                                    yield {
                                        "type": "error",
                                        "content": f"Failed to interrupt session: {str(int_error)}",
                                        "error_type": "InterruptError",
                                        "session_id": current_session_id,
                                    }
                                    # Don't continue processing - break and let cleanup happen
                                    break

                        # Dump raw message to log file if enabled
                        self.session_logger.dump_message(session_log_file, message_obj, message_count)

                        # Debug: Log raw message from Claude SDK with full structure
                        logger.debug(f"[Claude SDK #{message_count}] ========== RAW MESSAGE ==========")
                        logger.debug(f"[Claude SDK #{message_count}] Type: {type(message_obj).__name__}")
                        logger.debug(f"[Claude SDK #{message_count}] Full structure:\n{format_message_for_debug(message_obj)}")
                        logger.debug(f"[Claude SDK #{message_count}] ===================================")

                        formatted = format_sdk_message(message_obj, current_session_id, interrupt_initiated)

                        # Debug: Log formatted message
                        if formatted is not None:
                            logger.info(f"[Claude SDK #{message_count}] Formatted message type: {formatted.get('type')}, content_length: {len(formatted.get('content', ''))}")
                            logger.debug(f"[Claude SDK #{message_count}] Formatted output: {formatted}")
                        else:
                            logger.info(f"[Claude SDK #{message_count}] Message filtered out (returned None)")

                        if formatted is not None:  # Skip filtered messages
                            # Include session_id in formatted message if we have it
                            if current_session_id and formatted.get("session_id") is None:
                                formatted["session_id"] = current_session_id

                            yield formatted

                        # Stop when we get a ResultMessage
                        if isinstance(message_obj, ResultMessage):
                            logger.info(f"Received ResultMessage, stopping iteration")
                            break

                    logger.info(f"Finished receiving responses. Total messages: {message_count}")

                    # Write session completion to log file
                    self.session_logger.complete_session_log(session_log_file, message_count)

                except Exception as e:
                    # Check if this is exit code -9 (SIGKILL)
                    error_msg = str(e)
                    if "exit code -9" in error_msg or "exit code: -9" in error_msg:
                        if interrupt_initiated:
                            logger.info("Received expected exit code -9 after calling interrupt()")
                            # This is the expected result of interrupt() - yield the interrupted event now
                            if not interrupt_event_yielded:
                                yield {
                                    "type": "interrupted",
                                    "content": "Message interrupted by user",
                                    "session_id": current_session_id,
                                }
                                interrupt_event_yielded = True
                        else:
                            # Log captured stderr for debugging
                            logger.error(f"Session {current_session_id} died with exit code -9 (SIGKILL)")
                            logger.error(f"Possible causes: OOM kill, resource limits, or corrupted session")
                            logger.error(f"CLI stderr output:\n{stderr_capture.get_summary()}")
                            # Yield corrupted session error with stderr info
                            yield {
                                "type": "error",
                                "content": "Session was corrupted from a previous interrupt. Please try your message again.",
                                "error_type": "CorruptedSession",
                                "session_corrupted": True,
                                "stderr_lines": stderr_capture.get_recent_lines(10),
                            }
                    else:
                        # This is an unexpected error - include stderr in logs
                        logger.error(f"Error during receive_messages iteration: {e}", exc_info=True)
                        if stderr_capture.line_count > 0:
                            logger.error(f"CLI stderr output:\n{stderr_capture.get_summary()}")
                        raise
                finally:
                    # Unregister session from active session manager
                    if current_session_id:
                        await active_session_manager.unregister_session(current_session_id)
                        # Clear backend session mapping
                        clear_backend_session_id(current_session_id)

                    # Clear global SDK session ID
                    clear_current_sdk_session_id()

                    # Always disconnect after completing the message
                    try:
                        await client.disconnect()
                        logger.info("SDK client disconnected")
                    except Exception as e:
                        logger.error(f"Error disconnecting SDK client: {e}", exc_info=True)

            except ImportError as e:
                logger.error(f"Claude SDK not available: {e}")
                yield {
                    "type": "error",
                    "content": "Claude Code SDK is not installed or available",
                    "error_type": "ImportError",
                }
            except Exception as e:
                logger.error(f"SDK error: {type(e).__name__}: {e}", exc_info=True)
                # Include stderr in logs if available
                if 'stderr_capture' in locals() and stderr_capture.line_count > 0:
                    logger.error(f"CLI stderr output:\n{stderr_capture.get_summary()}")
                yield {
                    "type": "error",
                    "content": f"SDK error: {str(e)}",
                    "error_type": type(e).__name__,
                    "stderr_lines": stderr_capture.get_recent_lines(10) if 'stderr_capture' in locals() else [],
                }


# Global SDK manager instance
sdk_manager = ClaudeCodeSDKManager()
