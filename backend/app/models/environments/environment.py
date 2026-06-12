import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON, Text, DateTime, String
import sqlalchemy as sa


class AgentEnvironment(SQLModel, table=True):
    __tablename__ = "agent_environment"
    __table_args__ = (
        sa.Index(
            "ix_agent_environment_sync_active",
            "sync_active",
            postgresql_where=sa.text("sync_active = TRUE"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    agent_id: uuid.UUID = Field(foreign_key="agent.id", ondelete="CASCADE")
    env_name: str  # e.g., "python-env-basic"
    env_version: str = "1.0.0"  # e.g., "1.0.0"
    instance_name: str = "Instance"  # e.g., "Production", "Testing"
    type: str = "docker"  # "docker" | "remote_ssh" | "remote_http" | "kubernetes"
    status: str = "stopped"  # "stopped" | "creating" | "building" | "initializing" | "starting" | "running" | "rebuilding" | "suspended" | "activating" | "error" | "deprecated"
    is_active: bool = Field(default=False)
    status_message: str | None = None  # Detailed status message for UI (e.g., "Building Docker image...")
    config: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_health_check: datetime | None = None
    last_activity_at: datetime | None = None  # Last time environment was actively used (message sent, session opened, etc.)
    # SDK selection for agent (immutable after creation)
    agent_sdk_conversation: str | None = None  # "claude-code/anthropic" | "claude-code/minimax" | "opencode/anthropic"
    agent_sdk_building: str | None = None  # "claude-code/anthropic" | "claude-code/minimax" | "opencode/anthropic"
    # Model override per mode (optional; if None, adapter uses its own default)
    model_override_conversation: str | None = None  # e.g., "gpt-4o-mini", "claude-haiku-4-5"
    model_override_building: str | None = None  # e.g., "claude-opus-4", "gpt-4o"
    # AI credential linking (if False, use explicitly linked credentials)
    use_default_ai_credentials: bool = Field(default=True)
    conversation_ai_credential_id: uuid.UUID | None = Field(
        default=None, foreign_key="ai_credential.id", ondelete="SET NULL"
    )
    building_ai_credential_id: uuid.UUID | None = Field(
        default=None, foreign_key="ai_credential.id", ondelete="SET NULL"
    )
    # Agent self-reported status snapshot (from app-data/storage/STATUS.md in workspace)
    status_file_raw: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    status_file_severity: str | None = Field(default=None, sa_column=Column(sa.String(16), nullable=True))
    status_file_summary: str | None = Field(default=None, sa_column=Column(sa.String(512), nullable=True))
    status_file_reported_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    status_file_reported_at_source: str | None = Field(default=None, sa_column=Column(sa.String(16), nullable=True))
    status_file_fetched_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    status_file_prev_severity: str | None = Field(default=None, sa_column=Column(sa.String(16), nullable=True))
    status_file_severity_changed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    # CLI commands cache (from docs/CLI_COMMANDS.yaml in workspace)
    cli_commands_raw: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    cli_commands_parsed: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    cli_commands_fetched_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    cli_commands_error: str | None = Field(default=None, sa_column=Column(sa.String(256), nullable=True))
    # Agent REST API (cinna_api) spec + policy cache. Mirrors the CLI commands
    # cache: the harvested OpenAPI spec and the parsed policy.yaml are cached on
    # the env row so consumers, the spec viewer, and client generation can read
    # the contract without cold-starting a suspended producer or spawning the
    # serving child. Refreshed on the env-core reload notification.
    agent_api_spec_parsed: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    agent_api_spec_fetched_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    agent_api_spec_error: str | None = Field(default=None, sa_column=Column(sa.String(512), nullable=True))
    agent_api_policy_cache: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # Prompt-sync reconcile baselines — the per-environment common-ancestor
    # hash for each bidirectional prompt (the git-merge-base for the three-way
    # reconcile). SHA-256 hex of the last-reconciled normalised content.
    # ``None`` = never reconciled (seeding rule applies; DB wins if non-empty).
    workflow_prompt_synced_hash: str | None = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    entrypoint_prompt_synced_hash: str | None = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    refiner_prompt_synced_hash: str | None = Field(default=None, sa_column=Column(sa.String(64), nullable=True))
    # CLI live sync tracking
    last_sync_activity_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    sync_active: bool = Field(default=False)
    # Admin-managed build tracking (system-managed, not user-settable)
    last_build_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    current_image_tag: str | None = Field(default=None, sa_column=Column(String(255), nullable=True, index=True))


# Pydantic Schemas
class AgentEnvironmentCreate(SQLModel):
    env_name: str
    env_version: str = "1.0.0"
    instance_name: str = "Instance"
    type: str = "docker"  # "docker" | "remote_ssh" | "remote_http"
    config: dict = {}
    agent_sdk_conversation: str | None = None  # "claude-code/anthropic" | "claude-code/minimax" | "opencode/anthropic"
    agent_sdk_building: str | None = None  # "claude-code/anthropic" | "claude-code/minimax" | "opencode/anthropic"
    # Model override per mode (optional)
    model_override_conversation: str | None = None
    model_override_building: str | None = None
    # AI credential linking
    use_default_ai_credentials: bool = True
    conversation_ai_credential_id: uuid.UUID | None = None
    building_ai_credential_id: uuid.UUID | None = None


class AgentEnvironmentUpdate(SQLModel):
    instance_name: str | None = None
    config: dict | None = None


class AgentEnvironmentReconfigure(SQLModel):
    """Dynamic per-mode reconfiguration payload (SDK / credential / model).

    Mirrors the credential-bearing subset of ``AgentEnvironmentCreate``. Sent by
    the Environments tab when a developer edits a mode badge and rebuilds. Both
    modes are always supplied (the UI seeds the untouched mode from the current
    env), so ``None`` SDK fields mean "keep the env's current value".
    """
    agent_sdk_conversation: str | None = None
    agent_sdk_building: str | None = None
    model_override_conversation: str | None = None
    model_override_building: str | None = None
    use_default_ai_credentials: bool = True
    conversation_ai_credential_id: uuid.UUID | None = None
    building_ai_credential_id: uuid.UUID | None = None
    # When True (default) a rebuild is kicked off immediately after persisting.
    rebuild: bool = True


# ---------------------------------------------------------------------------
# Model health (computed, transient — never persisted)
# ---------------------------------------------------------------------------

class ModelHealthMode(SQLModel):
    """Per-mode model-health entry.

    Computed from the central model catalog + the linked credential's
    discovered-model cache. Purely a read-time signal; never stored.
    """
    mode: str                       # "conversation" | "building"
    model: str                      # effective resolved model (override or catalog default)
    status: str                     # "ok" | "retired_override" | "unknown_model" | "unverified"
    cause: str | None = None        # "stale_default" | "frozen_override" | None
    suggested_model: str | None = None  # tier-appropriate catalog default (when known)
    cta: str | None = None          # plain-language remediation copy for the UI


class ModelHealthPublic(SQLModel):
    """Roll-up of per-mode model health for an environment."""
    has_warning: bool = False
    modes: list[ModelHealthMode] = []


class AgentEnvironmentPublic(SQLModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    env_name: str
    env_version: str
    instance_name: str
    type: str
    status: str
    status_message: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_health_check: datetime | None
    last_activity_at: datetime | None
    agent_sdk_conversation: str | None
    agent_sdk_building: str | None
    # Model override per mode (optional)
    model_override_conversation: str | None
    model_override_building: str | None
    # AI credential linking
    use_default_ai_credentials: bool
    conversation_ai_credential_id: uuid.UUID | None
    building_ai_credential_id: uuid.UUID | None
    # Computed, transient (never persisted) model-health signal. Mirrors the
    # refresh_command_warning precedent on AgentStatusPublic: populated by the
    # route/service builders for list/detail responses; null when not computed.
    model_health: ModelHealthPublic | None = None


class AgentEnvironmentsPublic(SQLModel):
    data: list[AgentEnvironmentPublic]
    count: int


class UsageIntentResponse(SQLModel):
    """Response for a usage-intent signal (REST + WebSocket share this shape).

    ``status`` is ``"activating"`` when a suspended environment's activation was
    triggered in the background, or ``"ok"`` when no action was needed.
    ``environment_id`` is the *resolved* environment id, which may differ from
    the requested one when the request targeted a non-active environment and was
    redirected to the agent's active environment.
    """
    status: str
    message: str
    environment_id: uuid.UUID


# ---------------------------------------------------------------------------
# Admin-only response schemas (no database tables)
# ---------------------------------------------------------------------------

class AdminAgentEnvironmentPublic(AgentEnvironmentPublic):
    """Enriched environment row for the admin console.

    Inherits every field of ``AgentEnvironmentPublic`` and adds admin-only
    enrichment derived from joins (owner, agent) and live computation
    (expected tag, staleness, in-use flag).
    """
    # Admin-specific enrichment
    agent_name: str
    owner_id: uuid.UUID
    owner_email: str
    owner_username: str | None
    owner_workspace_id: uuid.UUID | None
    current_image_tag: str | None
    expected_image_tag: str | None  # None when template directory is missing
    template_hash_current: str | None  # 12-char hash extracted from current_image_tag
    template_hash_expected: str | None  # 12-char hash from TemplateImageService
    is_stale: bool
    in_use: bool
    active_sessions_count: int
    last_build_at: datetime | None
    sync_active: bool
    # Cheap model-health roll-up flag (computed per row in list_environments).
    # True when any mode resolves to a retired/unavailable model. Distinct from
    # is_stale (image-tag staleness → rebuild); this is a config-health signal
    # → reconfigure/restart.
    model_health_warning: bool = False


class AdminTemplateInfoPublic(SQLModel):
    """Per-template summary for the admin console."""
    env_name: str
    expected_image_tag: str | None
    expected_hash: str | None
    total_envs: int
    stale_envs: int


class AdminAgentEnvironmentsPublic(SQLModel):
    """Paginated list response for the admin environments console."""
    data: list[AdminAgentEnvironmentPublic]
    count: int
    stale_count: int
    in_use_count: int
    templates: list[AdminTemplateInfoPublic]


class AdminBulkSkipped(SQLModel):
    """A single environment that was skipped during a bulk rebuild."""
    environment_id: uuid.UUID
    reason: str  # "not_found" | "status_not_allowed"


class AdminBulkRebuildRequest(SQLModel):
    """Request body for bulk rebuild endpoint."""
    # Schema-level cap on batch size is defense-in-depth; the route also enforces
    # settings.ADMIN_ENV_MAX_BULK_SIZE at runtime (which may be lower).
    environment_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=200)


class AdminBulkRebuildResponse(SQLModel):
    """Response from the bulk rebuild endpoint."""
    queued_environment_ids: list[uuid.UUID]
    skipped: list[AdminBulkSkipped]
