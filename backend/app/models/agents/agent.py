import uuid
from datetime import datetime, UTC
from typing import List
from sqlmodel import Field, Relationship, SQLModel, Column
from sqlalchemy import JSON, Index, text, Text, UniqueConstraint, DateTime

from app.models.users.user import User
from app.models.credentials.link_models import AgentCredentialLink


# Update mode constant — repurposed from the old clone flow. Used by Install
# rows (every Agent row IS an Install) to control how published-bundle
# updates roll out to the user.
class UpdateMode:
    """Update mode for Installs — when a publisher releases a new revision."""
    AUTOMATIC = "automatic"  # Apply on next env activation cycle
    MANUAL = "manual"        # User decides when to apply


# Shared properties
class AgentBase(SQLModel):
    name: str = Field(min_length=1, max_length=255)
    workflow_prompt: str | None = Field(default=None)
    entrypoint_prompt: str | None = Field(default=None)
    refiner_prompt: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    # Short, capability-verb-focused natural-language description of what to
    # ask the agent to do. Used by the App MCP router for AI classification
    # of incoming external messages. Edited by the agent owner on the
    # Prompts tab; snapshotted onto each bundle revision at publish time
    # and propagated back into auto-managed AppAgentRoute rows on install
    # / apply-update.
    router_trigger_prompt: str | None = Field(default=None, sa_column=Column(Text, nullable=True))


# Properties to receive on agent creation
class AgentCreate(AgentBase):
    description: str | None = None
    user_workspace_id: uuid.UUID | None = None


