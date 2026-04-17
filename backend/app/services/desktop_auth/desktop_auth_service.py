"""Desktop App Authentication service.

Handles the server-side OAuth 2.0 with PKCE flow for Cinna Desktop clients:
  - Client registration and revocation
  - Authorization code issuance via consent flow (after browser redirect + SPA consent)
  - Token exchange (code → access + refresh token pair)
  - Refresh token rotation with replay detection
  - Expired record cleanup
"""
import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import create_access_token
from app.models.desktop_auth.desktop_auth_code import DesktopAuthCode
from app.models.desktop_auth.desktop_oauth_client import (
    DesktopOAuthClient,
    DesktopOAuthClientPublic,
)
from app.models.desktop_auth.desktop_refresh_token import DesktopRefreshToken
from app.services.desktop_auth.desktop_auth_crypto import (
    generate_auth_code,
    generate_client_id,
    generate_refresh_token,
    hash_token,
    verify_pkce,
)

logger = logging.getLogger(__name__)

# Only localhost (127.0.0.1 / localhost) with an explicit port is accepted.
# Per RFC 8252 §7.3 (native app OAuth BCP), loopback redirect URIs may use any
# path — the security boundary is the loopback host + per-port binding, not the
# path. Accept any path starting with "/".
_LOCALHOST_RE = re.compile(r"^http://(localhost|127\.0\.0\.1):(\d+)(/.*)?$")


