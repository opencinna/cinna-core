"""C3 — who hears ``catalog.updated`` for each kind of publish (P9).

Audience = users reached through the item's grants (role grants expanded)
∪ cloud installers ∪ local installers who are active and still have access.
Public items get no fan-out to everyone.

Scenarios:
  1. Cloud bundle publish: grant readers + a cloud installer whose grant was
     revoked; a bystander never; after going public, a bystander with
     ``use`` but no grant or install still hears nothing. Meta has no
     ``head_sha``.
  2. Skill publish: grant readers + a cloud installer whose grant was
     revoked; a local installer whose grant was revoked does NOT hear it.
  3. Desktop push: a local installer of a public agent with no grant hears
     it; once the agent goes private (access lost) they do not; after their
     DELETE they do not either. Meta carries ``head_sha``.

``catalog.granted`` is covered by ``catalog_grants_test.py``; the
grant-holder half of the desktop push by ``catalog_desktop_git_test.py``.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    make_person as make_developer,
    publish_agent_bundle,
    publish_skill_package,
)
from tests.utils.bundle import install_bundle, publish_bundle_revision
from tests.utils.catalog import (
    GitClient,
    create_desktop_agent,
    make_person,
    manifest_id_of,
    patch_item,
    put_grants,
    repo_id_of,
    user_grant,
    write_agent_tree,
)
from tests.utils.skill_catalog import (
    install_skill,
    make_agent_with_env,
    publish_skill,
    write_skill,
)

API = settings.API_V1_STR


def _updated(stub) -> list[dict]:
    return [
        e for e in stub.emitted_events
        if e["event"] == "event" and e["data"]["type"] == "catalog.updated"
    ]


def _rooms(stub) -> set[str]:
    events = _updated(stub)
    assert all(e["room"] for e in events)  # never a no-room broadcast
    return {e["room"] for e in events}


def _room(person) -> str:
    return f"user_{person.id}"


def _local_install(client, headers, item_uuid, device_id, host=None) -> None:
    body: dict = {"device_id": device_id, "revision_number": 1}
    if host is not None:
        body["host_manifest_id"] = host
    r = client.post(
        f"{API}/catalog/items/{item_uuid}/local-installs", headers=headers, json=body
    )
    assert r.status_code == 200, r.text


# ── Scenario 1: cloud bundle publish ──────────────────────────────────────


def test_bundle_publish_reaches_grants_and_cloud_installers_only(
    client: TestClient, superuser_token_headers: dict[str, str], socket_stub
) -> None:
    su = superuser_token_headers
    publisher = make_developer(client, su)
    reader = make_person(client, su)
    editor = make_person(client, su)
    installer = make_developer(client, su)
    bystander = make_developer(client, su)
    cloud = publish_agent_bundle(client, publisher.headers, "event-cloud")
    cb = cloud["bundle_uuid"]
    put_grants(client, publisher.headers, cb, [
        user_grant(publisher.id, "maintain"), user_grant(editor.id, "edit"),
        user_grant(reader.id, "use"), user_grant(installer.id, "use"),
    ])
    patch_item(client, publisher.headers, cb, visibility="users", is_listed=True)
    install_bundle(client, installer.headers, cloud["bundle_id"])
    # The installer loses the grant; their cloud agent keeps them in the audience.
    put_grants(client, publisher.headers, cb, [
        user_grant(publisher.id, "maintain"), user_grant(editor.id, "edit"),
        user_grant(reader.id, "use"),
    ])

    socket_stub.emitted_events.clear()
    publish_bundle_revision(client, publisher.headers, cloud["agent_id"], notes="v2")
    assert _rooms(socket_stub) == {
        _room(publisher), _room(editor), _room(reader), _room(installer),
    }
    metas = {tuple(sorted(e["data"]["meta"].items())) for e in _updated(socket_stub)}
    assert metas == {tuple(sorted({
        "kind": "agent", "uuid": cb, "id": cloud["bundle_id"], "revision_number": 2,
    }.items()))}

    # ── public: no fan-out to everyone ────────────────────────────────
    patch_item(client, publisher.headers, cb, visibility="public", is_listed=True)
    socket_stub.emitted_events.clear()
    publish_bundle_revision(client, publisher.headers, cloud["agent_id"], notes="v3")
    rooms = _rooms(socket_stub)
    assert _room(bystander) not in rooms
    # A ``use`` grant does nothing under ``public`` (P1): the reader's access
    # now comes from public reach, which has no fan-out, so they drop out.
    # ``edit``/``maintain`` grants and installers still hear it.
    assert rooms == {_room(publisher), _room(editor), _room(installer)}
    assert {e["data"]["meta"]["revision_number"] for e in _updated(socket_stub)} == {3}


# ── Scenario 2: skill publish ─────────────────────────────────────────────


def test_skill_publish_reaches_grants_cloud_installers_not_revoked_local_installers(
    client: TestClient, superuser_token_headers: dict[str, str], socket_stub
) -> None:
    su = superuser_token_headers
    publisher = make_developer(client, su)
    reader = make_person(client, su)
    cloud_installer = make_developer(client, su)
    local_installer = make_person(client, su)
    bystander = make_developer(client, su)
    skill = publish_skill_package(client, publisher.headers, "event-skill")
    sk = skill["package_uuid"]
    put_grants(client, publisher.headers, sk, [
        user_grant(publisher.id, "maintain"), user_grant(reader.id, "use"),
        user_grant(cloud_installer.id, "use"), user_grant(local_installer.id, "use"),
    ])
    patch_item(client, publisher.headers, sk, visibility="users", is_listed=True)
    agent_id, _ = make_agent_with_env(client, cloud_installer.headers, "skill-holder")
    install_skill(client, cloud_installer.headers, agent_id, sk)
    _local_install(client, local_installer.headers, sk, "laptop", str(uuid.uuid4()))

    # ── with grants intact: everyone with a grant hears it ────────────
    write_skill(skill["env_id"], "event-skill", description="Second.")
    socket_stub.emitted_events.clear()
    rev = publish_skill(client, publisher.headers, skill["agent_id"], "event-skill")
    assert rev["revision_number"] == 2
    assert _rooms(socket_stub) == {
        _room(publisher), _room(reader), _room(cloud_installer), _room(local_installer),
    }
    assert {
        tuple(sorted(e["data"]["meta"].items())) for e in _updated(socket_stub)
    } == {tuple(sorted({
        "kind": "skill", "uuid": sk, "id": skill["package_id"], "revision_number": 2,
    }.items()))}

    # ── both installers lose their grants ─────────────────────────────
    put_grants(client, publisher.headers, sk, [
        user_grant(publisher.id, "maintain"), user_grant(reader.id, "use"),
    ])
    write_skill(skill["env_id"], "event-skill", description="Third.")
    socket_stub.emitted_events.clear()
    publish_skill(client, publisher.headers, skill["agent_id"], "event-skill")
    rooms = _rooms(socket_stub)
    assert rooms == {_room(publisher), _room(reader), _room(cloud_installer)}
    assert _room(local_installer) not in rooms
    assert _room(bystander) not in rooms


# ── Scenario 3: desktop push ──────────────────────────────────────────────


def test_desktop_push_reaches_local_installers_while_they_have_access(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path,
    socket_stub,
) -> None:
    su = superuser_token_headers
    owner = make_person(client, su)
    local = make_person(client, su)
    bystander = make_person(client, su)
    item = create_desktop_agent(client, owner.headers, visibility="public")
    git = GitClient(tmp_path, token=owner.headers["Authorization"].removeprefix("Bearer "))
    work = tmp_path / "w"
    assert git.clone(bridge.repo_url(repo_id_of(item)), work).ok
    write_agent_tree(work, slug=item["name"], manifest_id=manifest_id_of(item))
    git.commit_all(work, "first")
    assert git.push(work).ok

    def push(n: int) -> str:
        (work / f"step-{n}.md").write_text(str(n))
        sha = git.commit_all(work, f"version {n}")
        socket_stub.emitted_events.clear()
        assert git.push(work).ok
        return sha

    # Public, no grant: a local install is what puts them in the audience.
    _local_install(client, local.headers, item["uuid"], "laptop")
    sha = push(2)
    assert _rooms(socket_stub) == {_room(owner), _room(local)}
    assert _updated(socket_stub)[0]["data"]["meta"] == {
        "kind": "agent", "uuid": item["uuid"], "id": item["id"],
        "revision_number": 2, "head_sha": sha,
    }

    # Private: the local row stays but access is gone.
    patch_item(client, owner.headers, item["uuid"], visibility="private")
    push(3)
    assert _rooms(socket_stub) == {_room(owner)}

    # Public again, then the install is removed.
    patch_item(client, owner.headers, item["uuid"], visibility="public", is_listed=True)
    push(4)
    assert _room(local) in _rooms(socket_stub)
    r = client.delete(
        f"{API}/catalog/items/{item['uuid']}/local-installs/laptop", headers=local.headers
    )
    assert r.status_code == 204
    push(5)
    rooms = _rooms(socket_stub)
    assert rooms == {_room(owner)}
    assert _room(bystander) not in rooms
