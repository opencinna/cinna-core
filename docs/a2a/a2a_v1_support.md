# A2A Protocol v1.0 Support

## Overview

This document describes the adapter layer strategy for supporting A2A Protocol v1.0 while maintaining backward compatibility with the current a2a library (which follows an older draft spec).

## Current Library Analysis

The installed `a2a` library (in `backend/.venv/lib/python3.13/site-packages/a2a/`) provides:

### Serialization (from `_base.py`)
- Uses `A2ABaseModel` with Pydantic's `serialize_by_alias=True`
- Snake_case internally → camelCase on serialization via `alias_generator=to_camel_custom`
- Fields like `context_id` → `contextId`, `message_id` → `messageId` on output

### Hardcoded Method Names (from `types.py`)
The library has Literal types for method names:
```python
# SendMessageRequest
method: Literal['message/send'] = 'message/send'

# SendStreamingMessageRequest
method: Literal['message/stream'] = 'message/stream'

# GetTaskRequest
method: Literal['tasks/get'] = 'tasks/get'

# CancelTaskRequest
method: Literal['tasks/cancel'] = 'tasks/cancel'
```

### AgentCard Structure (from `types.py`)
```python
protocol_version: str | None = '0.3.0'  # serializes to protocolVersion
url: str                                  # main endpoint URL
supports_authenticated_extended_card: bool | None
```

## Problem Statement

The a2a library currently implements an older A2A draft specification (v0.3.0). The A2A v1.0 draft specification introduces several breaking changes:

| Aspect | Current Library (v0.3.0 draft) | A2A v1.0 Spec |
|--------|--------------------------------|---------------|
| Method Names | `message/send`, `message/stream`, `tasks/get`, `tasks/cancel` | `SendMessage`, `SendStreamingMessage`, `GetTask`, `CancelTask`, `ListTasks` |
| Field Naming | ✅ Already camelCase via Pydantic alias | camelCase (`contextId`, `taskId`, `messageId`) |
| AgentCard.protocolVersion | `protocolVersion: string` | `protocolVersions: string[]` |
| AgentCard.url | `url: string` | `supportedInterfaces: AgentInterface[]` with `transport` field |
| Extended Card | `supportsAuthenticatedExtendedCard` | `capabilities.extendedAgentCard` |
| Task States | ✅ Already kebab-case (`input-required`) | kebab-case (same) |
| Task.id | `id: str` | Same |
| Message.messageId | ✅ `message_id` → `messageId` | Same |
| Artifact.artifactId | ✅ `artifact_id` → `artifactId` | Same |

## Solution: Adapter Layer

Since the a2a library doesn't support v1.0 yet, we implement an adapter layer that:
1. Transforms incoming v1.0 method names to internal format (PascalCase → slash-case)
2. Transforms outgoing AgentCard structure to v1.0 format
3. Supports backward compatibility via `X-A2A-Stable: 1` header

**Key insight**: Field naming (camelCase) is already handled by the library's Pydantic serialization. The main transformations needed are:
- **Method names** (inbound): `SendMessage` → `message/send`
- **AgentCard** (outbound): Structural transformation

### Header-Based Protocol Selection

```
X-A2A-Stable: 1  → Use current library format (no transformation)
(no header)       → Use A2A v1.0 format (apply transformations)
```

## Implementation Plan

### Phase 1: Request/Response Adapter

Create `backend/app/services/a2a_v1_adapter.py`:

