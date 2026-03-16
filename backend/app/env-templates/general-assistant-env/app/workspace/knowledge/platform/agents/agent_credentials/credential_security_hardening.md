# Credential Security Hardening

## Purpose

Prevent agent environments from directly reading, leaking, or tampering with credential files through a defense-in-depth approach: intercept tool calls that target credentials, redact credential values from agent output, and log all security-relevant events for auditing.

## Core Concepts

- **Credential Guard** — module-level singleton inside the agent container that holds known sensitive values and scans agent output for matches, replacing them with `***REDACTED***`
- **Tool Interception** — SDK-level hooks that detect when the agent tries to read, write, or cat credential files, and report the event to the backend before allowing or blocking the call
- **Security Event** — a logged record of a credential access attempt, output redaction, or write attempt, stored in a dedicated backend table
- **Blockable Reporting** — synchronous event reporting protocol where the backend can respond with `"allow"` or `"block"`, letting the server control interception policy without SDK-side changes
- **Fail-Open** — design principle: if the backend is unreachable or a hook script errors, the tool call is allowed. Availability is not sacrificed at the interception layer; the output redaction layer catches leaks as a second line of defense

## Why NOT File Permissions

File permissions (`chmod 000`) don't work because legitimate scripts written by the agent need credential access at runtime (e.g., a Python script using an API token). If scripts can read credentials, the agent can read them through scripts. The correct interception point is at the SDK tool level, where we can distinguish "agent directly reading credentials" vs "agent-written script using credentials at runtime."

## Security Layers

Three independently deployable layers, each covering a different attack surface:

1. **SDK Tool Interception (Phase 1)** — intercepts Read, Bash, Write, Edit tool calls that target credential files before they execute
2. **Output Redaction (Phase 2)** — scans all agent output for known credential values and replaces them with `***REDACTED***` before they reach the user
3. **Security Event Logging (Phase 3)** — records all credential access attempts, redaction triggers, and suspicious patterns in a backend audit table

## Architecture Overview

```
Agent SDK (Claude Code / ADK)
  │
  ├── Phase 1: Tool Interception
  │     ├── Claude Code: PreToolUse hook script
  │     └── ADK: inline Bash()/Read() interceptors
  │     ↓
  │   credential_access_detector (shared pattern matching)
  │     ↓
  │   event_reporter → POST /security/report (env server proxy)
  │     ↓
  │   Backend POST /api/v1/security-events/report → returns allow/block
  │
  ├── Phase 2: Output Redaction
  │     SDK Adapter emits SSE events
  │     ↓
  │   _redacted_event_stream() in routes.py
  │     ↓
  │   CredentialGuard.redact(content) — scans against known values
  │     ↓
  │   If match → replace with ***REDACTED***, fire-and-forget report
  │
  └── Phase 3: Security Event Logging
        Backend SecurityEvent table ← receives events from Phase 1 & 2
        GET /api/v1/security-events/ → paginated audit view
```

## User Stories / Flows

### 1. Agent Tries to Read Credentials Directly

1. Agent issues a `Read` tool call targeting `credentials/credentials.json`
2. SDK hook (Claude Code) or inline interceptor (ADK) detects credential path pattern
3. Security event is reported to backend via environment server proxy
4. Backend logs the event and returns `action: "allow"` (default policy, future: configurable)
5. If `"block"`: tool call is denied with "Credential file access denied by security policy"
6. If `"allow"`: tool call proceeds normally

### 2. Agent Output Contains Credential Values

1. Agent produces output that includes an API token value (e.g., from a script result)
2. The SSE stream passes through `_redacted_event_stream()` in the environment server
3. `CredentialGuard.redact()` scans the content against known sensitive values
4. Matching values are replaced with `***REDACTED***`
5. A fire-and-forget `OUTPUT_REDACTED` event is reported to the backend
6. User sees the redacted output

### 3. Admin Reviews Security Events

1. User navigates to security audit view (or calls `GET /api/v1/security-events/`)
2. Events are listed newest-first with filters: agent, environment, session, event type
3. Each event shows: timestamp, event type, severity, details (tool name, input, etc.)

## Business Rules

- **Fail-open design**: Backend unreachable → allow the tool call. Security events may be missing from the audit trail, but agent availability is preserved
- **3-second timeout**: Hook waits at most 3 seconds for backend response before fail-open
- **Minimum value length**: Only credential values of 8+ characters are tracked for redaction to avoid false positives (ports, "Bearer", "true")
- **Credential rotation**: When credentials are re-synced, CredentialGuard rebuilds its value set from scratch — old values are purged
- **No blocking logic yet**: The `/report` endpoint always returns `"allow"`. The action field is a hook point for future policy engines (risk scoring, guest session rules, N-attempt thresholds)
- **SENSITIVE_FIELDS mirroring**: The CredentialGuard in the container mirrors the `SENSITIVE_FIELDS` definition from the backend's `CredentialsService` — both must be kept in sync when new credential types are added

## Event Types

| Event Type | Default Severity | Trigger |
|------------|-----------------|---------|
| `CREDENTIAL_READ_ATTEMPT` | high | SDK tool interceptor detected credential file Read |
| `CREDENTIAL_BASH_ACCESS` | high | Bash command matched credential-access pattern |
| `OUTPUT_REDACTED` | medium | Credential value found and redacted in agent output |
| `CREDENTIAL_WRITE_ATTEMPT` | high | Attempt to Write/Edit credential files |

## Pattern Matching

Tool interception uses regex patterns to detect credential access:

- **File path tools** (Read, Write, Edit): match paths containing `credentials/credentials.json` or `credentials/<uuid>.json`
- **Bash**: match commands like `cat credentials/`, `python ... open ... credentials/`, `jq credentials/`, `cp credentials/`, `curl file://credentials/`, `base64 credentials/`

## Multi-SDK Support

- **Claude Code**: External hook script (`credential_guard_hook.py`) registered via PreToolUse hook in `.claude/settings.json`. Written by `environment_lifecycle.py` during environment creation
- **Google ADK**: Inline interception in the `Bash()` and `Read()` function definitions within the adapter. No external hook — detection code is called directly before tool execution
- **Future SDKs**: Should use the shared `credential_access_detector` module for pattern matching and `SecurityEventReporter` for event reporting

## Integration Points

- [Agent Credentials](agent_credentials.md) — `SENSITIVE_FIELDS` / `AGENT_ENV_ALLOWED_FIELDS` definitions reused for redaction value extraction
- [Credentials Whitelist](credentials_whitelist.md) — existing field whitelisting that this feature extends with runtime protection
- [Agent Environment Core](../agent_environment_core/agent_environment_core.md) — routes.py SSE stream wrapped with redaction; new `/security/report` proxy endpoint
- [Agent Environments](../agent_environments/agent_environments.md) — `environment_lifecycle.py` extended to write Claude Code hook settings
- [Agent Sessions](../../application/agent_sessions/agent_sessions.md) — `session_id` on SecurityEvent links events to sessions
- [Guest Sharing](../agent_sharing/guest_sharing.md) — `guest_share_id` on SecurityEvent enables guest-specific security queries

## Known Limitations

- **Creative bypass**: Agent could encode credentials in base64, split across messages, or use indirect access methods — Phase 2 redaction won't catch encoded/split values
- **SENSITIVE_FIELDS duplication**: The CredentialGuard mirrors field definitions from the backend. Adding a new credential type requires updating both copies
- **No blocking policies**: Currently all events are allowed. Blocking logic is deferred to a future policy engine
- **No frontend UI**: Security events are available via API only. A frontend audit view is planned but not implemented