"""
Native account-config models.

Response schema for ``GET /api/v1/external/account-config`` — the
native-token-gated endpoint that returns the caller's own usable AI
credentials *including the decrypted api_key* so Cinna Desktop/Mobile can
auto-create local "LLM providers" + a default chat mode on login.

This is a deliberate, product-approved relaxation of the platform's
"keys never exposed" invariant. See ``external_account_config_service`` and the
route module for the security boundary (native-token gate, self-scope, audit,
no-store).
"""
import uuid
from datetime import datetime

from sqlmodel import SQLModel

from app.models.credentials.ai_credential import AICredentialType


class AccountConfigProviderPublic(SQLModel):
    """One LLM provider descriptor for a native client, carrying the DECRYPTED
    api key. Every field except ``api_key`` is non-secret."""
    credential_id: uuid.UUID
    provider_type: AICredentialType
    display_name: str
    # The credential's own user-given name (e.g. "Work Claude"). Native clients
    # append this to the provider-family display_name so multiple credentials of
    # the same provider are distinguishable instead of all reading "Claude".
    credential_name: str
    descriptor_slug: str
    base_url: str | None = None
    model: str | None = None
    # *** DECRYPTED *** — the security boundary; only delivered to native tokens.
    api_key: str
    is_default: bool
    is_admin_managed: bool
    default_chat_mode_label: str
    suggested_models: list[str] = []


class AccountConfigResponse(SQLModel):
    """The native account configuration bundle for the authenticated user."""
    providers: list[AccountConfigProviderPublic]
    # Resolved conversation default so the app can pick precedence.
    default_provider_credential_id: uuid.UUID | None = None
    generated_at: datetime