```python
"""
A2A v1.0 Protocol Adapter

Transforms between v1.0 spec format and internal a2a library format.

The a2a library (v0.3.0 draft):
- Uses method names like `message/send`, `message/stream`, `tasks/get`
- Serializes to camelCase via Pydantic (handled automatically)
- Uses `protocolVersion` (string), `url` (string) in AgentCard

A2A v1.0 spec:
- Uses PascalCase method names: `SendMessage`, `SendStreamingMessage`, `GetTask`
- Uses `protocolVersions` (array), `supportedInterfaces` (array) in AgentCard
"""
from typing import Any
from fastapi import Request


class A2AV1Adapter:
    """Adapter for A2A Protocol v1.0 compatibility."""

    # Method name mappings: v1.0 → internal (a2a library format)
    METHOD_MAP_V1_TO_INTERNAL = {
        "SendMessage": "message/send",
        "SendStreamingMessage": "message/stream",
        "GetTask": "tasks/get",
        "CancelTask": "tasks/cancel",
        "ListTasks": "tasks/list",
        "SubscribeToTask": "tasks/resubscribe",
        "GetExtendedAgentCard": "agent/getAuthenticatedExtendedCard",
        "SetTaskPushNotificationConfig": "tasks/pushNotificationConfig/set",
        "GetTaskPushNotificationConfig": "tasks/pushNotificationConfig/get",
        "ListTaskPushNotificationConfig": "tasks/pushNotificationConfig/list",
        "DeleteTaskPushNotificationConfig": "tasks/pushNotificationConfig/delete",
    }

    # Method name mappings: internal → v1.0 (for error messages, etc.)
    METHOD_MAP_INTERNAL_TO_V1 = {v: k for k, v in METHOD_MAP_V1_TO_INTERNAL.items()}

    @staticmethod
    def should_use_v1(request: Request) -> bool:
        """Check if v1.0 format should be used based on headers.

        Returns True (use v1.0) unless X-A2A-Stable: 1 header is present.
        """
        return request.headers.get("X-A2A-Stable") != "1"

    @staticmethod
    def transform_request_inbound(body: dict) -> dict:
        """Transform v1.0 request to internal library format.

        Main transformation: PascalCase method → slash-case method
        """
        method = body.get("method", "")

        # Transform method name from v1.0 to internal
        if method in A2AV1Adapter.METHOD_MAP_V1_TO_INTERNAL:
            body["method"] = A2AV1Adapter.METHOD_MAP_V1_TO_INTERNAL[method]

        return body

    @staticmethod
    def transform_agent_card_outbound(card: dict) -> dict:
        """Transform AgentCard from library format to v1.0 format.

        Key transformations:
        - protocolVersion (string) → protocolVersions (array)
        - url (string) → supportedInterfaces (array of {url, transport})
        - supportsAuthenticatedExtendedCard → capabilities.extendedAgentCard
        """
        v1_card: dict[str, Any] = {}

        # Required fields (pass through)
        v1_card["name"] = card.get("name", "")
        v1_card["description"] = card.get("description", "")
        v1_card["version"] = card.get("version", "1.0.0")
        v1_card["defaultInputModes"] = card.get("defaultInputModes", ["text/plain"])
        v1_card["defaultOutputModes"] = card.get("defaultOutputModes", ["text/plain"])
        v1_card["skills"] = card.get("skills", [])

        # protocolVersion → protocolVersions (array)
        protocol_version = card.get("protocolVersion", "1.0")
        v1_card["protocolVersions"] = [protocol_version]

        # url → supportedInterfaces (array)
        # v1.0 uses AgentInterface with {url, transport}
        url = card.get("url", "")
        preferred_transport = card.get("preferredTransport", "JSONRPC")
        v1_card["supportedInterfaces"] = [{
            "url": url,
            "transport": preferred_transport
        }]

        # Also add additionalInterfaces if present
        additional = card.get("additionalInterfaces", [])
        for iface in additional:
            v1_card["supportedInterfaces"].append({
                "url": iface.get("url", url),
                "transport": iface.get("transport", "JSONRPC")
            })

        # capabilities transformation
        capabilities = card.get("capabilities", {})
        v1_capabilities: dict[str, Any] = {
            "streaming": capabilities.get("streaming", False),
            "pushNotifications": capabilities.get("pushNotifications", False),
            "stateTransitionHistory": capabilities.get("stateTransitionHistory", False),
        }

        # supportsAuthenticatedExtendedCard → capabilities.extendedAgentCard
        if card.get("supportsAuthenticatedExtendedCard"):
            v1_capabilities["extendedAgentCard"] = True

        # extensions stay in capabilities
        if capabilities.get("extensions"):
            v1_capabilities["extensions"] = capabilities["extensions"]

        v1_card["capabilities"] = v1_capabilities

        # Optional fields (pass through if present)
        optional_fields = [
            "securitySchemes", "security", "provider",
            "documentationUrl", "iconUrl", "signatures"
        ]
        for field in optional_fields:
            if field in card:
                v1_card[field] = card[field]

        return v1_card

    @staticmethod
    def transform_task_outbound(task: dict) -> dict:
        """Transform Task to v1.0 format.

        The library already serializes to camelCase, so minimal changes needed.
        Task structure: id, contextId, status, artifacts, history, metadata
        """
        # Add 'kind': 'task' discriminator if not present
        if "kind" not in task:
            task["kind"] = "task"
        return task

    @staticmethod
    def transform_message_outbound(message: dict) -> dict:
        """Transform Message to v1.0 format.

        The library already serializes to camelCase.
        Message structure: messageId, role, parts, contextId, taskId, metadata
        """
        # Add 'kind': 'message' discriminator if not present
        if "kind" not in message:
            message["kind"] = "message"
        return message

    @staticmethod
    def transform_sse_event_outbound(event: dict) -> dict:
        """Transform SSE streaming event to v1.0 format.

        Events already have 'kind' field for discrimination.
        Ensure status-update and artifact-update events are properly formatted.
        """
        # Events should already be properly formatted by A2AEventMapper
        return event
```

