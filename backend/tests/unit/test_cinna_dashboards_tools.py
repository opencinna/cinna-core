"""
Cinna dashboards tools (env-core, Dashboards C2 phase P5).

The agent environment's ``dashboards_tiles`` / ``dashboards_update_tile`` tools
equal ``dashboards-tools.schema.json`` (read independently from the kit
snapshot). Asserted here:

* the Claude Code binding registers exactly the schema tools, with the schema's
  description and input schema;
* the ``dashboards`` toolset is added only for a conversation whose
  HMAC-verified session context carries a ``dashboard_id``; a bad, missing or
  unkeyed signature gives no tools;
* the OpenCode bridge lists the tools in conversation mode and not in building;
* the client posts ``session_id`` to the route derived from the tool name and
  maps ``{"error": ...}`` to an error result, a success to a plain one;
* the prompt section appears only when the ``cinna-dashboards`` skill exists
  and uses the ``cinna://component/bundle:<id>/<name>`` reference format.
"""
import asyncio
import copy
import hashlib
import hmac
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.server.prompt_generator import PromptGenerator
from core.server.tools import cinna_dashboards_protocol as proto

_BACKEND = Path(__file__).parents[2]
_TEMPLATES = _BACKEND / "app" / "env-templates"
_KIT_SNAPSHOT = _TEMPLATES / "platform-knowledge-env" / "app" / "workspace" / "knowledge" / "local-kit"
_SCHEMA = _KIT_SNAPSHOT / "protocols" / "dashboards-tools.schema.json"
_TASKS_SCHEMA = _KIT_SNAPSHOT / "protocols" / "tasks.schema.json"
_ARTIFACTS_SCHEMA = _KIT_SNAPSHOT / "protocols" / "artifacts.schema.json"
_CORE_SERVER = _TEMPLATES / "app_core_base" / "core" / "server"
_BRIDGE = _CORE_SERVER / "tools" / "mcp_bridge" / "cinna_server.py"

KEY = "unit-test-agent-auth-token"
DASHBOARD_ID = "11111111-2222-3333-4444-555555555555"


def _tool_names(path: Path) -> list[str]:
    return list(json.loads(path.read_text(encoding="utf-8"))["tools"])


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(_SCHEMA.read_text(encoding="utf-8"))


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


def _expected_input_schema(schema: dict, name: str) -> dict:
    return _resolve(schema["tools"][name]["inputSchema"], schema.get("$defs") or {})


def _sign(context: dict, key: str = KEY) -> str:
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _state(context: dict, signature: str | None = "auto") -> dict:
    state: dict = {"session_context": context}
    if signature == "auto":
        state["session_context_signature"] = _sign(context)
    elif signature is not None:
        state["session_context_signature"] = signature
    return state


# ── Schema ───────────────────────────────────────────────────────────────


def test_schema_declares_the_cinna_server_and_both_tools(schema):
    assert schema["server"] == "cinna"
    assert list(schema["tools"]) == ["dashboards_tiles", "dashboards_update_tile"]
    assert all(n.startswith(proto.TOOL_PREFIX) for n in schema["tools"])


def test_loader_returns_the_schema_specs(schema):
    specs = proto.load_dashboard_specs(_SCHEMA)
    assert list(specs) == list(schema["tools"])
    for name, spec in specs.items():
        assert spec["description"] == schema["tools"][name]["description"]
        assert spec["inputSchema"] == _expected_input_schema(schema, name)


@pytest.mark.parametrize(
    "name, route",
    [("dashboards_tiles", "tiles"), ("dashboards_update_tile", "update-tile")],
)
def test_route_is_derived_from_the_tool_name(name, route):
    assert proto.route_for(name) == route


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

    _server, allowed = build_cinna_server(("dashboards",))

    assert [s["name"] for s in servers] == ["cinna"]
    assert [t["name"] for t in registered] == list(schema["tools"])
    for entry in registered:
        spec = schema["tools"][entry["name"]]
        assert entry["description"] == spec["description"]
        assert entry["inputSchema"] == _expected_input_schema(schema, entry["name"])
    assert allowed == [f"mcp__cinna__{name}" for name in schema["tools"]]


def test_claude_conversation_serves_tasks_artifacts_then_dashboards(fake_claude_sdk, schema):
    registered, _servers = fake_claude_sdk
    from core.server.tools.cinna_tasks_claude import build_cinna_server

    build_cinna_server(("tasks", "artifacts", "dashboards"))

    expected = _tool_names(_TASKS_SCHEMA) + _tool_names(_ARTIFACTS_SCHEMA) + list(schema["tools"])
    assert [t["name"] for t in registered] == expected


