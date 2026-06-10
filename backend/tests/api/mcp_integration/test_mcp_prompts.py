"""
MCP prompt integration tests.

Verifies that the MCP prompts layer correctly:
  - Parses prompt lines in "slug: prompt text" format
  - Handles edge cases (empty lines, no colon, whitespace)
  - Resolves agent example_prompts from connector context
  - Lists prompts dynamically from agent data
  - Gets individual prompts by name with message content
  - Returns error for unknown prompt names
  - Handles agents with no example_prompts

These tests call the prompt functions directly with the DB stubbed,
following the same pattern as test_mcp_resources.py.

Pure-logic unit tests for the line parser (_parse_prompt_line) live in
tests/unit/test_mcp_prompts.py. This file keeps the connector-context-driven
handler tests that need a real agent + connector.
"""
import asyncio
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP

from app.mcp.prompts import (
    _get_agent_example_prompts,
    register_mcp_prompts,
)
from app.mcp.server import mcp_connector_id_var
from tests.utils.agent import create_agent_via_api, get_agent, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import create_mcp_connector


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent_with_connector(
    client: TestClient,
    token_headers: dict[str, str],
    agent_name: str = "MCP Prompt Agent",
    connector_name: str = "Prompt Connector",
    example_prompts: list[str] | None = None,
) -> tuple[dict, dict]:
    """Create agent + connector. Optionally set example_prompts. Returns (agent, connector)."""
    agent = create_agent_via_api(client, token_headers, name=agent_name)
    drain_tasks()
    agent = get_agent(client, token_headers, agent["id"])

    if example_prompts is not None:
        agent = update_agent(
            client, token_headers, agent["id"],
            example_prompts=example_prompts,
        )

    connector = create_mcp_connector(
        client, token_headers, agent["id"],
        name=connector_name,
    )
    return agent, connector


def _run_async(coro):
    """Run an async function synchronously."""
    return asyncio.run(coro)


def _run_with_connector_context(connector_id: str, coro_fn):
    """Run an async function with the connector context var set."""
    async def _run():
        token = mcp_connector_id_var.set(connector_id)
        try:
            return await coro_fn()
        finally:
            mcp_connector_id_var.reset(token)
    return asyncio.run(_run())


# ── _get_agent_example_prompts Tests ─────────────────────────────────────────


def test_get_agent_example_prompts_with_prompts(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Agent with example_prompts returns them from connector context."""
    prompts = [
        "report_status: Send me status report",
        "check_email: Check my email for urgent items",
    ]
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Prompts Agent",
        example_prompts=prompts,
    )

    token = mcp_connector_id_var.set(connector["id"])
    try:
        result = _get_agent_example_prompts()
    finally:
        mcp_connector_id_var.reset(token)
    assert result == prompts


def test_get_agent_example_prompts_empty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """Agent without example_prompts returns empty list."""
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="No Prompts Agent",
    )

    token = mcp_connector_id_var.set(connector["id"])
    try:
        result = _get_agent_example_prompts()
    finally:
        mcp_connector_id_var.reset(token)
    assert result == []


def test_get_agent_example_prompts_no_context():
    """No connector context returns empty list."""
    result = _get_agent_example_prompts()
    assert result == []


# ── register_mcp_prompts Tests ───────────────────────────────────────────────


def test_register_mcp_prompts_installs_handlers():
    """register_mcp_prompts registers list_prompts and get_prompt handlers."""
    from mcp import types as mcp_types

    server = FastMCP(name="test-prompts-server")

    # Capture original handlers
    original_list = server._mcp_server.request_handlers.get(mcp_types.ListPromptsRequest)
    original_get = server._mcp_server.request_handlers.get(mcp_types.GetPromptRequest)

    register_mcp_prompts(server)

    new_list = server._mcp_server.request_handlers.get(mcp_types.ListPromptsRequest)
    new_get = server._mcp_server.request_handlers.get(mcp_types.GetPromptRequest)

    assert new_list is not None
    assert new_get is not None
    # Handlers should have been replaced
    assert new_list is not original_list
    assert new_get is not original_get


# ── Dynamic List/Get Prompt Tests ────────────────────────────────────────────


def test_dynamic_list_prompts(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Listing prompts returns parsed example_prompts from the agent:
      1. Create agent with example_prompts
      2. Register prompts on a FastMCP server
      3. Call list handler
      4. Verify prompts match agent data
    """
    prompts = [
        "report_status: Send me status report for the current month",
        "check_email: Check my email for urgent items",
    ]
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="List Prompts Agent",
        example_prompts=prompts,
    )

    server = FastMCP(name="test-list-prompts")
    register_mcp_prompts(server)

    from mcp import types as mcp_types
    handler = server._mcp_server.request_handlers[mcp_types.ListPromptsRequest]

    async def _list():
        token = mcp_connector_id_var.set(connector["id"])
        try:
            result = await handler(mcp_types.ListPromptsRequest(method="prompts/list"))
            return result
        finally:
            mcp_connector_id_var.reset(token)

    result = _run_async(_list())
    # Handler returns ServerResult wrapper; unwrap to get ListPromptsResult
    inner = result.root if hasattr(result, "root") else result
    assert len(inner.prompts) == 2
    assert inner.prompts[0].name == "report_status"
    assert inner.prompts[0].description == "Send me status report for the current month"
    assert inner.prompts[1].name == "check_email"
    assert inner.prompts[1].description == "Check my email for urgent items"