### Phase 2: Route Integration

Modify `backend/app/api/routes/a2a.py`:

```python
from app.services.a2a_v1_adapter import A2AV1Adapter

@router.get("/{agent_id}/")
async def get_agent_card(
    agent_id: uuid.UUID,
    session: SessionDep,
    request: Request,
    auth: Optional[A2AAuthContext] = Depends(get_optional_a2a_auth_context),
) -> JSONResponse:
    # ... existing validation logic ...

    # Get card_dict as before
    if not auth or not auth.is_authenticated():
        card_dict = A2AService.get_public_agent_card_dict(agent, base_url)
    else:
        card_dict = A2AService.get_agent_card_dict(agent, environment, base_url)

    # Transform to v1.0 format unless stable header is present
    if A2AV1Adapter.should_use_v1(request):
        card_dict = A2AV1Adapter.transform_agent_card_outbound(card_dict)

    return JSONResponse(content=card_dict)


@router.post("/{agent_id}/")
async def handle_jsonrpc(
    agent_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    auth: A2AAuthContext = Depends(get_a2a_auth_context),
):
    # ... existing validation ...

    body = await request.json()
    use_v1 = A2AV1Adapter.should_use_v1(request)

    # Transform v1.0 method names to internal format
    if use_v1:
        body = A2AV1Adapter.transform_request_inbound(body)

    method = body.get("method")
    request_id = body.get("id")
    params = body.get("params", {})

    # ... existing method dispatch ...

    if method == "message/stream":
        # For streaming, transform is handled in the event mapper
        return StreamingResponse(
            handler.handle_message_stream(params, str(request_id)),
            media_type="text/event-stream",
            # ...
        )

    elif method == "message/send":
        task = await handler.handle_message_send(params)
        result = task.model_dump(by_alias=True, exclude_none=True)
        if use_v1:
            result = A2AV1Adapter.transform_task_outbound(result)
        return _jsonrpc_success(request_id, result)

    elif method == "tasks/get":
        task = await handler.handle_tasks_get(params)
        if task:
            result = task.model_dump(by_alias=True, exclude_none=True)
            if use_v1:
                result = A2AV1Adapter.transform_task_outbound(result)
            return _jsonrpc_success(request_id, result)
        return _jsonrpc_error(request_id, -32001, "Task not found")

    # ... other methods ...
```

### Phase 3: SSE Event Transformation (Optional)

The SSE events are already properly formatted by `A2AEventMapper`. The `kind` discriminator field (`status-update`, `artifact-update`) is already present. If additional v1.0 transformations are needed, they can be added to the event mapper or the streaming handler.

```python
# In A2AEventMapper._create_status_update() - already has 'kind' field
return {
    "kind": "status-update",
    **event.model_dump(by_alias=True, exclude_none=True),
}
```

### Implementation Notes

The adapter has been implemented in `backend/app/services/a2a_v1_adapter.py` and integrated into `backend/app/api/routes/a2a.py`. The actual implementation follows the patterns described above with these key integration points:

- **GET `/{agent_id}/`**: Calls `A2AV1Adapter.should_use_v1(request)` and `transform_agent_card_outbound()` before returning
- **POST `/{agent_id}/`**: Calls `transform_request_inbound()` for method name translation, then `transform_task_outbound()` for responses

## Key Transformations

### 1. Method Names (Inbound)

| v1.0 Method | Internal (a2a library) Method |
|-------------|-------------------------------|
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

### 2. AgentCard (Outbound)

```javascript
// Current library format (v0.3.0 draft)
{
  "name": "My Agent",
  "description": "An agent that helps with tasks",
  "url": "https://example.com/a2a/123/",
  "protocolVersion": "0.3.0",
  "preferredTransport": "JSONRPC",
  "capabilities": {
    "streaming": true,
    "pushNotifications": false
  },
  "supportsAuthenticatedExtendedCard": true,
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [...]
}

// v1.0 format (after transformation)
{
  "name": "My Agent",
  "description": "An agent that helps with tasks",
  "protocolVersions": ["0.3.0"],
  "supportedInterfaces": [
    { "url": "https://example.com/a2a/123/", "transport": "JSONRPC" }
  ],
  "capabilities": {
    "streaming": true,
    "pushNotifications": false,
    "extendedAgentCard": true
  },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [...]
}
```

### 3. Task/Message (Outbound)

