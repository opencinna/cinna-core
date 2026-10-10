"""C3 — local installs Cinna Desktop reports (``/catalog/items/{uuid}/local-installs``).

Scenarios:
  1. Desktop agent: gate (404 for a stranger / unknown item, 401 anonymous),
     validation (unknown version, ``revision_number`` < 1, ``device_id``
     length, ``host_manifest_id`` refused for an agent), upsert moves the
     version, one row per device, delete is idempotent and own-rows-only.
  2. Skill: ``host_manifest_id`` required; one row per host agent; DELETE with
     ``host_manifest_id`` removes that row, without it every row on the device.
  3. A cloud bundle answers 409 ``cloud_only`` (after the access gate: a
     stranger still gets 404); a superuser passes the gate without a grant.
  4. U7: a private skill is installable by someone who can use a desktop
     agent whose LATEST version pins that exact skill version, and only then.
  5. Delete guard: other people's local installs block
     ``DELETE /catalog/items/{uuid}`` ("1 person …" / "2 people …") for a
     desktop agent and a skill; the caller's own local install does not.
  6. A skill linked by one of the caller's own cloud agents: DELETE 409
     "Remove it from your agents first." until the link is removed (P12).

Listing counts and ``my_installs`` across cloud + local live in
``catalog_listing_matrix_test.py``.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    make_person as make_developer,
    publish_agent_bundle,
    publish_skill_package,
    set_bundle_visibility,
)
from tests.utils.catalog import (
    GitClient,
    create_desktop_agent,
    delete_item,
    get_item,
    make_person,
    manifest_id_of,
    patch_item,
    put_grants,
    repo_id_of,
    server_contract_version,
    user_grant,
    write_agent_tree,
)
from tests.utils.skill_catalog import (
    install_skill,
    list_agent_plugins,
    make_agent_with_env,
    publish_skill,
    uninstall_agent_plugin,
    write_skill,
)

API = settings.API_V1_STR


def _url(item_uuid: str, device_id: str | None = None) -> str:
    base = f"{API}/catalog/items/{item_uuid}/local-installs"
    return base if device_id is None else f"{base}/{device_id}"


def _post(client, headers, item_uuid, device_id, revision_number, host=None):
    body: dict = {"device_id": device_id, "revision_number": revision_number}
    if host is not None:
        body["host_manifest_id"] = host
    return client.post(_url(item_uuid), headers=headers, json=body)


def _install(client, headers, item_uuid, device_id, revision_number, host=None) -> dict:
    r = _post(client, headers, item_uuid, device_id, revision_number, host)
    assert r.status_code == 200, r.text
    return r.json()


def _remove(client, headers, item_uuid, device_id, host=None) -> None:
    params = {"host_manifest_id": host} if host else None
    r = client.delete(_url(item_uuid, device_id), headers=headers, params=params)
    assert r.status_code == 204, r.text


def _local(device_id, revision_number, host=None) -> dict:
    return {
        "where": "local",
        "install_id": None,
        "device_id": device_id,
        "host_manifest_id": host,
        "revision_number": revision_number,
    }


class _Share:
    """A clone of a desktop agent's repo; each ``push`` is one version."""

    def __init__(self, bridge, tmp_path, owner, item) -> None:
        self.item = item
        self.git = GitClient(tmp_path, token=owner.headers["Authorization"].removeprefix("Bearer "))
        self.work = tmp_path / f"work-{uuid.uuid4().hex[:6]}"
        assert self.git.clone(bridge.repo_url(repo_id_of(item)), self.work).ok
        self.n = 0

    def push(self, catalog_skills: list[dict] | None = None) -> str:
        manifest: dict | bool = True
        if catalog_skills is not None:
            major, minor, patch = server_contract_version()
            manifest = {
                "id": manifest_id_of(self.item),
                "name": "Pinning Agent",
                "slug": self.item["name"],
                "description": "Pins catalog skills",
                "contract_version": f"{major}.{minor}.{patch}",
                "catalog_skills": catalog_skills,
            }
        write_agent_tree(
            self.work, slug=self.item["name"], manifest=manifest,
            manifest_id=manifest_id_of(self.item),
        )
        self.n += 1
        (self.work / f"step-{self.n}.md").write_text(str(self.n))
        sha = self.git.commit_all(self.work, f"version {self.n}")
        result = self.git.push(self.work)
        assert result.ok, result.output
        return sha


# ── Scenario 1: desktop agent upsert / delete ─────────────────────────────


