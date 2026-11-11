# Voice Transcription Relay - Technical Details

Business rules and Protocol v1: [Voice Transcription Relay](voice_transcription.md).

## Code Map

| Piece | Path |
|-------|------|
| Routes (WS, usage, admin helpers) | `backend/app/api/routes/voice_transcription.py` |
| Session runtime (one socket) | `backend/app/services/voice_transcription/session.py` |
| Entry points (serve a session, admin Test) | `backend/app/services/voice_transcription/session_service.py` |
| Config resolution, validation, discovery block | `backend/app/services/voice_transcription/config_service.py` |
| Ledger and monthly sums | `backend/app/services/voice_transcription/usage_service.py` |
| Live-session and rate-limit bookkeeping | `backend/app/services/voice_transcription/tracker.py` |
| Constants (capable types, frame size, PCM rate) | `backend/app/services/voice_transcription/constants.py` |
| `voice_transcription.updated` emitter | `backend/app/services/voice_transcription/events.py` |
| Adapter contract | `backend/app/services/voice_transcription/providers/base.py` |
| Adapter registry | `backend/app/services/voice_transcription/providers/registry.py` |
| Gemini Live adapter | `backend/app/services/voice_transcription/providers/gemini_live.py` |
| Ledger table and API shapes | `backend/app/models/voice_transcription/usage.py` |
| Settings columns | `backend/app/models/server_config/server_config.py` |
| Migration | `backend/app/alembic/versions/46e56c924dd3_voice_transcription.py` |
| WS auth (`SpeechPrincipal`, recheck) | `backend/app/api/deps.py` |
| Discovery | `backend/app/main.py` |
| Proxy | `frontend/nginx.conf` |

## Data Model

`ServerConfig` gains `voice_transcription_enabled` (bool, default false), `voice_transcription_provider_id` (FK `ai_provider.id`, nullable, `ON DELETE SET NULL`; the service layer asks for confirmation before deleting the provider, the DB never blocks it) and `voice_transcription_monthly_minutes_per_user` (int, null = no cap). `ServerConfigUpdate` validates the cap `1..44640`; for the two nullable fields an explicit `null` clears them (the service reads `model_fields_set`).

`voice_transcription_usage` (`VoiceTranscriptionUsage`): `id`, `user_id` (FK user, cascade), `session_id` (unique), `started_at`, `ended_at`, `audio_seconds`, `replay_seconds`, `provider_id` (FK, SET NULL), `provider_type`, `model`, `client_kind`, `outcome`. Index `ix_voice_transcription_usage_user_started` on `(user_id, started_at)`.

## API

| Route | Auth | Purpose |
|-------|------|---------|
| `WS /api/v1/speech/stream` | Bearer header at upgrade | The relay (Protocol v1) |
| `GET /api/v1/speech/usage` | CurrentUser | Caller's month (`VoiceTranscriptionUsagePublic`) |
| `GET /api/v1/admin/voice-transcription/providers` | superuser | Speech-capable provider options |
| `GET /api/v1/admin/voice-transcription/usage` | superuser | Per-user seconds and sessions this month, most first, with totals |
| `POST /api/v1/admin/voice-transcription/test` | superuser | Body `{provider_id?}`; silent probe; `{ok, error}`; 10/min per admin, 429 beyond |
| `GET/PUT /api/v1/admin/server-config` | superuser | The three settings; 400 `detail` = validation reason code |

`PUT /admin/server-config` compares the three fields before and after and, on change, schedules `VoiceTranscriptionEvents.updated(available)` as a background task. Provider deletion (`AIProvidersService.delete`) uses `AIProviderDeleteImpact.voice_transcription_in_use` for the 409 and emits the event after a forced delete.

## Session Runtime

`VoiceSession.run()`: accept, read `start` (10 s), then in order: open rate limit (`busy`), `resolve` + `load_api_key` (`disabled`), `tracker.try_acquire` (`busy`), cap check (`quota_exceeded`), `adapter.open` (exception: `unavailable`). Then three tasks: **reader** (client frames; PCM pushed immediately, never paced; truncated to the room left under the session and quota limits), **pump** (adapter events to client; server `start` is sent before the first transcript event; `Closed` becomes `stop` or `quota_exceeded`), **watchdog** (idle, ready timeout `VOICE_TRANSCRIPTION_READY_TIMEOUT_SECONDS`, revocation re-check every `VOICE_TRANSCRIPTION_REVOCATION_CHECK_SECONDS`; one erroring re-check counts as authorized; `MAX_RECHECK_FAILURES = 2` consecutive errors end the session as `unauthorized`, a success resets the count).

`_finish(outcome, terminal)` runs once (guard flag): cancel the provider stream, send the terminal event, close 1000. Teardown releases the tracker slot, writes the ledger row in a fresh DB session (failure is logged, never raised) and logs the metadata line. The provider key is a local that is dropped after `adapter.open`. DB sessions are short-lived; none is held for the socket's lifetime.

In-process state (`tracker.py`) is valid because the backend runs a single uvicorn worker; the open rate limiter is a reconnect-loop backstop, not a billing control (the cap is enforced from the ledger).

## Adapter Interface