def test_claude_handler_returns_error_json_as_text(fake_claude_sdk, monkeypatch):
    registered, _servers = fake_claude_sdk
    monkeypatch.setenv("AGENT_AUTH_TOKEN", "token")
    monkeypatch.setenv("BACKEND_URL", "http://backend:8000")
    from core.server.tools import cinna_tasks_claude

    body = {"error": {"code": "tile_not_found", "message": "No such tile"}}
    with patch.object(cinna_tasks_claude, "get_backend_session_id", return_value="sess-1", create=True), \
            patch("httpx.Client") as client_cls:
        client = MagicMock()
        client.__enter__.return_value = client
        client.post.return_value = MagicMock(status_code=200, json=MagicMock(return_value=body))
        client_cls.return_value = client
        tools = {t["name"]: t for t in cinna_tasks_claude.build_cinna_tools(toolsets=("dashboards",))}
        result = asyncio.run(tools["dashboards_tiles"]["handler"]({}))

    assert result["is_error"] is True
    assert json.loads(result["content"][0]["text"]) == body
    assert "structuredContent" not in result  # the SDK result carries text only


# ── Toolset gating ───────────────────────────────────────────────────────


@pytest.fixture()
def toolsets_for():
    source = (_CORE_SERVER / "adapters" / "claude_code_sdk_adapter.py").read_text(encoding="utf-8")
    namespace: dict = {}
    start = source.index("def cinna_toolsets_for_mode")
    end = source.index("\ndef ", start + 1)
    exec(source[start:end], namespace)  # noqa: S102 — a pure function lifted from the adapter
    return namespace["cinna_toolsets_for_mode"]


def test_conversation_with_dashboard_id_adds_dashboards(toolsets_for):
    assert toolsets_for("conversation", {"dashboard_id": DASHBOARD_ID}) == ("tasks", "artifacts", "dashboards")


@pytest.mark.parametrize("context", [None, {}, {"dashboard_id": None}, {"dashboard_id": ""}, {"bundle_id": "b"}])
def test_conversation_without_dashboard_id_has_no_dashboards(toolsets_for, context):
    assert toolsets_for("conversation", context) == ("tasks", "artifacts")


def test_building_never_gets_dashboards(toolsets_for):
    assert toolsets_for("building", {"dashboard_id": DASHBOARD_ID}) == ("artifacts",)
    assert toolsets_for("building") == ("artifacts",)


def test_adapter_feeds_the_verified_context_only():
    source = (_CORE_SERVER / "adapters" / "claude_code_sdk_adapter.py").read_text(encoding="utf-8")
    assert "cinna_toolsets_for_mode(\n                    mode, verified_session_context(session_state)," in source


def test_verified_dashboard_id_with_a_good_signature():
    state = _state({"dashboard_id": DASHBOARD_ID, "backend_session_id": "s"})
    assert proto.verified_dashboard_id(state, KEY) == DASHBOARD_ID
    assert proto.verified_session_context(state, KEY) == state["session_context"]


def test_verified_dashboard_id_reads_the_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENT_AUTH_TOKEN", KEY)
    assert proto.verified_dashboard_id(_state({"dashboard_id": DASHBOARD_ID})) == DASHBOARD_ID


@pytest.mark.parametrize(
    "state, key",
    [
        (_state({"dashboard_id": DASHBOARD_ID}, signature="0" * 64), KEY),  # wrong signature
        (_state({"dashboard_id": DASHBOARD_ID}, signature=None), KEY),  # no signature
        (_state({"dashboard_id": DASHBOARD_ID}, signature=""), KEY),  # empty signature
        (_state({"dashboard_id": DASHBOARD_ID}), "another-key"),  # signed with a different key
        (_state({"dashboard_id": DASHBOARD_ID}), ""),  # no key in the environment
        ({"session_context_signature": "x"}, KEY),  # no context
        (None, KEY),
        ({}, KEY),
    ],
)
def test_bad_or_missing_signature_gives_no_dashboard(state, key, monkeypatch):
    monkeypatch.delenv("AGENT_AUTH_TOKEN", raising=False)
    assert proto.verified_session_context(state, key) is None
    assert proto.verified_dashboard_id(state, key) is None


def test_tampered_context_gives_no_tools(toolsets_for):
    state = _state({"dashboard_id": DASHBOARD_ID})
    state["session_context"] = {"dashboard_id": "99999999-0000-0000-0000-000000000000"}
    verified = proto.verified_session_context(state, KEY)
    assert toolsets_for("conversation", verified) == ("tasks", "artifacts")


