"""Public description of one AI provider, as the adapter declares it.

Every field here already exists as an attribute on a provider adapter. Nothing is
computed for the API and nothing is restated: the point of publishing them is
that the browser stops keeping its own copies. Today "what is this provider
called" has three answers in the frontend and a fourth on the server, and "which
fields does this provider require" is stated three times in one dialog file
beside a server-side validator that is the actual authority. One endpoint fed by
the adapters makes the adapter the only declaration, so adding a sixth provider
is a module in ``app/services/ai_providers/`` rather than an edit in each of
those places.
"""
from sqlmodel import SQLModel

from app.models.credentials.ai_credential import AICredentialType


class ProviderAdapterPublic(SQLModel):
    """One provider, as the server understands it."""

    type: AICredentialType
    #: Human name. The single answer to "what is this provider called".
    label: str
    #: What the native/desktop account-config bundle calls it. Empty string means
    #: "use the credential's own free-form name" — the deliberate sentinel for
    #: ``openai_compatible``, which is a shape rather than a vendor.
    account_config_display_name: str
    account_config_slug: str
    #: ``<engine>/<provider>`` for the AddEnvironment SDK picker.
    sdk_engine: str
    #: The server-side "which fields does this provider require" answer, which
    #: is the authority behind the 400s the create/update routes raise.
    requires_base_url: bool
    requires_model: bool
    supports_model_listing: bool
    issues_oauth_tokens: bool
    #: Whether this provider's administration API can create keys. False for
    #: every provider but one; where it is false, per-user keys are added by
    #: hand, which is a normal path and not a degraded one.
    supports_minting: bool
    #: What an administration credential for this provider needs, when it can
    #: mint. ``None`` when it cannot.
    admin_config_schema: dict | None = None
    #: Whether an administrator may choose per-user minting for this provider
    #: **right now**: the adapter can mint *and* an organisation for it is
    #: connected. The **answer**, not its ingredients.
    #:
    #: ``supports_minting`` above is a fact about the provider and stays, because
    #: it is what tells an admin *why* the option is unavailable. This field is
    #: the policy, and it lives here so the browser does not assemble it from two
    #: endpoints — the create route enforces the same rule in
    #: ``ManagedAICredentialsService._validate_provisioning``, and a conjunction
    #: rebuilt in the client is the copy that stops agreeing the day a third
    #: condition is added.
    #: Required, no default: an absent policy answer read through a client-side
    #: fallback is the browser deciding the policy.
    can_mint_now: bool


class ProviderAdaptersPublic(SQLModel):
    data: list[ProviderAdapterPublic]
    count: int
