import os
import logging
from typing import AsyncIterator, Optional
from pathlib import Path

from .prompt_generator import PromptGenerator
from .sdk_utils import SessionLogger, format_message_for_debug, format_sdk_message
from .active_session_manager import active_session_manager

logger = logging.getLogger(__name__)


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

    async def send_message_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
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

        # Initialize session log file if dumping is enabled
        session_log_file = self.session_logger.init_session_log(message, session_id)

        try:
            # Import SDK here to avoid import errors if SDK not installed
            from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage

            # Build options
            options = ClaudeAgentOptions(
                allowed_tools=["Read", "Edit", "Glob", "Grep", "Bash", "Write"],
                permission_mode=self.permission_mode,
                cwd=self.workspace_dir,
            )

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
                logger.info(f"Registered session {current_session_id} for interrupt support")

            # Emit session_created event only for new sessions
            if not session_id:
                # We don't know the real session_id yet - will get it from ResultMessage
                yield {
                    "type": "session_created",
                    "session_id": None,  # Will be set from ResultMessage
                    "content": "",
                }

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
                    }
                    return

                logger.error(f"Error sending query to SDK client: {e}", exc_info=True)
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
                            logger.info(f"✅ Registered session {current_session_id} for interrupt support (early)")

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
                        logger.warning(f"Session {current_session_id} died with exit code -9 (likely corrupted from previous interrupt)")
                        # Yield corrupted session error
                        yield {
                            "type": "error",
                            "content": "Session was corrupted from a previous interrupt. Please try your message again.",
                            "error_type": "CorruptedSession",
                            "session_corrupted": True,
                        }
                else:
                    # This is an unexpected error
                    logger.error(f"Error during receive_messages iteration: {e}", exc_info=True)
                    raise
            finally:
                # Unregister session from active session manager
                if current_session_id:
                    await active_session_manager.unregister_session(current_session_id)

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
            yield {
                "type": "error",
                "content": f"SDK error: {str(e)}",
                "error_type": type(e).__name__,
            }


# Global SDK manager instance
sdk_manager = ClaudeCodeSDKManager()
