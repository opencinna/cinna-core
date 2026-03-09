# Credential Security Hardening — Implementation Plan

**Feature name**: `credential-security-hardening`
**Document path**: `docs/plans/credential_security_hardening.md`
**Status**: Draft
**Date**: 2026-03-09

---

## Overview

This plan describes a layered security hardening of the credential system for agent environments. While the existing system already provides strong encryption at rest, field whitelisting, and value redaction in the agent prompt, credentials are currently placed as a plaintext JSON file inside the container and are freely readable by the agent process. This feature closes that gap through three independently deployable security layers:

1. **SDK-level tool interception** — intercept tool calls (Read, Bash) that target credential files before they execute, report the event to the backend, and optionally block based on backend response.
2. **Output redaction pipeline** — scan all agent output for known credential values before it reaches the user, redacting matches at the environment server level. Reuses the existing `SENSITIVE_FIELDS` / `AGENT_ENV_ALLOWED_FIELDS` from `CredentialsService`.
3. **Security event logging** — record credential access attempts, redaction triggers, and suspicious patterns to a dedicated backend table with a reporting API. Event-driven reactions (risk scoring, automated responses) are deferred to a future phase.

### Why NOT File Permissions (chmod)

File permissions (`chmod 000`) were considered and rejected. The fundamental paradox: if the agent can't read `credentials.json`, then scripts written by the agent also can't read it — but scripts legitimately NEED credential access to function (e.g., a Python script using an API token). And if we make credentials readable by scripts, then the agent can simply read them through scripts (`python3 -c "print(open('credentials/credentials.json').read())"`). File permissions don't solve the problem at the right layer.

The correct interception point is at the **SDK tool level** — where we can distinguish between "agent directly reading credentials" vs "agent-written script using credentials at runtime".

Each layer is independently deployable and backward-compatible. Existing agents continue to work with no changes required.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    Docker Agent Container                        │
│                                                                  │
│  workspace/credentials/                                          │
│    credentials.json         (readable by scripts at runtime)    │
│    README.md                (redacted, included in prompt)      │
│                                                                  │
│  core/server/  (FastAPI — runs inside container)                 │
│    routes.py                                                     │
│      POST /chat/stream ─── Phase 2: output redaction wrapper    │
│                                                                  │
│  core/server/adapters/                                           │
│    claude_code.py ─────── Phase 1: Claude Code PreToolUse hook  │
│    google_adk.py ──────── Phase 1: direct Bash/Read interception│
│                                                                  │
│  core/server/security/                                           │
│    credential_guard.py ── credential value store + redactor     │
│    event_reporter.py ──── reports events to backend (blockable) │
└─────────────────────────────────────────────────────────────────┘
                │ HTTP (existing channel)
┌───────────────▼─────────────────────────────────────────────────┐
│                    Backend (FastAPI)                             │
│                                                                  │
│  backend/app/models/security_event.py   ── Phase 3 model        │
│  backend/app/services/security_event_service.py                  │
│  backend/app/api/routes/security_events.py                       │
│    POST /security-events/report  ← blockable (returns action)   │
│                                                                  │
│  backend/app/services/credentials_service.py                     │
│    SENSITIVE_FIELDS / AGENT_ENV_ALLOWED_FIELDS                   │
│    ── reused for redaction value extraction ── Phase 2 feed     │
└─────────────────────────────────────────────────────────────────┘
```

### Data Flow: Tool Interception (Phase 1)

```
Agent requests tool call (Read credentials.json / Bash cat credentials.json)
        │
        ▼
SDK-level interceptor (hook for Claude Code / inline for ADK)
  ↳ detect credential file access pattern
  ↳ POST to backend /api/v1/security-events/report (synchronous)
  ↳ backend logs event, returns { "action": "allow" | "block" }
  ↳ if "block" → deny tool execution, return error to agent
  ↳ if "allow" → proceed normally
        │
        ▼
Tool executes (or is blocked)
```

### Data Flow: Output Redaction (Phase 2)

```
Agent LLM response
        │
        ▼
SDK Adapter (claude_code.py / google_adk.py)
  emit SDKEvent(type=ASSISTANT, content=...)
        │
        ▼
