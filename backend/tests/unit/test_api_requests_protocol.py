"""Producer capability-request tools: schema, toolset selection, route mapping, install."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.environments import platform_protocol_files as ppf
from core.server.tools import cinna_api_requests_protocol as proto

_ADAPTERS = Path(proto.__file__).resolve().parents[1] / "adapters"


def test_schema_loads_with_the_three_tools():
    specs = proto.load_api_request_specs()
    assert set(specs) == {"api_requests_list", "api_requests_get", "api_requests_update_status"}
    for spec in specs.values():
        assert spec["description"] and spec["inputSchema"]


@pytest.mark.parametrize(
    "name,route",
    [
        ("api_requests_list", "list"),
        ("api_requests_get", "get"),
        ("api_requests_update_status", "update-status"),
    ],
)
def test_route_mapping(name, route):
    assert proto.route_for(name) == route


def test_serves_agent_api_follows_the_directory(tmp_path):
    assert proto.serves_agent_api(tmp_path / "agent_api") is False
    (tmp_path / "agent_api").mkdir()
    assert proto.serves_agent_api(tmp_path / "agent_api") is True


@pytest.fixture()
def toolsets_for():
    source = (_ADAPTERS / "claude_code_sdk_adapter.py").read_text(encoding="utf-8")
    namespace: dict = {}
    start = source.index("def cinna_toolsets_for_mode")
    end = source.index("\ndef ", start + 1)
    exec(source[start:end], namespace)  # noqa: S102 — pure function lifted from the adapter
    return namespace["cinna_toolsets_for_mode"]


def test_toolset_selection_by_mode_and_agent_api_dir(toolsets_for):
    assert toolsets_for("building", has_agent_api=True) == ("artifacts", "api_requests")
    assert toolsets_for("building", has_agent_api=False) == ("artifacts",)
    assert toolsets_for("building") == ("artifacts",)
    assert "api_requests" not in toolsets_for("conversation", None, has_agent_api=True)
    assert toolsets_for("other", None, has_agent_api=True) == ()


def test_client_posts_to_the_mapped_route_and_drops_none_args():
    client = proto.CinnaApiRequestsClient(
        backend_url="http://backend:8000", auth_token="tok", env_id="env-1"
    )
    response = MagicMock(status_code=200)
    response.json.return_value = {"requests": [], "count": 0}
    with patch("httpx.Client") as client_cls:
        post = client_cls.return_value.__enter__.return_value.post
        post.return_value = response
        text, is_error = client.call("api_requests_update_status", {"request_id": "r", "status": "completed", "resolution_note": None})
    assert is_error is False
    assert json.loads(text) == {"requests": [], "count": 0}
    url = post.call_args.args[0]
    assert url == "http://backend:8000/api/v1/agent/api-requests/update-status"
    assert post.call_args.kwargs["json"] == {"request_id": "r", "status": "completed"}
    assert post.call_args.kwargs["headers"]["X-Agent-Env-Id"] == "env-1"


def test_client_flags_tool_errors_and_unknown_tools():
    client = proto.CinnaApiRequestsClient(backend_url="http://b", auth_token="t", env_id="e")
    assert client.call("api_requests_nope", {}) == ("Error: unknown tool api_requests_nope", True)
    response = MagicMock(status_code=200)
    response.json.return_value = {"error": {"code": "not_found", "message": "x"}}
    with patch("httpx.Client") as client_cls:
        client_cls.return_value.__enter__.return_value.post.return_value = response
        text, is_error = client.call("api_requests_get", {"request_id": "r"})
    assert is_error is True and "not_found" in text


def test_schema_is_installed_with_the_platform_protocol_files(tmp_path):
    assert "protocols/api-requests-tools.schema.json" in ppf.PROTOCOL_FILES
    templates_dir = Path(proto.__file__).resolve().parents[4]
    core_dir = tmp_path / "core"
    installed = ppf.install_platform_protocol(core_dir, templates_dir)
    assert "protocols/api-requests-tools.schema.json" in installed
    schema = core_dir / "protocols" / "api-requests-tools.schema.json"
    assert schema.is_file()
    assert set(proto.load_api_request_specs(schema)) == set(proto.load_api_request_specs())
