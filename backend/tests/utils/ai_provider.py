"""Helpers for replacing AI provider adapters in tests.

The registry override — never ``unittest.mock.patch`` on a module attribute.
See ``tests/stubs/provider_adapter_stub.py`` for why, and for why the stubs wrap
the real adapters rather than replacing them.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.services.ai_providers import registry
from app.services.ai_providers.base import ProbeResult
from tests.stubs.key_provisioner_stub import (
    ProvisionerRecorder,
    StubOpenAIProvisioner,
)
from tests.stubs.provider_adapter_stub import (
    ProviderProbeRecorder,
    RecordingProviderAdapter,
)


@contextmanager
def stub_all_providers(
    result: ProbeResult | None = None,
) -> Iterator[ProviderProbeRecorder]:
    """Intercept **every** provider's model listing, keeping every provider fact.

    One stub per real adapter, all sharing one call log. Yields the recorder, so
    the test can assert how many probes happened and which providers they went
    to.

    Covering every type (rather than only the one the test names) means a request
    that resolves to an unexpected provider is still intercepted instead of
    escaping to the network — while the wrapping keeps the credential bag, SDK
    engines and display names identical to production inside the block.
    """
    outcome = result or ProbeResult(ok=True, models=[], reason=None)
    recorder = ProviderProbeRecorder()
    stubs = [
        RecordingProviderAdapter(real, outcome, recorder)
        for real in registry.all_adapters()
    ]
    with registry.override_for_tests(*stubs):
        yield recorder


def probe_success(models: list[str]) -> ProbeResult:
    """A successful listing."""
    return ProbeResult(ok=True, models=models, reason=None)


def probe_skip(reason: str) -> ProbeResult:
    """A benign skip — the credential is fine, the listing is not applicable."""
    return ProbeResult(ok=True, models=[], reason=reason)


def probe_invalid_key() -> ProbeResult:
    """A real auth rejection."""
    return ProbeResult(ok=False, models=[], reason="invalid_key")


@contextmanager
def stub_minting_providers(
    **provisioner_kwargs,
) -> Iterator[tuple[ProviderProbeRecorder, ProvisionerRecorder]]:
    """Every provider stubbed, with OpenAI's key provisioner stubbed too.

    Wraps the real adapters exactly as :func:`stub_all_providers` does — so every
    provider fact (bag slots, SDK engine, display name, required fields) stays
    true inside the block — and additionally gives the OpenAI adapter a
    :class:`StubOpenAIProvisioner`, which is the *real* provisioner with only its
    HTTP replaced.

    ``provisioner_kwargs`` configure the stubbed provider's behaviour, never our
    own logic: ``enforcement_status="inactive"`` makes the provider report a limit
    it is not enforcing and lets the real refusal fire. ``on_mint=<callable>`` is
    the one exception to "provider behaviour only", and it is a *timing* control
    rather than a behaviour one: it fires inside the create call, which is the
    only place a test can stand in the window where a key exists at the provider
    and nothing in our database names it yet.

    Yields both recorders: probes, and administration calls.
    """
    outcome = ProbeResult(ok=True, models=[], reason=None)
    probes = ProviderProbeRecorder()
    provisioning = ProvisionerRecorder()
    stubs = []
    for real in registry.all_adapters():
        provisioner = (
            StubOpenAIProvisioner(provisioning, **provisioner_kwargs)
            if real.supports_minting
            else None
        )
        stubs.append(
            RecordingProviderAdapter(real, outcome, probes, provisioner)
        )
    with registry.override_for_tests(*stubs):
        yield probes, provisioning