[Phase 2] CredentialGuard.redact(content)
  ↳ scan content against known credential values (from SENSITIVE_FIELDS)
  ↳ replace matches with ***REDACTED***
  ↳ if match found → report SecurityEvent(type=OUTPUT_REDACTED)
        │
        ▼
Redacted SDKEvent forwarded to routes.py → SSE stream → Backend → Frontend
```

---

## Phase Structure

| Phase | Layer | Effort | Risk |
|-------|-------|--------|------|
| Phase 1 | SDK-level tool interception + blockable event reporting | Medium | Medium |
| Phase 2 | Output redaction pipeline (reuses existing field definitions) | Medium | Low |
| Phase 3 | Security event logging (backend model + API) | Medium | Low |

**Note**: Phase 3 (backend logging) is a dependency for Phase 1's blockable reporting. Implementation order should be: Phase 3 → Phase 1 → Phase 2. However, Phase 1 can work in "log-only" mode (fire-and-forget, no blocking) without Phase 3, allowing parallel development.

---

## Phase 1 — SDK-Level Tool Interception

### Goal

Intercept tool calls that target credential files at the SDK adapter level. For each interception, report the event to the backend. The backend response determines whether to block or allow the tool call — making the interception policy server-controlled and dynamically adjustable.

### Multi-SDK Approach

#### Claude Code — PreToolUse Hook

Claude Code supports pre-tool hooks via settings files. A hook script receives tool input on stdin and can block the tool by outputting a specific JSON response.

**Hook configuration** (written to `/app/core/.claude/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Read|Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /app/core/hooks/credential_guard_hook.py"
          }
        ]
      }
    ]
  }
}
```

**New file**: `backend/app/env-templates/app_core_base/core/hooks/credential_guard_hook.py`

This script:
1. Reads tool input JSON from stdin.
2. Checks if the tool targets credential files (pattern matching).
3. If match: sends synchronous POST to `http://localhost:{SERVER_PORT}/security/report` with event details.
4. The environment server forwards to backend `POST /api/v1/security-events/report`.
5. Backend returns `{ "action": "block" }` or `{ "action": "allow" }`.
6. If `"block"`: prints `{"decision": "block", "reason": "Credential file access denied by security policy."}` and exits with code 2.
7. If `"allow"` or backend unreachable: exits with code 0 (allow — fail-open for availability).

**Pattern matching scope**:
- `Read` tool: block if `file_path` contains `credentials/credentials.json` or `credentials/*.json`
- `Bash` tool: block if `command` matches regex patterns for credential file access:
  - `(cat|less|head|tail|more|xxd|hexdump)\s+.*credentials/`
  - `python.*open.*credentials/`
  - `jq\s+.*credentials/`
  - `cp\s+.*credentials/`
  - `curl.*file://.*credentials/`
- `Write`/`Edit` tools: block if targeting credential files (prevents overwriting with attacker-controlled values)

#### Google ADK — Direct Tool Interception

Since the ADK adapter defines `Bash()` and `Read()` as our own Python functions (see `google_adk.py:226-311`), we intercept directly in those functions — no external hook mechanism needed.

**File**: `backend/app/env-templates/app_core_base/core/server/adapters/google_adk.py`

Modify the `Bash()` and `Read()` function definitions:

```python
def Bash(command: str) -> dict:
    # Check if command targets credential files
    if _is_credential_access(command, tool_type="bash"):
        action = _report_credential_access(command, tool_type="bash")
        if action == "block":
            return {
                "status": "blocked",
                "command": command,
                "return_code": 1,
                "stdout": "",
                "stderr": "Credential file access denied by security policy."
            }
    # ... existing execution logic ...

def Read(file_path: str) -> dict:
    # Check if path targets credential files
    if _is_credential_access(file_path, tool_type="read"):
        action = _report_credential_access(file_path, tool_type="read")
        if action == "block":
            return {
                "status": "blocked",
                "error": "Credential file access denied by security policy."
            }
    # ... existing read logic ...
```

**Shared helper** (new module): `backend/app/env-templates/app_core_base/core/server/security/credential_access_detector.py`

