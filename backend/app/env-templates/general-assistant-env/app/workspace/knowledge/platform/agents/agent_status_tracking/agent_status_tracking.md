# Agent Status Tracking

## Purpose

Lightweight, self-published heartbeat for every agent: the agent (or its scripts) writes a `STATUS.md` file at the workspace root, and the platform surfaces its contents through a slash command, REST endpoint, session-header badge, and A2A method. Lets users and external monitors see "what's this agent currently doing / is it healthy" without invoking the LLM.

## Core Concepts

- **STATUS.md** — a markdown file at `/app/workspace/STATUS.md` inside the agent environment. Always reflects the *current* state; agents overwrite it in place rather than appending.
- **Frontmatter (optional)** — YAML block with `timestamp`, `status`, `summary` keys. When present, the platform extracts structured metadata; otherwise the file is treated as freeform.
- **Severity** — one of `ok`, `warning`, `error`, `info`, or `unknown` (anything unrecognized normalizes to `unknown`).
- **Snapshot** — the cached parsed result stored on the `agent_environment` row, so status remains visible even when the environment is stopped.
- **Severity transition** — when the parsed severity differs from the previous fetch. Transitions emit a WebSocket event and create an activity-feed entry.
- **Reported-at source** — `frontmatter` (timestamp came from YAML), `file_mtime` (fallback to file modification time), or `null`.
- **Push path** — the agent-env process watches `STATUS.md` mtime and POSTs to a backend internal endpoint when it changes, so updates show up in real time without waiting for the polling scheduler.

## User Stories / Flows

### 1. Agent publishes its status
1. Agent (or a scheduled OK-pattern script) calls the bundled helper: `python scripts/update_status.py --status ok --summary "All clear"`.
2. The helper atomically writes `STATUS.md` (write to `.tmp` then `os.replace`).
3. The agent-env mtime watcher detects the change within ~5 s, debounces 2 s, then POSTs `/api/v1/internal/environments/{env_id}/status-updated`.
4. Backend fetches the file, parses it, persists the snapshot, and emits `agent_status_updated` over WebSocket.

### 2. User views status in the session header
1. Session loads. The `AgentStatusBadge` calls `GET /api/v1/agents/{id}/status`.
2. Badge renders: colored severity dot, summary (ellipsized), relative timestamp.
3. If a transition occurred within the last hour, a `prev → current` chip appears.
4. If `is_stale=true` (env not running or snapshot >10 min old), the badge dims and shows an "Outdated" pill.
5. Clicking the badge opens `AgentStatusDialog` with the full markdown body, header strip, refresh + copy buttons.

### 3. User runs `/agent-status` in chat
1. User types `/agent-status` (autocompleted from the command registry).
2. Backend renders a markdown response: severity icon + summary header line, `Reported …  fetched …` timestamps, divider, full body.
3. No LLM call is made — pure command output.

### 4. External monitor polls REST
1. External agent calls `GET /api/v1/agents/{id}/status` with a bearer token (user JWT, A2A token, or desktop auth).
2. Receives the structured `AgentStatusPublic` snapshot.
3. Optional `?force_refresh=true` bypasses the cache (rate-limited per environment).

### 5. A2A peer queries status
1. Peer calls JSON-RPC method `agent/status` against `/api/v1/a2a/{agent_id}/`.
2. Receives the same payload shape as the REST response.
3. The `status` skill is declared on the agent's A2A card.

### 6. Background scheduler keeps the cache warm
1. `environment_status_scheduler` ticks every 10 minutes.
2. For each healthy running env, if the snapshot is missing or older than 5 minutes, the scheduler opportunistically calls `fetch_status` (rate-limit aware).
3. Failures are swallowed silently — STATUS.md is optional.

## Business Rules

- **File location is fixed** — only `/app/workspace/STATUS.md` is read. No per-skill or nested status files in MVP.
- **Frontmatter is optional** — agents may publish freeform markdown; severity will normalize to `unknown` and the summary falls back to the first non-blank, non-heading body line.
- **Severity vocabulary is closed** — only `ok`, `warning`, `error`, `info` are recognized. Other values become `unknown`.
- **Size cap** — body truncated at 64 KB with a `... (truncated)` marker; frontmatter rejected if > 4 KB.
- **Timestamp resolution** — frontmatter `timestamp` wins when valid; otherwise the file's mtime; otherwise `null`. The chosen source is exposed as `reported_at_source`.
- **Severity transitions are first-class** — first-ever fetch counts as a transition from `null`. Transitions update `prev_severity` + `severity_changed_at`, emit `agent_status_updated`, and create an activity-feed entry.
- **Rate limit on force-refresh** — one live fetch per environment per 30 s. REST returns `429`; the slash command silently serves cached data.
- **Staleness** — snapshot is "stale" when env is not running OR snapshot fetch is > 10 minutes old. Surfaced via `is_stale` in the response.
- **No secrets in STATUS.md** — agents are explicitly instructed never to write credential values; treated as a public artifact (rendered to UI, returned via API, included in A2A responses).
- **Cache survives env stop** — snapshot fields stay on the `agent_environment` row until the env is deleted; users see the last published state with `is_stale=true`.

## Architecture Overview

```
Agent script ──writes──▶ /app/workspace/STATUS.md
                              │
                              ▼
              Agent-env mtime watcher (5 s poll, 2 s debounce)
                              │
                              ▼  POST /internal/environments/{id}/status-updated
              ┌───────────────┴───────────────┐
              │     AgentStatusService        │
              │  ┌─────────────────────────┐  │
              │  │ fetch_status            │  │
              │  │  ├ adapter.fetch_..._with_meta
              │  │  ├ parse_status_file   │  │
              │  │  ├ resolve_reported_at │  │
              │  │  ├ detect transition   │  │
              │  │  ├ persist snapshot    │  │
              │  │  └ emit event + activity│ │
              │  └─────────────────────────┘  │
              └───────────────┬───────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
 GET /agents/{id}/status   /agent-status cmd    A2A agent/status
   GET /agents/status       (markdown reply)      (JSON-RPC)
                              │
                              ▼
                    AgentStatusBadge / Dialog
                    (session header, WS-driven)
```

## Integration Points

- **[Agent Commands](../agent_commands/agent_commands.md)** — registers the `/agent-status` slash command via `CommandService`. Full command spec in [`agent_status_command.md`](../agent_commands/agent_status_command.md).
- **[Agent Environments](../agent_environment_core/agent_environment_core.md)** — extends the workspace adapter with `fetch_workspace_item_with_meta()`; piggybacks on `environment_status_scheduler` for the background refresh.
- **[A2A Integration](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — exposes the `status` skill and `agent/status` JSON-RPC method on the A2A agent card.
- **Activity feed** — severity transitions create an entry visible in the agent's activity timeline.
- **Event bus** — emits `agent_status_updated` events consumed by the WebSocket bridge and frontend React Query invalidation.
- **App-core env template** — ships `workspace/STATUS.md` (placeholder), `workspace/scripts/update_status.py` (helper), and the agent-env mtime watcher.
- **COMPLEX_AGENT_DESIGN.md** — documents the convention for agent authors and cross-links from the OK-pattern scheduled-script section.
