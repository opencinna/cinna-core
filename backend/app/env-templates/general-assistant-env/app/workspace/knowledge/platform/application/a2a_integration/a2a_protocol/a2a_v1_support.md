# A2A Protocol v1.0 Support

## Overview

Adapter layer strategy for supporting A2A Protocol v1.0 while maintaining backward compatibility with the a2a library (v0.3.0 draft). The adapter transforms requests/responses at the API boundary without modifying internal services.

## Protocol Differences

| Aspect | Library (v0.3.0 draft) | A2A v1.0 Spec |
|--------|------------------------|---------------|
| Method Names | `message/send`, `message/stream`, `tasks/get`, `tasks/cancel` | `SendMessage`, `SendStreamingMessage`, `GetTask`, `CancelTask`, `ListTasks` |
| Field Naming | camelCase via Pydantic alias (automatic) | camelCase (same) |
| AgentCard.protocolVersion | `protocolVersion: string` | `protocolVersions: string[]` |
| AgentCard.url | `url: string` | `supportedInterfaces: AgentInterface[]` with `transport` field |
| Extended Card | `supportsAuthenticatedExtendedCard` | `capabilities.extendedAgentCard` |
| Task States | kebab-case (`input-required`) | kebab-case (same) |
| Task/Message | No discriminator | `"kind": "task"` or `"kind": "message"` |

## Protocol Version Selection

| Header | Behavior |
|--------|----------|
| (none) | v1.0 format (default) |
| `X-A2A-Stable: 1` | Legacy format (v0.3.0 draft) |

## Key Transformations

### Method Names (Inbound)

| v1.0 Method | Internal Method |
|-------------|-----------------|
| `SendMessage` | `message/send` |
| `SendStreamingMessage` | `message/stream` |
| `GetTask` | `tasks/get` |
| `CancelTask` | `tasks/cancel` |
| `ListTasks` | `tasks/list` |
| `SubscribeToTask` | `tasks/resubscribe` |
| `GetExtendedAgentCard` | `agent/getAuthenticatedExtendedCard` |
| `SetTaskPushNotificationConfig` | `tasks/pushNotificationConfig/set` |
| `GetTaskPushNotificationConfig` | `tasks/pushNotificationConfig/get` |
| `ListTaskPushNotificationConfig` | `tasks/pushNotificationConfig/list` |
| `DeleteTaskPushNotificationConfig` | `tasks/pushNotificationConfig/delete` |

### AgentCard (Outbound)

| v0.3.0 Field | v1.0 Field |
|--------------|------------|
| `protocolVersion` (string) | `protocolVersions` (array) |
| `url` (string) | `supportedInterfaces` (array of `{url, transport}`) |
| `supportsAuthenticatedExtendedCard` | `capabilities.extendedAgentCard` |

### Task/Message (Outbound)

- `Task`: adds `"kind": "task"` discriminator
- `Message`: adds `"kind": "message"` discriminator
- Field names already camelCase via Pydantic (no change needed)

### SSE Events (Outbound)

Already properly formatted by A2AEventMapper:
- `TaskStatusUpdateEvent`: `"kind": "status-update"` (already present)
- `TaskArtifactUpdateEvent`: `"kind": "artifact-update"` (already present)
- No additional transformation needed

## Adapter Implementation

**File:** `backend/app/services/a2a/a2a_v1_adapter.py`

- `A2AV1Adapter.should_use_v1(request)` - Check header for protocol version
- `A2AV1Adapter.transform_request_inbound(body)` - Transform v1.0 method names (PascalCase to slash-case)
- `A2AV1Adapter.transform_agent_card_outbound(card)` - Transform AgentCard structure
- `A2AV1Adapter.transform_task_outbound(task)` - Add `kind` discriminator
- `A2AV1Adapter.transform_message_outbound(message)` - Add `kind` discriminator
- `A2AV1Adapter.transform_sse_event_outbound(event)` - Pass-through (already formatted)

### Route Integration

**File:** `backend/app/api/routes/a2a.py`

- `GET /{agent_id}/` - Calls `should_use_v1()` and `transform_agent_card_outbound()` before returning
- `POST /{agent_id}/` - Calls `transform_request_inbound()` for method name translation, then `transform_task_outbound()` for responses

## Migration Notes

### For Clients

- **Default behavior**: v1.0 compatible format
- **Legacy clients**: Add `X-A2A-Stable: 1` header to use library format
- **Recommended**: Migrate to v1.0 method names (`SendMessage`, `GetTask`, etc.)

### Breaking Changes (v1.0 Mode)

1. AgentCard: `protocolVersion` replaced by `protocolVersions` (string to array)
2. AgentCard: `url` removed, use `supportedInterfaces[0].url`
3. AgentCard: `supportsAuthenticatedExtendedCard` moved to `capabilities.extendedAgentCard`
4. JSON-RPC: Method names must be PascalCase (`SendMessage` not `message/send`)

### Future Work

1. Full v1.0 Validation - Pydantic models for v1.0 request validation
2. Version Negotiation - Support Accept header or query param for version selection
3. Library Update - Simplify adapter when a2a library supports v1.0 natively
4. Extended AgentCard - Full implementation of `GetExtendedAgentCard`
5. Push Notification Config - v1.0 push notification config methods

## Implementation Status

- [x] `backend/app/services/a2a/a2a_v1_adapter.py` - Adapter class
- [x] `GET /{agent_id}/` route - AgentCard transformation
- [x] `POST /{agent_id}/` route - Method name and response transformation
- [ ] Unit tests for adapter transformations
- [ ] Integration tests for both v1.0 and stable modes

---

*Last updated: 2026-03-02*
