"""Voice transcription relay API.

* ``WS /speech/stream`` — the relay (protocol v1); Bearer header auth at the
  upgrade, 1008 before accept on failure.
* ``GET /speech/usage`` — the caller's month.
* ``/admin/voice-transcription/*`` — provider options, the month's usage
  report and the "Test" probe (superuser). The settings themselves live on
  ``GET/PUT /admin/server-config``.
"""
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, status

from app.api.deps import (
    CurrentUser,
    SessionDep,
    SpeechPrincipal,
    get_current_active_superuser,
    get_speech_principal_ws,
    recheck_speech_principal,
)
from app.core.config import settings
from app.models import User
from app.models.voice_transcription.usage import (
    VoiceTranscriptionProviderOption,
    VoiceTranscriptionTestRequest,
    VoiceTranscriptionTestResult,
    VoiceTranscriptionUsagePublic,
    VoiceTranscriptionUsageReport,
)
from app.services.voice_transcription import tracker
from app.services.voice_transcription.config_service import (
    VoiceTranscriptionConfigService,
)
from app.services.voice_transcription.session_service import (
    VoiceTranscriptionSessionService,
)
from app.services.voice_transcription.usage_service import (
    VoiceTranscriptionUsageService,
)

router = APIRouter(prefix="/speech", tags=["voice-transcription"])
admin_router = APIRouter(prefix="/admin/voice-transcription", tags=["voice-transcription"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.websocket("/stream")
async def speech_stream(
    websocket: WebSocket,
    principal: Annotated[SpeechPrincipal, Depends(get_speech_principal_ws)],
) -> None:
    """Relay one dictation: client PCM in, partial / final transcripts out."""
    await VoiceTranscriptionSessionService.run(
        websocket,
        user_id=principal.user_id,
        client_kind=principal.client_kind,
        recheck=lambda: recheck_speech_principal(principal),
    )


@router.get("/usage", response_model=VoiceTranscriptionUsagePublic)
def get_my_speech_usage(session: SessionDep, current_user: CurrentUser) -> Any:
    """The caller's voice transcription usage for the current UTC month."""
    return VoiceTranscriptionUsageService.user_usage(session, current_user)


@admin_router.get("/providers", response_model=list[VoiceTranscriptionProviderOption])
def list_voice_transcription_providers(session: SessionDep, current_user: SuperUser) -> Any:
    """AI providers that can serve the relay (speech-capable, fixed key)."""
    return VoiceTranscriptionConfigService.list_provider_options(session)


@admin_router.get("/usage", response_model=VoiceTranscriptionUsageReport)
def get_voice_transcription_usage_report(session: SessionDep, current_user: SuperUser) -> Any:
    """Per-user minutes and sessions for the current UTC month, most first."""
    return VoiceTranscriptionUsageService.admin_report(session)


@admin_router.post("/test", response_model=VoiceTranscriptionTestResult)
async def test_voice_transcription_provider(
    session: SessionDep,
    current_user: SuperUser,
    data: VoiceTranscriptionTestRequest,
) -> Any:
    """Open a short silent session against the provider (default: the
    configured one) and report ``ok`` / ``invalid_key`` / ``unreachable`` /
    ``quota`` / ``disabled``. Never returns provider text."""
    retry_after = tracker.test_rate_limiter.check(
        f"speech-test:{current_user.id}",
        settings.VOICE_TRANSCRIPTION_TEST_RATE_LIMIT_PER_MIN,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(int(retry_after))},
        )
    return await VoiceTranscriptionSessionService.test_provider(session, data.provider_id)
