# Agent Status Tracking

## Purpose

Lightweight, self-published heartbeat for every agent: the agent (or its scripts) writes a `STATUS.md` file under the workspace `docs/` folder, and the platform surfaces its contents through a slash command, REST endpoint, agent-card footer in the agents list, and A2A method. Lets users and external monitors see "what's this agent currently doing / is it healthy" without invoking the LLM.

## Core Concepts

- **STATUS.md** — a markdown file at `/app/workspace/docs/STATUS.md` inside the agent environment. Always reflects the *current* state; agents overwrite it in place rather than appending.
- **Frontmatter (optional)** — YAML block with `timestamp`, `status`, `summary` keys. When present, the platform extracts structured metadata; otherwise the file is treated as freeform.
- **Severity** — one of `ok`, `warning`, `error`, `info`, or `unknown` (anything unrecognized normalizes to `unknown`).
- **Snapshot** — the cached parsed result stored on the `agent_environment` row, so status remains visible even when the environment is stopped.
- **Severity transition** — when the parsed severity differs from the previous fetch. Transitions emit a WebSocket event and create an activity-feed entry.
- **Reported-at source** — `frontmatter` (timestamp came from YAML), `file_mtime` (fallback to file modification time), or `null`.

## User Stories / Flows

### 1. Agent publishes its status
1. Agent (or a scheduled OK-pattern script) calls the bundled helper: `python scripts/update_status.py --status ok --summary "All clear"`.
2. The helper atomically writes `STATUS.md` (write to `.tmp` then `os.replace`).
3. The next backend-triggered action in the env (a session stream completing, a CRON run finishing) pulls the new contents via the post-action handler. Slash-command / force-refresh / A2A callers pick up the change on demand.

### 2. User views status from the agents list
1. Agents list page loads. The grid batch-fetches all snapshots in one call via `GET /api/v1/agents/status?workspace_id=…` and routes each one to its `AgentCard`.
2. Cards that received a non-empty snapshot render a compact `AgentStatusCardFooter`: colored severity dot, summary (ellipsized), relative timestamp from the agent's own `reported_at`. Cards with no published status omit the footer entirely.
3. Clicking the footer opens `AgentStatusDialog` with the full markdown body, header strip (severity, summary, reported-at, fetched-at, optional transition line), refresh + copy buttons. The card's main link is not triggered.

### 3. User runs `/agent-status` in chat
1. User types `/agent-status` (autocompleted from the command registry).
2. Backend renders a markdown response: severity icon + summary header line, `Reported …  fetched …` timestamps, divider, body (frontmatter stripped).
3. No LLM call is made — pure command output.

### 4. External monitor polls REST
1. External agent calls `GET /api/v1/agents/{id}/status` with a bearer token (user JWT, A2A token, or desktop auth).
2. Receives the structured `AgentStatusPublic` snapshot.
3. Optional `?force_refresh=true` bypasses the cache (rate-limited per environment).

### 5. A2A peer queries status
1. Peer calls JSON-RPC method `agent/status` against `/api/v1/a2a/{agent_id}/`.
2. Receives the same payload shape as the REST response.
3. The `status` skill is declared on the agent's A2A card.

### 6. Post-action pull after every backend-triggered agent-env action
1. The agent-env has no outbound network access, so the backend is the only actor that knows when an in-container action just finished.
2. Every such finish emits an event — session streams emit `STREAM_COMPLETED` / `STREAM_ERROR`; the CRON scheduler emits `CRON_COMPLETED_OK` (OK-pattern script returned "OK"), `CRON_TRIGGER_SESSION` (schedule started a session), or `CRON_ERROR` (schedule failed).
3. `AgentStatusService.handle_post_action_event` is registered against all five events. It reads `environment_id` from the event meta and calls `refresh_after_action(env)`.
4. `refresh_after_action` honors the 30 s per-env rate-limit so bursts collapse to a single fetch. Errors are swallowed — status tracking is best-effort.

