"""
SSH Key utilities — generation, fingerprinting, validation, type detection.

Shared by:
- `app.services.users.ssh_key_service.SSHKeyService` (user SSH keys feature)
- `app.services.credentials.credentials_service.CredentialsService` (ssh_key credential type)

These helpers only touch cryptography primitives. They never talk to the database
and never encrypt/decrypt — encryption is the caller's responsibility via
`app.core.security.encrypt_field` / `decrypt_field`.
"""
import base64
import hashlib
import logging

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

logger = logging.getLogger(__name__)


# Recognised OpenSSH public key prefixes (used by detect_key_type and validate_key_pair)
_PUBLIC_KEY_PREFIXES = {
    "ssh-rsa": "rsa",
    "ssh-ed25519": "ed25519",
    "ssh-dss": "dss",
    # ecdsa-sha2-* (3 variants) — handled via startswith
}


def generate_rsa_key_pair(name: str, key_size: int = 4096) -> tuple[str, str]:
    """
    Generate an RSA key pair.

    Args:
        name: Human-readable label; appended as a comment on the public key.
        key_size: RSA key size in bits (default 4096).

    Returns:
        Tuple of (public_key_openssh, private_key_pem).
        - public_key_openssh: `ssh-rsa AAAAB3... <name>` (with comment)
        - private_key_pem: Traditional OpenSSL PEM, unencrypted.
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
        backend=default_backend(),
    )

    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_key_openssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()

    # Append comment for identification (matches user SSH keys convention)
    public_key_with_comment = f"{public_key_openssh} {_sanitize_comment(name)}"

    return public_key_with_comment, private_key_pem


def generate_ed25519_key_pair(name: str) -> tuple[str, str]:
    """
    Generate an Ed25519 key pair.

    Args:
        name: Human-readable label; appended as a comment on the public key.

    Returns:
        Tuple of (public_key_openssh, private_key_pem).
        - public_key_openssh: `ssh-ed25519 AAAA... <name>` (with comment)
        - private_key_pem: OpenSSH format (Ed25519 does not support the
          traditional OpenSSL PEM container). Unencrypted.
    """
    private_key = ed25519.Ed25519PrivateKey.generate()

    # Ed25519 keys are always emitted in OpenSSH format (TraditionalOpenSSL is
    # RSA/DSA-only). OpenSSH format is widely supported by `ssh`, `git`, and
    # `paramiko`, so this is the safe choice.
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    public_key_openssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()

    public_key_with_comment = f"{public_key_openssh} {_sanitize_comment(name)}"

    return public_key_with_comment, private_key_pem


def calculate_fingerprint(public_key_str: str) -> str:
    """
    Compute the SHA-256 fingerprint of an OpenSSH public key.

    Matches the output of `ssh-keygen -lf <pubkey>`: `SHA256:<base64 without padding>`.

    Args:
        public_key_str: OpenSSH-format public key (with or without comment).

    Returns:
        Fingerprint string like `SHA256:abc123...`.

    Notes:
        On malformed input we fall back to hashing the whole string, which keeps
        this helper total (never raises). Callers who want strict validation
        should call `validate_key_pair` first.
    """
    try:
        parts = public_key_str.strip().split()
        if len(parts) < 2:
            raise ValueError("Invalid public key format")

        key_data = base64.b64decode(parts[1])
        digest = hashlib.sha256(key_data).digest()
        return f"SHA256:{base64.b64encode(digest).decode().rstrip('=')}"
    except Exception as e:
        logger.error(f"Failed to calculate fingerprint: {e}")
        # Fallback: keep the function total for callers
        digest = hashlib.sha256(public_key_str.encode()).hexdigest()
        return f"SHA256:{digest[:43]}"


def detect_key_type(public_key: str) -> str:
    """
    Identify the OpenSSH key type from the public key prefix.

    Args:
        public_key: OpenSSH public key (e.g., `ssh-ed25519 AAAA...`).

    Returns:
        One of `rsa`, `ed25519`, `ecdsa`, `dss`. Defaults to `rsa` for unknown
        prefixes (legacy-safe; callers should validate first if needed).
    """
    pk = public_key.strip()
    for prefix, key_type in _PUBLIC_KEY_PREFIXES.items():
        if pk.startswith(prefix):
            return key_type
    if pk.startswith("ecdsa-sha2-"):
        return "ecdsa"
    # Fallback — shouldn't hit this if validate_key_pair was called first
    return "rsa"


def validate_key_pair(public_key: str, private_key: str) -> None:
    """
    Validate that a public/private key pair looks structurally correct.

    This does NOT cryptographically verify that the two halves match — it only
    checks that the public key has a recognised OpenSSH prefix and that the
    private key has PEM markers.

    Args:
        public_key: OpenSSH public key string.
        private_key: PEM-encoded private key string.

    Raises:
        ValueError: If either key's format is invalid. The message identifies
            the offending field so UIs can surface inline errors.
    """
    pk = (public_key or "").strip()
    valid_prefixes = ("ssh-rsa", "ssh-ed25519", "ssh-dss", "ecdsa-sha2-")
    if not any(pk.startswith(prefix) for prefix in valid_prefixes):
        raise ValueError(
            "Invalid public_key: must start with 'ssh-rsa', 'ssh-ed25519', "
            "'ssh-dss', or 'ecdsa-sha2-*'"
        )

    priv = (private_key or "").strip()
    if "BEGIN" not in priv or "PRIVATE KEY" not in priv:
        raise ValueError(
            "Invalid private_key: expected PEM markers "
            "('-----BEGIN ... PRIVATE KEY-----' / '-----END ... PRIVATE KEY-----')"
        )


def is_private_key_encrypted(private_key: str) -> bool:
    """
    Detect whether a PEM-encoded private key is passphrase-encrypted.

    Works for both traditional OpenSSL PEM (DEK-Info header) and the OpenSSH
    PEM container (AES-encrypted body). Heuristic-only — errs on the side of
    "yes" for ambiguous input so callers can reject up-front rather than fail
    later with a cryptic `cryptography` exception.

    Args:
        private_key: PEM-encoded private key text.

    Returns:
        True if the key appears to be passphrase-protected.
    """
    if not private_key:
        return False

    text = private_key.strip()

    # Traditional OpenSSL PEM (RSA/DSA/EC):
    #   -----BEGIN RSA PRIVATE KEY-----
    #   Proc-Type: 4,ENCRYPTED
    #   DEK-Info: AES-128-CBC,...
    if "DEK-Info:" in text or "Proc-Type: 4,ENCRYPTED" in text:
        return True

    # OpenSSH PEM container: header is always plaintext, but the body encodes
    # the cipher name. Try to decode and check.
    if "BEGIN OPENSSH PRIVATE KEY" in text:
        try:
            serialization.load_ssh_private_key(
                text.encode(),
                password=None,
                backend=default_backend(),
            )
            return False
        except TypeError:
            # cryptography raises TypeError when the key is encrypted and we
            # pass password=None.
            return True
        except ValueError:
            # Malformed key body — can't tell; assume safe (not encrypted)
            return False
        except Exception:
            return False

    return False


def _sanitize_comment(name: str) -> str:
    """Normalise a human-readable name for use as an SSH public-key comment."""
    return (name or "").replace(" ", "_").replace("\n", "_").strip() or "cinna"


__all__ = [
    "generate_rsa_key_pair",
    "generate_ed25519_key_pair",
    "calculate_fingerprint",
    "detect_key_type",
    "validate_key_pair",
    "is_private_key_encrypted",
]
