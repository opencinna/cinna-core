"""AI credentials shared with a user group (User Groups plan P3b, D9/D10).

AI credentials have no person-to-person share surface; v1 adds a group-only one
plus ``GET /ai-credentials/shared-with-me``. Losing the last path (member
removal, group-share revoke) NULLs the pinned env columns of the lost user's
environments and restarts the running ones so ``.env`` drops the key. The
restart is backgrounded; it is observed at the ``_restart_environment_background``
seam, which is replaced by a recording mock.
"""
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.ai_credential import create_random_ai_credential, list_ai_credentials
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import (
    get_environment,
    link_ai_credential_to_environment,
    set_environment_status,
)
from tests.utils.skill_catalog import make_developer
from tests.utils.user_group import add_members, create_group, remove_member

API = settings.API_V1_STR
RESTART_SEAM = (
    "app.services.credentials.ai_credentials_service._restart_environment_background"
)
_ADMIN_LLM = f"{API}/admin/llm-providers"


def _share(client, headers, cred_id, group_id, expect=200):
    r = client.post(
        f"{API}/ai-credentials/{cred_id}/group-shares",
        headers=headers, json={"group_id": group_id},
    )
    assert r.status_code == expect, r.text
    return r.json()


def _shared_with_me(client, headers) -> dict[str, dict]:
    r = client.get(f"{API}/ai-credentials/shared-with-me", headers=headers)
    assert r.status_code == 200, r.text
    return {c["id"]: c for c in r.json()["data"]}


def _env_of(client, headers, name) -> str:
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None
    return agent["active_environment_id"]


def _pins(client, headers, env_id) -> tuple[str | None, str | None]:
    env = get_environment(client, headers, env_id)
    return env["conversation_ai_credential_id"], env["building_ai_credential_id"]


