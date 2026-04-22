from uuid import UUID
from datetime import datetime, UTC
from typing import AsyncIterator, NamedTuple
import json
import re
import time
import httpx
import logging
import asyncio
from sqlmodel import Session, select, func
from app.models import SessionMessage, Session as ChatSession, AgentEnvironment, Agent, SessionUpdate, MessagePublic
from app.services.sessions.active_streaming_manager import active_streaming_manager
from app.services.environments.agent_env_connector import agent_env_connector
from app.services.agents.agent_service import AgentService
from app.models.mcp.mcp_session_meta import MCPSessionMeta
from app.services.sessions.session_context_signer import sign_session_context

logger = logging.getLogger(__name__)

# Regex for matching complete <webapp_action>...</webapp_action> tags.
# Uses non-greedy matching to handle multiple tags in one response.
_WEBAPP_ACTION_TAG_RE = re.compile(
    r"<webapp_action>(.*?)</webapp_action>",
    re.DOTALL,
)

# Pre-allowed tools that never require user approval.
# Canonical source: tool_name_registry.PRE_APPROVED_TOOLS (inside agent-env).
# Kept in sync here because the backend cannot import from agent-env templates.
# Convention: all tool names are **lowercase**.
PRE_ALLOWED_TOOLS = frozenset([
    # Built-in SDK tools (unified lowercase)
    "read", "write", "edit", "bash", "glob", "grep",
    "webfetch", "websearch", "todowrite",
    "task", "skill", "askuserquestion",
    "enterplanmode", "exitplanmode", "notebookedit",
    "killshell", "taskoutput",
    # OpenCode-only built-ins
    "list", "patch",
    # MCP bridge tools (knowledge)
    "mcp__knowledge__query_integration_knowledge",
    # MCP bridge tools (agent task)
    "mcp__agent_task__add_comment",
    "mcp__agent_task__update_status",
    "mcp__agent_task__create_task",
    "mcp__agent_task__create_subtask",
    "mcp__agent_task__get_details",
    "mcp__agent_task__list_tasks",
])

# Metadata keys to forward from streaming events to response_metadata
_FORWARDED_METADATA_KEYS = {"model", "total_cost_usd", "claude_code_version", "duration_ms", "num_turns"}

# Keys to keep when copying event metadata for storage
_STORED_EVENT_METADATA_KEYS = {"tool_id", "tool_input", "model", "needs_approval", "tool_name"}

# Non-LLM to LLM context bridging
NON_LLM_BRIDGE_MAX_PER_BLOCK_BYTES: int = 16_384   # 16 KB per command block
NON_LLM_BRIDGE_MAX_TOTAL_BYTES: int = 65_536        # 64 KB total prior_commands block
NON_LLM_BRIDGE_TRUNCATION_MARKER: str = "\n[output truncated]"


def _extract_webapp_actions(content: str) -> tuple[list[dict], str]:
    """
    Extract all complete <webapp_action>...</webapp_action> tags from content.

    Parses the JSON payload inside each tag. Tags with malformed JSON are
    logged as warnings and skipped (not emitted). All complete tags —
    whether their JSON parsed or not — are stripped from the returned
    cleaned content so they never appear in the chat UI.

    Args:
        content: Raw text that may contain webapp_action tags.

    Returns:
        A tuple of (actions, cleaned_content) where:
        - actions: list of dicts with keys "action" (str) and "data" (dict, may be empty)
        - cleaned_content: the original text with all complete tags removed
    """
    actions: list[dict] = []
    matches = list(_WEBAPP_ACTION_TAG_RE.finditer(content))

    for match in matches:
        raw_json = match.group(1).strip()
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning(
                "webapp_action tag contained invalid JSON — skipping emission: %r", raw_json[:200]
            )
            continue

        action_name = payload.get("action")
        if not action_name or not isinstance(action_name, str):
            logger.warning(
                "webapp_action payload missing 'action' field — skipping: %r", payload
            )
            continue

        actions.append({
            "action": action_name,
            "data": payload.get("data", {}),
        })

    # Strip ALL complete tags from the content regardless of JSON validity
    cleaned_content = _WEBAPP_ACTION_TAG_RE.sub("", content)
    return actions, cleaned_content


async def _emit_webapp_action_events(
    session_id: UUID,
    actions: list[dict],
) -> None:
    """
    Emit webapp_action WebSocket events for a list of parsed action payloads.

    Each action is emitted as a separate stream event to the session stream
    room so the webapp frontend (WebappChatWidget) can forward it to the
    iframe via postMessage.

    Errors are caught and logged — a failed emission must never crash the stream.
    """
    try:
        from app.services.events.event_service import event_service
        for action in actions:
            await event_service.emit_stream_event(
                session_id=session_id,
                event_type="webapp_action",
                event_data={
                    "action": action["action"],
                    "data": action["data"],
                    "session_id": str(session_id),
                },
            )
            logger.info(
                "Emitted webapp_action event: action=%r session=%s", action["action"], session_id
            )
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception as e:
        logger.error(
            "Failed to emit webapp_action events for session %s: %s", session_id, e, exc_info=True
        )


def _compute_context_diff(old_context_str: str, new_context_str: str) -> dict | None:
    """
    Compute a semantic diff between two page_context JSON strings.

    Both arguments must be valid JSON strings. The context payload has the
    structure: {"selected_text": str, "page": {...}, "microdata": [...]}

    Returns:
        None      — if the contexts are semantically identical (no change)
        dict      — with keys "changed", "added", "removed" describing only what
                    differs. Each key is omitted if it has no entries, so the
                    returned dict is always non-empty when not None.

    Raises nothing — callers should handle any exception and fall back to full
    context injection.
    """
    old_data = json.loads(old_context_str)
    new_data = json.loads(new_context_str)

    # Fast path: identical after round-trip through json.loads (normalises key order)
    if old_data == new_data:
        return None

    changed: dict = {}
    added: dict = {}
    removed: list = []

    all_keys = set(old_data.keys()) | set(new_data.keys())
    for key in all_keys:
        if key not in old_data:
            added[key] = new_data[key]
        elif key not in new_data:
            removed.append(key)
        elif old_data[key] != new_data[key]:
            changed[key] = {"from": old_data[key], "to": new_data[key]}

    diff: dict = {}
    if changed:
        diff["changed"] = changed
    if added:
        diff["added"] = added
    if removed:
        diff["removed"] = removed

    # Diff should always be non-empty here because old_data != new_data
    return diff if diff else None


class MessageServiceError(Exception):
    """Domain exception for message service operations."""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class StreamContext(NamedTuple):
    """Pre-resolved context for streaming a message to agent environment."""
    user_id: UUID | None
    allowed_tools: set
    previous_result_state: str | None
    session_context: dict | None
    base_url: str
    auth_headers: dict
    env_auth_token: str | None


async def _emit_activity_event(
    event_type,
    session_id: UUID,
    environment_id: UUID,
    session_mode: str,
    user_id: UUID | None,
    **extra_meta,
) -> None:
    """Fire-and-forget activity event with standard error handling."""
    try:
        from app.services.events.event_service import event_service
        await event_service.emit_event(
            event_type=event_type,
            model_id=session_id,
            meta={
                "session_id": str(session_id),
                "environment_id": str(environment_id),
                "session_mode": session_mode,
                **extra_meta,
            },
            user_id=user_id,
        )
        logger.info(f"Emitted {event_type.value if hasattr(event_type, 'value') else event_type} event for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to emit {event_type} event: {e}", exc_info=True)


def _build_session_context(
    db: Session,
    session_db: ChatSession,
    env: AgentEnvironment | None,
    agent: Agent | None,
) -> dict:
    """Build session_context dict for agent-env, including email/MCP enrichment."""
    context = {
        "integration_type": session_db.integration_type,
        "agent_id": str(env.agent_id) if env and env.agent_id else None,
        "is_clone": agent.is_clone if agent else False,
        "parent_agent_id": str(agent.parent_agent_id) if agent and agent.parent_agent_id else None,
        "sender_email": session_db.sender_email,
        "email_thread_id": session_db.email_thread_id,
        "backend_session_id": str(session_db.id),
    }

    # Fetch email subject from the initiating EmailMessage (avoid data duplication)
    if session_db.integration_type == "email":
        from app.models.email.email_message import EmailMessage
        initiating_email = db.exec(
            select(EmailMessage)
            .where(EmailMessage.session_id == session_db.id)
            .order_by(EmailMessage.received_at.asc())
            .limit(1)
        ).first()
        if initiating_email:
            context["email_subject"] = initiating_email.subject

    # Fetch authenticated MCP user email (may differ from session owner)
    if session_db.integration_type == "mcp":
        mcp_meta = db.exec(
            select(MCPSessionMeta).where(
                MCPSessionMeta.session_id == session_db.id
            )
        ).first()
        if mcp_meta:
            context["mcp_user_email"] = mcp_meta.authenticated_user_email

    # Enrich with task context if this session is linked to an InputTask.
    # Use Session.source_task_id (immutable) rather than InputTask.session_id
    # which is overwritten on task re-execution.
    try:
        from app.models.tasks.input_task import InputTask
        task = (
            db.get(InputTask, session_db.source_task_id)
            if session_db.source_task_id
            else None
        )
        if task:
            context["task_short_code"] = task.short_code
            context["task_title"] = task.title or ""
            context["task_description"] = task.current_description or ""
            context["task_priority"] = task.priority or "normal"
            context["task_status"] = task.status or "in_progress"

            # Creator info
            if task.agent_initiated and task.source_agent_id:
                source_agent = db.get(Agent, task.source_agent_id)
                context["task_created_by_name"] = source_agent.name if source_agent else "Unknown Agent"
                context["task_created_by_type"] = "agent"
            else:
                from app.models.users.user import User as UserModel
                creator = db.get(UserModel, task.owner_id)
                context["task_created_by_name"] = (creator.full_name or creator.email) if creator else "Unknown User"
                context["task_created_by_type"] = "user"

            # Parent task context (subtasks)
            parent_task = None
            if task.parent_task_id:
                parent_task = db.get(InputTask, task.parent_task_id)
                if parent_task:
                    context["parent_task_short_code"] = parent_task.short_code
                    context["parent_task_title"] = parent_task.title or ""
                    context["parent_task_description"] = parent_task.current_description or ""
                    if parent_task.selected_agent_id:
                        parent_agent = db.get(Agent, parent_task.selected_agent_id)
                        if parent_agent:
                            context["parent_assigned_agent_name"] = parent_agent.name
                    if parent_task.assigned_node_id:
                        from app.models.agentic_teams.agentic_team import AgenticTeamNode
                        parent_node = db.get(AgenticTeamNode, parent_task.assigned_node_id)
                        if parent_node:
                            context["parent_node_name"] = parent_node.name

            # Team context
            if task.team_id:
                from app.models.agentic_teams.agentic_team import AgenticTeam
                team = db.get(AgenticTeam, task.team_id)
                if team:
                    context["team_name"] = team.name

            # Node context + downstream members
            if task.assigned_node_id:
                from app.models.agentic_teams.agentic_team import AgenticTeamNode, AgenticTeamConnection
                node = db.get(AgenticTeamNode, task.assigned_node_id)
                if node:
                    context["node_name"] = node.name

                # Find downstream connections
                downstream_conns = db.exec(
                    select(AgenticTeamConnection).where(
                        AgenticTeamConnection.source_node_id == task.assigned_node_id,
                        AgenticTeamConnection.enabled == True,
                    )
                ).all()
                if downstream_conns:
                    downstream_members = []
                    for conn in downstream_conns:
                        target_node = db.get(AgenticTeamNode, conn.target_node_id)
                        if target_node:
                            target_agent = db.get(Agent, target_node.agent_id) if target_node.agent_id else None
                            downstream_members.append({
                                "node_name": target_node.name,
                                "agent_name": target_agent.name if target_agent else "",
                                "agent_description": target_agent.description if target_agent else "",
                                "connection_prompt": conn.connection_prompt or "",
                            })
                    if downstream_members:
                        context["downstream_team_members"] = downstream_members

            # Delegation connection prompt (for subtasks with team context)
            if parent_task and task.assigned_node_id and parent_task.assigned_node_id:
                from app.models.agentic_teams.agentic_team import AgenticTeamConnection
                delegation_conn = db.exec(
                    select(AgenticTeamConnection).where(
                        AgenticTeamConnection.source_node_id == parent_task.assigned_node_id,
                        AgenticTeamConnection.target_node_id == task.assigned_node_id,
                    )
                ).first()
                if delegation_conn:
                    context["delegation_connection_prompt"] = delegation_conn.connection_prompt or ""
    except Exception as e:
        logger.warning(f"Task context enrichment failed (non-critical): {e}", exc_info=True)

    return context


