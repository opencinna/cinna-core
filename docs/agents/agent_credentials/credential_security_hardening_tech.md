# Credential Security Hardening — Technical Details

## File Locations

### Backend — Models & Migration

- `backend/app/models/security_event.py` — `SecurityEvent` table model, `SecurityEventCreate`, `SecurityEventPublic`, `SecurityEventsPublic` schemas, event type constants
- `backend/app/alembic/versions/5432134b1366_add_security_event_table.py` — migration creating `security_event` table with indexes

### Backend — Service Layer

- `backend/app/services/security_event_service.py` — `SecurityEventService` with `create_event()`, `create_event_from_report()`, `list_events()`, `to_public()`

### Backend — API Routes

- `backend/app/api/routes/security_events.py` — `SecurityEventReport`, `SecurityEventReportResponse` request/response models; three endpoints: `/report`, `POST /`, `GET /`
- `backend/app/api/main.py` — router registration under prefix `/security-events`

### Agent Environment — Security Module

- `backend/app/env-templates/app_core_base/core/server/security/__init__.py` — package init
- `backend/app/env-templates/app_core_base/core/server/security/credential_access_detector.py` — `is_credential_access()`, `get_event_type()`, pattern constants
- `backend/app/env-templates/app_core_base/core/server/security/credential_guard.py` — `CredentialGuard` class (singleton), `SENSITIVE_FIELDS`, `update_values()`, `redact()`
- `backend/app/env-templates/app_core_base/core/server/security/event_reporter.py` — `SecurityEventReporter` with sync `report()` and async `report_async()`, `_build_payload()`

### Agent Environment — Hook Script

- `backend/app/env-templates/app_core_base/core/hooks/credential_guard_hook.py` — Claude Code PreToolUse hook, reads stdin JSON, checks patterns, reports event, exits 0/2

### Modified Files

- `backend/app/env-templates/app_core_base/core/server/routes.py` — added `_redacted_event_stream()`, `POST /security/report` proxy endpoint, SSE stream wrapping
- `backend/app/env-templates/app_core_base/core/server/adapters/google_adk.py` — added `_check_credential_access()`, inline interception in `Bash()` and `Read()` functions
- `backend/app/env-templates/app_core_base/core/server/agent_env_service.py` — `update_credentials()` now calls `credential_guard.update_values()` after credential sync
- `backend/app/services/environment_lifecycle.py` — added `_write_claude_code_hook_settings()`, called during environment configuration
- `backend/app/models/__init__.py` — added `SecurityEvent` model import

### Tests

- `backend/tests/api/security_events/test_security_events.py` — API tests for security event endpoints

### Frontend (auto-generated)

- `frontend/src/client/types.gen.ts` — `SecurityEventReport`, `SecurityEventReportResponse`, `SecurityEventPublic` types
- `frontend/src/client/sdk.gen.ts` — `SecurityEventsService` with `reportSecurityEvent`, `ingestSecurityEvent`, `listSecurityEvents`
- `frontend/src/client/schemas.gen.ts` — Zod schemas for security event types

## Database Schema

Table: `security_event`

Key fields:
- `id` (UUID PK), `created_at` (datetime)
- `user_id` (FK → `user.id`, CASCADE)
- `agent_id` (FK → `agent.id`, SET NULL, nullable)
- `environment_id` (FK → `agent_environment.id`, SET NULL, nullable)
- `session_id` (FK → `session.id`, SET NULL, nullable)
- `guest_share_id` (FK → `agent_guest_share.id`, SET NULL, nullable)
- `event_type` (str), `severity` (str, default "medium")
- `details` (str — JSON-encoded), `risk_score` (float, nullable — reserved for future)

Indexes: `user_id`, `agent_id`, `session_id`, `event_type`, `created_at DESC`, composite `(guest_share_id, created_at DESC)`

Migration: `backend/app/alembic/versions/5432134b1366_add_security_event_table.py`

## API Endpoints

### Backend Routes (`backend/app/api/routes/security_events.py`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/security-events/report` | `AGENT_AUTH_TOKEN` | Blockable ingest — logs event, returns `{ action, reason }` |
| `POST` | `/api/v1/security-events/` | `AGENT_AUTH_TOKEN` | Fire-and-forget ingest for non-blockable events |
| `GET` | `/api/v1/security-events/` | User JWT | Paginated list (default 50, max 200), filterable by `agent_id`, `environment_id`, `session_id`, `event_type` |

### Environment Server Proxy (`backend/app/env-templates/app_core_base/core/server/routes.py`)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/security/report` | None (localhost only) | Proxy to backend `/api/v1/security-events/report` with `AGENT_AUTH_TOKEN` header |

## Services & Key Methods

### `SecurityEventService` (`backend/app/services/security_event_service.py`)

- `create_event(session, user_id, data)` — creates SecurityEvent from `SecurityEventCreate` schema
- `create_event_from_report(session, user_id, event_type, severity, details, tool_name, tool_input, ...)` — creates event from raw report payload, merges tool info into details, parses UUID strings safely
- `list_events(session, user_id, agent_id, environment_id, session_id_filter, event_type, skip, limit)` — paginated query with optional filters, ordered by `created_at DESC`
- `to_public(event)` — converts DB model to public schema, parses JSON details string to dict

### `CredentialGuard` (`backend/app/env-templates/app_core_base/core/server/security/credential_guard.py`)

- `update_values(credentials_data)` — extracts sensitive values from credential data using `SENSITIVE_FIELDS`, replaces the value set
- `redact(text)` — scans text for sensitive values, returns `(redacted_text, was_redacted)`

### `SecurityEventReporter` (`backend/app/env-templates/app_core_base/core/server/security/event_reporter.py`)

- `report(event_type, tool_name, tool_input, session_id, severity, details)` — synchronous POST to proxy, returns "allow"/"block"
- `report_async(event_type, session_id, severity, details)` — async fire-and-forget POST

### `credential_access_detector` (`backend/app/env-templates/app_core_base/core/server/security/credential_access_detector.py`)

- `is_credential_access(input_value, tool_type)` — pattern matching for file paths and bash commands
- `get_event_type(tool_type)` — maps tool type to security event type constant

## Configuration

- `AGENT_AUTH_TOKEN` — JWT token used by the environment server to authenticate with backend security event endpoints
- `BACKEND_URL` — backend URL for the security event proxy (defaults to `http://host.docker.internal:8000`)
- `SERVER_PORT` — environment server port used by the hook script to reach the proxy endpoint
- `ENV_ID`, `AGENT_ID` — environment variables injected by docker-compose, attached to security event payloads

## Security

- Proxy endpoint (`/security/report`) is localhost-only inside the Docker container — no external access
- Backend endpoints use `AGENT_AUTH_TOKEN` for authentication, resolving to the owning user via `CurrentUser` dependency
- `GET /security-events/` is scoped to `current_user.id` — users can only see their own events
- Fail-open design ensures agent availability when backend is unreachable
- 3-second timeout on synchronous reporting prevents agent hangs