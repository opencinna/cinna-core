"""
MCP Provider (consumer-side) API models.

An ``mcp_provider`` credential *is* the connection between a consumer agent and a
remote MCP server — either another platform agent's agent-to-agent connector
(``auth_mode="agent2agent"``) or an arbitrary external MCP server
(``fixed_token`` / ``oauth_dcr`` / ``none``). These schemas back the
"Connect MCP Provider" helper and the credential detail / status surfaces.

The encrypted blob shape itself lives in ``credential.MCPProviderData``; this
module carries only the request/response/projection models for the routes.
"""
import uuid

from sqlmodel import Field, SQLModel

# Valid transports for a remote MCP server.
MCP_PROVIDER_TRANSPORTS = ("streamable-http", "sse")
# Valid auth modes for the external connect flow.
MCP_PROVIDER_EXTERNAL_AUTH_MODES = ("fixed_token", "oauth_dcr", "none")
# Derived lifecycle states surfaced on the credential detail page.
MCP_PROVIDER_STATUSES = ("connected", "awaiting_auth", "expired", "error")


# ── Connect: platform agent2agent connector ──────────────────────────────────


class ConnectMcpProviderAgentRequest(SQLModel):
    """
    Connect to a platform agent's agent-to-agent MCP connector.

    Resolves the connector, ACL-checks the caller, mints a connector-scoped
    direct token bound to the new credential, builds the endpoint URL, and
    creates an ``mcp_provider`` credential (``auth_mode="agent2agent"``).
    """
    connector_id: uuid.UUID
    # Optional consumer agent to link the new credential to immediately.
    consumer_agent_id: uuid.UUID | None = None
    # Per-mode applicability (default both on; at least one must stay on).
    mcp_mode_conversation: bool = True
    mcp_mode_building: bool = True
    # Optional friendly label (defaults to the producer agent's name).
    label: str | None = Field(default=None, max_length=255)


# ── Connect: arbitrary external MCP server ───────────────────────────────────


class ConnectMcpProviderExternalRequest(SQLModel):
    """
    Add an arbitrary external MCP server.

    For ``fixed_token`` / ``none`` the credential is created immediately. For
    ``oauth_dcr`` the credential is created in ``awaiting_auth`` and the DCR +
    authorization flow runs separately (Phase 5).
    """
    endpoint_url: str = Field(min_length=1, max_length=2048)
    transport: str = "streamable-http"  # "streamable-http" | "sse"
    auth_mode: str = "none"  # "fixed_token" | "oauth_dcr" | "none"
    # Required for fixed_token; ignored otherwise.
    token: str | None = None
    consumer_agent_id: uuid.UUID | None = None
    # Active workspace to stamp the new credential with when there is no consumer
    # agent (a manually-created external provider follows the user's active
    # workspace, like any "My Credentials" entry). Ignored when a consumer agent
    # is given — the agent's workspace wins. Ownership is validated server-side.
    user_workspace_id: uuid.UUID | None = None
    mcp_mode_conversation: bool = True
    mcp_mode_building: bool = True
    label: str | None = Field(default=None, max_length=2048)


class MCPProviderConnectionResponse(SQLModel):
    """Result of either connect helper — what it created / linked."""
    credential_id: uuid.UUID
    auth_mode: str
    endpoint_url: str
    transport: str
    status: str  # one of MCP_PROVIDER_STATUSES
    linked_consumer_agent_id: uuid.UUID | None = None
    # (oauth_dcr only) the authorize URL the frontend opens — Phase 5 populates
    # this; absent for the immediate flows.
    authorize_url: str | None = None


# ── Status / detail projection ───────────────────────────────────────────────


class MCPProviderTargetAgent(SQLModel):
    """(agent2agent only) the producer agent this connection points at."""
    id: uuid.UUID
    name: str
    ui_color_preset: str | None = None


class MCPProviderStatus(SQLModel):
    """
    Derived connection status for an ``mcp_provider`` credential, surfaced on the
    credential detail panel. Owner-only.
    """
    credential_id: uuid.UUID
    auth_mode: str
    transport: str
    endpoint_url: str
    status: str  # one of MCP_PROVIDER_STATUSES
    mcp_mode_conversation: bool
    mcp_mode_building: bool
    # (agent2agent only) best-effort — None if the producer agent is gone.
    target_agent: MCPProviderTargetAgent | None = None
    # (agent2agent only) the single mode the producer connector actually serves
    # ("conversation" | "building"), resolved from the bound connector. This is
    # the server side's true reachability — distinct from the consumer-side
    # mcp_mode_* toggles, which only choose where the *client* injects it. None
    # for external/manual providers or a deleted connector.
    connector_mode: str | None = None
    # (agent2agent only) the consumer agent this connection is bound to (the pair's
    # consumer side), resolved from credential.mcp_consumer_agent_id. None for
    # external/manual providers, for floating (unbound) connections, or if the
    # consumer agent was deleted (SET NULL). Reuses the MCPProviderTargetAgent shape.
    consumer_agent: MCPProviderTargetAgent | None = None
    # Last error message for the ``error`` state (best-effort, Phase 5 fills it).
    last_error: str | None = None


# ── Producer-side discovery (drives the consumer picker) ──────────────────────


class DiscoverableAgent(SQLModel):
    """
    A platform agent that exposes an agent2agent connector the current user is
    allowed to consume. Drives the "Connect MCP Provider → platform agent" picker.
    """
    agent_id: uuid.UUID
    agent_name: str
    connector_id: uuid.UUID
    connector_name: str
    mode: str
    ui_color_preset: str | None = None


class DiscoverableAgents(SQLModel):
    data: list[DiscoverableAgent]
    count: int


# ── OAuth/DCR (Phase 5) ───────────────────────────────────────────────────────


class MCPProviderOAuthAuthorizeResponse(SQLModel):
    """The authorize URL the frontend opens to start the OAuth/DCR consent."""
    authorize_url: str


class MCPProviderOAuthCallbackRequest(SQLModel):
    """Authorization-code callback payload, forwarded by the frontend route."""
    code: str
    state: str


class MCPProviderOAuthCallbackResponse(SQLModel):
    credential_id: uuid.UUID
    status: str  # one of MCP_PROVIDER_STATUSES (typically "connected")
    message: str


class MCPProviderTestResult(SQLModel):
    """Result of the best-effort connectivity probe."""
    ok: bool
    tools: list[str] = []
    error: str | None = None
