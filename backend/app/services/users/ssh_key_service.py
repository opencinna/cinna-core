"""
SSH Key Service - Business logic for SSH key operations.
"""
import uuid
import hashlib
import logging
from datetime import datetime, UTC
from sqlmodel import Session, select
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

from app.models import UserSSHKey, SSHKeyGenerate, SSHKeyImport, SSHKeyUpdate
from app.core.security import encrypt_field, decrypt_field

logger = logging.getLogger(__name__)


class SSHKeyService:
    """
    Service for managing SSH keys.

    Responsibilities:
    - Generate RSA 4096-bit SSH key pairs
    - Import existing SSH keys
    - Encrypt/decrypt private keys and passphrases
    - Calculate SSH key fingerprints
    - Validate SSH key formats
    """

    @staticmethod
    def _calculate_fingerprint(public_key_str: str) -> str:
        """
        Calculate SHA256 fingerprint of SSH public key.

        Args:
            public_key_str: SSH public key in OpenSSH format

        Returns:
            Fingerprint string in format "SHA256:..."
        """
        try:
            # Parse the public key (format: "ssh-rsa AAAAB3... comment")
            parts = public_key_str.strip().split()
            if len(parts) < 2:
                raise ValueError("Invalid public key format")

            # Decode the base64 key data
            import base64
            key_data = base64.b64decode(parts[1])

            # Calculate SHA256 hash
            digest = hashlib.sha256(key_data).digest()

            # Encode as base64 (no padding)
            fingerprint = base64.b64encode(digest).decode().rstrip('=')

            return f"SHA256:{fingerprint}"
        except Exception as e:
            logger.error(f"Failed to calculate fingerprint: {e}")
            # Fallback: use hash of entire public key string
            digest = hashlib.sha256(public_key_str.encode()).hexdigest()
            return f"SHA256:{digest[:43]}"

    @staticmethod
    def _validate_ssh_key_format(public_key: str, private_key: str) -> bool:
        """
        Validate SSH key format.

        Args:
            public_key: SSH public key string
            private_key: SSH private key string

        Returns:
            True if valid, False otherwise
        """
        try:
            # Check public key format (should start with ssh-rsa, ssh-ed25519, etc.)
            public_key = public_key.strip()
            valid_prefixes = ['ssh-rsa', 'ssh-ed25519', 'ssh-dss', 'ecdsa-sha2-']
            if not any(public_key.startswith(prefix) for prefix in valid_prefixes):
                return False

            # Check private key format (should contain BEGIN markers)
            private_key = private_key.strip()
            if 'BEGIN' not in private_key or 'PRIVATE KEY' not in private_key:
                return False

            return True
        except Exception as e:
            logger.error(f"SSH key validation error: {e}")
            return False

    @staticmethod
    def generate_key_pair(
        session: Session,
        user_id: uuid.UUID,
        data: SSHKeyGenerate
    ) -> UserSSHKey:
        """
        Generate a new RSA 4096-bit SSH key pair.

        Args:
            session: Database session
            user_id: User ID
            data: Key generation request data

        Returns:
            Created SSH key record
        """
        # Generate RSA 4096-bit key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=4096,
            backend=default_backend()
        )

        # Serialize private key (PEM format, no passphrase)
        private_key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ).decode()

        # Get public key
        public_key_obj = private_key.public_key()

        # Serialize public key (OpenSSH format)
        public_key_openssh = public_key_obj.public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH
        ).decode()

        # Add comment to public key
        public_key_with_comment = f"{public_key_openssh} {data.name.replace(' ', '_')}"

        # Calculate fingerprint
        fingerprint = SSHKeyService._calculate_fingerprint(public_key_with_comment)

        # Encrypt private key
        encrypted_private_key = encrypt_field(private_key_pem)

        # Check for duplicate fingerprint
        existing = session.exec(
            select(UserSSHKey).where(
                UserSSHKey.user_id == user_id,
                UserSSHKey.fingerprint == fingerprint
            )
        ).first()

        if existing:
            raise ValueError("SSH key with this fingerprint already exists")

        # Create SSH key record
        ssh_key = UserSSHKey(
            user_id=user_id,
            name=data.name,
            public_key=public_key_with_comment,
            private_key_encrypted=encrypted_private_key,
            passphrase_encrypted=None,  # No passphrase for generated keys
            fingerprint=fingerprint,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC)
        )

        session.add(ssh_key)
        session.commit()
        session.refresh(ssh_key)

        logger.info(f"Generated SSH key {ssh_key.id} for user {user_id}")
        return ssh_key

    @staticmethod
    def import_key(
        session: Session,
        user_id: uuid.UUID,
        data: SSHKeyImport
    ) -> UserSSHKey:
        """
        Import an existing SSH key.

        Args:
            session: Database session
            user_id: User ID
            data: Key import request data

        Returns:
            Created SSH key record

        Raises:
            ValueError: If key format is invalid or duplicate exists
        """
        # Validate key format
        if not SSHKeyService._validate_ssh_key_format(data.public_key, data.private_key):
            raise ValueError("Invalid SSH key format")

        # Calculate fingerprint
        fingerprint = SSHKeyService._calculate_fingerprint(data.public_key)

        # Check for duplicate fingerprint
        existing = session.exec(
            select(UserSSHKey).where(
                UserSSHKey.user_id == user_id,
                UserSSHKey.fingerprint == fingerprint
            )
        ).first()

        if existing:
            raise ValueError("SSH key with this fingerprint already exists")

        # Encrypt private key and passphrase
        encrypted_private_key = encrypt_field(data.private_key)
        encrypted_passphrase = encrypt_field(data.passphrase) if data.passphrase else None

        # Create SSH key record
        ssh_key = UserSSHKey(
            user_id=user_id,
            name=data.name,
            public_key=data.public_key.strip(),
            private_key_encrypted=encrypted_private_key,
            passphrase_encrypted=encrypted_passphrase,
            fingerprint=fingerprint,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC)
        )

        session.add(ssh_key)
        session.commit()
        session.refresh(ssh_key)

        logger.info(f"Imported SSH key {ssh_key.id} for user {user_id}")
        return ssh_key

    @staticmethod
    def get_user_keys(
        session: Session,
        user_id: uuid.UUID
    ) -> list[UserSSHKey]:
        """
        Get all SSH keys for a user.

        Args:
            session: Database session
            user_id: User ID

        Returns:
            List of SSH keys
        """
        keys = session.exec(
            select(UserSSHKey)
            .where(UserSSHKey.user_id == user_id)
            .order_by(UserSSHKey.created_at.desc())
        ).all()

        return list(keys)

    @staticmethod
    def get_key_by_id(
        session: Session,
        key_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> UserSSHKey | None:
        """
        Get SSH key by ID (with ownership check).

        Args:
            session: Database session
            key_id: SSH key ID
            user_id: User ID (for ownership verification)

        Returns:
            SSH key if found and owned by user, None otherwise
        """
        key = session.exec(
            select(UserSSHKey).where(
                UserSSHKey.id == key_id,
                UserSSHKey.user_id == user_id
            )
        ).first()

        return key

    @staticmethod
    def update_key(
        session: Session,
        key_id: uuid.UUID,
        user_id: uuid.UUID,
        data: SSHKeyUpdate
    ) -> UserSSHKey | None:
        """
        Update SSH key (only name can be updated).

        Args:
            session: Database session
            key_id: SSH key ID
            user_id: User ID (for ownership verification)
            data: Update data

        Returns:
            Updated SSH key if found and owned by user, None otherwise
        """
        key = SSHKeyService.get_key_by_id(session, key_id, user_id)
        if not key:
            return None

        # Update name if provided
        if data.name is not None:
            key.name = data.name
            key.updated_at = datetime.now(UTC)

        session.add(key)
        session.commit()
        session.refresh(key)

        logger.info(f"Updated SSH key {key_id} for user {user_id}")
        return key

    @staticmethod
    def delete_key(
        session: Session,
        key_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> bool:
        """
        Delete SSH key.

        Args:
            session: Database session
            key_id: SSH key ID
            user_id: User ID (for ownership verification)

        Returns:
            True if deleted, False if not found or not owned by user
        """
        key = SSHKeyService.get_key_by_id(session, key_id, user_id)
        if not key:
            return False

        session.delete(key)
        session.commit()

        logger.info(f"Deleted SSH key {key_id} for user {user_id}")
        return True

    @staticmethod
    def get_decrypted_private_key(
        session: Session,
        key_id: uuid.UUID,
        user_id: uuid.UUID
    ) -> tuple[str, str | None] | None:
        """
        Get decrypted private key and passphrase (for Git operations only).

        SECURITY: This method should only be called from Git operation functions.
        Never expose decrypted keys in API responses.

        Args:
            session: Database session
            key_id: SSH key ID
            user_id: User ID (for ownership verification)

        Returns:
            Tuple of (private_key, passphrase) if found, None otherwise
        """
        key = SSHKeyService.get_key_by_id(session, key_id, user_id)
        if not key:
            return None

        # Decrypt private key
        private_key = decrypt_field(key.private_key_encrypted)

        # Decrypt passphrase if present
        passphrase = None
        if key.passphrase_encrypted:
            passphrase = decrypt_field(key.passphrase_encrypted)

        return (private_key, passphrase)
