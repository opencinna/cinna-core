"""
AI Credentials Service

Handles CRUD operations for named AI credentials and syncing defaults to User profile.
"""
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.security import encrypt_field, decrypt_field
from app.utils import detect_anthropic_credential_type
from app.models.credentials.ai_credential import (
    AICredential,
    AICredentialCreate,
    AICredentialUpdate,
    AICredentialData,
    AICredentialType,
    AICredentialPublic,
    AffectedEnvironmentsPublic,
    AffectedEnvironmentPublic,
    SharedUserPublic,
)
from app.models.credentials.ai_credential_share import (
    AICredentialShare,
    AICredentialSharePublic,
    SharedAICredentialPublic,
)
from app.models.users.user import User, AIServiceCredentials, AIServiceCredentialsUpdate

logger = logging.getLogger(__name__)


class AICredentialsService:
    """Service for managing AI credentials"""

    def list_credentials(
        self, session: Session, user_id: uuid.UUID
    ) -> list[AICredentialPublic]:
        """List all AI credentials for a user"""
        statement = (
            select(AICredential)
            .where(AICredential.owner_id == user_id)
            .order_by(AICredential.created_at.desc())
        )
        credentials = session.exec(statement).all()
        return [self._to_public(cred, session) for cred in credentials]

    def get_credential(
        self, session: Session, credential_id: uuid.UUID, user_id: uuid.UUID
    ) -> AICredential:
        """Get a single credential, verifying ownership"""
        credential = session.get(AICredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="AI credential not found")
        if credential.owner_id != user_id:
            raise HTTPException(status_code=403, detail="Not authorized to access this credential")
        return credential

    def create_credential(
        self, session: Session, user_id: uuid.UUID, data: AICredentialCreate
    ) -> AICredentialPublic:
        """Create a new AI credential"""
        # Validate type-specific fields
        self._validate_credential_data(
            data.type, data.api_key, data.base_url, data.model,
        )

        # Prepare data for encryption
        credential_data = AICredentialData(
            api_key=data.api_key,
            base_url=data.base_url,
            model=data.model,
        )
        encrypted_data = encrypt_field(json.dumps(credential_data.model_dump()))

        # Auto-set expiry notification date for OAuth tokens (11 months from now)
        expiry_date = data.expiry_notification_date
        if data.type == AICredentialType.ANTHROPIC and not expiry_date:
            env_var_name, key_type = detect_anthropic_credential_type(data.api_key)
            if env_var_name == "CLAUDE_CODE_OAUTH_TOKEN":
                # OAuth token - set expiry to 11 months from now
                expiry_date = datetime.now(timezone.utc) + timedelta(days=335)  # ~11 months
                logger.info(f"Auto-set OAuth token expiry notification to {expiry_date.date()}")

        # Create credential
        now = datetime.now(timezone.utc)
        credential = AICredential(
            owner_id=user_id,
            name=data.name,
            type=data.type,
            encrypted_data=encrypted_data,
            is_default=False,
            expiry_notification_date=expiry_date,
            created_at=now,
            updated_at=now,
        )
        session.add(credential)
        session.commit()
        session.refresh(credential)

        logger.info(f"Created AI credential '{data.name}' for user {user_id}")
        return self._to_public(credential, session)

    def update_credential(
        self,
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
        data: AICredentialUpdate,
    ) -> AICredentialPublic:
        """Update an existing AI credential"""
        credential = self.get_credential(session, credential_id, user_id)

        # Get existing decrypted data
        existing_data = self.decrypt_credential(credential)

        # Apply updates
        if data.name is not None:
            credential.name = data.name

        # Update expiry notification date if provided
        if data.expiry_notification_date is not None:
            credential.expiry_notification_date = data.expiry_notification_date

        # Update credential data fields
        new_api_key = data.api_key if data.api_key is not None else existing_data.api_key
        new_base_url = data.base_url if data.base_url is not None else existing_data.base_url
        new_model = data.model if data.model is not None else existing_data.model

        # Auto-set expiry notification date when API key is updated to an OAuth token
        if data.api_key is not None and credential.type == AICredentialType.ANTHROPIC and data.expiry_notification_date is None:
            env_var_name, key_type = detect_anthropic_credential_type(new_api_key)
            if env_var_name == "CLAUDE_CODE_OAUTH_TOKEN":
                # OAuth token - set expiry to 11 months from now
                credential.expiry_notification_date = datetime.now(timezone.utc) + timedelta(days=335)
                logger.info(f"Auto-set OAuth token expiry notification to {credential.expiry_notification_date.date()} (key updated)")

        # Validate updated data
        self._validate_credential_data(
            credential.type, new_api_key, new_base_url, new_model,
        )

        # Encrypt updated data
        updated_data = AICredentialData(
            api_key=new_api_key,
            base_url=new_base_url,
            model=new_model,
        )
        credential.encrypted_data = encrypt_field(json.dumps(updated_data.model_dump()))
        credential.updated_at = datetime.now(timezone.utc)

        session.add(credential)
        session.commit()
        session.refresh(credential)

        # If this is a default credential, sync to user profile
        if credential.is_default:
            user = session.get(User, user_id)
            if user:
                self._sync_default_to_user_profile(session, user, credential)

        logger.info(f"Updated AI credential {credential_id}")
        return self._to_public(credential, session)

    def delete_credential(
        self, session: Session, credential_id: uuid.UUID, user_id: uuid.UUID
    ) -> None:
        """Delete an AI credential"""
        credential = self.get_credential(session, credential_id, user_id)

        was_default = credential.is_default
        cred_type = credential.type

        session.delete(credential)
        session.commit()

        # If it was default, clear from user profile
        if was_default:
            user = session.get(User, user_id)
            if user:
                self._clear_user_profile_for_type(session, user, cred_type)

        logger.info(f"Deleted AI credential {credential_id}")

    def set_default(
        self, session: Session, credential_id: uuid.UUID, user_id: uuid.UUID
    ) -> AICredentialPublic:
        """Set a credential as the default for its type"""
        credential = self.get_credential(session, credential_id, user_id)

        # Unset previous default for this type
        cred_type_value = credential.type.value if isinstance(credential.type, AICredentialType) else credential.type
        statement = select(AICredential).where(
            AICredential.owner_id == user_id,
            AICredential.type == cred_type_value,
            AICredential.is_default == True,
        )
        previous_defaults = session.exec(statement).all()
        for prev in previous_defaults:
            prev.is_default = False
            session.add(prev)

        # Set this one as default
        credential.is_default = True
        credential.updated_at = datetime.now(timezone.utc)
        session.add(credential)
        session.commit()
        session.refresh(credential)

        # Sync to user profile
        user = session.get(User, user_id)
        if user:
            self._sync_default_to_user_profile(session, user, credential)

        logger.info(f"Set AI credential {credential_id} as default for type {credential.type}")
        return self._to_public(credential, session)

    def get_default_for_type(
        self, session: Session, user_id: uuid.UUID, cred_type: AICredentialType
    ) -> AICredential | None:
        """Get the default credential for a specific type"""
        statement = select(AICredential).where(
            AICredential.owner_id == user_id,
            AICredential.type == cred_type.value,
            AICredential.is_default == True,
        )
        return session.exec(statement).first()

    def resolve_default_credential_for_sdk(
        self, session: Session, user_id: uuid.UUID, sdk_engine: str
    ) -> AICredential | None:
        """
        Resolve the best default credential for a given SDK engine.

        Priority order:
        1. Anthropic credentials marked as default (highest priority)
        2. Google credentials marked as default
        3. OpenAI credentials marked as default
        4. Any other compatible type marked as default, ordered by created_at ASC

        Only credentials with is_default=True AND compatible with the SDK engine
        are considered.
        """
        from app.services.environments.environment_service import SDK_CREDENTIAL_COMPATIBILITY

        compatible_types = SDK_CREDENTIAL_COMPATIBILITY.get(sdk_engine, [])
        if not compatible_types:
            return None

        # Try priority types in order
        priority_types = ["anthropic", "google", "openai"]
        for cred_type in priority_types:
            if cred_type not in compatible_types:
                continue
            statement = select(AICredential).where(
                AICredential.owner_id == user_id,
                AICredential.type == cred_type,
                AICredential.is_default == True,
            )
            result = session.exec(statement).first()
            if result:
                return result

        # Fall back to any other compatible default, oldest first
        other_types = [t for t in compatible_types if t not in priority_types]
        if not other_types:
            return None

        statement = (
            select(AICredential)
            .where(
                AICredential.owner_id == user_id,
                AICredential.is_default == True,
                AICredential.type.in_(other_types),
            )
            .order_by(AICredential.created_at.asc())
        )
        return session.exec(statement).first()

    def decrypt_credential(self, credential: AICredential) -> AICredentialData:
        """Decrypt credential data"""
        decrypted_json = decrypt_field(credential.encrypted_data)
        data_dict = json.loads(decrypted_json)
        return AICredentialData(**data_dict)

    def _to_public(self, credential: AICredential, session: Session) -> AICredentialPublic:
        """Convert credential to public representation"""
        # Decrypt to get non-sensitive fields
        data = self.decrypt_credential(credential)

        # Detect OAuth token for Anthropic credentials
        is_oauth = False
        if credential.type == AICredentialType.ANTHROPIC and data.api_key:
            is_oauth = data.api_key.startswith("sk-ant-oat")

        return AICredentialPublic(
            id=credential.id,
            name=credential.name,
            type=credential.type,
            is_default=credential.is_default,
            has_api_key=bool(data.api_key),
            is_oauth_token=is_oauth,
            base_url=data.base_url,
            model=data.model,
            expiry_notification_date=credential.expiry_notification_date,
            created_at=credential.created_at,
            updated_at=credential.updated_at,
        )

    def _validate_credential_data(
        self,
        cred_type: AICredentialType,
        api_key: str | None,
        base_url: str | None,
        model: str | None,
    ) -> None:
        """Validate credential data based on type"""
        if not api_key:
            raise HTTPException(status_code=400, detail="API key is required")

        if cred_type == AICredentialType.OPENAI_COMPATIBLE:
            if not base_url:
                raise HTTPException(
                    status_code=400,
                    detail="Base URL is required for OpenAI Compatible credentials",
                )
            if not model:
                raise HTTPException(
                    status_code=400,
                    detail="Model is required for OpenAI Compatible credentials",
                )

    # ============= User Profile AI Credentials (legacy encrypted field) =============

    @staticmethod
    def get_user_ai_credentials(*, user: User) -> AIServiceCredentials | None:
        """Get decrypted AI service credentials for user."""
        if not user.ai_credentials_encrypted:
            return None

        try:
            decrypted_json = decrypt_field(user.ai_credentials_encrypted)
            credential_data = json.loads(decrypted_json)
            return AIServiceCredentials(**credential_data)
        except Exception:
            return None

    @staticmethod
    def update_user_ai_credentials(
        *, session: Session, user: User, credentials_update: AIServiceCredentialsUpdate
    ) -> User:
        """Update AI service credentials (partial update)."""
        existing = AICredentialsService.get_user_ai_credentials(user=user)
        if existing:
            current_data = existing.model_dump()
        else:
            current_data = {}

        update_data = credentials_update.model_dump(exclude_unset=True)
        current_data.update(update_data)

        credential_data_json = json.dumps(current_data)
        user.ai_credentials_encrypted = encrypt_field(credential_data_json)
        session.add(user)
        session.commit()
        session.refresh(user)

        return user

    @staticmethod
    def delete_user_ai_credentials(*, session: Session, user: User) -> User:
        """Delete all AI service credentials."""
        user.ai_credentials_encrypted = None
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    # ============= Profile Sync Helpers =============

    def _sync_default_to_user_profile(
        self, session: Session, user: User, credential: AICredential
    ) -> None:
        """Sync default credential to user's AI credentials profile for backward compatibility"""
        data = self.decrypt_credential(credential)

        # Map credential type to user profile fields
        update_data: dict = {}

        if credential.type == AICredentialType.ANTHROPIC:
            update_data["anthropic_api_key"] = data.api_key
        elif credential.type == AICredentialType.MINIMAX:
            update_data["minimax_api_key"] = data.api_key
        elif credential.type == AICredentialType.OPENAI_COMPATIBLE:
            update_data["openai_compatible_api_key"] = data.api_key
            update_data["openai_compatible_base_url"] = data.base_url
            update_data["openai_compatible_model"] = data.model

        if update_data:
            credentials_update = AIServiceCredentialsUpdate(**update_data)
            self.update_user_ai_credentials(
                session=session, user=user, credentials_update=credentials_update
            )
            logger.info(f"Synced default {credential.type} credential to user profile")

    def _clear_user_profile_for_type(
        self, session: Session, user: User, cred_type: AICredentialType
    ) -> None:
        """Clear user profile fields for a credential type when default is deleted"""
        # Get existing credentials
        existing = self.get_user_ai_credentials(user=user)
        if not existing:
            return

        # Get current data and clear the relevant fields
        current_data = existing.model_dump()

        if cred_type == AICredentialType.ANTHROPIC:
            current_data["anthropic_api_key"] = None
        elif cred_type == AICredentialType.MINIMAX:
            current_data["minimax_api_key"] = None
        elif cred_type == AICredentialType.OPENAI_COMPATIBLE:
            current_data["openai_compatible_api_key"] = None
            current_data["openai_compatible_base_url"] = None
            current_data["openai_compatible_model"] = None

        # Encrypt and save
        credential_data_json = json.dumps(current_data)
        user.ai_credentials_encrypted = encrypt_field(credential_data_json)
        session.add(user)
        session.commit()
        session.refresh(user)

        logger.info(f"Cleared {cred_type} from user profile after default deleted")

    # ============= Affected Environments Query =============

    def get_affected_environments(
        self,
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AffectedEnvironmentsPublic:
        """
        Find all environments affected by this credential.

        Returns environments that use this credential for conversation and/or building,
        along with information about users who have access to the credential via shares.

        Delegates environment lookup to EnvironmentService which handles both
        explicitly linked credentials and default credential resolution.
        """
        # Verify user has access to credential (ownership or share)
        if not self.can_access_credential(session, credential_id, user_id):
            raise HTTPException(
                status_code=403,
                detail="Not authorized to access this credential"
            )

        # Get credential
        credential = session.get(AICredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="AI credential not found")

        # Delegate environment lookup to EnvironmentService
        from app.services.environments.environment_service import EnvironmentService

        env_results = EnvironmentService.get_environments_for_credential(
            session, credential
        )

        environments = [
            AffectedEnvironmentPublic(
                environment_id=r["environment"].id,
                agent_id=r["agent"].id,
                agent_name=r["agent"].name,
                environment_name=r["environment"].instance_name,
                status=r["environment"].status,
                usage=r["usage"],
                owner_id=r["owner"].id,
                owner_email=r["owner"].email,
            )
            for r in env_results
        ]

        # Get shared users
        statement_shares = (
            select(AICredentialShare, User)
            .join(User, AICredentialShare.shared_with_user_id == User.id)
            .where(AICredentialShare.ai_credential_id == credential_id)
        )
        share_results = session.exec(statement_shares).all()

        shared_users = [
            SharedUserPublic(
                user_id=user.id,
                email=user.email,
                shared_at=share.shared_at,
            )
            for share, user in share_results
        ]

        return AffectedEnvironmentsPublic(
            credential_id=credential_id,
            credential_name=credential.name,
            environments=environments,
            shared_with_users=shared_users,
            count=len(environments),
        )

    # ============= Sharing Methods =============

    def get_credential_for_use(
        self,
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> AICredentialData | None:
        """
        Get decrypted credential data if user owns it or has a share.
        Returns None if credential doesn't exist or user has no access.
        """
        credential = session.get(AICredential, credential_id)
        if not credential:
            return None

        # Check if user owns the credential
        if credential.owner_id == user_id:
            return self.decrypt_credential(credential)

        # Check if user has a share
        statement = select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == credential_id,
            AICredentialShare.shared_with_user_id == user_id,
        )
        share = session.exec(statement).first()
        if share:
            return self.decrypt_credential(credential)

        return None

    def can_access_credential(
        self,
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> bool:
        """Check if user can access a credential (owns or has share)"""
        credential = session.get(AICredential, credential_id)
        if not credential:
            return False

        # Check ownership
        if credential.owner_id == user_id:
            return True

        # Check share
        statement = select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == credential_id,
            AICredentialShare.shared_with_user_id == user_id,
        )
        return session.exec(statement).first() is not None

    def share_credential(
        self,
        session: Session,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
        recipient_id: uuid.UUID,
    ) -> AICredentialShare:
        """
        Create a share for an AI credential.
        Owner must own the credential, and share must not already exist.
        """
        # Verify credential exists and is owned by owner
        credential = session.get(AICredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="AI credential not found")
        if credential.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="Only the owner can share this credential")

        # Can't share with yourself
        if owner_id == recipient_id:
            raise HTTPException(status_code=400, detail="Cannot share credential with yourself")

        # Check if share already exists
        statement = select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == credential_id,
            AICredentialShare.shared_with_user_id == recipient_id,
        )
        existing_share = session.exec(statement).first()
        if existing_share:
            # Share already exists, return it
            return existing_share

        # Create new share
        share = AICredentialShare(
            ai_credential_id=credential_id,
            shared_with_user_id=recipient_id,
            shared_by_user_id=owner_id,
        )
        session.add(share)
        session.commit()
        session.refresh(share)

        logger.info(f"Created AI credential share: {credential_id} -> user {recipient_id}")
        return share

    def revoke_share(
        self,
        session: Session,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
        recipient_id: uuid.UUID,
    ) -> None:
        """Revoke a credential share"""
        # Verify credential exists and is owned by owner
        credential = session.get(AICredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="AI credential not found")
        if credential.owner_id != owner_id:
            raise HTTPException(status_code=403, detail="Only the owner can revoke shares")

        # Find and delete the share
        statement = select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == credential_id,
            AICredentialShare.shared_with_user_id == recipient_id,
        )
        share = session.exec(statement).first()
        if share:
            session.delete(share)
            session.commit()
            logger.info(f"Revoked AI credential share: {credential_id} -> user {recipient_id}")

    def list_shared_with_me(
        self,
        session: Session,
        user_id: uuid.UUID,
    ) -> list[SharedAICredentialPublic]:
        """List AI credentials shared with the current user"""
        statement = (
            select(AICredentialShare)
            .where(AICredentialShare.shared_with_user_id == user_id)
            .order_by(AICredentialShare.shared_at.desc())
        )
        shares = session.exec(statement).all()

        result = []
        for share in shares:
            credential = session.get(AICredential, share.ai_credential_id)
            owner = session.get(User, credential.owner_id) if credential else None
            if credential and owner:
                result.append(SharedAICredentialPublic(
                    id=credential.id,
                    name=credential.name,
                    type=credential.type if isinstance(credential.type, str) else credential.type.value,
                    owner_id=credential.owner_id,
                    owner_email=owner.email,
                    shared_at=share.shared_at,
                ))
        return result

    def get_credential_public_info(
        self,
        session: Session,
        credential_id: uuid.UUID,
    ) -> tuple[str, str] | None:
        """Get credential name and type (for display purposes, no auth check)"""
        credential = session.get(AICredential, credential_id)
        if not credential:
            return None
        return (credential.name, credential.type if isinstance(credential.type, str) else credential.type.value)


# Singleton instance
ai_credentials_service = AICredentialsService()