def test_dynamic_get_prompt(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Getting a prompt by name returns the correct message content:
      1. Create agent with example_prompts
      2. Register prompts on a FastMCP server
      3. Call get handler with a valid prompt name
      4. Verify response has correct description and message
    """
    prompts = [
        "report_status: Send me status report for the current month",
        "check_email: Check my email for urgent items",
    ]
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Get Prompt Agent",
        example_prompts=prompts,
    )

    server = FastMCP(name="test-get-prompt")
    register_mcp_prompts(server)

    from mcp import types as mcp_types
    handler = server._mcp_server.request_handlers[mcp_types.GetPromptRequest]

    async def _get():
        token = mcp_connector_id_var.set(connector["id"])
        try:
            result = await handler(mcp_types.GetPromptRequest(
                method="prompts/get",
                params=mcp_types.GetPromptRequestParams(name="check_email"),
            ))
            return result
        finally:
            mcp_connector_id_var.reset(token)

    result = _run_async(_get())
    # Handler returns ServerResult wrapper; unwrap to get GetPromptResult
    inner = result.root if hasattr(result, "root") else result
    assert inner.description == "Check my email for urgent items"
    assert len(inner.messages) == 1
    assert inner.messages[0].role == "user"
    assert inner.messages[0].content.text == "Check my email for urgent items"


def test_get_prompt_not_found(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Getting a non-existent prompt name raises ValueError:
      1. Create agent with example_prompts
      2. Register prompts on a FastMCP server
      3. Call get handler with an invalid prompt name
      4. Verify ValueError is raised
    """
    prompts = ["report_status: Send me status report"]
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Not Found Prompt Agent",
        example_prompts=prompts,
    )

    server = FastMCP(name="test-not-found-prompt")
    register_mcp_prompts(server)

    from mcp import types as mcp_types
    handler = server._mcp_server.request_handlers[mcp_types.GetPromptRequest]

    async def _get():
        token = mcp_connector_id_var.set(connector["id"])
        try:
            result = await handler(mcp_types.GetPromptRequest(
                method="prompts/get",
                params=mcp_types.GetPromptRequestParams(name="nonexistent"),
            ))
            return result
        finally:
            mcp_connector_id_var.reset(token)

    with pytest.raises(ValueError, match="Prompt not found"):
        _run_async(_get())


def test_empty_prompts_list(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Agent with no example_prompts returns empty prompt list:
      1. Create agent without example_prompts
      2. Register prompts on a FastMCP server
      3. Call list handler
      4. Verify empty list
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Empty Prompts Agent",
    )

    server = FastMCP(name="test-empty-prompts")
    register_mcp_prompts(server)

    from mcp import types as mcp_types
    handler = server._mcp_server.request_handlers[mcp_types.ListPromptsRequest]

    async def _list():
        token = mcp_connector_id_var.set(connector["id"])
        try:
            result = await handler(mcp_types.ListPromptsRequest(method="prompts/list"))
            return result
        finally:
            mcp_connector_id_var.reset(token)

    result = _run_async(_list())
    # Handler returns ServerResult wrapper; unwrap to get ListPromptsResult
    inner = result.root if hasattr(result, "root") else result
    assert len(inner.prompts) == 0
