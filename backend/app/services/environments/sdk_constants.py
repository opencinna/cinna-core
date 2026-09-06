"""
SDK Constants — shared between environment_service.py and environment_lifecycle.py.

This module breaks the circular import between the two modules by providing
a single source of truth for SDK-related mappings and validation helpers.
"""
from app.models.credentials.ai_credential import AICredentialType
from app.services.ai_providers import registry


# SDK Provider Constants (full SDK IDs for legacy/backward compat)
SDK_ANTHROPIC = "claude-code/anthropic"
SDK_MINIMAX = "claude-code/minimax"
DEFAULT_SDK = SDK_ANTHROPIC
VALID_SDK_OPTIONS = [SDK_ANTHROPIC, SDK_MINIMAX]

# SDK Engine constants (engine prefix only, without provider suffix)
SDK_ENGINE_CLAUDE_CODE = "claude-code"
SDK_ENGINE_OPENCODE = "opencode"
VALID_SDK_ENGINES = [SDK_ENGINE_CLAUDE_CODE, SDK_ENGINE_OPENCODE]

# SDK to AICredentialType mapping — single source of truth
SDK_TO_CREDENTIAL_TYPE: dict[str, AICredentialType] = {
    SDK_ANTHROPIC: AICredentialType.ANTHROPIC,
    SDK_MINIMAX: AICredentialType.MINIMAX,
    # OpenCode variants
    "opencode/anthropic": AICredentialType.ANTHROPIC,
    "opencode/openai": AICredentialType.OPENAI,
    "opencode/openai_compatible": AICredentialType.OPENAI_COMPATIBLE,
    "opencode/google": AICredentialType.GOOGLE,
    "opencode": AICredentialType.ANTHROPIC,  # default provider
}

# SDK ↔ Credential type compatibility matrix
# Maps SDK engine prefix to list of compatible AICredentialType values
SDK_CREDENTIAL_COMPATIBILITY: dict[str, list[str]] = {
    "claude-code": ["anthropic", "minimax"],
    "opencode": ["anthropic", "openai", "openai_compatible", "google"],
}

# Credential type → the bag slot holding that provider's API key.
#
# DERIVED from the provider adapters, not declared here. It used to be a
# five-entry table stated independently of ``apply_credential_to_bag`` below,
# which meant this module named the same mapping twice and the two could drift.
CREDENTIAL_TYPE_TO_BAG_KEY: dict[AICredentialType, str] = {
    adapter.type: adapter.bag_key_api_key for adapter in registry.all_adapters()
}


def is_valid_sdk(sdk: str) -> bool:
    """
    Validate an SDK ID string.

    Accepts:
    - Known legacy full IDs (e.g., "claude-code/anthropic")
    - Any engine/provider format where engine is a known engine
    - Bare engine names (e.g., "opencode")
    """
    if sdk in VALID_SDK_OPTIONS:
        return True
    if "/" in sdk:
        engine = sdk.split("/")[0]
        return engine in VALID_SDK_ENGINES
    return sdk in VALID_SDK_ENGINES


def sdk_expected_credential_type(sdk_id: str | None) -> AICredentialType | None:
    """Return the exact credential type a full SDK id requires, or ``None``.

    Strict match keyed by the **full** SDK id (engine + provider suffix), not
    just the engine. ``opencode/anthropic`` expects ``ANTHROPIC``;
    ``opencode/openai`` expects ``OPENAI`` — even though both share the
    ``opencode`` engine. Unknown SDK strings return ``None`` so callers can
    decide whether to skip validation rather than reject silently.
    """
    if not sdk_id:
        return None
    return SDK_TO_CREDENTIAL_TYPE.get(sdk_id)


def is_credential_compatible_with_sdk(
    sdk_id: str | None, cred_type: AICredentialType | str
) -> bool:
    """Strict provider-level compatibility between an SDK id and a credential type.

    Returns ``True`` if the SDK's expected type equals the given credential
    type. Unknown SDK strings (not in :data:`SDK_TO_CREDENTIAL_TYPE`) return
    ``True`` — callers that want strict rejection should pre-validate the SDK
    with :func:`is_valid_sdk`.
    """
    expected = sdk_expected_credential_type(sdk_id)
    if expected is None:
        return True
    expected_value = expected.value if hasattr(expected, "value") else str(expected)
    cred_value = cred_type.value if hasattr(cred_type, "value") else str(cred_type)
    return expected_value == cred_value


# Bag slots that are not a provider's: the per-mode admin-curated default model
# carried alongside the keys (see admin_curated_model_list). Set by the resolver
# sites that know which mode a credential serves; consumed as the resolve_model
# override fallback (env per-mode override → credential default → catalog).
MODE_DEFAULT_MODEL_BAG_KEYS = ("model_default_conversation", "model_default_building")


def make_empty_credential_bag() -> dict[str, str | None]:
    """Create a fresh credential bag with all keys set to None.

    The provider slots come from the adapters that fill them, so a new provider
    cannot ship a bag key that the empty bag does not declare (which would make
    ``bag[key]`` a silent insert on one path and a KeyError on another).
    """
    bag: dict[str, str | None] = {}
    for adapter in registry.all_adapters():
        for key in adapter.bag_keys:
            bag[key] = None
    for key in MODE_DEFAULT_MODEL_BAG_KEYS:
        bag[key] = None
    return bag


def apply_credential_to_bag(
    bag: dict[str, str | None],
    cred_type: AICredentialType,
    cred_data,
) -> None:
    """
    Apply decrypted credential data into the credential bag based on type.

    One registry lookup; which slots a provider fills is the adapter's answer.
    A ``cred_type`` no adapter serves is left alone, exactly as the previous
    if/elif chain's absent else-branch did.

    Args:
        bag: Credential bag dict (mutated in place).
        cred_type: The AICredentialType of the credential.
        cred_data: Decrypted credential data (AICredentialData or similar with
                   api_key, base_url, model attributes).
    """
    adapter = registry.find_adapter(cred_type)
    if adapter is None:
        return
    adapter.apply_to_bag(bag, cred_data)
