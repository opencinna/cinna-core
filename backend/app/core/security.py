from datetime import datetime, timedelta, timezone
from typing import Any
import base64

import httpx
import jwt
from authlib.jose import JsonWebToken
from authlib.jose.errors import JoseError
from passlib.context import CryptContext
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


ALGORITHM = "HS256"


# Initialize encryption cipher using the encryption key from settings
def _get_cipher() -> Fernet:
    """Get Fernet cipher instance using the configured encryption key."""
    # Convert the URL-safe base64 key to proper Fernet key format
    key_bytes = settings.ENCRYPTION_KEY.encode()
    # Use PBKDF2 to derive a proper 32-byte key
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"credentials_salt",  # Static salt for deterministic key derivation
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(key_bytes))
    return Fernet(key)


def encrypt_field(value: str) -> str:
    """Encrypt a sensitive field value."""
    if not value:
        return value
    cipher = _get_cipher()
    encrypted_bytes = cipher.encrypt(value.encode())
    return encrypted_bytes.decode()


def decrypt_field(encrypted_value: str) -> str:
    """Decrypt a sensitive field value."""
    if not encrypted_value:
        return encrypted_value
    cipher = _get_cipher()
    decrypted_bytes = cipher.decrypt(encrypted_value.encode())
    return decrypted_bytes.decode()