```python
CREDENTIAL_PATH_PATTERNS = [
    r"credentials/credentials\.json",
    r"credentials/[a-f0-9-]+\.json",  # service account files
]

BASH_CREDENTIAL_PATTERNS = [
    r"(cat|less|head|tail|more|xxd|hexdump|strings)\s+.*credentials/",
    r"python[23]?\s+.*-c\s+.*credentials/",
    r"python[23]?\s+.*open\s*\(.*credentials/",
    r"jq\s+.*credentials/",
    r"cp\s+.*credentials/",
    r"curl.*file://.*credentials/",
    r"base64\s+.*credentials/",
]

def is_credential_access(input_value: str, tool_type: str) -> bool:
    """Check if a tool input targets credential files."""
    ...
```

This module is shared between the Claude Code hook script and the ADK adapter inline checks.

### Blockable Event Reporting Protocol

**New endpoint on environment server**: `POST /security/report`

The environment server acts as a proxy between the SDK hooks and the backend:

```python
# In routes.py (environment server)
@router.post("/security/report")
async def report_security_event(event: SecurityEventReport) -> SecurityEventResponse:
    """
    Proxy security event to backend and return action decision.
    Called by SDK hooks/interceptors. Synchronous — caller waits for response.
    """
    try:
        response = await httpx_client.post(
            f"{BACKEND_URL}/api/v1/security-events/report",
            json=event.dict(),
            headers={"Authorization": f"Bearer {AGENT_AUTH_TOKEN}"},
            timeout=3.0,  # 3-second timeout — don't block agent too long
        )
        return SecurityEventResponse(**response.json())
    except Exception:
        # Fail-open: if backend unreachable, allow the tool call
        logger.warning("Backend unreachable for security event report, allowing tool call")
        return SecurityEventResponse(action="allow")
```

**Request model**:
```python
class SecurityEventReport(BaseModel):
    event_type: str          # "CREDENTIAL_READ_ATTEMPT", "CREDENTIAL_BASH_ACCESS"
    tool_name: str           # "Read", "Bash", "Edit"
    tool_input: str          # The file path or command
    session_id: str | None
    environment_id: str | None
```

**Response model**:
```python
class SecurityEventResponse(BaseModel):
    action: str = "allow"    # "allow" | "block"
    reason: str | None = None
```

**Backend endpoint**: `POST /api/v1/security-events/report`

For the initial implementation, this endpoint:
1. Logs the event to the `security_event` table (Phase 3).
2. Returns `{ "action": "allow" }` (default — no blocking logic yet).

The blocking logic is intentionally left as a hook point. Future implementations can add policy evaluation here (e.g., block all credential access in guest sessions, block after N attempts, etc.) without changing the SDK-side code.

### Settings File Integration

**File**: `backend/app/services/environment_lifecycle.py`

In environment creation/rebuild, write the Claude Code hook settings:

1. Write `/app/core/hooks/credential_guard_hook.py` (the hook script).
2. Patch `/app/core/.claude/settings.json` to include the `PreToolUse` hook configuration.

For ADK: no settings file changes needed — interception is inline in the adapter code.

### Future SDK Support

Any new SDK adapter added to the platform should implement credential access interception following the same pattern:
- Use the shared `credential_access_detector.py` for pattern matching.
- Call `SecurityEventReporter.report()` for blockable event reporting.
- The interception mechanism depends on the SDK: hooks (Claude Code), inline (ADK), middleware, etc.

---

## Phase 2 — Output Redaction Pipeline

### Goal

Before any agent output leaves the environment server, scan it against known credential values. Replace matches with `***REDACTED***`. This catches cases where credentials leak through indirect means (e.g., agent reads a script that contains credentials, environment variable dumps, etc.).

### Reusing Existing Field Definitions

The `CredentialsService` already defines exactly which fields are sensitive:

```python
# backend/app/services/credentials_service.py

SENSITIVE_FIELDS = {
    "email_imap": ["password"],
    "odoo": ["api_token"],
    "gmail_oauth": ["access_token", "refresh_token"],
    "api_token": ["http_header_value"],
    "google_service_account": ["private_key", "private_key_id"],
    # ... etc
}
```

The output redaction pipeline reuses these same field definitions to extract the actual plaintext values that need redacting. This ensures consistency — the same fields that are redacted in the prompt README are also caught in output scanning.

