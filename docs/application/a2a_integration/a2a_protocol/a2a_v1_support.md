# A2A Protocol v1.0 Support

## Overview

Adapter layer strategy for supporting A2A Protocol v1.0 while maintaining backward compatibility with the a2a library (v0.3.0 draft). The adapter transforms requests/responses at the API boundary without modifying internal services.

## Protocol Differences

| Aspect | Library (v0.3.0 draft) | A2A v1.0 Spec |
|--------|------------------------|---------------|
| Method Names | `message/send`, `message/stream`, `tasks/get`, `tasks/cancel` | `SendMessage`, `SendStreamingMessage`, `GetTask`, `CancelTask`, `ListTasks` |
| Field Naming | camelCase via Pydantic alias (automatic) | camelCase (same) |
| AgentCard.protocolVersion | `protocolVersion: string` | `protocolVersions: string[]` |
| AgentCard.url | `url: string` | `supportedInterfaces: AgentInterface[]` with `protocolBinding` and `protocolVersion` fields |
| Extended Card | `supportsAuthenticatedExtendedCard` | `capabilities.extendedAgentCard` |
| Task States | kebab-case (`input-required`) | kebab-case (same) |
| Task/Message | No discriminator | `"kind": "task"` or `"kind": "message"` |

## Protocol Version Selection

Protocol version is determined by the URL the client connects to — no request headers are needed.

| URL | Protocol | Notes |
|-----|----------|-------|
| `/api/v1/a2a/{agent_id}/` | v1.0 (latest) | Default — what new clients should use |
| `/api/v1/a2a/v1.0/{agent_id}/` | v1.0 (explicit) | Identical to base URL behavior |
| `/api/v1/a2a/v0.3/{agent_id}/` | v0.3.0 (legacy) | No transformation applied — library native |

The `X-A2A-Stable: 1` header is no longer honored; it is ignored. Protocol version is URL-only.

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
| `url` (string) | `supportedInterfaces` (array of `{url, protocolBinding, protocolVersion}`) |
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

- `A2AV1Adapter.transform_request_inbound(body)` - Transform v1.0 method names (PascalCase to slash-case)
- `A2AV1Adapter.transform_agent_card_outbound(card)` - Transform AgentCard structure; builds distinct versioned URLs in `supportedInterfaces`
- `A2AV1Adapter.transform_task_outbound(task)` - Add `kind` discriminator
- `A2AV1Adapter.transform_message_outbound(message)` - Add `kind` discriminator
- `A2AV1Adapter.transform_sse_event_outbound(event)` - Pass-through (already formatted)

### Route Integration

**File:** `backend/app/api/routes/a2a.py`

Three routers share the same two underlying handler functions `_get_agent_card()` and `_handle_jsonrpc()`, which receive a `protocol_version` parameter:

| Router prefix | `protocol_version` passed | Adapter applied |
|---------------|--------------------------|-----------------|
| `/a2a` | `"latest"` (resolves to v1.0) | Yes |
| `/a2a/v1.0` | `"v1.0"` | Yes |
| `/a2a/v0.3` | `"v0.3"` | No — passthrough |

For v1.0 endpoints: `transform_agent_card_outbound()` is called, producing `supportedInterfaces` with versioned URLs. For v0.3 endpoints: the library-native card is returned with a v0.3-specific URL set via `url_override`.

## Migration Notes

### For Clients

- **New clients**: Connect to `/api/v1/a2a/{agent_id}/` (latest). Read `supportedInterfaces` in the AgentCard to discover versioned endpoints.
- **Legacy v0.3 clients**: Connect to `/api/v1/a2a/v0.3/{agent_id}/` and use slash-case method names (`message/send`, `tasks/get`, etc.)
- **Explicit v1.0 clients**: Connect to `/api/v1/a2a/v1.0/{agent_id}/` and use PascalCase method names (`SendMessage`, `GetTask`, etc.)
- **No header needed**: The `X-A2A-Stable` header is no longer supported and is ignored.

### Breaking Changes vs. Previous Header-Based Approach

1. `X-A2A-Stable: 1` header no longer selects the protocol — use the v0.3 URL instead
2. `supportedInterfaces` now contains distinct versioned URLs (not the same URL twice)
3. The v0.3 card URL points to the v0.3-specific endpoint rather than the base URL

### Future Work

1. Full v1.0 Validation - Pydantic models for v1.0 request validation
2. Version Negotiation - Support Accept header or query param for version selection
3. Library Update - Simplify adapter when a2a library supports v1.0 natively
4. Extended AgentCard - Full implementation of `GetExtendedAgentCard`
5. Push Notification Config - v1.0 push notification config methods

## Implementation Status

- [x] `backend/app/services/a2a/a2a_v1_adapter.py` - Adapter class; versioned URLs in `supportedInterfaces`
- [x] `backend/app/services/a2a/a2a_service.py` - `url_override` parameter for versioned card URLs
- [x] `GET /a2a/{agent_id}/` route - latest/v1.0 AgentCard with versioned `supportedInterfaces`
- [x] `GET /a2a/v1.0/{agent_id}/` route - explicit v1.0 AgentCard
- [x] `GET /a2a/v0.3/{agent_id}/` route - v0.3 native card (no adapter)
- [x] `POST /a2a/{agent_id}/` route - latest/v1.0 JSON-RPC (PascalCase method names)
- [x] `POST /a2a/v1.0/{agent_id}/` route - explicit v1.0 JSON-RPC
- [x] `POST /a2a/v0.3/{agent_id}/` route - v0.3 JSON-RPC passthrough (slash-case method names)
- [ ] Unit tests for adapter transformations
- [ ] Integration tests for all three versioned URLs

---

*Last updated: 2026-04-16*
