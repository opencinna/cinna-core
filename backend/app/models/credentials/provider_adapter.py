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
    #: Whether this provider's administration API can create keys, and — since
    #: a provider carries its own administration secret — the whole of the
    #: server's answer to "may an administrator choose per-user minting for
    #: this type". False for every provider but one; where it is false,
    #: per-user keys are added by hand, which is a normal path and not a
    #: degraded one.
    #:
    #: There used to be a second field, ``can_mint_now``, conjoining this with
    #: "an organisation of this type is already connected". That was the rule
    #: when minting borrowed a separately connected organisation's secret. It
    #: is not the rule now, and as a conjunction it was circular: it answered
    #: false for the *first* provider of a type, which is the case the create
    #: wizard exists to serve. ``AIProvidersService._validate_shape``
    #: is the enforcing authority and reads exactly this one term.
    supports_minting: bool
    #: What an administration credential for this provider needs, when it can
    #: mint. ``None`` when it cannot.
    admin_config_schema: dict | None = None


class ProviderAdaptersPublic(SQLModel):
    data: list[ProviderAdapterPublic]
    count: int
