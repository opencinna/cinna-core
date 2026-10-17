"""
Cinna artifacts parity ratchet — the agent environment's ``artifacts_publish``
tool equals ``artifacts.schema.json``.

Same arrangement as ``test_cinna_tasks_parity.py``: the schema and the
``cinna-artifacts`` skill are authored in ``docs/local_agent_kit/`` and synced
into the platform-knowledge-env kit snapshot, which is what environments
receive and what these tests read.

Asserted here, against the schema read independently of the code under test:

* the Claude Code binding registers the tool with the schema's description and
  input schema; building mode registers artifacts only, conversation mode tasks
  and artifacts (D4);
* the OpenCode bridge serves the task tools followed by the artifacts tool;
* the pre-approval registry, the OpenCode tool list and the backend's
  ``PRE_ALLOWED_TOOLS`` name the tool in both engine forms;
* the protocol files an environment receives are byte-identical to the kit, and
  ``cinna-artifacts`` is a reserved platform skill name;
* the request model the backend route accepts matches the schema's input fields
  and forbids extras (A8), and its response carries the schema's result fields.
"""
import asyncio
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
_SCHEMA = _KIT_SNAPSHOT / "protocols" / "artifacts.schema.json"
_TASKS_SCHEMA = _KIT_SNAPSHOT / "protocols" / "tasks.schema.json"
_CORE_SERVER = _TEMPLATES / "app_core_base" / "core" / "server"
_BRIDGE = _CORE_SERVER / "tools" / "mcp_bridge" / "cinna_server.py"

EXPECTED_TOOLS = ["artifacts_publish"]


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def task_tools() -> list[str]:
    return list(json.loads(_TASKS_SCHEMA.read_text(encoding="utf-8"))["tools"])


def test_schema_declares_the_cinna_server_and_one_tool(schema):
    assert schema["server"] == "cinna"
    assert schema["protocol_version"] == 1
    assert list(schema["tools"]) == EXPECTED_TOOLS
    spec = schema["tools"]["artifacts_publish"]
    assert spec["inputSchema"]["additionalProperties"] is False
    assert spec["inputSchema"]["required"] == ["path", "title"]
    assert set(spec["result"]["required"]) == {
        "artifact_id", "revision", "url", "kind", "file_count", "size_bytes", "excluded",
    }


def test_kit_snapshot_matches_the_authored_kit():
    if not _KIT_SOURCE.is_dir():
        pytest.skip("docs/local_agent_kit is not available (backend container)")
    relatives = ["protocols/artifacts.schema.json", "kit.json"] + [
        p.relative_to(_KIT_SOURCE).as_posix()
        for root in ("skills/cinna-artifacts", "templates/artifact-sqlite-dashboard")
        for p in (_KIT_SOURCE / root).rglob("*")
        if p.is_file()
    ]
    for relative in relatives:
        assert (_KIT_SOURCE / relative).read_bytes() == (_KIT_SNAPSHOT / relative).read_bytes(), (
            f"{relative} differs from docs/local_agent_kit — run `make sync-platform-knowledge`"
        )


def test_kit_json_names_the_protocol_and_the_skill():
    kit = json.loads((_KIT_SNAPSHOT / "kit.json").read_text(encoding="utf-8"))
    assert kit["protocols"]["artifacts"] == "protocols/artifacts.schema.json"
    assert kit["platform_skills"]["cinna-artifacts"] == "skills/cinna-artifacts"
    assert (_KIT_SNAPSHOT / "skills" / "cinna-artifacts" / "SKILL.md").is_file()


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


def test_claude_binding_registers_exactly_the_schema(fake_claude_sdk, schema):
    registered, servers = fake_claude_sdk
    from core.server.tools.cinna_tasks_claude import build_cinna_server

    _server, allowed = build_cinna_server(("artifacts",))

    assert [s["name"] for s in servers] == ["cinna"]
    assert [t["name"] for t in registered] == EXPECTED_TOOLS
    spec = schema["tools"]["artifacts_publish"]
    assert registered[0]["description"] == spec["description"]
    assert registered[0]["inputSchema"] == spec["inputSchema"]
    assert allowed == ["mcp__cinna__artifacts_publish"]


def test_claude_conversation_serves_tasks_then_artifacts(fake_claude_sdk, task_tools):
    registered, _servers = fake_claude_sdk
    from core.server.tools.cinna_tasks_claude import build_cinna_server

    _server, allowed = build_cinna_server(("tasks", "artifacts"))

    assert [t["name"] for t in registered] == task_tools + EXPECTED_TOOLS
    assert allowed == [f"mcp__cinna__{name}" for name in task_tools + EXPECTED_TOOLS]


def test_mode_availability_building_and_conversation():
    """D4: building gets artifacts only; conversation gets tasks and artifacts."""
    source = (_CORE_SERVER / "adapters" / "claude_code_sdk_adapter.py").read_text(encoding="utf-8")
    namespace: dict = {}
    start = source.index("def cinna_toolsets_for_mode")
    end = source.index("\ndef ", start + 1)
    exec(source[start:end], namespace)  # noqa: S102 — a pure function lifted from the adapter
    toolsets_for = namespace["cinna_toolsets_for_mode"]
    assert toolsets_for("building") == ("artifacts",)
    assert toolsets_for("conversation") == ("tasks", "artifacts")
    assert "build_cinna_server(cinna_toolsets)" in source


