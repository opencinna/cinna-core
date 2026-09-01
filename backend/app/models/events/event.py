"""Event models for WebSocket-based real-time communication."""

from datetime import datetime, UTC
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlmodel import SQLModel

#: Meta key on the terminal stream events (``STREAM_COMPLETED`` /
#: ``STREAM_ERROR`` / ``STREAM_INTERRUPTED``) carrying **turn identity**: the
#: id of the agent ``SessionMessage`` that batch wrote, stringified, or an
#: explicit ``None`` for a batch that wrote none.
#:
#: Defined here — the one module both sides already import — so the emitters
#: (``sessions/message_service.py``) and the consumer
#: (``server_channels/channel_outbound_service.py``, which re-exports it under
#: the same name) share one symbol. A consumer that reads a key the emitter
#: does not send falls back to its legacy newest-row arm *silently*, so the
#: two sides drifting apart is exactly the failure this shared symbol exists
#: to make impossible. The semantics of the key's three states, and which
#: events name a *finalized* row, are documented at the consumer's re-export.
AGENT_MESSAGE_ID_META_KEY = "agent_message_id"


# Event types - can be extended as needed
class EventType:
    """Available event types for the event bus."""

    # Session events
    SESSION_CREATED = "session_created"
    SESSION_UPDATED = "session_updated"
    SESSION_DELETED = "session_deleted"

    # Message events
    MESSAGE_CREATED = "message_created"
    MESSAGE_UPDATED = "message_updated"
    MESSAGE_DELETED = "message_deleted"

    # Activity events
    ACTIVITY_CREATED = "activity_created"
    ACTIVITY_UPDATED = "activity_updated"
    ACTIVITY_DELETED = "activity_deleted"

    # Agent events
    AGENT_CREATED = "agent_created"
    AGENT_UPDATED = "agent_updated"
    AGENT_DELETED = "agent_deleted"

    # Environment events
    ENVIRONMENT_ACTIVATING = "environment_activating"
    ENVIRONMENT_ACTIVATED = "environment_activated"
    ENVIRONMENT_ACTIVATION_FAILED = "environment_activation_failed"
    ENVIRONMENT_SUSPENDED = "environment_suspended"
    ENVIRONMENT_STATUS_CHANGED = "environment_status_changed"
    ENVIRONMENT_CRITICAL_STATE_CHANGED = "environment_critical_state_changed"

    # Streaming events
    STREAM_STARTED = "stream_started"
    STREAM_COMPLETED = "stream_completed"
    STREAM_ERROR = "stream_error"
    STREAM_INTERRUPTED = "stream_interrupted"
    SESSION_INTERACTION_STATUS_CHANGED = "session_interaction_status_changed"

    # Session state events
    SESSION_STATE_UPDATED = "session_state_updated"  # Agent declared session outcome

    # To-do progress events (from TodoWrite tool)
    TODO_LIST_UPDATED = "todo_list_updated"      # Session-level to-do update
    TASK_TODO_UPDATED = "task_todo_updated"      # Task-level to-do update (propagated from session)

    # Task lifecycle events
    TASK_CREATED = "task_created"
    TASK_STATUS_UPDATED = "task_status_changed"

    # Task collaboration events (new)
    TASK_COMMENT_ADDED = "task_comment_added"
    TASK_STATUS_CHANGED = "task_status_changed"   # alias for TASK_STATUS_UPDATED
    TASK_ATTACHMENT_ADDED = "task_attachment_added"
    SUBTASK_COMPLETED = "subtask_completed"
    TASK_SUBTASK_CREATED = "task_subtask_created"

    # Agent status events
    AGENT_STATUS_UPDATED = "agent_status_updated"

    # CLI commands cache events
    CLI_COMMANDS_UPDATED = "cli_commands_updated"

    # Agent REST API build/run status change — fired when the agent_api spec is
    # re-cached (build success / failure / reload) so the owner sees boot errors
    # live. Meta carries `agent_id`, `environment_id`, `state`, and `last_error`.
    AGENT_API_STATUS_CHANGED = "agent_api_status_changed"

    # Plugin sync warning — fired from environment start/rebuild (`_sync_dynamic_data`)
    # when one or more plugins failed to install in the container (non-blocking;
    # the env still started). Drives the owner-facing amber banner + React Query
    # invalidation on the plugins tab. Meta carries `agent_id`, `environment_id`,
    # `instance_name`, and `failures` (list of {marketplace_name, plugin_name,
    # source, error_message}).
    PLUGIN_SYNC_WARNING = "plugin_sync_warning"

    # Workspace file change event — fired by env-core when workspace files
    # that the backend caches (prompts, CLI_COMMANDS.yaml, STATUS.md) change
    # and stabilise. Typically triggered by a Mutagen sync from the CLI, but
    # also fires for any other workspace mutation. Meta always carries
    # `environment_id` and `agent_id`; `changed_files` optionally lists the
    # relative paths that tripped the watcher.
    WORKSPACE_FILES_CHANGED = "workspace_files_changed"

    # CRON / schedule lifecycle events — emitted by agent_schedule_scheduler when
    # a scheduled execution finishes (success / triggered-session / error). Meta
    # always carries `environment_id`, `agent_id`, `schedule_id`, `schedule_type`
    # so downstream handlers (e.g., AgentStatusService.handle_cron_event) can
    # pull STATUS.md without extra DB lookups.
    CRON_COMPLETED_OK = "cron_completed_ok"          # script_trigger returned "OK"; no session
    CRON_TRIGGER_SESSION = "cron_trigger_session"    # session started by the schedule (both types)
    CRON_ERROR = "cron_error"                        # schedule failed before or during execution

    # Bundle / install events (Phase 2 — Agent Bundles & Installs)
    BUNDLE_PUBLISHED = "bundle_published"               # New revision published
    INSTALL_UPDATE_AVAILABLE = "install_update_available"  # Pending update on install
    INSTALL_UPDATE_APPLIED = "install_update_applied"    # Apply succeeded
    INSTALL_UPDATE_FAILED = "install_update_failed"      # Apply errored

    # Install setup gate events (Phase 4 — Pre-LLM gate / setup page)
    # Emitted when a user→agent dispatch is short-circuited by the readiness
    # gate (placeholder credentials still empty, or publisher creds broken).
    INSTALL_SETUP_REQUIRED = "install_setup_required"
    # Emitted when the install transitions from non-ready to ready (e.g.
    # the user just filled the last placeholder credential on the setup page).
    INSTALL_SETUP_COMPLETED = "install_setup_completed"
    # Emitted when a publisher-provided credential becomes unavailable
    # (deleted / unshared / allow_sharing flipped off).
    PUBLISHER_CREDENTIAL_BROKEN = "publisher_credential_broken"

    # Role events (Phase 3 — Roles & agent-user UX)
    # Targeted at the user whose role changed; payload carries
    # ``new_role``, ``previous_role``, and ``changed_by_user_id`` so the
    # frontend can refetch ``["currentUser"]`` and re-route on demote.
    USER_ROLE_CHANGED = "user_role_changed"

    # Improvement requests — a session owner shared a session with the agent's
    # owner (bundle publisher, or themselves). Emitted to the RECIPIENT's user
    # room so the Configuration-tab card badge updates live. Meta carries
    # `request_id`, `target_agent_id`, `source_agent_id`, `bundle_uuid`, `status`.
    IMPROVEMENT_REQUEST_CREATED = "improvement_request_created"
    IMPROVEMENT_REQUEST_UPDATED = "improvement_request_updated"

    # Generic notification
    NOTIFICATION = "notification"