`SpeechAdapter` (`provider_type`, `model`, `open(api_key, locale, sample_rate) -> SpeechStream`, `probe(api_key) -> "ok"|"invalid_key"|"unreachable"|"quota"`). `SpeechStream`: `push(pcm)` (send or queue, never paces), `end_audio()`, `cancel()`, `events()`, `rotations`. Events: `Ready`, `Partial(text)`, `Final(text)`, `Closed`, `Failed(code in {failed, unavailable}, message, cause)`. Messages are user-facing sentences; `cause` is for the server only. Adapters are registered by `AIProvider.provider_type` in `providers/registry.py`. "Speech-capable" = type in `SPEECH_CAPABLE_PROVIDER_TYPES` AND `kind == fixed_key` AND adapter registered (`is_speech_capable`).

## Gemini Live Adapter

- Model `gemini-3.5-transcribe-live`, API version `v1beta`, `client.aio.live.connect`; config `response_modalities=[TEXT]`, `input_audio_transcription` with `language_codes=[locale]`. Audio: `send_realtime_input(audio=Blob(mime "audio/pcm;rate=16000"))`; end: `audio_stream_end=True`.
- `interim_input_transcription.text` is cumulative within an utterance (replace, may shrink) and becomes `Partial`; `input_transcription.text` is one utterance's final and becomes `Final`. Utterances split on server VAD (about 0.5 s pause).
- **Settle rule** after end of audio: the socket stays open. Settle when quiet (no pending interim, no open voice activity) for 0.3 s (0.6 s if the audio went out as a burst), re-armed on every message; also after 3 s without any message, and 10 s after the end in any case. A forced settle turns the last interim into a final.
- **Rotation**: a connection lives about 10 min. At 570 s or on `go_away` a new one opens, audio queues until it is ready, the old one is ended, and the new one's events are held until the old one's last final is out. A word at the seam may be cut.
- **Close mapping**: close code 1007 or an auth-like reason gives `failed` (cause auth); code 429 or a quota-like reason (`RESOURCE_EXHAUSTED`, rate limit) gives `unavailable` with an admin-contact message; a close before ready gives `unavailable`; otherwise `failed`. Provider text goes to the log only, scrubbed of the key and clipped to 300 characters.
- **SDK**: requires `google-genai>=2.9.0,<3.0.0` for `interim_input_transcription` and `audio_stream_end` (`pyproject.toml`); the lock resolves 2.34.0.
- `probe` sends 1 s of silence, ends the audio and classifies the outcome (deadline: connect timeout + stop cap + 1 s).
- Indicative price about $0.009 per minute (Google pricing, 2026-11-11).

## Frontend

`frontend/src/components/Admin/VoiceTranscription/` (`VoiceTranscriptionCard.tsx`, `VoiceMonthlyLimitDialog.tsx`, `VoiceUsageCard.tsx`, `VoiceUsageRow.tsx`, `AllVoiceUsageSheet.tsx`, `voiceTranscription.ts`), mounted in the `features` tab of `frontend/src/routes/_layout/admin/server-configuration.tsx`. Provider deletion is guarded in `frontend/src/components/Admin/AiProviders/DeleteProviderDialog.tsx`.

## Live Smoke (manual)

`backend/scripts/voice_transcription_live_smoke.py` drives `GeminiLiveAdapter` directly (no database, no WebSocket) with the key from `GEMINI_API_KEY`. Never run in CI; it bills about $0.009 per minute of audio. From `backend/`:

```
GEMINI_API_KEY=... python scripts/voice_transcription_live_smoke.py --wav <16k mono s16le wav> [--burst] [--locale en-US] [--show-text]
```

It prints timings and counts only; transcript text appears only with `--show-text`.

## Settings

`VOICE_TRANSCRIPTION_MAX_SESSION_SECONDS` (1800), `_IDLE_TIMEOUT_SECONDS` (60), `_MAX_SESSIONS_PER_USER` (1), `_OPEN_RATE_LIMIT_PER_MIN` (10), `_REVOCATION_CHECK_SECONDS` (300), `_START_FRAME_TIMEOUT_SECONDS` (10), `_READY_TIMEOUT_SECONDS` (30), `_TEST_RATE_LIMIT_PER_MIN` (10), in `backend/app/core/config.py`. Floats where a test patches sub-second values.

## Authentication Detail

`get_speech_principal_ws` reads only the `Authorization` header, resolves through `_resolve_platform_token` (revocation and account-valid checks) with scoped token types `guest_share`, `webapp_share`, `agent_env`, `agent` disallowed, and raises `WebSocketException(1008)` before accept. `recheck_speech_principal` repeats the account check and, for `client_kind == "desktop"`, the device check.

## Proxy Requirement

The route is WebSocket-only. The unified production config already upgrades everything under `/api/`. The local `frontend/nginx.conf` has an exact-match `location = /api/v1/speech/stream` block (upgrade headers, `proxy_buffering off`, 3600 s timeouts) before `/api/`, so `GET /api/v1/speech/usage` stays on the plain block. See [Nginx Setup](../../infrastructure/nginx_setup.md#voice-transcription-apiv1speechstream).

## Test Seams

- `providers/registry.py` `override_for_tests(provider_type, adapter)` swaps in a fake adapter and always restores it.
- `tracker.reset_for_tests()` clears live sessions and rate-limit windows.
- `GeminiTimings` and the `LiveConnect` callable are injectable, so the adapter runs against a fake live session with sub-second timers and no network.
- Tests: `backend/tests/unit/test_voice_transcription_gemini_live.py`, `backend/tests/migrations/voice_transcription_test.py`.
