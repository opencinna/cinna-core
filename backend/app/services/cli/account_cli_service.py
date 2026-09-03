"""
Account CLI Service.

The account-level CLI feature adds an authentication spine on top of the
existing per-agent CLI machinery: a user bootstraps one account workspace from a
Settings card, and from there the CLI can discover the user's agents and mint
*normal* per-agent CLI tokens on demand (provenance-stamped) without any further
UI interaction.

This service owns:
- account setup-token creation + exchange (mirrors the per-agent flow with
  ``agent_id=None, kind="account"``),
- the accessible-agents listing (with ``can_build`` / ``is_foreign_install``
  flags),
- child-token minting (a normal per-agent ``CLIToken`` stamped with
  ``minted_by_account_token_id``),
- account-token listing + cascade revocation.

It reuses ``CLIAuthService`` (JWT), ``AgentService`` (building-rights gate),
``SecurityEventService`` (audit), and the per-agent setup-token cleanup
scheduler (which deletes ``cli_setup_token`` rows regardless of ``kind``).
"""
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request
from sqlmodel import Session, select

if TYPE_CHECKING:
    from app.models.agent_api.agent_api_token import ConnectAgentApiResponse
    from app.models.agents.agent_schedule import (
        AgentSchedule,
        AgentSchedulesPublic,
        CreateScheduleRequest,
        ScheduleRequest,
        ScheduleResponse,
        UpdateScheduleRequest,
    )
    from app.models.agents.agent_schedule_log import AgentScheduleLogsPublic
    from app.models.agents.agent_status import AgentStatusPublic
    from app.models.cli.account_convenience import (
        AccountAgentCreateBody,
        AccountAgentStatusResult,
        AccountConnectAgentApiBody,
        AccountConnectMcpBody,
        AccountCredentialCreateBody,
        AccountCredentialDraftResult,
        AccountCredentialTypesPublic,
        AccountCredentialUpdateBody,
    )
    from app.models.cli.account_desktop_token import (
        AccountDesktopTokenBody,
        AccountDesktopTokenResponse,
    )
    from app.models.credentials.credential import CredentialPublic, CredentialsPublic
    from app.models.mcp.mcp_provider import (
        DiscoverableAgents,
        MCPProviderConnectionResponse,
    )
    from app.models.users.user_workspace import UserWorkspacesPublic
    from app.services.agents.agent_scheduler_service import ManualRunResult

from app.core.config import settings
from app.models import Agent, AgentEnvironment, User
from app.models.cli.account_agent import AccountAgentListItem
from app.models.cli.cli_setup_token import CLISetupToken, CLISetupTokenCreated
from app.models.cli.cli_token import CLIAccountTokenPublic, CLIToken
from app.models.events.security_event import (
    CLI_ACCOUNT_AGENT_API_ENABLED,
    CLI_ACCOUNT_CHILD_TOKEN_MINTED,
    CLI_ACCOUNT_CHILD_TOKEN_REVOKED,
    CLI_ACCOUNT_CONNECT_AGENT_API,
    CLI_ACCOUNT_CONNECT_MCP,
    CLI_ACCOUNT_CREDENTIAL_CREATED,
    CLI_ACCOUNT_CREDENTIAL_DELETED,
    CLI_ACCOUNT_CREDENTIAL_SHARED_WITH_AGENT,
    CLI_ACCOUNT_CREDENTIAL_UPDATED,
    CLI_ACCOUNT_DESKTOP_TOKEN_DENIED,
    CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED,
    CLI_ACCOUNT_ENV_RESTARTED,
    CLI_ACCOUNT_SCHEDULE_CREATED,
    CLI_ACCOUNT_SCHEDULE_DELETED,
    CLI_ACCOUNT_SCHEDULE_RUN,
    CLI_ACCOUNT_SCHEDULE_UPDATED,
    CLI_ACCOUNT_STATUS_COMMAND_SET,
    CLI_ACCOUNT_TOKEN_CREATED,
    SecurityEventCreate,
)
from app.services.agents.agent_service import AgentService, CanBuildError
from app.services.cli.cli_auth import CLI_TOKEN_EXPIRY_DAYS, CLIAuthService
from app.services.common.rate_limiter import RateLimiter
from app.services.cli.cli_service import (
    SETUP_TOKEN_EXPIRY_MINUTES,
    _ensure_utc,
    _get_platform_url,
)
from app.services.events.security_event_service import SecurityEventService
from app.utils import client_ip

logger = logging.getLogger(__name__)

# Desktop-token exchanges per account token per minute. A desktop links once per
# workshop and re-exchanges only when it has lost its refresh token, so a real
# client stays far below this; the ceiling exists to bound row creation.
DESKTOP_TOKEN_EXCHANGE_LIMIT_PER_MIN = 10


class WorkspaceNotFoundError(Exception):
    """Raised when an account-CLI create targets a workspace the user doesn't own.

    The route maps this to 404 (existence-leak discipline — a foreign workspace
    id is not confirmed to exist).
    """


