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
        - url (string) → supportedInterfaces (array of {url, protocolBinding, protocolVersion})
          with distinct versioned URLs per protocol (v1.0 and v0.3)
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
        # Advertise both supported versions; each has its own dedicated URL
        v1_card["protocolVersions"] = ["1.0", "0.3.0"]

        # url → supportedInterfaces (array)
        # v1.0 spec (4.4.6): AgentInterface has url, protocolBinding, protocolVersion (all required)
        # Derive versioned URLs from the base URL by inserting the version prefix after /a2a/
        # Base URL: http://host/api/v1/a2a/{agent_id}/
        # v1.0 URL: http://host/api/v1/a2a/v1.0/{agent_id}/
        # v0.3 URL: http://host/api/v1/a2a/v0.3/{agent_id}/
        base_url = card.get("url", "")
        preferred_transport = card.get("preferredTransport", "JSONRPC")

        v1_url = base_url.replace("/api/v1/a2a/", "/api/v1/a2a/v1.0/")
        v03_url = base_url.replace("/api/v1/a2a/", "/api/v1/a2a/v0.3/")

        v1_card["supportedInterfaces"] = [
            {
                "url": v1_url,
                "protocolBinding": preferred_transport,
                "protocolVersion": "1.0",
            },
            {
                "url": v03_url,
                "protocolBinding": preferred_transport,
                "protocolVersion": "0.3.0",
            },
        ]

        # Also add additionalInterfaces if present
        additional = card.get("additionalInterfaces", [])
        for iface in additional:
            v1_card["supportedInterfaces"].append({
                "url": iface.get("url", base_url),
                "protocolBinding": iface.get("transport", "JSONRPC"),
                "protocolVersion": "1.0",
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
