"""Addons policy flags on ``/admin/server-config`` and their face in the directory."""
from fastapi.testclient import TestClient

from tests.utils.addons import DIRECTORY_URL, get_directory
from tests.utils.server_config import SERVER_CONFIG_URL, get_server_config

FLAGS = {
    "addons_external_directories": "external_directories",
    "addons_custom_mcp": "custom_mcp",
    "addons_plugin_hooks": "plugin_hooks",
    "addons_stdio_mcp": "stdio_mcp",
}


def test_addons_policy_flags_default_true_update_and_reach_directory(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    1. All four default to true, in the config and in the directory ``policy``.
    2. A superuser update persists, shows in the directory policy and changes the ETag.
    3. A partial update leaves the other flags alone.
    4. A normal user gets 403 on both verbs.
    """
    h, u = superuser_token_headers, normal_user_token_headers
    cfg = get_server_config(client, h)
    assert all(cfg[f] is True for f in FLAGS)
    before = get_directory(client, u)
    assert all(before["policy"][p] is True for p in FLAGS.values())

    r = client.put(
        SERVER_CONFIG_URL,
        headers=h,
        json={"addons_plugin_hooks": False, "addons_stdio_mcp": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["addons_plugin_hooks"] is False
    cfg = get_server_config(client, h)
    assert (cfg["addons_plugin_hooks"], cfg["addons_stdio_mcp"]) == (False, False)
    assert (cfg["addons_external_directories"], cfg["addons_custom_mcp"]) == (True, True)

    after = get_directory(client, u)
    assert after["policy"] == {
        "external_directories": True,
        "custom_mcp": True,
        "plugin_hooks": False,
        "stdio_mcp": False,
    }
    assert after["etag"] != before["etag"]
    conditional = client.get(DIRECTORY_URL, headers={**u, "If-None-Match": before["etag"]})
    assert conditional.status_code == 200, "the old ETag no longer matches"

    r = client.put(SERVER_CONFIG_URL, headers=h, json={"addons_custom_mcp": False})
    assert r.status_code == 200
    assert get_directory(client, u)["policy"]["custom_mcp"] is False
    assert get_server_config(client, h)["addons_plugin_hooks"] is False

    assert client.get(SERVER_CONFIG_URL, headers=u).status_code == 403
    assert client.put(SERVER_CONFIG_URL, headers=u, json={"addons_custom_mcp": True}).status_code == 403
