# Channel Debug Monitor — Technical

Implementation reference for the [Channel Debug Monitor](channel_debug_monitor.md). Parent feature: [Server Channels](server_channels_tech.md).

## File Locations

**Backend**
- `backend/app/services/server_channels/channel_debug_buffer.py` — the capture buffer, event dataclass, and event-kind constants
- `backend/app/services/server_channels/channel_inbound_service.py` — inbound capture hooks at the pipeline's decision points
- `backend/app/services/server_channels/channel_outbound_service.py` — outbound capture hooks around adapter delivery
- `backend/app/services/server_channels/server_channel_service.py` — `list_recent_senders`, `resolve_test_thread_key`, and the buffer prune in `delete_channel`
- `backend/app/api/routes/server_channels.py` — debug routes, recent-senders route, and the reworked test-send
- `backend/app/models/server_channels/server_channel.py` — `ChannelDebugEventPublic`, `ChannelDebugEventsPublic`, `ChannelRecentSender`, `ChannelTestOutboundRequest`
- `backend/app/models/events/security_event.py` — `SERVER_CHANNEL_TEST_SEND`

**Frontend**
- `frontend/src/components/Admin/ServerChannels/ChannelDebugDialog.tsx` — the panel
- `frontend/src/components/Admin/ServerChannels/ChannelSetupInstructionsPanel.tsx` — the test-send target picker
- `frontend/src/components/Admin/ServerChannels/ServerChannelsCard.tsx` — the bug-icon action and query cleanup on delete

**Tests**
- `backend/tests/api/server_channels/server_channels_debug_test.py` — capture, authorization, targeting, audit
- `backend/tests/unit/test_channel_debug_buffer.py` — ring-buffer eviction, text clamp, per-channel isolation
- `backend/tests/utils/server_channel.py` — `list_debug_events`, `clear_debug_events`, `list_recent_senders`, `send_test_outbound`
- `backend/tests/api/server_channels/conftest.py` — `reset_channel_debug_buffer`

## Database Schema

**None, deliberately.** No table, no migration. Inbound message text at rest in the database is what the parent feature is careful to avoid, and a debug convenience is not a good reason to start. `SecurityEvent` remains the durable record. The consequences are real and accepted: the feed dies with the process, and behind multiple workers each panel sees only its own worker's events.

## API Endpoints

`backend/app/api/routes/server_channels.py` — all superuser-only:

- `GET /api/v1/admin/server-channels/{channel_id}/debug-events` → `ChannelDebugEventsPublic`. `capturing_since` is the process start time, so an empty feed is distinguishable from a webhook that never fired; `buffer_size` lets the panel say "last N" honestly.
- `DELETE /api/v1/admin/server-channels/{channel_id}/debug-events` → `Message`
- `GET /api/v1/admin/server-channels/{channel_id}/recent-senders` → `list[ChannelRecentSender]`
- `POST /api/v1/admin/server-channels/{channel_id}/test-outbound` (`ChannelTestOutboundRequest`) → `ChannelTestOutboundResult`. Takes `email` **or** `thread_key`; a model validator rejects both-or-neither with a 422. Failure still travels as a 200 with `success=false`, since the reason is the endpoint's whole purpose.

## Services & Key Methods

### `ChannelDebugBuffer` (`channel_debug_buffer.py`)

- `record(...)` — appends one event; swallows its own errors so a debug aid can never fail a webhook or a delivery. **That guard does not cover the caller's argument expressions**, which Python evaluates first: an f-string over a non-existent attribute raises at the call site, and inside `_route_new_thread` that lands in a broad `except` which abandons the install. This happened during implementation (`AgentBundle` has `display_name`, not `name`). Keep summary arguments to attributes known to exist.
- `record(...)` also **coalesces**: when the newest event for a channel is identical (`ChannelDebugEvent.same_as` — direction, kind, summary, sender, thread, text and detail all equal) it is replaced with a copy carrying `repeat + 1` and the newer timestamp, instead of appending. Bounds a flood to one row, so a repeated request cannot evict the feed from a bounded ring
- `list_events(channel_id)` — newest-first snapshot, built inside the lock so a reader cannot observe a torn deque
- `clear(channel_id)` / `reset()` — per-channel drop; `reset()` is test hygiene for process-global state
- Concurrency: a class-level `threading.Lock` guards every path. This is the correct primitive rather than `asyncio.Lock` because `record()` is called both from the event loop and from `anyio.to_thread` workers. Critical sections hold no `await` and no I/O.
- `CAPTURING_SINCE` — module-level process start stamp, surfaced by the debug route

