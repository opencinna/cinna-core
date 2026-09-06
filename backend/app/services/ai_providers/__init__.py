"""AI provider adapters — one module per provider, behind one registry.

See :mod:`app.services.ai_providers.base` for the contract and the architectural
property this package exists to enforce, and
:mod:`app.services.ai_providers.registry` for the lookup and the test seam.
"""
from app.services.ai_providers.base import (
    ERROR_INVALID_KEY,
    NO_BASE_URL,
    NO_LIST_ENDPOINT,
    OAUTH_TOKEN_UNSUPPORTED,
    SKIP_REASONS,
    UNSUPPORTED_TYPE,
    AIProviderAdapter,
    BaseProviderAdapter,
    KeyClassification,
    KeyProvisioner,
    MintedKey,
    ProbeResult,
    ProviderAdminError,
    ProvisionScope,
    SpendLimitStatus,
)
from app.services.ai_providers.registry import (
    UnknownProviderError,
    all_adapters,
    find_adapter,
    get_adapter,
    override_for_tests,
    supported_types,
)

__all__ = [
    "AIProviderAdapter",
    "BaseProviderAdapter",
    "ERROR_INVALID_KEY",
    "KeyClassification",
    "KeyProvisioner",
    "MintedKey",
    "NO_BASE_URL",
    "NO_LIST_ENDPOINT",
    "OAUTH_TOKEN_UNSUPPORTED",
    "ProbeResult",
    "ProviderAdminError",
    "ProvisionScope",
    "SpendLimitStatus",
    "SKIP_REASONS",
    "UNSUPPORTED_TYPE",
    "UnknownProviderError",
    "all_adapters",
    "find_adapter",
    "get_adapter",
    "override_for_tests",
    "supported_types",
]