## Business Rules

- **File location is fixed** — only `/app/workspace/docs/STATUS.md` is read. No per-skill or nested status files in MVP.
- **Frontmatter is optional** — agents may publish freeform markdown; severity will normalize to `unknown` and the summary falls back to the first non-blank, non-heading body line.
- **Severity vocabulary is closed** — only `ok`, `warning`, `error`, `info` are recognized. Other values become `unknown`.
- **Size cap** — body truncated at 64 KB with a `... (truncated)` marker; frontmatter rejected if > 4 KB.
- **Timestamp resolution** — frontmatter `timestamp` wins when valid; otherwise the file's mtime; otherwise `null`. The chosen source is exposed as `reported_at_source`.
- **Severity transitions are first-class** — first-ever fetch counts as a transition from `null`. Transitions update `prev_severity` + `severity_changed_at`, emit `agent_status_updated`, and create an activity-feed entry.
- **Rate limit on force-refresh** — one live fetch per environment per 30 s. REST returns `429`; the slash command silently serves cached data.
- **No built-in staleness concept** — update cadence is agent-specific (some run every minute, some once a week). The UI shows the agent's own `reported_at`; downstream consumers decide what "too old" means for their domain.
- **No secrets in STATUS.md** — agents are explicitly instructed never to write credential values; treated as a public artifact (rendered to UI, returned via API, included in A2A responses).
- **Cache survives env stop** — snapshot fields stay on the `agent_environment` row until the env is deleted; users see the last published state.

## Architecture Overview

```
Agent script ──writes──▶ /app/workspace/docs/STATUS.md
                              │
                              ▼ (pulled only when a backend-triggered
                                 action completes, or on REST/A2A/cmd)
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
              │  ┌─────────────────────────┐  │
              │  │ handle_post_action_event│  │
              │  │  ← STREAM_COMPLETED     │  │
              │  │  ← STREAM_ERROR         │  │
              │  │  ← CRON_COMPLETED_OK    │  │
              │  │  ← CRON_TRIGGER_SESSION │  │
              │  │  ← CRON_ERROR           │  │
              │  └─────────────────────────┘  │
              └───────────────┬───────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
 GET /agents/{id}/status   /agent-status cmd    A2A agent/status
   GET /agents/status       (markdown reply)      (JSON-RPC)
                              │
                              ▼
                    AgentCard Footer / Dialog
                    (agents list, WS-driven)
```

## Integration Points

- **[Agent Commands](../agent_commands/agent_commands.md)** — registers the `/agent-status` slash command via `CommandService`. Full command spec in [`agent_status_command.md`](../agent_commands/agent_status_command.md).
- **[Agent Environments](../agent_environment_core/agent_environment_core.md)** — extends the workspace adapter with `fetch_workspace_item_with_meta()`.
- **[A2A Integration](../../application/a2a_integration/a2a_protocol/a2a_protocol.md)** — exposes the `status` skill and `agent/status` JSON-RPC method on the A2A agent card.
- **Activity feed** — severity transitions create an entry visible in the agent's activity timeline.
- **Event bus (outbound)** — emits `agent_status_updated` events consumed by the WebSocket bridge and frontend React Query invalidation.
- **Event bus (inbound)** — subscribes `handle_post_action_event` to `STREAM_COMPLETED`, `STREAM_ERROR`, `CRON_COMPLETED_OK`, `CRON_TRIGGER_SESSION`, `CRON_ERROR`. The CRON events are emitted by `agent_schedule_scheduler._emit_cron_event` at every schedule-execution exit point.
- **App-core env template** — ships `workspace/docs/STATUS.md` (placeholder) and `workspace/scripts/update_status.py` (helper). No in-container watcher — the backend is the sole reader.
- **COMPLEX_AGENT_DESIGN.md** — documents the convention for agent authors and cross-links from the OK-pattern scheduled-script section.
