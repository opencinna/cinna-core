"""
MCP Provider service — consumer-side connection management.

An ``mcp_provider`` credential is the connection between a consumer agent and a
remote MCP server. This service is the producer/consumer wiring analogue of
``AgentApiTokenService`` for the MCP world:

- ``list_discoverable_agents`` — platform agents whose agent2agent connector the
  current user may consume (drives the consumer picker).
- ``connect_to_agent`` — ACL-check the producer connector, mint a connector-scoped
  direct token bound to the new credential (RD-2), build the endpoint URL, create
  an ``mcp_provider`` credential, optionally link it to a consumer agent.
- ``connect_to_external`` — add an arbitrary external MCP server (fixed_token /
  none here; oauth_dcr is created in ``awaiting_auth`` and handed to the Phase 5
  OAuth service).
- ``get_status`` — derive the lifecycle state for the credential detail panel.

Credential CRUD is delegated to ``CredentialsService`` (never duplicated). Direct
tokens are delegated to ``MCPDirectTokenService``; the producer RS / verifier
accept ``token_type="direct"`` unchanged (RD-1).
"""
import logging
import time
import uuid

from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    Agent,
    ConnectMcpProviderAgentRequest,
    ConnectMcpProviderExternalRequest,
    CredentialCreate,
    DiscoverableAgent,
    MCPConnector,
    MCPProviderConnectionResponse,
    MCPProviderStatus,
    MCPProviderTargetAgent,
    MCPToken,
    User,
    UserWorkspace,
)
from app.models.credentials.credential import Credential, CredentialType
from app.models.mcp.mcp_provider import (
    MCP_PROVIDER_EXTERNAL_AUTH_MODES,
    MCP_PROVIDER_TRANSPORTS,
)
from app.services.mcp.mcp_connector_service import MCPConnectorService
from app.services.mcp.mcp_direct_token_service import MCPDirectTokenService
from app.services.mcp_providers.egress_guard import (
    EgressBlockedError,
    validate_external_endpoint_url,
)

logger = logging.getLogger(__name__)


