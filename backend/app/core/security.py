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


def create_access_token(subject: str | Any, expires_delta: timedelta) -> str:
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode = {"exp": expire, "sub": str(subject)}
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# Cache for Google public keys (1 hour TTL)
_google_certs_cache: dict[str, Any] = {"certs": None, "expires_at": 0}


async def verify_google_token(token: str, client_id: str) -> dict[str, Any] | None:
    """Verify Google ID token and return claims if valid."""
    try:
        # Fetch Google's public keys (cached for 1 hour)
        now = datetime.now(timezone.utc).timestamp()
        if not _google_certs_cache["certs"] or now >= _google_certs_cache["expires_at"]:
            async with httpx.AsyncClient() as client:
                response = await client.get("https://www.googleapis.com/oauth2/v3/certs")
                _google_certs_cache["certs"] = response.json()
                _google_certs_cache["expires_at"] = now + 3600  # 1 hour

        # Decode and validate token
        jwt_instance = JsonWebToken(["RS256"])
        claims = jwt_instance.decode(
            token,
            _google_certs_cache["certs"],
            claims_options={
                "iss": {"values": ["https://accounts.google.com", "accounts.google.com"]},
                "aud": {"values": [client_id]},
            },
        )
        claims.validate()

        # Require verified email
        if not claims.get("email_verified", False):
            return None

        return dict(claims)
    except JoseError:
        return None
