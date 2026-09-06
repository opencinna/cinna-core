"""The provider adapter registry.

One lookup replaces the per-provider ``if/elif`` chains and five-entry tables
that used to live in six unrelated modules.

**The test seam is here, deliberately.** Provider I/O must be replaceable
without ``unittest.mock.patch`` on a module attribute: a patched module
attribute stops intercepting the moment the call site is rewritten to reach the
provider by another route, and it does so silently — the patch target still
resolves, the test still passes, and the suite starts making real HTTPS calls.
:func:`override_for_tests` swaps what the registry *returns*, so any call that
goes through the registry is intercepted no matter which module makes it, and a
call that does not go through the registry fails loudly against a stub that
counts its invocations.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.models.credentials.ai_credential import AICredentialType
from app.services.ai_providers.anthropic import AnthropicAdapter
from app.services.ai_providers.base import AIProviderAdapter
from app.services.ai_providers.google import GoogleAdapter
from app.services.ai_providers.minimax import MiniMaxAdapter
from app.services.ai_providers.openai import OpenAIAdapter
from app.services.ai_providers.openai_compatible import OpenAICompatibleAdapter


class UnknownProviderError(LookupError):
    """No adapter serves the requested credential type."""


# The one dictionary keyed on AICredentialType members in the codebase. It is
# inside this package on purpose — see ``base`` for the property and
# ``tests/architecture/provider_adapter_registry_test.py`` for its enforcement.
# Declaration order follows the enum so derived orderings (the credential bag's
# key order, for one) stay stable.
_ADAPTERS: dict[AICredentialType, AIProviderAdapter] = {
    AICredentialType.ANTHROPIC: AnthropicAdapter(),
    AICredentialType.MINIMAX: MiniMaxAdapter(),
    AICredentialType.OPENAI_COMPATIBLE: OpenAICompatibleAdapter(),
    AICredentialType.OPENAI: OpenAIAdapter(),
    AICredentialType.GOOGLE: GoogleAdapter(),
}

# Populated only by ``override_for_tests``. A module-level dict rather than a
# ContextVar because the test client runs the app on a different thread from the
# test function, and a ContextVar set in the test would not be visible there.
_overrides: dict[AICredentialType, AIProviderAdapter] = {}


def _coerce(cred_type: AICredentialType | str | None) -> AICredentialType | None:
    """Best-effort coercion of a credential ``type`` to the enum.

    Rows load their ``type`` as either the enum or a plain string depending on
    the path, and callers pass raw request values. ``None`` means "no such
    provider", which callers translate into their own not-supported answer
    rather than an exception.
    """
    if isinstance(cred_type, AICredentialType):
        return cred_type
    if cred_type is None:
        return None
    try:
        return AICredentialType(cred_type)
    except ValueError:
        return None


def find_adapter(
    cred_type: AICredentialType | str | None,
) -> AIProviderAdapter | None:
    """The adapter for ``cred_type``, or ``None`` when nothing serves it.

    Use this where an unknown type is a normal outcome (probing a credential
    whose ``type`` column holds something the enum no longer has). Use
    :func:`get_adapter` where it is a bug.
    """
    resolved = _coerce(cred_type)
    if resolved is None:
        return None
    override = _overrides.get(resolved)
    if override is not None:
        return override
    return _ADAPTERS.get(resolved)


def get_adapter(cred_type: AICredentialType | str) -> AIProviderAdapter:
    """The adapter for ``cred_type``. Raises when nothing serves it."""
    adapter = find_adapter(cred_type)
    if adapter is None:
        raise UnknownProviderError(
            f"No AI provider adapter for credential type {cred_type!r}"
        )
    return adapter


def all_adapters() -> list[AIProviderAdapter]:
    """Every adapter, in enum declaration order, overrides applied."""
    return [
        _overrides.get(cred_type, adapter)
        for cred_type, adapter in _ADAPTERS.items()
    ]


def supported_types() -> list[AICredentialType]:
    """Every credential type the registry serves, in enum declaration order."""
    return list(_ADAPTERS)


@contextmanager
def override_for_tests(
    *adapters: AIProviderAdapter,
    for_all: AIProviderAdapter | None = None,
) -> Iterator[None]:
    """Replace registry entries for the duration of the block.

    ``override_for_tests(stub)`` replaces the entry for ``stub.type``;
    ``override_for_tests(for_all=stub)`` replaces every entry with one object.

    Prefer one stub per provider (``tests/utils/ai_provider.stub_all_providers``
    builds them by wrapping the real adapters). ``for_all`` hands the same object
    to every type, which also replaces every per-provider *fact* — the bag slots,
    the SDK engine, the display name — and that reshapes anything reading them.

    Restores the previous mapping on exit, including on exception, so a failing
    test cannot leak a stub into the next one.

    Raises :class:`UnknownProviderError` for an adapter whose ``type`` resolves
    to no provider: such an entry could never be looked up, so the override would
    silently be a no-op and the test would reach the real provider.
    """
    previous = dict(_overrides)
    try:
        if for_all is not None:
            for cred_type in _ADAPTERS:
                _overrides[cred_type] = for_all
        for adapter in adapters:
            resolved = _coerce(adapter.type)
            if resolved is None:
                raise UnknownProviderError(
                    f"Cannot override provider type {adapter.type!r}: it resolves "
                    f"to no AICredentialType, so the override would never be used."
                )
            _overrides[resolved] = adapter
        yield
    finally:
        _overrides.clear()
        _overrides.update(previous)
