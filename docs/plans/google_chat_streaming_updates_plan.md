# Google Chat Streaming Updates + `/stop` Channel Control Command — Implementation Plan

**Status: PLANNED — not implemented.** One draft file exists (Phase 1, see below); everything else is untouched.

## 1. Feature summary

Today a Google Chat channel turn is silent while the agent works: the status notice says
"💬 Working on your message…" until `STREAM_COMPLETED`, then the whole reply lands at once
(`ChannelOutboundService.handle_stream_completed` → `_deliver(into_status_notice=True)`).

This feature adds:

1. **Live streaming updates**: while the agent streams, the status notice becomes a **rolling
   draft** — the same message is rewritten in place (debounced, ~every 3s) with the accumulated
   assistant text. When the draft approaches Chat's per-message size cap, it is **sealed** (rewritten
   one final time, cut at a good boundary) and a fresh draft message continues below it. The final
   delivery patches only the remaining tail into the current draft. Thinking blocks are never sent
   (`thinking` is a distinct stream event type from `assistant`). Tool-activity narration is
   **deferred** (decided, not in v1).
2. **`/stop` channel control command**: a sender typing `/stop` into a bound Google Chat thread
   interrupts the active stream — the channel-side equivalent of the web UI stop button. Built as a
   small extensible **channel control command** registry (future commands slot in without touching
   the pipeline).

UX contract (user-confirmed):
- Feels responsive like the web UI, but never "1 sentence = 1 message".
- Messages split only at good boundaries (paragraph > newline), never inside a code fence, never
  between the rows of one table (Chat renders tables as aligned monospace blocks — a split table is
  two separately-aligned blocks).
- The Chat hard limit in this codebase is **4096 chars** (`_MAX_MESSAGE_CHARS`,
  `backend/app/services/server_channels/adapters/google_chat.py:92`) — not 2000.

Decisions already made with the user (do NOT re-litigate):
- **Finalize divergence policy**: final tail delivery uses the **relay's own accumulated text**, not
  the stored `SessionMessage` content (which differs after webapp_action-tag stripping /
  `<cinna_attach>` materialization). Mismatches only logged.
- **Tool-activity narration in the draft**: deferred, not in v1.
- **Interrupt UX**: `/stop` command (this plan, Phase 4); interrupted turns settle the draft with the
  partial text + a stopped marker.
- **No DB migration.** `ChannelThreadBinding.status_message_id` (exists) is the only persisted
  pointer. Relay state is in-memory — legitimately so, because the relay, the stream, and the
  event emission all share one process and one task lifetime; if the process dies the stream dies
  with it, so there is no completion left to mis-reconcile. (This is NOT the ephemeral-cache trap —
  see the reasoning in §7.)
- **Email / App MCP unaffected**: gate on `capabilities.supports_status_notice` (email declares no
  progress surface; App MCP replies synchronously).

## 2. Read first

- `docs/README.md` → Server Channels; then `docs/application/server_channels/server_channels.md`
  (especially "The status notice" and "Formatting" sections) and `server_channels_tech.md`.
- `backend/app/services/server_channels/channel_outbound_service.py` — read the whole file,
  including docstrings; the never-raise / expired-instance discipline documented there is
  **load-bearing** and every new code path must follow it (total helpers `_binding_thread_key`,
  `_binding_status_message_id`, `_persist_status_message_id`; §11a Rule 2: no unguarded attribute
  reads or f-string interpolations in exception-handler argument lists).
- `backend/app/services/server_channels/adapters/google_chat.py` — `send_message`,
  `replace_message` (the `ChannelReplaceResult.replaced` ownership contract), `update_message`
  (truncates, never chunks), `_chunk`, `_request_with_retries` (429 backoff exists).
- `backend/app/services/server_channels/adapters/google_chat_format.py` — `markdown_to_chat` is
  total (never raises) and safe on partial markdown; each patch replaces the full draft text so
  transient artifacts self-heal.
- `backend/app/services/sessions/stream_processor.py` — `StreamEventHandler` protocol (the designed
  extension seam), `SessionStreamProcessor._process_inner`.