def _ensure_utc(dt: datetime) -> datetime:
    """Return a timezone-aware datetime in UTC, handling both naive and aware inputs."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _validate_redirect_uri(redirect_uri: str) -> None:
    """Raise HTTP 400 if the redirect_uri is not a valid localhost callback."""
    m = _LOCALHOST_RE.match(redirect_uri)
    if not m:
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")
    port = int(m.group(2))
    if not (1024 <= port <= 65535):
        raise HTTPException(status_code=400, detail="invalid_redirect_uri")


class DesktopAuthService:
    """Service class for Desktop OAuth 2.0 with PKCE operations."""

    # ── Client management ─────────────────────────────────────────────────

    @staticmethod
    def register_client(
        session: Session,
        user_id: UUID,
        device_name: str,
        platform: str | None = None,
        app_version: str | None = None,
    ) -> DesktopOAuthClientPublic:
        """Register a new desktop client and return its public representation."""
        client = DesktopOAuthClient(
            client_id=generate_client_id(),
            user_id=user_id,
            device_name=device_name,
            platform=platform,
            app_version=app_version,
        )
        session.add(client)
        session.commit()
        session.refresh(client)
        return DesktopOAuthClientPublic(
            client_id=client.client_id,
            device_name=client.device_name,
            platform=client.platform,
            app_version=client.app_version,
            last_used_at=client.last_used_at,
            created_at=client.created_at,
            is_revoked=client.is_revoked,
        )

    @staticmethod
    def list_clients(
        session: Session,
        user_id: UUID,
    ) -> list[DesktopOAuthClientPublic]:
        """Return all active (non-revoked) desktop clients for this user."""
        stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.user_id == user_id,
            DesktopOAuthClient.is_revoked == False,  # noqa: E712
        )
        clients = session.exec(stmt).all()
        return [
            DesktopOAuthClientPublic(
                client_id=c.client_id,
                device_name=c.device_name,
                platform=c.platform,
                app_version=c.app_version,
                last_used_at=c.last_used_at,
                created_at=c.created_at,
                is_revoked=c.is_revoked,
            )
            for c in clients
        ]

    @staticmethod
    def revoke_client(
        session: Session,
        user_id: UUID,
        client_id_str: str,
    ) -> None:
        """Soft-revoke a client and cascade-revoke all its refresh tokens."""
        stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.client_id == client_id_str,
            DesktopOAuthClient.user_id == user_id,
        )
        client = session.exec(stmt).first()
        if not client:
            raise HTTPException(status_code=404, detail="Client not found")

        client.is_revoked = True

        # Revoke all refresh tokens for this client
        token_stmt = select(DesktopRefreshToken).where(
            DesktopRefreshToken.client_id == client.id,
            DesktopRefreshToken.is_revoked == False,  # noqa: E712
        )
        for token in session.exec(token_stmt).all():
            token.is_revoked = True

        session.commit()
        logger.info("Revoked desktop client %s and its tokens", client_id_str)

    # ── Consent flow ───────────────────────────────────────────────────────

    @staticmethod
    def create_auth_request(
        session: Session,
        device_name: str | None,
        platform: str | None,
        app_version: str | None,
        client_id: str | None,
        code_challenge: str,
        redirect_uri: str,
        state: str,
    ) -> str:
        """Store a pending consent request and return the raw nonce.

        The nonce is stored as a SHA-256 hash. The raw nonce is returned to the
        route so it can be embedded in the redirect URL to the consent page.
        """
        from app.models.desktop_auth.desktop_auth_request import DesktopAuthRequest

        nonce = generate_auth_code()  # 48-char URL-safe opaque token
        record = DesktopAuthRequest(
            nonce_hash=hash_token(nonce),
            device_name=device_name,
            platform=platform,
            app_version=app_version,
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            state=state,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(record)
        session.commit()
        return nonce

    @staticmethod
    def get_auth_request(session: Session, nonce: str) -> dict | None:
        """Return non-secret display metadata for a pending consent request.

        Returns None if the nonce is not found, already used, or expired.
        This is the public endpoint used by the frontend consent page.
        """
        from app.models.desktop_auth.desktop_auth_request import DesktopAuthRequest

        nonce_hash = hash_token(nonce)
        stmt = select(DesktopAuthRequest).where(
            DesktopAuthRequest.nonce_hash == nonce_hash
        )
        record = session.exec(stmt).first()
        if not record:
            return None
        if record.is_used or _ensure_utc(record.expires_at) <= datetime.now(UTC):
            return None
        return {
            "device_name": record.device_name,
            "platform": record.platform,
            "app_version": record.app_version,
            "client_id": record.client_id,
            "expires_at": record.expires_at.isoformat(),
        }

    @staticmethod
    def process_consent(
        session: Session,
        user_id: UUID,
        nonce: str,
        action: str,
    ) -> dict:
        """Process approve/deny for a pending consent request.

        Returns {"redirect_to": "<redirect_uri>?code=...&state=...&client_id=..."}
        on approve, or {"redirect_to": "<redirect_uri>?error=access_denied&state=..."}
        on deny. The approve callback includes ``client_id`` so desktop apps using
        lazy registration learn their server-assigned client_id before calling
        /token (which requires it).

        Raises HTTP 400 if the nonce is invalid, used, or expired.
        Raises HTTP 403 if the client_id in the request belongs to a different user.
        """
        from app.models.desktop_auth.desktop_auth_request import DesktopAuthRequest

        nonce_hash = hash_token(nonce)
        stmt = select(DesktopAuthRequest).where(
            DesktopAuthRequest.nonce_hash == nonce_hash
        )
        record = session.exec(stmt).first()

        now = datetime.now(UTC)
        if not record or record.is_used or _ensure_utc(record.expires_at) <= now:
            raise HTTPException(status_code=400, detail="invalid_or_expired_request")

        redirect_uri = record.redirect_uri
        state = record.state
        separator = "&" if "?" in redirect_uri else "?"

        if action == "deny":
            record.is_used = True
            session.commit()
            return {"redirect_to": f"{redirect_uri}{separator}error=access_denied&state={state}"}

        # action == "approve": resolve or lazily create client
        if record.client_id:
            # Existing client — must belong to the current user
            client_stmt = select(DesktopOAuthClient).where(
                DesktopOAuthClient.client_id == record.client_id,
                DesktopOAuthClient.user_id == user_id,
                DesktopOAuthClient.is_revoked == False,  # noqa: E712
            )
            client = session.exec(client_stmt).first()
            if not client:
                raise HTTPException(status_code=403, detail="client_not_found_or_forbidden")
            resolved_client_id = record.client_id
        else:
            # Lazy registration: create a new client for this user
            new_client = DesktopOAuthClient(
                client_id=generate_client_id(),
                user_id=user_id,
                device_name=record.device_name or "Unknown Device",
                platform=record.platform,
                app_version=record.app_version,
            )
            session.add(new_client)
            session.flush()  # assigns PK without committing
            resolved_client_id = new_client.client_id

        # Issue a single-use authorization code
        raw_code = generate_auth_code()
        auth_code = DesktopAuthCode(
            code_hash=hash_token(raw_code),
            user_id=user_id,
            client_id=resolved_client_id,
            code_challenge=record.code_challenge,
            redirect_uri=record.redirect_uri,
            is_used=False,
            expires_at=now + timedelta(minutes=5),
        )
        session.add(auth_code)
        record.is_used = True
        session.commit()

        return {
            "redirect_to": (
                f"{redirect_uri}{separator}code={raw_code}&state={state}"
                f"&client_id={resolved_client_id}"
            )
        }

    # ── Authorization code flow ────────────────────────────────────────────

    @staticmethod
    def create_authorization_code(
        session: Session,
        user_id: UUID,
        client_id_str: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str:
        """Issue an authorization code for the given client and user.

        Validates the redirect_uri (must be localhost) and that the client
        exists and is not revoked.  Returns the raw (unhashed) code value.
        """
        _validate_redirect_uri(redirect_uri)

        stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.client_id == client_id_str,
            DesktopOAuthClient.is_revoked == False,  # noqa: E712
        )
        client = session.exec(stmt).first()
        if not client:
            raise HTTPException(status_code=400, detail="invalid_client")

        raw_code = generate_auth_code()
        auth_code = DesktopAuthCode(
            code_hash=hash_token(raw_code),
            user_id=user_id,
            client_id=client_id_str,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            is_used=False,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(auth_code)
        session.commit()
        return raw_code

    @staticmethod
    def exchange_code(
        session: Session,
        code: str,
        client_id_str: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict:
        """Exchange an authorization code for an access + refresh token pair.

        Validates: code exists and is unused/unexpired, client_id matches,
        redirect_uri matches, and PKCE verifier matches the stored challenge.
        On success, marks the code as used and rotates refresh tokens.
        Returns a dict that includes client_id so lazy-registered clients
        learn their assigned client_id after the first exchange.
        """
        code_hash = hash_token(code)
        stmt = select(DesktopAuthCode).where(DesktopAuthCode.code_hash == code_hash)
        auth_code = session.exec(stmt).first()

        now = datetime.now(UTC)

        if (
            not auth_code
            or auth_code.is_used
            or _ensure_utc(auth_code.expires_at) <= now
        ):
            raise HTTPException(status_code=400, detail="invalid_grant")

        if auth_code.client_id != client_id_str:
            raise HTTPException(status_code=400, detail="invalid_grant")

        if auth_code.redirect_uri != redirect_uri:
            raise HTTPException(status_code=400, detail="invalid_grant")

        if not verify_pkce(code_verifier, auth_code.code_challenge):
            raise HTTPException(status_code=400, detail="invalid_grant")

        # Mark code as used (single-use enforcement)
        auth_code.is_used = True

        # Look up the client and update last_used_at
        client_stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.client_id == client_id_str,
            DesktopOAuthClient.is_revoked == False,  # noqa: E712
        )
        client = session.exec(client_stmt).first()
        if not client:
            raise HTTPException(status_code=400, detail="invalid_client")

        client.last_used_at = now

        access_token, refresh_token_raw = DesktopAuthService._create_token_pair(
            session, client, auth_code.user_id
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token_raw,
            "token_type": "bearer",
            "expires_in": settings.DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "client_id": client_id_str,
        }

    # ── Refresh token rotation ─────────────────────────────────────────────

    @staticmethod
    def refresh_tokens(
        session: Session,
        refresh_token_value: str,
        client_id_str: str,
    ) -> dict:
        """Rotate a refresh token and issue a new access + refresh token pair.

        Implements replay detection: if a revoked token in the same family is
        reused, the entire family is revoked (forcing re-authentication).
        Returns a dict that includes client_id for consistency with exchange_code.
        """
        token_hash = hash_token(refresh_token_value)
        stmt = select(DesktopRefreshToken).where(
            DesktopRefreshToken.token_hash == token_hash
        )
        token_record = session.exec(stmt).first()

        if not token_record:
            raise HTTPException(status_code=400, detail="invalid_grant")

        now = datetime.now(UTC)

        # Replay detection: if the token is already revoked, revoke the entire
        # family (another instance may have already used this token)
        if token_record.is_revoked:
            logger.warning(
                "Replay detected: revoked refresh token reused for family %s — revoking family",
                token_record.token_family,
            )
            DesktopAuthService.revoke_token_family(session, token_record.token_family)
            raise HTTPException(status_code=400, detail="invalid_grant")

        if _ensure_utc(token_record.expires_at) <= now:
            raise HTTPException(status_code=400, detail="invalid_grant")

        # Verify the token belongs to the claimed client
        client_stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.id == token_record.client_id,
            DesktopOAuthClient.client_id == client_id_str,
            DesktopOAuthClient.is_revoked == False,  # noqa: E712
        )
        client = session.exec(client_stmt).first()
        if not client:
            raise HTTPException(status_code=400, detail="invalid_grant")

        # Revoke the old token and issue a new pair — preserving the family
        # so the rotation chain stays linked (replay of any ancestor revokes
        # the current live token too).
        token_record.is_revoked = True
        client.last_used_at = now

        access_token, refresh_token_raw = DesktopAuthService._create_token_pair(
            session, client, token_record.user_id, token_record.token_family
        )

        return {
            "access_token": access_token,
            "refresh_token": refresh_token_raw,
            "token_type": "bearer",
            "expires_in": settings.DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "client_id": client.client_id,
        }

    @staticmethod
    def revoke_token_family(session: Session, family_id: UUID) -> None:
        """Revoke all refresh tokens in a rotation chain (replay protection)."""
        stmt = select(DesktopRefreshToken).where(
            DesktopRefreshToken.token_family == family_id,
            DesktopRefreshToken.is_revoked == False,  # noqa: E712
        )
        for token in session.exec(stmt).all():
            token.is_revoked = True
        session.commit()

    @staticmethod
    def revoke_by_refresh_token(
        session: Session,
        user_id: UUID,
        refresh_token_value: str,
    ) -> None:
        """Revoke a specific refresh token and its entire rotation family."""
        token_hash = hash_token(refresh_token_value)
        stmt = select(DesktopRefreshToken).where(
            DesktopRefreshToken.token_hash == token_hash
        )
        token_record = session.exec(stmt).first()

        if not token_record or token_record.user_id != user_id:
            raise HTTPException(status_code=404, detail="Token not found")

        DesktopAuthService.revoke_token_family(session, token_record.token_family)

    # ── Cleanup ────────────────────────────────────────────────────────────

    @staticmethod
    def cleanup_expired(session: Session) -> int:
        """Delete expired authorization codes, auth requests, and old revoked/expired refresh tokens.

        Returns the total number of records removed.
        """
        from app.models.desktop_auth.desktop_auth_request import DesktopAuthRequest

        now = datetime.now(UTC)
        cutoff = now - timedelta(days=7)
        count = 0

        # Remove expired auth codes
        code_stmt = select(DesktopAuthCode).where(
            DesktopAuthCode.expires_at <= now
        )
        for code in session.exec(code_stmt).all():
            session.delete(code)
            count += 1

        # Remove expired consent requests
        req_stmt = select(DesktopAuthRequest).where(
            DesktopAuthRequest.expires_at <= now
        )
        for req in session.exec(req_stmt).all():
            session.delete(req)
            count += 1

        # Remove revoked or expired refresh tokens older than 7 days
        token_stmt = select(DesktopRefreshToken).where(
            (DesktopRefreshToken.is_revoked == True)  # noqa: E712
            | (DesktopRefreshToken.expires_at <= now),
            DesktopRefreshToken.created_at <= cutoff,
        )
        for token in session.exec(token_stmt).all():
            session.delete(token)
            count += 1

        session.commit()
        return count

    # ── Private helpers ────────────────────────────────────────────────────

    @staticmethod
    def _create_token_pair(
        session: Session,
        client: DesktopOAuthClient,
        user_id: UUID,
        token_family: UUID | None = None,
    ) -> tuple[str, str]:
        """Create and persist a new (access_token, refresh_token) pair.

        The access token is a standard JWT (compatible with CurrentUser dep).
        The refresh token is stored as a SHA-256 hash; when rotating, pass the
        parent's `token_family` so the rotation chain is preserved and replay
        detection can revoke the entire chain (RFC 9700 §4.14.2).
        """
        access_token = create_access_token(
            subject=str(user_id),
            expires_delta=timedelta(minutes=settings.DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES),
        )

        refresh_token_raw = generate_refresh_token()
        refresh_record = DesktopRefreshToken(
            client_id=client.id,
            user_id=user_id,
            token_hash=hash_token(refresh_token_raw),
            token_family=token_family if token_family is not None else uuid4(),
            expires_at=datetime.now(UTC)
            + timedelta(days=settings.DESKTOP_REFRESH_TOKEN_EXPIRE_DAYS),
        )
        session.add(refresh_record)
        session.commit()

        return access_token, refresh_token_raw