class EventBase(SQLModel):
    """Base event model with common fields."""

    type: str = Field(description="Event type (e.g., 'session_updated', 'message_created')")
    model_id: UUID | None = Field(default=None, description="ID of the related model (session_id, message_id, etc.)")
    text_content: str | None = Field(default=None, description="Optional notification text for the user")
    meta: dict[str, Any] | None = Field(default=None, description="Additional metadata (e.g., agent_id, session_id, etc.)")


class EventPublic(EventBase):
    """Public event model sent to clients."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC), description="When the event was created")
    user_id: UUID | None = Field(default=None, description="User ID for targeted events (None for broadcast)")


class EventBroadcast(BaseModel):
    """Event broadcast request model."""

    type: str = Field(description="Event type")
    model_id: UUID | None = Field(default=None, description="ID of the related model")
    text_content: str | None = Field(default=None, description="Optional notification text")
    meta: dict[str, Any] | None = Field(default=None, description="Additional metadata")
    user_id: UUID | None = Field(default=None, description="Target user ID (None for broadcast)")
    room: str | None = Field(default=None, description="Room name for targeted broadcast (e.g., 'user_{user_id}')")


class ConnectionInfo(BaseModel):
    """WebSocket connection information."""

    sid: str = Field(description="Socket.IO session ID")
    user_id: UUID = Field(description="Authenticated user ID")
    connected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    rooms: list[str] = Field(default_factory=list, description="Rooms the connection is subscribed to")
