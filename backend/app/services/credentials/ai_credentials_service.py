"""
AI Credentials Service

Handles CRUD operations for named AI credentials and syncing defaults to User profile.
"""
import asyncio
import json
import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, func, or_, select

from app.core.security import encrypt_field, decrypt_field
from app.services.ai_providers import registry
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
from app.models.credentials.ai_credential_group_share import (
    AICredentialEnvironmentsAtRisk,
    AICredentialGroupShare,
    AICredentialGroupSharePublic,
)
from app.models.users.user import User, AIServiceCredentials, AIServiceCredentialsUpdate
from app.models.users.user_group import UserGroup, UserGroupMember, UserGroupRef
from app.services.users.user_group_access import UserGroupAccess

logger = logging.getLogger(__name__)

# Strong references to fire-and-forget restart tasks, so the event loop's weak
# reference is not the only one and a pending restart is not garbage-collected.
_background_tasks: set[asyncio.Task] = set()


class AICredentialNotShareableError(HTTPException):
    """Raised when a credential the platform provisioned is asked to be shared.

    A **policy** refusal, permanent, and typed so that its one caller can tell it
    apart from the transient failures it deliberately swallows. It stays an
    ``HTTPException`` subclass so any future route path still answers 400 without
    a mapping layer, and carries the credential id so a log line can name the row
    that cannot be shared rather than only the bundle that wanted it.
    """

    def __init__(self, credential_id: uuid.UUID) -> None:
        self.credential_id = credential_id
        super().__init__(
            status_code=400,
            detail=(
                "This key was provisioned for one person and is withdrawn with "
                "their account, so it cannot be shared."
            ),
        )


