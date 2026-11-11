"""``voice_transcription.updated`` broadcast."""
from __future__ import annotations

import logging

from app.models.events.event import EventType
from app.services.events.event_service import event_service

logger = logging.getLogger(__name__)


class VoiceTranscriptionEvents:
    @staticmethod
    async def updated(available: bool) -> None:
        """Tell every connected client whether the relay is offered now.
        Never raises: the change it reports has already committed."""
        try:
            await event_service.emit_event(
                event_type=EventType.VOICE_TRANSCRIPTION_UPDATED,
                meta={"available": available},
            )
        except Exception:
            logger.exception("Failed to emit voice_transcription.updated")
