---
feature: voice_transcription
domain: application
one_liner: "Server-paid live speech-to-text relay: desktop and mobile stream microphone audio over one WebSocket and get partial and final text, with admin switch, monthly cap and usage ledger."
docs:
  tech: voice_transcription_tech.md
affects: [desktop_auth, server_configuration, admin_ai_credential_provisioning, nginx_setup]
---
# Voice Transcription Relay

## Overview

Core offers live speech-to-text to its users as a **server feature**. A superuser turns it on and chooses which company AI provider pays for it. Cinna Desktop (and later Cinna Mobile) streams microphone audio to core over a single WebSocket and receives partial and final transcript text. Core holds the provider key; the client never sees it and knows nothing about the provider behind the relay.

The relay is deliberately provider-neutral: clients speak only [Protocol v1](#protocol-v1) below. Google Gemini Live is the only provider today; the adapter layer is built so another can join.

Users' own stored AI credentials are **not** used here (they already reach the desktop through account-config, where the desktop's own engine covers them). Only an admin-owned, fixed-key provider can serve the relay.

## Core Capabilities

- **One switch for the instance.** When enabled, every signed-in user of a desktop or mobile client gets it. There are no per-user grants.
- **Provider-neutral protocol**, versioned, defined once in this document.
- **Per-user monthly cap** (optional, minutes, calendar month UTC) enforced at start and during the session, plus a ledger of every session.
- **No transcript or audio is ever stored or logged.** Only metadata.
- **Replay-friendly.** A client may send already-buffered audio faster than real time at the start of a session; the relay passes it straight through.
- **Discovery.** The desktop and mobile discovery documents announce the relay only while it is on and usable, so clients hide the "Cinna server" option otherwise.

## Protocol v1

This section is the single source of truth for desktop and mobile clients. A change that is not backward compatible bumps `version` (the `start` frame carries it; the discovery block announces it).

### Endpoint and authentication

`WS {backend}/api/v1/speech/stream` (the discovery block carries the full `ws(s)://` URL).

- Auth is `Authorization: Bearer <access token>` on the upgrade request. The query string is **never** read, so no secret sits in the URL. The token is resolved like any platform token, including the desktop device-revocation check and the account-validity check.
- Failed auth (missing/invalid/expired token, revoked device, invalid account, or a scoped token of a kind that may not use it: guest share, webapp share, agent environment, agent access) closes with code **1008** before the upgrade is accepted.
- The access token is short-lived and checked only at open. During a session core re-checks every 5 minutes that the account is still valid and, for a desktop token, that the device is not revoked; a revoked device or invalid account ends the session with `unauthorized`. One re-check that errors (for example a database blip) still counts as authorized; two consecutive errors also end it with `unauthorized`, and a successful re-check resets the count.

### Client frames

| Frame | Shape | Notes |
|-------|-------|-------|
| `start` (text, first, required) | `{"type":"start","version":1,"locale":"en-US","sample_rate":16000,"replay_seconds":0}` | `version` must be `1`; `sample_rate` must be `16000`; `locale` is BCP-47, at most 35 characters; `replay_seconds` is a finite number >= 0. Must arrive within 10 s of the upgrade. |
| audio (binary) | raw PCM, signed 16-bit little-endian, 16 kHz, mono | At most **64 KB (65 536 bytes)** per frame; about 100 ms per frame when live. |
| `stop` (text) | `{"type":"stop"}` | No more audio. The server flushes the last words, sends `stop`, closes. |
| `cancel` (text) | `{"type":"cancel"}` | Drop everything and close; the server sends nothing more. |

Anything else (a bad or late `start`, a second `start`, unknown text, malformed JSON, a binary frame over 64 KB) gets `error` `bad_request`, then close. A client that disconnects without a word is treated as `cancel`. Audio arriving after `stop` or after a cap ended the audio is dropped silently.

