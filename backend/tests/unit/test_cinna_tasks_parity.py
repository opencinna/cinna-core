"""
Cinna Task Protocol parity ratchet — the agent environment's `cinna` tools
equal ``tasks.schema.json``.

The schema is authored in ``docs/local_agent_kit/protocols/`` and synced into
the platform-knowledge-env kit snapshot (``docs/`` is not in the backend image,
so the snapshot is what the backend and these tests read; in a repo checkout
the two must also be byte-identical). Environments receive the schema and the
``cinna-tasks`` skill from that snapshot via ``install_platform_protocol`` —
there is no hand-maintained copy under ``app_core_base``.

Asserted here, against the schema read independently of the code under test:

* both bindings (Claude Code in-process server, OpenCode stdio bridge) register
  exactly the schema's tools, with its descriptions and its input schemas
  (``$ref`` inlined, ``additionalProperties: false`` kept);
* the pre-approval registry names exactly those tools in both engine forms;
* the protocol files an environment receives are byte-identical to the kit.
"""
import asyncio
import copy
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parents[2]
_TEMPLATES = _BACKEND / "app" / "env-templates"
_KIT_SNAPSHOT = _TEMPLATES / "platform-knowledge-env" / "app" / "workspace" / "knowledge" / "local-kit"
_KIT_SOURCE = _BACKEND.parent / "docs" / "local_agent_kit"  # absent inside the backend container
_SCHEMA = _KIT_SNAPSHOT / "protocols" / "tasks.schema.json"
_BRIDGE = _TEMPLATES / "app_core_base" / "core" / "server" / "tools" / "mcp_bridge" / "cinna_server.py"

EXPECTED_TOOLS = [
    "tasks_targets", "tasks_list", "tasks_get", "tasks_create",
    "tasks_update", "tasks_comment", "tasks_report", "tasks_reply",
]


