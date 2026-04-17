"""Cryptographic helpers for Desktop App Authentication."""
import base64
import hashlib
import secrets


def generate_client_id() -> str:
    """Generate a 32-char URL-safe random client identifier."""
    return secrets.token_urlsafe(24)[:32]


def generate_auth_code() -> str:
    """Generate a 48-char URL-safe random authorization code."""
    return secrets.token_urlsafe(36)[:48]


def generate_refresh_token() -> str:
    """Generate a 64-char URL-safe random refresh token."""
    return secrets.token_urlsafe(48)[:64]


def hash_token(value: str) -> str:
    """Return the SHA-256 hex digest of the given token value."""
    return hashlib.sha256(value.encode()).hexdigest()


def verify_pkce(verifier: str, challenge: str) -> bool:
    """Verify a PKCE S256 challenge.

    BASE64URL(SHA256(verifier)) must equal the stored challenge.
    Uses constant-time comparison to prevent timing attacks.
    """
    digest = hashlib.sha256(verifier.encode()).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)
