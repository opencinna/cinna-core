"""Deleting a person removes the data folders the database cascade orphans.

Bug 6 (user half) of the catalog-unification follow-up: both user-delete
routes are a database cascade, so the user's agents, environments, app-data
volumes and uploads lost their rows but kept their folders
(``data/agents/app-data/<user_id>/`` among them). After the delete commits the
routes now remove, for the deleted user only:

- every environment instance folder of every agent they owned
  (``<ENV_INSTANCES_DIR>/<env_id>/``);
- ``<APP_DATA_STORAGE_DIR>/<user_id>/``;
- ``<UPLOAD_BASE_PATH>/<user_id>/``.

The cleanup is a FastAPI background task, which ``TestClient`` runs before
the call returns. Storage roots are redirected to a tmp tree (module fixture
below); the environment instances dir is the lifecycle manager's tmp dir
(``patch_environment_adapter`` in this domain's conftest).

Scenarios:
  1. ``DELETE /users/me``: the user's env folder, app-data folder and upload
     folder are gone; a bystander's folders are untouched.
  2. ``DELETE /users/{id}`` by a superuser: the same, for the target user.
  3. A refused delete (a superuser deleting themself, 403) removes nothing.
  4. Deleting a superuser who created a group and is its member (self-added)
     succeeds; the group survives with created_by=None.

Notes:
  The app-data folder is seeded by hand: it is ``APP_DATA_STORAGE_DIR/<user_id>/
  <bundle_id>/...`` in the field and only its top segment matters here. Uploads
  go through ``POST /files/upload``. The name guard is unit-tested in
  ``tests/unit/test_data_dirs_remove_subdir.py``.
"""
import io
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import list_environments
from tests.utils.fixtures import patched_storage_dirs
from tests.utils.marketplace_skills import (
    create_second_superuser,
    new_skills_marketplace,
    skills_repo,
    snapshots_root,
)
from tests.utils.user import create_random_user_with_headers, promote_to_developer
from tests.utils.user_group import add_members, get_group, list_members

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def storage_roots(tmp_path_factory, tmp_path):
    """App-data and bundle roots → tmp (shared helper); uploads → tmp too."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    with (
        patched_storage_dirs(tmp_path_factory),
        patch.object(settings, "UPLOAD_BASE_PATH", str(uploads)),
    ):
        yield


@dataclass
class _Owned:
    user_id: str
    headers: dict[str, str]
    env_dir: Path
    app_data_dir: Path
    upload_dir: Path

    def all_present(self) -> bool:
        return (
            self.env_dir.is_dir()
            and self.app_data_dir.is_dir()
            and self.upload_dir.is_dir()
        )

    def none_present(self) -> bool:
        return not (
            self.env_dir.exists()
            or self.app_data_dir.exists()
            or self.upload_dir.exists()
        )


def _person_with_data(
    client: TestClient, superuser_headers: dict[str, str], lm
) -> _Owned:
    """A developer with one built agent, one app-data folder and one upload."""
    user, headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_headers, user["id"])
    create_random_ai_credential(client, headers, set_default=True)

    agent = create_agent_via_api(client, headers)
    drain_tasks()
    env_id = list_environments(client, headers, agent["id"])["data"][0]["id"]

    app_data = Path(settings.APP_DATA_STORAGE_DIR) / user["id"] / "some-bundle" / "storage"
    app_data.mkdir(parents=True)
    (app_data / "state.json").write_text("{}")

    r = client.post(
        f"{API}/files/upload",
        headers=headers,
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert r.status_code == 200, r.text

    owned = _Owned(
        user_id=user["id"],
        headers=headers,
        env_dir=lm.instances_dir / env_id,
        app_data_dir=Path(settings.APP_DATA_STORAGE_DIR) / user["id"],
        upload_dir=Path(settings.UPLOAD_BASE_PATH) / user["id"],
    )
    assert owned.all_present(), owned
    return owned


def test_delete_me_removes_the_users_env_app_data_and_upload_folders(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. Two developers each own a built agent, an app-data folder and an upload.
    2. One deletes their own account (DELETE /users/me → 200).
    3. Their three folders are gone; the bystander's are untouched.
    """
    lm = patch_environment_adapter
    leaving = _person_with_data(client, superuser_token_headers, lm)
    bystander = _person_with_data(client, superuser_token_headers, lm)

    r = client.delete(f"{API}/users/me", headers=leaving.headers)
    assert r.status_code == 200, r.text

    assert leaving.none_present(), (
        f"left behind: env={leaving.env_dir.exists()} "
        f"app_data={leaving.app_data_dir.exists()} uploads={leaving.upload_dir.exists()}"
    )
    assert bystander.all_present()
    assert client.get(
        f"{API}/users/{leaving.user_id}", headers=superuser_token_headers
    ).status_code == 404