# Properties to receive on agent update
class AgentUpdate(SQLModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None
    refiner_prompt: str | None = None
    router_trigger_prompt: str | None = None
    is_active: bool | None = None
    ui_color_preset: str | None = None
    show_on_dashboard: bool | None = None
    conversation_mode_ui: str | None = None
    a2a_config: dict | None = None
    example_prompts: list[str] | None = None
    inactivity_period_limit: str | None = None
    webapp_enabled: bool | None = None
    agent_api_enabled: bool | None = None
    # Install owners can update update mode for bundle updates
    update_mode: str | None = None  # "automatic" | "manual"
    # Publisher override map (Phase 5). Only meaningful on the publisher
    # install; ignored on foreign installs. The route validates the shape.
    publish_settings: dict | None = None


# Database model, database table inferred from class name
class Agent(AgentBase, table=True):
    __table_args__ = (
        Index(
            "ix_agent_pending_update",
            "pending_update",
            postgresql_where=text("pending_update = true"),
        ),
        Index(
            "ix_agent_bundle_uuid",
            "bundle_uuid",
            postgresql_where=text("bundle_uuid IS NOT NULL"),
        ),
        # Partial unique index — exactly one publisher install per bundle.
        Index(
            "uq_agent_publisher_install_per_bundle",
            "bundle_uuid",
            unique=True,
            postgresql_where=text("is_publisher_install = true"),
        ),
        # Partial unique index: one General Assistant per user
        Index(
            "ix_agent_general_assistant_per_user",
            "owner_id",
            postgresql_where=text("is_general_assistant = true"),
            unique=True,
        ),
        # One install per (owner, bundle_id, slot) — the slot is
        # ``is_publisher_install``. A user may own both a publisher
        # install (``is_publisher_install=True``) and a separate consumer
        # install (``is_publisher_install=False``) of the same bundle,
        # which is how a publisher dogfoods their own published bundle.
        # Foreign installs only ever populate the consumer slot.
        UniqueConstraint(
            "owner_id",
            "bundle_id",
            "is_publisher_install",
            name="uq_agent_bundle_id_per_publisher",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        foreign_key="user.id", nullable=False, ondelete="CASCADE"
    )
    user_workspace_id: uuid.UUID | None = Field(
        default=None, foreign_key="user_workspace.id", ondelete="CASCADE"
    )
    # Reverse-DNS bundle identifier — auto-generated on agent creation by
    # ``BundleIdService.generate_bundle_id``. Stable across bundle row
    # deletion so per-user app-data volumes can reattach on reinstall.
    # The Agent row IS the "Install" record; the publisher install and every
    # foreign user install share the same ``bundle_id`` once published.
    bundle_id: str = Field(max_length=255, nullable=False, index=True)
    # Phase 2 — bundle linkage. ``bundle_uuid`` is NULL until the agent
    # is published (the publisher install creates the bundle row on first
    # publish); foreign installs always have it set after install_bundle().
    bundle_uuid: uuid.UUID | None = Field(
        default=None,
        foreign_key="agent_bundle.id",
        ondelete="SET NULL",
    )
    installed_revision_id: uuid.UUID | None = Field(
        default=None,
        foreign_key="agent_bundle_revision.id",
        ondelete="SET NULL",
    )
    # True only on the publisher's working install. Exactly one such row per
    # bundle (enforced by partial unique index above).
    is_publisher_install: bool = Field(default=False)
    # NEW FIELDS for agent sessions
    description: str | None = None
    is_active: bool = Field(default=True)
    active_environment_id: uuid.UUID | None = Field(default=None, foreign_key="agent_environment.id")
    ui_color_preset: str | None = Field(default="slate")
    show_on_dashboard: bool = Field(default=True)
    conversation_mode_ui: str = Field(default="detailed")  # "detailed" or "compact"
    agent_sdk_config: dict = Field(default_factory=dict, sa_column=Column(JSON))  # SDK config: sdk_tools, allowed_tools
    a2a_config: dict = Field(default_factory=dict, sa_column=Column(JSON))  # A2A config: skills, version, generated_at
    example_prompts: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    inactivity_period_limit: str | None = Field(default=None)  # None="10min" | "2_days" | "1_week" | "1_month" | "always_on"
    webapp_enabled: bool = Field(default=False)  # Whether webapp feature is active
    agent_api_enabled: bool = Field(default=False)  # Whether the agent REST API (cinna_api) feature is active
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Per-prompt logical clocks — the LWW tiebreaker for the DB side of the
    # prompt-sync reconcile. Bumped whenever the corresponding DB prompt content
    # changes (UI edit, env→DB pull, bundle apply-update). Nullable so existing
    # rows backfill cleanly; ``None`` is treated as "-∞" (oldest) in the
    # tiebreak, which makes a populated env mtime win (preserve the env edit).
    workflow_prompt_updated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    entrypoint_prompt_updated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    refiner_prompt_updated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    # Update preferences (for installs of published bundles)
    update_mode: str = Field(default="manual")  # "automatic" | "manual"
    pending_update: bool = Field(default=False)
    pending_update_at: datetime | None = Field(default=None)
    last_sync_at: datetime | None = Field(default=None)
    last_update_status: str | None = Field(default=None)  # "synced" | "failed" | None

    # General Assistant flag
    is_general_assistant: bool = Field(default=False)

    # Publisher overrides (Phase 5 of the install-experience-redesign plan).
    # Lives only on the publisher install; ignored on foreign installs. The
    # publish-time spec collector reads ``credential_overrides[<spec_name>]
    # .provided_by`` to decide whether the spec is publisher-provided or
    # user-provided, falling back to inference from ``Credential.allow_sharing``
    # when the override is absent. Shape:
    #
    #   {
    #     "credential_overrides": {
    #       "<spec_name>": {"provided_by": "user" | "publisher"}
    #     }
    #   }
    publish_settings: dict = Field(
        default_factory=dict,
        sa_column=Column(JSON, nullable=False, server_default=text("'{}'::json")),
    )

    @property
    def app_data_catalog_type(self) -> str | None:
        """Slot policy for the per-(user, bundle) app-data volume.

        NULL = publisher install / unpublished standalone agent.
        "server" = consumer install from this server's local catalog.
        """
        if self.bundle_uuid is not None and not self.is_publisher_install:
            return "server"
        return None

    owner: User | None = Relationship(back_populates="agents")
    credentials: List["app.models.credentials.credential.Credential"] = Relationship(
        back_populates="agents", link_model=AgentCredentialLink
    )
    schedules: List["app.models.agents.agent_schedule.AgentSchedule"] = Relationship(
        back_populates="agent",
        cascade_delete=True
    )
    handover_configs: List["app.models.agents.agent_handover.AgentHandoverConfig"] = Relationship(
        back_populates="source_agent",
        sa_relationship_kwargs={
            "foreign_keys": "[AgentHandoverConfig.source_agent_id]",
            "cascade": "all, delete-orphan"
        }
    )


# Properties to return via API, id is always required
class AgentPublic(SQLModel):
    id: uuid.UUID
    name: str
    description: str | None
    workflow_prompt: str | None
    entrypoint_prompt: str | None
    refiner_prompt: str | None
    router_trigger_prompt: str | None = None
    is_active: bool
    active_environment_id: uuid.UUID | None
    ui_color_preset: str | None
    show_on_dashboard: bool
    conversation_mode_ui: str
    agent_sdk_config: dict | None = None
    a2a_config: dict | None = None
    example_prompts: list[str] = []
    inactivity_period_limit: str | None = None
    webapp_enabled: bool = False
    agent_api_enabled: bool = False
    created_at: datetime
    updated_at: datetime
    owner_id: uuid.UUID
    user_workspace_id: uuid.UUID | None

    # Bundle identity — reverse-DNS, stable per (publisher × bundle).
    # Displayed in monospace + copy on the agent detail header.
    bundle_id: str

    # Bundle / install linkage
    bundle_uuid: uuid.UUID | None = None
    installed_revision_id: uuid.UUID | None = None
    installed_revision_number: int | None = None
    installed_revision_version: str | None = None
    is_publisher_install: bool = False
    update_mode: str = "manual"
    pending_update: bool = False
    pending_update_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_update_status: str | None = None  # "synced" | "failed" | None

    # General Assistant flag
    is_general_assistant: bool = False

    # Publisher override map (Phase 5). Empty / absent on foreign installs.
    publish_settings: dict = {}


class AgentsPublic(SQLModel):
    data: list[AgentPublic]
    count: int


# Properties to return agent with credentials
class AgentWithCredentials(AgentPublic):
    credentials: list["CredentialPublic"]


# Request to link credential to agent
class AgentCredentialLinkRequest(SQLModel):
    credential_id: uuid.UUID


# Request to create agent with full flow (agent + environment + session)
class AgentCreateFlowRequest(SQLModel):
    description: str = Field(min_length=1, max_length=8000)
    mode: str = Field(default="building")  # "building" or "conversation"
    auto_create_session: bool = Field(default=False)  # If False, stop after environment is ready
    user_workspace_id: uuid.UUID | None = None
    agent_sdk_conversation: str | None = None  # SDK for conversation mode (e.g., "claude-code/anthropic")
    agent_sdk_building: str | None = None  # SDK for building mode
    # Environment template name (e.g., "python-env-advanced", "general-env").
    # When None, the service falls back to settings.DEFAULT_AGENT_ENV_NAME.
    env_name: str | None = None
    # Per-mode model override strings (e.g., "claude-haiku-4-5"); empty/None
    # leaves the SDK default in place.
    model_override_conversation: str | None = None
    model_override_building: str | None = None
    # Credential resolution: when True (default), the environment uses the
    # user's account-default AI credentials; when False, the explicit
    # *_ai_credential_id fields below pin specific credentials.
    use_default_ai_credentials: bool = True
    conversation_ai_credential_id: uuid.UUID | None = None
    building_ai_credential_id: uuid.UUID | None = None


# Response for agent creation flow initiation
class AgentCreateFlowResponse(SQLModel):
    agent_id: uuid.UUID
    message: str


# SDK Config schemas
class AgentSdkConfig(SQLModel):
    """Schema for agent SDK configuration"""
    sdk_tools: list[str] = []  # All tools discovered from agent-env
    allowed_tools: list[str] = []  # Tools approved by user for automatic permission grant


class AllowedToolsUpdate(SQLModel):
    """Schema for updating allowed tools list"""
    tools: list[str]  # Tools to add to allowed list


class PendingToolsResponse(SQLModel):
    """Response for pending tools endpoint"""
    pending_tools: list[str]  # Tools that need approval (in sdk_tools but not in allowed_tools)


class GenerateRouterTriggerPromptResponse(SQLModel):
    """Response for the router trigger prompt generator endpoint."""
    success: bool
    trigger_prompt: str | None = None
    error: str | None = None


class RouterTriggerPromptUpdate(SQLModel):
    """Owner-only update payload for ``Agent.router_trigger_prompt``."""
    router_trigger_prompt: str | None = None