def test_context_without_dashboard_id_verifies_but_has_no_dashboard():
    state = _state({"backend_session_id": "s"})
    assert proto.verified_session_context(state, KEY) == {"backend_session_id": "s"}
    assert proto.verified_dashboard_id(state, KEY) is None


# ── OpenCode bridge ──────────────────────────────────────────────────────


def _load_bridge():
    spec = importlib.util.spec_from_file_location("cinna_server_dashboards_under_test", _BRIDGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _list_tool_names(bridge) -> list[str]:
    import mcp.types as mcp_types

    server = bridge.build_server()
    handler = server.request_handlers[mcp_types.ListToolsRequest]
    result = asyncio.run(handler(mcp_types.ListToolsRequest(method="tools/list")))
    return [t.name for t in result.root.tools]


def test_bridge_lists_dashboard_tools_in_conversation_mode(monkeypatch, schema):
    monkeypatch.setenv("CINNA_OPENCODE_MODE", "conversation")
    names = _list_tool_names(_load_bridge())
    assert names == _tool_names(_TASKS_SCHEMA) + _tool_names(_ARTIFACTS_SCHEMA) + list(schema["tools"])


def test_bridge_hides_dashboard_tools_in_building_mode(monkeypatch, schema):
    monkeypatch.setenv("CINNA_OPENCODE_MODE", "building")
    names = _list_tool_names(_load_bridge())
    assert names == _tool_names(_TASKS_SCHEMA) + _tool_names(_ARTIFACTS_SCHEMA)
    assert not set(schema["tools"]) & set(names)


def test_bridge_without_mode_falls_back_to_the_context_dashboard_id(monkeypatch, tmp_path, schema):
    monkeypatch.delenv("CINNA_OPENCODE_MODE", raising=False)
    bridge = _load_bridge()
    context_path = tmp_path / "session_context.json"
    monkeypatch.setattr(bridge, "SESSION_CONTEXT_PATH", context_path)

    assert not set(schema["tools"]) & set(_list_tool_names(bridge))  # no file
    context_path.write_text(json.dumps({"backend_session_id": "s"}))
    assert not set(schema["tools"]) & set(_list_tool_names(bridge))
    context_path.write_text(json.dumps({"backend_session_id": "s", "dashboard_id": DASHBOARD_ID}))
    assert set(schema["tools"]) <= set(_list_tool_names(bridge))


def test_bridge_calls_a_dashboard_tool_with_structured_content(monkeypatch, tmp_path):
    import mcp.types as mcp_types

    monkeypatch.setenv("CINNA_OPENCODE_MODE", "conversation")
    context_path = tmp_path / "session_context.json"
    context_path.write_text(json.dumps({"backend_session_id": "sess-9"}))
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "SESSION_CONTEXT_PATH", context_path)

    seen: list[tuple] = []

    class _Dash:
        def call_result(self, name, args, session_id):
            seen.append((name, args, session_id))
            body = {"error": {"code": "no_dashboard", "message": "x"}}
            return proto.DashboardToolResult(json.dumps(body), True, body)

    server = bridge.build_server(dashboards_client=_Dash())
    handler = server.request_handlers[mcp_types.CallToolRequest]
    result = asyncio.run(handler(mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name="dashboards_tiles", arguments={}),
    ))).root

    assert seen == [("dashboards_tiles", {}, "sess-9")]
    assert result.isError is True
    assert result.structuredContent == {"error": {"code": "no_dashboard", "message": "x"}}
    assert json.loads(result.content[0].text) == result.structuredContent


def test_bridge_refuses_a_dashboard_tool_in_building_mode(monkeypatch):
    import mcp.types as mcp_types

    monkeypatch.setenv("CINNA_OPENCODE_MODE", "building")
    server = _load_bridge().build_server()
    handler = server.request_handlers[mcp_types.CallToolRequest]
    result = asyncio.run(handler(mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name="dashboards_tiles", arguments={}),
    ))).root
    assert result.isError is True
    assert "Unknown tool" in result.content[0].text


def _call_dashboard_tool(monkeypatch, tmp_path, arguments, name="dashboards_tiles"):
    """Call through the bridge with a shared context for sess-shared and per-session ones."""
    import mcp.types as mcp_types

    monkeypatch.setenv("CINNA_OPENCODE_MODE", "conversation")
    context_path = tmp_path / "session_context.json"
    context_path.write_text(json.dumps({"backend_session_id": "sess-shared"}))
    per_session = tmp_path / "session_context"
    per_session.mkdir()
    (per_session / "ses_A.json").write_text(json.dumps({"backend_session_id": "sess-A"}))
    bridge = _load_bridge()
    monkeypatch.setattr(bridge, "SESSION_CONTEXT_PATH", context_path)

    seen: list[tuple] = []

    class _Dash:
        def call_result(self, tool, args, session_id):
            seen.append((tool, args, session_id))
            return proto.DashboardToolResult("{}", False, {})

    server = bridge.build_server(dashboards_client=_Dash())
    handler = server.request_handlers[mcp_types.CallToolRequest]
    result = asyncio.run(handler(mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    ))).root
    return bridge, seen, result