class MessageService:
    @staticmethod
    def create_message(
        session: Session,
        session_id: UUID,
        role: str,
        content: str,
        message_metadata: dict | None = None,
        answers_to_message_id: UUID | None = None,
        tool_questions_status: str | None = None,
        status: str = "",
        status_message: str | None = None,
        file_ids: list[UUID] | None = None,
        sent_to_agent_status: str = "pending",
    ) -> SessionMessage:
        """Create message in session with auto-incremented sequence.

        If file_ids provided, creates message_files junction records.
        """
        # Get the next sequence number for this session
        statement = select(func.max(SessionMessage.sequence_number)).where(
            SessionMessage.session_id == session_id
        )
        max_sequence = session.exec(statement).first()
        next_sequence = (max_sequence or 0) + 1

        message = SessionMessage(
            session_id=session_id,
            role=role,
            content=content,
            sequence_number=next_sequence,
            message_metadata=message_metadata or {},
            answers_to_message_id=answers_to_message_id,
            tool_questions_status=tool_questions_status,
            status=status,
            status_message=status_message,
            sent_to_agent_status=sent_to_agent_status,
        )
        session.add(message)

        # If this message is answering another message's questions, update that message's status
        if answers_to_message_id:
            referenced_message = session.get(SessionMessage, answers_to_message_id)
            if referenced_message:
                referenced_message.tool_questions_status = "answered"
                session.add(referenced_message)

        # Update session's last_message_at
        chat_session = session.get(ChatSession, session_id)
        if chat_session:
            chat_session.last_message_at = datetime.now(UTC)
            session.add(chat_session)

        # Flush to ensure message exists in DB before creating message_files
        # This prevents foreign key constraint violations
        session.flush()

        # Create message_files records if files attached
        if file_ids:
            from app.models.files.file_upload import MessageFile
            for file_id in file_ids:
                message_file = MessageFile(
                    message_id=message.id,
                    file_id=file_id,
                    # agent_env_path set later when files uploaded to agent-env
                )
                session.add(message_file)

        session.commit()
        session.refresh(message)
        return message

    @staticmethod
    async def prepare_user_message_with_files(
        session: Session,
        session_id: UUID,
        message_content: str,
        file_ids: list[UUID],
        environment_id: UUID,
        user_id: UUID,
        answers_to_message_id: UUID | None = None,
        message_metadata: dict | None = None,
    ) -> tuple[SessionMessage, str]:
        """
        Prepare user message with file attachments.

        This method:
        1. Validates files (ownership, status)
        2. Uploads files to agent-env
        3. Creates user message with file associations
        4. Updates message_files with agent_env_paths
        5. Marks files as attached

        Raises:
            MessageServiceError: If validation fails or upload errors
        """
        from app.models.files.file_upload import FileUpload, MessageFile
        from app.services.files.file_service import FileService
        from sqlmodel import select

        # Validate files exist
        statement = select(FileUpload).where(FileUpload.id.in_(file_ids))
        files = session.exec(statement).all()

        if len(files) != len(file_ids):
            raise MessageServiceError("Some files not found", status_code=400)

        # Check ownership and status
        for file in files:
            if file.user_id != user_id:
                raise MessageServiceError(
                    f"Not authorized for file: {file.filename}",
                    status_code=403,
                )
            if file.status != "temporary":
                raise MessageServiceError(
                    f"File already attached: {file.filename}",
                    status_code=400,
                )

        # Upload files to agent-env
        try:
            agent_file_paths = await FileService.upload_files_to_agent_env(
                session=session,
                file_ids=file_ids,
                environment_id=environment_id,
            )
        except Exception as e:
            raise MessageServiceError(
                f"Failed to upload files to agent environment: {str(e)}",
                status_code=500,
            ) from e

        # Compose message content with file paths for agent
        file_list = "\n".join(f"- {path}" for path in agent_file_paths.values())
        message_content_for_agent = f"Uploaded files:\n{file_list}\n---\n\n{message_content}"

        # Create user message with file associations
        user_message = MessageService.create_message(
            session=session,
            session_id=session_id,
            role="user",
            content=message_content,  # Store original content without file paths
            answers_to_message_id=answers_to_message_id,
            file_ids=file_ids,
            message_metadata=message_metadata,
        )

        # Update message_files with agent_env_paths
        statement = select(MessageFile).where(MessageFile.message_id == user_message.id)
        message_files = session.exec(statement).all()

        for message_file in message_files:
            if message_file.file_id in agent_file_paths:
                message_file.agent_env_path = agent_file_paths[message_file.file_id]

        session.commit()

        # Mark files as attached
        FileService.mark_files_as_attached(
            session=session,
            file_ids=list(agent_file_paths.keys()),
        )

        logger.info(f"Prepared user message with {len(file_ids)} files for session {session_id}")

        return user_message, message_content_for_agent

    @staticmethod
    def get_session_messages(
        session: Session, session_id: UUID, limit: int = 100, offset: int = 0
    ) -> list["MessagePublic"]:
        """Get messages for session ordered by sequence with files populated"""
        from app.services.files.file_service import FileService
        from app.models.sessions.session import MessagePublic

        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number)
            .offset(offset)
            .limit(limit)
        )
        db_messages = list(session.exec(statement).all())

        # Convert to MessagePublic and populate files
        messages = []
        for msg in db_messages:
            files = FileService.get_message_files(
                session=session,
                message_id=msg.id
            )
            message_public = MessagePublic(
                id=msg.id,
                session_id=msg.session_id,
                role=msg.role,
                content=msg.content,
                sequence_number=msg.sequence_number,
                timestamp=msg.timestamp,
                message_metadata=msg.message_metadata,
                tool_questions_status=msg.tool_questions_status,
                answers_to_message_id=msg.answers_to_message_id,
                status=msg.status,
                status_message=msg.status_message,
                sent_to_agent_status=msg.sent_to_agent_status,
                files=files
            )
            messages.append(message_public)

        return messages

    @staticmethod
    def get_last_n_messages(
        session: Session, session_id: UUID, n: int = 20
    ) -> list[SessionMessage]:
        """Get last N messages for context window"""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number.desc())
            .limit(n)
        )
        messages = list(session.exec(statement).all())
        # Reverse to get chronological order
        return list(reversed(messages))

    @staticmethod
    def build_recovery_context(db, session_id: UUID, max_messages: int = 20) -> str:
        """Build history string for session recovery."""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number.desc())
            .limit(max_messages)
        )
        messages = list(reversed(list(db.exec(statement).all())))

        # Filter to user + agent messages only (skip system)
        filtered = [m for m in messages if m.role in ("user", "agent")]
        if not filtered:
            return ""

        lines = []
        for msg in filtered:
            role_label = "User" if msg.role == "user" else "Assistant"
            lines.append(f"{role_label}: {msg.content or ''}")

        return (
            "[SESSION RECOVERY]\n"
            "Previous conversation history:\n"
            + "\n".join(lines)
            + "\n[END SESSION RECOVERY]\n"
            "Please continue the conversation. The user's new message follows:"
        )

    @staticmethod
    def get_last_message(
        session: Session, session_id: UUID
    ) -> SessionMessage | None:
        """Get the last message in a session by sequence number"""
        statement = (
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id)
            .order_by(SessionMessage.sequence_number.desc())
            .limit(1)
        )
        return session.exec(statement).first()

    @staticmethod
    def collect_pending_messages(session: Session, session_id: UUID) -> tuple[str | None, list[SessionMessage]]:
        """
        Collect all pending user messages (sent_to_agent_status='pending') for a session.

        If messages have attached files, reconstructs the content with file paths prepended.

        Page context diff optimisation: instead of injecting the full page_context
        on every message, only the diff relative to the previously-sent context is
        sent.  If the context is unchanged from the last sent message the block is
        omitted entirely; if it changed a compact <context_update> block is used.
        The first message (or any message where diffing fails due to malformed JSON)
        always gets the full <page_context> block.

        Returns:
            tuple: (concatenated_content, list of pending_messages)
                - concatenated_content: All pending messages concatenated with formatting, or None if no pending messages
                - pending_messages: List of SessionMessage objects that are pending
        """
        from app.models.files.file_upload import MessageFile

        # Get all pending user messages
        statement = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "user",
                SessionMessage.sent_to_agent_status == "pending"
            )
            .order_by(SessionMessage.sequence_number)
        )
        pending_messages = list(session.exec(statement).all())

        if not pending_messages:
            return None, []

        # ── Context diff optimisation setup ───────────────────────────────────
        # Find the most recent already-sent user message that carried a page_context
        # so we can diff the current context against it rather than sending the full
        # payload every time.
        first_pending_seq = pending_messages[0].sequence_number
        prev_context_stmt = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "user",
                SessionMessage.sent_to_agent_status == "sent",
                SessionMessage.sequence_number < first_pending_seq,
            )
            .order_by(SessionMessage.sequence_number.desc())
            .limit(20)  # scan up to 20 sent messages to find one with page_context
        )
        sent_messages_recent = list(session.exec(prev_context_stmt).all())
        last_sent_context: str | None = None
        for sent_msg in sent_messages_recent:
            ctx = (sent_msg.message_metadata or {}).get("page_context")
            if ctx:
                last_sent_context = ctx
                break

        # Mutable closure variable: tracks the "previous context" as we process
        # multiple pending messages in sequence.  Wrapped in a list so the inner
        # function can update it without a nonlocal declaration.
        prev_context_ref: list[str | None] = [last_sent_context]

        # Helper function to reconstruct message content with files
        def get_message_content_with_files(message: SessionMessage) -> str:
            # Check if message has attached files
            file_statement = select(MessageFile).where(MessageFile.message_id == message.id)
            message_files = list(session.exec(file_statement).all())

            if not message_files:
                # No files — start with original content only
                agent_content = message.content
            else:
                # Reconstruct content with file paths (same format as prepare_user_message_with_files)
                file_paths = [mf.agent_env_path for mf in message_files if mf.agent_env_path]

                if not file_paths:
                    # Files exist but no agent_env_path, return original content
                    agent_content = message.content
                else:
                    file_list = "\n".join(f"- {path}" for path in file_paths)
                    agent_content = f"Uploaded files:\n{file_list}\n---\n\n{message.content}"

            # ── Page context diff injection ────────────────────────────────────
            # The page_context is stored in message_metadata (not in message.content)
            # so the chat UI never renders it — only the agent-env sees the XML block.
            stored_page_context = (message.message_metadata or {}).get("page_context")
            if stored_page_context:
                previous = prev_context_ref[0]
                context_block: str | None = None

                if previous is None:
                    # First message in this session that carries context — send full block.
                    context_block = f"<page_context>\n{stored_page_context}\n</page_context>"
                else:
                    # Attempt to diff; fall back to full block on any error.
                    try:
                        diff = _compute_context_diff(previous, stored_page_context)
                        if diff is None:
                            # Context is identical — omit the block entirely.
                            context_block = None
                        else:
                            diff_json = json.dumps(diff, ensure_ascii=False)
                            context_block = f"<context_update>\n{diff_json}\n</context_update>"
                    except Exception:
                        logger.debug(
                            "page_context diff failed for message %s; falling back to full block",
                            message.id,
                        )
                        context_block = f"<page_context>\n{stored_page_context}\n</page_context>"

                if context_block:
                    agent_content = f"{agent_content}\n\n{context_block}"

                # Advance the closure so the next pending message diffs against this one.
                prev_context_ref[0] = stored_page_context

            return agent_content

        # Concatenate messages with formatting
        if len(pending_messages) == 1:
            # Single message - no need for formatting
            concatenated_content = get_message_content_with_files(pending_messages[0])
        else:
            # Multiple messages - format with separators
            message_contents = []
            for i, msg in enumerate(pending_messages):
                msg_content = get_message_content_with_files(msg)
                message_contents.append(f"[Message {i+1}]:\n{msg_content}")
            concatenated_content = "\n\n".join(message_contents)

        logger.info(f"Collected {len(pending_messages)} pending messages for session {session_id}")
        return concatenated_content, pending_messages

    @staticmethod
    def collect_pending_batches(db: Session, session_id: UUID) -> list[dict]:
        """
        Collect pending messages and partition them into contiguous same-routing batches.

        Returns a list of batch dicts ordered by message sequence:
            [
              {"routing": None, "messages": [msg1, msg2], "content": "..."},
              {"routing": "command_stream", "messages": [msg3],
               "resolved_command": "...", "command_name": "/run:check"},
              {"routing": None, "messages": [msg4], "content": "..."},
            ]

        For routing=None batches, "content" is the concatenated message content
        (same logic as collect_pending_messages, including file paths and page-context diff).
        For routing="command_stream" batches, "resolved_command" and "command_name" are
        read from the single message's message_metadata.
        """
        # Fetch all pending user messages ordered by sequence number
        statement = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "user",
                SessionMessage.sent_to_agent_status == "pending",
            )
            .order_by(SessionMessage.sequence_number)
        )
        pending_messages = list(db.exec(statement).all())

        if not pending_messages:
            return []

        # Partition into contiguous same-routing runs
        batches: list[dict] = []
        current_routing = (pending_messages[0].message_metadata or {}).get("routing")
        current_group: list = [pending_messages[0]]

        for msg in pending_messages[1:]:
            routing = (msg.message_metadata or {}).get("routing")
            if routing == current_routing:
                current_group.append(msg)
            else:
                batches.append({"routing": current_routing, "messages": list(current_group)})
                current_routing = routing
                current_group = [msg]
        batches.append({"routing": current_routing, "messages": list(current_group)})

        # Enrich each batch with routing-specific data
        result: list[dict] = []
        for batch in batches:
            routing = batch["routing"]
            messages = batch["messages"]

            if routing == "command_stream":
                # Single command message — read metadata
                msg = messages[0]
                meta = msg.message_metadata or {}
                result.append({
                    "routing": "command_stream",
                    "messages": messages,
                    "resolved_command": meta.get("resolved_command", ""),
                    "command_name": meta.get("command_name", "/run"),
                })
            else:
                # LLM batch — build concatenated content using same logic as
                # collect_pending_messages (file paths + page-context diff),
                # but scoped to only the messages in this batch.
                content = MessageService._build_llm_batch_content(db, session_id, messages)
                result.append({
                    "routing": None,
                    "messages": messages,
                    "content": content,
                })

        # Inject non-LLM prior-commands prefix into the first LLM batch
        prefix, included_ids = MessageService.build_non_llm_prefix(db, session_id)
        if prefix:
            for batch in result:
                if batch["routing"] is None:
                    batch["content"] = f"{prefix}\n\n{batch['content']}"
                    break

        # Attach included_ids to the first batch so stream_processor can mark them
        # after the streaming step completes.
        if result:
            result[0]["_included_command_ids"] = included_ids

        return result

    @staticmethod
    def _build_llm_batch_content(
        db: Session,
        session_id: UUID,
        messages: list,
    ) -> str:
        """
        Build concatenated content string for a group of LLM pending messages.

        Handles file path reconstruction and page-context diff injection,
        mirroring the logic in collect_pending_messages but operating on
        an arbitrary subset of messages.
        """
        from app.models.files.file_upload import MessageFile

        if not messages:
            return ""

        # Find the last sent message that had page_context before the first message in this group
        first_seq = messages[0].sequence_number
        prev_context_stmt = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "user",
                SessionMessage.sent_to_agent_status == "sent",
                SessionMessage.sequence_number < first_seq,
            )
            .order_by(SessionMessage.sequence_number.desc())
            .limit(20)
        )
        sent_recent = list(db.exec(prev_context_stmt).all())
        last_sent_context: str | None = None
        for sent_msg in sent_recent:
            ctx = (sent_msg.message_metadata or {}).get("page_context")
            if ctx:
                last_sent_context = ctx
                break

        prev_context_ref: list[str | None] = [last_sent_context]

        def _get_content(message: SessionMessage) -> str:
            file_stmt = select(MessageFile).where(MessageFile.message_id == message.id)
            message_files = list(db.exec(file_stmt).all())

            if not message_files:
                agent_content = message.content
            else:
                file_paths = [mf.agent_env_path for mf in message_files if mf.agent_env_path]
                if not file_paths:
                    agent_content = message.content
                else:
                    file_list = "\n".join(f"- {path}" for path in file_paths)
                    agent_content = f"Uploaded files:\n{file_list}\n---\n\n{message.content}"

            stored_page_context = (message.message_metadata or {}).get("page_context")
            if stored_page_context:
                previous = prev_context_ref[0]
                context_block: str | None = None

                if previous is None:
                    context_block = f"<page_context>\n{stored_page_context}\n</page_context>"
                else:
                    try:
                        diff = _compute_context_diff(previous, stored_page_context)
                        if diff is None:
                            context_block = None
                        else:
                            diff_json = json.dumps(diff, ensure_ascii=False)
                            context_block = f"<context_update>\n{diff_json}\n</context_update>"
                    except Exception:
                        context_block = f"<page_context>\n{stored_page_context}\n</page_context>"

                if context_block:
                    agent_content = f"{agent_content}\n\n{context_block}"

                prev_context_ref[0] = stored_page_context

            return agent_content

        if len(messages) == 1:
            return _get_content(messages[0])

        message_contents = []
        for i, msg in enumerate(messages):
            msg_content = _get_content(msg)
            message_contents.append(f"[Message {i+1}]:\n{msg_content}")
        return "\n\n".join(message_contents)

    @staticmethod
    def mark_messages_as_sent(session: Session, message_ids: list[UUID]) -> None:
        """
        Mark messages as sent to agent-env.

        Args:
            session: Database session
            message_ids: List of message IDs to mark as sent
        """
        for message_id in message_ids:
            message = session.get(SessionMessage, message_id)
            if message and message.role == "user":
                message.sent_to_agent_status = "sent"
                session.add(message)

        session.commit()
        logger.info(f"Marked {len(message_ids)} messages as sent to agent")

    @staticmethod
    def build_non_llm_prefix(
        db: Session,
        session_id: UUID,
    ) -> tuple[str | None, list[UUID]]:
        """
        Collect command pairs between the last agent message and now,
        format them as a <prior_commands> XML block, and return
        (prefix_text_or_none, list_of_included_system_message_ids).

        Only system messages with message_metadata['command']==True and no
        'forwarded_to_llm_at' key are eligible. Handlers with
        include_in_llm_context=False are skipped and NOT marked.

        Returns:
            tuple: (prior_commands_xml_or_none, list_of_system_message_ids_included)
        """
        from app.services.agents.command_service import CommandService

        # Step 1: Find boundary — sequence_number of the most recent agent message
        boundary_stmt = (
            select(func.max(SessionMessage.sequence_number))
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "agent",
            )
        )
        boundary_seq: int = db.exec(boundary_stmt).one_or_none() or -1

        # Step 2: Query eligible command system messages since the boundary
        candidate_stmt = (
            select(SessionMessage)
            .where(
                SessionMessage.session_id == session_id,
                SessionMessage.role == "system",
                SessionMessage.sequence_number > boundary_seq,
            )
            .order_by(SessionMessage.sequence_number)
        )
        candidates = list(db.exec(candidate_stmt).all())

        # Filter to command messages without forwarded_to_llm_at
        eligible = [
            m for m in candidates
            if (m.message_metadata or {}).get("command") is True
            and "forwarded_to_llm_at" not in (m.message_metadata or {})
        ]

        if not eligible:
            return None, []

        command_blocks: list[str] = []
        included_ids: list[UUID] = []
        total_bytes = 0

        for sys_msg in eligible:
            meta = sys_msg.message_metadata or {}
            command_name = meta.get("command_name")
            if not command_name:
                logger.warning(
                    "[NonLLMBridge] session %s: system command message %s missing command_name key",
                    session_id, sys_msg.id,
                )
                continue

            # Normalize colon-form command names (e.g., "/run:check" → "/run")
            # The handler registry stores base names like "/run", but command_name in
            # message_metadata may be the full colon form (e.g., "/run:check").
            lookup_name = command_name.split(":")[0] if ":" in command_name else command_name

            # Check handler opt-in
            handler = CommandService.get_handler(lookup_name)
            if handler is None:
                # Unknown handler — skip safely, do not mark
                continue
            if not handler.include_in_llm_context:
                # Opted out — skip, do not mark
                continue

            # Skip if streaming is still in progress
            if meta.get("streaming_in_progress") is True:
                logger.debug(
                    "[NonLLMBridge] session %s: skipping in-progress command message %s",
                    session_id, sys_msg.id,
                )
                continue

            # Fetch the paired user invocation message
            user_msg: SessionMessage | None = None
            if sys_msg.answers_to_message_id:
                user_msg = db.get(SessionMessage, sys_msg.answers_to_message_id)
            if user_msg is None:
                # Fallback: sequence_number - 1
                fallback_stmt = (
                    select(SessionMessage)
                    .where(
                        SessionMessage.session_id == session_id,
                        SessionMessage.sequence_number == sys_msg.sequence_number - 1,
                        SessionMessage.role == "user",
                    )
                )
                user_msg = db.exec(fallback_stmt).first()

            invocation_text = user_msg.content if user_msg else f"<!-- {command_name} -->"
            at_ts = sys_msg.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if sys_msg.timestamp else ""
            output_text = sys_msg.content or ""

            # Per-block size cap
            output_bytes = output_text.encode("utf-8")
            if len(output_bytes) > NON_LLM_BRIDGE_MAX_PER_BLOCK_BYTES:
                truncated = output_bytes[:NON_LLM_BRIDGE_MAX_PER_BLOCK_BYTES].decode("utf-8", errors="ignore")
                output_text = truncated + NON_LLM_BRIDGE_TRUNCATION_MARKER

            block = (
                f'  <command name="{command_name}" at="{at_ts}">\n'
                f'    <invocation>{invocation_text}</invocation>\n'
                f'    <output>\n{output_text}\n    </output>\n'
                f'  </command>'
            )
            block_bytes = len(block.encode("utf-8"))

            # Total budget check
            if total_bytes + block_bytes > NON_LLM_BRIDGE_MAX_TOTAL_BYTES:
                logger.warning(
                    "[NonLLMBridge] session %s: dropping command '%s' from prior-commands block "
                    "(total budget exceeded)",
                    session_id, command_name,
                )
                continue

            command_blocks.append(block)
            included_ids.append(sys_msg.id)
            total_bytes += block_bytes

        if not command_blocks:
            return None, []

        prior_commands_xml = "<prior_commands>\n" + "\n".join(command_blocks) + "\n</prior_commands>"
        return prior_commands_xml, included_ids

    @staticmethod
    def mark_command_messages_as_forwarded(
        db: Session,
        message_ids: list[UUID],
    ) -> None:
        """
        Mark command system messages as forwarded to the LLM by setting
        message_metadata['forwarded_to_llm_at'] to the current UTC timestamp.

        Uses flag_modified to dirty JSON columns before commit, mirroring
        the existing pattern in stream_processor.py.
        """
        from sqlalchemy.orm.attributes import flag_modified

        if not message_ids:
            return

        ts = datetime.now(UTC).isoformat()
        for msg_id in message_ids:
            msg = db.get(SessionMessage, msg_id)
            if msg:
                meta = dict(msg.message_metadata or {})
                meta["forwarded_to_llm_at"] = ts
                msg.message_metadata = meta
                flag_modified(msg, "message_metadata")
                db.add(msg)

        db.commit()
        logger.info("[NonLLMBridge] Marked %d command messages as forwarded to LLM", len(message_ids))

    @staticmethod
    async def process_pending_messages(session_id: UUID, get_fresh_db_session: callable) -> None:
        """
        Process all pending messages for a session by streaming them to the agent.

        Delegates to ``SessionStreamProcessor`` with a ``WebSocketEventHandler``
        that emits events to the frontend via Socket.IO.

        The processor handles:
        1. Collecting pending messages (with page-context diffing)
        2. Injecting recovery context and one-time webapp context
        3. Marking messages as sent
        4. Streaming from the agent environment
        5. Updating session state on completion

        Args:
            session_id: Session UUID
            get_fresh_db_session: Callable that returns a fresh DB session (context manager)
        """
        logger.info(f"process_pending_messages called for session {session_id}")

        from app.services.sessions.stream_processor import SessionStreamProcessor
        from app.services.sessions.stream_event_handlers import WebSocketEventHandler

        # Quick check: reset session state if no pending messages exist
        with get_fresh_db_session() as db:
            chat_session = db.get(ChatSession, session_id)
            if not chat_session:
                logger.error(f"Session {session_id} not found")
                return
            pending_check, _ = MessageService.collect_pending_messages(db, session_id)
            if not pending_check:
                logger.info(f"No pending messages found for session {session_id}")
                chat_session.pending_messages_count = 0
                chat_session.interaction_status = ""
                db.add(chat_session)
                db.commit()
                return

        handler = WebSocketEventHandler(session_id, get_fresh_db_session)

        processor = SessionStreamProcessor(
            session_id=session_id,
            get_fresh_db_session=get_fresh_db_session,
            event_handler=handler,
            use_session_lock=False,
            ensure_env_ready=False,  # UI path handles env readiness in initiate_stream
            inject_recovery_context=True,
            inject_webapp_context=True,
            log_prefix="[UI]",
        )

        try:
            await processor.process()
        except Exception as e:
            logger.error(f"Error in process_pending_messages for session {session_id}: {e}", exc_info=True)
            await handler.on_error(e)
            raise

    @staticmethod
    def detect_ask_user_question_tool(streaming_events: list[dict]) -> bool:
        """Check if AskUserQuestion tool was called in streaming events"""
        for event in streaming_events:
            if event.get("type") == "tool" and (event.get("tool_name") or "").lower() == "askuserquestion":
                return True
        return False

    @staticmethod
    def get_environment_url(environment: AgentEnvironment) -> str:
        """Get environment base URL from config."""
        container_name = environment.config.get("container_name", f"agent-{environment.id}")
        port = environment.config.get("port", 8000)
        return f"http://{container_name}:{port}"

    @staticmethod
    def get_auth_headers(environment: AgentEnvironment) -> dict:
        """Get authentication headers for environment API calls."""
        auth_token = environment.config.get("auth_token")
        if auth_token:
            return {"Authorization": f"Bearer {auth_token}"}
        return {}

    @staticmethod
    async def forward_interrupt_to_environment(
        base_url: str,
        auth_headers: dict,
        external_session_id: str
    ) -> dict:
        """
        Forward interrupt request to the agent environment.

        Returns:
            dict with status information

        Raises:
            Exception: If communication with environment fails
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{base_url}/chat/interrupt/{external_session_id}",
                    headers=auth_headers
                )

                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Interrupt request sent successfully: {data}")
                    return {
                        "status": "ok",
                        "message": "Interrupt request sent",
                        "external_session_id": external_session_id
                    }
                else:
                    logger.warning(f"Environment returned {response.status_code}: {response.text}")
                    return {
                        "status": "warning",
                        "message": "Interrupt request sent but session may have completed",
                        "external_session_id": external_session_id
                    }

        except httpx.RequestError as e:
            logger.error(f"Failed to send interrupt to environment: {e}")
            raise Exception(f"Failed to communicate with environment: {str(e)}")

    @staticmethod
    async def send_message_to_environment_stream(
        base_url: str,
        auth_headers: dict,
        user_message: str,
        mode: str,
        external_session_id: str | None = None,
        backend_session_id: str | None = None,
        session_state: dict | None = None,
        include_extra_instructions: str | None = None,
        extra_instructions_prepend: str | None = None,
    ) -> AsyncIterator[dict]:
        """
        Send message to environment and stream response.

        Delegates HTTP/SSE logic to agent_env_connector (injectable for testing).

        Args:
            include_extra_instructions: Absolute path (inside agent-core container filesystem) to
                a file whose contents should be inlined into a one-time <extra_instructions> block
                prepended to the user message before the SDK call. Generic mechanism — any feature
                can reuse it by passing a different path.
            extra_instructions_prepend: Optional static text prepended before the file contents
                inside the <extra_instructions> block.
        """
        payload = {
            "message": user_message,
            "mode": mode,
            "session_id": external_session_id,
            "backend_session_id": backend_session_id,
        }
        if session_state:
            payload["session_state"] = session_state
        if include_extra_instructions:
            payload["include_extra_instructions"] = include_extra_instructions
        if extra_instructions_prepend:
            payload["extra_instructions_prepend"] = extra_instructions_prepend

        async for event in agent_env_connector.stream_chat(
            base_url=base_url,
            auth_headers=auth_headers,
            payload=payload,
        ):
            yield event

    @staticmethod
    async def enrich_messages_with_streaming(
        messages: list[MessagePublic],
        session_id: UUID,
    ) -> list[MessagePublic]:
        """
        Merge in-memory streaming events into message list.

        If the session has an active stream, patches the in-progress message
        with new streaming events and accumulated content from the stream manager.
        """
        if not await active_streaming_manager.is_streaming(session_id):
            return messages

        stream_data = await active_streaming_manager.get_stream_events(session_id)
        if not stream_data or not stream_data["streaming_events"]:
            return messages

        # Find the in-progress message
        in_progress_msg = None
        for msg in messages:
            if msg.message_metadata and msg.message_metadata.get("streaming_in_progress"):
                in_progress_msg = msg
                break

        if in_progress_msg:
            db_events = in_progress_msg.message_metadata.get("streaming_events", [])
            db_max_seq = max((e.get("event_seq", 0) for e in db_events), default=0)
            new_events = [
                e for e in stream_data["streaming_events"]
                if e.get("event_seq", 0) > db_max_seq
            ]
            if new_events:
                in_progress_msg.message_metadata["streaming_events"] = db_events + new_events
            if stream_data["accumulated_content"]:
                in_progress_msg.content = stream_data["accumulated_content"]

        return messages

    @staticmethod
    async def interrupt_stream(
        db_session: Session,
        session_id: UUID,
        environment_id: UUID | None,
    ) -> dict:
        """
        Interrupt an active streaming message.

        Handles the full flow: request interrupt via active_streaming_manager,
        check for pending state, and forward to agent environment if needed.

        Access control: **the caller must authorize session access before
        calling this method.** This method trusts its inputs and does not
        re-check ownership, sharing, or scope. Callers today:
        ``messages.interrupt_message`` (UI/guest via ``_verify_message_access``),
        ``webapp_chat.interrupt_message`` (webapp-share scope), and
        ``A2ARequestHandler.handle_tasks_cancel`` (A2A scope via
        ``_authorize_existing_session``). Any new caller must follow suit.

        Detached sessions (environment_id is None, e.g. after the environment
        was deleted) have nothing to forward to — treated as "no active stream"
        so callers convert to 400 via their existing ValueError handling.

        Returns:
            dict with status, message, session_id, and queued flag.

        Raises:
            ValueError: If no active stream to interrupt, or session is detached.
            Exception: If environment not found or communication fails.
        """
        if environment_id is None:
            raise ValueError("No active stream to interrupt")

        interrupt_info = await active_streaming_manager.request_interrupt(session_id)

        if not interrupt_info["found"]:
            raise ValueError("No active stream to interrupt")

        # Command stream interrupt — forward to agent-env command interrupt endpoint
        if interrupt_info.get("stream_type") == "command":
            exec_id = interrupt_info.get("exec_id")
            if exec_id:
                environment = db_session.get(AgentEnvironment, environment_id)
                if environment:
                    base_url = MessageService.get_environment_url(environment)
                    auth_headers = MessageService.get_auth_headers(environment)
                    await agent_env_connector.interrupt_command(base_url, auth_headers, exec_id)
            return {
                "status": "ok",
                "message": "Command interrupt sent",
                "session_id": str(session_id),
                "queued": False,
            }

        if interrupt_info["pending"]:
            return {
                "status": "ok",
                "message": "Interrupt queued",
                "session_id": str(session_id),
                "queued": True,
            }

        external_session_id = interrupt_info["external_session_id"]
        environment = db_session.get(AgentEnvironment, environment_id)
        if not environment:
            raise ValueError("Environment not found")

        base_url = MessageService.get_environment_url(environment)
        auth_headers = MessageService.get_auth_headers(environment)

        result = await MessageService.forward_interrupt_to_environment(
            base_url=base_url,
            auth_headers=auth_headers,
            external_session_id=external_session_id,
        )
        return {**result, "session_id": str(session_id), "queued": False}

    @staticmethod
    def build_stream_response(session_id: UUID, result: dict) -> dict:
        """
        Build a standardized response dict from SessionService.send_session_message() result.

        Used by both regular message and webapp chat routes.
        """
        if result["action"] == "command_executed":
            return {
                "status": "ok",
                "session_id": str(session_id),
                "stream_room": f"session_{session_id}_stream",
                "message": "Command executed",
                "command_executed": True,
            }

        if result["action"] == "queued":
            return {
                "status": "ok",
                "session_id": str(session_id),
                "stream_room": f"session_{session_id}_stream",
                "message": "Command queued",
                "queued": True,
            }

        response = {
            "status": "ok",
            "session_id": str(session_id),
            "stream_room": f"session_{session_id}_stream",
        }

        if result["action"] == "streaming":
            response["message"] = result["message"]
            response["streaming"] = True
        elif result["action"] == "pending":
            response["message"] = result["message"]
            response["pending"] = True
        else:
            response["message"] = result.get("message", "Message received")

        if result.get("files_attached"):
            response["files_attached"] = result["files_attached"]

        return response

    @staticmethod
    def _resolve_stream_context(
        db: Session,
        session_id: UUID,
        environment_id: UUID,
    ) -> StreamContext:
        """
        Resolve all context needed before starting a stream.

        Loads session, environment, agent; builds session_context;
        resets result_state if needed. Returns a StreamContext NamedTuple.
        """
        session_db = db.get(ChatSession, session_id)
        user_id = session_db.user_id if session_db else None
        allowed_tools = set()
        previous_result_state = None
        session_context = None
        env_base_url = ""
        env_auth_headers = {}
        env_auth_token = None

        if session_db:
            env = db.get(AgentEnvironment, environment_id)
            agent = None
            if env:
                env_base_url = MessageService.get_environment_url(env)
                env_auth_headers = MessageService.get_auth_headers(env)
                env_auth_token = env.config.get("auth_token") if env.config else None

                if env.agent_id:
                    agent = db.get(Agent, env.agent_id)
                    if agent and agent.agent_sdk_config:
                        allowed_tools = set(agent.agent_sdk_config.get("allowed_tools", []))

            session_context = _build_session_context(db, session_db, env, agent)

            # Reset result_state when user sends a new message
            if session_db.result_state is not None:
                previous_result_state = session_db.result_state
                session_db.result_state = None
                session_db.result_summary = None
                db.add(session_db)
                db.commit()
                logger.info(
                    f"Reset result_state '{previous_result_state}' for session {session_id}"
                )

                # Sync task status if session is linked to a task
                if session_db.source_task_id:
                    from app.services.tasks.input_task_service import InputTaskService
                    InputTaskService.sync_task_status_from_sessions(
                        db_session=db, task_id=session_db.source_task_id
                    )

        return StreamContext(
            user_id=user_id,
            allowed_tools=allowed_tools,
            previous_result_state=previous_result_state,
            session_context=session_context,
            base_url=env_base_url,
            auth_headers=env_auth_headers,
            env_auth_token=env_auth_token,
        )

    @staticmethod
    async def _handle_session_id_capture(
        session_id: UUID,
        new_external_session_id: str,
        base_url: str,
        auth_headers: dict,
        get_fresh_db_session: callable,
    ) -> None:
        """
        Handle first capture of external_session_id during streaming.

        Updates ActiveStreamingManager, forwards any pending interrupt,
        and persists the ID to the database.
        """
        # Update active streaming manager — returns True if there's a pending interrupt
        interrupt_pending = await active_streaming_manager.update_external_session_id(
            session_id=session_id,
            external_session_id=new_external_session_id,
        )

        # If interrupt was requested before external_session_id was available, forward it now
        if interrupt_pending:
            logger.info(f"Forwarding pending interrupt to agent env for session {session_id}")
            try:
                await MessageService.forward_interrupt_to_environment(
                    base_url=base_url,
                    auth_headers=auth_headers,
                    external_session_id=new_external_session_id,
                )
            except Exception as e:
                logger.error(f"Error forwarding pending interrupt: {e}")

        # Store external session ID in DB
        def _store_session_id():
            with get_fresh_db_session() as db:
                from app.services.sessions.session_service import SessionService
                chat_session_db = db.get(ChatSession, session_id)
                if chat_session_db:
                    SessionService.set_external_session_id(
                        db=db,
                        session=chat_session_db,
                        external_session_id=new_external_session_id,
                    )
        await asyncio.to_thread(_store_session_id)

    @staticmethod
    async def _handle_tools_init_event(
        event: dict,
        environment_id: UUID,
        get_fresh_db_session: callable,
    ) -> None:
        """Update agent's sdk_tools from a tools_init system event."""
        tools_list = event.get("data", {}).get("tools", [])
        if not tools_list:
            return

        def _update_sdk_tools():
            with get_fresh_db_session() as db:
                env = db.get(AgentEnvironment, environment_id)
                if env and env.agent_id:
                    try:
                        AgentService.update_sdk_tools(
                            session=db,
                            agent_id=env.agent_id,
                            tools=tools_list,
                        )
                        logger.info(f"Updated sdk_tools for agent {env.agent_id} with {len(tools_list)} tools")
                    except Exception as e:
                        logger.error(f"Failed to update sdk_tools: {e}")
        await asyncio.to_thread(_update_sdk_tools)

    @staticmethod
    async def _handle_tool_event(
        event: dict,
        agent_allowed_tools: set,
        tools_needing_approval: set,
        session_id: UUID,
        user_id: UUID | None,
        get_fresh_db_session: callable,
    ) -> None:
        """
        Process tool events: approval flagging, TodoWrite progress tracking.

        Mutates event dict (adds metadata flags) and tools_needing_approval set.
        """
        from app.models.events.event import EventType

        tool_name = event["tool_name"]
        # Normalize to lowercase for comparison (adapters should already emit
        # lowercase, but agent_allowed_tools in DB may contain legacy PascalCase)
        tool_name_lower = tool_name.lower()

        # Check if tool needs approval (not in pre-allowed or agent's allowed_tools)
        agent_allowed_lower = {t.lower() for t in agent_allowed_tools}
        needs_approval = (
            tool_name_lower not in PRE_ALLOWED_TOOLS and
            tool_name_lower not in agent_allowed_lower
        )
        if needs_approval:
            if "metadata" not in event:
                event["metadata"] = {}
            event["metadata"]["needs_approval"] = True
            event["metadata"]["tool_name"] = tool_name
            tools_needing_approval.add(tool_name)
            logger.info(f"Tool '{tool_name}' flagged as needing approval")

        # Detect TodoWrite tool calls for progress tracking
        if tool_name.lower() == "todowrite":
            tool_input = event.get("metadata", {}).get("tool_input", {})
            todos = tool_input.get("todos", [])
            if todos:
                def _update_todo_progress():
                    with get_fresh_db_session() as db:
                        session_db = db.get(ChatSession, session_id)
                        if session_db:
                            session_db.todo_progress = todos
                            db.add(session_db)
                            db.commit()
                            logger.info(f"Updated todo_progress for session {session_id} with {len(todos)} items")
                await asyncio.to_thread(_update_todo_progress)

                await _emit_activity_event(
                    event_type=EventType.TODO_LIST_UPDATED,
                    session_id=session_id,
                    environment_id=UUID(int=0),  # not relevant for this event
                    session_mode="",
                    user_id=user_id,
                    todos=todos,
                )

    @staticmethod
    async def _flush_streaming_to_db(
        agent_message_id: UUID,
        streaming_events: list[dict],
        response_metadata: dict,
        get_fresh_db_session: callable,
        session_id: UUID,
    ) -> None:
        """Periodic flush of streaming content to DB for crash recovery."""
        flush_events = list(streaming_events)
        flush_content = "".join(
            e["content"] for e in flush_events
            if e["type"] == "assistant" and e.get("content")
        ) or "Agent is responding..."

        flush_metadata = dict(response_metadata)

        def _flush(
            msg_id=agent_message_id,
            content=flush_content,
            events=flush_events,
            metadata=flush_metadata,
        ):
            from sqlalchemy.orm.attributes import flag_modified
            with get_fresh_db_session() as db:
                agent_msg = db.get(SessionMessage, msg_id)
                if agent_msg:
                    agent_msg.content = content
                    metadata["streaming_in_progress"] = True
                    metadata["streaming_events"] = events
                    agent_msg.message_metadata = metadata
                    flag_modified(agent_msg, "message_metadata")
                    db.add(agent_msg)
                    db.commit()

        await asyncio.to_thread(_flush)
        flush_seq = streaming_events[-1].get("event_seq", 0) if streaming_events else 0
        await active_streaming_manager.update_last_flushed_seq(session_id, flush_seq)
        logger.debug(f"Flushed streaming content to DB (seq={flush_seq}) for session {session_id}")

    @staticmethod
    async def _flush_command_stream_to_db(
        system_message_id: UUID,
        streaming_events: list[dict],
        command_metadata: dict,
        get_fresh_db_session: callable,
        session_id: UUID,
    ) -> None:
        """Periodic flush of command stream events to DB for crash recovery.

        Mirrors _flush_streaming_to_db but is tailored for command output:
        - Assembles in-progress content from tool_result_delta events
        - Does not accumulate assistant text (commands have no LLM output)
        """
        flush_events = list(streaming_events)
        # Build in-progress content from tool_result_delta events
        output_chunks = [
            e["content"] for e in flush_events
            if e.get("type") == "tool_result_delta" and e.get("content")
        ]
        flush_content = "".join(output_chunks) or "Command is running..."

        flush_metadata = dict(command_metadata)

        def _flush(
            msg_id=system_message_id,
            content=flush_content,
            events=flush_events,
            metadata=flush_metadata,
        ):
            from sqlalchemy.orm.attributes import flag_modified
            with get_fresh_db_session() as db:
                msg = db.get(SessionMessage, msg_id)
                if msg:
                    msg.content = content
                    metadata["streaming_in_progress"] = True
                    metadata["streaming_events"] = events
                    msg.message_metadata = metadata
                    flag_modified(msg, "message_metadata")
                    db.add(msg)
                    db.commit()

        await asyncio.to_thread(_flush)
        flush_seq = flush_events[-1].get("event_seq", 0) if flush_events else 0
        await active_streaming_manager.update_last_flushed_seq(session_id, flush_seq)
        logger.debug(
            f"Flushed command stream to DB (seq={flush_seq}) for session {session_id}"
        )

    @staticmethod
    async def stream_command_via_agent_env(
        session_id: UUID,
        environment_id: UUID,
        resolved_command: str,
        command_name: str,
        user_message_id: UUID,
        get_fresh_db_session: callable,
        event_handler,
    ) -> None:
        """
        Stream a CLI command execution through agent-env and persist the output.

        This method mirrors stream_message_with_events in structure but routes to
        POST /command/stream on agent-env instead of POST /chat/stream.

        Key differences from the LLM streaming path:
        - No SDK session management (_get_session_context_and_reset_state not called)
        - No external_session_id tracking
        - No LLM token usage
        - Events are tool / tool_result_delta / system (done) shaped

        Args:
            session_id: Backend session UUID.
            environment_id: Agent environment UUID.
            resolved_command: Shell command string to execute.
            command_name: Human-readable command label (e.g., "/run:check").
            user_message_id: The pending user message that triggered this command.
            get_fresh_db_session: Callable returning a fresh DB session context manager.
            event_handler: StreamEventHandler implementation for WS/MCP/A2A delivery.
        """
        from uuid import uuid4
        from app.services.events.event_service import event_service
        from app.core.config import settings

        exec_id = str(uuid4())
        system_message_id: UUID | None = None
        streaming_events: list[dict] = []
        event_seq_counter = 0
        last_flush_time = time.time()
        FLUSH_INTERVAL = 2.0
        total_bytes = 0
        was_interrupted = False
        was_error = False
        exec_exit_code: int | None = None
        exec_duration_seconds: float | None = None
        exec_timed_out = False
        exec_truncated = False
        truncation_interrupt_sent = False

        # --- Resolve environment URL and auth ---
        def _get_env_url_and_auth():
            with get_fresh_db_session() as db:
                env = db.get(AgentEnvironment, environment_id)
                if not env:
                    return None, None
                return (
                    MessageService.get_environment_url(env),
                    MessageService.get_auth_headers(env),
                )

        base_url, auth_headers = await asyncio.to_thread(_get_env_url_and_auth)
        if not base_url:
            logger.error(f"stream_command_via_agent_env: environment {environment_id} not found")
            return

        # --- Create system message placeholder ---
        command_metadata: dict = {
            "command": True,
            "command_name": command_name,
            "routing": "command_stream",
            "synthesized": True,
            "resolved_command": resolved_command,
            "streaming_in_progress": True,
            "streaming_events": [],
        }

        def _create_system_message():
            with get_fresh_db_session() as db:
                msg = MessageService.create_message(
                    session=db,
                    session_id=session_id,
                    role="system",
                    content="",
                    sent_to_agent_status="sent",
                    answers_to_message_id=user_message_id,
                    message_metadata=dict(command_metadata),
                )
                return msg.id

        system_message_id = await asyncio.to_thread(_create_system_message)

        # --- Register stream with ActiveStreamingManager ---
        await active_streaming_manager.register_stream(session_id, external_session_id=None)
        await active_streaming_manager.update_stream_type(
            session_id, stream_type="command", exec_id=exec_id
        )

        logger.info(
            f"[Command] Starting command stream for session {session_id} "
            f"(exec_id={exec_id}, command={command_name})"
        )

        # --- Emit tool invocation event ---
        event_seq_counter += 1
        tool_event: dict = {
            "type": "tool",
            "tool_name": "bash",
            "content": command_name,
            "event_seq": event_seq_counter,
            "metadata": {
                "tool_id": exec_id,
                "tool_input": {"command": resolved_command},
                "tool_name": "bash",
                "synthesized": True,
            },
        }
        streaming_events.append(tool_event)
        await active_streaming_manager.append_streaming_event(session_id, tool_event)
        await event_handler.on_event(tool_event)

        # --- Stream from agent-env ---
        try:
            async for raw_event in agent_env_connector.stream_command(
                base_url=base_url,
                auth_headers=auth_headers,
                exec_id=exec_id,
                resolved_command=resolved_command,
                timeout=settings.RUN_COMMAND_TIMEOUT_SECONDS,
                max_output_bytes=settings.RUN_COMMAND_MAX_OUTPUT_BYTES,
            ):
                event_type = raw_event.get("type", "")

                # Check if backend-side interrupt was requested
                stream_info = await active_streaming_manager.get_stream_events(session_id)
                if stream_info is None:
                    # Stream was unregistered externally
                    break
                active_stream = await active_streaming_manager.get_stream_info(session_id)
                if active_stream and active_stream.get("is_interrupted"):
                    was_interrupted = True
                    # Send interrupt to agent-env if not already done
                    if not truncation_interrupt_sent:
                        await agent_env_connector.interrupt_command(base_url, auth_headers, exec_id)
                    break

                if event_type == "done":
                    exec_exit_code = raw_event.get("exit_code")
                    exec_duration_seconds = raw_event.get("duration_seconds")
                    exec_timed_out = bool(raw_event.get("timed_out", False))
                    break

                if event_type == "interrupted":
                    was_interrupted = True
                    exec_exit_code = raw_event.get("exit_code", -1)
                    break

                if event_type == "error":
                    was_error = True
                    error_content = raw_event.get("content", "Command execution failed")
                    event_seq_counter += 1
                    error_event = {
                        "type": "tool_result_delta",
                        "content": f"\n[Error: {error_content}]",
                        "event_seq": event_seq_counter,
                        "metadata": {
                            "tool_id": exec_id,
                            "stream": "stderr",
                        },
                    }
                    streaming_events.append(error_event)
                    await active_streaming_manager.append_streaming_event(session_id, error_event)
                    await event_handler.on_event(error_event)
                    break

                if event_type == "tool_result_delta":
                    chunk_content = raw_event.get("content", "")
                    chunk_bytes = len(chunk_content.encode("utf-8"))
                    total_bytes += chunk_bytes

                    # Output cap enforcement
                    if total_bytes > settings.RUN_COMMAND_MAX_OUTPUT_BYTES:
                        if not truncation_interrupt_sent:
                            truncation_interrupt_sent = True
                            exec_truncated = True
                            await agent_env_connector.interrupt_command(
                                base_url, auth_headers, exec_id
                            )
                        # Emit truncation marker
                        event_seq_counter += 1
                        trunc_event = {
                            "type": "tool_result_delta",
                            "content": f"\n[...output truncated at {settings.RUN_COMMAND_MAX_OUTPUT_BYTES // 1024} KB]",
                            "event_seq": event_seq_counter,
                            "metadata": {
                                "tool_id": exec_id,
                                "stream": "stderr",
                            },
                        }
                        streaming_events.append(trunc_event)
                        await active_streaming_manager.append_streaming_event(session_id, trunc_event)
                        await event_handler.on_event(trunc_event)
                        break

                    event_seq_counter += 1
                    stored_event = {
                        "type": "tool_result_delta",
                        "content": chunk_content,
                        "event_seq": event_seq_counter,
                        "metadata": {
                            "tool_id": exec_id,
                            "stream": raw_event.get("metadata", {}).get("stream", "stdout"),
                        },
                    }
                    streaming_events.append(stored_event)
                    await active_streaming_manager.append_streaming_event(session_id, stored_event)
                    await event_handler.on_event(stored_event)

                # Periodic flush to DB
                if system_message_id and (time.time() - last_flush_time >= FLUSH_INTERVAL):
                    await MessageService._flush_command_stream_to_db(
                        system_message_id=system_message_id,
                        streaming_events=streaming_events,
                        command_metadata=command_metadata,
                        get_fresh_db_session=get_fresh_db_session,
                        session_id=session_id,
                    )
                    last_flush_time = time.time()

        except Exception as exc:
            logger.error(
                f"[Command] Unexpected error during command stream for session {session_id}: {exc}",
                exc_info=True,
            )
            was_error = True

        # --- Build done event ---
        done_summary_parts: list[str] = []
        if exec_timed_out:
            done_summary_parts.append("timed out")
        elif was_interrupted:
            done_summary_parts.append("interrupted")
        elif was_error:
            done_summary_parts.append("error")
        elif exec_exit_code is not None:
            done_summary_parts.append(f"exit {exec_exit_code}")
        else:
            done_summary_parts.append("done")

        duration_str = f" ({exec_duration_seconds:.1f}s)" if exec_duration_seconds is not None else ""
        done_content = f"Command {', '.join(done_summary_parts)}{duration_str}"

        event_seq_counter += 1
        done_event: dict = {
            "type": "system",
            "content": done_content,
            "event_seq": event_seq_counter,
            "metadata": {
                "tool_id": exec_id,
                "exit_code": exec_exit_code,
                "duration_seconds": exec_duration_seconds,
                "command_done": True,
            },
        }
        streaming_events.append(done_event)
        await event_handler.on_event(done_event)

        # --- Build final message content ---
        output_chunks = [
            e["content"] for e in streaming_events
            if e.get("type") == "tool_result_delta" and e.get("content")
        ]
        full_output = "".join(output_chunks)

        resolved_display = (
            resolved_command[:80] + "..." if len(resolved_command) > 80 else resolved_command
        )
        exit_label = f"exit {exec_exit_code}" if exec_exit_code is not None else "?"
        if exec_timed_out:
            exit_label = "timed out"
        elif was_interrupted:
            exit_label = "interrupted"

        duration_label = f" · {exec_duration_seconds:.1f}s" if exec_duration_seconds is not None else ""
        final_content = (
            f"**Command**: `{command_name}` → `{resolved_display}`\n\n"
            f"```\n{full_output}\n```\n\n"
            f"**Exit**: {exit_label}{duration_label}"
        )

        # --- Finalize system message ---
        final_metadata = {
            "command": True,
            "command_name": command_name,
            "routing": "command_stream",
            "synthesized": True,
            "resolved_command": resolved_command,
            "streaming_in_progress": False,
            "streaming_events": streaming_events,
            "exec_exit_code": exec_exit_code,
            "exec_duration_seconds": exec_duration_seconds,
            "exec_timed_out": exec_timed_out,
            "exec_interrupted": was_interrupted,
            "exec_truncated": exec_truncated,
        }

        message_status = ""
        if exec_timed_out:
            message_status = "error"
        elif was_interrupted:
            message_status = "user_interrupted"
        elif was_error:
            message_status = "error"

        def _finalize():
            from sqlalchemy.orm.attributes import flag_modified
            with get_fresh_db_session() as db:
                if system_message_id:
                    msg = db.get(SessionMessage, system_message_id)
                    if msg:
                        msg.content = final_content
                        msg.message_metadata = final_metadata
                        flag_modified(msg, "message_metadata")
                        if message_status:
                            msg.status = message_status
                        db.add(msg)
                        db.commit()

        await asyncio.to_thread(_finalize)

        # --- Emit post-action event for event-driven handlers ---
        # (session_interaction_status_changed WS fanout, activity log,
        # CLI commands / agent status cache refresh, task status sync, etc.)
        # Order matches stream_message_with_events: emit first, then unregister.
        from app.models.events.event import EventType

        def _get_session_post_action_info():
            with get_fresh_db_session() as db:
                session_db = db.get(ChatSession, session_id)
                env_db = db.get(AgentEnvironment, environment_id)
                return (
                    session_db.user_id if session_db else None,
                    (session_db.mode or "conversation") if session_db else "conversation",
                    str(env_db.agent_id) if env_db and env_db.agent_id else None,
                )

        post_user_id, post_session_mode, post_agent_id = await asyncio.to_thread(
            _get_session_post_action_info
        )

        if was_interrupted:
            post_event_type = EventType.STREAM_INTERRUPTED
            post_extra: dict = {}
        elif was_error or exec_timed_out:
            post_event_type = EventType.STREAM_ERROR
            post_extra = {
                "error_type": "CommandTimedOut" if exec_timed_out else "CommandError",
                "error_message": (
                    "Command execution timed out"
                    if exec_timed_out else "Command execution failed"
                ),
            }
        else:
            post_event_type = EventType.STREAM_COMPLETED
            post_extra = {"was_interrupted": False}

        await _emit_activity_event(
            post_event_type, session_id, environment_id,
            post_session_mode, post_user_id,
            agent_id=post_agent_id,
            **post_extra,
        )

        # --- Unregister stream ---
        await active_streaming_manager.unregister_stream(session_id)

        logger.info(
            f"[Command] Command stream complete for session {session_id} "
            f"(exec_id={exec_id}, exit={exec_exit_code}, "
            f"interrupted={was_interrupted}, error={was_error})"
        )

    @staticmethod
    async def _finalize_agent_message(
        agent_message_id: UUID | None,
        session_id: UUID,
        agent_content: str,
        response_metadata: dict,
        tool_questions_status: str | None,
        was_interrupted: bool,
        get_fresh_db_session: callable,
    ) -> None:
        """Save or update the final agent message after stream completes."""
        def _save_or_update():
            from sqlalchemy.orm.attributes import flag_modified
            response_metadata["streaming_in_progress"] = False
            status = "user_interrupted" if was_interrupted else ""
            status_msg = "Interrupted by user" if was_interrupted else None
            with get_fresh_db_session() as db:
                if agent_message_id:
                    agent_message = db.get(SessionMessage, agent_message_id)
                    if agent_message:
                        agent_message.content = agent_content
                        agent_message.message_metadata = response_metadata
                        flag_modified(agent_message, "message_metadata")
                        agent_message.tool_questions_status = tool_questions_status
                        agent_message.status = status
                        agent_message.status_message = status_msg
                        db.add(agent_message)
                        db.commit()
                        logger.info(f"Updated agent message {agent_message_id} with final content")
                        return
                    else:
                        logger.error(f"Agent message {agent_message_id} not found for update, creating new one")

                # Fallback: create new message
                MessageService.create_message(
                    session=db,
                    session_id=session_id,
                    role="agent",
                    content=agent_content,
                    message_metadata=response_metadata,
                    tool_questions_status=tool_questions_status,
                    status=status,
                    status_message=status_msg,
                )
        await asyncio.to_thread(_save_or_update)

    @staticmethod
    async def stream_message_with_events(
        session_id: UUID,
        environment_id: UUID,
        user_message_content: str,
        session_mode: str,
        external_session_id: str | None,
        get_fresh_db_session: callable,
        include_extra_instructions: str | None = None,
        extra_instructions_prepend: str | None = None,
    ) -> AsyncIterator[dict]:
        """
        Stream message to environment and handle all business logic.

        This method:
        - Resolves environment URL and auth headers from environment_id
        - Streams message to environment
        - Handles session ID capture
        - Saves messages to database
        - Updates session status
        - Syncs agent prompts (for building mode)
        - Yields SSE events for frontend

        Args:
            include_extra_instructions: When set, forwarded to agent-core as an absolute path to
                a file whose contents are inlined into a one-time <extra_instructions> block that
                is prepended to the user message before the SDK call.
            extra_instructions_prepend: Optional static text prepended before the file contents
                inside the <extra_instructions> block.
        """
        from app.models.events.event import EventType

        # Register this as an active stream BEFORE starting
        await active_streaming_manager.register_stream(
            session_id=session_id,
            external_session_id=external_session_id
        )

        # Resolve all context needed for streaming
        def _resolve_context():
            with get_fresh_db_session() as db:
                return MessageService._resolve_stream_context(db, session_id, environment_id)

        ctx = await asyncio.to_thread(_resolve_context)

        # Emit STREAM_STARTED event for activity tracking
        await _emit_activity_event(
            EventType.STREAM_STARTED, session_id, environment_id, session_mode, ctx.user_id
        )

        # Variables to collect agent response
        streaming_events = []
        new_external_session_id = external_session_id
        was_interrupted = False
        agent_message_id = None
        tools_needing_approval = set()
        event_seq_counter = 0
        last_flush_time = time.time()
        FLUSH_INTERVAL = 2.0
        response_metadata = {
            "external_session_id": external_session_id,
            "mode": session_mode,
        }

        # Webapp action tracking: accumulate assistant text to scan for action tags mid-stream.
        # We track how many characters of the buffer have already been scanned so we only
        # scan the newly arrived suffix (avoiding O(n²) rescanning on long responses).
        accumulated_assistant_content: str = ""
        webapp_action_scan_offset: int = 0

        # Build session_state for agent-env
        session_state = None
        if ctx.previous_result_state or ctx.session_context:
            session_state = {}
            if ctx.previous_result_state:
                session_state["previous_result_state"] = ctx.previous_result_state
            if ctx.session_context:
                session_state["session_context"] = ctx.session_context
                # HMAC-sign context so agent-env can verify authenticity
                if ctx.env_auth_token:
                    session_state["session_context_signature"] = sign_session_context(
                        ctx.session_context, ctx.env_auth_token
                    )

        try:
            # Stream from environment
            async for event in MessageService.send_message_to_environment_stream(
                base_url=ctx.base_url,
                auth_headers=ctx.auth_headers,
                user_message=user_message_content,
                mode=session_mode,
                external_session_id=external_session_id,
                backend_session_id=str(session_id),
                session_state=session_state,
                include_extra_instructions=include_extra_instructions,
                extra_instructions_prepend=extra_instructions_prepend,
            ):
                # Handle interrupted events
                if event.get("type") == "interrupted":
                    was_interrupted = True
                    logger.info("Message was interrupted by user")
                    await _emit_activity_event(
                        EventType.STREAM_INTERRUPTED, session_id, environment_id,
                        session_mode, ctx.user_id,
                    )
                    yield event
                    break

                # Handle error events from message service
                if event.get("type") == "error":
                    error_content = event.get("content", "Unknown error occurred")
                    error_type = event.get("error_type", "Error")

                    # Check if this is a corrupted session error
                    if event.get("session_corrupted"):
                        logger.warning(f"Session {session_id} has corrupted external_session_id, clearing it")
                        def _clear_corrupted_session():
                            with get_fresh_db_session() as db:
                                from app.services.sessions.session_service import SessionService
                                chat_session_db = db.get(ChatSession, session_id)
                                if chat_session_db:
                                    SessionService.set_external_session_id(
                                        db=db,
                                        session=chat_session_db,
                                        external_session_id=None
                                    )
                        await asyncio.to_thread(_clear_corrupted_session)

                    # Save error as a system message in the chat
                    def _save_error_message():
                        with get_fresh_db_session() as db:
                            MessageService.create_message(
                                session=db,
                                session_id=session_id,
                                role="system",
                                content=f"⚠️ {error_content}",
                                message_metadata={"error_type": error_type},
                                status="error",
                            )
                    await asyncio.to_thread(_save_error_message)

                    await _emit_activity_event(
                        EventType.STREAM_ERROR, session_id, environment_id,
                        session_mode, ctx.user_id,
                        error_type=error_type, error_message=error_content,
                    )

                    yield event
                    return

                # Capture external session ID from first event that has it
                if not new_external_session_id:
                    event_session_id = event.get("session_id") or event.get("metadata", {}).get("session_id")
                    logger.debug(f"Event type={event.get('type')}, session_id={event.get('session_id')}, metadata.session_id={event.get('metadata', {}).get('session_id')}")
                    if event_session_id:
                        new_external_session_id = event_session_id
                        logger.info(f"External session ID captured early from event type={event.get('type')}: {new_external_session_id}")

                        await MessageService._handle_session_id_capture(
                            session_id=session_id,
                            new_external_session_id=new_external_session_id,
                            base_url=ctx.base_url,
                            auth_headers=ctx.auth_headers,
                            get_fresh_db_session=get_fresh_db_session,
                        )

                # Handle tools_init event - update agent's sdk_tools
                if event.get("type") == "system" and event.get("subtype") == "tools_init":
                    await MessageService._handle_tools_init_event(
                        event, environment_id, get_fresh_db_session,
                    )
                    # Don't forward this event to frontend - it's internal
                    continue

                # Check tool events for approval status and TodoWrite progress
                if event.get("type") == "tool" and event.get("tool_name"):
                    await MessageService._handle_tool_event(
                        event=event,
                        agent_allowed_tools=ctx.allowed_tools,
                        tools_needing_approval=tools_needing_approval,
                        session_id=session_id,
                        user_id=ctx.user_id,
                        get_fresh_db_session=get_fresh_db_session,
                    )

                # Detect webapp_action tags mid-stream from assistant content.
                # Accumulate text and scan only the newly arrived suffix for complete tags.
                if event.get("type") == "assistant" and event.get("content"):
                    accumulated_assistant_content += event["content"]
                    # Only scan the region that contains newly arrived content.
                    # We look back _WEBAPP_ACTION_TAG_RE's max tag length to catch tags
                    # that straddle the previous scan boundary.  Using a generous 2 KB
                    # lookback is safe and avoids missed tags at chunk boundaries.
                    scan_start = max(0, webapp_action_scan_offset - 2048)
                    new_suffix = accumulated_assistant_content[scan_start:]
                    new_actions, _ = _extract_webapp_actions(new_suffix)
                    if new_actions:
                        await _emit_webapp_action_events(session_id, new_actions)
                    # Advance the scan offset so the next chunk only rescans the lookback window
                    webapp_action_scan_offset = len(accumulated_assistant_content)

                # Store raw event for visualization (exclude done/error events from storage)
                if event.get("type") not in ["done", "error", "session_created"]:
                    event_seq_counter += 1
                    event_copy = {
                        "type": event.get("type"),
                        "content": event.get("content", ""),
                        "event_seq": event_seq_counter,
                    }
                    if event.get("tool_name"):
                        event_copy["tool_name"] = event["tool_name"]
                    if event.get("metadata"):
                        event_copy["metadata"] = {
                            k: v for k, v in event["metadata"].items()
                            if k in _STORED_EVENT_METADATA_KEYS
                        }
                    streaming_events.append(event_copy)

                    # Append to ActiveStreamingManager buffer for API access
                    await active_streaming_manager.append_streaming_event(session_id, event_copy)

                    # Include event_seq in the event forwarded to frontend via WS
                    event["event_seq"] = event_seq_counter

                    # Periodic flush to DB (non-blocking)
                    if agent_message_id and (time.time() - last_flush_time >= FLUSH_INTERVAL):
                        await MessageService._flush_streaming_to_db(
                            agent_message_id, streaming_events, response_metadata,
                            get_fresh_db_session, session_id,
                        )
                        last_flush_time = time.time()

                # Create agent message in DB as soon as we receive the first "assistant" event
                # This ensures the message exists BEFORE any tool calls execute (like handover)
                if event.get("type") == "assistant" and agent_message_id is None:
                    def _create_initial_agent_message():
                        with get_fresh_db_session() as db:
                            initial_content = event.get("content", "Agent is responding...")
                            initial_metadata = {
                                "external_session_id": new_external_session_id,
                                "mode": session_mode,
                                "streaming_in_progress": True
                            }
                            message = MessageService.create_message(
                                session=db,
                                session_id=session_id,
                                role="agent",
                                content=initial_content,
                                message_metadata=initial_metadata,
                            )
                            return message.id

                    agent_message_id = await asyncio.to_thread(_create_initial_agent_message)
                    logger.info(f"Created initial agent message {agent_message_id} on first assistant event")

                # Collect metadata from events
                event_metadata = event.get("metadata", {})
                if event_metadata:
                    response_metadata.update({
                        k: v for k, v in event_metadata.items()
                        if k in _FORWARDED_METADATA_KEYS
                    })

                # Forward event to frontend (except 'done' - we'll send our own after saving message)
                if event.get("type") != "done":
                    yield event

            # After stream completes, save agent response to database
            if streaming_events:
                text_parts = [e["content"] for e in streaming_events if e["type"] == "assistant" and e.get("content")]
                agent_content = "".join(text_parts) if text_parts else "Agent response"

                # Emit any webapp_action tags that appear in the final assembled content
                # but were not caught mid-stream (e.g. they spanned two chunk boundaries
                # in a way that the lookback window missed them, or the stream ended before
                # the next scan).  We do a fresh extract on the full assembled content and
                # only emit actions whose tags still exist (i.e. weren't already emitted).
                # To avoid double-emitting we compare against what was already scanned:
                # any tag that was in accumulated_assistant_content AND had full content
                # would already have been emitted.  The safest guard is to emit from the
                # portion of agent_content beyond the last scan offset.
                remaining_content = agent_content[webapp_action_scan_offset:]
                if remaining_content and _WEBAPP_ACTION_TAG_RE.search(remaining_content):
                    final_actions, _ = _extract_webapp_actions(remaining_content)
                    if final_actions:
                        await _emit_webapp_action_events(session_id, final_actions)

                # Strip webapp_action tags from the content saved to DB so they are
                # never rendered in the chat UI.  The agent's visible reply is the text
                # around the tags; the tags themselves are purely a signalling mechanism.
                _, agent_content = _extract_webapp_actions(agent_content)
                agent_content = agent_content.strip() or "Agent response"

                # Post-process streaming_events: split assistant events that contain
                # <webapp_action> tags into interleaved text + action chunks so the
                # frontend renders them in the correct position within the conversation.
                if any(
                    e.get("type") == "assistant"
                    and e.get("content")
                    and _WEBAPP_ACTION_TAG_RE.search(e["content"])
                    for e in streaming_events
                ):
                    processed_events: list[dict] = []
                    for evt in streaming_events:
                        if (
                            evt.get("type") == "assistant"
                            and evt.get("content")
                            and _WEBAPP_ACTION_TAG_RE.search(evt["content"])
                        ):
                            # Split content around each <webapp_action> tag, producing
                            # interleaved text chunks and action events in order.
                            content = evt["content"]
                            last_end = 0
                            for match in _WEBAPP_ACTION_TAG_RE.finditer(content):
                                # Text chunk before this tag
                                text_before = content[last_end:match.start()].strip()
                                if text_before:
                                    processed_events.append({
                                        **{k: v for k, v in evt.items() if k not in ("content", "event_seq")},
                                        "content": text_before,
                                    })
                                # The webapp_action event itself
                                raw_json = match.group(1).strip()
                                try:
                                    payload = json.loads(raw_json)
                                    action_name = payload.get("action", "")
                                    if action_name:
                                        processed_events.append({
                                            "type": "webapp_action",
                                            "content": action_name,
                                            "metadata": {
                                                "action": action_name,
                                                "data": payload.get("data", {}),
                                            },
                                        })
                                except (json.JSONDecodeError, AttributeError):
                                    logger.debug("Malformed webapp_action tag during post-processing: %r", raw_json[:200])
                                last_end = match.end()
                            # Remaining text after the last tag
                            text_after = content[last_end:].strip()
                            if text_after:
                                processed_events.append({
                                    **{k: v for k, v in evt.items() if k not in ("content", "event_seq")},
                                    "content": text_after,
                                })
                        else:
                            processed_events.append(evt)
                    # Re-number all events sequentially to preserve correct display order
                    for i, evt in enumerate(processed_events, start=1):
                        evt["event_seq"] = i
                    streaming_events = processed_events

                response_metadata["external_session_id"] = new_external_session_id
                if tools_needing_approval:
                    response_metadata["tools_needing_approval"] = list(tools_needing_approval)
                    logger.info(f"Message has {len(tools_needing_approval)} tools needing approval: {tools_needing_approval}")
                response_metadata["streaming_events"] = streaming_events

                has_questions = MessageService.detect_ask_user_question_tool(streaming_events)
                tool_questions_status = "unanswered" if has_questions else None

                await MessageService._finalize_agent_message(
                    agent_message_id=agent_message_id,
                    session_id=session_id,
                    agent_content=agent_content,
                    response_metadata=response_metadata,
                    tool_questions_status=tool_questions_status,
                    was_interrupted=was_interrupted,
                    get_fresh_db_session=get_fresh_db_session,
                )
                logger.info(f"Agent response finalized ({len(streaming_events)} events, model={response_metadata.get('model')}, has_questions={has_questions}, interrupted={was_interrupted})")

            # Emit stream_completed event for event-driven post-processing
            if not was_interrupted:
                try:
                    def _get_session_info():
                        with get_fresh_db_session() as db:
                            session_db = db.get(ChatSession, session_id)
                            if session_db:
                                env_db = db.get(AgentEnvironment, environment_id)
                                return session_db.user_id, env_db.agent_id if env_db else None
                            return None, None

                    user_id, agent_id = await asyncio.to_thread(_get_session_info)

                    await _emit_activity_event(
                        EventType.STREAM_COMPLETED, session_id, environment_id,
                        session_mode, user_id,
                        agent_id=str(agent_id) if agent_id else None,
                        was_interrupted=was_interrupted,
                    )
                except Exception as event_error:
                    logger.error(f"Failed to emit stream_completed event: {event_error}", exc_info=True)

            # Send final done event to frontend
            yield {
                "type": "done",
                "content": "",
                "metadata": {
                    **response_metadata,
                    "interrupted": was_interrupted
                }
            }

        except Exception as e:
            logger.error(f"Error in message stream: {e}", exc_info=True)

            await _emit_activity_event(
                EventType.STREAM_ERROR, session_id, environment_id,
                session_mode, ctx.user_id,
                error_type=type(e).__name__, error_message=str(e),
            )

            yield {
                "type": "error",
                "content": str(e),
                "error_type": type(e).__name__
            }
        finally:
            # Always unregister stream when done (success, error, or interruption)
            await active_streaming_manager.unregister_stream(session_id)
            logger.info(f"Stream unregistered for session {session_id}")

    @staticmethod
    async def create_user_message_and_emit_event(
        db_session: Session,
        session_id: UUID,
        message_content: str,
        answers_to_message_id: UUID | None
    ) -> SessionMessage:
        """
        Create user message and emit event to frontend.

        This is a helper method to avoid code duplication.
        """
        from app.services.events.event_service import event_service

        # Create user message
        user_message = MessageService.create_message(
            session=db_session,
            session_id=session_id,
            role="user",
            content=message_content,
            answers_to_message_id=answers_to_message_id
        )

        # Emit user message created event
        await event_service.emit_stream_event(
            session_id=session_id,
            event_type="user_message_created",
            event_data={
                "id": str(user_message.id),
                "role": "user",
                "content": message_content,
                "sequence_number": user_message.sequence_number,
                "timestamp": user_message.timestamp.isoformat()
            }
        )

        return user_message
