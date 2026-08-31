# Channel Debug Monitor — Technical

Implementation reference for the [Channel Debug Monitor](channel_debug_monitor.md). Parent feature: [Server Channels](server_channels_tech.md).

## File Locations

**Backend**
- `backend/app/services/server_channels/channel_debug_buffer.py` — the capture buffer, event dataclass, and event-kind constants
- `backend/app/services/server_channels/channel_inbound_service.py` — inbound capture hooks at the pipeline's decision points; also `_attachment_detail` / `_attachment_summary`, the attachment-fields producer described below (see [Attachment fields](#attachment-fields))
- `backend/app/services/server_channels/channel_outbound_service.py` — outbound capture hooks around adapter delivery
- `backend/app/services/server_channels/server_channel_service.py` — `list_recent_senders`, `resolve_test_thread_key`, and the buffer prune in `delete_channel`
- `backend/app/api/routes/server_channels.py` — debug routes, recent-senders route, and the reworked test-send
- `backend/app/models/server_channels/server_channel.py` — `ChannelDebugEventPublic`, `ChannelDebugEventsPublic`, `ChannelRecentSender`, `ChannelTestOutboundRequest`
- `backend/app/models/events/security_event.py` — `SERVER_CHANNEL_TEST_SEND`

**Frontend**
- `frontend/src/components/Admin/ServerChannels/ChannelDebugDialog.tsx` — the panel; also `readAttachments` / `parseSkips` / `SKIP_REASON_FAMILY`, the attachment-fields consumer described below (see [Attachment fields](#attachment-fields))
- `frontend/src/components/Admin/ServerChannels/ChannelSetupInstructionsPanel.tsx` — the test-send target picker
- `frontend/src/components/Admin/ServerChannels/ServerChannelsCard.tsx` — the bug-icon action and query cleanup on delete

**Tests**
- `backend/tests/api/server_channels/server_channels_debug_test.py` — capture, authorization, targeting, audit
- `backend/tests/unit/test_channel_debug_buffer.py` — ring-buffer eviction, text clamp, per-channel isolation
- `backend/tests/unit/test_channel_attachment_debug_detail.py` — **new**: pins `_attachment_detail`'s producer-side wire format (separator, per-entry shape, the 500-char cap, and that the cap is not entry-aware) — see [Attachment fields](#attachment-fields)
- `backend/tests/utils/server_channel.py` — `list_debug_events`, `clear_debug_events`, `list_recent_senders`, `send_test_outbound`
- `backend/tests/api/server_channels/conftest.py` — `reset_channel_debug_buffer`

## Database Schema

**None for this buffer, deliberately.** No table, no migration for `ChannelDebugBuffer` itself — it stays in-memory and dies with the process. `SecurityEvent` remains the durable audit record for denials and verification failures. The consequences of the buffer's own ephemerality are real and accepted: the feed dies with the process, and behind multiple workers each panel sees only its own worker's events.

**This no longer means inbound message text is kept out of the database at the feature level.** The [Auto Routing Tuning](../routing_tuning/routing_tuning.md) feature durably persists a `routing_decision` row per routed message — `message_text` clamped to `ROUTING_TRACE_TEXT_MAX_CHARS`, retained for `ROUTING_TRACE_RETENTION_DAYS`, superuser-only. `ROUTING_TRACE_STORE_MESSAGE_TEXT=False` withholds not just that field but the sender's words wherever else they could appear in the row: the stored `stages` payload is projected through an allowlist of fields explicitly declared free of sender text, applied on both the write path and the read path from one shared definition, so anything not named on it is withheld by default — including a field added after the gate was written — rather than requiring someone to keep naming every field that turns out to carry sender text. A `message_sha256` is always kept regardless, so a trace stays useful with text off. The gate hides, it does not erase — rows keep their data until `ROUTING_TRACE_RETENTION_DAYS` expires them or an admin clears the traces. `RoutingTrace.capture()` opens at **three** sites, and none of them is in this file's neighbour `channel_inbound_service.py` — that module no longer imports the trace service at all (`tests/architecture/routing_trace_layering_test.py` is the authority). Two are `ChannelRoutingService`'s Pass-1 and Pass-2 thread targets in `channel_routing_service.py`, carrying `origin="server_channel"` for a webhook transport, `origin="email"` for the polled one, or `origin="simulate"` when an admin simulate/replay reuses the same two sites; the third is `AppMCPRoutingService.route_message`, which since Phase 6 of the channels & identity unification writes `origin="app_mcp"` rows with a populated `channel_id`. App MCP capture is governed by `ROUTING_TRACE_APP_MCP_MODE` and **defaults to metadata-only** — App MCP routes every message rather than only thread openings and sits behind no webhook rate limit, so it is the one origin whose write volume is unbounded; `off` writes no `app_mcp` row at all. **None of that gives App MCP a debug feed**: it emits no `ChannelDebugBuffer` events, by design, because its reply is the synchronous MCP response and there is no inbound pipeline or outbound delivery here to hook. See [Auto Routing Tuning tech](../routing_tuning/routing_tuning_tech.md). This debug buffer is unaffected by that change — it is exactly as ephemeral as described above — but it now carries the durable trace's id in `detail.trace_id` when Auto Routing Tuning actually wrote a row, linking the live row to the row that outlives the process.

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
- `delete_channel(...)` — calls `ChannelDebugBuffer.clear(channel.id)`; the buffer has no cascade of its own. It also **refuses a singleton channel type outright** (`_is_singleton_type` → `UnsupportedChannelOperationError`, a 422), so the App MCP row cannot be deleted — a delete would be re-materialized on the next list or token verification with default values, turning "remove this" into "silently reset the kill switch to on". A second layer backs it in the database: the partial unique index `uq_server_channel_singleton_type` (`channel_type IN ('app_mcp')`, migration `867cacb5a827`). Note the interaction with the durable trace: `RoutingDecision.channel_id` is `ON DELETE CASCADE`, so deleting a channel takes its routing traces with it — which is part of why the guard matters now that App MCP writes traces.

### Capture hook placement

- Inbound (`channel_inbound_service.py`): verification failure (before the re-raise), ignored / added-to-space events, verified arrival, missing sender identity, whitelist denial, user-resolution denial, Pass-1 route, Pass-2 no-match, Pass-2 auto-install. **Every one of these is keyed on `channel.id` inside `process_inbound`, which is why the email transport needed no hooks of its own**: `ChannelPollService` feeds polled messages into the same method a verified webhook reaches, so email inherited the whole set when it was added. App MCP does not reach this method at all.
- Outbound (`channel_outbound_service.py` `_deliver`, and `channel_inbound_service.py` `_reply`): delivery success and failure
- Every hook is a standalone statement, never inside a conditional expression, so none can alter a branch

### Attachment fields

The wire format between the inbound pipeline and the admin panel for a message that carried [inbound channel attachments](server_channels.md#inbound-file-attachments). `ChannelDebugEvent.detail` is typed `dict[str, str]` — already part of the generated OpenAPI client — so this feature deliberately did not widen it; widening it would have rippled into `schemas.gen.ts` and cost the feature its "no client regeneration" property.

**Producer** (`_attachment_detail` in `channel_inbound_service.py`) writes, only on a message that carried at least one attachment:

- `attachments_accepted` — a stringified count.
- `attachments_skipped` — a stringified count, and the **authoritative** total: unlike the string below, it is never truncated.
- `attachment_skips` — present **only when at least one attachment was skipped** (a fully-accepted message gets no bare `attachment_skips=` line). One flattened string, entries separated by `"; "`, each `"filename (raw_reason_code)"`, e.g. `"report.mp4 (too_large); logo.svg (type_not_allowed)"`. Capped at 500 characters (`_MAX_SKIP_DETAIL_CHARS`) with a trailing `"…"` when cut. The **codes are raw**, not the sender-facing prose `_reason_phrase` renders elsewhere — the feed's whole value is telling the two failure families apart by exact token.

**Consumer** (`ChannelDebugDialog.tsx`'s `readAttachments` / `parseSkips`) parses that string back apart, entry-by-entry, and is written to be total over a string it does not control the shape of: a segment that doesn't match the expected `"name (code)"` pattern is carried through verbatim rather than dropped (a mangled row is more useful than a silently missing one), and when the string was truncated, an unmatched **trailing** segment is specifically dropped rather than rendered as a fake whole entry — a bare `report.m` fragment left by the character cap would otherwise read as a real, differently-named file. `attachments_skipped` is what the panel trusts for "how many" precisely because `attachment_skips` can under-name that count once truncated.

**The three-family split, and where it actually lives.** The panel groups codes into `refused` (the sender's to fix), `guidance` (also the sender's, but nothing broke — a resend just works), and `failed` (the operator's) via a hardcoded `SKIP_REASON_FAMILY` map in `ChannelDebugDialog.tsx`, plus an `other` bucket for anything not in the map (rendered under a neutral "Skipped" heading with the raw code, never guessed into one of the three). The map's own comment states it covers "all 14 codes any of them can currently emit."

**Discrepancy found while writing this documentation, recorded rather than silently corrected:** the backend's own `_reason_phrase` groups its codes into three matching comment blocks — "Refused by validation," "Failed to fetch or store," and "Guidance, not just a refusal" — and that third block lists exactly two codes, `drive_file` and `poll_budget_exhausted`. `fetch_budget_exhausted` sits in the backend's **second** ("Failed to fetch or store") block. In the frontend's `SKIP_REASON_FAMILY` map, `fetch_budget_exhausted` is **absent** from all three families — it is not in `refused`, `guidance`, or `failed`. At runtime a message skipped for that reason therefore falls to the `other` bucket (`"Skipped" / "unrecognised reason"`), not to `failed` as the backend's own grouping comment implies, and not to `guidance` either. This also means the map's "14 codes" tally is one short of the 15 the two backend modules can currently emit. The sender-facing text (`_reason_phrase` in `channel_inbound_service.py`) is unaffected — it always renders `fetch_budget_exhausted` as its own specific sentence ("there wasn't time to download it — please send fewer files at once") regardless of this gap; only the **admin panel's family grouping** for that one code is affected. This is a frontend code gap, not a docs-vs-plan drift — flagged here rather than silently assigned to a family in this documentation, per instructions to report a code/plan disagreement rather than paper over it.

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
- **Attachment filenames and skip reasons carry no new secret exposure.** They are sender-supplied text (filenames) and a fixed, code-owned vocabulary (reason codes), never attachment bytes, never a Chat media token or URL, and never the file's platform id. See [Attachment fields](#attachment-fields).
- **No new secret exposure.** No hook passes the encrypted secrets, the webhook token, the inbound bearer JWT or a minted access token. `detail` carries only pipeline stage markers plus the admin-authored whitelist, and — since Auto Routing Tuning — an optional `trace_id` (`channel_inbound_service.py`'s `_decision_detail`), a real key into the durable `routing_decision` table, published only when a row was actually written for that message (never emitted unconditionally, so the panel never links to a trace that 404s). The two send-failure hooks interpolate an exception whose string is method, URL and status — bearer tokens live in headers and signed assertions in request bodies, so neither appears; the identical string is already persisted to `binding.last_error`.
- **Verification failures capture nothing from the payload**, since it failed the check that would make it trustworthy.
- **Admin test sends are audited** (`SERVER_CHANNEL_TEST_SEND`) with the resolved thread and how it was targeted, but never the message body: `SecurityEvent` rows are broadly readable, and the durable record is needed precisely because the buffer is clearable.
- **Flood resistance:** consecutive identical events collapse into one counted row, so a caller repeating a request cannot evict the feed from the bounded ring. A flood of *varying* events still rolls the ring — that is what a ring is — but the common case (a retry loop, a redelivery storm) is bounded to a single row.
- **`list_recent_senders` is capped** at `_RECENT_SENDERS_SCAN_LIMIT` bindings (newest first, then deduped by address), so the picker cannot load an unbounded result set on a busy channel.
