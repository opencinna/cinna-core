"""
AI provider adapters — the contract.

One module per :class:`AICredentialType` answers every "what does this provider
do" question in one place, so adding a sixth provider is one new module plus one
registry entry rather than an edit in every module that happened to branch on
the type.

**The property this package exists to enforce** is stated once, here, and made
executable by ``backend/tests/architecture/provider_adapter_registry_test.py``:

    No dictionary keyed on :class:`AICredentialType` members may be declared
    outside ``app/services/ai_providers/``.

A per-provider *table* is what forces an edit in N files when a provider is
added. A scattered ``cred.type == ANTHROPIC`` inside a feature is provider-
specific logic that legitimately lives with its feature and is deliberately not
forbidden. The allowlist for that test is empty and is meant to stay empty.

Nothing in this package may import a service that imports it back. In practice
that means: models and stdlib and HTTP clients only. :class:`ProbeResult` and
the skip/error reason codes live here rather than in
``model_discovery_service`` for exactly that reason — that module imports
``ai_credentials_service``, and an adapter reaching back for ``ProbeResult``
would close the cycle. ``model_discovery_service`` re-exports them so the
existing import paths keep working.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.models.credentials.ai_credential import AICredentialType

# Network timeout for the native /models calls (cheap GETs).
HTTP_TIMEOUT_SECONDS = 20.0

# ── Probe reason codes ─────────────────────────────────────────────────────
#
# A "skip" means the credential is valid/usable but model listing is not
# applicable; ``invalid_key`` is a real auth rejection.

# Recorded when an Anthropic OAuth token is encountered (the token cannot call
# the models endpoint, but the credential is fine).
OAUTH_TOKEN_UNSUPPORTED = "oauth_token_unsupported"
# The provider publishes no model-list endpoint (catalog-only).
NO_LIST_ENDPOINT = "no_list_endpoint"
# An OpenAI-compatible credential with no base URL — nowhere to ask.
NO_BASE_URL = "no_base_url"
# The credential's ``type`` resolves to no adapter at all.
UNSUPPORTED_TYPE = "unsupported_type"

SKIP_REASONS = frozenset(
    {OAUTH_TOKEN_UNSUPPORTED, NO_LIST_ENDPOINT, NO_BASE_URL, UNSUPPORTED_TYPE}
)
ERROR_INVALID_KEY = "invalid_key"


@dataclass
class ProbeResult:
    """Outcome of probing a provider's model list with a raw (decrypted) key.

    DB-free — usable both by the cron (which then persists onto the credential
    row) and by the synchronous "Test Connection" endpoint (which may not have
    a row yet).

    - ``ok``        — the probe completed without a hard auth failure. True for
                      both a successful listing AND a benign skip (OAuth /
                      minimax / openai_compatible-without-base-url). False only
                      on a real auth rejection (``invalid_key``).
    - ``models``    — discovered model ids (empty on skip).
    - ``reason``    — a coarse skip/error code when applicable (one of
                      ``SKIP_REASONS`` or ``invalid_key``), else ``None``.
                      ``None`` reason with ``ok`` and a non-empty list means a
                      clean successful listing.
    """

    ok: bool
    models: list[str]
    reason: str | None = None

    @property
    def is_skip(self) -> bool:
        return self.ok and self.reason in SKIP_REASONS


@dataclass(frozen=True)
class KeyClassification:
    """How a provider reads one of its own key strings.

    ``is_oauth_token`` is the field every caller may rely on: True when the key
    is a delegated OAuth token rather than an API key. Providers that issue only
    API keys always answer False, so a caller does **not** need to check the
    credential's type before asking — which is the point, since asking without
    the type guard is what removes the duplicated ``startswith("sk-ant-oat")``
    tests from five unrelated modules.

    ``env_var_name`` is ``None`` for every provider that does not route its key
    into a *different* container environment variable depending on the key kind.
    Only Anthropic does (``ANTHROPIC_API_KEY`` vs ``CLAUDE_CODE_OAUTH_TOKEN``),
    and it is the sole consumer of the field.

    ``label`` is the human description the Anthropic detection helper has always
    returned ("OAuth Token" / "API Key" / "API Key (Empty)" / "API Key (Unknown
    Format)"). It is log/diagnostic text, never a policy input.
    """

    is_oauth_token: bool
    env_var_name: str | None
    label: str


# ── Half B slot ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MintedKey:
    """A key the provider just created, plus the handles needed to revoke it."""

    api_key: str
    external_ref: dict


@dataclass(frozen=True)
class ProvisionScope:
    """Who and what a mint is for."""

    user_id: object
    parent_id: object
    config: dict


@dataclass(frozen=True)
class SpendLimitStatus:
    """What a provider says about one project's spend cap.

    ``is_capped`` is **the** predicate, defined here and nowhere else. Every
    caller asks this object; none re-derives the rule from the fields, and the
    docs describe this implementation rather than restating the rule beside it.

    **The cap is the limit's existence, not its ``enforcement.status``.** This
    predicate used to read ``status == "enforcing"``, which inverted the gate:
    the provider's own generated types document that object as *"Represents a
    hard spend limit configured at the project level"* and its status as
    *"Whether the hard spend limit is **currently** enforcing"* — a runtime
    state, not a configuration. A correctly capped project sitting at $0 of $100
    reports ``inactive``; it reports ``enforcing`` only once it has hit the
    threshold and is already 429-ing with ``project_spend_limit_exceeded``. So
    the old rule refused every healthy project and would have admitted only an
    exhausted one, where a minted key is dead on arrival. It could never pass in
    the state it was written to allow.

    Monitoring without enforcement is not this object: that is a project *spend
    alert*, a separate endpoint with a ``notification_channel``, which this code
    never reads. If a ``project.spend_limit`` came back, a hard limit is
    configured.

    ``threshold_cents`` is **integer cents**, matching the provider's own
    contract. The unit is in the name at every layer because an off-by-100 here
    is a hundred-fold cap.
    """

    #: ``enforcing`` | ``inactive`` | ``absent``. An explicit value in every
    #: case, including "the project has no limit at all" — never ``None``
    #: standing in for a state. ``absent`` is ours, synthesized from the
    #: provider's 404; the other two are the provider's own runtime states and
    #: are equally capped.
    enforcement_status: str
    threshold_cents: int | None = None
    currency: str | None = None
    interval: str | None = None

    @property
    def is_capped(self) -> bool:
        # A threshold is still required alongside existence: a limit object with
        # no amount caps nothing, and a zero would read as falsy here for the
        # same reason it would be meaningless there.
        return self.enforcement_status != "absent" and bool(self.threshold_cents)


class ProviderAdminError(Exception):
    """A provider administration call failed, reduced to a coarse code.

    ``code`` is what gets stored and audited (``invalid_admin_secret``,
    ``project_not_found``, ``spend_limit_exceeded``, ``rate_limited``,
    ``provider_error``, ``no_secret_returned``). The provider's own response body
    is never stored: it is unbounded, occasionally echoes request material, and a
    reason code is what an operator can actually act on.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@runtime_checkable
