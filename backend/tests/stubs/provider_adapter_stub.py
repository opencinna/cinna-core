"""Recording provider-adapter stub for the registry override seam.

WHY THIS EXISTS
---------------
Provider I/O used to be intercepted by patching one module attribute
(``model_discovery_service.probe_models``). That works only while the dispatch
happens to be a module global in the module the patch names, and it fails
**quietly** when it stops being one: ``unittest.mock.patch`` still resolves the
target, the tests still run, and the ones whose assertions do not depend on the
stub's specific return value keep passing — while making real HTTPS calls to the
providers from CI with a fake key.

So the seam is the registry (``ai_providers.registry.override_for_tests``): the
adapter that any call site resolves is replaced, whichever module makes the
call. That closes the "call went around the patch" hole but not the "call never
happened" hole, which is why this stub **counts its invocations**. A fixture that
is merely present proves nothing; the call-count assertion is what makes it
honest.

WHY IT WRAPS RATHER THAN REPLACES
---------------------------------
An adapter is not only an I/O boundary — it is also where every per-provider
*fact* now lives: the credential-bag slot names, the SDK engine string, the
account-config display name, the required fields. ``make_empty_credential_bag``
and ``apply_credential_to_bag`` read those at call time. A stub that substituted
its own data for all five providers would therefore reshape the credential bag
inside the block: four of the seven provider slots would disappear, and an
OpenAI key would be written **silently** into the Anthropic slot. Nothing would
raise; the test would just be measuring a different system.

So each stub copies its real adapter's data and delegates ``classify_key``,
overriding only ``list_models``. Every provider fact stays true; only the network
call is intercepted.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.ai_providers.base import (
    AIProviderAdapter,
    BaseProviderAdapter,
    KeyClassification,
    ProbeResult,
)

# The data half of the adapter contract — copied verbatim from the real adapter
# so a stubbed registry describes the same providers the real one does.
_DATA_ATTRS = (
    "type",
    "label",
    "account_config_display_name",
    "account_config_slug",
    "sdk_engine",
    "bag_key_api_key",
    "bag_key_base_url",
    "bag_key_model",
    "requires_base_url",
    "requires_model",
    "supports_model_listing",
    "issues_oauth_tokens",
)


@dataclass
class ProbeCall:
    """One recorded ``list_models`` invocation.

    ``provider`` is the credential type of the adapter that was resolved, which
    is meaningful precisely because each stub wraps one real adapter — a single
    stub registered for every type could not know which slot it came through.
    """

    provider: object
    api_key: str
    base_url: str | None


class ProviderProbeRecorder:
    """The shared call log across a set of stubs, plus its assertions."""

    def __init__(self) -> None:
        self.calls: list[ProbeCall] = []

    @property
    def probe_count(self) -> int:
        return len(self.calls)

    @property
    def probed_providers(self) -> list[object]:
        return [call.provider for call in self.calls]

    def assert_probed(self, times: int = 1) -> None:
        """A provider was reached exactly ``times``. Fails loudly if it was not.

        A test that reached the real provider instead would land here with a
        count of zero — which is the whole point of counting.
        """
        assert self.probe_count == times, (
            f"expected the stubbed provider adapters to be probed {times} time(s), "
            f"got {self.probe_count}. A count of 0 means the call went around the "
            f"registry — i.e. straight to the real provider."
        )

    def assert_not_probed(self) -> None:
        """No provider was reached — the request was refused before any I/O."""
        assert self.probe_count == 0, (
            f"expected no provider probe, got {self.probe_count}: {self.calls}"
        )


class RecordingProviderAdapter(BaseProviderAdapter):
    """One real adapter with its ``list_models`` replaced by a canned result."""

    def __init__(
        self,
        real: AIProviderAdapter,
        result: ProbeResult,
        recorder: ProviderProbeRecorder,
        key_provisioner: object | None = None,
    ) -> None:
        self._real = real
        for attr in _DATA_ATTRS:
            setattr(self, attr, getattr(real, attr))
        # Minting is the one capability a stub must not *inherit*: the real
        # adapter's provisioner is a live provider client, so it is dropped
        # unless the caller passes a stubbed one in. Passing one deliberately is
        # how the minting tests keep ``supports_minting`` true without keeping
        # the network with it.
        self.key_provisioner = key_provisioner
        self.result = result
        self.recorder = recorder

    def classify_key(self, api_key: str | None) -> KeyClassification:
        """Delegated — key classification is local, not network, and must stay true."""
        return self._real.classify_key(api_key)

    async def list_models(
        self, api_key: str, base_url: str | None = None
    ) -> ProbeResult:
        self.recorder.calls.append(
            ProbeCall(provider=self.type, api_key=api_key, base_url=base_url)
        )
        return self.result
