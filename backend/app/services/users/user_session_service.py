"""User session version and "end every session" — the revocation seam.

Every non-desktop user JWT carries ``sv`` (``User.session_version``). Bumping
the version kills all of them at once; :meth:`UserSessionService.end_sessions`
does that together with revoking browser sessions, native clients and trusted
devices. Desktop/mobile JWTs (``client_kind == "desktop"``) carry no ``sv``:
their revocation is the per-request ``DesktopOAuthClient.is_revoked`` check.
"""
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, delete

from app.models import (
    AgentAccessToken,
    CLISetupToken,
    CLIToken,
    MCPToken,
    SecurityEvent,
    User,
    UserTrustedDevice,
)
from app.models.events import security_event as security_event_constants

logger = logging.getLogger(__name__)

# ``end_sessions`` reasons.
REASON_PASSWORD_CHANGED = "password_changed"
REASON_PASSWORD_SET = "password_set"
REASON_PASSWORD_RESET = "password_reset"
REASON_ADMIN_PASSWORD_SET = "admin_password_set"
REASON_GOOGLE_LINK_WIPE = "google_link_wipe"
REASON_SIGN_OUT_EVERYWHERE = "sign_out_everywhere"


class UserSessionService:
    @staticmethod
    def is_token_current(user: User, payload: dict[str, Any]) -> bool:
        """Whether a decoded user JWT still matches the user's session version.

        Desktop/mobile tokens are always current here (their revocation is the
        client row). A token without ``sv`` reads as 0, which no user has.
        """
        if payload.get("client_kind") == "desktop":
            return True
        try:
            token_sv = int(payload.get("sv", 0))
        except (TypeError, ValueError):
            return False
        return token_sv == user.session_version

    @staticmethod
    def kept_sessions_from_claims(
        claims: dict[str, Any],
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """``(keep_web_session_id, keep_desktop_client_pk)`` for "keep the
        caller's own session" from the caller's token claims.

        ``sid`` names the web session (cookie sessions only); a desktop token's
        ``external_client_id`` is the ``DesktopOAuthClient`` primary key.
        Malformed values are treated as absent (nothing kept).
        """
        keep_web: uuid.UUID | None = None
        keep_desktop: uuid.UUID | None = None
        try:
            if claims.get("sid"):
                keep_web = uuid.UUID(str(claims["sid"]))
            if claims.get("client_kind") == "desktop" and claims.get(
                "external_client_id"
            ):
                keep_desktop = uuid.UUID(str(claims["external_client_id"]))
        except ValueError:
            return None, None
        return keep_web, keep_desktop

    @staticmethod
    def end_sessions(
        session: Session,
        user: User,
        *,
        reason: str,
        keep_web_session_id: uuid.UUID | None = None,
        keep_desktop_client_pk: uuid.UUID | None = None,
        include_api_tokens: bool = False,
    ) -> None:
        """End every session of ``user`` except the kept ones.

        Bumps ``session_version`` (every non-desktop JWT, the kept session's
        included, goes stale; a kept web session recovers with one refresh),
        revokes web sessions and native clients, deletes all trusted devices
        and, with ``include_api_tokens``, soft-revokes CLI / CLI setup / A2A /
        MCP tokens. Writes ``SESSIONS_REVOKED``.

        Does NOT commit: the caller commits it in the same transaction as the
        write that caused it, and must mint any new token only after that.
        """
        from app.services.desktop_auth.desktop_auth_service import (
            DesktopAuthService,
        )
        from app.services.users.web_session_service import WebSessionService

        now = datetime.now(UTC)
        user.session_version = (user.session_version or 0) + 1
        session.add(user)

        WebSessionService.revoke_all_for_user(
            session, user.id, except_id=keep_web_session_id
        )

        DesktopAuthService.revoke_all_clients_for_user(
            session, user.id, except_client_pk=keep_desktop_client_pk
        )
        session.exec(
            delete(UserTrustedDevice).where(UserTrustedDevice.user_id == user.id)
        )
        if include_api_tokens:
            UserSessionService.revoke_api_tokens_for_user(session, user.id)

        session.add(
            SecurityEvent(
                user_id=user.id,
                event_type=security_event_constants.SESSIONS_REVOKED,
                severity="medium",
                details=json.dumps(
                    {"reason": reason, "api_tokens": include_api_tokens}
                ),
                created_at=now,
            )
        )
        logger.info(
            "Ended sessions for user %s (reason=%s, api_tokens=%s)",
            user.id,
            reason,
            include_api_tokens,
        )

    @staticmethod
    def revoke_api_tokens_for_user(session: Session, user_id: uuid.UUID) -> None:
        """Soft-revoke the user's CLI, CLI setup, A2A and MCP tokens.

        Sets the same flag each service sets on a user-initiated revoke, so the
        existing "revoked" UI states hold. Bulk statements; no commit.
        """
        session.execute(
            update(CLIToken)
            .where(CLIToken.owner_id == user_id, CLIToken.is_revoked == False)  # noqa: E712
            .values(is_revoked=True)
        )
        session.execute(
            update(CLISetupToken)
            .where(CLISetupToken.owner_id == user_id, CLISetupToken.is_used == False)  # noqa: E712
            .values(is_used=True)
        )
        session.execute(
            update(AgentAccessToken)
            .where(
                AgentAccessToken.owner_id == user_id,
                AgentAccessToken.is_revoked == False,  # noqa: E712
            )
            .values(is_revoked=True)
        )
        session.execute(
            update(MCPToken)
            .where(MCPToken.user_id == user_id, MCPToken.revoked == False)  # noqa: E712
            .values(revoked=True)
        )