class AICredentialInUseError(Exception):
    """Raised when an AI credential cannot be deleted because one or more
    published bundles reference it as a publisher-provided AI credential.

    Deleting it would null the bundle's ``publisher_ai_credential_*_id`` FK
    (``ON DELETE SET NULL``), silently degrading those bundles back to
    "user provides". The route maps this to HTTP 409 and serialises
    ``impact`` into ``detail``. The owner can override via ``force=True``.
    """

    def __init__(self, impact: "AICredentialDeletionImpact") -> None:
        self.impact = impact
        super().__init__(
            "AI credential is provided by the publisher in one or more "
            "published bundles and cannot be deleted without force."
        )


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
        group_counts = self._group_share_counts(session, [c.id for c in credentials])
        return [
            self._to_public(cred, session, group_share_count=group_counts.get(cred.id, 0))
            for cred in credentials
        ]

    @staticmethod
    def _group_share_counts(
        session: Session, credential_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """``{credential_id: number of user groups it is shared with}`` (one query)."""
        if not credential_ids:
            return {}
        return dict(
            session.exec(
                select(AICredentialGroupShare.ai_credential_id, func.count())
                .where(col(AICredentialGroupShare.ai_credential_id).in_(credential_ids))
                .group_by(col(AICredentialGroupShare.ai_credential_id))
            ).all()
        )

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

        # Auto-set expiry notification date for OAuth tokens (11 months from now).
        # No provider type guard: the adapter answers is_oauth_token=False for
        # every provider that issues only API keys.
        expiry_date = data.expiry_notification_date
        if not expiry_date and self._is_oauth_token(data.type, data.api_key):
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
        *,
        admin_override: bool = False,
    ) -> AICredentialPublic:
        """Update an existing AI credential.

        Admin-managed rows are read-only through the user-facing path: a 403 is
        raised unless ``admin_override`` is passed (the admin provisioning
        service sets it). The owner may still set such a row as their default
        (that path does not call this method).
        """
        credential = self.get_credential(session, credential_id, user_id)

        if credential.is_admin_managed and not admin_override:
            raise HTTPException(
                status_code=403,
                detail=(
                    "This credential is managed by your administrator and "
                    "cannot be modified."
                ),
            )

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
        if (
            data.api_key is not None
            and data.expiry_notification_date is None
            and self._is_oauth_token(credential.type, new_api_key)
        ):
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

    def _get_owned_credential_or_404(
        self, session: Session, credential_id: uuid.UUID, user_id: uuid.UUID
    ) -> AICredential:
        """Fetch an owned AI credential, returning 404 for missing OR not-owned.

        The shared :meth:`get_credential` raises 403 for non-owners; other
        callers depend on that. The owner-only deletion paths instead use this
        helper so they don't leak credential existence via a 403 — matching the
        service-credential side, which returns 404 for both cases.
        """
        credential = session.get(AICredential, credential_id)
        if not credential or credential.owner_id != user_id:
            raise HTTPException(status_code=404, detail="AI credential not found")
        return credential

    def get_deletion_impact(
        self, session: Session, credential_id: uuid.UUID, user_id: uuid.UUID
    ) -> "AICredentialDeletionImpact":
        """Classify the blast radius of deleting an AI credential.

        Detects published bundles whose
        ``publisher_ai_credential_conversation_id`` /
        ``publisher_ai_credential_building_id`` reference this credential.
        Any such reference makes deletion Tier 2 (blocked unless forced),
        since deleting nulls the FK and degrades the bundle to "user provides".

        Owner-only: raises 404 if the credential does not exist OR is not owned
        by ``user_id`` (no existence leak — mirrors the service-credential side).
        """
        from app.models import (
            AICredentialBundleUsage,
            AICredentialDeletionImpact,
        )
        from app.models.bundles.agent_bundle import AgentBundle
        from app.models.agents.agent import Agent

        # Ownership / existence check (404 for missing or not-owned).
        self._get_owned_credential_or_404(session, credential_id, user_id)

        stmt = select(AgentBundle).where(
            (AgentBundle.publisher_ai_credential_conversation_id == credential_id)
            | (AgentBundle.publisher_ai_credential_building_id == credential_id)
        )
        bundles = session.exec(stmt).all()

        usages: list[AICredentialBundleUsage] = []
        for bundle in bundles:
            publisher_install = session.exec(
                select(Agent.id).where(
                    Agent.bundle_uuid == bundle.id,
                    Agent.is_publisher_install == True,  # noqa: E712
                )
            ).first()
            usages.append(
                AICredentialBundleUsage(
                    bundle_uuid=bundle.id,
                    bundle_id=bundle.bundle_id,
                    display_name=bundle.display_name,
                    publisher_install_id=publisher_install,
                    used_for_conversation=(
                        bundle.publisher_ai_credential_conversation_id
                        == credential_id
                    ),
                    used_for_building=(
                        bundle.publisher_ai_credential_building_id
                        == credential_id
                    ),
                )
            )

        return AICredentialDeletionImpact(
            tier=2 if usages else 0,
            bundle_usages=usages,
            group_share_count=self._group_share_counts(
                session, [credential_id]
            ).get(credential_id, 0),
        )

    def delete_credential(
        self,
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
        force: bool = False,
        *,
        admin_override: bool = False,
    ) -> None:
        """Delete an AI credential.

        Blocked with :class:`AICredentialInUseError` (HTTP 409) when one or
        more published bundles reference this credential as a
        publisher-provided AI credential, unless ``force`` is passed.

        Admin-managed rows are read-only through the user-facing path: a 403 is
        raised unless ``admin_override`` is passed (the admin provisioning
        service sets it).

        Owner-only: returns 404 for missing or not-owned (no existence leak).
        """
        credential = self._get_owned_credential_or_404(
            session, credential_id, user_id
        )

        if credential.is_admin_managed and not admin_override:
            raise HTTPException(
                status_code=403,
                detail=(
                    "This credential is managed by your administrator and "
                    "cannot be deleted."
                ),
            )

        if not force:
            impact = self.get_deletion_impact(session, credential_id, user_id)
            if impact.tier == 2:
                raise AICredentialInUseError(impact)

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

    def upsert_onboarding_credentials(
        self,
        session: Session,
        user: User,
        credentials_in: AIServiceCredentialsUpdate,
    ) -> None:
        """
        Create or update AI credentials from the onboarding flow.

        For each provided key, finds the existing default credential of that type
        and updates it, or creates a new one and sets it as default.

        When an Anthropic key is provided and the user's AI functions preference
        is unset or "system", auto-sets it to "personal:anthropic" so AI utility
        functions work immediately.
        """
        type_configs = []

        if credentials_in.anthropic_api_key:
            type_configs.append((
                AICredentialType.ANTHROPIC,
                "Anthropic API Key",
                credentials_in.anthropic_api_key,
                None,  # base_url
                None,  # model
            ))

        if credentials_in.minimax_api_key:
            type_configs.append((
                AICredentialType.MINIMAX,
                "MiniMax API Key",
                credentials_in.minimax_api_key,
                None,
                None,
            ))

        if credentials_in.openai_compatible_api_key:
            type_configs.append((
                AICredentialType.OPENAI_COMPATIBLE,
                "OpenAI Compatible API",
                credentials_in.openai_compatible_api_key,
                credentials_in.openai_compatible_base_url,
                credentials_in.openai_compatible_model,
            ))

        for cred_type, name, api_key, base_url, model in type_configs:
            self._upsert_default_credential(
                session, user.id, cred_type, name, api_key, base_url, model,
            )

        # Auto-set AI functions to use the personal Anthropic key
        # so the user can start using platform features immediately
        if credentials_in.anthropic_api_key:
            if not user.default_ai_functions_sdk or user.default_ai_functions_sdk == "system":
                user.default_ai_functions_sdk = "personal:anthropic"
                session.add(user)
                session.commit()
                session.refresh(user)
                logger.info(f"Auto-set AI functions to personal:anthropic for user {user.id}")

        # Auto-set AI functions to personal:openai when first OpenAI key is added
        # and no personal preference is already configured (Anthropic takes priority
        # if both keys are submitted in the same request)
        if credentials_in.openai_api_key:
            if not user.default_ai_functions_sdk or user.default_ai_functions_sdk == "system":
                user.default_ai_functions_sdk = "personal:openai"
                session.add(user)
                session.commit()
                session.refresh(user)
                logger.info(f"Auto-set AI functions to personal:openai for user {user.id}")

    def _upsert_default_credential(
        self,
        session: Session,
        user_id: uuid.UUID,
        cred_type: AICredentialType,
        name: str,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        """Create or update the default credential for a given type."""
        existing = self.get_default_for_type(session, user_id, cred_type)
        if existing:
            self.update_credential(
                session, existing.id, user_id,
                AICredentialUpdate(api_key=api_key, base_url=base_url, model=model),
            )
        else:
            new_cred = self.create_credential(
                session, user_id,
                AICredentialCreate(
                    name=name, type=cred_type,
                    api_key=api_key, base_url=base_url, model=model,
                ),
            )
            self.set_default(session, new_cred.id, user_id)

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

    def owner_ids_with_a_default(
        self, session: Session, user_ids: Iterable[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Which of ``user_ids`` hold at least one default AI credential.

        **The one implementation of "can this person's account do anything".**
        Set-shaped rather than a per-user boolean because every caller asks it
        for a list — the admin member projection asks it once per reconcile, not
        once per member — and an N+1 there is how a cheap predicate becomes a
        reason not to use it. There is deliberately no single-user convenience
        wrapper: one shipped, nothing ever called it, and the shape it offered is
        precisely the one that reintroduces the N+1 inside a loop.

        *A default*, not *a credential*: every path that resolves a credential
        for an environment ends at ``get_default_for_type``, so a row that is not
        anybody's default is a row no environment will pick up on its own.

        *Any provider*, not Anthropic. An environment requires a default of the
        type **its SDK expects**, and every credential type is served by an SDK
        the user can choose (``claude-code`` for anthropic/minimax,
        ``opencode/<provider>`` for the rest — see
        ``sdk_constants.SDK_CREDENTIAL_COMPATIBILITY``). Nothing in the
        environment code requires Anthropic specifically, so scoping this to
        Anthropic asserted a requirement the platform does not have — and made
        a person who had just been minted an OpenAI key look, forever, like a
        person with no key at all.
        """
        ids = list(user_ids)
        if not ids:
            return set()
        rows = session.exec(
            select(AICredential.owner_id).where(
                col(AICredential.owner_id).in_(ids),
                AICredential.is_default == True,  # noqa: E712
            )
        ).all()
        return set(rows)

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

    @staticmethod
    def _is_oauth_token(cred_type, api_key: str | None) -> bool:
        """Whether ``api_key`` is a delegated OAuth token rather than an API key.

        Delegates to the provider adapter, which is the single implementation of
        every provider's key-prefix rule. Safe to call for any provider: those
        that issue only API keys always answer False, which is what lets the
        callers drop their ``type == ANTHROPIC`` guards.
        """
        if not api_key:
            return False
        adapter = registry.find_adapter(cred_type)
        return adapter is not None and adapter.classify_key(api_key).is_oauth_token

    def _to_public(
        self,
        credential: AICredential,
        session: Session,
        group_share_count: int | None = None,
    ) -> AICredentialPublic:
        """Convert credential to public representation.

        ``group_share_count`` is batched by list callers; single-row callers
        leave it ``None`` and it is counted here.
        """
        if group_share_count is None:
            group_share_count = self._group_share_counts(
                session, [credential.id]
            ).get(credential.id, 0)
        # Decrypt to get non-sensitive fields
        data = self.decrypt_credential(credential)

        # Detect OAuth token. The adapter owns the prefix rule and answers
        # False for providers that issue only API keys, so no type guard here.
        is_oauth = self._is_oauth_token(credential.type, data.api_key)

        return AICredentialPublic(
            id=credential.id,
            name=credential.name,
            type=credential.type,
            is_default=credential.is_default,
            is_admin_managed=credential.is_admin_managed,
            has_api_key=bool(data.api_key),
            is_oauth_token=is_oauth,
            base_url=data.base_url,
            model=data.model,
            discovered_models=credential.discovered_models,
            models_discovered_at=credential.models_discovered_at,
            models_discovery_error=credential.models_discovery_error,
            # Admin-curated model metadata (read-only for the owner — see
            # admin_curated_model_list). NULL for self-created credentials.
            default_model=credential.default_model,
            available_models=credential.available_models,
            group_share_count=group_share_count,
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

        # Which extra fields a provider needs is the adapter's answer — the one
        # server-side authority behind these 400s.
        adapter = registry.find_adapter(cred_type)
        if adapter is None:
            return
        if adapter.requires_base_url and not base_url:
            raise HTTPException(
                status_code=400,
                detail=f"Base URL is required for {adapter.label} credentials",
            )
        if adapter.requires_model and not model:
            raise HTTPException(
                status_code=400,
                detail=f"Model is required for {adapter.label} credentials",
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

        # Direct or user-group share
        if self.has_share(session, credential_id, user_id):
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

        return self.has_share(session, credential_id, user_id)

    # ============= User group access =============

    @staticmethod
    def has_share(
        session: Session, credential_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Whether ``user_id`` holds a direct ``AICredentialShare`` or a group share.

        Ownership is not checked; callers test it first.
        """
        direct = session.exec(
            select(AICredentialShare.id).where(
                AICredentialShare.ai_credential_id == credential_id,
                AICredentialShare.shared_with_user_id == user_id,
            )
        ).first()
        if direct is not None:
            return True
        group = session.exec(
            select(AICredentialGroupShare.id).where(
                AICredentialGroupShare.ai_credential_id == credential_id,
                col(AICredentialGroupShare.group_id).in_(
                    UserGroupAccess.member_group_ids_subquery(user_id)
                ),
            )
        ).first()
        return group is not None

    @staticmethod
    def users_without_access(
        session: Session,
        credential_id: uuid.UUID,
        user_ids: Iterable[uuid.UUID],
        *,
        exclude_group_id: uuid.UUID | None = None,
    ) -> set[uuid.UUID]:
        """Users among ``user_ids`` with no remaining path to the AI credential.

        Excludes the owner, direct ``AICredentialShare`` holders and members of
        any group it is shared with. ``exclude_group_id`` ignores one group
        share, to preview what revoking it would cost.
        """
        remaining = {uid for uid in user_ids if uid is not None}
        if not remaining:
            return set()
        credential = session.get(AICredential, credential_id)
        if credential is None:
            return remaining
        remaining.discard(credential.owner_id)
        if not remaining:
            return set()
        direct = set(
            session.exec(
                select(AICredentialShare.shared_with_user_id).where(
                    AICredentialShare.ai_credential_id == credential_id,
                    col(AICredentialShare.shared_with_user_id).in_(remaining),
                )
            ).all()
        )
        group_stmt = (
            select(UserGroupMember.user_id)
            .join(
                AICredentialGroupShare,
                col(AICredentialGroupShare.group_id) == col(UserGroupMember.group_id),
            )
            .where(
                AICredentialGroupShare.ai_credential_id == credential_id,
                col(UserGroupMember.user_id).in_(remaining),
            )
        )
        if exclude_group_id is not None:
            group_stmt = group_stmt.where(
                AICredentialGroupShare.group_id != exclude_group_id
            )
        return remaining - direct - set(session.exec(group_stmt).all())

    @staticmethod
    def _pinning_environments(
        session: Session, credential_id: uuid.UUID, owner_ids: set[uuid.UUID]
    ) -> list:
        """Environments of agents owned by ``owner_ids`` that pin the credential
        for conversation or building."""
        from app.models.agents.agent import Agent
        from app.models.environments.environment import AgentEnvironment

        if not owner_ids:
            return []
        return list(
            session.exec(
                select(AgentEnvironment)
                .join(Agent, col(Agent.id) == col(AgentEnvironment.agent_id))
                .where(
                    col(Agent.owner_id).in_(owner_ids),
                    or_(
                        AgentEnvironment.conversation_ai_credential_id == credential_id,
                        AgentEnvironment.building_ai_credential_id == credential_id,
                    ),
                )
            ).all()
        )

    @staticmethod
    def _group_member_ids(session: Session, group_id: uuid.UUID) -> set[uuid.UUID]:
        """Every member of the group, active or not (an inactive one may still
        own environments pinning the key)."""
        return set(
            session.exec(
                select(UserGroupMember.user_id).where(
                    UserGroupMember.group_id == group_id
                )
            ).all()
        )

    def _environments_at_risk(
        self,
        session: Session,
        credential_id: uuid.UUID,
        group_id: uuid.UUID,
        member_ids: set[uuid.UUID],
    ) -> AICredentialEnvironmentsAtRisk:
        lost = self.users_without_access(
            session, credential_id, member_ids, exclude_group_id=group_id
        )
        envs = self._pinning_environments(session, credential_id, lost)
        return AICredentialEnvironmentsAtRisk(
            total=len(envs),
            running=sum(1 for env in envs if env.status == "running"),
        )

    @staticmethod
    def _members_by_group(
        session: Session, group_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[set[uuid.UUID], int]]:
        """``{group_id: (all member ids, active member count)}`` in one query.

        Every member feeds ``environments_at_risk``; ``member_count`` counts
        active members only.
        """
        result: dict[uuid.UUID, tuple[set[uuid.UUID], int]] = {
            gid: (set(), 0) for gid in group_ids
        }
        if not group_ids:
            return result
        rows = session.exec(
            select(UserGroupMember.group_id, UserGroupMember.user_id, User.is_active)
            .join(User, col(User.id) == col(UserGroupMember.user_id))
            .where(col(UserGroupMember.group_id).in_(group_ids))
        ).all()
        for gid, uid, is_active in rows:
            members, active = result[gid]
            members.add(uid)
            result[gid] = (members, active + (1 if is_active else 0))
        return result

    def _group_share_public(
        self,
        session: Session,
        share: AICredentialGroupShare,
        group: UserGroup,
        members: tuple[set[uuid.UUID], int] | None = None,
    ) -> AICredentialGroupSharePublic:
        if members is None:
            members = self._members_by_group(session, [group.id])[group.id]
        member_ids, active_count = members
        return AICredentialGroupSharePublic(
            group_id=group.id,
            group_name=group.name,
            member_count=active_count,
            shared_by_user_id=share.shared_by_user_id,
            shared_at=share.shared_at,
            environments_at_risk=self._environments_at_risk(
                session, share.ai_credential_id, group.id, member_ids
            ),
        )

    def _owned_for_group_share(
        self, session: Session, credential_id: uuid.UUID, owner_id: uuid.UUID
    ) -> AICredential:
        credential = session.get(AICredential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="AI credential not found")
        if credential.owner_id != owner_id:
            raise HTTPException(
                status_code=403,
                detail="Only the owner can manage this credential's group shares",
            )
        return credential

    def share_with_group(
        self,
        session: Session,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
        group_id: uuid.UUID,
    ) -> AICredentialGroupSharePublic:
        """Share an owned AI credential with a user group (D9).

        404 unknown credential / group, 403 not the owner, 400 admin-managed
        (``AICredentialNotShareableError``), 409 duplicate.
        """
        credential = self._owned_for_group_share(session, credential_id, owner_id)
        if not self.is_shareable(credential):
            raise AICredentialNotShareableError(credential.id)
        # Read lock: a concurrent group delete / member removal (FOR UPDATE)
        # waits for this share, so its coverage snapshot includes it.
        group = session.exec(
            select(UserGroup).where(UserGroup.id == group_id).with_for_update(read=True)
        ).first()
        if group is None:
            raise HTTPException(status_code=404, detail="User group not found")
        existing = session.exec(
            select(AICredentialGroupShare.id).where(
                AICredentialGroupShare.ai_credential_id == credential_id,
                AICredentialGroupShare.group_id == group_id,
            )
        ).first()
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"AI credential is already shared with group '{group.name}'",
            )
        share = AICredentialGroupShare(
            ai_credential_id=credential_id,
            group_id=group_id,
            shared_by_user_id=owner_id,
        )
        session.add(share)
        try:
            session.commit()
        except IntegrityError as exc:
            # A concurrent request created the same share after our check.
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"AI credential is already shared with group '{group.name}'",
            ) from exc
        session.refresh(share)
        logger.info(
            "AI credential %s shared with user group %s by owner %s",
            credential_id,
            group_id,
            owner_id,
        )
        return self._group_share_public(session, share, group)

    def list_group_shares(
        self, session: Session, credential_id: uuid.UUID, owner_id: uuid.UUID
    ) -> list[AICredentialGroupSharePublic]:
        """Every group share of an owned AI credential, by group name."""
        self._owned_for_group_share(session, credential_id, owner_id)
        rows = session.exec(
            select(AICredentialGroupShare, UserGroup)
            .join(UserGroup, col(UserGroup.id) == col(AICredentialGroupShare.group_id))
            .where(AICredentialGroupShare.ai_credential_id == credential_id)
            .order_by(col(UserGroup.name))
        ).all()
        members = self._members_by_group(session, [group.id for _, group in rows])
        return [
            self._group_share_public(session, share, group, members[group.id])
            for share, group in rows
        ]

    async def revoke_group_share(
        self,
        session: Session,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
        group_id: uuid.UUID,
    ) -> None:
        """Owner revoke of a group share; see :meth:`remove_group_share`."""
        self._owned_for_group_share(session, credential_id, owner_id)
        await self.remove_group_share(
            session, credential_id, group_id, actor_id=owner_id
        )

    async def remove_group_share(
        self,
        session: Session,
        credential_id: uuid.UUID,
        group_id: uuid.UUID,
        *,
        actor_id: uuid.UUID,
    ) -> None:
        """Remove a group share, then release it from members who lost every path.

        No ownership check: callers authorise (the owner route, or the
        superuser group page). 404 when the share does not exist.
        """
        share = session.exec(
            select(AICredentialGroupShare).where(
                AICredentialGroupShare.ai_credential_id == credential_id,
                AICredentialGroupShare.group_id == group_id,
            )
        ).first()
        if share is None:
            raise HTTPException(status_code=404, detail="Group share not found")
        member_ids = self._group_member_ids(session, group_id)
        session.delete(share)
        session.commit()
        logger.info(
            "AI credential %s group share with %s revoked by %s",
            credential_id,
            group_id,
            actor_id,
        )
        await self.release_lost(
            session, credential_ids={credential_id}, user_ids=member_ids
        )

    def lost_environments(
        self,
        session: Session,
        credential_id: uuid.UUID,
        user_ids: Iterable[uuid.UUID],
        *,
        exclude_group_id: uuid.UUID | None = None,
    ) -> tuple[set[uuid.UUID], list]:
        """``(users who would lose the key, their environments pinning it)``.

        Read-only; ``exclude_group_id`` previews the loss of one group share.
        """
        lost = self.users_without_access(
            session,
            credential_id,
            {uid for uid in user_ids if uid is not None},
            exclude_group_id=exclude_group_id,
        )
        return lost, self._pinning_environments(session, credential_id, lost)

    async def release_lost(
        self,
        session: Session,
        *,
        credential_ids: Iterable[uuid.UUID],
        user_ids: Iterable[uuid.UUID],
    ) -> None:
        """Drop AI credentials from the environments of users who lost them (D10).

        The caller passes exactly the pairs a change covered (the credentials ×
        the affected users), snapshotted before the change. Only users with no
        remaining path (owner, direct share, another group) are touched: their
        environments pinning the key get the matching column(s) set to NULL
        (committed), and the running ones are restarted in the background so
        ``.env`` is regenerated without it. Call after the change that removed
        access has been committed.
        """
        targets = {uid for uid in user_ids if uid is not None}
        if not targets:
            return

        to_restart: list[uuid.UUID] = []
        for cid in dict.fromkeys(credential_ids):
            _, envs = self.lost_environments(session, cid, targets)
            for env in envs:
                if env.conversation_ai_credential_id == cid:
                    env.conversation_ai_credential_id = None
                if env.building_ai_credential_id == cid:
                    env.building_ai_credential_id = None
                session.add(env)
                if env.status == "running":
                    to_restart.append(env.id)
        session.commit()

        for env_id in dict.fromkeys(to_restart):
            task = asyncio.create_task(_restart_environment_background(env_id))
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        if to_restart:
            logger.info(
                "AI credential release: %s environment(s) scheduled for restart",
                len(to_restart),
            )

    @staticmethod
    def is_shareable(credential: AICredential) -> bool:
        """Whether this credential's owner may hand it to somebody else.

        **A child of a managed record is never shareable, minted or shared.**
        The predicate is ``is_admin_managed`` — a plain column on the row, with
        no lookup — and both halves of that are deliberate.

        *Never*, not "not if minted". A minted child is revoked at the provider
        when its holder is deactivated or removed from the record; a
        ``shared``-mode child is deleted by the same reconcile when its holder is
        removed. Either way the ``AICredentialShare`` rows cascade with it and
        the sharees lose access with no event in their feed and no way to see
        why. The distinction that used to be drawn here — minted refused, shared
        allowed — described a difference in *how* the row dies, not in whether it
        does.

        *The column*, not the parent record. ``managed_credential_id`` is
        ``ON DELETE SET NULL`` and an orphaned child is a documented tolerated
        state, so a guard that started with ``if managed_credential_id is None:
        return False`` answered "shareable" for exactly the rows whose parent had
        been force-deleted — the ones most likely to be in a strange state. The
        tolerated orphan of one component must not be the silent assumption of
        another; ``is_admin_managed`` survives the orphaning, so it is what this
        asks.
        """
        return not credential.is_admin_managed

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

        # A credential the platform provisioned for one person cannot be shared.
        # "Provisioned for you" and "shareable" are contradictory claims, and the
        # contradiction is not abstract: the row is destroyed when its holder is
        # deactivated or removed from the record, and the share rows go with it
        # (``ON DELETE CASCADE``). Refused here rather than papered over in the
        # revoke path, because the revoke is right and the share is the thing
        # that should not exist.
        #
        # Its own exception type, not a bare ``HTTPException``. The only caller
        # is bundle publisher wiring, which catches ``HTTPException`` broadly to
        # keep a transient hiccup from aborting an install — so a plain 400 here
        # was indistinguishable from "the row vanished", and a permanent policy
        # refusal was being logged as a transient warning and then reported to
        # the publisher as ``publisher_credential_unshared``: fix a thing that
        # policy will never let you fix.
        if not self.is_shareable(credential):
            raise AICredentialNotShareableError(credential.id)

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
        """List AI credentials shared with the current user, directly or through
        a user group. A direct share's ``shared_at`` wins; ``via_groups`` names
        the groups either way. Newest first."""
        statement = (
            select(AICredentialShare)
            .where(AICredentialShare.shared_with_user_id == user_id)
            .order_by(AICredentialShare.shared_at.desc())
        )
        shared_at_by_id: dict[uuid.UUID, datetime] = {
            share.ai_credential_id: share.shared_at
            for share in session.exec(statement).all()
        }
        group_rows = session.exec(
            select(
                AICredentialGroupShare.ai_credential_id,
                UserGroup.id,
                UserGroup.name,
                AICredentialGroupShare.shared_at,
            )
            .join(UserGroup, col(UserGroup.id) == col(AICredentialGroupShare.group_id))
            .where(
                col(AICredentialGroupShare.group_id).in_(
                    UserGroupAccess.member_group_ids_subquery(user_id)
                )
            )
            .order_by(col(UserGroup.name))
        ).all()
        groups_by_id: dict[uuid.UUID, list[UserGroupRef]] = {}
        group_first_shared: dict[uuid.UUID, datetime] = {}
        for cred_id, gid, gname, group_shared_at in group_rows:
            groups_by_id.setdefault(cred_id, []).append(UserGroupRef(id=gid, name=gname))
            current = group_first_shared.get(cred_id)
            if current is None or group_shared_at < current:
                group_first_shared[cred_id] = group_shared_at
        for cred_id, group_shared_at in group_first_shared.items():
            shared_at_by_id.setdefault(cred_id, group_shared_at)

        credentials = {
            c.id: c
            for c in session.exec(
                select(AICredential).where(
                    col(AICredential.id).in_(list(shared_at_by_id)),
                    AICredential.owner_id != user_id,
                )
            ).all()
        } if shared_at_by_id else {}
        owners = {
            u.id: u
            for u in session.exec(
                select(User).where(
                    col(User.id).in_({c.owner_id for c in credentials.values()})
                )
            ).all()
        } if credentials else {}

        result = []
        for cred_id, shared_at in shared_at_by_id.items():
            credential = credentials.get(cred_id)
            if not credential:
                continue
            owner = owners.get(credential.owner_id)
            if not owner:
                continue
            data = self.decrypt_credential(credential)
            result.append(SharedAICredentialPublic(
                id=credential.id,
                name=credential.name,
                type=credential.type if isinstance(credential.type, str) else credential.type.value,
                owner_id=credential.owner_id,
                owner_email=owner.email,
                shared_at=shared_at,
                base_url=data.base_url,
                model=data.model,
                default_model=credential.default_model,
                via_groups=groups_by_id.get(credential.id, []),
            ))
        # Direct share timestamps are naive (UTC), group ones are aware.
        result.sort(
            key=lambda row: row.shared_at
            if row.shared_at.tzinfo
            else row.shared_at.replace(tzinfo=timezone.utc),
            reverse=True,
        )
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


async def _restart_environment_background(env_id: uuid.UUID) -> None:
    """Restart one environment on its own session so ``.env`` is regenerated
    without a released AI credential (D10). Failures are logged, not raised."""
    from app.core.db import create_session
    from app.services.environments.environment_service import EnvironmentService

    try:
        with create_session() as bg_session:
            await EnvironmentService.restart_environment(bg_session, env_id)
    except Exception:
        logger.exception(
            "Restart after AI credential release failed for environment %s", env_id
        )


# Singleton instance
ai_credentials_service = AICredentialsService()
