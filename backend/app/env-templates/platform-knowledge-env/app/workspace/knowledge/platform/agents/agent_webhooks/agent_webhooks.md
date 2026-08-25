# Agent Webhooks

## Purpose

Allows external systems to trigger an agent on demand via authenticated HTTP endpoints. Each webhook is scoped to one agent and carries its own bearer token. Callers POST to a public URL with the token — no platform account required on the caller side.

Two trigger types, mirroring the `static_prompt` / `script_trigger` split used by [Agent Schedulers](../agent_schedulers/agent_schedulers.md):

- **Session trigger** — starts a new agent session, seeding it with a configured prompt plus the incoming HTTP payload.
- **Script trigger** — runs a shell command inside the agent's Docker environment, forwarding the payload as environment variables and stdin.

Every invocation — success or failure — is recorded in an immutable log visible from the Integrations tab.

## Core Concepts

| Concept | Definition |
|---------|-----------|
| **Agent Webhook** | A named, per-agent endpoint configuration with its own URL slug and bearer token |
| **Webhook ID** | Short URL-safe slug (11 chars) that forms the public URL, e.g. `abc123xy` |
| **Bearer Token** | Cryptographically random token (URL-safe base64, 32 bytes entropy); shown once on creation, Fernet-encrypted at rest |
| **Session Trigger** | Webhook type that creates a new agent session seeded with the payload |
| **Script Trigger** | Webhook type that executes a shell command in the agent's Docker environment |
| **Payload Template** | Static text prepended to the session prompt on each invocation (session type) or stored alongside the call (script type); optional |
| **Webhook Log** | Immutable invocation record: status, payload received, output, session link, error details |
| **Token Prefix** | First 8 chars of the plaintext token, stored for UI display so owners can identify which token is active |

## Trigger Types

### Session Trigger

Each call creates a new agent session. The assembled session prompt is:

```
[webhook prompt or agent entrypoint_prompt or "Start webhook-triggered execution."]

---
Webhook: {webhook name}
{payload_template if set}

Payload (Content-Type: {content_type}):
{request body}

Headers:
{allowlisted headers as JSON}
```

The assembled prompt is capped at 20,000 characters; longer payloads are truncated with a `[truncated]` marker. Sessions are created with `integration_type="webhook"` and appear in the agent's session list alongside manual and email-triggered sessions.

Use this type when the incoming event needs the agent's reasoning and you want a conversation trail.

### Script Trigger

Each call runs a configured shell command inside the agent's Docker container. The payload is forwarded via environment variables and stdin:

| Variable | Content |
|----------|---------|
| `WEBHOOK_PAYLOAD` | Raw request body (empty string if no body) |
| `WEBHOOK_NAME` | Webhook's configured name |
| `WEBHOOK_ID` | Webhook's public URL slug |
| `WEBHOOK_HEADERS_JSON` | Allowlisted request headers, JSON-encoded |
| `WEBHOOK_CONTENT_TYPE` | Value of the incoming `Content-Type` header |

The raw body is also piped to the command's stdin, so both shell `read` and command-line tools expecting stdin work without referencing `$WEBHOOK_PAYLOAD`.

Commands execute in `/app/workspace/` with the same sandbox as agent SDK tool calls. No new execution surface is introduced; the same `/exec` endpoint used by [Agent Schedulers](../agent_schedulers/agent_schedulers.md) script triggers is reused.

Use this type for predefined data-processing or notification scripts that don't need conversational interaction.

## User Flows

### Creating a Webhook

1. Navigate to **Agent > Integrations tab**. The Webhooks card is visible to the agent owner only.
2. Click **+ Webhook**. A type selector dialog opens with two choices:
   - **Session Trigger** (MessageSquare icon) — starts a new agent session with the payload
   - **Script Trigger** (Terminal icon, amber accent) — runs a shell command in the agent's environment
3. Select a type. A type-specific form opens.