def create_access_token(
    subject: str | Any,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a signed JWT access token.

    Args:
        subject: The ``sub`` claim value (typically a user or token UUID string).
        expires_delta: Token lifetime.
        extra_claims: Optional additional claims merged into the payload before
            signing. Keys that conflict with ``sub`` or ``exp`` are silently
            ignored to prevent accidental claim shadowing.
    """
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode: dict[str, Any] = {}
    if extra_claims:
        # Merge extra claims first so that sub/exp cannot be shadowed.
        to_encode.update(extra_claims)
    to_encode["sub"] = str(subject)
    to_encode["exp"] = expire
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token_claims(token: str) -> dict[str, Any] | None:
    """Decode a JWT and return the full claim dict, or None on any failure.

    Used by the external A2A surface to extract ``client_kind`` and
    ``external_client_id`` claims from desktop-issued access tokens without
    raising an error for ordinary web-session JWTs that omit those claims.

    Returns:
        The full decoded claim dict, or ``None`` if the token cannot be
        decoded (expired, invalid signature, malformed, etc.).
    """
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# JWKS endpoints Google publishes. Each issuer family has its own key set, so
# the cache below is keyed by URL rather than being a single global slot.
GOOGLE_OAUTH_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_OAUTH_ISSUERS = ["https://accounts.google.com", "accounts.google.com"]

# Cache for Google public keys, keyed by certs URL (1 hour TTL per entry).
_google_certs_cache: dict[str, dict[str, Any]] = {}

_GOOGLE_CERTS_TTL_SECONDS = 3600


class GoogleCertsUnavailable(Exception):
    """Google's JWKS could not be fetched or was unusable.

    Explicitly NOT a verification failure: it means we could not check the
    signature, not that the signature was bad. Callers that must fail closed
    (the channel webhook) treat it as a denial but log it distinctly; callers
    whose contract is "return None for an unusable token" (the Google OAuth
    paths) catch it and return None, which is what they did before the JWKS
    fetch learned to raise.

    DO NOT re-parent this under ``ValueError``. That looks like tidying — it is
    a fail-open-shaped bug. ``verify_google_signed_jwt`` catches
    ``(JoseError, ValueError)`` to turn Authlib's bare ``ValueError`` (unknown
    ``kid``, oversized header) into "invalid token". If this class became a
    ``ValueError`` it would be swallowed by that same handler, and "we cannot
    verify right now" would start being reported as "this signature is
    invalid" — collapsing the exact distinction the channel webhook relies on
    to tell a Google outage apart from a forgery. Enforced by test.
    """


async def _get_google_certs(certs_url: str) -> Any:
    """Fetch (and cache for an hour) the JWKS document at ``certs_url``.

    Only a response that is both 2xx AND shaped like a JWKS is cached. Without
    those two guards a Google 5xx carrying a JSON error body would be cached
    for the full hour and would then reject every token verified against it —
    turning a transient upstream blip into an hour-long outage. A bad response
    raises instead, so the caller can distinguish "cannot verify right now"
    from "this signature is invalid".
    """
    now = datetime.now(timezone.utc).timestamp()
    entry = _google_certs_cache.get(certs_url)
    if entry is None or not entry["certs"] or now >= entry["expires_at"]:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(certs_url)
                response.raise_for_status()
                certs = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                # Includes an HTML error page from an intermediary, which
                # `.json()` raises ValueError on.
                raise GoogleCertsUnavailable(
                    f"Could not fetch JWKS from {certs_url}: {exc}"
                ) from exc
            if not isinstance(certs, dict) or not certs.get("keys"):
                raise GoogleCertsUnavailable(
                    f"JWKS endpoint {certs_url} returned no 'keys'"
                )
            entry = {"certs": certs, "expires_at": now + _GOOGLE_CERTS_TTL_SECONDS}
            _google_certs_cache[certs_url] = entry
    return entry["certs"]


async def verify_google_signed_jwt(
    token: str,
    *,
    audience: str,
    issuers: list[str],
    certs_url: str = GOOGLE_OAUTH_CERTS_URL,
) -> dict[str, Any] | None:
    """Verify an RS256 JWT signed by Google against a cached JWKS.

    Shared by every Google-signed inbound token: OAuth ID tokens (issuer
    ``accounts.google.com``) and Google Chat webhook bearer tokens (issuer
    ``chat@system.gserviceaccount.com``, a different JWKS URL). Issuer and
    audience are *required* arguments — a verifier that defaults them would
    accept tokens minted for a different relying party.

    Returns the validated claims, or ``None`` when the signature, issuer,
    audience, or expiry check fails. Network failures propagate (a JWKS
    outage is not a verification failure and must not read as one).

    ``certs_url`` must serve a **JWKS** document. Google publishes service
    account keys at two endpoints and only one of them qualifies — use
    ``.../service_accounts/v1/jwk/<account>`` (JWKS), never
    ``.../service_accounts/v1/metadata/x509/<account>`` (a ``{kid: PEM}`` map,
    which Authlib cannot decode). A successful 200 carrying the wrong shape is
    cached for the full hour and fails every verification until it expires.
    """
    try:
        certs = await _get_google_certs(certs_url)
        jwt_instance = JsonWebToken(["RS256"])
        claims = jwt_instance.decode(
            token,
            certs,
            claims_options={
                "iss": {"values": issuers},
                "aud": {"values": [audience]},
            },
        )
        claims.validate()
        return dict(claims)
    except (JoseError, ValueError):
        # Authlib does not raise JoseError for every malformed input: an
        # unknown `kid` (key rotation, or an attacker sending a made-up one)
        # surfaces as a bare ValueError from the key-set lookup, and an
        # oversized header does the same. Both are "this token is not valid",
        # so they belong here — without this the public channel webhook 500s
        # on the cheapest probe there is, and skips its verification audit.
        # `GoogleCertsUnavailable` is not a ValueError, so "cannot verify"
        # still propagates and stays distinguishable from "invalid".
        return None


async def verify_google_token(token: str, client_id: str) -> dict[str, Any] | None:
    """Verify a Google OAuth ID token and return claims if valid.

    Adds the OAuth-specific requirement on top of signature verification:
    the account's email must be Google-verified.

    Returns ``None`` — never raises — when Google's JWKS is unreachable or
    unusable. Both callers depend on that: the credential callback falls back
    to the userinfo endpoint and still completes the grant, and the Google
    login path reports an invalid token rather than a 500. Before the JWKS
    fetch learned to validate its response, a Google 5xx reached this function
    as a decode failure and produced exactly this ``None``.
    """
    try:
        claims = await verify_google_signed_jwt(
            token,
            audience=client_id,
            issuers=GOOGLE_OAUTH_ISSUERS,
            certs_url=GOOGLE_OAUTH_CERTS_URL,
        )
    except GoogleCertsUnavailable:
        return None
    if claims is None:
        return None

    if not claims.get("email_verified", False):
        return None

    return claims
