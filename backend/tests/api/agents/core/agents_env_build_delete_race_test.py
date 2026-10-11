"""An environment deleted while it builds leaves no instance folder behind.

Bug 3 of the catalog-unification follow-up: uninstalling a cloud install a few
seconds after installing it, while the build still copied the template, left
``agent-environments/<env_id>/`` on disk with no DB row and no container. The
delete removed the folder it found, then the copy finished and wrote it again.

The build now ends with a tail that re-reads the environment row and, when it
is gone, removes the container and the instance folder after every build step
has returned.

How the race is reproduced: agent creation / install only *collects* the build
coroutine (``background_tasks`` autouse fixture); it runs in ``drain_tasks()``.
``EnvironmentLifecycleManager._copy_template`` is wrapped so that the delete
lands through the API at the moment the build reaches the copy, and the copy
then writes the folder as the in-flight copy did in the field. Without the
tail, the folder outlives the row.

Scenarios:
  1. Delete the agent while the template copy runs: no instance folder, no
     agent, no environment afterwards.
  2. Uninstall a cloud install while its build copies the template (the field
     report): no instance folder, install gone.
  3. Delete the agent before its build ever starts: draining the build is
     harmless and nothing is written.
  4. Control: an undisturbed build keeps its instance folder (the tail never
     discards a live environment).

Notes:
  ``DockerEnvironmentAdapter.delete`` tolerating a missing ``docker-compose.yml``
  (the other half of the fix) is unit-tested in
  ``tests/unit/test_docker_adapter_delete_without_compose.py``.
"""
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.environment import list_environments

API = settings.API_V1_STR


def _only_env_id(client: TestClient, headers: dict[str, str], agent_id: str) -> str:
    envs = list_environments(client, headers, agent_id)
    assert envs["count"] == 1, envs
    return envs["data"][0]["id"]


def _delete_lands_during_copy(lm, on_copy):
    """Patch ``lm._copy_template``: run ``on_copy()`` (the delete), then copy.

    The delete runs first, so it removes whatever folder exists at that moment;
    the copy then writes the instance folder again, exactly what the in-flight
    copy did after the field delete. Returns the patcher and a call log.
    """
    original = lm._copy_template
    calls: list[Path] = []

    async def _copy(template_dir: Path, instance_dir: Path):
        calls.append(instance_dir)
        on_copy()
        instance_dir.mkdir(parents=True, exist_ok=True)
        await original(template_dir, instance_dir)
        # The copy really laid the template down after the delete.
        assert (instance_dir / "app" / "workspace").is_dir()

    return patch.object(lm, "_copy_template", _copy), calls


# ── Scenario 1: delete the agent mid-copy ─────────────────────────────────


