# Webapp Chat — Context Management (Technical Details)

## File Locations

### Backend

- `backend/app/models/session.py` — `page_context: str | None` optional field on `MessageCreate`
- `backend/app/api/routes/webapp_chat.py` — `_PAGE_CONTEXT_MAX_CHARS` constant (10,000); truncation and dispatch in `send_chat_message_stream`
- `backend/app/services/session_service.py` — `send_session_message(page_context=...)` stores context in `message_metadata`
- `backend/app/services/message_service.py` — `_compute_context_diff()` function; `collect_pending_messages()` with three-tier injection logic

### Frontend

- `frontend/src/components/Webapp/WebappChatWidget.tsx` — `collectIframeContext()`, `buildPageContext()` functions; context collection on message send
- `frontend/src/routes/webapp/$webappToken.tsx` — declares `iframeRef` passed to chat widget for context bridge communication

### Agent Prompts

- `backend/app/env-templates/app_core_base/core/prompts/WEBAPP_BUILDING.md` — schema.org markup instructions and context bridge script specification for the building agent

### Tests

- `backend/tests/api/agents/agents_webapp_chat_test.py` — Section I (context metadata storage/injection) and Section J (context diff optimization)

## Database Schema

No dedicated table or migration required. `page_context` is stored inside the existing `message_metadata` JSON column on `session_message`.

## Frontend: Context Collection

### `collectIframeContext(iframeRef)` — `WebappChatWidget.tsx`

Sends `{ type: "request_page_context" }` via `postMessage` to the iframe's `contentWindow`. Listens for `{ type: "page_context_response" }` with a 500ms timeout. Returns `{ url, title, microdata }` on success, `null` on timeout or error. Guard: `event.source !== contentWindow` prevents responding to messages from other origins.

### `buildPageContext(iframeRef)` — `WebappChatWidget.tsx`

Orchestrates context collection:
1. Captures `window.getSelection()` (selected text, truncated at 2,000 chars)
2. Calls `collectIframeContext(iframeRef)` for schema.org microdata
3. Assembles result as JSON string; returns `undefined` if neither source produces data

## Route: Truncation and Dispatch

File: `backend/app/api/routes/webapp_chat.py`

`send_chat_message_stream` truncates incoming context via `_PAGE_CONTEXT_MAX_CHARS = 10_000` and passes it as a separate `page_context` kwarg to `SessionService.send_session_message` — never embedded in message content.

## Service: Metadata Storage

File: `backend/app/services/session_service.py`

`send_session_message` accepts `page_context: str | None`. When provided, stores it in `message_metadata["page_context"]` on the created `SessionMessage`. The `message.content` DB column always contains only the user's typed text.

## Service: Agent-Side Injection with Context Diff

File: `backend/app/services/message_service.py`

### `collect_pending_messages()` — Three-Tier Logic

Builds agent-bound message content with context diff optimization:

1. **Previous context lookup** — queries for the most recent already-sent user message (up to 20 messages scanned) in the same session that has `page_context` in its `message_metadata`
2. **Diff computation** — calls `_compute_context_diff(old_str, new_str)`
3. **Block injection** — based on the diff result:

| Situation | Injected block |
|---|---|
| No previous sent context (first message) | Full `<page_context>\n{json}\n</page_context>` |
| Context identical to previous | Nothing — block omitted entirely |
| Context changed | `<context_update>\n{diff_json}\n</context_update>` |
| JSON parse error on either side | Full `<page_context>` (fallback) |

The closure variable `prev_context_ref` (a single-element list) tracks the "last processed context" across multiple pending messages in one batch.

### Previous Context Lookup Query

Scans up to 20 recent sent messages to find the latest one with `page_context` in `message_metadata`:
- Filters: `session_id` match, `role = "user"`, `sent_to_agent_status = "sent"`, `sequence_number < first_pending_seq`
- Orders by `sequence_number DESC`, limits to 20

### `_compute_context_diff(old_context_str, new_context_str)` — Module-Level Function

Pure function, no DB access:
- Parses both strings as JSON
- Returns `None` if parsed objects are equal (unchanged context — fast path)
- Returns a dict with up to three keys: `"changed"`, `"added"`, `"removed"` — each only present when non-empty
- Raises `json.JSONDecodeError` on malformed input; callers handle and fall back to full injection

Example diff output:
```json
{
  "changed": {
    "selected_text": {"from": "$2.4M", "to": "$3.1M"}
  }
}
```

## Context Payload Format

The `page_context` string sent from the frontend:

```json
{
  "selected_text": "Q4 Revenue: $2.4M",
  "page": {
    "url": "https://…/api/v1/webapp/{token}/",
    "title": "Sales Dashboard"
  },
  "microdata": [
    {
      "type": "https://schema.org/QuantitativeValue",
      "properties": { "value": "2.4M", "unitText": "USD" }
    }
  ]
}
```

`selected_text` included only if user has text selected. `page` and `microdata` included only if iframe context collection succeeds. If none present, `buildPageContext()` returns `undefined` and no `page_context` field is sent.

## Context Bridge Script

File: `backend/app/env-templates/app_core_base/core/prompts/WEBAPP_BUILDING.md`

The building agent creates `webapp/assets/context-bridge.js` and includes it in every HTML page. The script:
1. Registers a `message` event listener on `window`
2. On receiving `{ type: "request_page_context" }`, collects all `[itemscope]` elements and their `[itemprop]` child values into a structured array
3. Responds with `{ type: "page_context_response", context: { url, title, microdata } }` back to the requesting window

## Tests

### Section I — Page Context Metadata Storage and Injection

- `test_webapp_chat_message_with_page_context_stores_clean_content` — verifies `send_session_message` receives clean user text as `content` and context string as separate `page_context` kwarg
- `test_webapp_chat_message_without_page_context_unchanged` — regression: no page_context means content unchanged, `page_context` kwarg is None
- `test_webapp_chat_message_page_context_truncated_at_limit` — oversized context truncated to 10,000 chars in `page_context` kwarg
- `test_webapp_chat_message_content_stored_clean_no_xml_in_message_list` — end-to-end: sends real message with page_context, verifies stored content has no XML

Tests I.1-I.3 patch `SessionService.send_session_message` with `AsyncMock` and capture kwargs. Test I.4 patches `SessionService.initiate_stream` only and verifies via messages API.

### Section J — Context Diff Optimization

- `test_context_diff_first_message_sends_full_page_context` — first message must forward full `<page_context>` block
- `test_context_diff_identical_context_omits_block` — identical context must produce no block
- `test_context_diff_changed_context_sends_diff_block` — changed field must produce `<context_update>` with diff JSON

Section J tests use `StubAgentEnvConnector` patched at `app.services.message_service.agent_env_connector` and inspect `stub.stream_calls[0]["payload"]["message"]`. Background tasks drained via `drain_tasks()`.

---

*Last updated: 2026-03-08 — extracted as standalone aspect document from webapp_chat_tech.md*
