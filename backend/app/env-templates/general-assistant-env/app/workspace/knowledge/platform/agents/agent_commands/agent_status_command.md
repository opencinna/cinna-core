# /agent-status Command

## Purpose

Returns the agent's self-reported status from `STATUS.md` in the workspace root. Provides an instant, LLM-free way for users and A2A clients to check what a complex agent is currently doing — without streaming a full session message.

## Core Concepts

- **`STATUS.md`** — A convention file at `/app/workspace/STATUS.md` written by the agent or its scripts to publish current state. Content is freeform markdown; an optional YAML frontmatter header enables structured parsing.
- **Severity** — Parsed from frontmatter `status` field. One of `ok`, `warning`, `error`, `info`, or `unknown` (default when missing or unrecognized).
- **Summary** — Short description of current state. Taken from frontmatter `summary` field, or derived from the first non-blank, non-heading line of the body if no frontmatter.
- **Reported At** — Timestamp for the status. Taken from frontmatter `timestamp` when present; falls back to the file's last-modification time; `null` only when both are unavailable.
- **Cached snapshot** — The last-fetched status is persisted to the `agent_environment` table so it remains visible even when the environment is stopped.
- **Stale indicator** — Status is considered stale when the environment is not running or the snapshot is older than 10 minutes.

## Structured Format (Optional Frontmatter)

```markdown
---
timestamp: 2026-04-19T14:32:05Z
status: ok
summary: Invoice poll caught up; 0 pending items
---

# Full detail body (freeform markdown)

## Now
- Inbox polling every 10 min — last ran 14:30 UTC, 0 unread.
```

- `timestamp` — ISO 8601 with explicit timezone. Treated as `reported_at`.
- `status` — Severity value; unknown strings normalize to `unknown`.
- `summary` — Truncated to 512 characters.
- Frontmatter is entirely optional; plain files without headers work the same way.

## User Stories / Flows

**User checks agent status in chat:**
1. User types `/agent-status` in the session input
2. Backend detects the command and routes to `AgentStatusCommandHandler`
3. Handler attempts a live fetch of `STATUS.md` from the running environment
4. If the environment is running, the file is downloaded, parsed, and the snapshot persisted to DB
5. Response appears instantly as a system message — no LLM call involved

**Environment not running (cached fallback):**
1. User types `/agent-status` while the environment is stopped
2. Handler detects the environment is not running; live fetch is skipped
3. Cached snapshot from DB is returned with a stale warning: "_Environment is not running — showing last cached status._"
4. If no cached snapshot exists: "No STATUS.md available for this agent."

**Agent has never written STATUS.md:**
- Response: "No STATUS.md available for this agent. See [COMPLEX\_AGENT\_DESIGN.md] for the expected format."

**A2A client sends `/agent-status`:**
1. A2A client sends `/agent-status` via `message/send` or `message/stream`
2. Backend executes the command identically to the UI flow
3. Client receives a completed task with the formatted markdown response (including severity icon and timestamps)
4. For machine-readable structured output, A2A clients should use the `agent/status` JSON-RPC method instead

## Response Format

```markdown
**Status:** 🟢 OK — All monitors green

_Reported 2026-04-19 14:32:05 UTC · fetched 2026-04-19 14:32:10 UTC_
_Changed from 🔴 error_          ← only shown when a severity transition occurred

---

# STATUS — 2026-04-19 14:32 UTC

- Inbox poll: caught up (0 pending)
- Last cache refresh: 2026-04-19 13:58 UTC
```

**Severity icon mapping:**

| Severity | Icon |
|----------|------|
| `ok`      | 🟢   |
| `info`    | 🔵   |
| `warning` | 🟡   |
| `error`   | 🔴   |
| `unknown` | ⚪   |

## Business Rules

- `/agent-status` bypasses the LLM pipeline — no streaming, no agent-env activation
- The command always tries a live fetch first; falls back to cached snapshot on any error
- A live fetch is rate-limited: at most one per environment per 30 seconds (shared lock with the `force_refresh` REST parameter and the push-path endpoint)
- Freeform files with no frontmatter are fully supported: severity is `unknown`, summary is the first non-blank body line, `reported_at` comes from the file mtime
- The file is capped at 64 KB; content beyond that is stored with a `\n... (truncated)` marker appended
- Frontmatter larger than 4 KB is ignored and the whole file is treated as body
- Non-UTF-8 bytes are decoded with `errors="replace"` — the command never raises a decode error
- Timestamps in the future are accepted without warning (agent clocks may be slightly skewed)
- When a severity transition is detected, the response includes a "Changed from `prev_severity`" line and an `agent_activities` entry is created for the feed

## Architecture Overview

```
/agent-status command received
        │
        ▼
AgentStatusCommandHandler.execute(context, args)
        │
        ├── Load AgentEnvironment from context.environment_id
        │
        ├── AgentStatusService.fetch_status(env)  ← live fetch
        │       │   (rate-limited, 30 s lock)
        │       ├── adapter.fetch_workspace_item_with_meta("STATUS.md")
        │       ├── Consume bounded byte stream (64 KB cap)
        │       ├── parse_status_file(content)
        │       │       ├── Extract YAML frontmatter (if present, ≤ 4 KB)
        │       │       ├── Normalize severity
        │       │       ├── Resolve summary (frontmatter or fallback)
        │       │       └── Parse timestamp
        │       ├── _resolve_reported_at(frontmatter_ts, file_mtime)
        │       ├── Detect severity transition → update prev_severity, severity_changed_at
        │       ├── Persist snapshot to agent_environment row
        │       └── Emit agent_status_updated event if content/severity changed
        │
        ├── Fetch error? → fall back to get_cached_status(env)
        │
        ├── No data at all? → "No STATUS.md available…"
        │
        └── Build markdown response (icon, severity, summary, timestamps, raw body)
            → CommandResult(content=markdown)
```

## Integration Points

- **[Agent Environments](../agent_environments/agent_environments.md)** — Status file is read from the workspace via `fetch_workspace_item_with_meta()` on the environment adapter; cached snapshot persists in the `agent_environment` table
- **[Agent Commands](agent_commands.md)** — Registered as a standard command handler; inherits command framework routing, A2A differentiation, and system message rendering
- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** — `STATUS.md` lives in the workspace root (`/app/workspace/STATUS.md`) and is authored by agent scripts; the push-path watcher in the agent-env process POSTs to the backend on mtime changes
- **[Complex Agent Design](../agent_environment_core/agent_environment_core.md)** — The `COMPLEX_AGENT_DESIGN.md` prompt doc describes the `STATUS.md` convention, recommended frontmatter format, and the `scripts/update_status.py` helper
- **[A2A Protocol](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — A2A callers can send the command as a message (returns markdown) or call the `agent/status` JSON-RPC method (returns structured `AgentStatusPublic` payload)
- **[Agent Activities](../../application/agent_activities/agent_activities.md)** — Severity transitions create activity feed entries via `ActivityService`
- **[Realtime Events](../../application/realtime_events/event_bus_system.md)** — `agent_status_updated` WebSocket event is emitted on fetch when severity or content changes; frontend invalidates its React Query cache on receipt

---

*Last updated: 2026-04-19*
