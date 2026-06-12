# Channel Ingestion

## Purpose

Every inbound entry point that turns an external call into an agent session — A2A JSON-RPC, App MCP, web-UI `POST /sessions`, scheduler fires, and human-initiated task execution — historically reinvented the same three steps: figure out *who* the sender is, resolve or create a `Session`, and inject the user message. The Channel Ingestion layer collapses this into one stateless orchestration service over the existing session primitives.

The win is **one canonical entry point for the production send paths** (A2A and App MCP) and **one canonical session-resolution helper for the empty-create paths** (web-UI session create, scheduler, task execution). Net negative lines on the send paths; net neutral with consistent stamping elsewhere.

## Core Concepts

| Term | Definition |
|------|-----------|
| **`SessionSender`** | Immutable value type that names the sender uniformly across channels — `kind`, `external_id`, `display_name`, `platform_user_id`. Built via per-channel constructors (`from_a2a`, `from_app_mcp`, `from_webui`, `from_guest_share`, `from_task_execution`, `from_system_trigger`). |
| **Sender kind** | `Literal["platform_user", "a2a_caller", "mcp_caller", "webui_user", "task_executor", "system_trigger", "anonymous"]`. Not an Enum — flat string literals so new kinds can be added without machinery. |
| **`ChannelAccessPolicy`** | Per-call policy (`expected_owner_id`, `allow_system_trigger_fastpath`, `require_owner_match`, `require_access_token_scope`, `require_caller_in_route`) supplied by each entry point. The service interprets it; new access primitives never live here. |
| **`ChannelIngestionService`** | Stateless three-method service: `assert_access`, `resolve_or_create_session`, `ingest_inbound_message`. The ≥2-callers rule applies to every method. |
| **`IngestionResult`** | Return shape from `ingest_inbound_message` — `session`, `message_id`, `is_new_session`, `streaming_initiated`, `action`, `message`. Mirrors the dict shape of `SessionService.send_session_message` so per-channel migration is a near-textual swap. |
| **`integration_type`** | Plain `str` ("a2a", "app_mcp", "identity_mcp", "task", "schedule", …) stamped on `Session` at create time. Drives reader-side `get_session_sender(session)` mapping. |

## Sender Kinds

| Kind | Sent by | `platform_user_id` |
|------|---------|----|
| `webui_user` | `POST /sessions`, grant-based guest share | Authenticated user id |
| `anonymous` | Anonymous guest-share caller | `None` (session still owned by `agent.owner_id` via route override) |
| `a2a_caller` | A2A core surface, External A2A surface (all target types) | Agent owner id; caller stamped via `access_token_id` |
| `mcp_caller` | App MCP (plain + identity routing) | Caller's platform user id |
| `task_executor` | Human-initiated `POST /input-tasks/{id}/execute` | The executing human's user id |
| `system_trigger` | Scheduler (cron-fired), handover-spawned sessions | Always `agent.owner_id` by construction |
| `platform_user` | Reader fallback for unknown `integration_type` | Best-effort from session row |

## User Stories / Flows

### Inbound message via A2A or App MCP
1. Channel handler parses transport-layer auth (JWT, token payload, routing decision).
2. Handler builds a `SessionSender` via the matching constructor.
3. Handler builds a `ChannelAccessPolicy` reflecting its scope rules.
4. Handler calls `ChannelIngestionService.ingest_inbound_message(...)`:
   - Step 1: `assert_access` enforces kind-specific gates (raises on denial).
   - Step 2: `resolve_or_create_session` either looks up the existing `Session` by `thread_key` and verifies the resume sender matches, or creates a fresh session with stamped extras.
   - Step 3: Delegates message creation + stream kick to `SessionService.send_session_message`.
5. Handler maps the returned `IngestionResult` back to its transport format (A2A `Task`, MCP response, SSE stream).

### Web-UI session create (no message)
1. `POST /sessions` resolves `caller` to `CurrentUser` or `GuestShareContext`.
2. Route checks ownership / build-mode permissions itself (this stays in the route — not in the service).
3. Route builds `SessionSender.from_webui(...)` or `SessionSender.from_guest_share(...)`.
4. Route calls `ChannelIngestionService.resolve_or_create_session` directly (NOT `ingest_inbound_message` — no message body to inject; first message lands via a separate `/messages/stream` call later).

### Scheduler / human task execution
- **`system_trigger`** kind (scheduler, handover): `policy.allow_system_trigger_fastpath=True` plus the structural invariant `expected_owner_id == agent.owner_id == sender.platform_user_id` is *asserted*, not skipped. If invariant is violated the call raises — cron fires that mis-stamp the owner can't sneak past.
- **`task_executor`** kind (human-initiated tasks): the executing user is passed through as `sender.platform_user_id`, and `assert_access` runs a real owner-match check. This split makes the trust model visible in code rather than hidden inside `send_session_message`'s `user_id` check.