**Replay burst.** `replay_seconds` > 0 announces that the first N seconds of the audio are replayed (for example the desktop's unconfirmed buffer after an on-device engine failed and the user chose to continue with the Cinna server). It is informational: recorded in the ledger (clamped to the audio actually sent) and the session log, and it changes no behaviour. The relay never paces audio, so a burst (a live test sent 21 s in one burst and every word came back) followed by live audio just works. Replayed audio counts against the monthly cap and the 30-minute session limit like any other.

### Server events (text frames)

| Event | Shape | Meaning |
|-------|-------|---------|
| `start` | `{"type":"start","backend":"core-relay","session_id":"<uuid>","remaining_seconds":1234}` | Sent once, when the provider session is ready and before any transcript event. `remaining_seconds` is the user's monthly allowance left (integer), or `null` with no cap. |
| `partial` | `{"type":"partial","text":"..."}` | The whole unconfirmed text of the current utterance. **Replaces** the previous partial, never appended; may shrink. |
| `final` | `{"type":"final","text":"..."}` | One utterance, confirmed, never revised. Finals arrive in spoken order. |
| `error` | `{"type":"error","code":"...","message":"...","recoverable":false}` | Terminal. `message` is a sentence fit to show to the user. `recoverable` is always `false`. |
| `stop` | `{"type":"stop","full_text":"..."}` | Terminal. `full_text` is the finals joined with single spaces. |

**Exactly one terminal event per session** (`stop` or `error`; none after `cancel` or a client disconnect), then the server closes with **1000**. Every close after the upgrade is 1000; 1008 appears only for failed auth before it.

### Error codes

| code | when |
|------|------|
| `disabled` | the feature is off, or its provider is missing, deleted or unusable |
| `quota_exceeded` | the monthly cap was already reached at start, or reached mid-session (the finals so far are flushed first, then this) |
| `busy` | the user already has a live session (one per user), **or** the user opened more than 10 sessions in the last minute (the open rate limit; a ruling of the product owner, so clients need only one "try again shortly" path) |
| `unavailable` | the provider was unreachable, refused to start, or did not become ready in time; also a provider quota that is used up (the message tells the user to contact the admin) |
| `failed` | the provider rejected the key, closed unexpectedly, or anything else |
| `unauthorized` | the device or account was revoked mid-session |
| `bad_request` | protocol violation |

### Limits

| Limit | Default | Setting |
|-------|---------|---------|
| Audio per session | 30 min; reaching it ends the session with a normal `stop` (not an error) | `VOICE_TRANSCRIPTION_MAX_SESSION_SECONDS` |
| Idle (no audio frame) | 60 s, then ends like `stop` | `VOICE_TRANSCRIPTION_IDLE_TIMEOUT_SECONDS` |
| Concurrent sessions per user | 1 | `VOICE_TRANSCRIPTION_MAX_SESSIONS_PER_USER` |
| Session opens per user per minute | 10 | `VOICE_TRANSCRIPTION_OPEN_RATE_LIMIT_PER_MIN` |
| Frame size | 64 KB | fixed |

### `GET /api/v1/speech/usage`

Authenticated (any signed-in user). Returns `{"enabled": bool, "used_seconds": number, "cap_seconds": number|null, "period_end": "<ISO datetime>"}`. `enabled` is true only while the relay is on and usable; `cap_seconds` is null with no cap; `period_end` is the start of the next UTC month. `used_seconds` counts finished sessions of the current UTC month. Desktop shows the remaining minutes in its settings.

### Discovery

Both `GET /.well-known/cinna-desktop` and `GET /.well-known/cinna-app` carry

```json
"voice_transcription": {"version": 1, "url": "wss://<backend>/api/v1/speech/stream"}
```

The block is **present only while the feature is on and its provider is usable** (enabled, provider exists, speech-capable fixed-key, adapter registered, and its key loads; an undecryptable key means the block is absent). Absent means the client hides the "Cinna server" option. A lookup failure never breaks discovery: the block is simply omitted.

## Admin Setup

Superusers configure the relay on **Admin > Server Configuration > Features** (a tab for server features that are off by default), in two cards.

- **Voice transcription card**: master switch; **Provider** select listing only speech-capable providers (an empty state links to AI providers when none exists); **Monthly limit per person** (minutes, 1 to 44 640; empty means no limit) edited in a dialog; a **Test provider** button that opens a short silent provider session and reports the result as a toast. The switch is disabled only while the feature is off and no provider is saved; it stays usable when the feature is on but its provider was deleted, so the admin can turn it off.
- **Voice usage card**: this month's minutes per user (the session count is in the row's info tooltip), most minutes first, with totals in minutes, capped to five rows plus "Show all". A person who reached the cap is flagged.

