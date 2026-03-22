# OpenCode Adapter — In-Progress Work

## Problem

The OpenCode SDK adapter (`opencode/*` variants) was newly added but non-functional. Multiple issues needed fixing to get end-to-end message streaming working between the platform backend and `opencode serve`.

## What Was Fixed

### 1. Binary not found
- `opencode` binary installed to `/root/.opencode/bin/` but not in container PATH
- **Fix**: Added `/root/.opencode/bin` to `ENV PATH` in all three Dockerfiles

### 2. Read-only filesystem
- `/app/core` mounted as `:ro` but adapter writes `session_context.json` and `AGENTS.md` at runtime
- **Fix**: Runtime writes go to per-mode writable dirs (`/tmp/.opencode_building`, `/tmp/.opencode_conversation`). Static configs symlinked from read-only dir on startup.

### 3. SSE event envelope
- OpenCode wraps SSE events in `{"payload": {...}}` — adapter expected flat structure
- **Fix**: `_parse_sse_event` unwraps the `payload` envelope

### 4. Event structure mismatch
- OpenCode uses flat per-event model (`message.part.delta`, `message.part.updated`, `session.idle`) not the nested session/messages structure the adapter expected
- **Fix**: Complete rewrite of event translation (see `opencode_event_adapter.py`)

### 5. SSE timing — events missed
- SSE stream opened AFTER posting message — fast models (GPT-4o-mini) finished before SSE connected
- **Fix**: Connect SSE first, post message on first event received

### 6. POST blocks SSE loop
- `POST /session/{id}/message` blocks until LLM finishes. Awaiting it inline deadlocked the SSE event loop.
- **Fix**: Fire POST as `asyncio.create_task()`, clean up in finally block

### 7. Session resume — no `server.connected`
- On second message, opencode sends `project.updated` instead of `server.connected`. Adapter waited forever.
- **Fix**: Trigger message post on first event of ANY type, not just `server.connected`

### 8. Tool event structure
- Tool parts have `type=tool` (not `tool-invocation`), with `state` as a dict `{"status": "pending", "input": {...}}` not a plain string
- **Fix**: Updated `_handle_tool_part` to parse nested state structure

### 9. Text streaming — extra newlines
- Each small text delta rendered as a separate `<MarkdownRenderer>` in the frontend, adding paragraph spacing
- **Fix**: Newline-based buffering — accumulate deltas, flush complete lines, emit remainder on part completion

### 10. Permission blocking
- OpenCode asks `permission.asked` for `external_directory` when accessing paths outside cwd
- **Fix (config)**: Per-mode `opencode.json` with `external_directory` rules pre-approving `/app/workspace/**`, `/app/**`, `/tmp/**`
- **Fix (runtime fallback)**: Any unapproved permission forwarded to UI as `SDKEventType.SYSTEM` with `subtype=permission_asked` and human-readable content

### 11. Tool hang — session stuck forever
- OpenCode's `read` tool hangs when given a directory path instead of a file
- Heartbeats keep the SSE socket alive, so `sock_read=300` timeout never triggers
- Session shows "Streaming..." indefinitely in the frontend
- **Fix**: Added `OPENCODE_PROGRESS_TIMEOUT` (120s) — tracks the last *meaningful* event (text, tool, session.idle, etc.) and aborts if only heartbeats come for too long. Also `DELETE`s the OpenCode session to clean up the hung process.

### 12. Permission events invisible in UI
- `permission.asked` events were translated to `SDKEvent(type=SYSTEM, content="")` with empty content
- Frontend `StreamEventRenderer.tsx` checks `event.content.trim()` — empty content → `null` → nothing rendered
- **Fix**: `_handle_permission_asked` now generates a human-readable summary as `content` (e.g. "Permission requested: external_directory for /app/workspace/scripts/*")

### 13. Permissions still asked despite config rules
- Per-mode configs (`building_config.json`, `conversation_config.json`) contained permission rules, but these were our custom files only read by the adapter for model selection
- OpenCode itself reads `opencode.json` from its cwd — which was never generated
- **Fix**: Now generates per-mode `opencode.json` files in `/app/core/.opencode/{mode}/` directories

### 14. Reasoning events not forwarded as thinking
- OpenCode sends `reasoning` parts with chain-of-thought text, but `_handle_part_delta` only processed `text` parts
- `_handle_part_updated` had a comment "step-start, reasoning — skip" that silently dropped completed reasoning parts
- **Fix**: Both `_handle_part_delta` and `_handle_part_updated` now handle `reasoning` parts, emitting them as `SDKEventType.THINKING` events with the same newline buffering strategy as text

### 15. Per-mode server isolation
- Single shared `opencode serve` process on port 4096 caused race conditions when building and conversation modes ran concurrently (model set globally via `PATCH /config`)
- **Fix**: Each mode now runs its own `opencode serve` instance on a dedicated port (building: 4096, conversation: 4097) with its own runtime dir and config. No shared state, no race conditions. Removed `PATCH /config` entirely — model baked into per-mode `opencode.json`.