### `ServerChannelService` (`server_channel_service.py`)

- `list_recent_senders(session, channel)` — one joined query over `ChannelThreadBinding` + `User` (no per-binding lookup), merged with the buffer; bindings win on conflict
- `_as_utc(...)` / `_EPOCH` — normalise `last_seen` before sorting. The two sources disagree: `channel_thread_binding.updated_at` is a bare `DateTime` column and returns naive from Postgres, while the buffer stamps timezone-aware UTC. Sorting a mix raised `TypeError: can't compare offset-naive and offset-aware datetimes` — on exactly the one-bound-plus-one-buffered case the merge exists to produce. Covered by a regression test.
- `resolve_test_thread_key(session, channel, email)` — email → an observed thread, entirely local; raises `ChannelError` with an actionable explanation when the address has never been seen
- `delete_channel(...)` — calls `ChannelDebugBuffer.clear(channel.id)`; the buffer has no cascade of its own

### Capture hook placement

- Inbound (`channel_inbound_service.py`): verification failure (before the re-raise), ignored / added-to-space events, verified arrival, missing sender identity, whitelist denial, user-resolution denial, Pass-1 route, Pass-2 no-match, Pass-2 auto-install
- Outbound (`channel_outbound_service.py` `_deliver`, and `channel_inbound_service.py` `_reply`): delivery success and failure
- Every hook is a standalone statement, never inside a conditional expression, so none can alter a branch

## Frontend Components

- `ChannelDebugDialog.tsx` — `useQuery(["serverChannelDebug", channelId])` with `enabled: open` gating **both** the query and its `refetchInterval`, so polling stops with the dialog. Per-row "Reply here" calls the test-send; the in-flight row is tracked by **event id**, not thread key, because several events share one thread and keying on the thread spins every matching row. Any in-flight send disables all rows, since a second mutation supersedes the first observer and makes the resulting toast ambiguous. Unknown event kinds fall back to a neutral badge.
- `ChannelSetupInstructionsPanel.tsx` — `useQuery(["serverChannelRecentSenders", channelId])` feeding a `Select` with a `__custom__` sentinel that reveals the raw thread input. Distinguishes loading, error and genuinely-empty states: without the error branch a failed admin GET renders as "Nobody has messaged this channel yet", sending the admin to re-check their provider configuration over a single failed request. A test result is cleared when the target changes, so it cannot read as a verdict about the newly picked one.
- `ServerChannelsCard.tsx` — bug-icon action; `removeQueries` for both debug and recent-sender keys on channel delete

## Configuration

- `SERVER_CHANNEL_DEBUG_BUFFER_SIZE` (`backend/app/core/config.py`, default `50`) — ring-buffer depth per channel
- `SERVER_CHANNEL_DEBUG_TEXT_MAX_CHARS` (default `2_000`) — captured-text clamp; truncation is marked, not silent

## Security

- **Superuser-only on every route**, reading and clearing alike, because the feed carries sender identity and message text. Authorization runs before the channel lookup, so a non-superuser never learns whether a channel id exists. Both the unauthenticated (401) and plain-authenticated (403) cases are asserted — only the second distinguishes a real guard from a missing dependency.
- **Server channels are instance-global**; `created_by` is provenance, not an ACL, so there is deliberately no ownership check.
- **No new secret exposure.** No hook passes the encrypted secrets, the webhook token, the inbound bearer JWT or a minted access token. `detail` carries only pipeline stage markers plus the admin-authored whitelist. The two send-failure hooks interpolate an exception whose string is method, URL and status — bearer tokens live in headers and signed assertions in request bodies, so neither appears; the identical string is already persisted to `binding.last_error`.
- **Verification failures capture nothing from the payload**, since it failed the check that would make it trustworthy.
- **Admin test sends are audited** (`SERVER_CHANNEL_TEST_SEND`) with the resolved thread and how it was targeted, but never the message body: `SecurityEvent` rows are broadly readable, and the durable record is needed precisely because the buffer is clearable.
- **Flood resistance:** consecutive identical events collapse into one counted row, so a caller repeating a request cannot evict the feed from the bounded ring. A flood of *varying* events still rolls the ring — that is what a ring is — but the common case (a retry loop, a redelivery storm) is bounded to a single row.
- **`list_recent_senders` is capped** at `_RECENT_SENDERS_SCAN_LIMIT` bindings (newest first, then deduped by address), so the picker cannot load an unbounded result set on a busy channel.