### Architecture

**New module**: `backend/app/env-templates/app_core_base/core/server/security/credential_guard.py`

```python
class CredentialGuard:
    """
    Holds the set of known sensitive credential values for this environment.
    Provides output redaction scanning.
    """

    # Mirror of CredentialsService.SENSITIVE_FIELDS — the source of truth for
    # which fields contain values that must be redacted from output.
    SENSITIVE_FIELDS = {
        "email_imap": ["password"],
        "odoo": ["api_token"],
        "gmail_oauth": ["access_token", "refresh_token"],
        "gmail_oauth_readonly": ["access_token", "refresh_token"],
        "gdrive_oauth": ["access_token", "refresh_token"],
        "gdrive_oauth_readonly": ["access_token", "refresh_token"],
        "gcalendar_oauth": ["access_token", "refresh_token"],
        "gcalendar_oauth_readonly": ["access_token", "refresh_token"],
        "api_token": ["http_header_value"],
        "google_service_account": ["private_key", "private_key_id"],
    }

    MIN_VALUE_LENGTH = 8  # Skip short values to avoid false positives

    def __init__(self):
        self._sensitive_values: set[str] = set()

    def update_values(self, credentials_data: list[dict]) -> None:
        """
        Extract sensitive values from credentials data using SENSITIVE_FIELDS.
        Called whenever credentials are synced to the container.

        credentials_data format (same as credentials.json structure):
        [
            {
                "type": "email_imap",
                "credential_data": {"host": "...", "password": "secret123", ...}
            },
            ...
        ]
        """
        self._sensitive_values.clear()
        for cred in credentials_data:
            cred_type = cred.get("type", "")
            cred_data = cred.get("credential_data", {})
            sensitive_fields = self.SENSITIVE_FIELDS.get(cred_type, [])
            for field in sensitive_fields:
                value = cred_data.get(field)
                if isinstance(value, str) and len(value) >= self.MIN_VALUE_LENGTH:
                    self._sensitive_values.add(value)

    def redact(self, text: str) -> tuple[str, bool]:
        """
        Scan text for sensitive values. Replace matches with ***REDACTED***.
        Returns (redacted_text, was_redacted).
        """
        was_redacted = False
        for value in self._sensitive_values:
            if value in text:
                text = text.replace(value, "***REDACTED***")
                was_redacted = True
        return text, was_redacted
```

### Redaction Injection Point

**File**: `backend/app/env-templates/app_core_base/core/server/routes.py`

The `/chat/stream` endpoint yields SSE events from the SDK adapter. Wrap the generator:

```python
async def _redacted_stream(
    source: AsyncIterator[SDKEvent],
    guard: CredentialGuard,
    event_reporter: SecurityEventReporter,
    session_id: str | None,
):
    async for event in source:
        if event.type in (SDKEventType.ASSISTANT, SDKEventType.TOOL_USE):
            redacted_content, was_redacted = guard.redact(event.content)
            if was_redacted:
                event = dataclasses.replace(event, content=redacted_content)
                # Fire-and-forget (non-blocking) — redaction event is informational
                asyncio.create_task(
                    event_reporter.report_async(
                        event_type="OUTPUT_REDACTED",
                        session_id=session_id,
                        details={"event_subtype": event.type.value},
                    )
                )
        yield event
```

The `CredentialGuard` instance is a module-level singleton, updated by `AgentEnvService.update_credentials()` after each credential sync.

### Backend: Feeding Redaction Values

**Option A (preferred)**: The environment server extracts sensitive values locally from the credentials data it already receives. The `CredentialGuard.update_values()` method reads `credentials_data` (the same data written to `credentials.json`) and applies `SENSITIVE_FIELDS` to extract values. No backend changes needed — the field definitions are duplicated in the environment server code.

**Option B (alternative)**: The backend sends an explicit `redaction_values: list[str]` alongside the credential payload. This avoids duplicating `SENSITIVE_FIELDS` but sends plaintext secrets over the wire explicitly labeled as "values to redact." Option A is preferred because the data is already being sent (in `credentials_data`) — we just need to extract from it.

### Considerations

