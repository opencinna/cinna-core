"""Desktop App Authentication service.

Handles the server-side OAuth 2.0 with PKCE flow for Cinna Desktop clients:
  - Client registration and revocation
  - Authorization code issuance via consent flow (after browser redirect + SPA consent)
  - Token exchange (code → access + refresh token pair)
  - Token issuance for a CLI account-token exchange (POST /cli/account/desktop-token)
  - Refresh token rotation with replay detection
  - Expired record cleanup
"""
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import create_access_token
from app.models.desktop_auth.desktop_auth_code import DesktopAuthCode
from app.models.desktop_auth.desktop_oauth_client import (
    CLIENT_ORIGIN_BROWSER_CONSENT,
    CLIENT_ORIGIN_CLI_EXCHANGE,
    DesktopOAuthClient,
    DesktopOAuthClientPublic,
    GrantProvenance,
)
from app.models.desktop_auth.desktop_refresh_token import DesktopRefreshToken
from app.models.users.user import User
from app.services.desktop_auth.desktop_auth_crypto import (
    generate_auth_code,
    generate_client_id,
    generate_refresh_token,
    hash_token,
    verify_pkce,
)

logger = logging.getLogger(__name__)

# Desktop loopback redirect (RFC 8252 §7.3, native app OAuth BCP): localhost /
# 127.0.0.1 with an explicit port. Loopback redirect URIs may use any path — the
# security boundary is the loopback host + per-port binding, not the path. Accept
# any path starting with "/".
_LOCALHOST_RE = re.compile(r"^http://(localhost|127\.0\.0\.1):(\d+)(/.*)?$")

# Mobile native redirect (RFC 8252 §7.1, private-use URI scheme): the Cinna
# Mobile app's own scheme. The production build uses `cinna-mobile`; dev/preview/
# staging builds get a hyphenated suffix (e.g. `cinna-mobile-dev`). Fixed + tied
# to the installed app, so safe everywhere.
_APP_SCHEME_RE = re.compile(r"^cinna-mobile(-[a-z0-9]+)*://[^\s]*$")

# iOS native redirect (RFC 8252 §7.1, private-use URI scheme): Apple's standard
# is to use the app's bundle identifier as the custom URL scheme. The production
# build is `io.opencinna.ios`; debug/dev/staging builds get a dotted suffix on
# the bundle id (e.g. `io.opencinna.ios.dev`) and therefore a matching scheme.
# Like `cinna-mobile://`, these are tied to the installed app, so safe everywhere.
_IOS_SCHEME_RE = re.compile(r"^io\.opencinna\.ios(\.[a-z0-9]+)*://[^\s]*$")

# Expo Go development redirect: exp://<dev-host>:<port>/--/oauth/callback. Host/
# port are the developer's Metro server and vary per machine — non-production only.
_EXPO_DEV_RE = re.compile(r"^exp://[^/\s]+(/.*)?$")


# How often `verify_active_or_raise` re-stamps `last_used_at` on the
# `DesktopOAuthClient` row.  Throttling avoids a DB write on every
# authenticated request while still keeping the Settings UI's "last
# active" timestamp accurate to the minute.
DESKTOP_LAST_USED_THROTTLE_SECONDS = 60


class DesktopAuthError(Exception):
    """Raised when desktop OAuth client validation fails.

    Carries a ``reason`` enum so callers can decide how to surface
    the failure (HTTP status, WS close code, log severity).  The
    ``message`` is the user-facing description.

    Reasons:
      - ``"client_missing"``  — JWT carries ``client_kind="desktop"`` but no
                                ``external_client_id`` claim
      - ``"client_invalid"``  — ``external_client_id`` is not a valid UUID
      - ``"revoked"``         — client row is missing or ``is_revoked=True``
    """

    def __init__(self, reason: str, message: str):
        self.reason = reason
        self.message = message
        super().__init__(message)


