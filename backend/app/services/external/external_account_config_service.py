"""
External Account-Config Service.

Builds the native account-config bundle for a single authenticated user:
their own usable AI credentials, each with the DECRYPTED api key, mapped to a
provider descriptor (display name / slug / suggested model). Consumed only by
``GET /api/v1/external/account-config`` (native-token-gated, audited).

Security: this is the one service that returns decrypted key bytes. It is
strictly self-scoped (``owner_id == user.id`` only — shares are NOT included,
see OQ-6) and never logs key material. The route enforces the native-token
gate and writes the high-severity audit event.
"""
import logging
import uuid
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models.credentials.ai_credential import (
    AICredential,
    AICredentialType,
)
from app.models.external.account_config import (
    AccountConfigProviderPublic,
    AccountConfigResponse,
)
from app.models.users.user import User
from app.services.credentials.ai_credentials_service import (
    ai_credentials_service,
)
from app.services.environments.model_catalog import (
    _strip_provider_prefix,
    is_known_word,
    resolve_model,
)

logger = logging.getLogger(__name__)


# Provider → (display_name, descriptor_slug). For openai_compatible the display
# name comes from the credential's own free-form name.
_PROVIDER_DISPLAY: dict[AICredentialType, tuple[str, str]] = {
    AICredentialType.ANTHROPIC: ("Claude", "claude"),
    AICredentialType.OPENAI: ("OpenAI", "openai"),
    AICredentialType.GOOGLE: ("Gemini", "gemini"),
    AICredentialType.OPENAI_COMPATIBLE: ("", "openai-compatible"),
    AICredentialType.MINIMAX: ("MiniMax", "minimax"),
}

# Credential type → (engine, provider) for catalog default-model lookup.
# Mirrors the AddEnvironment SDK composition (claude-code for anthropic/minimax,
# opencode for the rest).
_TYPE_TO_ENGINE_PROVIDER: dict[AICredentialType, tuple[str, str]] = {
    AICredentialType.ANTHROPIC: ("claude-code", "anthropic"),
    AICredentialType.MINIMAX: ("claude-code", "minimax"),
    AICredentialType.OPENAI: ("opencode", "openai"),
    AICredentialType.GOOGLE: ("opencode", "google"),
    AICredentialType.OPENAI_COMPATIBLE: ("opencode", "openai_compatible"),
}


