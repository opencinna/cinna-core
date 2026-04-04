"""
Credential Share Service - Business logic for credential sharing operations.
"""
import logging
from uuid import UUID

from sqlmodel import Session, select, and_

from app.models.credentials.credential import Credential
from app.models.credentials.credential_share import (
    CredentialShare,
    CredentialSharePublic,
    SharedCredentialPublic,
)
from app.models.users.user import User

logger = logging.getLogger(__name__)


class CredentialShareService:
    """
    Service for managing credential sharing between users.

    Responsibilities:
    - Share credentials with other users
    - Revoke credential shares
    - Query shares for credentials and users
    - Validate sharing permissions
    """

    @staticmethod
    def share_credential(
        session: Session,
        credential_id: UUID,
        owner_id: UUID,
        shared_with_email: str
    ) -> CredentialSharePublic:
        """
        Share a credential with another user.

        Validations:
        - Credential must exist and be owned by owner_id
        - Credential must have allow_sharing=true
        - Target user must exist (by email)
        - Cannot share with yourself
        - Cannot create duplicate share

        Returns:
            CredentialSharePublic with resolved user info

        Raises:
            ValueError for validation failures
        """
        # Get credential and verify ownership
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions to share this credential")

        # Check if sharing is allowed
        if not credential.allow_sharing:
            raise ValueError("This credential does not allow sharing. Enable sharing first.")

        # Find target user by email
        statement = select(User).where(User.email == shared_with_email)
        target_user = session.exec(statement).first()
        if not target_user:
            raise ValueError(f"User with email '{shared_with_email}' not found")

        # Cannot share with yourself
        if target_user.id == owner_id:
            raise ValueError("Cannot share a credential with yourself")

        # Check for duplicate share
        existing_share = session.exec(
            select(CredentialShare).where(
                and_(
                    CredentialShare.credential_id == credential_id,
                    CredentialShare.shared_with_user_id == target_user.id
                )
            )
        ).first()
        if existing_share:
            raise ValueError(f"Credential is already shared with {shared_with_email}")

        # Get owner info
        owner = session.get(User, owner_id)

        # Create the share
        share = CredentialShare(
            credential_id=credential_id,
            shared_with_user_id=target_user.id,
            shared_by_user_id=owner_id,
            access_level="read"
        )
        session.add(share)
        session.commit()
        session.refresh(share)

        logger.info(
            f"Credential {credential_id} shared with user {target_user.id} "
            f"(email: {shared_with_email}) by owner {owner_id}"
        )

        return CredentialSharePublic(
            id=share.id,
            credential_id=share.credential_id,
            credential_name=credential.name,
            credential_type=credential.type.value,
            shared_with_user_id=target_user.id,
            shared_with_email=target_user.email,
            shared_by_user_id=owner_id,
            shared_by_email=owner.email if owner else "",
            shared_at=share.shared_at,
            access_level=share.access_level
        )

    @staticmethod
    def revoke_credential_share(
        session: Session,
        share_id: UUID,
        owner_id: UUID
    ) -> None:
        """
        Revoke a credential share.

        Validations:
        - Share must exist
        - Credential must be owned by owner_id

        Deletes the CredentialShare record.
        """
        share = session.get(CredentialShare, share_id)
        if not share:
            raise ValueError("Share not found")

        # Verify credential ownership
        credential = session.get(Credential, share.credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions to revoke this share")

        session.delete(share)
        session.commit()

        logger.info(
            f"Credential share {share_id} revoked by owner {owner_id}"
        )

    @staticmethod
    def get_shares_by_credential(
        session: Session,
        credential_id: UUID,
        owner_id: UUID
    ) -> list[CredentialSharePublic]:
        """
        Get all shares for a credential (for credential owner).

        Validations:
        - Credential must be owned by owner_id

        Returns list of shares with resolved user emails.
        """
        # Verify credential ownership
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions to view shares for this credential")

        # Get owner info
        owner = session.get(User, owner_id)

        # Query all shares for this credential
        statement = select(CredentialShare).where(
            CredentialShare.credential_id == credential_id
        )
        shares = session.exec(statement).all()

        result = []
        for share in shares:
            # Get shared_with user info
            shared_with_user = session.get(User, share.shared_with_user_id)
            result.append(CredentialSharePublic(
                id=share.id,
                credential_id=share.credential_id,
                credential_name=credential.name,
                credential_type=credential.type.value,
                shared_with_user_id=share.shared_with_user_id,
                shared_with_email=shared_with_user.email if shared_with_user else "",
                shared_by_user_id=share.shared_by_user_id,
                shared_by_email=owner.email if owner else "",
                shared_at=share.shared_at,
                access_level=share.access_level
            ))

        return result

    @staticmethod
    def get_credentials_shared_with_me(
        session: Session,
        user_id: UUID
    ) -> list[SharedCredentialPublic]:
        """
        Get all credentials shared with a user.

        Returns list of SharedCredentialPublic objects where user has read access via CredentialShare.
        """
        # Query all shares where user is the recipient
        statement = select(CredentialShare).where(
            CredentialShare.shared_with_user_id == user_id
        )
        shares = session.exec(statement).all()

        result = []
        for share in shares:
            credential = session.get(Credential, share.credential_id)
            if not credential:
                continue  # Skip if credential was deleted

            owner = session.get(User, credential.owner_id)
            result.append(SharedCredentialPublic(
                id=credential.id,
                name=credential.name,
                type=credential.type.value,
                notes=credential.notes,
                owner_id=credential.owner_id,
                owner_email=owner.email if owner else "",
                shared_at=share.shared_at,
                access_level=share.access_level
            ))

        return result

    @staticmethod
    def get_share_count_for_credential(
        session: Session,
        credential_id: UUID
    ) -> int:
        """
        Get the count of shares for a credential.

        Returns the number of users this credential is shared with.
        """
        from sqlmodel import func

        statement = select(func.count()).select_from(CredentialShare).where(
            CredentialShare.credential_id == credential_id
        )
        count = session.exec(statement).one()
        return count

    @staticmethod
    def update_credential_sharing(
        session: Session,
        credential_id: UUID,
        owner_id: UUID,
        allow_sharing: bool
    ) -> Credential:
        """
        Enable or disable sharing for a credential.

        If disabling (allow_sharing=false):
        - All existing CredentialShare records are DELETED
        - Users who had access lose it immediately

        Returns updated Credential.
        """
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions to update this credential")

        # If disabling sharing, delete all existing shares
        if not allow_sharing and credential.allow_sharing:
            statement = select(CredentialShare).where(
                CredentialShare.credential_id == credential_id
            )
            shares = session.exec(statement).all()
            for share in shares:
                session.delete(share)
            logger.info(
                f"Disabled sharing for credential {credential_id}, "
                f"deleted {len(shares)} share(s)"
            )

        credential.allow_sharing = allow_sharing
        session.add(credential)
        session.commit()
        session.refresh(credential)

        return credential

    @staticmethod
    def can_user_access_credential(
        session: Session,
        credential_id: UUID,
        user_id: UUID
    ) -> bool:
        """
        Check if user can access a credential.

        Returns True if:
        - User owns the credential, OR
        - User has CredentialShare for the credential
        """
        credential = session.get(Credential, credential_id)
        if not credential:
            return False

        # Check if user is the owner
        if credential.owner_id == user_id:
            return True

        # Check if user has a share
        statement = select(CredentialShare).where(
            and_(
                CredentialShare.credential_id == credential_id,
                CredentialShare.shared_with_user_id == user_id
            )
        )
        share = session.exec(statement).first()
        return share is not None

    @staticmethod
    def delete_all_shares_for_credential(
        session: Session,
        credential_id: UUID
    ) -> int:
        """
        Delete all shares for a credential (called when credential is deleted).

        Returns the number of shares deleted.
        """
        statement = select(CredentialShare).where(
            CredentialShare.credential_id == credential_id
        )
        shares = session.exec(statement).all()
        count = len(shares)
        for share in shares:
            session.delete(share)
        session.commit()
        return count
