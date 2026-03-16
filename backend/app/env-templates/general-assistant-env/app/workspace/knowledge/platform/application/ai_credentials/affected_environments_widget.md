# Affected Environments Widget

## Purpose

After AI credential updates, automatically detect which environments use the credential and enable batch or individual rebuilds to apply the changes.

## User Flow

1. User updates an AI credential (API key change) or sets a new default
2. Dialog appears automatically showing affected environments
3. All environments pre-selected by default for quick batch rebuild
4. User can:
   - **Rebuild all** (default) - batch rebuild all selected environments
   - **Selective rebuild** - uncheck some environments, rebuild the rest
   - **Individual rebuild** - per-environment rebuild button
   - **Skip** - close dialog without rebuilding
5. Rebuilds execute in background; dialog is closable during operation
6. Status preservation: suspended/stopped environments stay unchanged, running environments restart

## Widget Details

- Shows credential usage per environment: "conversation", "building", or "conversation & building"
- Displays shared users who also use the credential
- Background execution with WebSocket progress updates
- Batch execution via parallel requests

## Architecture Overview

```
Credential updated → Frontend triggers affected-environments API call
                  ↓
Backend queries environments linked to credential → Returns list with usage info
                  ↓
Dialog renders environment list → User selects which to rebuild
                  ↓
Rebuild triggered → POST /api/v1/environments/{id}/rebuild (parallel)
                  ↓
WebSocket progress updates → UI reflects rebuild status
```

## Business Rules

- Widget auto-appears after credential update or set-default operations
- All environments pre-selected by default (opt-out model)
- Suspended/stopped environments are not automatically restarted
- Running environments are restarted after rebuild
- Shared users' environments are shown for visibility but rebuilt with same logic

## Integration Points

- **AI Credentials Service** - `get_affected_environments()` provides the environment list. See [AI Credentials](ai_credentials.md)
- **Environment Rebuild** - Uses existing `POST /api/v1/environments/{id}/rebuild` endpoint
- **WebSocket Events** - Progress updates via real-time event system. See [Event Bus](../realtime_events/event_bus_system.md)

## Component Structure

- `frontend/src/components/UserSettings/AffectedEnvironmentsDialog.tsx` - Main dialog component
- Triggered from `AICredentialDialog.tsx` (after update) and `AICredentials.tsx` (after set-default)

## API

- `GET /api/v1/ai-credentials/{credential_id}/affected-environments` - Returns `AffectedEnvironmentsPublic` with environment details, usage type, and shared users

---

*Last updated: 2026-03-02*