### 16. Model not found errors
- OpenCode has a built-in model registry and rejects unknown model IDs (e.g. `gpt-5.4-nano`)
- **Fix**: Added `provider` section to `opencode.json` that explicitly registers the selected model with OpenCode (e.g. `"provider": {"openai": {"models": {"gpt-5.4-nano": {"name": "gpt-5.4-nano"}}}}`)

### 17. API key not reaching OpenCode
- Auth was written to `auth.json` beside `opencode.json`, but OpenCode reads credentials from `~/.local/share/opencode/auth.json` — our file was ignored
- Tried `{env:OPENAI_API_KEY}` references but env vars weren't in the container, and existing containers needed restart
- **Fix**: API key written directly into the `provider.options.apiKey` field in `opencode.json`. Config file permissions set to `0o600`.
- Also added `OPENAI_API_KEY` and `GOOGLE_API_KEY` to the container `.env` template for other uses.

## Architecture

### Per-mode server instances

| | Building | Conversation |
|---|---|---|
| Port | 4096 | 4097 |
| Config source | `/app/core/.opencode/building/opencode.json` | `/app/core/.opencode/conversation/opencode.json` |
| Runtime dir | `/tmp/.opencode_building/` | `/tmp/.opencode_conversation/` |
| Model | Baked into config | Baked into config |

Each adapter instance (cached per mode by `SDKManager`) manages its own `opencode serve` process. No shared state between modes.

## Key Files

### Adapter code (inside agent container)
- `backend/app/env-templates/app_core_base/core/server/adapters/opencode_adapter.py` — main adapter (per-mode process mgmt, HTTP, SSE loop, progress timeout)
- `backend/app/env-templates/app_core_base/core/server/adapters/opencode_event_adapter.py` — event translation (text/reasoning buffering, tool events, permissions) + JSONL logging
- `backend/app/env-templates/app_core_base/core/server/adapters/base.py` — `SDKEvent`, `SDKEventType` definitions
- `backend/app/env-templates/app_core_base/core/server/tools/mcp_bridge/` — MCP bridge servers (read `session_context.json` from cwd)

### Config generation (backend)
- `backend/app/services/environment_lifecycle.py` — `_generate_opencode_config_files()` — generates per-mode `opencode.json` with model, provider registration, API key, permissions, tools, MCP bridges

### Dockerfiles
- `backend/app/env-templates/general-env/Dockerfile`
- `backend/app/env-templates/general-assistant-env/Dockerfile`
- `backend/app/env-templates/python-env-advanced/Dockerfile`

### Tests
- `backend/tests/unit/test_opencode_event_adapter.py` — **43 tests** for the event adapter

### Real captured logs (for test development)
- `backend/agent-environments/91a6340c-fec1-4b44-af8b-cf84a269f673/app/workspace/logs/opencode_session_*.jsonl`
- JSONL format, bi-directional: `{"ts": "...", "dir": "send"|"recv", "event": {...}}`
- Enable with `DUMP_LLM_SESSION=true` in docker-compose environment

### Documentation
- `docs/agents/agent_environment_core/multi_sdk.md` / `multi_sdk_tech.md` — multi-SDK architecture
- `docs/agents/agent_environment_core/tools_approval_management.md` / `_tech.md` — tool approval system
- OpenCode permissions docs: https://opencode.ai/docs/permissions/
- OpenCode SDK docs: https://opencode.ai/docs/sdk/

## Test Architecture

Tests use the `OpenCodeEventAdapter` class in isolation — no HTTP, no async, no Docker needed.

```python
adapter = OpenCodeEventAdapter("/tmp/test")
events = adapter.translate(raw_event_dict, session_id)
# events is list[SDKEvent]
```

Test categories (43 tests total):
- **Informational events** (10) — verify silent skipping (8 event types + unknown + project.updated)
- **Session completion** (2) — `session.idle` → DONE, with buffer flush
- **Error events** (2) — error in type name or properties
- **Text streaming** (5) — newline buffering, flush on `\n`, flush on part complete, reasoning buffered, reasoning flushed as THINKING
- **Tool events** (4) — pending (skipped), running (TOOL_USE), completed (TOOL_RESULT), error
- **Permission events** (3) — forwarded as SYSTEM with non-empty content, request ID, tool context, patterns
- **Step events** (2) — step-start and step-finish silently skipped
- **Conversation replays** (10) — full event sequences: text-only, tool use, tool use with permission, session resume, multi-turn
- **Real session replays** (5) — from captured JSONL: reasoning with OpenAI metadata, reasoning deltas with newlines, parallel tool calls, step-finish with "tool-calls" reason, full session end-to-end

Run tests:
```bash
cd backend && source .venv/bin/activate
python -m pytest tests/unit/test_opencode_event_adapter.py -v --noconftest
```

## Still TODO

- Frontend handling of `permission_asked` SYSTEM events (show approval UI with approve/deny buttons, call `POST /permission/{id}/reply`)
- OpenCode `question.asked` events (similar to permission but for user questions — not yet observed)
- Verify tool approval integration with existing `tools_approval_management` system for plugin tools
- Test with longer conversations and multiple tool calls in sequence
- OpenAI Compatible provider support — verify `openai_compatible` provider config generates correct `npm` package reference (`@ai-sdk/openai-compatible`)