- **Minimum value length (8 chars)**: Prevents false positives from short common values like port numbers, "Bearer", "true", etc.
- **Credential rotation**: When `update_credentials()` is called, `CredentialGuard` rebuilds its value set from scratch. Old values are purged.
- **Performance**: String scanning is O(n*m) where n = text length, m = number of sensitive values. For typical credential counts (<20 values) and message sizes (<10KB), this is negligible.

---

## Phase 3 — Security Event Logging

### Goal

Create a dedicated `security_event` table to store credential access attempts, output redaction triggers, and other security-relevant patterns. Expose a backend API for:
- **Ingest** (called by environment server) — including the blockable `/report` endpoint for Phase 1.
- **Retrieval** (called by frontend or admin) — read-only audit view.

No escalation logic, risk scoring, or automated responses in this phase. The table and API are designed to support event-driven reactions in the future without schema changes.

### Data Model

**New file**: `backend/app/models/security_event.py`

```python
import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field


class SecurityEvent(SQLModel, table=True):
    __tablename__ = "security_event"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Context — who and where
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")
    agent_id: uuid.UUID | None = Field(default=None, foreign_key="agent.id", ondelete="SET NULL")
    environment_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_environment.id", ondelete="SET NULL"
    )
    session_id: uuid.UUID | None = Field(default=None, foreign_key="session.id", ondelete="SET NULL")
    guest_share_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent_guest_share.id", ondelete="SET NULL"
    )

    # Event classification
    event_type: str  # "CREDENTIAL_READ_ATTEMPT", "OUTPUT_REDACTED", etc.
    severity: str = Field(default="medium")  # "low", "medium", "high", "critical"

    # Free-form details (JSON string)
    details: str = Field(default="{}")

    # Reserved for future risk scoring engine
    risk_score: float | None = Field(default=None)


# --- Pydantic schemas ---

class SecurityEventCreate(SQLModel):
    agent_id: uuid.UUID | None = None
    environment_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    guest_share_id: uuid.UUID | None = None
    event_type: str
    severity: str = "medium"
    details: dict = {}


class SecurityEventPublic(SQLModel):
    id: uuid.UUID
    created_at: datetime
    user_id: uuid.UUID
    agent_id: uuid.UUID | None
    environment_id: uuid.UUID | None
    session_id: uuid.UUID | None
    guest_share_id: uuid.UUID | None
    event_type: str
    severity: str
    details: dict
    risk_score: float | None


class SecurityEventsPublic(SQLModel):
    data: list[SecurityEventPublic]
    count: int
```

**Indexes**:
- `user_id` (btree)
- `agent_id` (btree)
- `session_id` (btree)
- `event_type` (btree)
- `created_at` (btree DESC)
- Composite: `(guest_share_id, created_at DESC)`

### Event Types

| Event Type | Default Severity | Trigger |
|------------|-----------------|---------|
| `CREDENTIAL_READ_ATTEMPT` | high | SDK tool interceptor detected credential file read |
| `CREDENTIAL_BASH_ACCESS` | high | Bash command matched credential-access pattern |
| `OUTPUT_REDACTED` | medium | Credential value found and redacted in agent output |
| `CREDENTIAL_WRITE_ATTEMPT` | high | Attempt to write/edit credential files |

### API Endpoints

**New file**: `backend/app/api/routes/security_events.py`

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/security-events/report` | `AGENT_AUTH_TOKEN` | **Blockable** ingest — logs event, returns `{ action: "allow" \| "block" }` |
| `POST` | `/api/v1/security-events/` | `AGENT_AUTH_TOKEN` | Fire-and-forget ingest (for non-blockable events like OUTPUT_REDACTED) |
| `GET` | `/api/v1/security-events/` | `CurrentUser` | List events for current user (paginated, filterable) |

#### Blockable Report Endpoint Design

```python
@router.post("/report")
async def report_security_event(
    event_data: SecurityEventReport,
    session: SessionDep,
    current_user: CurrentUser,  # resolved from AGENT_AUTH_TOKEN
) -> SecurityEventReportResponse:
    """
    Ingest a security event and return an action decision.
    Called synchronously by SDK hooks — must respond quickly.
    """
    # 1. Log the event
    security_event = await SecurityEventService.create_event(
        session=session,
        user_id=current_user.id,
        data=event_data,
    )

    # 2. Determine action (initially: always allow)
    # Future: plug in policy engine here
    action = "allow"

    return SecurityEventReportResponse(action=action, reason=None)