The library already handles camelCase serialization via Pydantic's `serialize_by_alias=True`:
- `task_id` → `taskId` ✅
- `context_id` → `contextId` ✅
- `message_id` → `messageId` ✅
- `artifact_id` → `artifactId` ✅

Task states already use kebab-case (`input-required`, `auth-required`) ✅

The only addition needed is the `kind` discriminator field:
- `Task`: add `"kind": "task"`
- `Message`: add `"kind": "message"`

### 4. SSE Events (Outbound)

SSE events are already properly formatted by `A2AEventMapper`:
- `TaskStatusUpdateEvent`: `"kind": "status-update"` ✅
- `TaskArtifactUpdateEvent`: `"kind": "artifact-update"` ✅

No additional transformation needed for SSE events.

## File Changes Summary

| File | Changes |
|------|---------|
| `backend/app/services/a2a_v1_adapter.py` | **NEW** - Adapter class with transformation methods |
| `backend/app/api/routes/a2a.py` | Add v1 header check, inbound/outbound transformations |
| `backend/app/services/a2a_service.py` | No changes (adapter wraps output at route level) |
| `backend/app/services/a2a_request_handler.py` | No changes (returns a2a library types) |
| `backend/app/services/a2a_event_mapper.py` | No changes (already has `kind` field) |

## Testing Strategy

### 1. Default Mode (v1.0, no header)

**AgentCard (GET request)**:
```bash
curl https://example.com/a2a/{agent_id}/
# Should return:
# - "protocolVersions": ["0.3.0"]
# - "supportedInterfaces": [{"url": "...", "transport": "JSONRPC"}]
# - "capabilities": {"extendedAgentCard": true, ...}
```

**JSON-RPC Methods**:
```bash
curl -X POST https://example.com/a2a/{agent_id}/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {...}}'
# Method "SendMessage" is accepted and transformed to "message/send" internally
```

### 2. Stable Mode (with `X-A2A-Stable: 1` header)

**AgentCard (GET request)**:
```bash
curl https://example.com/a2a/{agent_id}/ -H "X-A2A-Stable: 1"
# Should return original library format:
# - "protocolVersion": "0.3.0"
# - "url": "..."
# - "supportsAuthenticatedExtendedCard": true
```

**JSON-RPC Methods**:
```bash
curl -X POST https://example.com/a2a/{agent_id}/ \
  -H "Content-Type: application/json" \
  -H "X-A2A-Stable: 1" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {...}}'
# Method "message/send" is used directly (no transformation)
```

## Migration Notes

### For Clients

- **Default behavior**: v1.0 compatible format
- **Legacy clients**: Add `X-A2A-Stable: 1` header to use library format
- **Recommended**: Migrate to v1.0 method names (`SendMessage`, `GetTask`, etc.)

### For Internal Components

- The a2a library internals remain unchanged
- Adapter layer handles all transformations at the API boundary (routes)
- When the a2a library is updated to v1.0, the adapter can be simplified or removed

### Breaking Changes (v1.0 Mode)

Clients that depend on the old format will see these changes:
1. AgentCard: `protocolVersion` → `protocolVersions` (string → array)
2. AgentCard: `url` removed, use `supportedInterfaces[0].url`
3. AgentCard: `supportsAuthenticatedExtendedCard` → `capabilities.extendedAgentCard`
4. JSON-RPC: Method names must be PascalCase (`SendMessage` not `message/send`)

## Implementation Checklist

- [x] Create `backend/app/services/a2a_v1_adapter.py`
- [x] Modify `GET /{agent_id}/` route to transform AgentCard
- [x] Modify `POST /{agent_id}/` route to transform method names (inbound)
- [x] Modify `POST /{agent_id}/` route to add `kind` to Task/Message responses
- [ ] Add unit tests for adapter transformations
- [ ] Add integration tests for both v1.0 and stable modes
- [x] Update API documentation

## Future Work

1. **Full v1.0 Validation**: Add Pydantic models for v1.0 request validation
2. **Version Negotiation**: Support Accept header or query param for version selection
3. **Library Update**: When a2a library supports v1.0, simplify adapter
4. **Extended AgentCard**: Full implementation of `GetExtendedAgentCard`
5. **Push Notification Config**: v1.0 push notification config methods

## References

- A2A v1.0 Spec: `Overview_-_A2A_Protocol.html`
- Migration Guide: `A2A_Migration.rtf`
- v1.0 Types Reference: `A2A_V1.rtf`
- Current Implementation: `backend/app/api/routes/a2a.py`
- Library Types: `backend/.venv/lib/python3.13/site-packages/a2a/types.py`

---

**Document Version:** 1.1
**Last Updated:** 2026-01-17
**Status:** Implemented (Core Features)