def _ensure_utc(dt: datetime) -> datetime:
    """Return a timezone-aware datetime in UTC, handling both naive and aware inputs."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _client_kind_for_redirect_uri(redirect_uri: str) -> str:
    """Classify a (already validated) redirect_uri as "mobile" or "desktop".

    Used purely for consent-screen display copy — the redirect_uri itself stays
    secret, only this derived label is exposed. Mobile native redirects use the
    app's private-use scheme (``cinna-mobile://``), the iOS bundle-id scheme
    (``io.opencinna.ios[.dev]://``), or Expo Go's ``exp://`` dev scheme;
    everything else (loopback HTTP) is the desktop app.
    """
    if (
        _APP_SCHEME_RE.match(redirect_uri)
        or _IOS_SCHEME_RE.match(redirect_uri)
        or _EXPO_DEV_RE.match(redirect_uri)
    ):
        return "mobile"
    return "desktop"


def _validate_redirect_uri(redirect_uri: str) -> None:
    """Raise HTTP 400 if the redirect_uri is not an accepted native-client callback.

    Accepts:
      - Desktop loopback:  http://127.0.0.1:<port>/...  /  http://localhost:<port>/...
      - Mobile app scheme: cinna-mobile://...           (all environments)
      - iOS bundle scheme: io.opencinna.ios[.dev]://...  (all environments)
      - Expo Go dev:       exp://...                    (non-production only)
    """
    m = _LOCALHOST_RE.match(redirect_uri)
    if m:
        port = int(m.group(2))
        if not (1024 <= port <= 65535):
            raise HTTPException(status_code=400, detail="invalid_redirect_uri")
        return

    if _APP_SCHEME_RE.match(redirect_uri) or _IOS_SCHEME_RE.match(redirect_uri):
        return

    if settings.ENVIRONMENT != "production" and _EXPO_DEV_RE.match(redirect_uri):
        return

    raise HTTPException(status_code=400, detail="invalid_redirect_uri")


class DesktopAuthService:
    """Service class for Desktop OAuth 2.0 with PKCE operations."""

    # ── Redirect URI validation ───────────────────────────────────────────

    @staticmethod
    def validate_redirect_uri(redirect_uri: str) -> None:
        """Raise HTTP 400 unless ``redirect_uri`` is an accepted native-client callback.

        Public entry point for the route layer (both the ``/desktop-auth`` and
        ``/app-auth`` surfaces). See the module-level ``_validate_redirect_uri``
        for the accepted forms.
        """
        _validate_redirect_uri(redirect_uri)

    # ── Client management ─────────────────────────────────────────────────

    @staticmethod
    def register_client(
        session: Session,
        user_id: UUID,
        device_name: str,
        platform: str | None = None,
        app_version: str | None = None,
    ) -> DesktopOAuthClientPublic:
        """Register a new desktop client and return its public representation.

        NOTE: currently has no caller — registration happens lazily through
        ``_resolve_or_register_client`` on the consent and CLI-exchange paths.
        If it is revived, it must set the grant columns (``origin`` and
        ``minted_by_account_token_id``, via ``_stamp_grant`` at the point tokens
        are actually issued); as written it takes the ``browser_consent``
        default, which would silently mislabel a client registered any other way.
        """
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
            origin=client.origin,
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
                origin=c.origin,
                last_used_at=c.last_used_at,
                created_at=c.created_at,
                is_revoked=c.is_revoked,
            )
            for c in clients
        ]

    @staticmethod
    def verify_active_or_raise(
        session: Session,
        external_client_id: str | None,
    ) -> DesktopOAuthClient:
        """Verify a desktop-issued JWT's ``external_client_id`` claim.

        Loads the matching ``DesktopOAuthClient`` row, rejects revoked /
        missing / malformed clients with ``DesktopAuthError``, and stamps
        ``last_used_at`` (throttled — see
        ``DESKTOP_LAST_USED_THROTTLE_SECONDS``) so the Settings UI's
        "last active" timestamp stays current without a write per request.

        Called by ``get_current_user`` whenever a JWT carries
        ``client_kind="desktop"``.  Returns the active client row so
        callers can stamp additional metadata if they need to.

        Raises:
            DesktopAuthError: on missing / invalid / revoked client
        """
        if not external_client_id:
            raise DesktopAuthError(
                "client_missing", "Desktop client identifier missing"
            )
        try:
            client_uuid = UUID(str(external_client_id))
        except (TypeError, ValueError):
            raise DesktopAuthError(
                "client_invalid", "Desktop client identifier invalid"
            )

        desktop_client = session.get(DesktopOAuthClient, client_uuid)
        if desktop_client is None or desktop_client.is_revoked:
            raise DesktopAuthError(
                "revoked", "Desktop session has been revoked"
            )

        now = datetime.now(UTC)
        last_used = desktop_client.last_used_at
        if (
            last_used is None
            or (now - _ensure_utc(last_used)).total_seconds()
            >= DESKTOP_LAST_USED_THROTTLE_SECONDS
        ):
            desktop_client.last_used_at = now
            session.commit()

        return desktop_client

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
            "client_kind": _client_kind_for_redirect_uri(record.redirect_uri),
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

        # ── Bind the request to the caller, before either action spends it ──
        # ``/authorize`` is public, so a pending request is stored with no
        # authenticated user on it. Its only tie to one is the ``client_id`` it
        # names, and that tie is checked here rather than inside a branch,
        # because BOTH actions consume the request: deny mints nothing, but it
        # still sets ``is_used``, so an unowned deny is a one-shot denial of
        # service on the owner's pending sign-in. The approve branch below used
        # to be the only guarded one, and only incidentally — it had to resolve
        # the client in order to mint a code, so the ownership check came along
        # for free. Nothing forced deny to resolve anything, so nothing did.
        #
        # Conditional on a named client for a reason, not for tidiness:
        # ``_resolve_or_register_client`` *registers* when handed no client id,
        # so calling it unconditionally would create a client row for a request
        # the user is about to deny. With an id it is a pure read that raises
        # 403, which is exactly the guard wanted on both paths. A request naming
        # no client has no owner to compare against — see
        # ``test_consent_lazy_registration_request_has_no_owner_to_bind_to``.
        named_client = (
            DesktopAuthService._resolve_or_register_client(
                session,
                user_id=user_id,
                client_id_str=record.client_id,
                device_name=record.device_name,
                platform=record.platform,
                app_version=record.app_version,
                created_origin=CLIENT_ORIGIN_BROWSER_CONSENT,
            )
            if record.client_id
            else None
        )

        if action == "deny":
            record.is_used = True
            session.commit()
            return {"redirect_to": f"{redirect_uri}{separator}error=access_denied&state={state}"}

        # action == "approve": resolve the named client or lazily register one.
        # Shared with the CLI token exchange (``issue_tokens_for_cli_exchange``)
        # so both surfaces apply identical ownership checks and identical
        # lazy-registration semantics.
        client = named_client or DesktopAuthService._resolve_or_register_client(
            session,
            user_id=user_id,
            client_id_str=None,
            device_name=record.device_name,
            platform=record.platform,
            app_version=record.app_version,
            created_origin=CLIENT_ORIGIN_BROWSER_CONSENT,
        )
        resolved_client_id = client.client_id

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
        # This grant came from a browser consent (an authorization code only
        # exists because a signed-in human approved one), and it supersedes any
        # earlier grant on this client — including a CLI-exchanged one.
        DesktopAuthService._stamp_grant(
            session,
            client,
            GrantProvenance(
                origin=CLIENT_ORIGIN_BROWSER_CONSENT,
                minted_by_account_token_id=None,
            ),
        )

        access_token, refresh_token_raw = DesktopAuthService._create_token_pair(
            session, client, auth_code.user_id
        )

        return DesktopAuthService._token_payload(
            session, client_id_str, auth_code.user_id, access_token, refresh_token_raw
        )

    # ── CLI account-token exchange ─────────────────────────────────────────

    @staticmethod
    def issue_tokens_for_cli_exchange(
        session: Session,
        user_id: UUID,
        client_id_str: str | None,
        device_name: str | None,
        platform: str | None,
        app_version: str | None,
        account_token_id: UUID,
    ) -> dict[str, Any]:
        """Issue a desktop token pair for a caller already authenticated elsewhere.

        The issuance half of ``POST /cli/account/desktop-token``: Cinna Desktop
        finds a CLI account token in the workshop tree and trades it for a
        desktop session instead of making the user re-authenticate in a browser.
        **The authorization decision is not made here** — the caller has already
        proved who it is, and ``AccountCLIService.exchange_for_desktop_token``
        holds the reasoning for why that proof is allowed to buy this token, plus
        the audit record. Read that before calling this from anywhere new.

        Deliberately *not* a parallel issuance path: it resolves-or-lazily-
        registers through the same helper the consent flow uses and mints
        through ``_create_token_pair``, the single primitive behind
        ``exchange_code`` and every rotation. So the pair it returns is
        indistinguishable to the refresh endpoint from a browser-issued one —
        rotation, replay detection, family revocation, the reuse-grace window and
        per-client revocation all apply to it unchanged, with no branch anywhere
        that has to remember this path exists.

        What it does *not* do is bypass the consent flow's checks silently: the
        resulting client is stamped ``origin="cli_exchange"`` so the session is
        distinguishable from a browser consent wherever clients are listed.

        Returns the same shape as ``exchange_code`` (minus nothing, plus
        nothing), so a caller can hand it to the desktop unchanged.

        Raises:
            HTTPException: 403 if ``client_id_str`` names a client that is
                revoked, missing, or another user's.
        """
        client = DesktopAuthService._resolve_or_register_client(
            session,
            user_id=user_id,
            client_id_str=client_id_str,
            device_name=device_name,
            platform=platform,
            app_version=app_version,
            created_origin=CLIENT_ORIGIN_CLI_EXCHANGE,
        )

        client.last_used_at = datetime.now(UTC)
        # Stamp the grant, not just the registration: an existing client id
        # (registered long ago in a browser) that is handed to this endpoint
        # must still surface as a CLI-exchanged session, or the origin field
        # would be trivially bypassable by reusing a known client id.
        DesktopAuthService._stamp_grant(
            session,
            client,
            GrantProvenance(
                origin=CLIENT_ORIGIN_CLI_EXCHANGE,
                minted_by_account_token_id=account_token_id,
            ),
        )

        access_token, refresh_token_raw = DesktopAuthService._create_token_pair(
            session, client, user_id
        )

        return DesktopAuthService._token_payload(
            session, client.client_id, user_id, access_token, refresh_token_raw
        )

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

        # Reuse of an already-revoked token. Two cases, distinguished by the
        # rotation reuse-grace window (OWASP / RFC 9700 §4.14.2):
        #   1. Within grace  — a benign lost-rotation-response retry. A native
        #      client refreshed, the server rotated and revoked this token, but
        #      the client never persisted the successor (suspended JS thread /
        #      timed-out response). Re-rotate from the same family instead of
        #      nuking it. The orphaned successor the client never saw is revoked
        #      so the family collapses back to a single live token.
        #   2. Outside grace / legacy row — a genuine replay (token theft).
        #      Revoke the entire family to force re-authentication.
        if token_record.is_revoked:
            within_grace = (
                token_record.revoked_at is not None
                and now - _ensure_utc(token_record.revoked_at)
                <= timedelta(seconds=settings.DESKTOP_REFRESH_TOKEN_REUSE_GRACE_SECONDS)
            )
            if within_grace:
                # Re-validate exactly as the normal path: token must not be
                # expired and the client must be valid before we issue.
                if _ensure_utc(token_record.expires_at) <= now:
                    raise HTTPException(status_code=400, detail="invalid_grant")
                client = DesktopAuthService._resolve_refresh_client(
                    session, token_record.client_id, client_id_str
                )
                logger.info(
                    "grace reuse: re-rotating within grace window for family %s",
                    token_record.token_family,
                )
                # Collapse the family to a single live token: revoke any other
                # still-live token (the successor the client never received)
                # before issuing the fresh pair.
                DesktopAuthService._revoke_live_family_tokens(
                    session, token_record.token_family, now
                )
                client.last_used_at = now
                return DesktopAuthService._issue_refresh_pair(
                    session, client, token_record.user_id, token_record.token_family
                )

            logger.warning(
                "Replay detected: revoked refresh token reused for family %s — revoking family",
                token_record.token_family,
            )
            DesktopAuthService.revoke_token_family(session, token_record.token_family)
            raise HTTPException(status_code=400, detail="invalid_grant")

        if _ensure_utc(token_record.expires_at) <= now:
            raise HTTPException(status_code=400, detail="invalid_grant")

        # Verify the token belongs to the claimed client
        client = DesktopAuthService._resolve_refresh_client(
            session, token_record.client_id, client_id_str
        )

        # Revoke the old token and issue a new pair — preserving the family
        # so the rotation chain stays linked (replay of any ancestor revokes
        # the current live token too).
        token_record.is_revoked = True
        token_record.revoked_at = now
        client.last_used_at = now

        return DesktopAuthService._issue_refresh_pair(
            session, client, token_record.user_id, token_record.token_family
        )

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
    def is_cli_exchanged_session(
        session: Session,
        external_client_id: str | None,
    ) -> bool:
        """True if this desktop session's current grant came from a CLI exchange.

        The predicate behind the credential-minting gate (see
        ``forbid_cli_exchanged_desktop_session`` in ``api/deps.py``). Reads the
        provenance ``_stamp_grant`` maintains, so a session the user has since
        re-authorized in a browser answers False — the gate follows the *current*
        grant, exactly like the badge and the cascade do.

        Returns False for anything that is not a live desktop session (a web JWT
        with no claims, a malformed id, a missing or revoked client). That is
        safe here rather than fail-open: a revoked client cannot authenticate at
        all — ``get_current_user`` rejects it before any route runs — so the only
        callers that reach a False are ones this gate was never meant to catch.
        """
        if not external_client_id:
            return False
        try:
            client_uuid = UUID(str(external_client_id))
        except (TypeError, ValueError):
            return False
        client = session.get(DesktopOAuthClient, client_uuid)
        if client is None or client.is_revoked:
            return False
        return client.origin == CLIENT_ORIGIN_CLI_EXCHANGE

    @staticmethod
    def classify_client_rejection(
        session: Session,
        user_id: UUID,
        client_id_str: str | None,
    ) -> str:
        """Label a rejected ``client_id`` **for audit purposes only**.

        ``_resolve_or_register_client`` answers the caller with one merged 403
        for missing / revoked / foreign, deliberately, so a client id cannot be
        probed for existence. That is right for the response and useless for the
        audit log, where the two cases could not be less alike:

        - ``"revoked_own"`` — the caller's own client, which they disconnected
          from Settings. A desktop that has not been told retries at every app
          start, so this fires repeatedly and benignly.
        - ``"unknown_or_foreign"`` — no such client, or someone else's. Rare,
          and the case worth looking at.

        Called only on the failure path, and only by the auditing caller. It
        never influences the response: the 403 is raised by the resolve path
        before this runs, so there is no way for this classification to widen
        what a caller can learn.
        """
        if not client_id_str:
            return "unknown_or_foreign"
        stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.client_id == client_id_str,
            DesktopOAuthClient.user_id == user_id,
        )
        client = session.exec(stmt).first()
        if client is not None and client.is_revoked:
            return "revoked_own"
        return "unknown_or_foreign"

    @staticmethod
    def _stamp_grant(
        session: Session,
        client: DesktopOAuthClient,
        provenance: GrantProvenance,
    ) -> None:
        """Record how this client's *current* grant was obtained.

        **The single writer of every *grant* of ``origin`` and
        ``minted_by_account_token_id`` — which is not the same as being the
        single writer of the columns, and the difference is worth stating so
        nobody audits for the wrong property.** ``origin`` is also written at
        row creation, by ``_resolve_or_register_client``, which mints no tokens
        and does not call this function; it seeds the column so it is never null
        before the first grant. What holds is that no *grant* stamps provenance
        anywhere else: the only callers are ``exchange_code`` and
        ``issue_tokens_for_cli_exchange``, and rotation carries the pair forward
        untouched.

        The two columns describe one grant between them, so they are never
        written apart — a row saying ``origin="browser_consent"`` while still
        pointing at the account token that bought it would make the App Sessions
        badge and the revocation cascade disagree about the same session.
        ``GrantProvenance`` exists so the pair is one value rather than two
        assignments.

        **When the provenance actually changes, every still-live refresh token
        on the client is revoked**, and that is what lets two per-client scalars
        describe a per-family fact. A client can otherwise hold concurrent live
        families — ``_create_token_pair`` mints a fresh family whenever it is
        called without a parent — while these columns describe only the newest.
        The consequence was not academic: a session obtained by exchanging a
        stolen CLI account token could be *unbadged* by a later ordinary browser
        consent on the same client id, which overwrote ``origin`` while leaving
        the CLI-minted family alive and refreshable. The badge is relied on in
        the contrapositive — no badge means no live CLI-minted session — so
        laundering it in that direction defeats the control, and the same stale
        link would drop the session out of the revoke cascade's filter.

        **How far that revoke reaches, since neither this function's name nor
        its callers show it: one client row, never the user.** The filter is
        ``DesktopRefreshToken.client_id == client.id``, and nothing widens it to
        ``user_id`` — so the user's other registered clients keep their sessions
        untouched: a second desktop, a phone on the ``/app-auth`` surface, and
        every ordinary web session, which does not live in this table at all.
        Linking a new machine does not sign the old ones out. Combined with the
        conditionality below, a re-grant that changes no provenance ends nothing
        whatsoever. This is written down because it is the first question anyone
        asks of a rule that revokes tokens while stamping a column, and the code
        answers it only for a reader who goes and reads the ``where`` clause.

        **Why it is conditional rather than unconditional**, since a security
        primitive is usually better off simple: an unconditional revoke also
        fires on same-provenance re-grants, which reaches two shipped surfaces
        this feature otherwise does not touch — the browser consent flow and the
        mobile ``/app-auth`` flow, both of which route through here. There it
        buys nothing: families that share a provenance share a badge and a
        cascade link, so leaving them live loses no property. And it costs
        something real on mobile, where an app suspended mid-refresh is the
        very scenario the reuse-grace window exists for, while a superseded
        token is deliberately ineligible for that window (below). Conditioning
        on the provenance changing confines the behaviour change to the mixed
        case, which is the only case that needs it.

        The comparison is the same value that gets written, so a provenance
        field added to ``GrantProvenance`` enters both automatically. What that
        does **not** cover — say it plainly rather than let a reader assume
        more — is someone adding a provenance column and writing it outside this
        function; that exposure is identical whether the revoke is conditional
        or not.

        Revoked **without** stamping ``revoked_at``: this is a hard revocation
        (a new grant supersedes the old), not a rotation, so the superseded
        tokens must never qualify for the reuse-grace re-rotation window — the
        same rule the disconnect and replay paths follow.

        The bounded gap: an access token already issued from the superseded
        grant keeps working until it expires (``DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES``),
        because access tokens are only checked against the *client's*
        ``is_revoked``, which a re-grant deliberately does not set. Killing that
        too would mean revoking the client the user is in the middle of signing
        in to.

        Does NOT commit — the caller's ``_create_token_pair`` does.
        """
        superseded = GrantProvenance.of(client) != provenance
        provenance.apply(client)

        if not superseded:
            return

        stmt = select(DesktopRefreshToken).where(
            DesktopRefreshToken.client_id == client.id,
            DesktopRefreshToken.is_revoked == False,  # noqa: E712
        )
        for token in session.exec(stmt).all():
            token.is_revoked = True

    @staticmethod
    def revoke_clients_for_account_token(
        session: Session,
        account_token_id: UUID,
    ) -> int:
        """Revoke every desktop client whose live grant came from this account token.

        The desktop half of ``AccountCLIService.revoke_account_token``'s cascade:
        revoking a CLI account token disconnects the desktop sessions it minted,
        the same way it disconnects the per-agent child tokens it minted.

        Only clients whose *current* grant is the CLI exchange are matched —
        ``_stamp_grant`` clears the link when a later browser consent supersedes
        it, so a session the user has since re-authorized in a browser survives,
        and there is no live CLI-granted family left behind when it does.

        Returns the number of clients revoked. Does NOT commit: the caller
        commits the whole cascade at once, so the CLI tokens and the desktop
        sessions are revoked together or not at all.
        """
        stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.minted_by_account_token_id == account_token_id,
            DesktopOAuthClient.is_revoked == False,  # noqa: E712
        )
        revoked = 0
        for client in session.exec(stmt).all():
            client.is_revoked = True
            token_stmt = select(DesktopRefreshToken).where(
                DesktopRefreshToken.client_id == client.id,
                DesktopRefreshToken.is_revoked == False,  # noqa: E712
            )
            for token in session.exec(token_stmt).all():
                token.is_revoked = True
            revoked += 1

        if revoked:
            logger.info(
                "Revoked %d desktop client(s) minted by account token %s",
                revoked,
                account_token_id,
            )
        return revoked

    @staticmethod
    def _resolve_or_register_client(
        session: Session,
        user_id: UUID,
        client_id_str: str | None,
        device_name: str | None,
        platform: str | None,
        app_version: str | None,
        created_origin: str,
    ) -> DesktopOAuthClient:
        """Resolve a named client for ``user_id``, or lazily register a new one.

        The lazy-registration path both native-client surfaces need: a client
        that has never talked to this instance has no ``client_id`` yet, so the
        first successful authorization creates its row from the display fields
        it supplied (``device_name`` / ``platform`` / ``app_version``).

        ``created_origin`` seeds ``origin`` on a newly created row only — an
        existing row keeps whatever its last grant stamped, and the *granting*
        caller re-stamps it (see ``origin`` on ``DesktopOAuthClient``).

        Does NOT commit: callers flush-and-commit alongside whatever else the
        grant writes.

        Raises:
            HTTPException: 403 if ``client_id_str`` names a client that does not
                exist, is revoked, or belongs to another user. The three are
                deliberately one response — a client id is guessable-ish and
                confirming existence would leak another user's registrations.
        """
        if client_id_str:
            client_stmt = select(DesktopOAuthClient).where(
                DesktopOAuthClient.client_id == client_id_str,
                DesktopOAuthClient.user_id == user_id,
                DesktopOAuthClient.is_revoked == False,  # noqa: E712
            )
            client = session.exec(client_stmt).first()
            if not client:
                raise HTTPException(
                    status_code=403, detail="client_not_found_or_forbidden"
                )
            return client

        client = DesktopOAuthClient(
            client_id=generate_client_id(),
            user_id=user_id,
            device_name=device_name or "Unknown Device",
            platform=platform,
            app_version=app_version,
            origin=created_origin,
        )
        session.add(client)
        session.flush()  # assigns PK without committing
        return client

    @staticmethod
    def _resolve_refresh_client(
        session: Session,
        token_client_pk: UUID,
        client_id_str: str,
    ) -> DesktopOAuthClient:
        """Load the active client for a refresh, or raise 400 invalid_grant.

        The token must belong to the claimed (non-revoked) client. Shared by
        the normal rotation path and the grace re-rotation path so both apply
        identical client validation.
        """
        client_stmt = select(DesktopOAuthClient).where(
            DesktopOAuthClient.id == token_client_pk,
            DesktopOAuthClient.client_id == client_id_str,
            DesktopOAuthClient.is_revoked == False,  # noqa: E712
        )
        client = session.exec(client_stmt).first()
        if not client:
            raise HTTPException(status_code=400, detail="invalid_grant")
        return client

    @staticmethod
    def _revoke_live_family_tokens(
        session: Session,
        family_id: UUID,
        now: datetime,
    ) -> None:
        """Revoke every still-live token in a family, stamping ``revoked_at``.

        Used by the grace re-rotation path to collapse the family back to a
        single live token: the orphaned successor the client never received is
        revoked so it can't linger. Does NOT commit — the caller commits when
        it persists the fresh pair.
        """
        stmt = select(DesktopRefreshToken).where(
            DesktopRefreshToken.token_family == family_id,
            DesktopRefreshToken.is_revoked == False,  # noqa: E712
        )
        for token in session.exec(stmt).all():
            token.is_revoked = True
            token.revoked_at = now

    @staticmethod
    def _token_payload(
        session: Session,
        client_id_str: str,
        user_id: UUID,
        access_token: str,
        refresh_token_raw: str,
    ) -> dict[str, Any]:
        """Build the token-endpoint response body. The single writer of its shape.

        Three paths issue desktop tokens — ``exchange_code``,
        ``issue_tokens_for_cli_exchange`` and ``_issue_refresh_pair`` — and each
        built this dict itself. A field added to one was silently absent from the
        others, which is the drift this consolidation removes: there is now one
        place to add a field and no way to add it to only some responses.

        ``email`` names the account the returned tokens actually belong to, and
        it is here for a security reason rather than for convenience. A consent
        request that names no client can be approved by any authenticated caller
        holding its nonce (see ``process_consent``); the resulting code is bound
        to *that* caller, while the app which redeems it is the one that started
        the flow and holds the PKCE verifier. So an app can be handed a working
        session for an account that is not the one its user meant to sign in to.
        Nothing in the rest of this response would reveal that.

        **This field makes that substitution visible; it does not prevent it.**
        Preventing it needs a check the client performs — comparing this address
        against the account the user expected — so a reader must not treat the
        field's presence as the fix. Removing it, or letting a new issuance path
        skip it, silently removes a client's only means of detecting the swap.
        """
        user = session.get(User, user_id)
        if user is None:
            # The row a live token points at cannot normally vanish mid-request.
            # Fail closed rather than return a payload with no account named:
            # a caller cannot compare an address that is not there, so a
            # silently-absent email would defeat the check this field exists for.
            raise HTTPException(status_code=400, detail="invalid_grant")

        return {
            "access_token": access_token,
            "refresh_token": refresh_token_raw,
            "token_type": "bearer",
            "expires_in": settings.DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "client_id": client_id_str,
            "email": user.email,
        }

    @staticmethod
    def _issue_refresh_pair(
        session: Session,
        client: DesktopOAuthClient,
        user_id: UUID,
        token_family: UUID,
    ) -> dict:
        """Issue a fresh token pair within ``token_family`` and build the response dict.

        Shared by the normal rotation path and the grace re-rotation path so
        both return the identical response shape.
        """
        access_token, refresh_token_raw = DesktopAuthService._create_token_pair(
            session, client, user_id, token_family
        )
        return DesktopAuthService._token_payload(
            session, client.client_id, user_id, access_token, refresh_token_raw
        )

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
            extra_claims={
                "client_kind": "desktop",
                "external_client_id": str(client.id),
            },
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
