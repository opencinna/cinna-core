import os
import logging
from typing import AsyncIterator, Optional
import uuid
import json
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class ClaudeCodeSDKManager:
    """
    Manages Claude Code SDK sessions for build mode.

    Responsibilities:
    - Initialize SDK with workspace and API key
    - Create and resume SDK sessions using ClaudeSDKClient
    - Stream responses from SDK
    - Handle SDK errors
    """

    def __init__(self):
        self.workspace_dir = os.getenv("CLAUDE_CODE_WORKSPACE", "/app/app")
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        self.permission_mode = os.getenv("CLAUDE_CODE_PERMISSION_MODE", "acceptEdits")

        # Session dump configuration
        self.dump_llm_session = os.getenv("DUMP_LLM_SESSION", "false").lower() == "true"
        self.logs_dir = Path(self.workspace_dir) / "logs"

        # Create logs directory if dump is enabled
        if self.dump_llm_session:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"LLM session dumping enabled. Logs will be saved to: {self.logs_dir}")

        # Validate configuration
        if not self.api_key:
            logger.warning("ANTHROPIC_API_KEY not set. SDK will not work.")

        # Load building agent prompt if exists
        self.building_agent_prompt = self._load_building_agent_prompt()

    def _load_building_agent_prompt(self) -> Optional[str]:
        """
        Load BUILDING_AGENT.md file from workspace.

        Returns:
            Content of BUILDING_AGENT.md if exists, None otherwise
        """
        building_agent_path = Path(self.workspace_dir) / "BUILDING_AGENT.md"

        if building_agent_path.exists():
            try:
                with open(building_agent_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    logger.info(f"Loaded BUILDING_AGENT.md ({len(content)} chars)")
                    return content
            except Exception as e:
                logger.error(f"Failed to load BUILDING_AGENT.md: {e}")
                return None
        else:
            logger.debug(f"BUILDING_AGENT.md not found at {building_agent_path}")
            return None

    def _load_scripts_readme(self) -> Optional[str]:
        """
        Load scripts/README.md file from workspace if it exists and is not empty.

        This file contains the catalog of existing scripts and will be included
        in the system prompt so the agent knows about existing scripts.

        Returns:
            Content of scripts/README.md if exists and not empty, None otherwise
        """
        scripts_readme_path = Path(self.workspace_dir) / "scripts" / "README.md"

        if scripts_readme_path.exists():
            try:
                with open(scripts_readme_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        logger.info(f"Loaded scripts/README.md ({len(content)} chars)")
                        return content
                    else:
                        logger.debug("scripts/README.md is empty")
                        return None
            except Exception as e:
                logger.error(f"Failed to load scripts/README.md: {e}")
                return None
        else:
            logger.debug(f"scripts/README.md not found at {scripts_readme_path}")
            return None

    def _init_session_log(self, message: str, session_id: Optional[str]) -> Optional[Path]:
        """
        Initialize a session log file for dumping raw LLM messages.

        Args:
            message: User message being sent
            session_id: External session ID (None for new session)

        Returns:
            Path to the log file if dumping is enabled, None otherwise
        """
        if not self.dump_llm_session:
            return None

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        log_file = self.logs_dir / f"session_{timestamp}_run.log"

        # Write header
        with open(log_file, "w") as f:
            f.write(f"# Claude Code SDK Session Log\n")
            f.write(f"# Started at: {datetime.utcnow().isoformat()}\n")
            f.write(f"# Session ID: {session_id or 'NEW SESSION'}\n")
            f.write(f"# User Message: {message[:100]}{'...' if len(message) > 100 else ''}\n")
            f.write(f"# ========================================\n\n")

        logger.info(f"Session log initialized: {log_file}")
        return log_file

    def _dump_message_to_log(self, log_file: Optional[Path], message_obj, message_count: int):
        """
        Dump raw message object to session log file.

        Args:
            log_file: Path to log file (None to skip)
            message_obj: SDK message object to dump
            message_count: Message sequence number
        """
        if not log_file:
            return

        try:
            with open(log_file, "a") as f:
                f.write(f"\n{'='*80}\n")
                f.write(f"MESSAGE #{message_count}\n")
                f.write(f"Timestamp: {datetime.utcnow().isoformat()}\n")
                f.write(f"Type: {type(message_obj).__name__}\n")
                f.write(f"{'-'*80}\n")
                f.write(self._format_message_for_debug(message_obj))
                f.write(f"\n{'='*80}\n")
        except Exception as e:
            logger.error(f"Error writing to session log: {e}", exc_info=True)

    async def send_message_stream(
        self,
        message: str,
        session_id: Optional[str] = None,
        system_prompt: Optional[str] = None,
        use_building_mode: bool = False,
    ) -> AsyncIterator[dict]:
        """
        Send message to Claude Code SDK and stream responses.

        Uses ClaudeSDKClient with proper session resumption via ClaudeAgentOptions.

        Args:
            message: User message
            session_id: External SDK session ID to resume (None = create new)
            system_prompt: Custom system prompt for this session (overrides building_mode)
            use_building_mode: If True, uses claude_code preset with BUILDING_AGENT.md appended

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
        # Initialize session log file if dumping is enabled
        session_log_file = self._init_session_log(message, session_id)

        try:
            # Import SDK here to avoid import errors if SDK not installed
            from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage

            # Build options
            options = ClaudeAgentOptions(
                allowed_tools=["Read", "Edit", "Glob", "Grep", "Bash", "Write"],
                permission_mode=self.permission_mode,
                cwd=self.workspace_dir,
            )

            # Set system prompt based on mode
            if system_prompt:
                # Explicit system_prompt overrides everything
                options.system_prompt = system_prompt
            elif use_building_mode and self.building_agent_prompt:
                # Build the complete building mode prompt
                building_prompt = self.building_agent_prompt

                # Append scripts README if it exists
                scripts_readme = self._load_scripts_readme()
                if scripts_readme:
                    building_prompt += f"\n\n---\n\n## Existing Scripts in Workspace\n\nThe following is the current contents of `./scripts/README.md` which catalogs all existing scripts in this workspace:\n\n```markdown\n{scripts_readme}\n```\n\n**Important**: When you create, modify, or remove scripts, you MUST update this file to keep it accurate."
                    logger.info("Included scripts/README.md in building mode prompt")

                # Use claude_code preset with complete building prompt appended
                options.system_prompt = {
                    "type": "preset",
                    "preset": "claude_code",
                    "append": building_prompt
                }
                logger.info("Using building mode with claude_code preset + BUILDING_AGENT.md + scripts catalog")
            elif use_building_mode:
                # Building mode requested but no BUILDING_AGENT.md available
                logger.warning("Building mode requested but BUILDING_AGENT.md not loaded, using claude_code preset only")
                options.system_prompt = {
                    "type": "preset",
                    "preset": "claude_code"
                }

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
                logger.error(f"Error sending query to SDK client: {e}", exc_info=True)
                raise

            # Stream responses using receive_messages()
            logger.info("Starting to iterate over receive_messages()...")
            message_count = 0
            try:
                async for message_obj in client.receive_messages():
                    message_count += 1

                    # Dump raw message to log file if enabled
                    self._dump_message_to_log(session_log_file, message_obj, message_count)

                    # Debug: Log raw message from Claude SDK with full structure
                    logger.debug(f"[Claude SDK #{message_count}] ========== RAW MESSAGE ==========")
                    logger.debug(f"[Claude SDK #{message_count}] Type: {type(message_obj).__name__}")
                    logger.debug(f"[Claude SDK #{message_count}] Full structure:\n{self._format_message_for_debug(message_obj)}")
                    logger.debug(f"[Claude SDK #{message_count}] ===================================")

                    formatted = self._format_sdk_message(message_obj, current_session_id)

                    # Debug: Log formatted message
                    if formatted is not None:
                        logger.info(f"[Claude SDK #{message_count}] Formatted message type: {formatted.get('type')}, content_length: {len(formatted.get('content', ''))}")
                        logger.debug(f"[Claude SDK #{message_count}] Formatted output: {formatted}")
                    else:
                        logger.info(f"[Claude SDK #{message_count}] Message filtered out (returned None)")

                    if formatted is not None:  # Skip filtered messages
                        # Extract session_id from ResultMessage for new sessions
                        if isinstance(message_obj, ResultMessage) and not current_session_id:
                            current_session_id = message_obj.session_id
                            formatted["session_id"] = current_session_id
                            logger.info(f"Captured session_id from ResultMessage: {current_session_id}")

                        yield formatted

                    # Stop when we get a ResultMessage
                    if isinstance(message_obj, ResultMessage):
                        logger.info(f"Received ResultMessage, stopping iteration")
                        break

                logger.info(f"Finished receiving responses. Total messages: {message_count}")

                # Write session completion to log file
                if session_log_file:
                    try:
                        with open(session_log_file, "a") as f:
                            f.write(f"\n\n{'='*80}\n")
                            f.write(f"SESSION COMPLETED\n")
                            f.write(f"Total messages: {message_count}\n")
                            f.write(f"Completed at: {datetime.utcnow().isoformat()}\n")
                            f.write(f"{'='*80}\n")
                        logger.info(f"Session log completed: {session_log_file}")
                    except Exception as e:
                        logger.error(f"Error writing session completion: {e}")
            except Exception as e:
                logger.error(f"Error during receive_messages iteration: {e}", exc_info=True)
                raise
            finally:
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

    def _format_message_for_debug(self, message_obj) -> str:
        """
        Format SDK message object for debug logging.

        Args:
            message_obj: SDK message object

        Returns:
            Formatted string representation with all fields
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage
        import json

        try:
            msg_type = type(message_obj).__name__

            if isinstance(message_obj, SystemMessage):
                return json.dumps({
                    "type": "SystemMessage",
                    "subtype": message_obj.subtype,
                    "data": message_obj.data
                }, indent=2)

            elif isinstance(message_obj, AssistantMessage):
                # Format content blocks
                content_blocks = []
                for block in message_obj.content:
                    block_type = type(block).__name__
                    if hasattr(block, '__dict__'):
                        content_blocks.append({
                            "type": block_type,
                            "data": block.__dict__
                        })
                    else:
                        content_blocks.append({
                            "type": block_type,
                            "data": str(block)
                        })

                return json.dumps({
                    "type": "AssistantMessage",
                    "model": getattr(message_obj, "model", None),
                    "content_blocks": content_blocks,
                    "num_blocks": len(message_obj.content)
                }, indent=2)

            elif isinstance(message_obj, ResultMessage):
                return json.dumps({
                    "type": "ResultMessage",
                    "subtype": message_obj.subtype,
                    "session_id": message_obj.session_id,
                    "duration_ms": message_obj.duration_ms,
                    "duration_api_ms": message_obj.duration_api_ms,
                    "is_error": message_obj.is_error,
                    "num_turns": message_obj.num_turns,
                    "total_cost_usd": message_obj.total_cost_usd,
                    "usage": message_obj.usage,
                    "result": message_obj.result
                }, indent=2)

            elif isinstance(message_obj, UserMessage):
                return json.dumps({
                    "type": "UserMessage",
                    "content": str(message_obj.content)[:200] + "..." if len(str(message_obj.content)) > 200 else str(message_obj.content)
                }, indent=2)

            else:
                # Fallback for unknown types
                if hasattr(message_obj, '__dict__'):
                    return json.dumps({
                        "type": msg_type,
                        "data": message_obj.__dict__
                    }, indent=2, default=str)
                else:
                    return f"{msg_type}: {str(message_obj)}"

        except Exception as e:
            return f"Error formatting message: {e}, raw: {str(message_obj)[:500]}"

    def _format_sdk_message(self, message_obj, session_id: str) -> dict:
        """
        Format SDK message object into standard dictionary.

        Args:
            message_obj: SDK message object (AssistantMessage, ResultMessage, SystemMessage, etc.)
            session_id: Current session ID

        Returns:
            Formatted message dict, or None to skip this message
        """
        from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock, ToolResultBlock, ResultMessage, ThinkingBlock, SystemMessage

        formatted = {
            "session_id": session_id,
            "content": "",
            "metadata": {},
        }

        # Handle SystemMessage (metadata messages from SDK)
        if isinstance(message_obj, SystemMessage):
            # Extract useful metadata but don't show init messages to user
            if message_obj.subtype == "init":
                # Store metadata but return empty content (will be filtered out)
                data = message_obj.data
                formatted["type"] = "system"
                formatted["content"] = ""  # Don't show init message to user
                formatted["metadata"] = {
                    "subtype": "init",
                    "model": data.get("model"),
                    "claude_code_version": data.get("claude_code_version"),
                    "cwd": data.get("cwd"),
                    "permission_mode": data.get("permissionMode"),
                }
                # Return None to skip this message (don't send to frontend)
                return None
            else:
                # Other system messages - log subtype for debugging
                formatted["type"] = "system"
                formatted["content"] = f"System: {message_obj.subtype}"
                formatted["metadata"]["subtype"] = message_obj.subtype
                return formatted

        # Handle AssistantMessage
        elif isinstance(message_obj, AssistantMessage):
            content_parts = []

            for block in message_obj.content:
                if isinstance(block, TextBlock):
                    # Regular text content
                    formatted["type"] = "assistant"
                    content_parts.append(block.text)

                elif isinstance(block, ThinkingBlock):
                    # Thinking block (extended thinking models)
                    formatted["type"] = "thinking"
                    content_parts.append(f"[Thinking] {block.thinking}")

                elif isinstance(block, ToolUseBlock):
                    # Tool use request - send as separate event
                    tool_input_str = ""
                    if block.input:
                        # Format tool input for display
                        import json
                        try:
                            # Try to format as readable JSON, but limit size
                            input_json = json.dumps(block.input, indent=2)
                            if len(input_json) > 200:
                                # Truncate long inputs
                                input_json = input_json[:200] + "..."
                            tool_input_str = f"\nInput: {input_json}"
                        except:
                            tool_input_str = f"\nInput: {str(block.input)[:200]}"

                    formatted["type"] = "tool"
                    formatted["tool_name"] = block.name
                    formatted["content"] = f"🔧 Using tool: {block.name}{tool_input_str}"
                    formatted["metadata"]["tool_id"] = block.id
                    formatted["metadata"]["tool_input"] = block.input
                    # Return immediately for tool use to preserve event granularity
                    return formatted

                elif isinstance(block, ToolResultBlock):
                    # Tool result - don't show to user, just metadata
                    # Skip tool results as they're internal
                    continue

            # Set default type if not set by blocks
            if "type" not in formatted:
                formatted["type"] = "assistant"

            formatted["content"] = "\n".join(content_parts) if content_parts else ""

            # Extract model metadata from AssistantMessage
            if hasattr(message_obj, "model"):
                formatted["metadata"]["model"] = message_obj.model

        # Handle ResultMessage
        elif isinstance(message_obj, ResultMessage):
            formatted["type"] = "done"  # Signal completion
            formatted["content"] = ""  # Don't show "Task completed" to user
            formatted["metadata"] = {
                "subtype": message_obj.subtype,
                "duration_ms": message_obj.duration_ms,
                "is_error": message_obj.is_error,
                "num_turns": message_obj.num_turns,
                "total_cost_usd": message_obj.total_cost_usd,
                "session_id": message_obj.session_id,
            }

        # Handle other message types
        else:
            logger.warning(f"Unknown message type: {type(message_obj)}")
            formatted["type"] = "unknown"
            formatted["content"] = ""
            return None

        return formatted


# Global SDK manager instance
sdk_manager = ClaudeCodeSDKManager()