class AccountCLIService:
    """Account-level CLI operations. All methods are static (mirrors CLIService)."""

    # Throttles ``POST /account/desktop-token`` per account token. Process-local,
    # like every other consumer of this limiter — a backstop against a runaway
    # client, not a billing control.
    _desktop_token_rate_limiter = RateLimiter()

    # ── Account Setup Token Lifecycle ────────────────────────────────────

    @staticmethod
    def create_account_setup_token(
        db: Session,
        user: User,
        request: Request,
    ) -> CLISetupTokenCreated:
        """
        Create a short-lived account setup token (``agent_id=None``,
        ``kind="account"``).

        The route is already ``require_developer``-gated. Returns a
        ``CLISetupTokenCreated`` whose ``setup_command`` points at the account
        bootstrap endpoint.
        """
        token_value = secrets.token_urlsafe(24)
        expires_at = datetime.now(UTC) + timedelta(minutes=SETUP_TOKEN_EXPIRY_MINUTES)

        token = CLISetupToken(
            token=token_value,
            agent_id=None,
            environment_id=None,
            owner_id=user.id,
            kind="account",
            expires_at=expires_at,
        )
        db.add(token)
        db.commit()
        db.refresh(token)

        platform_url = _get_platform_url(request)
        setup_command = (
            f"curl -sL {platform_url}/api/cli-setup/account/{token_value} | python3 -"
        )

        return CLISetupTokenCreated(
            id=token.id,
            token=token_value,
            agent_id=None,
            environment_id=None,
            expires_at=token.expires_at,
            created_at=token.created_at,
            setup_command=setup_command,
        )

    @staticmethod
    async def mint_account_cli_token(
        db: Session,
        *,
        owner_id: uuid.UUID,
        machine_name: str,
        machine_info: str | None,
        request: Request,
    ) -> tuple[str, CLIToken]:
        """Mint a fresh account CLI token (``token_type="cli-account"``).

        The SINGLE source of truth for account-token minting — shared by the
        setup-token paste path (``exchange_account_setup_token``) and the device-
        login approval path (``DeviceLoginService.approve``) so a token minted by
        either route is provably scoped identically: ``agent_id=None``,
        ``token_type="cli-account"``, 7-day rolling expiry, owned by ``owner_id``.

        Creates the ``CLIToken``, **commits**, and emits
        ``CLI_ACCOUNT_TOKEN_CREATED``. Returns ``(raw_jwt, cli_token)``.

        COMMIT ORDERING: the helper owns the single ``db.commit()``. A caller that
        needs to persist additional rows atomically with the token (e.g. marking a
        setup token used) must ``db.add(...)`` them BEFORE calling this helper so
        they ride this commit — see ``exchange_account_setup_token``.
        """
        now = datetime.now(UTC)
        cli_token_id = uuid.uuid4()
        cli_expires_at = now + timedelta(days=CLI_TOKEN_EXPIRY_DAYS)

        jwt_value = CLIAuthService.create_cli_jwt(
            cli_token_id=cli_token_id,
            agent_id=None,
            owner_id=owner_id,
            expires_at=cli_expires_at,
            token_type="cli-account",
        )
        token_hash = CLIAuthService.hash_token(jwt_value)
        prefix = jwt_value[:12]

        cli_token = CLIToken(
            id=cli_token_id,
            agent_id=None,
            owner_id=owner_id,
            name=machine_name,
            token_hash=token_hash,
            prefix=prefix,
            token_type="cli-account",
            machine_info=machine_info,
            expires_at=cli_expires_at,
        )
        db.add(cli_token)
        db.commit()
        db.refresh(cli_token)

        await SecurityEventService.create_event(
            session=db,
            user_id=owner_id,
            data=SecurityEventCreate(
                agent_id=None,
                event_type=CLI_ACCOUNT_TOKEN_CREATED,
                severity="medium",
                details={"machine_name": machine_name, "ip": client_ip(request)},
            ),
        )

        return jwt_value, cli_token

    @staticmethod
    async def exchange_account_setup_token(
        db: Session,
        token_str: str,
        machine_name: str,
        machine_info: str | None,
        request: Request,
    ) -> dict:
        """
        Exchange an account setup token for an account CLI token + bootstrap
        payload.

        Validates the setup token (kind=="account", not used, not expired), marks
        it used, and delegates to ``mint_account_cli_token`` for the actual mint +
        audit. The setup token is marked used (``db.add``) BEFORE the helper's
        commit so both the token and the used-flag persist atomically.
        """
        stmt = select(CLISetupToken).where(CLISetupToken.token == token_str)
        setup_token = db.exec(stmt).first()
        if not setup_token:
            raise ValueError("Invalid setup token")
        if setup_token.kind != "account":
            raise ValueError("Not an account setup token")

        now = datetime.now(UTC)
        if setup_token.is_used:
            raise ValueError("Setup token has already been used")
        if _ensure_utc(setup_token.expires_at) < now:
            raise ValueError("Setup token has expired")

        # Mark used BEFORE minting so it rides the helper's single commit.
        setup_token.is_used = True
        db.add(setup_token)

        jwt_value, _cli_token = await AccountCLIService.mint_account_cli_token(
            db,
            owner_id=setup_token.owner_id,
            machine_name=machine_name,
            machine_info=machine_info,
            request=request,
        )

        platform_url = _get_platform_url(request)
        frontend_url = settings.FRONTEND_HOST.rstrip("/")

        return {
            "account_token": jwt_value,
            "platform_url": platform_url,
            "frontend_url": frontend_url,
            "machine_name": machine_name,
        }

    # ── Accessible Agents Listing ────────────────────────────────────────

    @staticmethod
    def list_accessible_agents(db: Session, user: User) -> list[AccountAgentListItem]:
        """
        Project the user's accessible agents into minimal listing items with
        ``can_build`` / ``is_foreign_install`` / ``has_active_environment``
        flags. No credentials, prompts, or env internals are exposed.

        The access set mirrors what ``list_agents`` surfaces for the user,
        which is always owner-scoped — foreign-bundle installs and the General
        Assistant are themselves per-user owned rows, so ``owner_id == user.id``
        fully captures the set with no cross-tenant leak.
        """
        agents = list(db.exec(select(Agent).where(Agent.owner_id == user.id)).all())

        # Active-environment lookup in one pass to avoid an N+1 over the list.
        agent_ids = [a.id for a in agents]
        active_env_agent_ids: set[uuid.UUID] = set()
        if agent_ids:
            env_stmt = select(AgentEnvironment.agent_id).where(
                AgentEnvironment.agent_id.in_(agent_ids),
                AgentEnvironment.is_active == True,  # noqa: E712
            )
            active_env_agent_ids = set(db.exec(env_stmt).all())

        items: list[AccountAgentListItem] = []
        for agent in agents:
            items.append(
                AccountAgentListItem(
                    id=agent.id,
                    name=agent.name,
                    description=agent.description,
                    ui_color_preset=agent.ui_color_preset,
                    owner_id=agent.owner_id,
                    user_workspace_id=agent.user_workspace_id,
                    bundle_uuid=agent.bundle_uuid,
                    is_publisher_install=agent.is_publisher_install,
                    is_foreign_install=AgentService.is_foreign_install(agent),
                    can_build=AgentService.can_build(db, user, agent),
                    has_active_environment=agent.id in active_env_agent_ids,
                )
            )
        return items

    # ── Knowledge Search ─────────────────────────────────────────────────

    @staticmethod
    async def search_knowledge(
        db: Session,
        user: User,
        query: str,
        topic: str | None = None,
    ) -> list[dict]:
        """
        Account-level analogue of the per-agent knowledge search.

        Scoped to the account user's accessible knowledge sources (public + own
        private). There is no agent and no workspace filter; delegates to the
        shared user-scoped core in ``CLIService``.
        """
        # Lazy import of the sibling service class (matches the local-import
        # convention used elsewhere in this module; the module-level import from
        # cli_service is value-only and the dependency stays one-directional).
        from app.services.cli.cli_service import CLIService

        return await CLIService.search_user_knowledge(
            db,
            user_id=user.id,
            query=query,
            topic=topic,
            workspace_id=None,
        )

    # ── Child Token Minting ──────────────────────────────────────────────

    @staticmethod
    async def mint_child_token(
        db: Session,
        user: User,
        account_token: CLIToken,
        agent_id: uuid.UUID,
        machine_name: str,
        machine_info: str | None,
        request: Request,
    ) -> dict:
        """
        Mint a *normal* per-agent CLI token on behalf of the account token.

        The child token is a standard ``token_type="cli"`` token scoped to the
        target agent, additionally stamped with ``minted_by_account_token_id``
        for the cascade-revoke. Returns the mint payload the CLI uses to write a
        standard per-agent workspace (mirrors ``exchange_setup_token``).

        Raises ``CanBuildError`` (route maps reason → 403/404).
        """
        agent = db.get(Agent, agent_id)
        if not agent:
            # Do not leak existence of inaccessible/missing agents.
            raise CanBuildError("not_accessible", "Agent not found")

        # Building-rights gate (access → 404, role/foreign → 403).
        AgentService.assert_can_build(db, user, agent)

        # Resolve the agent's active environment (optional).
        env_stmt = select(AgentEnvironment).where(
            AgentEnvironment.agent_id == agent.id,
            AgentEnvironment.is_active == True,  # noqa: E712
        )
        environment = db.exec(env_stmt).first()

        now = datetime.now(UTC)
        cli_token_id = uuid.uuid4()
        cli_expires_at = now + timedelta(days=CLI_TOKEN_EXPIRY_DAYS)

        jwt_value = CLIAuthService.create_cli_jwt(
            cli_token_id=cli_token_id,
            agent_id=agent.id,
            owner_id=user.id,
            expires_at=cli_expires_at,
            token_type="cli",
        )
        token_hash = CLIAuthService.hash_token(jwt_value)
        prefix = jwt_value[:12]

        child_token = CLIToken(
            id=cli_token_id,
            agent_id=agent.id,
            owner_id=user.id,
            name=machine_name,
            token_hash=token_hash,
            prefix=prefix,
            token_type="cli",
            minted_by_account_token_id=account_token.id,
            machine_info=machine_info,
            expires_at=cli_expires_at,
        )
        db.add(child_token)
        db.commit()
        db.refresh(child_token)

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent.id,
                environment_id=environment.id if environment else None,
                event_type=CLI_ACCOUNT_CHILD_TOKEN_MINTED,
                severity="medium",
                details={
                    "account_token_id": str(account_token.id),
                    "child_token_id": str(child_token.id),
                    "prefix": prefix,
                    "ip": client_ip(request),
                },
            ),
        )

        frontend_url = settings.FRONTEND_HOST.rstrip("/")

        # Mirror the per-agent exchange payload so the CLI's existing workspace
        # writer is reused verbatim. Credentials/knowledge are fetched
        # separately by the CLI as in the per-agent flow.
        return {
            "token": jwt_value,
            "id": str(child_token.id),
            "agent_id": str(agent.id),
            "owner_id": str(user.id),
            "prefix": prefix,
            "expires_at": child_token.expires_at,
            "agent_name": agent.name,
            "environment_id": str(environment.id) if environment else None,
            "template": environment.env_name if environment else None,
            "frontend_url": frontend_url,
            "knowledge_sources": [],
        }

    # ── Account Token Listing + Revocation ───────────────────────────────

    @staticmethod
    def list_account_tokens(db: Session, user: User) -> list[CLIAccountTokenPublic]:
        """List the user's active account tokens with their blast radius.

        Each row carries what revoking it would disconnect: the per-agent child
        tokens it minted (``child_count``) and the desktop sessions bought with
        it (``desktop_session_count``). Both are counted here because the
        revoke confirmation dialog is the only place a user can learn the cost
        before paying it, and the cascade now covers both.
        """
        from app.models.desktop_auth.desktop_oauth_client import DesktopOAuthClient

        now = datetime.now(UTC)
        stmt = (
            select(CLIToken)
            .where(
                CLIToken.owner_id == user.id,
                CLIToken.token_type == "cli-account",
                CLIToken.is_revoked == False,  # noqa: E712
                CLIToken.expires_at > now,
            )
            .order_by(CLIToken.created_at.desc())
        )
        account_tokens = list(db.exec(stmt).all())

        results: list[CLIAccountTokenPublic] = []
        for token in account_tokens:
            child_stmt = select(CLIToken).where(
                CLIToken.minted_by_account_token_id == token.id,
                CLIToken.is_revoked == False,  # noqa: E712
                CLIToken.expires_at > now,
            )
            child_count = len(list(db.exec(child_stmt).all()))
            desktop_stmt = select(DesktopOAuthClient).where(
                DesktopOAuthClient.minted_by_account_token_id == token.id,
                DesktopOAuthClient.is_revoked == False,  # noqa: E712
            )
            desktop_session_count = len(list(db.exec(desktop_stmt).all()))
            results.append(
                CLIAccountTokenPublic(
                    id=token.id,
                    name=token.name,
                    owner_id=token.owner_id,
                    prefix=token.prefix,
                    is_revoked=token.is_revoked,
                    last_used_at=token.last_used_at,
                    machine_info=token.machine_info,
                    expires_at=token.expires_at,
                    created_at=token.created_at,
                    child_count=child_count,
                    desktop_session_count=desktop_session_count,
                )
            )
        return results

    @staticmethod
    def revoke_account_token(db: Session, token_id: uuid.UUID, user: User) -> int:
        """
        Soft-revoke an account token, every child it minted, and every desktop
        session bought with it.

        Ownership-checked. Returns the number of **sessions** revoked: the
        account token, its per-agent child tokens, and any desktop client whose
        live grant came from this token via ``POST /account/desktop-token``
        (each such client counts once, however many refresh tokens it holds).
        The route surfaces that number as "N session(s) disconnected", and a
        desktop app is a session in the plain reading of that sentence — so
        counting it here is what makes the message true rather than merely
        larger. This is the cascade described in the plan — revocation, not row
        deletion, is the primary teardown mechanism.

        The desktop half runs in the same transaction as the CLI half, so a
        caller can never be told sessions were disconnected while some of them
        are still live.
        """
        from app.services.desktop_auth.desktop_auth_service import DesktopAuthService

        token = db.get(CLIToken, token_id)
        if not token or token.token_type != "cli-account":
            raise ValueError("Account token not found")
        if token.owner_id != user.id:
            raise ValueError("Not allowed to revoke this token")

        revoked = 0
        if not token.is_revoked:
            token.is_revoked = True
            db.add(token)
            revoked += 1

        child_stmt = select(CLIToken).where(
            CLIToken.minted_by_account_token_id == token.id,
            CLIToken.is_revoked == False,  # noqa: E712
        )
        for child in db.exec(child_stmt).all():
            child.is_revoked = True
            db.add(child)
            revoked += 1

        revoked += DesktopAuthService.revoke_clients_for_account_token(db, token.id)

        db.commit()
        return revoked

    @staticmethod
    async def revoke_child_token(
        db: Session,
        account_token: CLIToken,
        child_token_id: uuid.UUID,
        request: Request,
    ) -> None:
        """
        Soft-revoke a single child token minted by *this* account token.

        Used by ``cinna agent unsync`` so the CLI can revoke its own minted
        child token server-side (the child gets 401 on its next API call).

        Authorization is provenance-scoped, not just ownership: the target must
        be a ``token_type="cli"`` child whose ``minted_by_account_token_id``
        equals the calling account token's id. Anything else — another user's
        token, a child of a *different* account token (even of the same user),
        an account token itself, or a nonexistent id — raises ``ValueError``
        (route maps to 404, existence-leak discipline). Already-revoked is a
        no-op (idempotent unsync).
        """
        child = db.get(CLIToken, child_token_id)
        if (
            not child
            or child.token_type != "cli"
            or child.minted_by_account_token_id != account_token.id
        ):
            # Do not leak existence of tokens this account token didn't mint.
            raise ValueError("Token not found")

        if child.is_revoked:
            # Idempotent: a second unsync of the same token is a no-op.
            return

        child.is_revoked = True
        db.add(child)
        db.commit()

        await SecurityEventService.create_event(
            session=db,
            user_id=account_token.owner_id,
            data=SecurityEventCreate(
                agent_id=child.agent_id,
                event_type=CLI_ACCOUNT_CHILD_TOKEN_REVOKED,
                severity="medium",
                details={
                    "account_token_id": str(account_token.id),
                    "child_token_id": str(child.id),
                    "prefix": child.prefix,
                    "ip": client_ip(request),
                },
            ),
        )

    # ── Phase 3 — convenience verbs (thin wrappers over shipped services) ─

    @staticmethod
    async def create_agent(
        db: Session,
        user: User,
        body: "AccountAgentCreateBody",
        request: Request,
    ) -> Agent:
        """Create an agent via the normal create path (thin client, Decision 3).

        Maps the minimal CLI body → ``AgentCreate`` and delegates to
        ``AgentService.create_agent``, which applies ALL platform defaults
        (default AI-credential resolution, default env template, environment
        creation) exactly as ``POST /api/v1/agents/`` does.

        O1: ``body.env_name`` (env-template selection) is **not** honored at
        create time in v1 — the normal create path hard-codes
        ``settings.DEFAULT_AGENT_ENV_NAME``. The field is accepted-but-noop.

        No Phase-3-specific SecurityEvent is written: agent creation is already a
        first-class action on the normal path; this is a normal create that
        happens to originate from the CLI.
        """
        from app.models.agents.agent import AgentCreate

        # Resolve the target workspace from the account workspace's active-
        # workspace config. ``None`` = Default (unassigned). A non-null value
        # must belong to the account user, else the agent would be orphaned into
        # an invisible/foreign workspace — raise ``WorkspaceNotFoundError``
        # (route → 404, existence-leak discipline).
        workspace_id = AccountCLIService._resolve_owned_workspace_id(
            db, user, body.user_workspace_id
        )

        agent = await AgentService.create_agent(
            session=db,
            user_id=user.id,
            data=AgentCreate(
                name=body.name,
                description=body.description,
                user_workspace_id=workspace_id,
            ),
            user=user,
        )
        return agent

    @staticmethod
    def _resolve_owned_workspace_id(
        db: Session,
        user: User,
        workspace_id: uuid.UUID | None,
    ) -> uuid.UUID | None:
        """Validate that ``workspace_id`` (if given) belongs to ``user``.

        Returns the id unchanged when valid, ``None`` for the Default workspace.
        Raises ``WorkspaceNotFoundError`` when the workspace is missing or owned
        by another user — the route maps it to 404 so a foreign workspace id is
        not confirmed to exist (existence-leak discipline).
        """
        if workspace_id is None:
            return None

        from app.services.users.user_workspace_service import UserWorkspaceService

        workspace = UserWorkspaceService.get_workspace(db, workspace_id)
        if not workspace or workspace.user_id != user.id:
            raise WorkspaceNotFoundError("Workspace not found")
        return workspace_id

    @staticmethod
    def list_user_workspaces(db: Session, user: User) -> "UserWorkspacesPublic":
        """List the account user's workspaces for ``cinna account user-workspace
        list`` / ``--activate`` validation.

        Account-token-reachable projection over the user's own workspaces. The
        active-workspace *selection* lives client-side in ``.cinna/account.json``;
        this endpoint only supplies the catalogue the CLI lists and validates the
        activated id against. No server-side "active workspace" state is kept.
        """
        from app.models.users.user_workspace import (
            UserWorkspacePublic,
            UserWorkspacesPublic,
        )
        from app.services.users.user_workspace_service import UserWorkspaceService

        workspaces = UserWorkspaceService.get_user_workspaces(db, user.id)
        return UserWorkspacesPublic(
            data=[UserWorkspacePublic.model_validate(ws) for ws in workspaces],
            count=len(workspaces),
        )

    @staticmethod
    async def connect_agent_api(
        db: Session,
        user: User,
        body: "AccountConnectAgentApiBody",
        request: Request,
    ) -> "ConnectAgentApiResponse":
        """Wrap the ``agent_api`` one-click connect helper.

        Producer-ownership + consumer-ownership are enforced by the underlying
        ``AgentApiTokenService.connect_agent_api`` (reused verbatim, incl. its
        403/404 mapping). Emits ``CLI_ACCOUNT_CONNECT_AGENT_API`` on success.
        """
        from app.models.agent_api.agent_api_token import ConnectAgentApiRequest
        from app.services.agent_api.agent_api_token_service import (
            AgentApiTokenService,
        )

        result = await AgentApiTokenService.connect_agent_api(
            session=db,
            producer_agent_id=body.producer_agent_id,
            user_id=user.id,
            data=ConnectAgentApiRequest(
                credential_label=body.credential_label,
                read_only_override=body.read_only_override,
                consumer_agent_id=body.consumer_agent_id,
            ),
            is_superuser=user.is_superuser,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=result.linked_consumer_agent_id,
                event_type=CLI_ACCOUNT_CONNECT_AGENT_API,
                severity="medium",
                details={
                    "producer_agent_id": str(body.producer_agent_id),
                    "credential_id": str(result.credential_id),
                    "token_prefix": result.token_prefix,
                    "ip": client_ip(request),
                },
            ),
        )
        return result

    # ── Agent REST API producer management ───────────────────────────────
    # Reach the producer-side enable / refresh / spec actions through the
    # account token. Ownership is enforced by ``resolve_agent_only`` (404
    # no-leak); the underlying work is delegated to the same services the UI
    # uses (``AgentService.update_agent`` and ``AgentApiService``), so the
    # account verbs add no new behaviour beyond a thin, audited entry point.

    @staticmethod
    def _resolve_agent_api_env(
        db: Session, agent: "Agent"
    ) -> "AgentEnvironment | None":
        """The agent's active environment, or ``None`` (suspended / absent)."""
        if not agent.active_environment_id:
            return None
        return db.get(AgentEnvironment, agent.active_environment_id)

    @staticmethod
    async def set_agent_api_enabled(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        enabled: bool,
        request: Request,
    ) -> dict:
        """Toggle a producer agent's REST API on/off and return its status.

        Mirrors the UI ``PUT /agents/{id}`` ``agent_api_enabled`` toggle.
        Ownership is checked up front (404 no-leak via ``resolve_agent_only``);
        the field flip goes through ``AgentService.update_agent`` (the same path
        the UI uses). Emits ``CLI_ACCOUNT_AGENT_API_ENABLED`` on success.
        """
        from app.models.agents.agent import AgentUpdate
        from app.services.agent_api.agent_api_service import AgentApiService

        # 404 no-leak ownership check (does NOT require a running env).
        AgentApiService.resolve_agent_only(
            db, agent_id, user.id, is_superuser=user.is_superuser
        )

        agent = await AgentService.update_agent(
            session=db,
            agent_id=agent_id,
            data=AgentUpdate(agent_api_enabled=enabled),
            user_id=user.id,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent_id,
                event_type=CLI_ACCOUNT_AGENT_API_ENABLED,
                severity="medium",
                details={
                    "enabled": enabled,
                    "ip": client_ip(request),
                },
            ),
        )

        environment = AccountCLIService._resolve_agent_api_env(db, agent)
        return await AgentApiService.get_status(db, agent, environment)

    @staticmethod
    async def refresh_agent_api(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        request: Request,
    ) -> dict:
        """Force an on-demand spec + policy re-harvest; return the status.

        Mirrors the producer ``POST /_refresh`` action. Best-effort: only
        meaningful when enabled + env running; the harvest error (if any) is
        persisted by ``get_spec`` and surfaced via the returned status's
        ``last_error`` (this never raises on a harvest failure). Not audited —
        a re-harvest is diagnostic, not a state-changing grant.
        """
        from app.services.agent_api.agent_api_service import (
            AgentApiError,
            AgentApiService,
        )

        # 404 no-leak ownership check.
        agent = AgentApiService.resolve_agent_only(
            db, agent_id, user.id, is_superuser=user.is_superuser
        )
        environment = AccountCLIService._resolve_agent_api_env(db, agent)

        if (
            agent.agent_api_enabled
            and environment is not None
            and environment.status == "running"
        ):
            try:
                await AgentApiService.get_spec(db, environment, force_refresh=True)
            except AgentApiError:
                pass  # persisted; surfaced via the status payload
            try:
                await AgentApiService.load_policy(
                    db, environment, force_refresh=True
                )
            except Exception:  # best-effort (matches the UI _refresh route)
                logger.debug(
                    "account agent-api refresh policy reload failed for env %s",
                    environment.id,
                )

        return await AgentApiService.get_status(db, agent, environment)

    @staticmethod
    async def get_agent_api_spec(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
    ) -> dict:
        """Return the producer's harvested OpenAPI spec (cache or import-only).

        Mirrors the owner ``GET /openapi.json`` preview. Requires the agent to be
        owned (404 no-leak), ``agent_api_enabled``, and a reachable spec (cache or
        a running env to harvest from). Keeps the env warm while inspecting.
        """
        from app.services.agent_api.agent_api_service import AgentApiService

        agent, environment = AgentApiService.resolve_producer_environment(
            db, agent_id, user.id, is_superuser=user.is_superuser
        )
        spec = await AgentApiService.get_spec(db, environment)
        # Keep the env alive while the spec is inspected (mirrors the UI route).
        AgentApiService.update_last_activity(db, environment)
        return spec

    @staticmethod
    async def call_agent_api(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        method: str,
        path: str,
        query: dict | None,
        json_body,
    ) -> dict:
        """Owner-side smoke test of a producer endpoint — ``cinna agent-api call``.

        Invokes one endpoint on the producer's *own* REST API through the
        owner-preview proxy (no consumer token, no policy edge — same as the UI
        "try it" proxy). Buffers the response and returns
        ``{status_code, headers, body, is_json}``. Diagnostic, not audited (no
        ``request`` needed — there is nothing to attribute an IP to).

        Requires the agent to be owned (404 no-leak), ``agent_api_enabled``, and
        a running env (503 otherwise — the consumer would see the same). Query
        params are forwarded verbatim so this catches a silent query-drop.
        """
        import json as _json
        from urllib.parse import urlencode

        from app.services.agent_api.agent_api_service import (
            AgentApiError,
            AgentApiService,
        )
        from app.services.environments.environment_service import EnvironmentService

        agent, environment = AgentApiService.resolve_producer_environment(
            db, agent_id, user.id, is_superuser=user.is_superuser
        )

        headers: dict[str, str] = {}
        body: bytes | None = None
        if json_body is not None:
            body = _json.dumps(json_body).encode("utf-8")
            headers["content-type"] = "application/json"

        query_string = urlencode(query, doseq=True) if query else ""
        norm_path = path.lstrip("/")

        lifecycle = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle.get_adapter(environment)
        try:
            status_code, resp_headers, body_bytes = await adapter.proxy_agent_api(
                method=method,
                path=norm_path,
                headers=headers,
                body=body,
                stream=False,
                query_string=query_string,
            )
        except Exception as e:  # transport failure (env up, child unreachable)
            # Mirror the UI owner-preview proxy: a proxy transport error is a 502.
            raise AgentApiError(
                f"Agent API proxy error: {e}", status_code=502
            ) from e
        AgentApiService.update_last_activity(db, environment)

        if isinstance(body_bytes, (bytes, bytearray)):
            text = bytes(body_bytes).decode("utf-8", errors="replace")
        else:  # defensive — non-stream path always returns bytes
            text = str(body_bytes)
        content_type = (resp_headers or {}).get("content-type", "")
        return {
            "status_code": status_code,
            "headers": dict(resp_headers or {}),
            "body": text,
            "is_json": "json" in content_type.lower(),
        }

    @staticmethod
    async def restart_agent_env(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        request: Request,
    ) -> dict:
        """Restart an agent's active environment — ``cinna agent restart-env``.

        Wraps ``EnvironmentService.restart_environment`` (the same path the UI's
        restart button drives) so a builder can recover a stuck env / serving
        child without discovering the raw ``environments/{id}/restart`` route.
        Build-rights gated (``assert_can_build`` → 404 no-leak / 403); the call
        blocks until the container is back, then returns the post-restart status.
        Emits ``CLI_ACCOUNT_ENV_RESTARTED``.

        Raises ``ValueError`` if the agent has no active environment (→ 400).
        """
        from app.services.agent_api.agent_api_service import AgentApiService
        from app.services.environments.environment_service import EnvironmentService

        # 404 no-leak existence/ownership check, then build-rights gate.
        agent = AgentApiService.resolve_agent_only(
            db, agent_id, user.id, is_superuser=user.is_superuser
        )
        AgentService.assert_can_build(db, user, agent)

        if not agent.active_environment_id:
            raise ValueError("Agent has no active environment to restart.")

        environment = await EnvironmentService.restart_environment(
            session=db, env_id=agent.active_environment_id
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent_id,
                event_type=CLI_ACCOUNT_ENV_RESTARTED,
                severity="medium",
                details={
                    "environment_id": str(environment.id),
                    "ip": client_ip(request),
                },
            ),
        )

        return {
            "environment_id": environment.id,
            "status": environment.status,
            "status_message": environment.status_message,
        }

    @staticmethod
    async def inspect_agent(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
    ) -> dict:
        """Effective agent config as the runtime sees it — ``cinna agent show``.

        Aggregates the agent's prompts (the DB fields that sync verbatim into the
        workspace prompt docs the runtime reads), enabled features, and connected
        credential metadata (name + type ONLY — never a secret), plus the live
        agent-api status when the REST API is enabled. Ownership-checked (404
        no-leak). Diagnostic read, not audited.
        """
        from app.services.agent_api.agent_api_service import AgentApiService
        from app.services.credentials.credentials_service import CredentialsService

        agent = AgentApiService.resolve_agent_only(
            db, agent_id, user.id, is_superuser=user.is_superuser
        )

        credentials = CredentialsService.get_agent_credentials(db, agent_id)
        cred_items = [
            {"name": c.name, "type": c.type} for c in credentials
        ]

        agent_api_status: dict | None = None
        if agent.agent_api_enabled:
            environment = AccountCLIService._resolve_agent_api_env(db, agent)
            try:
                agent_api_status = await AgentApiService.get_status(
                    db, agent, environment
                )
            except Exception as exc:  # best-effort; inspection still returns
                logger.debug(
                    "account inspect_agent: agent-api status failed for %s: %s",
                    agent_id, exc,
                )

        return {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "features": {
                "is_active": agent.is_active,
                "show_on_dashboard": agent.show_on_dashboard,
                "webapp_enabled": agent.webapp_enabled,
                "agent_api_enabled": agent.agent_api_enabled,
            },
            "prompts": {
                "entrypoint": agent.entrypoint_prompt,
                "workflow": agent.workflow_prompt,
                "refiner": agent.refiner_prompt,
            },
            "credentials": cred_items,
            "agent_api_status": agent_api_status,
        }

    # ── Schedule management (full CRUD via dedicated account verbs) ───────
    # Mirror the agent Config → Schedules card. Ownership is enforced
    # 404-no-leak via ``resolve_agent_only``; foreign (bundle-consumer) installs
    # keep the same read-only contract as the UI (toggle / Run now / logs only —
    # the schedule *definitions* are publisher-authored). Each write audits one
    # event; list / generate-preview / logs are diagnostic reads (not audited).

    @staticmethod
    def _resolve_owned_agent(db: Session, user: User, agent_id: uuid.UUID) -> "Agent":
        """404-no-leak ownership resolve shared by the schedule + status verbs."""
        from app.services.agent_api.agent_api_service import AgentApiService

        return AgentApiService.resolve_agent_only(
            db, agent_id, user.id, is_superuser=user.is_superuser
        )

    @staticmethod
    def _guard_foreign_schedule_write(agent: "Agent") -> None:
        """403 when mutating schedule *definitions* on a foreign install.

        Mirrors the UI route guard: a consumer (bundle-owned, non-publisher)
        install may toggle / run / view logs, but the create / edit / delete of
        the definitions belongs to the publisher.
        """
        from app.services.agents.agent_scheduler_service import ScheduleError

        if AgentService.is_foreign_install(agent):
            raise ScheduleError(
                "Schedules on an installed bundle are managed by the publisher "
                "and cannot be created, edited, or deleted. You can "
                "enable/disable, run, and view logs.",
                status_code=403,
            )

    @staticmethod
    def list_schedules(
        db: Session, user: User, agent_id: uuid.UUID
    ) -> "AgentSchedulesPublic":
        """List an agent's schedules (``cinna agent schedule list``). Read."""
        from app.models.agents.agent_schedule import AgentSchedulesPublic
        from app.services.agents.agent_scheduler_service import AgentSchedulerService

        AccountCLIService._resolve_owned_agent(db, user, agent_id)
        schedules = AgentSchedulerService.get_agent_schedules(db, agent_id)
        return AgentSchedulesPublic(data=schedules, count=len(schedules))

    @staticmethod
    def generate_schedule(
        db: Session, user: User, agent_id: uuid.UUID, body: "ScheduleRequest"
    ) -> "ScheduleResponse":
        """Natural-language → CRON preview (``cinna agent schedule generate``).

        Stateless: nothing is persisted. Lets the CLI turn "every weekday at
        7am" into a cron string + next-execution preview before ``create``.
        """
        from app.models.agents.agent_schedule import ScheduleResponse
        from app.services.agents.agent_scheduler_service import AgentSchedulerService

        AccountCLIService._resolve_owned_agent(db, user, agent_id)
        result = AgentSchedulerService.generate_schedule_preview(
            natural_language=body.natural_language,
            timezone=body.timezone,
            schedule_type=body.schedule_type,
            user=user,
            db=db,
        )
        return ScheduleResponse(**result)

    @staticmethod
    async def create_schedule(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        body: "CreateScheduleRequest",
        request: Request,
    ) -> "AgentSchedule":
        """Create a schedule (``cinna agent schedule create``).

        Foreign-install-guarded; validates the type/command combo exactly like
        the UI route; the cron-string is converted local→UTC and frequency-floor
        validated inside ``AgentSchedulerService.create_schedule``. Audits
        ``CLI_ACCOUNT_SCHEDULE_CREATED``.
        """
        from app.services.agents.agent_scheduler_service import (
            AgentSchedulerService,
            ScheduleError,
        )

        agent = AccountCLIService._resolve_owned_agent(db, user, agent_id)
        AccountCLIService._guard_foreign_schedule_write(agent)

        if body.schedule_type == "script_trigger":
            if not body.command or not body.command.strip():
                raise ScheduleError(
                    "command is required for script_trigger schedules",
                    status_code=400,
                )
        elif body.schedule_type != "static_prompt":
            raise ScheduleError(
                f"Invalid schedule_type '{body.schedule_type}'. Must be "
                "'static_prompt' or 'script_trigger'",
                status_code=400,
            )

        schedule = AgentSchedulerService.create_schedule(
            session=db,
            agent_id=agent_id,
            name=body.name,
            cron_string=body.cron_string,
            timezone=body.timezone,
            description=body.description,
            prompt=body.prompt,
            enabled=body.enabled,
            schedule_type=body.schedule_type,
            command=body.command,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent_id,
                event_type=CLI_ACCOUNT_SCHEDULE_CREATED,
                severity="low",
                details={
                    "schedule_id": str(schedule.id),
                    "schedule_type": schedule.schedule_type,
                    "ip": client_ip(request),
                },
            ),
        )
        return schedule

    @staticmethod
    async def update_schedule(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        schedule_id: uuid.UUID,
        body: "UpdateScheduleRequest",
        request: Request,
    ) -> "AgentSchedule":
        """Partial-update / toggle a schedule (``cinna agent schedule update``).

        On a foreign install only ``enabled`` may change (the toggle the
        consumer is allowed); any other set field → 403, mirroring the UI route.
        Audits ``CLI_ACCOUNT_SCHEDULE_UPDATED``.
        """
        from app.services.agents.agent_scheduler_service import (
            AgentSchedulerService,
            ScheduleError,
        )

        agent = AccountCLIService._resolve_owned_agent(db, user, agent_id)
        schedule = AgentSchedulerService.get_schedule_for_agent(
            db, agent_id, schedule_id
        )
        fields = body.model_dump(exclude_unset=True)

        if AgentService.is_foreign_install(agent) and set(fields.keys()) - {"enabled"}:
            raise ScheduleError(
                "Schedules on an installed bundle are managed by the publisher. "
                "You can only enable or disable them.",
                status_code=403,
            )

        updated = AgentSchedulerService.update_schedule(db, schedule, **fields)

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent_id,
                event_type=CLI_ACCOUNT_SCHEDULE_UPDATED,
                severity="low",
                details={
                    "schedule_id": str(schedule_id),
                    "fields": sorted(fields.keys()),
                    "ip": client_ip(request),
                },
            ),
        )
        return updated

    @staticmethod
    async def delete_schedule(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        schedule_id: uuid.UUID,
        request: Request,
    ) -> None:
        """Delete a schedule (``cinna agent schedule delete``).

        Foreign-install-guarded (publisher-managed definitions). Audits
        ``CLI_ACCOUNT_SCHEDULE_DELETED``.
        """
        from app.services.agents.agent_scheduler_service import AgentSchedulerService

        agent = AccountCLIService._resolve_owned_agent(db, user, agent_id)
        AccountCLIService._guard_foreign_schedule_write(agent)
        schedule = AgentSchedulerService.get_schedule_for_agent(
            db, agent_id, schedule_id
        )
        AgentSchedulerService.delete_schedule(db, schedule)

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent_id,
                event_type=CLI_ACCOUNT_SCHEDULE_DELETED,
                severity="low",
                details={
                    "schedule_id": str(schedule_id),
                    "ip": client_ip(request),
                },
            ),
        )

    @staticmethod
    async def run_schedule(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        schedule_id: uuid.UUID,
        request: Request,
    ) -> "ManualRunResult":
        """Trigger a schedule immediately (``cinna agent schedule run``).

        Allowed on foreign installs too (Run now is a consumer-permitted action).
        Returns the ``ManualRunResult`` (the route maps ``action`` → message).
        Audits ``CLI_ACCOUNT_SCHEDULE_RUN`` (run spends tokens / spins a session).
        """
        from app.services.agents.agent_scheduler_service import AgentSchedulerService

        AccountCLIService._resolve_owned_agent(db, user, agent_id)
        AgentSchedulerService.get_schedule_for_agent(db, agent_id, schedule_id)
        result = await AgentSchedulerService.execute_now(db, agent_id, schedule_id)

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent_id,
                event_type=CLI_ACCOUNT_SCHEDULE_RUN,
                severity="low",
                details={
                    "schedule_id": str(schedule_id),
                    "action": result.action,
                    "ip": client_ip(request),
                },
            ),
        )
        return result

    @staticmethod
    def get_schedule_logs(
        db: Session, user: User, agent_id: uuid.UUID, schedule_id: uuid.UUID
    ) -> "AgentScheduleLogsPublic":
        """List a schedule's execution logs (``cinna agent schedule logs``). Read."""
        from app.models.agents.agent_schedule_log import AgentScheduleLogsPublic
        from app.services.agents.agent_scheduler_service import AgentSchedulerService

        AccountCLIService._resolve_owned_agent(db, user, agent_id)
        AgentSchedulerService.get_schedule_for_agent(db, agent_id, schedule_id)
        logs = AgentSchedulerService.get_schedule_logs(db, schedule_id)
        return AgentScheduleLogsPublic(data=logs, count=len(logs))

    # ── Status management (access / refresh / set pre-command) ───────────
    # Mirror the agent Integrations → Agent status card. The status read +
    # force-refresh are diagnostic (not audited, mirroring the REST status
    # surface); setting the status-refresh pre-command is a config state change
    # and is audited.

    @staticmethod
    def _status_snapshot_to_public(snapshot, agent_id: uuid.UUID) -> "AgentStatusPublic":
        """Mirror of ``agent_status.py::_snapshot_to_public`` for the account verb."""
        from app.models.agents.agent_status import AgentStatusPublic

        return AgentStatusPublic(
            agent_id=agent_id,
            environment_id=snapshot.environment_id,
            severity=snapshot.severity,
            summary=snapshot.summary,
            reported_at=snapshot.reported_at,
            reported_at_source=snapshot.reported_at_source,
            fetched_at=snapshot.fetched_at,
            raw=snapshot.raw,
            body=snapshot.body,
            has_structured_metadata=snapshot.has_structured_metadata,
            prev_severity=snapshot.prev_severity,
            severity_changed_at=snapshot.severity_changed_at,
            refresh_command_warning=snapshot.refresh_command_warning,
        )

    @staticmethod
    async def get_agent_status(
        db: Session, user: User, agent_id: uuid.UUID, force_refresh: bool
    ) -> "AccountAgentStatusResult":
        """Status snapshot + configured pre-command (``cinna agent status``).

        ``force_refresh=True`` runs the full force flow (wake suspended env →
        pre-command → live STATUS.md read → cache fallback) via the single
        ``force_refresh_status`` entrypoint; otherwise the cached snapshot is
        returned. Ownership 404-no-leak. Read — not audited.
        """
        from app.models.agents.agent_status import AgentStatusPublic
        from app.models.cli.account_convenience import AccountAgentStatusResult
        from app.services.agents.agent_status_service import AgentStatusService

        agent = AccountCLIService._resolve_owned_agent(db, user, agent_id)
        environment = AgentStatusService.get_primary_environment(
            db, agent_id, agent.active_environment_id
        )
        if not environment:
            status_public = AgentStatusPublic(agent_id=agent_id)
        else:
            if force_refresh:
                snapshot = await AgentStatusService.force_refresh_status(
                    environment, agent=agent
                )
            else:
                snapshot = AgentStatusService.get_cached_status(environment)
            status_public = AccountCLIService._status_snapshot_to_public(
                snapshot, agent_id
            )

        return AccountAgentStatusResult(
            status=status_public,
            status_refresh_command=agent.status_refresh_command,
        )

    @staticmethod
    async def set_status_refresh_command(
        db: Session,
        user: User,
        agent_id: uuid.UUID,
        command: str | None,
        request: Request,
    ) -> "AccountAgentStatusResult":
        """Set the status-refresh pre-command (``cinna agent status set-command``).

        Mirrors the Integrations → Agent status card command input — flips
        ``status_refresh_command`` through ``AgentService.update_agent`` (the same
        path the UI ``PATCH /agents/{id}`` uses). Returns the (cached) status plus
        the new command value. Audits ``CLI_ACCOUNT_STATUS_COMMAND_SET``.
        """
        from app.models.agents.agent import AgentUpdate
        from app.models.agents.agent_status import AgentStatusPublic
        from app.models.cli.account_convenience import AccountAgentStatusResult
        from app.services.agents.agent_status_service import AgentStatusService

        # 404 no-leak ownership check (does NOT require a running env).
        AccountCLIService._resolve_owned_agent(db, user, agent_id)

        agent = await AgentService.update_agent(
            session=db,
            agent_id=agent_id,
            data=AgentUpdate(status_refresh_command=command),
            user_id=user.id,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent_id,
                event_type=CLI_ACCOUNT_STATUS_COMMAND_SET,
                severity="low",
                details={
                    "command": command,
                    "ip": client_ip(request),
                },
            ),
        )

        environment = AgentStatusService.get_primary_environment(
            db, agent_id, agent.active_environment_id
        )
        if environment:
            status_public = AccountCLIService._status_snapshot_to_public(
                AgentStatusService.get_cached_status(environment), agent_id
            )
        else:
            status_public = AgentStatusPublic(agent_id=agent_id)

        return AccountAgentStatusResult(
            status=status_public,
            status_refresh_command=agent.status_refresh_command,
        )

    @staticmethod
    def list_discoverable_mcp_agents(
        db: Session,
        user: User,
        consumer_agent_id: uuid.UUID | None = None,
    ) -> "DiscoverableAgents":
        """Account-token passthrough to the MCP discoverable-agents picker (O2).

        Lets the CLI map ``--producer <agent>`` → ``connector_id`` before calling
        connect, without making the (user-JWT-only) discoverable route
        account-token-reachable.
        """
        from app.models.mcp.mcp_provider import DiscoverableAgents
        from app.services.mcp_providers.mcp_provider_service import (
            MCPProviderService,
        )

        agents = MCPProviderService.list_discoverable_agents(
            db, user, consumer_agent_id=consumer_agent_id
        )
        return DiscoverableAgents(data=agents, count=len(agents))

    @staticmethod
    async def connect_mcp(
        db: Session,
        user: User,
        body: "AccountConnectMcpBody",
        request: Request,
    ) -> "MCPProviderConnectionResponse":
        """Wrap the ``mcp_provider`` agent2agent connect helper.

        Producer-connector ACL membership + consumer-ownership are enforced by
        the underlying ``MCPProviderService.connect_to_agent`` (reused verbatim,
        incl. its 403/404 mapping). Emits ``CLI_ACCOUNT_CONNECT_MCP`` on success.
        """
        from app.models.mcp.mcp_provider import ConnectMcpProviderAgentRequest
        from app.services.mcp_providers.mcp_provider_service import (
            MCPProviderService,
        )

        result = await MCPProviderService.connect_to_agent(
            session=db,
            user=user,
            data=ConnectMcpProviderAgentRequest(
                connector_id=body.connector_id,
                consumer_agent_id=body.consumer_agent_id,
                mcp_mode_conversation=body.mcp_mode_conversation,
                mcp_mode_building=body.mcp_mode_building,
                label=body.label,
            ),
            is_superuser=user.is_superuser,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=result.linked_consumer_agent_id,
                event_type=CLI_ACCOUNT_CONNECT_MCP,
                severity="medium",
                details={
                    "connector_id": str(body.connector_id),
                    "credential_id": str(result.credential_id),
                    "ip": client_ip(request),
                },
            ),
        )
        return result

    # ── Credential drafting verbs (metadata + structure only) ────────────
    # SECURITY INVARIANT: none of these methods ever reads or writes a
    # credential's secret VALUE. The account CLI scaffolds *drafts* (empty data →
    # ``status="incomplete"``) and wires them to agents; the user fills the secret
    # in the UI. ``credential_data`` is never accepted from the account body and
    # ``with-data`` is never called. This preserves Decision 6 (no credential
    # secrets via the account token) for writes as well as reads.
    #
    # Scope note, so the invariant is not read as more than it is: it is about
    # what *these routes* return. ``exchange_for_desktop_token`` (below) hands
    # the caller a standard user JWT, which can call ``with-data`` — read its
    # SECURITY POSITION before treating Decision 6 as a property of anyone
    # holding an account CLI token.

    @staticmethod
    def _credential_public(db: Session, credential) -> "CredentialPublic":
        """Project a ``Credential`` → ``CredentialPublic`` (no secret values).

        Mirrors the credentials route's owner projection: ``share_count`` +
        computed completeness ``status``. Decryption happens only to compute the
        status server-side; the plaintext never leaves this function.
        """
        from app.models.credentials.credential import CredentialPublic
        from app.services.credentials.credential_share_service import (
            CredentialShareService,
        )
        from app.services.credentials.credentials_service import CredentialsService

        share_count = CredentialShareService.get_share_count_for_credential(
            session=db, credential_id=credential.id
        )
        credential_data = CredentialsService.decrypt_credential_data(
            session=db, credential=credential
        )
        status = CredentialsService.check_credential_completeness(
            credential_type=credential.type.value,
            credential_data=credential_data,
        )
        return CredentialPublic(
            id=credential.id,
            name=credential.name,
            type=credential.type,
            notes=credential.notes,
            allow_sharing=credential.allow_sharing,
            allow_template_sharing=credential.allow_template_sharing,
            service_uri=credential.service_uri,
            template_private_fields=list(credential.template_private_fields or []),
            owner_id=credential.owner_id,
            user_workspace_id=credential.user_workspace_id,
            share_count=share_count,
            is_shared=False,
            owner_email=None,
            is_placeholder=credential.is_placeholder,
            placeholder_source_id=credential.placeholder_source_id,
            status=status,
        )

    @staticmethod
    def _required_fields_for(credential_type: str) -> list[str]:
        """The secret/config fields the user must fill for this type to be
        ``complete`` (from the platform's per-type required-field map)."""
        from app.services.credentials.credentials_service import CredentialsService

        return list(CredentialsService.REQUIRED_FIELDS.get(credential_type, []))

    @staticmethod
    def list_credentials(
        db: Session,
        user: User,
        user_workspace_id: str | None = None,
    ) -> "CredentialsPublic":
        """List the account user's credentials (metadata only — no values).

        Mirrors ``GET /credentials/`` workspace-filter semantics: ``None`` = all,
        ``""`` = Default workspace (NULL), a UUID string = that workspace.
        """
        from app.models.credentials.credential import Credential, CredentialsPublic

        stmt = select(Credential).where(Credential.owner_id == user.id)
        if user_workspace_id is not None:
            if user_workspace_id == "":
                stmt = stmt.where(Credential.user_workspace_id == None)  # noqa: E711
            else:
                try:
                    ws = uuid.UUID(user_workspace_id)
                except ValueError:
                    raise WorkspaceNotFoundError("Invalid workspace id")
                stmt = stmt.where(Credential.user_workspace_id == ws)

        credentials = list(db.exec(stmt).all())
        data = [
            AccountCLIService._credential_public(db, c) for c in credentials
        ]
        return CredentialsPublic(data=data, count=len(data))

    @staticmethod
    def list_credential_types() -> "AccountCredentialTypesPublic":
        """Static catalogue of credential types + their required fields.

        Lets the orchestrator pick a type and tell the user exactly which fields
        they will need to fill after the draft is created.
        """
        from app.models.cli.account_convenience import (
            AccountCredentialTypeInfo,
            AccountCredentialTypesPublic,
        )
        from app.models.credentials.credential import CredentialType

        items: list[AccountCredentialTypeInfo] = []
        for ct in CredentialType:
            note = None
            if ct == CredentialType.API_TOKEN:
                note = (
                    "Bearer tokens need only 'api_token'; the 'custom' variant also "
                    "needs 'api_token_template'."
                )
            items.append(
                AccountCredentialTypeInfo(
                    type=ct,
                    required_fields=AccountCLIService._required_fields_for(ct.value),
                    note=note,
                )
            )
        return AccountCredentialTypesPublic(data=items, count=len(items))

    @staticmethod
    async def create_credential_draft(
        db: Session,
        user: User,
        body: "AccountCredentialCreateBody",
        request: Request,
    ) -> "AccountCredentialDraftResult":
        """Create a draft credential (no secret value) in the active workspace.

        The credential is created empty → ``status="incomplete"``; the response
        carries the ``required_fields`` the user must fill and a ``setup_url``
        deep-link to the Credentials page. Raises ``WorkspaceNotFoundError`` for a
        foreign workspace id (route → 404).
        """
        from app.models.cli.account_convenience import AccountCredentialDraftResult
        from app.models.credentials.credential import CredentialCreate
        from app.services.credentials.credentials_service import CredentialsService

        workspace_id = AccountCLIService._resolve_owned_workspace_id(
            db, user, body.user_workspace_id
        )

        credential = CredentialsService.create_credential(
            session=db,
            credential_in=CredentialCreate(
                name=body.name,
                type=body.type,
                notes=body.notes,
                service_uri=body.service_uri,
                allow_sharing=body.allow_sharing,
                # No secret value — the user fills it in the UI.
                credential_data=None,
                user_workspace_id=workspace_id,
            ),
            owner_id=user.id,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=CLI_ACCOUNT_CREDENTIAL_CREATED,
                severity="medium",
                details={
                    "credential_id": str(credential.id),
                    "type": credential.type.value,
                    "ip": client_ip(request),
                },
            ),
        )

        frontend_url = settings.FRONTEND_HOST.rstrip("/")
        return AccountCredentialDraftResult(
            credential=AccountCLIService._credential_public(db, credential),
            required_fields=AccountCLIService._required_fields_for(
                credential.type.value
            ),
            setup_url=f"{frontend_url}/credentials",
        )

    @staticmethod
    async def update_credential_metadata(
        db: Session,
        user: User,
        credential_id: uuid.UUID,
        body: "AccountCredentialUpdateBody",
        request: Request,
    ) -> "CredentialPublic":
        """Update a credential's metadata only (never its secret value).

        Builds a ``CredentialUpdate`` from the provided safe fields only and
        delegates to ``CredentialsService.update_credential`` (which re-syncs
        affected agent envs). Raises ``ValueError`` on missing/forbidden (route →
        404/400).
        """
        from app.models.credentials.credential import Credential, CredentialUpdate
        from app.services.credentials.credentials_service import CredentialsService

        provided = body.model_dump(exclude_unset=True)
        # Defensive: the account body has no credential_data field, but never let
        # one through even if a future edit adds it.
        provided.pop("credential_data", None)

        await CredentialsService.update_credential(
            session=db,
            credential_id=credential_id,
            credential_in=CredentialUpdate(**provided),
            owner_id=user.id,
            is_superuser=user.is_superuser,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=CLI_ACCOUNT_CREDENTIAL_UPDATED,
                severity="medium",
                details={
                    "credential_id": str(credential_id),
                    "fields": sorted(provided.keys()),
                    "ip": client_ip(request),
                },
            ),
        )

        credential = db.get(Credential, credential_id)
        return AccountCLIService._credential_public(db, credential)

    @staticmethod
    async def delete_credential(
        db: Session,
        user: User,
        credential_id: uuid.UUID,
        force: bool,
        request: Request,
    ) -> None:
        """Delete a credential (tier-gated, reuses the blast-radius guard).

        Propagates ``CredentialInUseError`` (route → 409 with impact) and
        ``ValueError`` (route → 404/400). Emits the audit only after a successful
        delete.
        """
        from app.services.credentials.credentials_service import CredentialsService

        await CredentialsService.delete_credential(
            session=db,
            credential_id=credential_id,
            owner_id=user.id,
            is_superuser=user.is_superuser,
            force=force,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=CLI_ACCOUNT_CREDENTIAL_DELETED,
                severity="medium",
                details={
                    "credential_id": str(credential_id),
                    "force": force,
                    "ip": client_ip(request),
                },
            ),
        )

    @staticmethod
    async def share_credential_with_agent(
        db: Session,
        user: User,
        credential_id: uuid.UUID,
        agent_id: uuid.UUID,
        request: Request,
    ) -> None:
        """Attach a credential to an agent the account user owns.

        Delegates to ``CredentialsService.link_credential_to_agent`` (ownership +
        access checks, idempotent, re-syncs the agent env). Raises ``ValueError``
        on missing/forbidden (route → 404/400).
        """
        from app.services.credentials.credentials_service import CredentialsService

        await CredentialsService.link_credential_to_agent(
            session=db,
            agent_id=agent_id,
            credential_id=credential_id,
            owner_id=user.id,
            is_superuser=user.is_superuser,
        )

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=agent_id,
                event_type=CLI_ACCOUNT_CREDENTIAL_SHARED_WITH_AGENT,
                severity="medium",
                details={
                    "credential_id": str(credential_id),
                    "ip": client_ip(request),
                },
            ),
        )

    # ── Desktop token exchange (handover §8.2) ───────────────────────────

    @staticmethod
    async def exchange_for_desktop_token(
        db: Session,
        user: User,
        cli_token: CLIToken,
        body: "AccountDesktopTokenBody",
        request: Request,
    ) -> "AccountDesktopTokenResponse":
        """Trade an account CLI token for a Cinna Desktop session.

        Cinna Desktop opens a workshop tree, finds the ``account.json`` a
        previous ``cinna login`` left there, and silently links the profile
        instead of making the user type credentials into a connect card. This is
        the server half of that: prove-once-with-the-CLI-token, receive a desktop
        access + refresh pair bound to the desktop's client id, plus the account
        email so the desktop can name the profile without a second call.

        The CLI token is **used, not spent**: it is neither consumed nor revoked
        here, and its rolling expiry rolls exactly as it does on any other
        account route (``_resolve_account_cli_context``). Repeating the exchange
        is legitimate — a desktop that lost its refresh token can simply ask
        again.

        SECURITY POSITION — this is the whole question on this endpoint, so it is
        written here rather than left to be re-derived:

        **The exchange converts a deliberately scope-restricted credential into a
        broader one, and that is a real escalation, not a relabelling.** An
        account CLI token reaches ``/cli/account/*`` and nothing else
        (``AccountCLIContext`` rejects it everywhere else, and the generic
        ``api-proxy`` hatch subtracts credentials, admin, MFA, the CLI router
        itself and other clients' auth surfaces — user management too, bar two
        explicitly re-admitted GETs). The desktop access token it buys is a
        **standard user JWT**: it satisfies ``CurrentUser`` on every route,
        including ``GET /credentials/{id}/with-data``, which returns decrypted
        secret values that no account-CLI route will ever return — the very
        thing the credential-drafting invariant above exists to prevent.

        It also used to reach the platform's credential-minting surfaces, which
        made the session self-replicating: mint a *fresh* account CLI token,
        carrying no provenance link, and the original could be revoked without
        ending anything. ``ensure_not_cli_exchanged_session`` (and its ``Depends``
        adapter ``forbid_cli_exchanged_desktop_session``) closes that loop — see
        property 2 for what it does and does not buy.

        Two properties make that acceptable. Note which argument is doing the
        work, because the obvious one does not hold in general:

        1. **The capabilities it adds are mostly ones the file already carried
           — for a developer.** Whoever can read an ``account.json`` can already
           mint a per-agent CLI token for every agent that user can build
           (``POST /account/agents/{id}/mint``) and run arbitrary commands inside
           those environments (``POST /cli/agents/{id}/exec``).
           **This argument fails for a non-developer holder**, and the failure is
           reachable: ``cinna login`` device approval has no role gate, so an
           agent-user can hold an account token, and ``mint`` would refuse them
           every agent. For them the exchange is a straight escalation from
           account routes to a full user session. **That escalation was weighed
           and accepted, not relabelled away**: it is a real increase in what a
           stolen ``account.json`` yields for that group, and this docstring
           will not describe it as anything smaller. It is still allowed, on a
           different and narrower ground: that session is *their own* account,
           no more than they already get by signing in to the web app with their
           password. What the exchange never does is cross to another user.

           **RULING — the exchange is ungated by role, deliberately. Do not
           "harden" it by adding one.** Written as a ruling so that a future
           editor arriving here finds a decision rather than a gap: the absence
           of a role check is the decision. The reasoning, in the terms it was
           ruled in: **role restrictions belong in the backend's reply to a
           specific capability, not in gating authentication.** An agent-user
           develops locally all day; when they ask to publish, the backend
           refuses *the publish*. That is the platform answering a capability
           request on its own merits — it is not the CLI, and not the desktop,
           deciding by role who may hold a session at all. A gate here would put
           the refusal at the wrong layer and would still withhold nothing,
           since the session it denied is one the same user obtains by typing
           their password into the web app.

           **And on hijacking, which is the objection this ruling meets first:
           a compromised session is compromised whatever its type** — a
           role-gated one no less than this one, since the compromise is of the
           session and not of the path that issued it. The remedy is revoking it
           in the UI, not a narrower issuance path. Which is precisely why
           revoke must *actually* revoke, and what property 2's cascade and the
           badge's honesty about its own bounded gap exist for: they are the
           controls this ruling leans on, so weakening either of them is what
           would make the ruling wrong.
        2. **The result is visible, killable, and now revoked by the same act
           that revokes the token that bought it.** The client is stamped
           ``origin="cli_exchange"`` and linked to the account token
           (``_stamp_grant`` writes both, and collapses the client to a single
           live grant so neither column can be laundered by a later browser
           consent). Disconnecting from Settings revokes the refresh family and
           kills the access token on its next request —
           ``get_current_user`` re-checks the client row on every call.
           Revoking the account CLI token itself cascades into the desktop
           session too (``revoke_account_token``), and the session cannot mint
           replacement CLI credentials or a fresh native session to outlive
           that cascade.

           **What that remediation is NOT is complete, and saying otherwise is
           the most harmful thing this docstring could do.** A compromised user
           who believes one revocation ended it stops looking, and nothing in
           the system will ever correct them — no test fails, no alert fires.
           So, plainly: the exchanged session is a full user JWT for as long as
           it lives, and a full user JWT has persistence routes no gate here
           can reach. It can read decrypted credential values and use them
           anywhere, off-platform and after every token in this system is dead;
           nothing here can recall a secret that has already left.

           The gate covers the minting surfaces known to satisfy its property
           today — the two CLI setup-token routes, device-login approval, and
           the desktop and mobile **consent** endpoints, which would otherwise
           mint a whole new native session with clean browser provenance and no
           cascade link. What it cannot cover is a minting route added tomorrow
           that nobody re-derives the property against, which is why the
           dependency's docstring tells you to re-derive rather than copy a list.

           And one class of credential outlives every revocation control the
           product has, recorded here rather than fixed: the exchanged session is
           not refused on the MCP OAuth consent (``routes/mcp_consent.py``), and
           the App MCP access + refresh pair it can mint there survives this
           cascade and an App Sessions disconnect for up to thirty days, is
           listed nowhere, and is revocable only through ``POST /mcp/oauth/revoke``
           with the token *value* and no authentication. Reproduced by execution.
           Closing it is the MCP feature's territory and is raised separately.

           So revoking the account token and disconnecting the session are
           necessary and not sufficient: credentials the session could read must
           be rotated, App Sessions must be read for entries nobody recognises,
           and until the MCP gap is closed an MCP credential may still be live.

           One further gap in the badge, disclosed here because this is where
           the contrapositive is asserted rather than only where it is
           implemented: after a browser or mobile re-consent on a CLI-exchanged
           client, an access token issued by the superseded grant stays valid
           until it expires (``DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES``) while the
           row already reads ``browser_consent``. For that window the badge
           understates what is live. Revoking the client, or the account token
           behind it, closes it on the next request.

        **What is NOT claimed, because the server does not enforce it:** that
        the exchange stays on one machine. A CLI account token is a bearer
        credential with no device binding — ``machine_name`` / ``machine_info``
        are self-reported at mint time — and this endpoint checks no IP, origin
        or device. A copied ``account.json`` works from anywhere. ``cinna login``
        exists precisely so the approving browser and the requesting machine can
        differ, so this is not a corner case. The single-machine story is the
        *intended* usage, never a control, and must not be written down as one.

        **Both outcomes are audited** — issued and refused — with the client id
        and source IP, never a token value. The audit row is written after the
        tokens are committed (matching every other account-CLI verb), so a
        database failure between the two would leave a live session with no
        ``ISSUED`` row; the user-facing control does not depend on that row,
        since the session is listed in App Sessions either way.

        **What must NOT be done to "narrow" the escalation:** mint a restricted
        token variant here. The desktop refreshes this pair through the same
        ``/desktop-auth/token`` endpoint as a browser-issued one, and a token the
        rotation logic has never seen is exactly the failure mode D12 names. The
        scope question is answered by audit, revocability and the cascade, not by
        a bespoke token shape.
        """
        from app.models.cli.account_desktop_token import AccountDesktopTokenResponse
        from app.services.desktop_auth.desktop_auth_service import DesktopAuthService

        # Per-account-token backstop. Every call with no ``client_id`` registers
        # a client row and a refresh token, so an unthrottled loop would both
        # amplify writes and bury the App Sessions list the security position
        # above depends on being readable. Mirrors the escape hatch's limiter.
        if (
            AccountCLIService._desktop_token_rate_limiter.check(
                str(cli_token.id), DESKTOP_TOKEN_EXCHANGE_LIMIT_PER_MIN
            )
            is not None
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many desktop-token exchanges. Try again shortly.",
            )

        try:
            tokens = DesktopAuthService.issue_tokens_for_cli_exchange(
                session=db,
                user_id=user.id,
                client_id_str=body.client_id,
                device_name=body.device_name,
                platform=body.platform,
                app_version=body.app_version,
                account_token_id=cli_token.id,
            )
        except HTTPException as e:
            if e.status_code != 403:
                # Only the authorization refusal is a DENIED event. Anything
                # else raised from the issuance path is a failure, not a
                # decision, and auditing it as one would file a misleading row
                # (``requested_client_id`` on an error that had nothing to do
                # with the client id). Let it propagate untouched.
                raise
            # Authorization failure (a client id that is revoked, unknown, or
            # another user's). Nothing has been written at this point — the
            # resolve step raises before any insert — so the audit write below
            # commits only itself.
            #
            # The caller gets one merged 403 so a client id cannot be probed,
            # but the audit log distinguishes the two: a desktop retrying with a
            # client the user disconnected is routine and would otherwise flood
            # the feed with high-severity rows that all say the same thing.
            classification = DesktopAuthService.classify_client_rejection(
                db, user.id, body.client_id
            )
            await SecurityEventService.create_event(
                session=db,
                user_id=user.id,
                data=SecurityEventCreate(
                    event_type=CLI_ACCOUNT_DESKTOP_TOKEN_DENIED,
                    severity="low" if classification == "revoked_own" else "high",
                    details={
                        "requested_client_id": body.client_id,
                        "reason": classification,
                        "detail": str(e.detail),
                        "cli_token_id": str(cli_token.id),
                        "ip": client_ip(request),
                    },
                ),
            )
            raise

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED,
                severity="high",
                details={
                    "client_id": tokens["client_id"],
                    "registered_client": body.client_id is None,
                    "device_name": body.device_name,
                    "platform": body.platform,
                    "app_version": body.app_version,
                    # Which CLI token bought the session — the link an
                    # investigator follows back to the account-token row, and
                    # from there to its machine label, which is self-reported
                    # and therefore a lead rather than an attribution (see the
                    # SECURITY POSITION above). The token VALUE is never
                    # recorded, here or anywhere.
                    "cli_token_id": str(cli_token.id),
                    "ip": client_ip(request),
                },
            ),
        )

        logger.info(
            "Issued desktop tokens for user %s via CLI account-token exchange "
            "(desktop client %s)",
            user.id,
            tokens["client_id"],
        )

        # ``email`` now rides the shared token payload (``_token_payload``), so it
        # is already in ``tokens`` — passing it again here would be a duplicate
        # keyword. Kept as one dict rather than re-adding the field so this path
        # and ``/desktop-auth/token`` cannot disagree about the account they name.
        return AccountDesktopTokenResponse(**tokens)