def test_claude_handler_reports_a_missing_path(fake_claude_sdk, monkeypatch):
    registered, _servers = fake_claude_sdk
    monkeypatch.setenv("AGENT_AUTH_TOKEN", "token")
    from core.server.tools.cinna_tasks_claude import build_cinna_tools

    tools = {t["name"]: t for t in build_cinna_tools(toolsets=("artifacts",))}
    missing = "no-such-folder-3f9a2b1c"
    result = asyncio.run(tools["artifacts_publish"]["handler"]({"path": missing, "title": "T"}))
    assert result["is_error"] is True
    assert "not found in the workspace" in result["content"][0]["text"]


# ── OpenCode binding ─────────────────────────────────────────────────────


def _load_bridge():
    spec = importlib.util.spec_from_file_location("cinna_server_artifacts_under_test", _BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_opencode_bridge_serves_tasks_then_artifacts(schema, task_tools):
    import mcp.types as mcp_types

    bridge = _load_bridge()
    server = bridge.build_server()
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
    tools = result.root.tools

    assert [t.name for t in tools] == task_tools + EXPECTED_TOOLS
    listed = tools[-1]
    spec = schema["tools"]["artifacts_publish"]
    assert listed.description == spec["description"]
    assert listed.inputSchema == spec["inputSchema"]


def test_opencode_bridge_routes_artifacts_to_the_artifacts_client():
    import mcp.types as mcp_types

    calls: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, label: str) -> None:
            self.label = label

        def call(self, name, args, session_id):
            calls.append((self.label, name))
            return "{}", False

    bridge = _load_bridge()
    server = bridge.build_server(client=_Recorder("tasks"), artifacts_client=_Recorder("artifacts"))
    handler = server.request_handlers[mcp_types.CallToolRequest]
    for name, arguments in (("artifacts_publish", {"path": "x", "title": "T"}), ("tasks_list", {})):
        asyncio.run(handler(mcp_types.CallToolRequest(
            method="tools/call", params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
        )))
    assert calls == [("artifacts", "artifacts_publish"), ("tasks", "tasks_list")]


# ── Names everywhere else ────────────────────────────────────────────────


def test_pre_approval_registry_names_the_schema_tool(schema):
    from core.server.adapters.tool_name_registry import (
        CINNA_ARTIFACT_TOOLS,
        PRE_APPROVED_TOOLS,
    )

    assert list(CINNA_ARTIFACT_TOOLS) == list(schema["tools"])
    for name in schema["tools"]:
        assert f"mcp__cinna__{name}" in PRE_APPROVED_TOOLS
        assert f"cinna_{name}" in PRE_APPROVED_TOOLS


def test_opencode_tool_list_advertises_the_tool():
    source = (_CORE_SERVER / "adapters" / "opencode_sdk_adapter.py").read_text(encoding="utf-8")
    assert "*CINNA_ARTIFACT_TOOL_NAMES_CLAUDE," in source


def test_backend_pre_allowed_tools_name_the_schema_tool(schema):
    from app.services.sessions.message_service import PRE_ALLOWED_TOOLS

    for name in schema["tools"]:
        assert f"mcp__cinna__{name}" in PRE_ALLOWED_TOOLS
        assert f"cinna_{name}" in PRE_ALLOWED_TOOLS


def test_cinna_artifacts_is_a_reserved_platform_skill():
    from app.services.agents.skill_manifest import PLATFORM_SKILL_NAMES

    assert "cinna-artifacts" in PLATFORM_SKILL_NAMES


# ── Files an environment receives ────────────────────────────────────────


def test_environment_receives_byte_identical_artifact_files(tmp_path):
    from app.services.environments.platform_protocol_files import install_platform_protocol

    core = tmp_path / "app" / "core"
    installed = install_platform_protocol(core, _TEMPLATES)

    assert "protocols/artifacts.schema.json" in installed
    assert "skills/cinna-artifacts" in installed
    kit_files = sorted(
        p.relative_to(_KIT_SNAPSHOT)
        for p in (_KIT_SNAPSHOT / "skills" / "cinna-artifacts").rglob("*")
        if p.is_file()
    ) + [Path("protocols/artifacts.schema.json")]
    for relative in kit_files:
        assert (core / relative).read_bytes() == (_KIT_SNAPSHOT / relative).read_bytes(), relative


def test_env_tool_finds_the_installed_schema_path():
    from app.services.environments.platform_protocol_files import PROTOCOL_FILES
    from core.server.tools.cinna_artifacts_protocol import SCHEMA_RELATIVE_PATH, SKILL_RELATIVE_PATH

    assert SCHEMA_RELATIVE_PATH.as_posix() in PROTOCOL_FILES
    assert SKILL_RELATIVE_PATH.as_posix() in PROTOCOL_FILES


# ── Backend wire models ──────────────────────────────────────────────────


def test_backend_request_model_matches_the_schema_and_forbids_extras(schema):
    from pydantic import ValidationError

    from app.models.artifacts.artifact_source import (
        AgentArtifactPublished,
        AgentArtifactPublishRequest,
    )

    spec = schema["tools"]["artifacts_publish"]
    tool_fields = set(spec["inputSchema"]["properties"])
    # The env client adds the calling conversation; nothing else.
    assert set(AgentArtifactPublishRequest.model_fields) == tool_fields | {"source_session_id"}
    with pytest.raises(ValidationError):
        AgentArtifactPublishRequest(path="x", title="T", visibility="instance")
    assert set(AgentArtifactPublished.model_fields) == set(spec["result"]["properties"])
