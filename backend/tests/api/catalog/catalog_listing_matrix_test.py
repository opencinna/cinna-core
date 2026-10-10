"""C3 — one listing for every kind: who sees what, counts and ``my_installs``.

Scenarios:
  1. Visibility matrix through ``GET /catalog/items`` for a cloud bundle, a
     desktop agent and a skill package side by side, with the same grants on
     each (a user ``edit`` grant and an ``agent-user`` role ``use`` grant):
     users+listed / users+delisted / private / public+listed /
     public+delisted × publisher, editor, role holder, developer stranger,
     superuser — membership and ``access`` per viewer, identical across kinds.
  2. P4 with a role grant: an unpushed desktop agent is listed to an
     ``edit`` role holder, never to a ``use`` grantee.
  3. ``install_count`` and ``my_installs`` across cloud and local installs:
     a skill (cloud link + local pin, one person counted once, publisher
     excluded, the old ``/skills/catalog`` count agrees), a cloud bundle
     (cloud installs, publisher's working install not counted) and a desktop
     agent (local only, several devices).

``catalog_items_test.py`` covers the desktop-only matrix with user grants,
``scope=all`` and detail revisions; ``catalog_access_test.py`` the old
per-kind routes.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    make_person as make_developer,
    publish_agent_bundle,
    publish_skill_package,
    role_grant,
)
from tests.utils.bundle import install_bundle
from tests.utils.catalog import (
    GitClient,
    create_desktop_agent,
    get_item,
    list_items,
    make_person,
    manifest_id_of,
    patch_item,
    put_grants,
    repo_id_of,
    user_grant,
    write_agent_tree,
)
from tests.utils.skill_catalog import (
    entry_for,
    install_skill,
    list_skill_catalog,
    make_agent_with_env,
)

API = settings.API_V1_STR


def _token(person) -> str:
    return person.headers["Authorization"].removeprefix("Bearer ")


def _push(bridge, tmp_path, owner, item) -> None:
    git = GitClient(tmp_path, token=_token(owner))
    work = tmp_path / f"work-{uuid.uuid4().hex[:6]}"
    assert git.clone(bridge.repo_url(repo_id_of(item)), work).ok
    write_agent_tree(work, slug=item["name"], manifest_id=manifest_id_of(item))
    git.commit_all(work, "first")
    result = git.push(work)
    assert result.ok, result.output


def _by_uuid(items: list[dict], item_uuid: str) -> dict | None:
    return next((i for i in items if i["uuid"] == item_uuid), None)


def _local_install(client, headers, item_uuid, device_id, host=None) -> dict:
    body: dict = {"device_id": device_id, "revision_number": 1}
    if host is not None:
        body["host_manifest_id"] = host
    r = client.post(
        f"{API}/catalog/items/{item_uuid}/local-installs", headers=headers, json=body
    )
    assert r.status_code == 200, r.text
    return r.json()


def _local(device_id, host=None, revision_number=1) -> dict:
    return {
        "where": "local", "install_id": None, "device_id": device_id,
        "host_manifest_id": host, "revision_number": revision_number,
    }


# ── Scenario 1: visibility × grants × roles × kinds ───────────────────────


def test_visibility_matrix_is_the_same_for_every_kind(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """
    Cast: publisher P (maintain), editor E (user edit grant, developer),
    role holder R (plain ``agent-user``, reached by the role ``use`` grant),
    developer stranger D, superuser S (no grant).

      users + listed    → P maintain, E edit, R use
      users + delisted  → P, E
      private           → P, E (the role ``use`` grant does nothing)
      public + listed   → P maintain, E edit, R use, D use, S use
      public + delisted → P, E; S can still open it, R cannot
    """
    su = superuser_token_headers
    publisher = make_developer(client, su)
    editor = make_developer(client, su)
    role_holder = make_person(client, su)
    stranger = make_developer(client, su)

    cloud = publish_agent_bundle(client, publisher.headers, "matrix-cloud")["bundle_uuid"]
    skill = publish_skill_package(client, publisher.headers, "matrix-skill")["package_uuid"]
    desk_item = create_desktop_agent(client, publisher.headers, display_name="Matrix Desk")
    _push(bridge, tmp_path, publisher, desk_item)
    desk = desk_item["uuid"]
    kinds = {cloud: ("agent", "cloud"), desk: ("agent", "desktop"), skill: ("skill", None)}

    grants = [
        user_grant(publisher.id, "maintain"),
        user_grant(editor.id, "edit"),
        role_grant("agent-user", "use"),
    ]
    for item_uuid in kinds:
        put_grants(client, publisher.headers, item_uuid, grants)

    viewers = {
        "P": publisher.headers, "E": editor.headers, "R": role_holder.headers,
        "D": stranger.headers, "S": su,
    }

    def seen() -> dict[str, dict[str, str]]:
        """``{viewer: {item_uuid: access}}`` over the three items."""
        out: dict[str, dict[str, str]] = {}
        for label, headers in viewers.items():
            items = list_items(client, headers)
            out[label] = {i["uuid"]: i["access"] for i in items if i["uuid"] in kinds}
            for i in items:
                if i["uuid"] in kinds:
                    assert (i["kind"], i["runs_on"]) == kinds[i["uuid"]]
        return out

    def expect(levels: dict[str, str]) -> dict[str, dict[str, str]]:
        return {
            label: {u: levels[label] for u in kinds} if label in levels else {}
            for label in viewers
        }

    def set_state(visibility: str, is_listed: bool) -> None:
        for item_uuid in kinds:
            body = patch_item(
                client, publisher.headers, item_uuid,
                visibility=visibility, is_listed=is_listed,
            )
            assert (body["visibility"], body["is_listed"]) == (visibility, is_listed)

    # ── users + listed ────────────────────────────────────────────────
    set_state("users", True)
    assert seen() == expect({"P": "maintain", "E": "edit", "R": "use"})

    # ── users + delisted ──────────────────────────────────────────────
    set_state("users", False)
    assert seen() == expect({"P": "maintain", "E": "edit"})
    for item_uuid in kinds:
        r = client.get(f"{API}/catalog/items/{item_uuid}", headers=role_holder.headers)
        assert r.status_code == 404

    # ── private ───────────────────────────────────────────────────────
    set_state("private", True)
    assert seen() == expect({"P": "maintain", "E": "edit"})

    # ── public + listed ───────────────────────────────────────────────
    set_state("public", True)
    assert seen() == expect(
        {"P": "maintain", "E": "edit", "R": "use", "D": "use", "S": "use"}
    )

    # ── public + delisted ─────────────────────────────────────────────
    set_state("public", False)
    assert seen() == expect({"P": "maintain", "E": "edit"})
    for item_uuid in kinds:
        assert get_item(client, su, item_uuid)["uuid"] == item_uuid
        r = client.get(f"{API}/catalog/items/{item_uuid}", headers=role_holder.headers)
        assert r.status_code == 404

    # The kind filter narrows the same answer.
    set_state("public", True)
    only_skills = {i["uuid"] for i in list_items(client, stranger.headers, kind="skill")}
    assert skill in only_skills and not ({cloud, desk} & only_skills)
    only_desktop = {i["uuid"] for i in list_items(client, stranger.headers, runs_on="desktop")}
    assert desk in only_desktop and not ({cloud, skill} & only_desktop)


# ── Scenario 2: P4 with a role grant ──────────────────────────────────────


def test_unpushed_desktop_agent_listed_to_edit_role_holders_only(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    su = superuser_token_headers
    creator = make_person(client, su)
    developer = make_developer(client, su)  # reached by the role edit grant
    user = make_person(client, su)          # user use grant
    item = create_desktop_agent(
        client, creator.headers, visibility="users",
        grants=[role_grant("agent-developer", "edit"), user_grant(user.id, "use")],
    )

    assert _by_uuid(list_items(client, developer.headers), item["uuid"])["access"] == "edit"
    assert _by_uuid(list_items(client, user.headers), item["uuid"]) is None
    assert _by_uuid(list_items(client, creator.headers), item["uuid"])["latest"] is None

    _push(bridge, tmp_path, creator, item)
    assert _by_uuid(list_items(client, user.headers), item["uuid"])["access"] == "use"
    assert _by_uuid(list_items(client, developer.headers), item["uuid"])["latest"][
        "revision_number"
    ] == 1


# ── Scenario 3: install_count and my_installs across cloud + local ────────


def test_counts_and_my_installs_across_cloud_and_local(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """
    Skill (public): A links it on a cloud agent and pins it locally → counted
    once; B pins it locally → 2; the publisher's own local pin is not
    counted. Each viewer's ``my_installs`` is their own (cloud first, then
    local). ``/skills/catalog`` reports the same count. B's removal → 1.

    Cloud bundle (public): A installs it → 1; the publisher's working agent
    is in the publisher's ``my_installs`` but not in the count.

    Desktop agent (public): A on two devices + B → 2; publisher not counted.
    """
    su = superuser_token_headers
    publisher = make_developer(client, su)
    a = make_developer(client, su)
    b = make_person(client, su)
    host_a, host_b, host_p = (str(uuid.uuid4()) for _ in range(3))

    # ── Skill ─────────────────────────────────────────────────────────
    skill = publish_skill_package(client, publisher.headers, "counted-skill")
    sk = skill["package_uuid"]
    patch_item(client, publisher.headers, sk, visibility="public", is_listed=True)
    assert get_item(client, b.headers, sk)["install_count"] == 0

    a_agent, _ = make_agent_with_env(client, a.headers, "skill-consumer")
    install_skill(client, a.headers, a_agent, sk)
    view = get_item(client, a.headers, sk)
    assert view["install_count"] == 1
    assert view["my_installs"] == [
        {"where": "cloud", "install_id": a_agent, "device_id": None,
         "host_manifest_id": None, "revision_number": 1}
    ]

    body = _local_install(client, a.headers, sk, "a-laptop", host_a)
    assert body["install_count"] == 1
    assert [i["where"] for i in body["my_installs"]] == ["cloud", "local"]
    assert body["my_installs"][1] == _local("a-laptop", host_a)

    _local_install(client, b.headers, sk, "b-laptop", host_b)
    body = _local_install(client, publisher.headers, sk, "p-laptop", host_p)
    assert body["install_count"] == 2
    assert body["my_installs"] == [_local("p-laptop", host_p)]

    listed = _by_uuid(list_items(client, b.headers), sk)
    assert listed["install_count"] == 2
    assert listed["my_installs"] == [_local("b-laptop", host_b)]
    assert _by_uuid(list_items(client, su), sk)["my_installs"] == []
    assert entry_for(list_skill_catalog(client, b.headers), sk)["install_count"] == 2

    r = client.delete(
        f"{API}/catalog/items/{sk}/local-installs/b-laptop", headers=b.headers
    )
    assert r.status_code == 204
    assert get_item(client, publisher.headers, sk)["install_count"] == 1
    assert entry_for(list_skill_catalog(client, b.headers), sk)["install_count"] == 1

    # ── Cloud bundle ──────────────────────────────────────────────────
    cloud = publish_agent_bundle(client, publisher.headers, "counted-cloud")
    cb = cloud["bundle_uuid"]
    patch_item(client, publisher.headers, cb, visibility="public", is_listed=True)
    assert get_item(client, b.headers, cb)["install_count"] == 0
    installed = install_bundle(client, a.headers, cloud["bundle_id"])
    view = get_item(client, a.headers, cb)
    assert view["install_count"] == 1
    assert view["my_installs"] == [
        {"where": "cloud", "install_id": installed["id"], "device_id": None,
         "host_manifest_id": None, "revision_number": 1}
    ]
    own = get_item(client, publisher.headers, cb)
    assert own["install_count"] == 1
    assert [i["install_id"] for i in own["my_installs"]] == [cloud["agent_id"]]
    assert get_item(client, b.headers, cb)["my_installs"] == []

    # ── Desktop agent ─────────────────────────────────────────────────
    desk = create_desktop_agent(client, publisher.headers, visibility="public")
    _push(bridge, tmp_path, publisher, desk)
    _local_install(client, a.headers, desk["uuid"], "a-laptop")
    body = _local_install(client, a.headers, desk["uuid"], "a-desktop")
    assert body["install_count"] == 1
    assert body["my_installs"] == [_local("a-desktop"), _local("a-laptop")]
    _local_install(client, b.headers, desk["uuid"], "b-laptop")
    body = _local_install(client, publisher.headers, desk["uuid"], "p-laptop")
    assert body["install_count"] == 2
    assert body["my_installs"] == [_local("p-laptop")]
    assert _by_uuid(list_items(client, a.headers), desk["uuid"])["my_installs"] == [
        _local("a-desktop"), _local("a-laptop")
    ]