def test_bridge_routes_a_stamped_call_to_its_own_session(monkeypatch, tmp_path):
    """The plugin's session id wins over the shared file another chat overwrote."""
    bridge, seen, result = _call_dashboard_tool(
        monkeypatch, tmp_path, {"_cinna_opencode_session_id": "ses_A"},
    )
    assert result.isError is False
    # The stamp is stripped before the schema check and the backend call.
    assert seen == [("dashboards_tiles", {}, "sess-A")]
    assert bridge.SESSION_ARG == "_cinna_opencode_session_id"


@pytest.mark.parametrize("stamp", ["ses_missing", "../ses_A", ""])
def test_bridge_never_falls_back_to_the_shared_file_for_a_stamped_call(monkeypatch, tmp_path, stamp):
    _bridge, seen, _result = _call_dashboard_tool(
        monkeypatch, tmp_path, {"_cinna_opencode_session_id": stamp},
    )
    assert seen == [("dashboards_tiles", {}, None)]


def test_bridge_without_a_stamp_uses_the_shared_file(monkeypatch, tmp_path):
    _bridge, seen, _result = _call_dashboard_tool(monkeypatch, tmp_path, {})
    assert seen == [("dashboards_tiles", {}, "sess-shared")]


def test_bridge_still_validates_arguments_against_the_schema(monkeypatch, tmp_path):
    _bridge, seen, result = _call_dashboard_tool(
        monkeypatch, tmp_path, {"_cinna_opencode_session_id": "ses_A", "bogus": 1},
    )
    assert seen == []
    assert result.isError is True
    assert "Input validation error" in result.content[0].text


def test_session_plugin_stamps_dashboard_tools_only():
    source = (_BRIDGE.parent / "cinna_session_plugin.js").read_text(encoding="utf-8")
    assert '"tool.execute.before"' in source
    assert 'DASHBOARD_TOOL_PREFIX = "cinna_dashboards_"' in source
    assert 'SESSION_ARG = "_cinna_opencode_session_id"' in source


# ── Client ───────────────────────────────────────────────────────────────


def _client(**kwargs) -> proto.CinnaDashboardsClient:
    return proto.CinnaDashboardsClient(
        backend_url="http://backend:8000", auth_token="tok", env_id="env-1",
        tool_names=frozenset({"dashboards_tiles", "dashboards_update_tile"}), **kwargs,
    )


def _post(client, body=None, status=200, args=None, name="dashboards_update_tile", session_id="sess-1"):
    with patch("httpx.Client") as client_cls:
        http = MagicMock()
        http.__enter__.return_value = http
        http.post.return_value = MagicMock(
            status_code=status, json=MagicMock(return_value=body if body is not None else {}), text="boom",
        )
        client_cls.return_value = http
        result = client.call_result(name, args or {}, session_id)
    return result, http.post


def test_client_posts_session_id_to_the_derived_route():
    result, post = _post(_client(), body={"ok": True}, args={"tile_id": "t1", "params": {"a": None}, "x": None})

    assert post.call_args.args[0] == "http://backend:8000/api/v1/agent/dashboards/update-tile"
    # A top-level None is omitted; a None inside params is kept (reset to default).
    assert post.call_args.kwargs["json"] == {"tile_id": "t1", "params": {"a": None}, "session_id": "sess-1"}
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"
    assert post.call_args.kwargs["headers"]["X-Agent-Env-Id"] == "env-1"
    assert result.is_error is False


def test_client_maps_a_200_error_body_to_an_error_result_with_structured_content():
    body = {"error": {"code": "tile_not_found", "message": "No such tile"}}
    result, _post_mock = _post(_client(), body=body)

    assert result.is_error is True
    assert result.structured == body
    assert json.loads(result.text) == body
    assert _client().call("dashboards_tiles", {}, None)[1] is True


def test_client_returns_a_success_result_with_structured_content():
    body = {"tiles": [{"tile_id": "t1", "title": "Revenue"}]}
    result, _post_mock = _post(_client(), body=body, name="dashboards_tiles")

    assert result.is_error is False
    assert result.structured == body
    assert json.loads(result.text) == body