class KeyProvisioner(Protocol):
    """Optional capability: this provider can mint a per-user key by API.

    Anthropic's is permanently ``None``: its administration API lists and updates
    keys but cannot create them, in the API or in any SDK. That is why keys added
    by hand are a first-class path rather than a fallback.

    Every method raises :class:`ProviderAdminError` for a provider-side failure,
    so callers branch on a code rather than on an HTTP status they would have to
    re-interpret.
    """

    async def mint(
        self, secret: str, *, label: str, scope: ProvisionScope
    ) -> MintedKey: ...

    async def revoke(self, secret: str, external_ref: dict) -> None: ...

    async def verify_admin_access(self, secret: str, config: dict) -> str: ...

    async def verify_spend_limit(
        self, secret: str, config: dict
    ) -> SpendLimitStatus: ...

    def config_schema(self) -> dict: ...


# ── The adapter contract ───────────────────────────────────────────────────


@runtime_checkable
class AIProviderAdapter(Protocol):
    """Everything the rest of the backend needs to know about one provider.

    This is a :class:`~typing.Protocol` rather than a base class so a test can
    substitute a plain object through
    :func:`app.services.ai_providers.registry.override_for_tests` without
    inheriting anything. The five shipped adapters extend
    :class:`BaseProviderAdapter`, which supplies the defaults.
    """

    type: AICredentialType
    label: str
    account_config_display_name: str
    account_config_slug: str
    sdk_engine: str
    bag_key_api_key: str
    bag_key_base_url: str | None
    bag_key_model: str | None
    requires_base_url: bool
    requires_model: bool
    supports_model_listing: bool
    issues_oauth_tokens: bool
    key_provisioner: KeyProvisioner | None

    @property
    def bag_keys(self) -> tuple[str, ...]: ...

    @property
    def catalog_engine_provider(self) -> tuple[str, str]: ...

    @property
    def supports_minting(self) -> bool: ...

    def classify_key(self, api_key: str | None) -> KeyClassification: ...

    def apply_to_bag(self, bag: dict[str, str | None], data) -> None: ...

    async def list_models(
        self, api_key: str, base_url: str | None = None
    ) -> ProbeResult: ...


