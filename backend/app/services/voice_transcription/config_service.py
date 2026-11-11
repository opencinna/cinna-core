"""Voice transcription settings: which provider serves the relay, and whether
the relay is offered at all.

The three columns live on ``ServerConfig`` and are edited through
``PUT /admin/server-config``; this module is the one place that decides
whether they add up to a usable relay (``resolve``), validates an update
before it is applied, and builds the discovery block.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlmodel import Session

from app.core.config import settings
from app.models.credentials.provider_admin_credential import AIProvider, AIProviderKind
from app.models.server_config.server_config import ServerConfig, ServerConfigUpdate
from app.models.voice_transcription.usage import VoiceTranscriptionProviderOption
from app.services.credentials.ai_providers_service import ai_providers_service
from app.services.voice_transcription.constants import (
    PROTOCOL_VERSION,
    SPEECH_CAPABLE_PROVIDER_TYPES,
)
from app.services.voice_transcription.providers.registry import get_adapter

STREAM_PATH = "/speech/stream"


class VoiceTranscriptionValidationError(Exception):
    """A voice transcription settings update the server refuses.

    ``reason`` is the stable code the admin UI branches on (400 ``detail``).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ResolvedVoiceConfig:
    """A usable relay configuration. Holds no secret."""

    provider_id: uuid.UUID
    provider_type: str
    # Per-user monthly cap in minutes; ``None`` = no cap.
    cap_minutes: int | None


def _type_value(provider: AIProvider) -> str:
    raw = provider.provider_type
    return str(getattr(raw, "value", raw))


def is_speech_capable(provider: AIProvider) -> bool:
    """Speech-capable = a speech provider type, a ``fixed_key`` provider, and a
    registered adapter (plan D3)."""
    provider_type = _type_value(provider)
    return (
        provider_type in SPEECH_CAPABLE_PROVIDER_TYPES
        and provider.kind == AIProviderKind.FIXED_KEY.value
        and get_adapter(provider_type) is not None
    )


def _config_row(session: Session) -> ServerConfig:
    # Local import: the server-config service imports this module for validation.
    from app.services.server_config.server_config_service import ServerConfigService

    return ServerConfigService.get_or_create(session)


class VoiceTranscriptionConfigService:
    @staticmethod
    def resolve(session: Session) -> ResolvedVoiceConfig | None:
        """The relay configuration, or ``None`` while it is off or unusable
        (no provider, provider deleted, not speech-capable). Never decrypts."""
        config = _config_row(session)
        if not config.voice_transcription_enabled:
            return None
        provider = ai_providers_service.load(session, config.voice_transcription_provider_id)
        if provider is None or not is_speech_capable(provider):
            return None
        return ResolvedVoiceConfig(
            provider_id=provider.id,
            provider_type=_type_value(provider),
            cap_minutes=config.voice_transcription_monthly_minutes_per_user,
        )

    @staticmethod
    def cap_minutes(session: Session) -> int | None:
        """The configured monthly cap, whether or not the relay is usable."""
        return _config_row(session).voice_transcription_monthly_minutes_per_user

    @staticmethod
    def load_api_key(session: Session, provider_id: uuid.UUID) -> str | None:
        """The provider key for one provider call. ``None`` when it cannot be had."""
        return ai_providers_service.fixed_key_api_key(session, provider_id)

    @staticmethod
    def speech_provider(session: Session, provider_id: uuid.UUID | None) -> AIProvider | None:
        """A speech-capable provider by id, or ``None``."""
        provider = ai_providers_service.load(session, provider_id)
        if provider is None or not is_speech_capable(provider):
            return None
        return provider

    @staticmethod
    def validate_update(
        session: Session, config: ServerConfig, data: ServerConfigUpdate
    ) -> None:
        """Refuse an update that would name an unusable provider, or switch the
        relay on with none. A provider that became unusable *after* it was
        chosen does not block unrelated saves; ``resolve`` treats it as off."""
        fields = data.model_fields_set
        if "voice_transcription_provider_id" in fields:
            provider_id = data.voice_transcription_provider_id
        else:
            provider_id = config.voice_transcription_provider_id
        if data.voice_transcription_enabled is not None:
            enabled = data.voice_transcription_enabled
        else:
            enabled = config.voice_transcription_enabled

        if (
            "voice_transcription_provider_id" in fields
            and data.voice_transcription_provider_id is not None
        ):
            provider = ai_providers_service.load(session, data.voice_transcription_provider_id)
            if provider is None:
                raise VoiceTranscriptionValidationError(
                    "voice_transcription_provider_not_found"
                )
            if not is_speech_capable(provider):
                raise VoiceTranscriptionValidationError(
                    "voice_transcription_provider_not_speech_capable"
                )
        if enabled and provider_id is None:
            raise VoiceTranscriptionValidationError("voice_transcription_provider_required")

    @staticmethod
    def list_provider_options(session: Session) -> list[VoiceTranscriptionProviderOption]:
        """The AI providers an admin may pick for the relay."""
        return [
            VoiceTranscriptionProviderOption(
                id=provider.id, name=provider.name, type=_type_value(provider)
            )
            for provider in ai_providers_service.list_records(session)
            if is_speech_capable(provider)
        ]

    @staticmethod
    def stream_url() -> str:
        """``ws(s)://<backend>/api/v1/speech/stream``."""
        base = settings.backend_base_url
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return f"{base}{settings.API_V1_STR}{STREAM_PATH}"

    @staticmethod
    def discovery_block(session: Session) -> dict | None:
        """The ``voice_transcription`` discovery block, present only while the
        relay is on with a usable provider, i.e. one whose key loads."""
        resolved = VoiceTranscriptionConfigService.resolve(session)
        if resolved is None:
            return None
        if VoiceTranscriptionConfigService.load_api_key(session, resolved.provider_id) is None:
            return None
        return {
            "version": PROTOCOL_VERSION,
            "url": VoiceTranscriptionConfigService.stream_url(),
        }
