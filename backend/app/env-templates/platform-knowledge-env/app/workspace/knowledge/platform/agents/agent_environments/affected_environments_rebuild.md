# Affected Environments Rebuild

## Purpose

When an AI credential is updated or set as default, all environments using that credential must be rebuilt to receive the new configuration. This document covers the rebuild behavior from the environment lifecycle perspective.

## Core Concepts

- **Credential-Triggered Rebuild** - A rebuild initiated automatically after an AI credential update, as opposed to a user-initiated manual rebuild from the environment card
- **Status Preservation** - After a credential-triggered rebuild, each environment returns to its exact pre-rebuild operational state
- **Parallel Batch Rebuild** - Multiple environments can rebuild concurrently; each is independent

## Trigger Conditions

A credential-triggered rebuild is prompted when the user:
1. **Updates a credential** (API key, name, or configuration) via Settings > AI Credentials
2. **Sets a credential as default** by clicking the star icon

After either action, the system identifies all environments using the affected credential and presents the [Affected Environments Widget](../../application/ai_credentials/affected_environments_widget.md) to manage rebuilds.

## Rebuild Behavior

### Status Preservation

Environments return to their pre-rebuild operational state after the rebuild:

| Pre-Rebuild State | Post-Rebuild State | Credential Effect |
|---|---|---|
| `running` | `running` (restarted) | New credentials applied immediately |
| `suspended` | `suspended` | New credentials ready; applied on next activation |
| `stopped` | `stopped` | New credentials ready; applied on next start |

### Rebuild Process

For each selected environment:
1. Container stopped (if running)
2. Old container removed (`docker-compose down`)
3. Infrastructure files overwritten from template
4. Docker image rebuilt — new credential values baked into `.env`
5. Container recreated with fresh configuration
6. Container restarted only if the environment was previously running
7. WebSocket events emit real-time status changes throughout

### Parallel Execution

- Each rebuild runs independently — a failure on one does not block others
- Batch rebuilds execute in parallel via `Promise.allSettled`
- Toast confirms rebuild start; dialog can be closed immediately
- Progress tracked via WebSocket status events

## Business Rules

- Credential-triggered rebuilds use the same mechanism as the manual rebuild from the environment card
- Running environments are automatically restarted; suspended/stopped environments are **not** automatically activated
- Users can only rebuild their own environments; shared credential recipients cannot rebuild the owner's environments
- Environments continue using old credentials until a rebuild completes — skipping is always safe
- Partial batch failures do not affect successfully rebuilt environments

## Architecture Overview

```
AI Credential updated or set as default
          ↓
GET /api/v1/ai-credentials/{id}/affected-environments
AICredentialsService.get_affected_environments()
Queries AgentEnvironment WHERE conversation_ai_credential_id OR building_ai_credential_id = credential_id
          ↓
Affected Environments Widget presents list
(see: docs/application/ai_credentials/affected_environments_widget.md)
          ↓
User selects environments → POST /api/v1/environments/{id}/rebuild (parallel per selected env)
          ↓
EnvironmentLifecycleManager.rebuild_environment() per environment:
  stop container → docker-compose down → overwrite infra files → docker build
  → docker-compose up (only if environment was running before)
          ↓
WebSocket events: real-time status updates in frontend
```

## Integration Points

- **[AI Credentials](../../application/ai_credentials/ai_credentials.md)** - Source of the credential update that triggers this flow; `AICredentialsService.get_affected_environments()` provides the environment list
- **[Affected Environments Widget](../../application/ai_credentials/affected_environments_widget.md)** - UI dialog for selecting environments and initiating rebuilds from the credential management side
- **[Agent Environments](./agent_environments.md)** - Core rebuild mechanics shared by all rebuild types; see Environment Rebuild section
- **[Realtime Events](../../application/realtime_events/event_bus_system.md)** - WebSocket events (`ENVIRONMENT_REBUILDING`, status changes) for real-time progress updates