def _resolve(node, defs):
    """Independent $ref resolver: the expected schema, not the code's."""
    if isinstance(node, list):
        return [_resolve(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = copy.deepcopy(defs[node["$ref"].removeprefix("#/$defs/")])
        for key, value in node.items():
            if key != "$ref":
                target[key] = value
        return _resolve(target, defs)
    return {key: _resolve(value, defs) for key, value in node.items()}


def _contains_ref(node) -> bool:
    if isinstance(node, dict):
        return "$ref" in node or any(_contains_ref(v) for v in node.values())
    if isinstance(node, list):
        return any(_contains_ref(v) for v in node)
    return False


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def expected(schema) -> dict:
    defs = schema["$defs"]
    return {
        name: {"description": spec["description"], "inputSchema": _resolve(spec["inputSchema"], defs)}
        for name, spec in schema["tools"].items()
    }


def test_schema_declares_the_cinna_server_and_eight_tools(schema):
    assert schema["server"] == "cinna"
    assert schema["protocol_version"] == 1
    assert list(schema["tools"]) == EXPECTED_TOOLS
    for name, spec in schema["tools"].items():
        assert spec["inputSchema"]["additionalProperties"] is False, name


def test_kit_snapshot_matches_the_authored_kit():
    if not _KIT_SOURCE.is_dir():
        pytest.skip("docs/local_agent_kit is not available (backend container)")
    for relative in ("protocols/tasks.schema.json", "skills/cinna-tasks/SKILL.md",
                     "skills/cinna-tasks/references/file-protocol.md"):
        assert (_KIT_SOURCE / relative).read_bytes() == (_KIT_SNAPSHOT / relative).read_bytes(), (
            f"{relative} differs from docs/local_agent_kit — run `make sync-platform-knowledge`"
        )


# ── Claude Code binding ──────────────────────────────────────────────────


@pytest.fixture()
def fake_claude_sdk(monkeypatch):
    registered: list[dict] = []
    servers: list[dict] = []

    def tool(name, description, input_schema, *args, **kwargs):
        def wrap(handler):
            entry = {"name": name, "description": description, "inputSchema": input_schema, "handler": handler}
            registered.append(entry)
            return entry
        return wrap

    def create_sdk_mcp_server(**kwargs):
        servers.append(kwargs)
        return kwargs

    module = types.ModuleType("claude_agent_sdk")
    module.tool = tool
    module.create_sdk_mcp_server = create_sdk_mcp_server
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    monkeypatch.delitem(sys.modules, "core.server.tools.cinna_tasks_claude", raising=False)
    return registered, servers


def test_claude_binding_registers_exactly_the_schema(fake_claude_sdk, expected):
    registered, servers = fake_claude_sdk
    from core.server.tools.cinna_tasks_claude import build_cinna_server

    _server, allowed = build_cinna_server()

    assert [s["name"] for s in servers] == ["cinna"]
    assert [t["name"] for t in registered] == EXPECTED_TOOLS
    for entry in registered:
        assert entry["description"] == expected[entry["name"]]["description"]
        assert entry["inputSchema"] == expected[entry["name"]]["inputSchema"], entry["name"]
        assert not _contains_ref(entry["inputSchema"]), entry["name"]
    assert allowed == [f"mcp__cinna__{name}" for name in EXPECTED_TOOLS]


def test_claude_handler_reports_errors_without_a_session(fake_claude_sdk, monkeypatch):
    registered, _servers = fake_claude_sdk
    monkeypatch.setenv("AGENT_AUTH_TOKEN", "token")
    from core.server.tools.cinna_tasks_claude import build_cinna_tools

    tools = {t["name"]: t for t in build_cinna_tools()}
    result = asyncio.run(tools["tasks_list"]["handler"]({}))
    assert result["is_error"] is True
    assert "conversation" in result["content"][0]["text"]


# ── OpenCode binding ─────────────────────────────────────────────────────


def _load_bridge():
    spec = importlib.util.spec_from_file_location("cinna_server_under_test", _BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opencode_bridge_lists_exactly_the_schema(expected):
    import mcp.types as mcp_types

    bridge = _load_bridge()
    server = bridge.build_server()
    assert server.name == "cinna"

    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
    tools = result.root.tools

    # The bridge also serves artifacts_publish after the task tools
    # (test_cinna_artifacts_parity.py); the task tools come first, exactly.
    tools = [t for t in tools if t.name.startswith("tasks_")]
    assert [t.name for t in tools] == EXPECTED_TOOLS
    for listed in tools:
        assert listed.description == expected[listed.name]["description"]
        assert listed.inputSchema == expected[listed.name]["inputSchema"], listed.name


# ── Names everywhere else ────────────────────────────────────────────────


def test_pre_approval_registry_names_the_schema_tools(schema):
    from core.server.adapters.tool_name_registry import (
        CINNA_TASK_TOOLS,
        PRE_APPROVED_TOOLS,
    )

    assert list(CINNA_TASK_TOOLS) == list(schema["tools"])
    for name in schema["tools"]:
        assert f"mcp__cinna__{name}" in PRE_APPROVED_TOOLS
        assert f"cinna_{name}" in PRE_APPROVED_TOOLS


def test_backend_pre_allowed_tools_name_the_schema_tools(schema):
    from app.services.sessions.message_service import PRE_ALLOWED_TOOLS

    for name in schema["tools"]:
        assert f"mcp__cinna__{name}" in PRE_ALLOWED_TOOLS
        assert f"cinna_{name}" in PRE_ALLOWED_TOOLS


def test_old_names_survive_only_as_pre_approval_aliases(schema):
    from app.services.sessions.message_service import PRE_ALLOWED_TOOLS
    from core.server.adapters.tool_name_registry import PRE_APPROVED_TOOLS

    for old in schema["aliases"]["cinna_core"]:
        assert f"mcp__agent_task__{old}" in PRE_APPROVED_TOOLS
        assert f"mcp__agent_task__{old}" in PRE_ALLOWED_TOOLS
    tools_dir = _TEMPLATES / "app_core_base" / "core" / "server" / "tools"
    assert not list(tools_dir.glob("agent_task_*.py")), "old agent_task tool modules are still present"
    assert not (tools_dir / "mcp_bridge" / "task_server.py").exists()


# ── Files an environment receives ────────────────────────────────────────


def test_environment_receives_byte_identical_protocol_files(tmp_path):
    from app.services.environments.platform_protocol_files import install_platform_protocol

    core = tmp_path / "app" / "core"
    stale = core / "skills" / "cinna-tasks" / "stale.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("left over from an older kit")

    installed = install_platform_protocol(core, _TEMPLATES)

    assert installed[:2] == ["protocols/tasks.schema.json", "skills/cinna-tasks"]
    assert not stale.exists(), "a rebuild must replace the skill wholesale"
    kit_files = sorted(
        p.relative_to(_KIT_SNAPSHOT) for p in (_KIT_SNAPSHOT / "skills" / "cinna-tasks").rglob("*") if p.is_file()
    ) + [Path("protocols/tasks.schema.json")]
    for relative in kit_files:
        assert (core / relative).read_bytes() == (_KIT_SNAPSHOT / relative).read_bytes(), relative


def test_env_tools_find_the_installed_schema_path():
    """The env-side loader and the installer agree on where the schema lives."""
    from app.services.environments.platform_protocol_files import PROTOCOL_FILES
    from core.server.tools.cinna_tasks_protocol import SCHEMA_RELATIVE_PATH, SKILL_RELATIVE_PATH

    assert SCHEMA_RELATIVE_PATH.as_posix() in PROTOCOL_FILES
    assert SKILL_RELATIVE_PATH.as_posix() in PROTOCOL_FILES
