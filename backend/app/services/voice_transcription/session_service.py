"""Entry points for the relay: serve a WebSocket session, probe a provider."""
from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import WebSocket
from sqlmodel import Session

from app.models.voice_transcription.usage import VoiceTranscriptionTestResult
from app.services.voice_transcription.config_service import (
    VoiceTranscriptionConfigService,
)
from app.services.voice_transcription.providers.registry import get_adapter
from app.services.voice_transcription.session import VoiceSession


class VoiceTranscriptionSessionService:
    @staticmethod
    async def run(
        websocket: WebSocket,
        *,
        user_id: uuid.UUID,
        client_kind: str | None,
        recheck: Callable[[], bool],
    ) -> None:
        """Serve one ``/speech/stream`` socket until its terminal event."""
        await VoiceSession(
            websocket, user_id=user_id, client_kind=client_kind, recheck=recheck
        ).run()

    @staticmethod
    async def test_provider(
        session: Session, provider_id: uuid.UUID | None
    ) -> VoiceTranscriptionTestResult:
        """Admin "Test": a short silent session against ``provider_id`` (or the
        configured provider). Reports a category only, never provider text."""
        if provider_id is None:
            from app.services.server_config.server_config_service import (
                ServerConfigService,
            )

            provider_id = ServerConfigService.get_or_create(session).voice_transcription_provider_id
        provider = VoiceTranscriptionConfigService.speech_provider(session, provider_id)
        if provider is None:
            return VoiceTranscriptionTestResult(ok=False, error="disabled")
        provider_type = str(getattr(provider.provider_type, "value", provider.provider_type))
        adapter = get_adapter(provider_type)
        if adapter is None:
            return VoiceTranscriptionTestResult(ok=False, error="disabled")
        api_key = VoiceTranscriptionConfigService.load_api_key(session, provider.id)
        if api_key is None:
            return VoiceTranscriptionTestResult(ok=False, error="invalid_key")
        try:
            outcome = await adapter.probe(api_key=api_key)
        except Exception:  # noqa: BLE001
            outcome = "unreachable"
        finally:
            api_key = None
        if outcome == "ok":
            return VoiceTranscriptionTestResult(ok=True)
        return VoiceTranscriptionTestResult(ok=False, error=outcome)