def test_client_without_a_session_answers_no_dashboard_without_a_request():
    with patch("httpx.Client") as client_cls:
        result = _client().call_result("dashboards_tiles", {}, None)
    client_cls.assert_not_called()
    assert result.is_error is True
    assert result.structured["error"]["code"] == proto.NO_DASHBOARD


def test_client_rejects_an_unknown_tool_and_an_unconfigured_environment():
    assert _client().call_result("dashboards_nope", {}, "s").is_error is True
    unconfigured = proto.CinnaDashboardsClient(backend_url="http://b", auth_token="", env_id="e", tool_names=frozenset({"dashboards_tiles"}))
    result = unconfigured.call_result("dashboards_tiles", {}, "s")
    assert result.is_error is True and "not configured" in result.text


@pytest.mark.parametrize("status", [401, 500])
def test_client_maps_non_200_to_an_error_result(status):
    result, _post_mock = _post(_client(), body={"detail": "nope"}, status=status)
    assert result.is_error is True
    assert result.text.startswith("Error:")
    assert result.structured is None


def test_client_logs_status_and_code_but_never_the_body():
    # The module logger is patched rather than read through caplog: the session's
    # Alembic run (fileConfig, disable_existing_loggers=True) disables loggers that
    # already exist, which makes caplog empty in a combined run (tests/README.md).
    body = {"error": {"code": "invalid_input", "message": "m"}, "input": {"params": {"secret": "v"}}}
    with patch.object(proto, "logger") as logger_mock:
        _post(_client(), body=body, status=422)
    logged = " ".join(call.args[0] % call.args[1:] for call in logger_mock.warning.call_args_list)
    assert "422" in logged and "invalid_input" in logged
    assert "secret" not in logged and "boom" not in logged


# ── Names everywhere else ────────────────────────────────────────────────


def test_pre_approval_registry_names_the_schema_tools(schema):
    from core.server.adapters.tool_name_registry import CINNA_DASHBOARD_TOOLS, PRE_APPROVED_TOOLS

    assert list(CINNA_DASHBOARD_TOOLS) == list(schema["tools"])
    for name in schema["tools"]:
        assert f"mcp__cinna__{name}" in PRE_APPROVED_TOOLS
        assert f"cinna_{name}" in PRE_APPROVED_TOOLS


# ── Prompt section ───────────────────────────────────────────────────────


def _generator(tmp_path: Path, with_skill: bool) -> PromptGenerator:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    platform = tmp_path / "platform_skills"
    platform.mkdir()
    if with_skill:
        (platform / "cinna-dashboards").mkdir()
        (platform / "cinna-dashboards" / "SKILL.md").write_text("---\nname: cinna-dashboards\n---\n")
    generator = PromptGenerator(str(workspace))
    generator.platform_skills_dir = platform
    return generator


def test_dashboards_section_absent_without_the_skill(tmp_path):
    generator = _generator(tmp_path, with_skill=False)
    assert generator._get_dashboards_section({"bundle_id": "b1"}) is None


def test_dashboards_section_uses_the_bundle_reference_format(tmp_path):
    generator = _generator(tmp_path, with_skill=True)
    section = generator._get_dashboards_section({"bundle_id": "bundle-123"})

    assert section is not None
    assert "## Dashboards" in section
    assert "cinna://component/bundle:bundle-123/name" in section
    assert "Never put it in backticks" in section
    assert "refreshes itself" in section
    assert "only in a chat opened from a dashboard" in section


def test_dashboards_section_without_a_bundle_id_prints_no_reference(tmp_path):
    generator = _generator(tmp_path, with_skill=True)
    for context in (None, {}):
        section = generator._get_dashboards_section(context)
        assert "cinna://" not in section
        assert "<bundle_id>" not in section
        assert "refreshes itself" in section


# ── OpenCode adapter: per-session context files ──────────────────────────


def test_server_start_prunes_per_session_context_files(tmp_path):
    from core.server.adapters import opencode_sdk_adapter as adapter

    per_session = tmp_path / adapter.SESSION_CONTEXT_DIRNAME
    per_session.mkdir()
    for name in ("ses_A.json", "ses_B.json"):
        (per_session / name).write_text("{}")
    (tmp_path / "session_context.json").write_text("{}")

    assert adapter._prune_session_contexts(tmp_path) == 2
    assert list(per_session.iterdir()) == []
    assert (tmp_path / "session_context.json").exists()  # the shared file stays
    assert adapter._prune_session_contexts(tmp_path / "absent") == 0
    source = Path(adapter.__file__).read_text(encoding="utf-8")
    assert "_prune_session_contexts(self._runtime_dir)" in source