**Session form fields:**
- Name (required, max 255 chars)
- Session mode — Conversation or Building (tooltip explains the difference; defaults to Conversation)
- Prompt (optional; leave empty to use the agent's entrypoint prompt)
- Payload template (optional; static context prepended to each call's payload)

**Script form fields:**
- Name (required, max 255 chars)
- Command (required; monospace input, max 2000 chars; references `$WEBHOOK_PAYLOAD`, `$WEBHOOK_NAME`, `$WEBHOOK_HEADERS_JSON`, `$WEBHOOK_ID`, `$WEBHOOK_CONTENT_TYPE`)
- Timeout in seconds (1–300; default 120)
- Payload template (optional; stored and logged alongside the call, not injected into the shell command)

4. Click **Create webhook**. The backend generates the URL slug and token, returns both once.
5. The form transitions to the **token reveal panel** — the same one-time reveal pattern used by Task Triggers and MCP OAuth. It shows:
   - Full webhook URL
   - Plaintext bearer token
   - Ready-to-run `curl` example
   - "Copy" buttons for each
   - A prominent banner: "Save this token now — it will not be shown again"
6. Close the panel. The webhook appears in the list. The token is no longer accessible from the UI.

### Firing a Webhook from an External System

```bash
curl -X POST "https://your-instance.com/agent-hooks/abc123xy" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"event": "push", "repo": "my-app"}'
```

The token can also be passed as a query parameter (`?token=<token>`) for systems that cannot set arbitrary headers.

A successful response (HTTP 200):
```json
{
  "success": true,
  "webhook_type": "session",
  "log_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

The `log_id` can be used to look up the invocation record in the UI or to correlate with platform logs.

### Viewing Execution Logs

1. Click the **History icon** on a webhook card.
2. A modal opens showing the last 50 invocations, newest first.
3. Each row shows: status badge, type badge, timestamp, and duration.
4. Click **View** on a row to expand it and see:
   - Caller IP
   - Forwarded headers (JSON)
   - Payload received (monospace)
   - Prompt assembled (session type) or command executed (script type)
   - stdout / stderr (script type)
   - Exit code (script type; green = 0, red = non-zero)
   - Session link (session type; opens in a new tab)
   - Error message (if the invocation failed)

### Regenerating a Token

1. Click the **Refresh icon** on a webhook card.
2. A confirmation dialog warns: "The current token will stop working immediately."
3. Confirm. The backend generates a new token; the old one is invalidated immediately.
4. The same one-time token reveal panel appears with the new token.

Any external system using the old token will receive HTTP 401 on the next call.

### Enabling and Disabling

Click the **Power icon** on a webhook card to toggle its enabled state. Disabled webhooks return HTTP 404 (not 401) to callers — this prevents confirming whether the webhook exists.

### Deleting a Webhook

Click the **Trash icon** on a webhook card and confirm. All execution logs for the webhook are removed via cascade. Sessions created by the webhook remain unaffected.

## Access Control

- Only the **agent owner** can create, view, edit, or delete webhooks for an agent.
- Non-owners (including users with shared / guest access) do not see the Webhooks card on the Integrations tab.
- The public `/agent-hooks/{webhook_id}` endpoint has no JWT requirement — authentication is entirely by the bearer token in the request.
- Cloned agents have **independent** webhooks. When an agent is shared and the recipient accepts, no webhook configurations or logs are copied. Clone owners manage their own webhooks with their own URLs and tokens. This matches the [Agent Schedulers](../agent_schedulers/agent_schedulers.md#cloned-agents-and-scheduling) convention.

## Security Model

- **Tokens** — generated with `secrets.token_urlsafe(32)` (256 bits of entropy). Stored only as Fernet-encrypted ciphertext; the plaintext is never persisted and is returned to the client exactly once.
- **Token validation** — uses `hmac.compare_digest` for timing-safe comparison against the decrypted token. Identical to the [Task Triggers](../../application/input_tasks/task_triggers.md) pattern.
- **Disabled webhook response** — HTTP 404, same response shape as "webhook not found". No existence leakage.
- **Header allowlist** — only the following headers are forwarded to prompts, script env vars, and logs: `user-agent`, `x-forwarded-for`, `x-real-ip`, `x-github-event`, `x-gitlab-event`, `x-hub-signature-256`, `x-event-key`. `Authorization`, `Cookie`, and anything else is dropped before logging or forwarding.
- **Payload size cap** — 64 KB maximum request body. Larger requests receive HTTP 413 before any token validation.
- **Log payload truncation** — raw body stored up to 10,000 characters; longer bodies are truncated with a `[truncated]` marker. The same limit applies to stdout/stderr for script-type logs.
- **Prompt size cap** — assembled session prompt is capped at 20,000 characters; the payload portion is trimmed if needed.
- **Script sandbox** — commands execute inside the agent's Docker container via the same `/exec` endpoint used by scheduled script triggers. The backend never executes shell on the host.
- **Env var injection** — payload bytes are passed as string values in the subprocess `env` dict, not interpolated into the command string. This eliminates shell injection from untrusted payload content.

## Log Status Reference

| Status | Trigger Type | When |
|--------|-------------|------|
| `session_started` | session | Session created and user message queued successfully |
| `success` | script | Command exited with code 0 |
| `script_error` | script | Command exited with non-zero code (normal outcome — output preserved) |
| `error` | both | Infrastructure failure: no active environment, activation timeout, session-creation failure, or unhandled exception |

Post-validation failures (after the token is verified) return HTTP 200 with a `log_id` regardless of the internal outcome. The caller knows the webhook was received; the error detail is in the log.

## Error Scenarios

| Scenario | Response |
|----------|----------|
| Unknown or disabled webhook ID | HTTP 404 "Webhook not found" |
| Token missing | HTTP 401 "Token required" |
| Token mismatch | HTTP 401 "Invalid or expired token" |
| Payload > 64 KB | HTTP 413 "Payload exceeds maximum size of 64KB" |
| No active environment (script type) | HTTP 200; log status `error`, message "No active environment found for agent" |
| Environment activation timeout (script type) | HTTP 200; log status `error` |
| Script non-zero exit | HTTP 200; log status `script_error`; exit code + stdout + stderr preserved |
| Script timeout | HTTP 200; log status `script_error`; exit code `-1`, stderr contains timeout message |
| Session creation fails — no active environment | HTTP 200; log status `error`, message "Could not create session — no active environment" |
| Update body sets a field that doesn't belong to the webhook's type | HTTP 400 |

## Integration Points

- **[Agent Schedulers](../agent_schedulers/agent_schedulers.md)** — sister feature with an identical `static_prompt` / `script_trigger` split, type-specific forms, log schemas, and environment auto-activation behavior. The shared environment helpers (`get_active_environment`, `ensure_environment_running`) were extracted from the scheduler service into `backend/app/services/agents/environment_resolver.py` specifically to support both features.
- **[Task Triggers](../../application/input_tasks/task_triggers.md)** — mirrors the bearer-token pattern: Fernet encryption, one-time reveal, timing-safe compare, `Authorization: Bearer` + `?token=` dual auth, 64 KB payload cap, and 404-on-disabled.
- **[Agent Sessions](../../application/agent_sessions/agent_sessions.md)** — session-type webhooks create sessions via the same `SessionService` path used by CRON schedules and email processing, tagged with `integration_type="webhook"`. These sessions appear in the agent's session list and are deletable like any other session.
- **[Agent Environment Core](../agent_environment_core/agent_environment_core.md)** — the `/exec` endpoint was extended with optional `env` dict and `stdin` parameters (additive, backward-compatible) to support script-type webhooks.
- **[Agent Management — Integrations Tab](../../application/agent_management/agent_management.md)** — the Webhooks card (`AgentWebhooksCard`) is rendered on the Integrations tab alongside A2A tokens, Agent REST API, Guest Share, MCP Connectors, Webapp Share, Local Dev, and GIT Versioning. It is owner-gated. There is no Email card — per-agent Email Integration was deleted in Phase 4 of the channels & identity unification.
- **[Nginx Setup](../../infrastructure/nginx_setup.md)** — the public `/agent-hooks/` path must be proxied to the backend. See that doc for the required location block.

## Future Enhancements (Out of Scope)

- HMAC signature verification (e.g., GitHub `X-Hub-Signature-256`) against a configured signing secret
- Rate limiting per webhook (e.g., max 60 fires per minute) with HTTP 429 responses
- Log retention job (prune logs older than N days)
- Payload jq-style transforms before forwarding to the agent
- Per-webhook `webhook_fired` activity entry in the agent activity feed (especially useful for script-type where no session is created)
- Retry-on-failure queue for session-type webhooks when the environment is temporarily unavailable