def test_deleting_agent_while_template_copy_runs_leaves_no_instance_folder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. Create an agent; its build is collected, not run.
    2. Drain the build; when it reaches the template copy, DELETE the agent
       through the API, then let the copy write the folder.
    3. The instance folder is gone, the agent is 404.
    """
    lm = patch_environment_adapter
    headers = superuser_token_headers

    # ── Phase 1: agent created, build pending ─────────────────────────────
    agent = create_agent_via_api(client, headers, name="Delete Mid Copy")
    env_id = _only_env_id(client, headers, agent["id"])
    instance_dir = lm.instances_dir / env_id
    assert not instance_dir.exists()

    # ── Phase 2: the delete lands during the copy ─────────────────────────
    deletes: list[int] = []

    def _delete_agent() -> None:
        r = client.delete(f"{API}/agents/{agent['id']}", headers=headers)
        deletes.append(r.status_code)

    patcher, copies = _delete_lands_during_copy(lm, _delete_agent)
    with patcher:
        drain_tasks()

    assert copies == [instance_dir], "the build must have reached the template copy"
    assert deletes == [200]

    # ── Phase 3: nothing left behind ──────────────────────────────────────
    assert not instance_dir.exists(), (
        f"instance folder of the deleted environment survived: {list(instance_dir.rglob('*'))[:5]}"
    )
    assert client.get(f"{API}/agents/{agent['id']}", headers=headers).status_code == 404
    names = [
        a["name"]
        for a in client.get(f"{API}/agents/", headers=headers).json()["data"]
    ]
    assert "Delete Mid Copy" not in names


# ── Scenario 2: uninstall a cloud install mid-build ───────────────────────


def test_uninstalling_cloud_install_during_its_build_leaves_no_instance_folder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. A publisher publishes a public bundle; another user installs it, and the
       install's build is collected, not run.
    2. Drain the build; at the template copy the installer uninstalls.
    3. The install's instance folder is gone; the install is 404; the
       publisher's own environment folder is untouched.
    """
    lm = patch_environment_adapter
    su = superuser_token_headers

    # ── Phase 1: publish, then install without draining ───────────────────
    publisher_agent = create_agent_via_api(client, su, name="Race Bundle")
    drain_tasks()
    publisher_env_id = _only_env_id(client, su, publisher_agent["id"])
    assert (lm.instances_dir / publisher_env_id).is_dir()
    publish_bundle_and_make_public(client, su, publisher_agent["id"])
    bundle_id = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=su
    ).json()["bundle_id"]

    _, installer = make_user_and_headers(client)
    r = client.post(f"{API}/catalog/{bundle_id}/install", headers=installer, json={})
    assert r.status_code == 200, r.text
    install_id = r.json()["id"]
    install_env_id = _only_env_id(client, installer, install_id)
    instance_dir = lm.instances_dir / install_env_id
    assert not instance_dir.exists()

    # ── Phase 2: uninstall lands during the copy ──────────────────────────
    uninstalls: list[int] = []

    def _uninstall() -> None:
        resp = client.post(f"{API}/agents/{install_id}/uninstall", headers=installer)
        uninstalls.append(resp.status_code)

    patcher, copies = _delete_lands_during_copy(lm, _uninstall)
    with patcher:
        drain_tasks()

    assert copies == [instance_dir]
    assert uninstalls == [200]

    # ── Phase 3: no orphan, publisher untouched ───────────────────────────
    assert not instance_dir.exists(), "uninstalled install left its instance folder"
    assert client.get(f"{API}/agents/{install_id}", headers=installer).status_code == 404
    assert (lm.instances_dir / publisher_env_id).is_dir()
    assert client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=su
    ).status_code == 200


# ── Scenario 3: delete before the build starts ────────────────────────────


def test_deleting_agent_before_its_build_starts_drains_cleanly(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. Create an agent; delete it before its collected build runs.
    2. Draining the build raises nothing and writes no instance folder.
    """
    lm = patch_environment_adapter
    headers = superuser_token_headers

    agent = create_agent_via_api(client, headers, name="Delete Before Build")
    env_id = _only_env_id(client, headers, agent["id"])

    r = client.delete(f"{API}/agents/{agent['id']}", headers=headers)
    assert r.status_code == 200, r.text

    drain_tasks()

    assert not (lm.instances_dir / env_id).exists()
    assert client.get(f"{API}/agents/{agent['id']}", headers=headers).status_code == 404


# ── Scenario 4: control — a live build keeps its folder ───────────────────


def test_undisturbed_build_keeps_its_instance_folder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The tail removes a folder only for a row that is gone: a normal build ends
    with its instance folder in place and the environment running.
    """
    lm = patch_environment_adapter
    headers = superuser_token_headers

    agent = create_agent_via_api(client, headers, name="Undisturbed Build")
    drain_tasks()

    envs = list_environments(client, headers, agent["id"])
    assert envs["count"] == 1
    env = envs["data"][0]
    assert env["status"] == "running", env
    instance_dir = lm.instances_dir / env["id"]
    assert (instance_dir / "app" / "workspace").is_dir()