class BaseProviderAdapter:
    """Shared implementation for the five shipped adapters.

    Subclasses declare data (``type``, ``label``, the bag slots, ``sdk_engine``)
    and override only the behaviour that is genuinely provider-specific.
    """

    #: The credential type this adapter serves.
    type: AICredentialType
    #: Human name used in validation messages and admin surfaces.
    label: str
    #: Name the native/desktop account-config bundle shows. The empty string is
    #: a deliberate sentinel meaning "use the credential's own free-form name",
    #: which is how ``openai_compatible`` — a shape, not a vendor — is labelled.
    account_config_display_name: str = ""
    #: Stable slug the native client keys its provider descriptor on.
    account_config_slug: str
    #: ``<engine>/<provider>`` composed for this type by the AddEnvironment SDK
    #: picker: claude-code for anthropic/minimax, opencode/<provider> for the
    #: OpenCode-only providers. The single declaration of that mapping.
    sdk_engine: str

    #: Credential-bag slot names. ``base_url`` / ``model`` are ``None`` for the
    #: providers that carry neither. Declared once so the empty-bag factory and
    #: the apply step cannot drift apart.
    bag_key_api_key: str
    bag_key_base_url: str | None = None
    bag_key_model: str | None = None

    #: Server-side "which fields does this provider require" — the authority
    #: behind the 400s in ``_validate_credential_data``.
    requires_base_url: bool = False
    requires_model: bool = False

    #: Whether the provider publishes a model-list endpoint at all.
    supports_model_listing: bool = True

    #: Whether this provider issues delegated OAuth tokens as well as API keys —
    #: i.e. whether :meth:`classify_key` can ever answer ``is_oauth_token=True``.
    #:
    #: ``classify_key`` is safe to call for any provider, so most callers need not
    #: consult this. It exists for the callers that must **decrypt a key** in
    #: order to classify it: for a provider that issues only API keys the answer
    #: is known in advance, and the decrypt is pure cost. Those call sites used to
    #: spell this ``cred.type == ANTHROPIC``.
    issues_oauth_tokens: bool = False

    #: The provider's key-minting capability, or ``None`` when it has none.
    #:
    #: **Today: only ``OpenAIAdapter`` declares one.** The other four are
    #: ``None``, and the two kinds of ``None`` here mean completely different
    #: things — read this before adding an adapter:
    #:
    #: - **Anthropic's ``None`` is permanent, and it is a fact about the
    #:   provider, not about us.** Its administration API can list and update
    #:   API keys and cannot create them, in the API and in every SDK. No amount
    #:   of work on our side changes that, which is why keys added by hand are a
    #:   first-class path here rather than a fallback, and why nothing should
    #:   ever be written that reads as "minting is coming for Anthropic".
    #: - **MiniMax, Google and openai_compatible are ``None`` because nobody has
    #:   built one.** That is an absence of work, not a provider limitation, and
    #:   a future pass may fill any of them in.
    #:
    #: :attr:`supports_minting` is the predicate every caller asks; nothing reads
    #: this field to decide anything.
    key_provisioner: KeyProvisioner | None = None

    @property
    def bag_keys(self) -> tuple[str, ...]:
        """The bag slots this provider fills, in declaration order."""
        keys = [self.bag_key_api_key]
        if self.bag_key_base_url:
            keys.append(self.bag_key_base_url)
        if self.bag_key_model:
            keys.append(self.bag_key_model)
        return tuple(keys)

    @property
    def catalog_engine_provider(self) -> tuple[str, str]:
        """``sdk_engine`` split into the model catalog's ``(engine, provider)``.

        Derived rather than declared: these were two separate five-entry tables
        in two modules that always held the same mapping in two encodings.
        """
        engine, _, provider = self.sdk_engine.partition("/")
        return engine, provider

    @property
    def supports_minting(self) -> bool:
        return self.key_provisioner is not None

    def classify_key(self, api_key: str | None) -> KeyClassification:
        """Providers that issue only API keys never see an OAuth token."""
        return KeyClassification(
            is_oauth_token=False, env_var_name=None, label="API Key"
        )

    def apply_to_bag(self, bag: dict[str, str | None], data) -> None:
        """Write decrypted credential data into the environment credential bag.

        ``data`` is duck-typed on ``AICredentialData`` (``api_key``,
        ``base_url``, ``model``). Only the slots this adapter declares are
        touched, so a provider cannot accidentally clobber another's.
        """
        bag[self.bag_key_api_key] = data.api_key
        if self.bag_key_base_url:
            bag[self.bag_key_base_url] = data.base_url
        if self.bag_key_model:
            bag[self.bag_key_model] = data.model

    async def list_models(
        self, api_key: str, base_url: str | None = None
    ) -> ProbeResult:
        """Probe the provider's native model list with a raw (decrypted) key.

        Pure I/O — no DB access, never logs the key. Blocking HTTP must run via
        ``anyio.to_thread``. A 401/403 maps to a non-ok ``invalid_key`` result;
        other HTTP/transport errors propagate to the caller, which records the
        exception class name.
        """
        raise NotImplementedError