- `backend/app/services/sessions/stream_event_handlers.py` — `WebSocketEventHandler` (the handler
  channel sessions currently ride).
- `backend/app/services/sessions/message_service.py` — `process_pending_messages` (~line 1377,
  where `WebSocketEventHandler` is constructed — the hook point), `stream_message_with_events`,
  `interrupt_stream` (~line 1640), `_finalize_agent_message`.
- `backend/app/services/server_channels/channel_inbound_service.py` — step 7 binding dispatch
  (~line 1443), `_continue_thread` (~line 1715), `_reply` (~line 3873), `_settle_notice`
  (~line 2439), `_status_notice_supported` (~line 205), REPLY_* constants (~line 320).
- `backend/tests/README.md`, then `backend/tests/api/server_channels/server_channels_status_notice_test.py`
  (mocking conventions: `_Chat` class patching the four adapter verbs, `StubAgentEnvConnector`,
  `drain_tasks`) and `backend/tests/unit/test_google_chat_adapter_chunk.py`.

## 3. Stream-event facts the implementation depends on (verified in code)

- Stream events are dicts with `type` ∈ {`assistant`, `thinking`, `tool`, `attachment`, `error`,
  `done`, `session_created`, `interrupted`, …}. Assistant text = concatenation of `content` of
  `assistant` events (no separator) — `stream_processor.py` accumulates exactly this way.
  OpenCode flushes per newline; Claude Code per content block — a time-debounced flush coalesces
  both (cf. `_coalesce_assistant_events` at finalize, memory: "OpenCode per-newline markdown break").
- `STREAM_COMPLETED` (bus event) is emitted **inside** `stream_message_with_events` at the end of
  each LLM batch (`message_service.py` ~line 2898), i.e. **possibly more than once per turn** for a
  multi-batch turn, and **before** the processor's `on_complete`. It is **NOT emitted when the
  stream was interrupted** (`if not was_interrupted:` guard ~line 2886).
- On interrupt, `STREAM_INTERRUPTED` is emitted instead (~line 2584). `ChannelOutboundService`
  currently subscribes only to `STREAM_COMPLETED` + `STREAM_ERROR` (`app/main.py` ~line 352) — an
  interrupted channel turn today leaves the notice stranded on "working…" until the next turn.
- Channel sessions stream via `MessageService.process_pending_messages` (background task started by
  `send_session_message(initiate_streaming=True)`), which builds a `WebSocketEventHandler` and a
  `SessionStreamProcessor`. Both channel-initiated turns and web-UI turns on the same session go
  through this one path.
- Interrupt seam: `MessageService.interrupt_stream(db_session, session_id, environment_id)` —
  **caller must authorize access first** (documented contract in its docstring); raises
  `ValueError` when there is nothing to interrupt.

## 4. Phases

Each phase = one developer-agent task, code-reviewed, with tests passing before the next phase.

### Phase 1 — shared fence-aware splitter (DRAFT EXISTS — review & finish)

`backend/app/services/server_channels/adapters/chat_text_chunking.py` **already exists as an
unreviewed draft** written during planning. Treat it as input, not as done work: review it,
fix anything wrong, and wire it in.