class MCPProviderError(Exception):
    """Base exception for MCP-provider connect/management errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class MCPProviderService:
    # ------------------------------------------------------------------ #
    # URL helpers                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_endpoint_url(connector_id: uuid.UUID) -> str:
        """Public agent2agent endpoint: {MCP_SERVER_BASE_URL}/{connector_id}/mcp."""
        base = (settings.MCP_SERVER_BASE_URL or "").rstrip("/")
        return f"{base}/{connector_id}/mcp"

    # ------------------------------------------------------------------ #
    # Discovery                                                            #
    # ------------------------------------------------------------------ #

    @staticmethod
    def list_discoverable_agents(
        session: Session,
        user: User,
        consumer_agent_id: uuid.UUID | None = None,
    ) -> list[DiscoverableAgent]:
        """
        Platform agents exposing an active agent2agent connector the current user
        may consume (owner or in ``allowed_user_ids`` / legacy ``allowed_emails``).

        Excludes the consumer's own agent when ``consumer_agent_id`` is supplied
        (an agent connecting to itself is pointless and confusing in the picker).
        """
        connectors = session.exec(
            select(MCPConnector).where(
                MCPConnector.is_agent_to_agent == True,  # noqa: E712
                MCPConnector.is_active == True,  # noqa: E712
            )
        ).all()

        excluded_agent_id: uuid.UUID | None = None
        if consumer_agent_id is not None:
            consumer = session.get(Agent, consumer_agent_id)
            if consumer is not None:
                excluded_agent_id = consumer.id

        results: list[DiscoverableAgent] = []
        for connector in connectors:
            if not MCPConnectorService.check_user_access(
                session, connector.id, user
            ):
                continue
            if excluded_agent_id is not None and connector.agent_id == excluded_agent_id:
                continue
            agent = session.get(Agent, connector.agent_id)
            if agent is None:
                continue
            results.append(
                DiscoverableAgent(
                    agent_id=agent.id,
                    agent_name=agent.name,
                    connector_id=connector.id,
                    connector_name=connector.name,
                    mode=connector.mode,
                    ui_color_preset=agent.ui_color_preset,
                )
            )
        return results

    # ------------------------------------------------------------------ #
    # Connect: platform agent2agent                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def connect_to_agent(
        session: Session,
        user: User,
        data: ConnectMcpProviderAgentRequest,
        is_superuser: bool = False,
    ) -> MCPProviderConnectionResponse:
        """
        Connect to a platform agent's agent2agent connector (mirror of
        ``connect_agent_api``).

        Steps:
          1. Resolve the connector; require it is agent2agent + active.
          2. ACL-check: caller must be owner / in the connector ACL / superuser.
          3. Validate ``consumer_agent_id`` up front (ownership) so a non-owned
             agent never leaves an orphaned credential behind.
          4. Mint a connector-scoped direct token.
          5. Create the ``mcp_provider`` credential (auth_mode=agent2agent).
          6. Bind the token to the credential (RD-2 cascade-revoke).
          7. Optionally link to the consumer agent (immediate sync).
        """
        from app.services.credentials.credentials_service import CredentialsService

        MCPProviderService._validate_modes(
            data.mcp_mode_conversation, data.mcp_mode_building
        )

        connector = session.get(MCPConnector, data.connector_id)
        if connector is None or not connector.is_agent_to_agent:
            # 404 (not 403) so a non-agent2agent / missing connector is not
            # distinguishable — no existence leak.
            raise MCPProviderError("Connector not found", status_code=404)
        if not connector.is_active:
            raise MCPProviderError(
                "This connector is disabled and cannot be connected to",
                status_code=400,
            )

        # ACL: owner / allowed user / superuser. A caller outside the producer
        # ACL gets 403 (the connector exists but they may not consume it).
        if not is_superuser and not MCPConnectorService.check_user_access(
            session, connector.id, user
        ):
            raise MCPProviderError(
                "You are not allowed to connect to this agent's MCP connector",
                status_code=403,
            )

        producer_agent = session.get(Agent, connector.agent_id)
        if producer_agent is None:
            raise MCPProviderError("Producer agent not found", status_code=404)

        label = data.label or f"{producer_agent.name} (MCP)"

        # Resolve the credential workspace consumer-first (mirror agent_api).
        workspace_id: uuid.UUID | None = producer_agent.user_workspace_id
        consumer_agent: Agent | None = None
        if data.consumer_agent_id is not None:
            consumer_agent = session.get(Agent, data.consumer_agent_id)
            if consumer_agent is None:
                raise MCPProviderError("Consumer agent not found", status_code=404)
            if not is_superuser and consumer_agent.owner_id != user.id:
                raise MCPProviderError(
                    "You do not own the consumer agent", status_code=403
                )
            workspace_id = consumer_agent.user_workspace_id

            # One-per-pair idempotency (Fix 5). Connecting the same
            # (producer connector, consumer agent) twice returns the existing
            # connection instead of minting a second token / creating a second
            # credential. Resolved decrypt-free via the bound mcp_token join:
            # the connector linkage lives on mcp_token.connector_id, and the
            # consumer linkage on the new credential.mcp_consumer_agent_id column.
            existing = session.exec(
                select(Credential)
                .join(MCPToken, MCPToken.credential_id == Credential.id)
                .where(
                    Credential.type == CredentialType.MCP_PROVIDER,
                    Credential.mcp_consumer_agent_id == consumer_agent.id,
                    MCPToken.connector_id == connector.id,
                )
            ).first()
            if existing is not None:
                existing_data = CredentialsService.decrypt_credential_data(
                    session=session, credential=existing
                )
                # Re-assert the consumer link (idempotent) in case it was lost.
                await CredentialsService.link_credential_to_agent(
                    session,
                    agent_id=consumer_agent.id,
                    credential_id=existing.id,
                    owner_id=user.id,
                    is_superuser=is_superuser,
                )
                return MCPProviderConnectionResponse(
                    credential_id=existing.id,
                    auth_mode=existing_data.get("auth_mode", "agent2agent"),
                    endpoint_url=existing_data.get("endpoint_url", ""),
                    transport=existing_data.get("transport", "streamable-http"),
                    status="connected",
                    linked_consumer_agent_id=consumer_agent.id,
                )

        # 4. Mint a connector-scoped direct token (full value returned once).
        created_token = MCPDirectTokenService.create_token(
            db_session=session, connector=connector, label=label
        )

        endpoint_url = MCPProviderService.build_endpoint_url(connector.id)

        # 5. Create the mcp_provider credential. The direct token is already
        #    committed (it is independently usable against the producer connector),
        #    so on any credential-creation failure we best-effort delete the token
        #    to avoid leaving an orphaned, un-revocable grant behind.
        try:
            credential = CredentialsService.create_credential(
                session,
                CredentialCreate(
                    name=label,
                    type=CredentialType.MCP_PROVIDER,
                    notes=(
                        f"MCP connection to agent {producer_agent.name} "
                        f"(ID: {producer_agent.id})"
                    ),
                    allow_sharing=False,
                    user_workspace_id=workspace_id,
                    mcp_mode_conversation=data.mcp_mode_conversation,
                    mcp_mode_building=data.mcp_mode_building,
                    # Auto-managed pair → "Automatic Credentials" tab.
                    mcp_auth_mode="agent2agent",
                    # Record the consumer side of the pair (Fix 2). NULL when
                    # connecting without a consumer (a "floating" connection);
                    # link_credential_to_agent binds it on first link.
                    mcp_consumer_agent_id=(
                        consumer_agent.id if consumer_agent is not None else None
                    ),
                    credential_data={
                        "endpoint_url": endpoint_url,
                        "transport": "streamable-http",
                        "auth_mode": "agent2agent",
                        "label": label,
                        "target_agent_id": str(producer_agent.id),
                        "target_connector_id": str(connector.id),
                        "token": created_token.token,
                    },
                ),
                owner_id=user.id,
            )
        except Exception:
            orphan = session.get(MCPToken, created_token.id)
            if orphan is not None:
                session.delete(orphan)
                session.commit()
            raise

        # 6. Bind the token to the credential so deleting the credential
        #    cascade-deletes the token — revoking THIS consumer only (RD-2).
        token_row = session.get(MCPToken, created_token.id)
        if token_row is not None:
            token_row.credential_id = credential.id
            session.add(token_row)
            session.commit()
        else:
            logger.warning(
                "Direct token %s missing when binding to credential %s; "
                "disconnect will not auto-revoke this token",
                created_token.id,
                credential.id,
            )

        # 7. Optionally link to the consumer agent (syncs into its containers).
        linked_consumer_agent_id: uuid.UUID | None = None
        if consumer_agent is not None:
            try:
                await CredentialsService.link_credential_to_agent(
                    session,
                    agent_id=consumer_agent.id,
                    credential_id=credential.id,
                    owner_id=user.id,
                    is_superuser=is_superuser,
                )
                linked_consumer_agent_id = consumer_agent.id
            except ValueError as e:
                raise MCPProviderError(
                    f"Connection created, but linking to the consumer agent "
                    f"failed: {e}",
                    status_code=400,
                )

        return MCPProviderConnectionResponse(
            credential_id=credential.id,
            auth_mode="agent2agent",
            endpoint_url=endpoint_url,
            transport="streamable-http",
            status="connected",
            linked_consumer_agent_id=linked_consumer_agent_id,
        )

    # ------------------------------------------------------------------ #
    # Connect: external MCP server                                         #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def connect_to_external(
        session: Session,
        user: User,
        data: ConnectMcpProviderExternalRequest,
        is_superuser: bool = False,
    ) -> MCPProviderConnectionResponse:
        """
        Add an arbitrary external MCP server.

        ``fixed_token`` / ``none`` create the credential immediately
        (status ``connected``). ``oauth_dcr`` creates the credential in
        ``awaiting_auth`` with no token yet; the live DCR + authorization flow
        that produces the token is Phase 5 (the ``authorize_url`` is filled there).
        """
        from app.services.credentials.credentials_service import CredentialsService

        MCPProviderService._validate_modes(
            data.mcp_mode_conversation, data.mcp_mode_building
        )

        if data.transport not in MCP_PROVIDER_TRANSPORTS:
            raise MCPProviderError(
                f"Unsupported transport '{data.transport}'", status_code=400
            )
        if data.auth_mode not in MCP_PROVIDER_EXTERNAL_AUTH_MODES:
            raise MCPProviderError(
                f"Unsupported auth mode '{data.auth_mode}'", status_code=400
            )
        if data.auth_mode == "fixed_token" and not (data.token or "").strip():
            raise MCPProviderError(
                "A token is required for fixed_token auth mode", status_code=400
            )

        # SSRF/egress hygiene: reject obviously unsafe targets up front (RD-6).
        try:
            endpoint_url = validate_external_endpoint_url(data.endpoint_url)
        except EgressBlockedError as e:
            raise MCPProviderError(str(e), status_code=400)

        label = data.label or endpoint_url

        # Validate consumer agent ownership up front (mirror connect_to_agent).
        workspace_id: uuid.UUID | None = None
        consumer_agent: Agent | None = None
        if data.consumer_agent_id is not None:
            consumer_agent = session.get(Agent, data.consumer_agent_id)
            if consumer_agent is None:
                raise MCPProviderError("Consumer agent not found", status_code=404)
            if not is_superuser and consumer_agent.owner_id != user.id:
                raise MCPProviderError(
                    "You do not own the consumer agent", status_code=403
                )
            workspace_id = consumer_agent.user_workspace_id
        elif data.user_workspace_id is not None:
            # No consumer agent: a manual external provider follows the user's
            # active workspace (like any "My Credentials" entry). Validate
            # ownership before stamping it (mirror POST /credentials).
            workspace = session.get(UserWorkspace, data.user_workspace_id)
            if workspace is None:
                raise MCPProviderError("Workspace not found", status_code=400)
            if not is_superuser and workspace.user_id != user.id:
                raise MCPProviderError(
                    "You do not own this workspace", status_code=403
                )
            workspace_id = data.user_workspace_id

        credential_data: dict = {
            "endpoint_url": endpoint_url,
            "transport": data.transport,
            "auth_mode": data.auth_mode,
            "label": label,
        }
        if data.auth_mode == "fixed_token":
            credential_data["token"] = data.token
        elif data.auth_mode == "oauth_dcr":
            # Token populated by the Phase 5 authorization-code exchange.
            credential_data["oauth_resource"] = endpoint_url

        credential = CredentialsService.create_credential(
            session,
            CredentialCreate(
                name=label,
                type=CredentialType.MCP_PROVIDER,
                notes=f"External MCP server: {endpoint_url}",
                allow_sharing=False,
                user_workspace_id=workspace_id,
                mcp_mode_conversation=data.mcp_mode_conversation,
                mcp_mode_building=data.mcp_mode_building,
                mcp_auth_mode=data.auth_mode,
                credential_data=credential_data,
            ),
            owner_id=user.id,
        )

        linked_consumer_agent_id: uuid.UUID | None = None
        if consumer_agent is not None:
            try:
                await CredentialsService.link_credential_to_agent(
                    session,
                    agent_id=consumer_agent.id,
                    credential_id=credential.id,
                    owner_id=user.id,
                    is_superuser=is_superuser,
                )
                linked_consumer_agent_id = consumer_agent.id
            except ValueError as e:
                raise MCPProviderError(
                    f"Connection created, but linking to the consumer agent "
                    f"failed: {e}",
                    status_code=400,
                )

        status = "awaiting_auth" if data.auth_mode == "oauth_dcr" else "connected"

        # For oauth_dcr, kick off DCR + authorization immediately so the caller
        # receives a ready-to-open authorize URL (mirrors the Google credential
        # OAuth UX). A discovery / DCR failure does not orphan the credential —
        # it stays in awaiting_auth and the user can retry via the reauthorize
        # route; we surface the error so the dialog can show it.
        authorize_url: str | None = None
        if data.auth_mode == "oauth_dcr":
            from app.services.mcp_providers.mcp_provider_oauth_service import (
                MCPProviderOAuthError,
                MCPProviderOAuthService,
            )

            try:
                authorize_url = await MCPProviderOAuthService.begin_authorization(
                    session, credential, user.id
                )
            except MCPProviderOAuthError as e:
                raise MCPProviderError(e.message, status_code=e.status_code)

        return MCPProviderConnectionResponse(
            credential_id=credential.id,
            auth_mode=data.auth_mode,
            endpoint_url=endpoint_url,
            transport=data.transport,
            status=status,
            linked_consumer_agent_id=linked_consumer_agent_id,
            authorize_url=authorize_url,
        )

    # ------------------------------------------------------------------ #
    # Status                                                               #
    # ------------------------------------------------------------------ #

    @staticmethod
    def get_status(
        session: Session,
        credential_id: uuid.UUID,
        user: User,
        is_superuser: bool = False,
    ) -> MCPProviderStatus:
        """Derive the connection status for the credential detail panel. Owner-only."""
        from app.services.credentials.credentials_service import CredentialsService

        credential = session.get(Credential, credential_id)
        if credential is None or (
            credential.owner_id != user.id and not is_superuser
        ):
            # 404 on non-owner — no existence leak.
            raise MCPProviderError("Credential not found", status_code=404)
        if credential.type != CredentialType.MCP_PROVIDER:
            raise MCPProviderError(
                "Credential is not an MCP provider connection", status_code=400
            )

        data = CredentialsService.decrypt_credential_data(
            session=session, credential=credential
        )
        return MCPProviderService._to_status(session, credential, data)

    @staticmethod
    def get_owned_credential(
        session: Session,
        credential_id: uuid.UUID,
        user: User,
        is_superuser: bool = False,
    ) -> Credential:
        """
        Resolve an ``mcp_provider`` credential the caller owns (owner-only;
        superuser bypass). 404 on non-owner / missing / wrong type — no existence
        leak. Shared by the OAuth authorize / reauthorize / callback-after-auth
        and the connectivity-probe routes.
        """
        credential = session.get(Credential, credential_id)
        if credential is None or (
            credential.owner_id != user.id and not is_superuser
        ):
            raise MCPProviderError("Credential not found", status_code=404)
        if credential.type != CredentialType.MCP_PROVIDER:
            raise MCPProviderError(
                "Credential is not an MCP provider connection", status_code=400
            )
        return credential

    @staticmethod
    def _to_status(
        session: Session, credential: Credential, data: dict
    ) -> MCPProviderStatus:
        auth_mode = data.get("auth_mode", "agent2agent")
        status = MCPProviderService._derive_lifecycle_state(auth_mode, data)

        target_agent: MCPProviderTargetAgent | None = None
        target_agent_id_raw = data.get("target_agent_id")
        if target_agent_id_raw:
            try:
                agent = session.get(Agent, uuid.UUID(target_agent_id_raw))
            except (ValueError, TypeError):
                agent = None
            if agent is not None:
                target_agent = MCPProviderTargetAgent(
                    id=agent.id,
                    name=agent.name,
                    ui_color_preset=agent.ui_color_preset,
                )

        # (agent2agent only) the single mode the producer connector serves — the
        # server side's true reachability. Resolved from the bound connector.
        connector_mode: str | None = None
        target_connector_id_raw = data.get("target_connector_id")
        if target_connector_id_raw:
            try:
                connector = session.get(
                    MCPConnector, uuid.UUID(target_connector_id_raw)
                )
            except (ValueError, TypeError):
                connector = None
            if connector is not None:
                connector_mode = connector.mode

        # (Fix 2) Resolve the consumer side of the pair from the first-class
        # column. None for external/manual providers, floating connections, or a
        # deleted consumer agent (SET NULL).
        consumer_agent: MCPProviderTargetAgent | None = None
        if credential.mcp_consumer_agent_id is not None:
            consumer = session.get(Agent, credential.mcp_consumer_agent_id)
            if consumer is not None:
                consumer_agent = MCPProviderTargetAgent(
                    id=consumer.id,
                    name=consumer.name,
                    ui_color_preset=consumer.ui_color_preset,
                )

        return MCPProviderStatus(
            credential_id=credential.id,
            auth_mode=auth_mode,
            transport=data.get("transport", "streamable-http"),
            endpoint_url=data.get("endpoint_url", ""),
            status=status,
            mcp_mode_conversation=credential.mcp_mode_conversation,
            mcp_mode_building=credential.mcp_mode_building,
            target_agent=target_agent,
            connector_mode=connector_mode,
            consumer_agent=consumer_agent,
            last_error=data.get("last_error"),
        )

    @staticmethod
    def _derive_lifecycle_state(auth_mode: str, data: dict) -> str:
        """
        Derive the (not stored) lifecycle state from the credential blob.

        - oauth_dcr with no token yet  -> awaiting_auth
        - oauth_dcr with an expired token -> expired (pre-stream refresh, Phase 5)
        - last_error present           -> error
        - token present / no auth need -> connected
        """
        if data.get("last_error"):
            return "error"
        if auth_mode == "oauth_dcr":
            if not data.get("token"):
                return "awaiting_auth"
            expires_at = data.get("oauth_token_expires_at")
            if isinstance(expires_at, (int, float)) and expires_at <= time.time():
                return "expired"
            return "connected"
        if auth_mode in ("fixed_token", "agent2agent"):
            return "connected" if data.get("token") else "error"
        # "none"
        return "connected"

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_modes(conversation: bool, building: bool) -> None:
        """At least one mode must be enabled, else the credential is inert."""
        if not conversation and not building:
            raise MCPProviderError(
                "At least one mode (conversation or building) must be enabled",
                status_code=400,
            )