def test_share_list_and_revoke_group_share(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    owner, oh = make_developer(client, su)
    member, mh = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"]])
    cred = create_random_ai_credential(client, oh, name="shared-ai-key")

    # Owner only
    assert client.post(
        f"{API}/ai-credentials/{cred['id']}/group-shares",
        headers=mh, json={"group_id": group["id"]},
    ).status_code in (403, 404)
    _share(client, oh, cred["id"], "00000000-0000-0000-0000-000000000000", 404)

    row = _share(client, oh, cred["id"], group["id"])
    assert row["group_id"] == group["id"]
    assert row["group_name"] == group["name"]
    assert row["member_count"] == 1
    assert row["shared_by_user_id"] == owner["id"]
    _share(client, oh, cred["id"], group["id"], 409)

    r = client.get(f"{API}/ai-credentials/{cred['id']}/group-shares", headers=oh)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1
    assert r.json()["data"][0]["environments_at_risk"] == {"total": 0, "running": 0}

    # Owner rows carry the count
    rows = {c["id"]: c for c in list_ai_credentials(client, oh)["data"]}
    assert rows[cred["id"]]["group_share_count"] == 1

    # Member listing: via_groups, no secret
    shared = _shared_with_me(client, mh)[cred["id"]]
    assert shared["name"] == "shared-ai-key"
    assert shared["owner_id"] == owner["id"]
    assert [g["id"] for g in shared["via_groups"]] == [group["id"]]
    assert "api_key" not in shared and "encrypted_data" not in shared

    with patch(RESTART_SEAM, new=AsyncMock()):
        r = client.delete(
            f"{API}/ai-credentials/{cred['id']}/group-shares/{group['id']}", headers=oh
        )
    assert r.status_code == 200, r.text
    assert cred["id"] not in _shared_with_me(client, mh)
    assert client.get(
        f"{API}/ai-credentials/{cred['id']}/group-shares", headers=oh
    ).json()["count"] == 0
    rows = {c["id"]: c for c in list_ai_credentials(client, oh)["data"]}
    assert rows[cred["id"]]["group_share_count"] == 0


def test_non_member_does_not_see_group_shared_key_and_late_member_does(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    late, lh = make_developer(client, su)
    group = create_group(client, su)
    cred = create_random_ai_credential(client, oh)
    _share(client, oh, cred["id"], group["id"])
    assert cred["id"] not in _shared_with_me(client, lh)
    add_members(client, su, group["id"], [late["id"]])
    assert cred["id"] in _shared_with_me(client, lh)


def test_admin_managed_ai_credential_cannot_be_group_shared(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    target, th = make_developer(client, su)
    group = create_group(client, su)
    r = client.post(
        f"{_ADMIN_LLM}/", headers=su,
        json={
            "name": "Managed", "type": "anthropic", "api_key": "sk-ant-managed-grp",
            "target_user_ids": [target["id"]],
        },
    )
    assert r.status_code == 200, r.text
    child_id = r.json()["added"][0]["child_credential_id"]
    _share(client, th, child_id, group["id"], 400)


def test_member_removal_nulls_pins_and_restarts_running_envs_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    member, mh = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"]])
    cred = create_random_ai_credential(client, oh)
    own = create_random_ai_credential(client, mh)
    _share(client, oh, cred["id"], group["id"])

    running_env = _env_of(client, mh, "AIGS-Running")
    stopped_env = _env_of(client, mh, "AIGS-Stopped")
    own_env = _env_of(client, mh, "AIGS-Own")
    link_ai_credential_to_environment(db, running_env, cred["id"], conversation=True, building=True)
    link_ai_credential_to_environment(db, stopped_env, cred["id"], building=True)
    link_ai_credential_to_environment(db, own_env, own["id"], conversation=True)
    set_environment_status(db, running_env, "running")
    set_environment_status(db, stopped_env, "stopped")
    set_environment_status(db, own_env, "running")
    assert _pins(client, mh, running_env) == (cred["id"], cred["id"])

    restart = AsyncMock()
    with patch(RESTART_SEAM, new=restart):
        remove_member(client, su, group["id"], member["id"])
        drain_tasks()

    assert _pins(client, mh, running_env) == (None, None)
    assert _pins(client, mh, stopped_env) == (None, None)
    assert _pins(client, mh, own_env) == (own["id"], None), "own key is untouched"
    assert [c.args[0] for c in restart.call_args_list] == [
        __import__("uuid").UUID(running_env)
    ]
    assert cred["id"] not in _shared_with_me(client, mh)


def test_revoking_group_share_releases_members_but_not_a_member_of_another_sharing_group(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    only, only_h = make_developer(client, su)
    both, both_h = make_developer(client, su)
    g1 = create_group(client, su, member_ids=[only["id"], both["id"]])
    g2 = create_group(client, su, member_ids=[both["id"]])
    cred = create_random_ai_credential(client, oh)
    _share(client, oh, cred["id"], g1["id"])
    _share(client, oh, cred["id"], g2["id"])

    only_env = _env_of(client, only_h, "AIGS-Only")
    both_env = _env_of(client, both_h, "AIGS-Both")
    link_ai_credential_to_environment(db, only_env, cred["id"], conversation=True)
    link_ai_credential_to_environment(db, both_env, cred["id"], conversation=True)
    set_environment_status(db, only_env, "running")
    set_environment_status(db, both_env, "running")

    r = client.get(f"{API}/ai-credentials/{cred['id']}/group-shares", headers=oh)
    by_group = {s["group_id"]: s for s in r.json()["data"]}
    assert by_group[g1["id"]]["environments_at_risk"] == {"total": 1, "running": 1}

    restart = AsyncMock()
    with patch(RESTART_SEAM, new=restart):
        r = client.delete(
            f"{API}/ai-credentials/{cred['id']}/group-shares/{g1['id']}", headers=oh
        )
        assert r.status_code == 200, r.text
        drain_tasks()

    assert _pins(client, only_h, only_env) == (None, None)
    assert _pins(client, both_h, both_env) == (cred["id"], None)
    assert restart.call_count == 1
    assert cred["id"] in _shared_with_me(client, both_h)
    assert cred["id"] not in _shared_with_me(client, only_h)


def test_group_share_and_removal_never_touch_the_owners_own_environments(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    member, _ = make_developer(client, su)
    group = create_group(client, su, member_ids=[member["id"]])
    cred = create_random_ai_credential(client, oh)
    _share(client, oh, cred["id"], group["id"])
    owner_env = _env_of(client, oh, "AIGS-OwnerEnv")
    link_ai_credential_to_environment(db, owner_env, cred["id"], conversation=True)
    set_environment_status(db, owner_env, "running")

    restart = AsyncMock()
    with patch(RESTART_SEAM, new=restart):
        remove_member(client, su, group["id"], member["id"])
        drain_tasks()

    assert _pins(client, oh, owner_env) == (cred["id"], None)
    restart.assert_not_called()
