"""Helper functions for managing agent webhooks via API in tests."""
from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR


# ── URL builders ─────────────────────────────────────────────────────────────


def _webhooks_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks"


def _webhook_url(agent_id: str, webhook_pk: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks/{webhook_pk}"


def _logs_url(agent_id: str, webhook_pk: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks/{webhook_pk}/logs"


def _regen_url(agent_id: str, webhook_pk: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks/{webhook_pk}/regenerate-token"


def _public_url(webhook_id: str) -> str:
    """Public execution endpoint (no /api/v1 prefix)."""
    return f"/agent-hooks/{webhook_id}"


# ── Create helpers ────────────────────────────────────────────────────────────


def create_session_webhook(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    name: str = "Test Session Webhook",
    prompt: str | None = None,
    session_mode: str = "conversation",
    payload_template: str | None = None,
) -> dict:
    """Create a session-type webhook. Returns AgentWebhookPublicWithToken.

    Asserts 200. Use inline client.post() to test non-200 responses.
    """
    body: dict = {"name": name, "type": "session", "session_mode": session_mode}
    if prompt is not None:
        body["prompt"] = prompt
    if payload_template is not None:
        body["payload_template"] = payload_template

    r = client.post(
        f"{_webhooks_url(agent_id)}/session",
        headers=headers,
        json=body,
    )
    assert r.status_code == 200, f"Create session webhook failed: {r.text}"
    return r.json()


def create_script_webhook(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    name: str = "Test Script Webhook",
    command: str = "echo hello",
    command_timeout_seconds: int = 120,
    payload_template: str | None = None,
) -> dict:
    """Create a script-type webhook. Returns AgentWebhookPublicWithToken.

    Asserts 200. Use inline client.post() to test non-200 responses.
    """
    body: dict = {
        "name": name,
        "type": "script",
        "command": command,
        "command_timeout_seconds": command_timeout_seconds,
    }
    if payload_template is not None:
        body["payload_template"] = payload_template

    r = client.post(
        f"{_webhooks_url(agent_id)}/script",
        headers=headers,
        json=body,
    )
    assert r.status_code == 200, f"Create script webhook failed: {r.text}"
    return r.json()


# ── Read helpers ──────────────────────────────────────────────────────────────


def list_webhooks(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> list[dict]:
    """List all webhooks for an agent. Returns the ``data`` array.

    Asserts 200 and that count == len(data).
    """
    r = client.get(_webhooks_url(agent_id), headers=headers)
    assert r.status_code == 200, f"List webhooks failed: {r.text}"
    body = r.json()
    assert "data" in body
    assert "count" in body
    assert body["count"] == len(body["data"])
    return body["data"]


def get_webhook(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    webhook_pk: str,
) -> dict:
    """Fetch a single webhook by its row UUID. Asserts 200."""
    r = client.get(_webhook_url(agent_id, webhook_pk), headers=headers)
    assert r.status_code == 200, f"Get webhook failed: {r.text}"
    return r.json()


def list_webhook_logs(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    webhook_pk: str,
    limit: int | None = None,
) -> list[dict]:
    """List logs for a webhook. Returns the ``data`` array.

    Asserts 200 and that count == len(data).
    """
    url = _logs_url(agent_id, webhook_pk)
    params = {}
    if limit is not None:
        params["limit"] = limit
    r = client.get(url, headers=headers, params=params)
    assert r.status_code == 200, f"List webhook logs failed: {r.text}"
    body = r.json()
    assert "data" in body
    assert "count" in body
    assert body["count"] == len(body["data"])
    return body["data"]


# ── Mutate helpers ────────────────────────────────────────────────────────────


def update_webhook(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    webhook_pk: str,
    **fields,
) -> dict:
    """Partially update a webhook (PATCH). Asserts 200."""
    r = client.patch(
        _webhook_url(agent_id, webhook_pk),
        headers=headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update webhook failed: {r.text}"
    return r.json()


def delete_webhook(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    webhook_pk: str,
) -> dict:
    """Delete a webhook. Asserts 200."""
    r = client.delete(_webhook_url(agent_id, webhook_pk), headers=headers)
    assert r.status_code == 200, f"Delete webhook failed: {r.text}"
    return r.json()


def regenerate_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    webhook_pk: str,
) -> dict:
    """Regenerate token. Returns AgentWebhookPublicWithToken. Asserts 200."""
    r = client.post(_regen_url(agent_id, webhook_pk), headers=headers)
    assert r.status_code == 200, f"Regenerate token failed: {r.text}"
    return r.json()


# ── Public execution helper ───────────────────────────────────────────────────


def fire_webhook(
    client: TestClient,
    webhook_id: str,
    token: str,
    payload: str | None = None,
    content_type: str = "application/json",
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """POST to the public /agent-hooks/{webhook_id} endpoint via Bearer header.

    Asserts 200. Use inline client.post() to test 4xx responses.
    """
    headers: dict[str, str] = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)

    kwargs: dict = {"headers": headers}
    if payload is not None:
        kwargs["content"] = payload.encode("utf-8") if isinstance(payload, str) else payload

    r = client.post(_public_url(webhook_id), **kwargs)
    assert r.status_code == 200, f"Fire webhook failed ({r.status_code}): {r.text}"
    return r.json()
