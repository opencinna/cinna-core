"""Speech adapter registry, keyed by ``AIProvider.provider_type``."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from app.services.voice_transcription.providers.base import SpeechAdapter
from app.services.voice_transcription.providers.gemini_live import GeminiLiveAdapter

_ADAPTERS: dict[str, SpeechAdapter] = {
    GeminiLiveAdapter.provider_type: GeminiLiveAdapter(),
}


def get_adapter(provider_type: str | None) -> SpeechAdapter | None:
    """The adapter for a provider type, or ``None`` when it has no speech support."""
    if provider_type is None:
        return None
    return _ADAPTERS.get(provider_type)


@contextmanager
def override_for_tests(provider_type: str, adapter: SpeechAdapter) -> Iterator[None]:
    """Replace the adapter for ``provider_type`` for the duration of the block.

    Restores the previous entry (or its absence) on exit, including on
    exception, so a failing test cannot leak a fake into the next one.
    """
    missing = object()
    previous = _ADAPTERS.get(provider_type, missing)
    _ADAPTERS[provider_type] = adapter
    try:
        yield
    finally:
        if previous is missing:
            _ADAPTERS.pop(provider_type, None)
        else:
            _ADAPTERS[provider_type] = previous  # type: ignore[assignment]
