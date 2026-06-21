"""
Device-Login Service — ``cinna login`` (RFC 8628 device-authorization grant).

Server-side state machine for refreshing an account CLI token through a browser
approval instead of pasting a setup token. Static methods, mirrors
``DesktopAuthService``.

State machine::

    pending ──approve──▶ approved ──first authorized poll──▶ consumed   (terminal)
       │  └─reject──▶ denied                                            (terminal)
       └─expires_at<now (lazy, on any read)──▶ expired                  (terminal)

Security highlights:
- ``device_code`` = ``secrets.token_urlsafe(32)`` (~256-bit); stored only as a
  SHA-256 hash; single-use (first ``authorized`` poll → ``consumed``).
- ``user_code`` = 8 chars from an unambiguous alphabet, normalized-uppercase
  storage, dashed display, generation-time collision retry.
- ``start`` / ``poll`` are unauthenticated → per-IP rate limiting + lazy expiry.
- Mint happens at APPROVAL (the only authenticated moment); the raw JWT is held
  transiently in ``account_token_jwt`` and nulled on the first ``authorized``
  poll.
"""
import logging
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlmodel import Session, select

from app.core.config import settings
from app.models import User
from app.models.cli.cli_device_login import (
    CLIDeviceLoginRequest,
    DeviceLoginPollResponse,
    DeviceLoginRequestPublic,
    DeviceLoginStartResponse,
)
from app.models.events.security_event import (
    CLI_DEVICE_LOGIN_APPROVED,
    CLI_DEVICE_LOGIN_REJECTED,
    SecurityEventCreate,
)
from app.services.cli.account_cli_service import AccountCLIService, _client_ip
from app.services.cli.cli_auth import CLIAuthService
from app.services.cli.cli_service import _ensure_utc, _get_platform_url
from app.services.cli.rate_limiter import RateLimiter
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

# RFC 8628 contract knobs.
DEVICE_LOGIN_EXPIRY_SECONDS = 900  # expires_in (15 minutes)
DEVICE_LOGIN_POLL_INTERVAL = 5  # interval

# Unambiguous alphabet — no 0/O/1/I/L. 32 symbols, 8 chars → 32**8 codespace.
USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
USER_CODE_LENGTH = 8
_USER_CODE_GEN_RETRIES = 5

# Per-IP throttles for the unauthenticated endpoints.
START_LIMIT_PER_MIN = 10
POLL_IP_LIMIT_PER_MIN = 60