Rules:
- "Speech-capable" is a fixed set of provider types (`SPEECH_CAPABLE_PROVIDER_TYPES`, Google today) and the provider must be a **fixed-key** one with a registered adapter. A per-user-minted-key provider is not eligible.
- Saving an unusable provider is refused with 400: `voice_transcription_provider_not_found`, `voice_transcription_provider_not_speech_capable`, or `voice_transcription_provider_required` (switched on with no provider). A provider that became unusable after it was chosen does not block unrelated saves; the relay just reports `disabled` and disappears from discovery.
- **Deleting the provider** from Company AI providers returns **409 unless forced**, even if no member holds a key, with the reason that voice transcription uses it (the delete dialog adds a sentence when `voice_transcription_in_use` is set). A forced delete clears the provider (the switch stays on but dictation is unavailable until another provider is chosen).
- Any change to the three settings, or deleting the provider, broadcasts `voice_transcription.updated` with `{available}`. Clients refresh discovery on their own schedule; the event is a hint only.
- **Test** results: `ok`, `invalid_key`, `unreachable`, `quota`, `disabled`; the report never contains provider text. Limited to 10 tests per admin per minute (429 beyond).

## Ledger and Monthly Cap

- **One ledger row per session that sent a valid `start`**, on every outcome: `stop`, `cancelled`, or an error code (`disabled`, `busy`, `quota_exceeded`, `unavailable`, `failed`, `unauthorized`, `bad_request` after start). A session that never sent a valid `start` (bad first frame, timeout, failed auth) leaves no row.
- A row holds user, relay session id, start and end time, `audio_seconds` (PCM bytes accepted / 32 000, replay included), `replay_seconds`, provider id/type/model, client kind (`desktop`, `mobile`, `web`) and outcome. No text, audio, key or token.
- **The cap** is minutes per user per **UTC calendar month**, null by default. Used = the sum of `audio_seconds` of the user's sessions that started this month, plus the running session. At start, remaining = cap - used; zero or less gives `quota_exceeded`. During the session, audio beyond the remaining allowance is not accepted: the audio ends, finals already spoken are delivered, then `quota_exceeded` is sent instead of `stop`.
- Lowering the cap applies from the next session start; an in-progress session keeps the allowance computed when it opened.

## Privacy

- Transcript text, audio, the provider key, the access token and the URL are never logged or stored.
- One metadata line per session: user id, session id, provider type, model, audio seconds, replay seconds, time to first partial, counts of partials and finals, rotations, outcome.
- Messages sent to clients are fixed sentences; provider text is never passed through. Provider failure reasons are logged scrubbed of the key and clipped.

## Business Rules

- Admin-configured and server-paid; all users when enabled; no silent fallback is implied by the relay (the client decides, and asks its user, before moving audio to the cloud).
- One live session per user.
- The relay needs a reverse proxy that upgrades WebSockets on `/api/v1/speech/stream` (see [Nginx Setup](../../infrastructure/nginx_setup.md#voice-transcription-apiv1speechstream)).

## Integration Points

- [Server Configuration](../server_configuration/server_configuration.md): the three settings live on the `ServerConfig` row and the Features tab; saves go through `PUT /admin/server-config`.
- [Admin AI Credential Provisioning](../ai_credentials/admin_ai_credential_provisioning.md): the provider is a company AI provider (fixed key); deleting it is guarded by a 409 unless forced.
- [Desktop App Authentication](../desktop_auth/desktop_auth.md): the stream authenticates with the desktop/mobile access token (device revocation honoured); both discovery documents carry the `voice_transcription` block.
- [Nginx Setup](../../infrastructure/nginx_setup.md): WebSocket upgrade on `/api/v1/speech/stream`.
- [Real-Time Event Bus System](../realtime_events/event_bus_system.md): `voice_transcription.updated` broadcast.

Technical details: [Voice Transcription - Technical Details](voice_transcription_tech.md).

## Changelog

- First version: protocol v1, Gemini Live adapter, ledger and cap, admin Features tab, discovery block.