class ExternalAccountConfigService:
    """Builds the native account-config response for one user."""

    def build_config(
        self, session: Session, user: User
    ) -> AccountConfigResponse:
        """Build the account-config bundle for ``user``.

        Returns every AI credential the user OWNS (admin-managed + self-created)
        with its decrypted key and a provider descriptor. Does NOT emit the
        audit event — the route does that so it can include request context.
        """
        statement = (
            select(AICredential)
            .where(AICredential.owner_id == user.id)
            .order_by(AICredential.created_at.asc())
        )
        credentials = session.exec(statement).all()

        # Skip (don't crash the login bootstrap on) a single undecryptable row.
        # Never log key bytes — only the credential id and a coarse marker.
        providers: list[AccountConfigProviderPublic] = []
        for credential in credentials:
            try:
                providers.append(self._to_provider(credential))
            except Exception:
                logger.warning(
                    "Skipping AI credential %s in account-config: could not "
                    "build provider descriptor (likely undecryptable).",
                    credential.id,
                )

        # Resolved conversation default: the user's default_sdk_conversation
        # determines provider precedence (Anthropic > Google > OpenAI), reusing
        # the existing per-user resolution. Falls back to None when no default.
        default_credential_id = self._resolve_default_credential_id(session, user)

        return AccountConfigResponse(
            providers=providers,
            default_provider_credential_id=default_credential_id,
            generated_at=datetime.now(timezone.utc),
        )

    def _to_provider(
        self, credential: AICredential
    ) -> AccountConfigProviderPublic:
        """Map one owned credential to a provider descriptor with its decrypted
        key. ``credential`` MUST be owned by the caller (enforced upstream)."""
        data = ai_credentials_service.decrypt_credential(credential)
        cred_type = (
            credential.type
            if isinstance(credential.type, AICredentialType)
            else AICredentialType(credential.type)
        )

        display_name, slug = _PROVIDER_DISPLAY.get(
            cred_type, (credential.name, "provider")
        )
        if not display_name:  # openai_compatible → use the credential's name
            display_name = credential.name

        model = self._resolve_model(
            cred_type,
            data.model,
            credential.discovered_models,
            credential.default_model,
            credential.available_models,
        )

        # Curated wins, else discovered (see admin_curated_model_list).
        suggested_models = (
            credential.available_models
            or credential.discovered_models
            or []
        )

        return AccountConfigProviderPublic(
            credential_id=credential.id,
            provider_type=cred_type,
            display_name=display_name,
            descriptor_slug=slug,
            base_url=data.base_url,
            model=model,
            api_key=data.api_key,
            is_default=credential.is_default,
            is_admin_managed=credential.is_admin_managed,
            default_chat_mode_label=display_name,
            suggested_models=suggested_models,
        )

    def _resolve_model(
        self,
        cred_type: AICredentialType,
        credential_model: str | None,
        discovered_models: list[str] | None,
        default_model: str | None = None,
        available_models: list[str] | None = None,
    ) -> str | None:
        """Suggested model for a NATIVE client, which talks to the provider API
        directly with the decrypted key. It therefore needs a concrete,
        provider-usable model id — NOT a Claude-Code tier word (haiku/sonnet)
        nor an ``opencode/``-namespaced id, both of which are SDK-internal.

        Resolution (single default wins early; see admin_curated_model_list):
          1. ``credential.default_model`` (admin curated) — when set. Dropped if
             it is an SDK tier word (the native client can't use "haiku" against
             the provider API) → falls through to the next step.
          2. ``credential.model`` (openai_compatible's required model) — when set.
          3. First entry of ``available_models`` (curated), prefix-stripped.
          4. First entry of ``discovered_models``, prefix-stripped.
          5. The model-catalog default for this provider, but ONLY when it is a
             concrete id (tier words are dropped).
          6. ``None`` — the client falls back to its own default / picks from
             ``suggested_models``.
        """
        # 1. Admin-curated default (tier words can't be used directly).
        if default_model and not is_known_word(default_model):
            return _strip_provider_prefix(default_model)

        # 2. openai_compatible's required model.
        if credential_model:
            return credential_model

        # 3. First curated model.
        if available_models:
            return _strip_provider_prefix(available_models[0])

        # 4. First discovered model the key can see.
        if discovered_models:
            return _strip_provider_prefix(discovered_models[0])

        engine_provider = _TYPE_TO_ENGINE_PROVIDER.get(cred_type)
        if engine_provider is None:
            return None
        engine, provider = engine_provider
        catalog_default = resolve_model(
            engine=engine,
            provider=provider,
            mode="building",  # BALANCED — the representative headline model
            override=None,
            openai_compatible_model=None,
        )
        # Drop SDK-internal tier words (claude-code stores haiku/sonnet/opus,
        # which are not provider API model ids).
        if is_known_word(catalog_default):
            return None
        return _strip_provider_prefix(catalog_default)

    def _resolve_default_credential_id(
        self, session: Session, user: User
    ) -> uuid.UUID | None:
        """Resolve the conversation-default credential id for precedence,
        reusing the existing per-user resolution."""
        sdk_engine = user.default_sdk_conversation or "claude-code"
        # resolve_default_credential_for_sdk keys off the engine prefix.
        engine_prefix = sdk_engine.split("/")[0]
        resolved = ai_credentials_service.resolve_default_credential_for_sdk(
            session, user.id, engine_prefix
        )
        return resolved.id if resolved else None


# Singleton instance.
external_account_config_service = ExternalAccountConfigService()