def test_desktop_agent_local_install_upsert_validation_and_delete(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """
    1. Owner shares a desktop agent with U (use) and pushes Version 1
    2. Gate: stranger and unknown uuid 404, anonymous 401
    3. Validation 422: unknown version, version 0, empty / 129-char device,
       a host_manifest_id on an agent
    4. U reports laptop@1 → my_installs has one local entry; a repeat is a no-op
    5. Version 2 pushed; U reports laptop@2 → the same row moves
    6. U reports desk@1 → two rows (by device), still one person
    7. Owner's DELETE of laptop touches nothing of U's; U's DELETE is
       idempotent; an unknown item DELETE is 204
    """
    su = superuser_token_headers
    owner = make_person(client, su)
    user = make_person(client, su)
    stranger = make_person(client, su)
    item = create_desktop_agent(
        client, owner.headers, visibility="users", grants=[user_grant(user.id, "use")]
    )
    uuid_ = item["uuid"]
    share = _Share(bridge, tmp_path, owner, item)
    share.push()

    # ── Phase 2: gate ─────────────────────────────────────────────────
    assert _post(client, stranger.headers, uuid_, "laptop", 1).status_code == 404
    assert _post(client, user.headers, str(uuid.uuid4()), "laptop", 1).status_code == 404
    r = client.post(_url(uuid_), json={"device_id": "laptop", "revision_number": 1})
    assert r.status_code == 401

    # ── Phase 3: validation ───────────────────────────────────────────
    r = _post(client, user.headers, uuid_, "laptop", 2)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "This item has no version 2."
    assert _post(client, user.headers, uuid_, "laptop", 0).status_code == 422
    assert _post(client, user.headers, uuid_, "", 1).status_code == 422
    assert _post(client, user.headers, uuid_, "x" * 129, 1).status_code == 422
    r = _post(client, user.headers, uuid_, "laptop", 1, host=str(uuid.uuid4()))
    assert r.status_code == 422, r.text
    assert get_item(client, user.headers, uuid_)["my_installs"] == []

    # ── Phase 4: first report ─────────────────────────────────────────
    body = _install(client, user.headers, uuid_, "laptop", 1)
    assert body["uuid"] == uuid_ and body["access"] == "use"
    assert body["my_installs"] == [_local("laptop", 1)]
    assert body["install_count"] == 1
    body = _install(client, user.headers, uuid_, "laptop", 1)
    assert body["my_installs"] == [_local("laptop", 1)]

    # ── Phase 5: upsert moves the version ─────────────────────────────
    share.push()
    body = _install(client, user.headers, uuid_, "laptop", 2)
    assert body["my_installs"] == [_local("laptop", 2)]

    # ── Phase 6: a second device ──────────────────────────────────────
    long_device = "d" * 128
    body = _install(client, user.headers, uuid_, long_device, 1)
    assert body["my_installs"] == [_local(long_device, 1), _local("laptop", 2)]
    assert body["install_count"] == 1
    detail = get_item(client, user.headers, uuid_)
    assert detail["my_installs"] == body["my_installs"]
    owner_view = get_item(client, owner.headers, uuid_)
    assert owner_view["my_installs"] == [] and owner_view["install_count"] == 1

    # ── Phase 7: delete ───────────────────────────────────────────────
    _remove(client, owner.headers, uuid_, "laptop")  # owner has no rows there
    assert len(get_item(client, user.headers, uuid_)["my_installs"]) == 2
    _remove(client, user.headers, uuid_, "laptop")
    _remove(client, user.headers, uuid_, "laptop")  # idempotent
    assert get_item(client, user.headers, uuid_)["my_installs"] == [_local(long_device, 1)]
    _remove(client, user.headers, str(uuid.uuid4()), "laptop")
    _remove(client, user.headers, uuid_, long_device)
    detail = get_item(client, user.headers, uuid_)
    assert detail["my_installs"] == [] and detail["install_count"] == 0
    assert client.delete(_url(uuid_, "laptop")).status_code == 401


# ── Scenario 2: skills are installed into a host agent ────────────────────


def test_skill_local_install_needs_host_and_keeps_one_row_per_host(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Publisher shares a skill with U (use)
    2. U without host_manifest_id → 422; unknown version → 422
    3. U pins it into host A then host B on one device → two rows; a repeat
       for A is an upsert
    4. DELETE ?host_manifest_id=B removes only B; DELETE without it removes
       the rest on that device
    """
    su = superuser_token_headers
    publisher = make_developer(client, su)
    user = make_person(client, su)
    stranger = make_person(client, su)
    skill = publish_skill_package(client, publisher.headers, "local-pdf")
    uuid_ = skill["package_uuid"]
    put_grants(client, publisher.headers, uuid_, [
        user_grant(publisher.id, "maintain"), user_grant(user.id, "use"),
    ])
    patch_item(client, publisher.headers, uuid_, visibility="users", is_listed=True)
    host_a, host_b = sorted([str(uuid.uuid4()), str(uuid.uuid4())])

    # ── Phase 2: validation ───────────────────────────────────────────
    r = _post(client, user.headers, uuid_, "laptop", 1)
    assert r.status_code == 422, r.text
    assert "host_manifest_id" in r.json()["detail"]
    assert _post(client, user.headers, uuid_, "laptop", 9, host=host_a).status_code == 422
    assert _post(client, user.headers, uuid_, "laptop", 1, host="not-a-uuid").status_code == 422
    assert _post(client, stranger.headers, uuid_, "laptop", 1, host=host_a).status_code == 404

    # ── Phase 3: one row per host agent ───────────────────────────────
    body = _install(client, user.headers, uuid_, "laptop", 1, host=host_a)
    assert body["kind"] == "skill" and body["access"] == "use"
    assert body["my_installs"] == [_local("laptop", 1, host_a)]
    _install(client, user.headers, uuid_, "laptop", 1, host=host_b)
    body = _install(client, user.headers, uuid_, "laptop", 1, host=host_a)
    assert body["my_installs"] == [_local("laptop", 1, host_a), _local("laptop", 1, host_b)]
    assert body["install_count"] == 1

    # ── Phase 4: delete one host, then the device ─────────────────────
    _remove(client, user.headers, uuid_, "laptop", host=host_b)
    assert get_item(client, user.headers, uuid_)["my_installs"] == [
        _local("laptop", 1, host_a)
    ]
    _install(client, user.headers, uuid_, "laptop", 1, host=host_b)
    _remove(client, user.headers, uuid_, "laptop")
    detail = get_item(client, user.headers, uuid_)
    assert detail["my_installs"] == [] and detail["install_count"] == 0


# ── Scenario 3: cloud bundles, superusers ─────────────────────────────────


def test_cloud_bundle_is_cloud_only_and_superuser_passes_the_gate(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    su = superuser_token_headers
    publisher = make_developer(client, su)
    stranger = make_person(client, su)
    cloud = publish_agent_bundle(client, publisher.headers, "cloud-local")
    set_bundle_visibility(client, publisher.headers, cloud["bundle_uuid"], visibility="private")

    r = _post(client, publisher.headers, cloud["bundle_uuid"], "laptop", 1)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"code": "cloud_only", "message": "This agent runs in Cinna."}
    # The access gate answers first: a stranger learns nothing.
    assert _post(client, stranger.headers, cloud["bundle_uuid"], "laptop", 1).status_code == 404
    mine = get_item(client, publisher.headers, cloud["bundle_uuid"])["my_installs"]
    assert all(i["where"] == "cloud" for i in mine)

    owner = make_person(client, su)
    private = create_desktop_agent(client, owner.headers)  # private, no grants
    _Share(bridge, tmp_path, owner, private).push()
    body = _install(client, su, private["uuid"], "admin-box", 1)
    assert body["access"] == "maintain"
    assert body["my_installs"] == [_local("admin-box", 1)]
    assert _post(client, stranger.headers, private["uuid"], "laptop", 1).status_code == 404


# ── Scenario 4: U7, a skill reached through a desktop agent's pin ─────────


def test_skill_pinned_by_usable_desktop_agent_latest_version_is_installable(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """
    1. P publishes a PRIVATE skill (v1) and a desktop agent pinning skill v1,
       shared with X (use); X has no grant on the skill
    2. X installs skill v1 → 200 as ``use``; a bystander → 404
    3. P publishes skill v2; X installs v2 → 404 (the agent pins v1)
    4. P pushes an agent version pinning v2 → X v2 200, v1 now 404 (latest only)
    5. P removes X from the agent → v2 404
    """
    su = superuser_token_headers
    publisher = make_developer(client, su)
    member = make_person(client, su)
    bystander = make_person(client, su)
    skill = publish_skill_package(client, publisher.headers, "pinned-tool", visibility="private")
    sk_uuid = skill["package_uuid"]
    sha1 = get_item(client, publisher.headers, sk_uuid)["skill"]["archive_sha256"]

    def pin(revision_number: int, sha: str) -> dict:
        return {
            "package_id": skill["package_id"], "package_uuid": sk_uuid,
            "name": "pinned-tool", "revision_number": revision_number,
            "archive_sha256": sha,
        }

    agent = create_desktop_agent(
        client, publisher.headers, visibility="users",
        grants=[user_grant(member.id, "use")],
    )
    share = _Share(bridge, tmp_path, publisher, agent)
    share.push(catalog_skills=[pin(1, sha1)])
    host = str(uuid.uuid4())

    # ── Phase 2: reached through the pin ──────────────────────────────
    assert client.get(f"{API}/catalog/items/{sk_uuid}", headers=member.headers).status_code == 404
    body = _install(client, member.headers, sk_uuid, "laptop", 1, host=host)
    assert body["uuid"] == sk_uuid and body["access"] == "use"
    assert body["my_installs"] == [_local("laptop", 1, host)]
    assert _post(client, bystander.headers, sk_uuid, "laptop", 1, host=host).status_code == 404

    # ── Phase 3: another version is not pinned ────────────────────────
    write_skill(skill["env_id"], "pinned-tool", description="Second version.")
    rev2 = publish_skill(client, publisher.headers, skill["agent_id"], "pinned-tool")
    assert rev2["revision_number"] == 2
    assert _post(client, member.headers, sk_uuid, "laptop", 2, host=host).status_code == 404

    # ── Phase 4: the latest agent version decides ─────────────────────
    sha2 = get_item(client, publisher.headers, sk_uuid)["skill"]["archive_sha256"]
    share.push(catalog_skills=[pin(2, sha2)])
    body = _install(client, member.headers, sk_uuid, "laptop", 2, host=host)
    assert body["my_installs"] == [_local("laptop", 2, host)]
    assert _post(client, member.headers, sk_uuid, "laptop", 1, host=host).status_code == 404

    # ── Phase 5: losing the agent loses the skill ─────────────────────
    put_grants(client, publisher.headers, agent["uuid"], [user_grant(publisher.id, "maintain")])
    assert _post(client, member.headers, sk_uuid, "laptop", 2, host=host).status_code == 404


# ── Scenario 5: delete guard counts other people's local installs ─────────


def test_delete_guard_counts_other_peoples_local_installs(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """
    Desktop agent and skill alike:
      1. The maintainer's own local install does not block
      2. One other person's local install → 409 "1 person uses this. …"
      3. Two → "2 people use this. …"
      4. Both remove theirs → DELETE 204 (own row still present), item 404
    """
    su = superuser_token_headers
    owner = make_developer(client, su)
    a = make_person(client, su)
    b = make_person(client, su)
    desk = create_desktop_agent(client, owner.headers, visibility="public")
    _Share(bridge, tmp_path, owner, desk).push()
    skill = publish_skill_package(client, owner.headers, "guarded-skill")
    patch_item(client, owner.headers, skill["package_uuid"], visibility="public", is_listed=True)
    host = str(uuid.uuid4())

    for item_uuid, h in ((desk["uuid"], None), (skill["package_uuid"], host)):
        _install(client, owner.headers, item_uuid, "own-box", 1, host=h)

        _install(client, a.headers, item_uuid, "a-box", 1, host=h)
        r = delete_item(client, owner.headers, item_uuid)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "1 person uses this. It can be removed once nobody does."

        _install(client, b.headers, item_uuid, "b-box", 1, host=h)
        _install(client, b.headers, item_uuid, "b-box-2", 1, host=h)
        r = delete_item(client, owner.headers, item_uuid)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "2 people use this. It can be removed once nobody does."
        assert get_item(client, owner.headers, item_uuid)["install_count"] == 2

        _remove(client, a.headers, item_uuid, "a-box")
        _remove(client, b.headers, item_uuid, "b-box")
        r = delete_item(client, owner.headers, item_uuid)
        assert r.status_code == 409, r.text  # b-box-2 is still there
        assert r.json()["detail"].startswith("1 person ")
        _remove(client, b.headers, item_uuid, "b-box-2")

        r = delete_item(client, owner.headers, item_uuid)
        assert r.status_code == 204, r.text
        r = client.get(f"{API}/catalog/items/{item_uuid}", headers=owner.headers)
        assert r.status_code == 404


# ── Scenario 6: the caller's own cloud link blocks a skill delete (P12) ───


def test_skill_delete_refused_while_callers_own_agent_links_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_developer(client, su)
    skill = publish_skill_package(client, publisher.headers, "self-linked")
    uuid_ = skill["package_uuid"]
    agent_id, _ = make_agent_with_env(client, publisher.headers, "self-consumer")
    install_skill(client, publisher.headers, agent_id, uuid_)

    r = delete_item(client, publisher.headers, uuid_)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "Remove it from your agents first."
    assert get_item(client, publisher.headers, uuid_)["uuid"] == uuid_

    link_id = list_agent_plugins(client, publisher.headers, agent_id)[0]["id"]
    uninstall_agent_plugin(client, publisher.headers, agent_id, link_id)

    r = delete_item(client, publisher.headers, uuid_)
    assert r.status_code == 204, r.text
    r = client.get(f"{API}/catalog/items/{uuid_}", headers=publisher.headers)
    assert r.status_code == 404