def test_superuser_delete_removes_the_target_users_folders(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. Two developers with data; a superuser deletes one
       (DELETE /users/{id} → 200).
    2. The target's folders are gone; the bystander's are untouched.
    """
    lm = patch_environment_adapter
    target = _person_with_data(client, superuser_token_headers, lm)
    bystander = _person_with_data(client, superuser_token_headers, lm)

    r = client.delete(f"{API}/users/{target.user_id}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text

    assert target.none_present(), (
        f"left behind: env={target.env_dir.exists()} "
        f"app_data={target.app_data_dir.exists()} uploads={target.upload_dir.exists()}"
    )
    assert bystander.all_present()


def test_refused_self_delete_by_a_superuser_removes_nothing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A superuser may not delete themself (403); their folders stay."""
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    app_data = Path(settings.APP_DATA_STORAGE_DIR) / me["id"] / "b" / "storage"
    app_data.mkdir(parents=True)
    uploads = Path(settings.UPLOAD_BASE_PATH) / me["id"] / "2026"
    uploads.mkdir(parents=True)

    r = client.delete(f"{API}/users/me", headers=superuser_token_headers)
    assert r.status_code == 403, r.text

    assert app_data.is_dir()
    assert uploads.is_dir()


def test_superuser_delete_removes_the_targets_marketplace_skill_snapshots(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. A second superuser owns a synced skills marketplace; the admin owns another.
    2. Deleting the owner removes `<mid>/` of their marketplace only.
    """
    owner, owner_headers = create_second_superuser(client, superuser_token_headers)
    mine = new_skills_marketplace(client, owner_headers, skills_repo())
    bystander = new_skills_marketplace(client, superuser_token_headers, skills_repo())
    assert (snapshots_root() / mine["id"]).is_dir()

    r = client.delete(f"{API}/users/{owner['id']}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text

    assert not (snapshots_root() / mine["id"]).exists()
    assert (snapshots_root() / bystander["id"]).is_dir()


def test_deleting_a_group_creator_who_is_also_a_member_keeps_the_group(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. A second superuser creates a group and adds themself plus a bystander.
    2. Deleting that superuser succeeds (SET NULL + CASCADE reach the group in
       one statement; the FK is DEFERRABLE).
    3. The group survives with created_by=None, minus the deleted member.
    """
    owner, owner_headers = create_second_superuser(client, superuser_token_headers)
    bystander, _ = create_random_user_with_headers(client)
    r = client.post(f"{API}/admin/user-groups/", headers=owner_headers, json={"name": f"g-{owner['id'][:8]}"})
    assert r.status_code == 200, r.text
    gid = r.json()["id"]
    assert r.json()["created_by"] is not None
    add_members(client, owner_headers, gid, [owner["id"], bystander["id"]])

    r = client.delete(f"{API}/users/{owner['id']}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text

    group = get_group(client, superuser_token_headers, gid)
    assert group["created_by"] is None
    assert group["member_count"] == 1
    members = list_members(client, superuser_token_headers, gid)
    assert [m["user_id"] for m in members["data"]] == [bystander["id"]]