class DeviceLoginError(Exception):
    """Raised when a device-login lookup/transition fails.

    Carries a ``reason`` enum the route maps to an HTTP status:
      - ``"not_found"``        → 404 (unknown / consumed — existence-leak-safe)
      - ``"expired"``          → 404
      - ``"already_resolved"`` → 409
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


def _normalize_user_code(user_code: str) -> str:
    """Normalize a user code for storage/lookup: uppercase, strip dashes/space."""
    return (user_code or "").upper().replace("-", "").replace(" ", "").strip()


def _dash_user_code(normalized: str) -> str:
    """Display form: ``WX7K9Q2P`` → ``WX7K-9Q2P`` (single dash at the midpoint)."""
    if len(normalized) == USER_CODE_LENGTH:
        mid = USER_CODE_LENGTH // 2
        return f"{normalized[:mid]}-{normalized[mid:]}"
    return normalized


def _frontend_base(request: Request) -> str:
    """Reachable browser base URL.

    Mirrors ``_get_platform_url``'s dev/prod logic: in production the public
    ``FRONTEND_HOST`` is correct; on a localhost dev box ``FRONTEND_HOST`` points
    at the Vite dev server, but the device page is served by the SPA there too,
    so we keep ``FRONTEND_HOST`` for the browser URL (the CLI opens it locally).
    """
    return settings.FRONTEND_HOST.rstrip("/")


class DeviceLoginService:
    """Account device-login operations. All methods static (mirrors CLIService)."""

    _rate_limiter = RateLimiter()

    @staticmethod
    def start(
        db: Session,
        machine_name: str,
        machine_info: str | None,
        request: Request,
    ) -> DeviceLoginStartResponse:
        """Begin a device-login request. Unauthenticated; per-IP rate limited."""
        from fastapi import HTTPException
        from fastapi import status as http_status

        ip = _client_ip(request) or "unknown"
        if DeviceLoginService._rate_limiter.check(ip, START_LIMIT_PER_MIN) is not None:
            raise HTTPException(
                status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many device-login requests. Try again shortly.",
            )

        device_code = secrets.token_urlsafe(32)
        device_code_hash = CLIAuthService.hash_token(device_code)
        user_code = DeviceLoginService._generate_unique_user_code(db)

        now = datetime.now(UTC)
        row = CLIDeviceLoginRequest(
            device_code_hash=device_code_hash,
            user_code=user_code,
            status="pending",
            machine_name=machine_name,
            machine_info=machine_info,
            client_ip=_client_ip(request),
            expires_at=now + timedelta(seconds=DEVICE_LOGIN_EXPIRY_SECONDS),
        )
        db.add(row)
        db.commit()

        dashed = _dash_user_code(user_code)
        frontend = _frontend_base(request)
        return DeviceLoginStartResponse(
            device_code=device_code,
            user_code=dashed,
            verification_uri=f"{frontend}/device",
            verification_uri_complete=f"{frontend}/device?code={dashed}",
            interval=DEVICE_LOGIN_POLL_INTERVAL,
            expires_in=DEVICE_LOGIN_EXPIRY_SECONDS,
        )

    @staticmethod
    def _generate_unique_user_code(db: Session) -> str:
        """Generate a normalized user code, retrying on collision with a live row.

        Collision is only meaningful against non-terminal (``pending``/
        ``approved``) requests — a terminal row with the same code can't be
        approved/polled into a wrong token.
        """
        for _ in range(_USER_CODE_GEN_RETRIES):
            candidate = "".join(
                secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH)
            )
            stmt = select(CLIDeviceLoginRequest).where(
                CLIDeviceLoginRequest.user_code == candidate,
                CLIDeviceLoginRequest.status.in_(("pending", "approved")),  # type: ignore[attr-defined]
            )
            if db.exec(stmt).first() is None:
                return candidate
        # 32**8 codespace makes repeated collisions astronomically unlikely; if it
        # somehow happens, surface it rather than risk reusing a live code.
        raise RuntimeError("Could not generate a unique device-login user code")

    @staticmethod
    async def poll(
        db: Session,
        device_code: str,
        request: Request,
    ) -> DeviceLoginPollResponse:
        """Poll for the device-login result. ALWAYS returns flow state in 200."""
        ip = _client_ip(request) or "unknown"
        if (
            DeviceLoginService._rate_limiter.check(ip, POLL_IP_LIMIT_PER_MIN)
            is not None
        ):
            # Flood control still returns 200 — the CLI honors slow_down.
            return DeviceLoginPollResponse(status="slow_down")

        device_code_hash = CLIAuthService.hash_token(device_code)
        row = db.exec(
            select(CLIDeviceLoginRequest).where(
                CLIDeviceLoginRequest.device_code_hash == device_code_hash
            )
        ).first()
        # Unknown == expired (anti-enumeration).
        if row is None:
            return DeviceLoginPollResponse(status="expired_token")

        # Lazy expiry.
        if DeviceLoginService._lazy_expire(db, row):
            return DeviceLoginPollResponse(status="expired_token")

        # Per-device_code slow_down: poll faster than the interval → slow_down,
        # without advancing the clock so the next on-time poll proceeds.
        now = datetime.now(UTC)
        if row.last_polled_at is not None:
            since = (now - _ensure_utc(row.last_polled_at)).total_seconds()
            if since < DEVICE_LOGIN_POLL_INTERVAL:
                return DeviceLoginPollResponse(status="slow_down")
        row.last_polled_at = now
        db.add(row)
        db.commit()

        if row.status == "pending":
            return DeviceLoginPollResponse(status="authorization_pending")
        if row.status == "denied":
            return DeviceLoginPollResponse(status="access_denied")
        if row.status in ("consumed", "expired"):
            return DeviceLoginPollResponse(status="expired_token")
        if row.status == "approved":
            # Single-use: hand back the transient JWT once, then consume + null it.
            account_token = row.account_token_jwt
            row.status = "consumed"
            row.account_token_jwt = None
            db.add(row)
            db.commit()
            return DeviceLoginPollResponse(
                status="authorized",
                account_token=account_token,
                platform_url=_get_platform_url(request),
                frontend_url=settings.FRONTEND_HOST.rstrip("/"),
                machine_name=row.machine_name,
            )
        # Defensive: an unknown status is treated as terminal.
        return DeviceLoginPollResponse(status="expired_token")

    @staticmethod
    def get_request_for_display(
        db: Session,
        user_code: str,
    ) -> DeviceLoginRequestPublic | None:
        """Browser display metadata for a user code, or ``None`` (route → 404)."""
        normalized = _normalize_user_code(user_code)
        if not normalized:
            return None
        row = DeviceLoginService._load_live_by_user_code(db, normalized)
        if row is None:
            return None
        DeviceLoginService._lazy_expire(db, row)
        return DeviceLoginRequestPublic(
            user_code=_dash_user_code(row.user_code),
            machine_name=row.machine_name,
            machine_info=row.machine_info,
            status=row.status,
        )

    @staticmethod
    async def approve(
        db: Session,
        user: User,
        user_code: str,
        request: Request,
    ) -> None:
        """Approve a pending request: mint the account token + flip to approved."""
        row = DeviceLoginService._load_for_resolution(db, user_code)

        jwt_value, cli_token = await AccountCLIService.mint_account_cli_token(
            db,
            owner_id=user.id,
            machine_name=row.machine_name,
            machine_info=row.machine_info,
            request=request,
        )

        row.status = "approved"
        row.approved_by_user_id = user.id
        row.minted_token_id = cli_token.id
        row.account_token_jwt = jwt_value
        db.add(row)
        db.commit()

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=None,
                event_type=CLI_DEVICE_LOGIN_APPROVED,
                severity="medium",
                details={
                    "user_code": row.user_code,
                    "machine_name": row.machine_name,
                    "machine_info": row.machine_info,
                    "minted_token_id": str(cli_token.id),
                    "ip": _client_ip(request),
                },
            ),
        )

    @staticmethod
    async def reject(
        db: Session,
        user: User,
        user_code: str,
        request: Request,
    ) -> None:
        """Reject a pending request → ``denied`` (poll then returns access_denied)."""
        row = DeviceLoginService._load_for_resolution(db, user_code)
        row.status = "denied"
        row.approved_by_user_id = user.id
        db.add(row)
        db.commit()

        await SecurityEventService.create_event(
            session=db,
            user_id=user.id,
            data=SecurityEventCreate(
                agent_id=None,
                event_type=CLI_DEVICE_LOGIN_REJECTED,
                severity="low",
                details={
                    "user_code": row.user_code,
                    "machine_name": row.machine_name,
                    "ip": _client_ip(request),
                },
            ),
        )

    @staticmethod
    def cleanup_expired(db: Session) -> int:
        """Hard-delete device-login rows that are past expiry.

        Lazy-on-read keeps the flow correct without this; this is pure
        housekeeping (mirrors ``CLIService.cleanup_expired_setup_tokens`` /
        ``DesktopAuthService.cleanup_expired``). A past-``expires_at`` row is safe
        to drop EXCEPT one still ``approved`` (and not yet polled): it holds a
        minted token that must still be handed back on the next poll (see
        ``_lazy_expire``). ``approved`` rows are removed only once consumed.
        Terminal rows hold no live secret (``account_token_jwt`` is nulled on the
        first authorized poll). Returns the count of rows removed.
        """
        now = datetime.now(UTC)
        stmt = select(CLIDeviceLoginRequest).where(
            CLIDeviceLoginRequest.expires_at <= now,
            CLIDeviceLoginRequest.status != "approved",
        )
        rows = list(db.exec(stmt).all())
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)

    # ── Internal helpers ─────────────────────────────────────────────────

    @staticmethod
    def _lazy_expire(db: Session, row: CLIDeviceLoginRequest) -> bool:
        """Flip a still-``pending`` row to ``expired`` when past ``expires_at``.

        Returns ``True`` iff the row is (now) expired.

        Only ``pending`` is subject to the expiry flip. An ``approved`` row has
        already minted its account token (committed, valid 7 days); expiring it
        here would orphan that live token and force a needless re-login if the
        first ``authorized`` poll lands just after ``expires_at``. So an approved
        request always yields its token on the next poll regardless of the
        15-minute boundary; ``denied`` / ``consumed`` / ``expired`` are terminal.
        """
        if row.status != "pending":
            return row.status == "expired"
        if _ensure_utc(row.expires_at) < datetime.now(UTC):
            row.status = "expired"
            db.add(row)
            db.commit()
            return True
        return False

    @staticmethod
    def _load_live_by_user_code(
        db: Session, normalized_user_code: str
    ) -> CLIDeviceLoginRequest | None:
        """Newest non-``consumed`` request with this normalized user code."""
        stmt = (
            select(CLIDeviceLoginRequest)
            .where(
                CLIDeviceLoginRequest.user_code == normalized_user_code,
                CLIDeviceLoginRequest.status != "consumed",
            )
            .order_by(CLIDeviceLoginRequest.created_at.desc())  # type: ignore[attr-defined]
        )
        return db.exec(stmt).first()

    @staticmethod
    def _load_for_resolution(
        db: Session, user_code: str
    ) -> CLIDeviceLoginRequest:
        """Load a row that must be ``pending`` to approve/reject; else raise.

        ``not_found`` (unknown/consumed) and ``expired`` → 404 (existence-leak
        safe); a non-pending live row → 409 ``already_resolved``.
        """
        normalized = _normalize_user_code(user_code)
        row = DeviceLoginService._load_live_by_user_code(db, normalized)
        if row is None:
            raise DeviceLoginError("not_found", "Login request not found")
        if DeviceLoginService._lazy_expire(db, row):
            raise DeviceLoginError("expired", "Login request has expired")
        if row.status != "pending":
            raise DeviceLoginError(
                "already_resolved", "This login request was already handled"
            )
        return row
