"""
SDK Constants — shared between environment_service.py and environment_lifecycle.py.

This module breaks the circular import between the two modules by providing
a single source of truth for SDK-related mappings and validation helpers.
"""
from app.models.credentials.ai_credential import AICredentialType


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

# Credential type to credential bag key mapping
CREDENTIAL_TYPE_TO_BAG_KEY: dict[AICredentialType, str] = {
    AICredentialType.ANTHROPIC: "anthropic_api_key",
    AICredentialType.MINIMAX: "minimax_api_key",
    AICredentialType.OPENAI_COMPATIBLE: "openai_compatible_api_key",
    AICredentialType.OPENAI: "openai_api_key",
    AICredentialType.GOOGLE: "google_api_key",
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


def make_empty_credential_bag() -> dict[str, str | None]:
    """Create a fresh credential bag with all keys set to None."""
    return {
        "anthropic_api_key": None,
        "minimax_api_key": None,
        "openai_compatible_api_key": None,
        "openai_compatible_base_url": None,
        "openai_compatible_model": None,
        "openai_api_key": None,
        "google_api_key": None,
    }


def apply_credential_to_bag(
    bag: dict[str, str | None],
    cred_type: AICredentialType,
    cred_data,
) -> None:
    """
    Apply decrypted credential data into the credential bag based on type.

    Args:
        bag: Credential bag dict (mutated in place).
        cred_type: The AICredentialType of the credential.
        cred_data: Decrypted credential data (AICredentialData or similar with
                   api_key, base_url, model attributes).
    """
    if cred_type == AICredentialType.ANTHROPIC:
        bag["anthropic_api_key"] = cred_data.api_key
    elif cred_type == AICredentialType.MINIMAX:
        bag["minimax_api_key"] = cred_data.api_key
    elif cred_type == AICredentialType.OPENAI_COMPATIBLE:
        bag["openai_compatible_api_key"] = cred_data.api_key
        bag["openai_compatible_base_url"] = cred_data.base_url
        bag["openai_compatible_model"] = cred_data.model
    elif cred_type == AICredentialType.OPENAI:
        bag["openai_api_key"] = cred_data.api_key
    elif cred_type == AICredentialType.GOOGLE:
        bag["google_api_key"] = cred_data.api_key