- Module exposes `chunk_text(text, limit)`, `fence_open_after(piece, open_before)` (both moved
  verbatim-in-behavior from `GoogleChatAdapter._chunk` / `_fence_open_after`), and
  `find_seal_boundary(text, window) -> int | None` (new: best cut offset — last paragraph break
  outside a fence, else last newline outside a fence; walks a cut back off a pipe-table split
  unless that surrenders > half the window; returns `None` for "no acceptable boundary — don't
  seal yet"; returned offset consumes the boundary newline(s); remainder always non-empty).
  **The draft's `find_seal_boundary` / `_back_out_of_table` offset arithmetic needs careful
  review and thorough unit tests — it was written without tests.**
- `GoogleChatAdapter._chunk` and `._fence_open_after` become thin delegates so the existing unit
  tests (`tests/unit/test_google_chat_adapter_chunk.py`) pass **unchanged** — do not edit that
  test file; it is the behavior pin for the extraction.
- New unit tests: `tests/unit/test_chat_text_chunking.py` for `find_seal_boundary` (paragraph
  preference, fence avoidance, table walk-back, table-split acceptance when the table opened
  early, no-boundary → None, boundary-at-end → None, window larger than text → None).

### Phase 2 — settings + `ChannelStreamRelay` + registry + stream tee

**Settings** (`backend/app/core/config.py`, in the "Server channels" section ~line 441):
- `CHANNEL_STREAM_UPDATES_ENABLED: bool = True` — global kill switch.
- `CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS: float = 3.0` — min seconds between draft patches.
  **A value <= 0 means "flush immediately on every event"** — that is what tests set so they never
  sleep; document this in the setting's comment. Default is conservative because Chat's write quota
  (~60/min) is per **space**, shared by all threads in a group space.
- `CHANNEL_STREAM_SEAL_TARGET_CHARS: int = 3400` — soft seal threshold on the **translated** text,
  comfortably under the 4096 hard cap.

**New module** `backend/app/services/server_channels/channel_stream_relay.py`:

- `class ChannelStreamRelay` — holds plain ids only (`session_id`, `binding_id`, `channel_id`) plus
  `get_fresh_db_session`; DB rows are re-fetched per flush (instances expire across commits — same
  hazard the outbound service documents at length).
  - `feed(text: str)` — append raw markdown to the accumulation buffer; wake the flusher.
  - Flusher: single background asyncio task; first flush immediate, then rate-limited to the
    interval (track `last_flush_time`; sleep only the remainder). All work inside try/except —
    a relay failure must NEVER propagate into (or slow) the stream it tees off.
  - `_flush()` (under an internal `asyncio.Lock` shared with `take_tail`):
    1. `suffix = self._fence_prefix + raw[self._sealed_offset:]` (`_fence_prefix` is `"```\n"`
       only after a forced mid-fence seal, else `""` — see below).
    2. Measure `len(markdown_to_chat(suffix))`. While it exceeds
       `CHANNEL_STREAM_SEAL_TARGET_CHARS`: call `find_seal_boundary(suffix, window)` (window in
       raw chars ≈ the target; if the *translated* sealed slice still exceeds 4096 − margin,
       halve the window and retry, a few iterations max).
       - Boundary found → **seal**: `ChannelOutboundService.set_binding_status(db, channel,
         binding, text=sealed_slice_markdown, settle=True)` (settle = rewrite one last time and
         drop the id — exactly the existing verb), advance `_sealed_offset`, clear/set
         `_fence_prefix`, recompute `suffix`, loop.
       - No boundary and translated length still < ~4000 → defer (keep growing; `update_message`
         truncates at 4096 as a transient cosmetic backstop).
       - No boundary and ≥ ~4000 (forced) → cut at the last newline in-window even inside a
         fence; if the cut leaves a fence open, append `\n```` ``` ```` to the sealed slice and set
         `_fence_prefix = "```\n"` so the next suffix re-opens it (the remainder's original
         closing fence then correctly closes the re-opened block).
    3. Patch the draft: `ChannelOutboundService.set_binding_status(db, channel, binding,
       text=suffix)` — this **reuses** the existing verb wholesale: patches the current
       `status_message_id`, posts a fresh message when there is none (e.g. right after a seal),
       persists the id, degrades a failed patch to a fresh post, and is total. The relay adds
       only debounce + seal decisions on top of the three existing verbs.
  - `take_tail() -> tuple[str, bool]` — under the same lock: stop implication none (idempotent);
    returns `(raw[self._sealed_offset:] with _fence_prefix applied, delivered_anything_flag)` and
    advances `_sealed_offset` to the end. Called by the outbound handlers (Phase 3). A second call
    returns `("", …)` — that idempotency is what makes multi-batch turns correct (each per-batch
    `STREAM_COMPLETED` delivers that batch's increment).
  - `stop()` — cancel/stop the flusher task; called from `on_complete`/`on_error` and from
    `take_tail`'s callers. Idempotent.
- `ChannelStreamRegistry` — module-level dict `session_id → relay`, same bounded-eviction pattern
  as `stream_processor._session_locks` (evict stopped relays beyond ~500 entries). **Entries are
  replaced (or explicitly removed) at the start of each channel turn, never popped by the
  consumers** — a per-batch `STREAM_COMPLETED` must still find the relay, and a turn where the
  feature got disabled must find the entry *removed* (else the fallback full-text delivery would
  be skipped and the reply lost — this exact hazard is why `maybe_attach` must remove stale
  entries when it declines to attach).
- `maybe_attach_channel_relay(session_id, integration_type, base_handler, get_fresh_db_session)
  -> StreamEventHandler` — returns `base_handler` untouched unless ALL of: setting enabled,
  `integration_type.startswith("channel_")`, a `ChannelThreadBinding` exists for the session, the
  channel row resolves + is enabled, and `get_adapter(channel.channel_type)
  .capabilities.supports_status_notice`. When declining for a channel session, **remove** any
  registry entry for the session. When attaching: build relay, register (replacing any prior
  entry), and return a `CompositeStreamEventHandler([base_handler, relay_handler])`.
  Must itself be total — any exception → log + return `base_handler`.
- `CompositeStreamEventHandler` — fans each protocol method out to its children, isolating
  exceptions per child (a relay bug must not break Socket.IO emission, and vice versa). Put it in
  `stream_event_handlers.py` (it is generic) with the relay-side handler in the relay module.
- Relay-side handler: `on_event` → `relay.feed(content)` only for `type == "assistant"` with
  non-empty content; `on_complete`/`on_error` → `relay.stop()`; `on_stream_starting` → no-op.

**Hook** in `MessageService.process_pending_messages` (~line 1449, where
`handler = WebSocketEventHandler(...)` is built): read the session's `integration_type` in the
already-open DB block, then wrap via `maybe_attach_channel_relay` (function-level import from
`app.services.server_channels.channel_stream_relay` — the established circular-import dodge).
Keep the change minimal; everything channel-aware lives in the relay module.

### Phase 3 — outbound integration (tail delivery + interrupted handler)

In `backend/app/services/server_channels/channel_outbound_service.py`:

- `handle_stream_completed`: after `_resolve_channel_session`, look up the relay
  (`ChannelStreamRegistry.get(session_id)` — do NOT pop).
  - Relay present: `tail, delivered_any = await relay.take_tail()`; `relay.stop()`.
    - `tail` non-empty → `_deliver(db, channel, binding, text=tail, into_status_notice=True)`
      (chunks via `replace_message` if the tail is huge — existing machinery).
    - `tail` empty and `delivered_any` → no-op (everything already on screen; draft id already
      released by the last settle or will be patched next turn — leave it to the existing
      self-heal).
    - `tail` empty and not `delivered_any` → the stream produced nothing:
      `clear_binding_status` exactly as today.
  - Relay absent: today's `_last_agent_message` full-text path, byte-for-byte.
- `handle_stream_error`: relay present → `tail = take_tail()`; deliver
  `tail + "\n\n" + <existing generic error text>` (or just the error text when tail is empty)
  into the notice; relay absent → unchanged.
- **New** `handle_stream_interrupted(event_data)`: same gate/lookup shape as the other two.
  Relay present → deliver `tail + "\n\n⏹️ _Stopped._"` (tail empty → settle the notice as
  `"⏹️ Stopped."` via `set_binding_status(..., settle=True)`). Relay absent → settle the notice
  as `"⏹️ Stopped."` **only if** the binding currently has a `status_message_id` (don't post a
  new message into a thread that wasn't narrating). This is what gives `/stop` its visible
  acknowledgement.
- Register the new handler in `app/main.py` next to the existing two (~line 352):
  `EventType.STREAM_INTERRUPTED → ChannelOutboundService.handle_stream_interrupted`.
- Every new read of `binding.*`/`channel.*` in these paths goes through the existing total
  helpers or an equivalent guard — re-read the file's §11a documentation before writing.

### Phase 4 — `/stop` channel control command

New module `backend/app/services/server_channels/channel_control_commands.py`:

- A small registry mapping normalized command text → handler coroutine; v1 has exactly one entry.
  `match_control_command(text: str) -> str | None` — strip + casefold; matches `"/stop"` exactly
  (no arguments). Extensible by adding entries, no pipeline change (mirror the adapter-registry
  philosophy).
- `async def handle_stop(...)`: given `binding_id`, opens its own session (`create_session()`),
  re-fetches binding + channel; resolves `binding.session_id`; loads the `Session` row for
  `environment_id`; calls `MessageService.interrupt_stream(db, session_id, environment_id)`.
  - Success → **no reply** (the `STREAM_INTERRUPTED` handler from Phase 3 settles the draft with
    "⏹️ Stopped." — that IS the acknowledgement; a second message would double it).
  - `ValueError` ("no active stream") or no bound session → reply via
    `ChannelInboundService._reply(db, channel, binding.thread_key, REPLY_NOTHING_TO_STOP)` with a
    new constant `REPLY_NOTHING_TO_STOP = "There's nothing running right now."` (add next to the
    other REPLY_* constants with a matching comment — it reveals nothing about the server).
  - Total: every failure logged, never raised (this runs as a background task).

Interception in `channel_inbound_service.process_inbound`, step 7 (~line 1478): **after** the
thread-ownership decline and **before** the `CHANNEL_BINDING_ACTIVE` dispatch — i.e. only for
messages on an existing binding, from the thread's owner, already past every security gate
(rate limit, verify, whitelist, policy):

```
command = match_control_command(inbound.text)
if command is not None and not file_ids:
    ChannelInboundService._schedule(execute_control_command(command, binding_id=binding_id), "channel_control_command")
    return {}
```

- Applies to ACTIVE **and** PENDING_INSTALL bindings (on pending there is nothing to stop →
  the handler's "nothing running" reply; the `/stop` must NOT be parked as a message).
- A `/stop` as the *first* message of a brand-new thread is deliberately NOT intercepted — there
  is no binding and nothing to stop; it falls through to routing like any text (documented
  behavior, add to docs).
- `/stop` with attachments (`file_ids` non-empty) is treated as an ordinary message, not a
  command — matching the guard above.
- Authorization argument (state it in a comment): the ownership gate one screen up already
  enforced `binding_user_id == user_id`, and `interrupt_stream`'s documented contract is
  "caller authorizes"; the sender interrupting their own thread's stream satisfies it — including
  on an identity-routed thread, where the *conversation* is still the sender's even though the
  session lives in the identity owner's workspace.
- Also record the interception in the channel debug buffer (kind: reuse `DEBUG_REPLIED`-style
  outbound record or an inbound "control command" summary — follow whatever
  `channel_debug_buffer.py` supports without new kinds if possible).

### Phase 5 — tests

Unit (`backend/tests/unit/`, `asyncio.run` style — no pytest-asyncio marker in this suite):
- `test_chat_text_chunking.py` (Phase 1, listed there).
- `test_channel_stream_relay.py`: relay with mocked `ChannelOutboundService.set_binding_status` —
  interval <= 0; feed small text → one patch with translated suffix; feed past seal target →
  a settle call with the sealed slice + a fresh-draft patch with the remainder; `take_tail`
  idempotency (second call empty); forced mid-fence seal sets/uses the fence prefix; a raising
  outbound verb neither propagates nor stops subsequent flushes.
- `test_channel_control_commands.py`: matching (`"/stop"`, `" /STOP "`, rejection of
  `"/stopx"`, `"stop"`, `"/stop now"`).

API (`backend/tests/api/server_channels/`, reusing the `_Chat` mock + `StubAgentEnvConnector` +
`drain_tasks` conventions from `server_channels_status_notice_test.py`; set
`CHANNEL_STREAM_UPDATE_INTERVAL_SECONDS = 0` via the settings-override fixture pattern used in
that directory):
- `server_channels_streaming_updates_test.py`:
  - a stream yielding several `assistant` events → `update_message` called with partial
    (translated) text **before** completion; final text delivered into the notice; id released.
  - `thinking` events never appear in any outbound text.
  - a stream long enough to seal → the settle/patch/new-draft sequence, and
    `binding.status_message_id` repointed.
  - feature disabled (settings override) → behavior identical to today (single final
    `replace_message`, no intermediate `update_message` beyond the pipeline's own notices).
  - stream error mid-way → partial tail + error text delivered.
- `/stop` test (same file or `server_channels_stop_command_test.py`):
  - `/stop` on an active binding with a live (stubbed) stream → `interrupt_stream` invoked
    (patch it), no LLM ingest of the `/stop` text, `{}` webhook ack.
  - `/stop` with nothing running → the "nothing running" reply via `send_message`.
  - `/stop` from a non-owner in someone else's thread → existing `REPLY_THREAD_OWNED` decline
    (i.e. interception sits AFTER the ownership gate — pin the ordering).
  - `STREAM_INTERRUPTED` handler settles the notice with the stopped marker.

Regression scope: `tests/unit/test_google_chat_adapter_chunk.py` must pass unchanged; then
`docker compose exec backend python -m pytest tests/api/server_channels/ tests/unit/ -v`
(services via `docker compose up -d` first). Full-suite run at the end.

### Phase 6 — documentation

- `docs/application/server_channels/server_channels.md`: extend "The status notice" (the notice
  now streams — rolling draft, sealing, one-message feel preserved), "Formatting" (seal
  boundaries), a new short "Channel control commands" subsection (`/stop`: scope, first-message
  behavior, why declines/acks look the way they do), and Known Limitations (per-space write
  quota sharing; multi-worker double-patch rides the existing single-process limitation).
- `docs/application/server_channels/server_channels_tech.md`: relay/registry mechanics, the
  settings, the `STREAM_INTERRUPTED` subscription, control-command registry.
- Keep `docs/README.md` untouched unless the feature map needs a one-line touch (no new feature
  row — this extends Server Channels). **No feature-count columns** (standing rule).

## 5. Invariants that must survive review (summarized from code reading)

1. Relay failures never propagate into the stream, the Socket.IO handler, or the event bus.
2. Every outbound helper stays total: no unguarded ORM attribute reads after a commit, no
   raw `f"{exc}"` in handler argument lists (`channel_outbound_service.py` documents why).
3. `ChannelReplaceResult.replaced` semantics untouched: the notice id is released only when the
   patch really landed.
4. Registry entries are replaced/removed at turn start by `maybe_attach_channel_relay`, never
   popped by consumers; `take_tail` is idempotent (multi-batch `STREAM_COMPLETED` correctness).
5. Relay-absent paths in all three outbound handlers are byte-for-byte today's behavior.
6. `/stop` interception runs strictly after the thread-ownership gate and channel-policy gate,
   and only when a binding exists.
7. The email transport and App MCP see zero behavior change (capability gate).
8. No DB migration, no OpenAPI client regeneration (nothing request/response-shaped changes) —
   if either seems needed, the design drifted; stop and flag it.

## 6. Out of scope (explicitly)

- Tool-activity narration in the draft (deferred by decision).
- Any per-channel admin toggle (global settings only for v1).
- Durable outbound queue for webhook transports (pre-existing limitation, unchanged).
- Multi-worker coordination (documented single-process assumption, unchanged).
- Frontend changes (none needed).

## 7. Why in-memory relay state is safe (for the reviewer)

The relay is created by `process_pending_messages`, lives for one turn, and is consumed by bus
handlers in the same process. The stream itself runs in this process; a crash kills both the
stream and the relay together, so no completion event can arrive that needs relay state which no
longer exists. A stranded draft after a crash is healed by the existing next-turn
"working on your message…" patch of `binding.status_message_id`. This differs from the
plugin-marketplace ephemeral-cache failure (state assumed durable across restarts); here the
state's lifetime is designed to equal the lifetime of the thing it describes.