```

The `action` field is the hook point for future policy logic. Initially it always returns `"allow"`. When we add risk detection later, this is where the policy engine evaluates and returns `"block"` when appropriate — no SDK-side changes needed.

### Service Layer

**New file**: `backend/app/services/security_event_service.py`

```python
class SecurityEventService:
    @staticmethod
    async def create_event(
        session: Session,
        user_id: uuid.UUID,
        data: SecurityEventCreate,
    ) -> SecurityEvent:
        """Validate ownership of agent_id/environment_id, write record."""
        ...

    @staticmethod
    async def list_events(
        session: Session,
        user_id: uuid.UUID,
        filters: dict,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[SecurityEvent], int]:
        """Query events for a user with optional filters."""
        ...
```

### Database Migration

- Create `security_event` table with all columns and indexes.
- Foreign keys: `CASCADE` on `user_id`, `SET NULL` on all others.

### Route Registration

**File**: `backend/app/api/main.py`

```python
from app.api.routes import security_events
api_router.include_router(
    security_events.router, prefix="/security-events", tags=["security-events"]
)
```

---

## Security Architecture Summary

| Layer | Mechanism | SDK Coverage | Bypass Risk |
|-------|-----------|-------------|-------------|
| Encryption at rest | Fernet + PBKDF2 | N/A (backend) | Very Low |
| Field whitelisting | `AGENT_ENV_ALLOWED_FIELDS` | N/A (backend) | Very Low |
| Value redaction in prompt | README.md generation | N/A (backend) | Very Low |
| MCP exclusion | Credentials folder excluded from MCP resources | N/A (backend) | Very Low |
| **Phase 1** SDK tool interception | Claude Code hooks + ADK inline | All current SDKs | Medium (creative bypass possible) |
| **Phase 2** Output redaction | Environment server stream wrapper | All SDKs | Low (server-side, no bypass) |
| **Phase 3** Security event logging | Backend audit trail | All SDKs | N/A (audit, not prevention) |

Defense in depth: Phase 1 tries to prevent access, Phase 2 catches leaks that slip through, Phase 3 records everything for analysis. The blockable reporting protocol in Phase 1 allows the backend to evolve its blocking policy independently of the SDK code.

---

## Implementation Checklist

### Phase 3 — Security Event Logging (implement first)

**Backend**:
- [ ] Create `backend/app/models/security_event.py` with model, schemas, and event type constants
- [ ] Generate Alembic migration: `add_security_event_table`
- [ ] Create `backend/app/services/security_event_service.py` with `create_event()` and `list_events()`
- [ ] Create `backend/app/api/routes/security_events.py` with `/report` (blockable) and `/` (fire-and-forget) ingest endpoints, plus `GET /` for retrieval
- [ ] Register router in `backend/app/api/main.py`

### Phase 1 — SDK-Level Tool Interception

**Environment server**:
- [ ] Create `backend/app/env-templates/app_core_base/core/server/security/credential_access_detector.py` with shared pattern matching logic
- [ ] Create `backend/app/env-templates/app_core_base/core/server/security/event_reporter.py` with `SecurityEventReporter` (synchronous for blockable, async for fire-and-forget)
- [ ] Add `POST /security/report` proxy endpoint to environment server `routes.py`

**Claude Code adapter**:
- [ ] Create `backend/app/env-templates/app_core_base/core/hooks/credential_guard_hook.py`
- [ ] Modify `backend/app/services/environment_lifecycle.py`: write hook settings to `/app/core/.claude/settings.json`

**Google ADK adapter**:
- [ ] Modify `backend/app/env-templates/app_core_base/core/server/adapters/google_adk.py`: add credential access checks to `Bash()` and `Read()` functions

### Phase 2 — Output Redaction

**Environment server**:
- [ ] Create `backend/app/env-templates/app_core_base/core/server/security/credential_guard.py` with `CredentialGuard` class (uses `SENSITIVE_FIELDS` to extract values from credentials data)
- [ ] Modify `backend/app/env-templates/app_core_base/core/server/routes.py`: wrap SSE stream generator with `_redacted_stream()` using `CredentialGuard`
- [ ] Modify `backend/app/env-templates/app_core_base/core/server/agent_env_service.py`: `update_credentials()` calls `CredentialGuard.update_values()` after writing credential files

### Client Regeneration
- [ ] Run `bash scripts/generate-client.sh` after adding new backend routes

---

## Error Handling and Edge Cases

| Scenario | Handling |
|----------|----------|
| Backend unreachable during blockable report (Phase 1) | Fail-open: allow the tool call. Log warning locally. Security event will be missing from audit trail. |
| `CredentialGuard` not yet initialized (Phase 2) | Redaction is skipped (no values to scan). Log a warning. |
| Hook script fails to start (Phase 1, Claude Code) | Claude Code treats hook failures as non-blocking by default. Tool call proceeds. |
| Blockable report timeout (>3 seconds) | Fail-open: allow the tool call. Backend may be under load. |
| Redaction false positive (Phase 2) | Minimum 8-char length filter prevents most cases. Structural values (URLs, "Bearer") are not in `SENSITIVE_FIELDS`. |
| Credentials rotated mid-session | `update_credentials()` rebuilds `CredentialGuard` from scratch. Old values purged. |
| Creative bypass (agent encodes credentials in base64, splits across messages) | Phase 2 redaction won't catch encoded/split values. This is a known limitation — future ML-based detection can address it. |

---

## Integration Points

| System | Integration |
|--------|------------|
| `credentials_service.py` | `SENSITIVE_FIELDS` definitions reused by `CredentialGuard` for value extraction |
| `environment_lifecycle.py` | Extended to write Claude Code hook settings into container |
| `agent_env_service.py` | `update_credentials()` feeds `CredentialGuard` with new credential data |
| `routes.py` (env server) | SSE stream wrapped with redaction; new `/security/report` proxy endpoint |
| `google_adk.py` (adapter) | `Bash()` and `Read()` functions extended with credential access checks |
| `Activity` model | Pattern reference for `SecurityEvent` model design |
| `AgentGuestShare` model | `guest_share_id` on `SecurityEvent` enables guest-specific queries |
| `Session` model | `session_id` on `SecurityEvent` links events to sessions |
| `AGENT_AUTH_TOKEN` | Used by `SecurityEventReporter` to authenticate backend API calls |

---

## Backward Compatibility

- Existing environments continue to work unchanged. New interception code only activates when the updated environment server and hook files are deployed.
- Claude Code hooks only activate if the settings file is present. Containers without updated `environment_lifecycle.py` will not have hooks.
- ADK inline checks are a code change in the adapter — new containers get them, old ones don't.
- The `CredentialGuard` is initialized empty. If no credentials are synced, no redaction occurs.
- The new backend endpoints are additive. No existing endpoints are changed.

---

## Future Improvements

These are deferred to future phases. The current architecture supports them without schema changes:

- **Event-driven risk detection**: Policy engine that evaluates `SecurityEvent` patterns and triggers automated responses (disable guest link, pause environment, notify owner). The blockable `/report` endpoint is already the hook point — just add policy evaluation logic.
- **ML-based anomaly detection**: Train a model on normal credential access patterns per agent and flag statistical outliers.
- **Per-credential sensitivity policies**: Add `sensitivity_level` to `Credential` model. High-sensitivity credentials trigger stricter blocking policies in the `/report` endpoint.
- **Credential access approval workflow**: Agent requests credential via a custom tool. Backend holds value in escrow, asks owner to approve. On approval, sends only the requested value to session context.
- **SIEM integration**: Webhook-based forwarding of `SecurityEvent` records to external systems (Splunk, Datadog).
- **Credential rotation on security events**: Auto-expire OAuth credentials on critical events. Notify owner to rotate API tokens.
- **Encoded value detection**: Detect base64-encoded, hex-encoded, or split credential values in output using fuzzy matching or ML classifiers.
- **Cross-session correlation**: Detect patterns across multiple sessions (e.g., same agent environment triggering events in different guest sessions).

---

*Plan created: 2026-03-09*