## Business Rules

- **The "≥ 2 callers" rule.** A method belongs in `ChannelIngestionService` only if **two or more channels** would call it. If a method would have one caller, it stays in that channel's module. Enforced by the architecture contract test.
- **Resume verification by kind:**
  - `webui_user`, `task_executor`, `platform_user`: `existing.user_id == sender.platform_user_id`.
  - `a2a_caller`: best-effort lineage check — `sender.external_id` must match `session.access_token_id` or `session.user_id`. Real scope enforcement runs at the channel edge via `AccessTokenService.can_access_session`.
  - `mcp_caller`: service refuses resume verification; the App MCP handler does its own `(integration_type, caller-column)` match in `_try_resume_session` before reaching the service.
  - `system_trigger`: cannot resume. Every cron fire is a fresh session — a `thread_key` reaching the service with this kind is a caller bug.
- **Owner selection on create** (`_select_session_owner_id`):
  - `webui_user`, `a2a_caller`, `mcp_caller`, `anonymous`: honor optional `session_owner_id` override in `extra_session_kwargs`; fall back to `agent.owner_id`.
  - `task_executor`: must use `sender.platform_user_id`.
  - `system_trigger`: always `agent.owner_id`.
- **Post-create stamping** is consolidated. Channels supply extras via `extra_session_kwargs`:
  - **Whitelisted create-time columns**: `access_token_id`, `source_task_id`, `email_thread_id`, `sender_email`.
  - **Whitelisted post-create columns**: `caller_id`, `identity_caller_id`, `identity_binding_id`, `identity_binding_assignment_id`.
  - **Metadata**: `session_metadata_extra` (merged into existing `session_metadata`).
  - Any other key raises `ValueError("Unknown post-create stamping keys: ...")` — typos surface immediately.
- **App MCP message injection** keeps the legacy `MessageService.create_message` + `stream_and_collect_response` pipeline (not `ingest_inbound_message`) due to a session-lock conflict with `initiate_stream`. The session-resolve step still uses `ChannelIngestionService.resolve_or_create_session` + `assert_access`. Documented in the handler's inline comment.
- **`integration_type` is metadata only.** Access policy is driven by sender kind. The External A2A surface stamps `integration_type="app_mcp"` while building an `a2a_caller` sender — fine; the reader (`get_session_sender(session)`) maps the row back to `mcp_caller` for surfacing, but the writer's kind is what mattered at access-control time.

## Architecture Overview

```
INBOUND ENTRY POINT          ChannelIngestionService            EXISTING PRIMITIVES
───────────────────          ────────────────────────           ───────────────────
A2A handler                  ┌─ assert_access ────────────►    (in-service kind/policy checks)
App MCP handler         ───► ├─ resolve_or_create_session ──►  SessionService.create_session
POST /sessions               └─ ingest_inbound_message  ────►  SessionService.send_session_message
Scheduler / task exec                                          → MessageService.create_message
                                                               → SessionService.initiate_stream
```

The service has no state, no instance fields, no notion of "which adapter." Outbound delivery (SSE / MCP streaming / WS fan-out / SMTP queue) stays in its own modules — outbound is genuinely heterogeneous and is **not** unified.

## Integration Points

- **[Agent Sessions](agent_sessions.md)** — session creation, message dispatch, and streaming all flow through `SessionService` primitives that this service composes.
- **[A2A Protocol](../a2a_integration/a2a_protocol/a2a_protocol.md)** — both the core A2A surface and the External A2A surface (with three target types: `agent`, `app_mcp_route`, `identity`) use `ingest_inbound_message`.
- **[App MCP Server](../app_mcp_server/app_mcp_server.md)** — the plain App MCP and identity-routed App MCP handlers both consume `assert_access` and `resolve_or_create_session`.
- **[Input Tasks](../input_tasks/input_tasks.md)** — human-initiated task execution uses the `task_executor` kind with real owner-match enforcement.
- **[Agent Schedulers](../../agents/agent_schedulers/agent_schedulers.md)** — cron-fired and handover-fired paths use the `system_trigger` kind, with the structural invariant asserted (not bypassed).
- **[Guest Sharing](../../agents/guest_sharing/guest_sharing.md)** — the `from_guest_share` constructor produces `kind="anonymous"` for unauthenticated guests and `kind="webui_user"` for grant-based ones; the route supplies `session_owner_id=agent.owner_id` so the session is still created in the owner's space.
- **[External Agent Access](../external_agent_access/external_agent_access.md)** — the External A2A handler subclass overrides `_extra_session_kwargs` to thread `session_owner_id` through for the `external` and `identity_mcp` target types (sessions owned under non-owner users).
