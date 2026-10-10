"""C2 — creating, editing and removing desktop agents (``/catalog/desktop-agents``,
``PATCH``/``DELETE /catalog/items/{uuid}``).

Scenarios:
  1. Create: minted ``bundle_id`` (``<reversed host>.<8 hex of manifest id>``),
     no version yet, ``name`` = slug, creator is the only maintainer and the
     only person who sees it; audit row; a stranger gets 404.
  2. Conflicts: duplicate slug and duplicate manifest id (409 with the
     sentences), invalid slug / manifest id (422); a manifest id sharing the
     first 8 hex with another gets a ``-2`` / ``-3`` suffix.
  3. Grants at create: user ``use``/``edit`` and role grants are stored, the
     creator is folded to ``maintain``, ``catalog.granted`` reaches grantees.
  4. A refused create leaves nothing behind: invalid grants (422) and a bare
     repo that cannot be created; the slug and manifest id stay free.
  5. PATCH: maintainer and superuser may rename / re-describe / change
     visibility and listing; ``edit`` 403, stranger 404, bad bodies 422.
  6. DELETE: ``edit`` 403, stranger 404; refused (409) while another person
     holds a cloud agent bound to the repo (own binding does not count);
     afterwards the repo moves to ``.deleted/``, git answers 404 and the slug
     and manifest id are free again; superuser may delete.

Unit tests for the id minting live in ``tests/unit/test_desktop_bundle_id.py``.
Local installs (the other half of the delete guard) arrive in C3.
"""
import re
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person as make_developer
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.catalog import (
    bind_cloud_install_seam,
    create_desktop_agent,
    delete_item,
    get_item,
    list_items,
    make_person,
    patch_item,
    random_slug,
    repo_id_of,
    role_grant,
    user_grant,
)
from tests.utils.security_event import events_of_type

API = settings.API_V1_STR
BUNDLE_ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9.\-]{1,253}$"


def _create(client: TestClient, headers: dict, **body):
    payload = {
        "slug": random_slug(),
        "display_name": "Desktop Agent",
        "manifest_id": str(uuid.uuid4()),
        **body,
    }
    return client.post(f"{API}/catalog/desktop-agents", headers=headers, json=payload)


def _uuids(items: list[dict]) -> set[str]:
    return {i["uuid"] for i in items}


def _emits(stub, event_type: str) -> list[dict]:
    return [
        e for e in stub.emitted_events
        if e["event"] == "event" and e["data"]["type"] == event_type
    ]


def _repo_dirs(storage) -> set[str]:
    return {p.name for p in storage.iterdir() if p.name.endswith(".git")}


def _host_prefix(client: TestClient, headers: dict) -> str:
    """The reversed-host prefix every minted id carries, read off a cloud agent."""
    agent = create_agent_via_api(client, headers, name="prefix-probe")
    drain_tasks()
    return agent["bundle_id"].rsplit(".", 1)[0]


# ── Scenario 1: create ────────────────────────────────────────────────────


def test_create_mints_bundle_id_and_is_visible_to_creator_only(
    client: TestClient, superuser_token_headers: dict[str, str], git_storage
) -> None:
    """
    1. A plain user creates a desktop agent
    2. The item: kind agent / runs_on desktop, minted id, no version, the
       creator is publisher + only maintainer, access maintain
    3. The creator lists it; a stranger neither lists nor opens it (404)
    4. The bare repo exists on disk; the creation is audited
    """
    su = superuser_token_headers
    creator = make_person(client, su)
    stranger = make_person(client, su)
    developer = make_developer(client, su)
    manifest_id = uuid.uuid4()
    slug = random_slug()

    item = create_desktop_agent(
        client, creator.headers, slug=slug, display_name="  My Desk Agent ",
        description="Does desk things", manifest_id=str(manifest_id),
    )

    assert item["kind"] == "agent" and item["runs_on"] == "desktop"
    prefix = _host_prefix(client, developer.headers)
    assert item["id"] == f"{prefix}.{manifest_id.hex[:8]}"
    assert re.match(BUNDLE_ID_PATTERN, item["id"])
    assert item["name"] == slug
    assert item["display_name"] == "My Desk Agent"
    assert item["description"] == "Does desk things"
    assert item["visibility"] == "private" and item["is_listed"] is True
    assert item["latest"] is None
    assert item["access"] == "maintain"
    assert item["publisher"]["user_id"] == creator.id
    assert [m["user_id"] for m in item["maintainers"]] == [creator.id]
    assert item["install_count"] == 0 and item["my_installs"] == []
    assert item["required_credential_specs"] == [] and item["skills"] == []
    assert item["skill"] is None
    desktop = item["desktop"]
    assert desktop["manifest_id"] == str(manifest_id)
    assert desktop["slug"] == slug
    assert desktop["default_branch"] == "main"
    assert desktop["head_sha"] is None
    assert desktop["clone_url"] == f"{settings.backend_base_url}/git/{desktop['repo_id']}.git"
    assert f"{desktop['repo_id']}.git" in _repo_dirs(git_storage)

    assert item["uuid"] in _uuids(list_items(client, creator.headers))
    assert item["uuid"] not in _uuids(list_items(client, stranger.headers))
    r = client.get(f"{API}/catalog/items/{item['uuid']}", headers=stranger.headers)
    assert r.status_code == 404

    detail = get_item(client, creator.headers, item["uuid"])
    assert detail["revisions"] == []

    audit = events_of_type(client, creator.headers, "CATALOG_DESKTOP_AGENT_CREATED")
    assert len(audit) == 1
    assert audit[0]["details"]["uuid"] == item["uuid"]
    assert audit[0]["details"]["bundle_id"] == item["id"]
    assert audit[0]["details"]["slug"] == slug


def test_create_requires_sign_in(client: TestClient) -> None:
    r = client.post(
        f"{API}/catalog/desktop-agents",
        json={"slug": random_slug(), "display_name": "x", "manifest_id": str(uuid.uuid4())},
    )
    assert r.status_code == 401


# ── Scenario 2: conflicts and validation ──────────────────────────────────


def test_duplicate_slug_and_manifest_id_are_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. A creates an agent with slug S and manifest M
    2. B (anyone) with slug S → 409 naming the slug
    3. B with manifest M (any slug) → 409 "already in the catalog"
    4. Neither refusal created anything: B lists nothing
    """
    su = superuser_token_headers
    a = make_person(client, su)
    b = make_person(client, su)
    slug, manifest_id = random_slug(), str(uuid.uuid4())
    create_desktop_agent(client, a.headers, slug=slug, manifest_id=manifest_id)

    r = _create(client, b.headers, slug=slug)
    assert r.status_code == 409
    assert r.json()["detail"] == f"An agent called {slug} is already in the catalog."

    r = _create(client, b.headers, manifest_id=manifest_id)
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "This agent is already in the catalog. Share changes to it instead."
    )
    # Same manifest id in another letter case is the same id.
    r = _create(client, b.headers, manifest_id=manifest_id.upper())
    assert r.status_code == 409

    assert [i for i in list_items(client, b.headers) if i["runs_on"] == "desktop"] == []


def test_invalid_create_bodies_are_422(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    person = make_person(client, superuser_token_headers)
    for body in (
        {"slug": "Not A Slug"},
        {"slug": ""},
        {"manifest_id": "not-a-uuid"},
        {"display_name": ""},
        {"display_name": "x" * 256},
        {"description": "x" * 10_001},
        {"visibility": "everyone"},
    ):
        r = _create(client, person.headers, **body)
        assert r.status_code == 422, (body, r.text)


def test_bundle_id_collision_gets_numeric_suffix(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Three manifest ids sharing their first 8 hex → ``base``, ``-2``, ``-3``."""
    person = make_person(client, superuser_token_headers)
    head = uuid.uuid4().hex[:8]
    ids = [
        str(uuid.UUID(head + uuid.uuid4().hex[8:])) for _ in range(3)
    ]
    minted = [
        create_desktop_agent(client, person.headers, manifest_id=m)["id"] for m in ids
    ]
    base = minted[0]
    assert base.endswith(f".{head}")
    assert minted[1:] == [f"{base}-2", f"{base}-3"]
    assert len({i["id"] for i in list_items(client, person.headers, runs_on="desktop")}) == 3


# ── Scenario 3: grants at create ──────────────────────────────────────────


def test_grants_at_create_are_stored_and_announced(
    client: TestClient, superuser_token_headers: dict[str, str], socket_stub
) -> None:
    """
    1. Creator grants U use, E edit, role agent-user use, and names themself
       with ``use`` (folded to maintain)
    2. GET grants: creator maintain, U use, E edit, role use
    3. ``catalog.granted`` went to U and E, not to the creator
    4. Before any push, E lists it (edit) but U does not (P4); U can open it
    """
    su = superuser_token_headers
    creator = make_person(client, su)
    user = make_person(client, su)
    editor = make_person(client, su)
    socket_stub.emitted_events.clear()

    item = create_desktop_agent(
        client, creator.headers, visibility="users",
        grants=[
            user_grant(creator.id, "use"),
            user_grant(user.id, "use"),
            user_grant(editor.id, "edit"),
            role_grant("agent-user", "use"),
        ],
    )
    assert item["visibility"] == "users"
    assert [m["user_id"] for m in item["maintainers"]] == [creator.id]

    r = client.get(f"{API}/catalog/items/{item['uuid']}/grants", headers=creator.headers)
    assert r.status_code == 200, r.text
    levels = {
        (f"user:{g['principal']['user_id']}" if "user_id" in g["principal"]
         else f"role:{g['principal']['role']}"): g["access"]
        for g in r.json()
    }
    assert levels == {
        f"user:{creator.id}": "maintain",
        f"user:{user.id}": "use",
        f"user:{editor.id}": "edit",
        "role:agent-user": "use",
    }

    rooms = {e["room"] for e in _emits(socket_stub, "catalog.granted")}
    assert f"user_{user.id}" in rooms and f"user_{editor.id}" in rooms
    assert f"user_{creator.id}" not in rooms

    assert item["uuid"] in _uuids(list_items(client, editor.headers))
    assert item["uuid"] not in _uuids(list_items(client, user.headers))
    assert get_item(client, user.headers, item["uuid"])["access"] == "use"
    assert get_item(client, editor.headers, item["uuid"])["access"] == "edit"


# ── Scenario 4: a refused create leaves nothing behind ────────────────────


def test_refused_create_leaves_no_repo_and_frees_slug_and_manifest(
    client: TestClient, superuser_token_headers: dict[str, str], git_storage
) -> None:
    """
    1. A role maintainer (422) and an unknown user (422) are refused
    2. The bare repo cannot be created → the request fails
    3. Nothing is listed, no repo dir is on disk
    4. The same slug and manifest id then create fine
    """
    su = superuser_token_headers
    person = make_person(client, su)
    slug, manifest_id = random_slug(), str(uuid.uuid4())

    r = _create(client, person.headers, slug=slug, manifest_id=manifest_id,
                grants=[role_grant("agent-user", "maintain")])
    assert r.status_code == 422, r.text
    r = _create(client, person.headers, slug=slug, manifest_id=manifest_id,
                grants=[user_grant(str(uuid.uuid4()), "use")])
    assert r.status_code == 422, r.text
    assert _repo_dirs(git_storage) == set()

    with patch(
        "app.services.catalog.desktop_agent_service.GitRepoStorage.init_bare",
        side_effect=OSError("disk full"),
    ):
        try:
            r = _create(client, person.headers, slug=slug, manifest_id=manifest_id)
            failed = r.status_code >= 500
        except OSError:
            failed = True
    assert failed

    assert [i for i in list_items(client, person.headers) if i["runs_on"] == "desktop"] == []
    assert _repo_dirs(git_storage) == set()

    item = create_desktop_agent(client, person.headers, slug=slug, manifest_id=manifest_id)
    assert item["name"] == slug
    assert _repo_dirs(git_storage) == {f"{repo_id_of(item)}.git"}


# ── Scenario 5: PATCH ─────────────────────────────────────────────────────


def test_patch_by_maintainer_and_superuser_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Maintainer renames, re-describes, sets visibility public and delists
    2. ``name`` stays the slug; the manifest id and repo do not move
    3. An ``edit`` grantee 403, a stranger 404, a bad body 422
    4. A superuser (no grant) may PATCH
    """
    su = superuser_token_headers
    creator = make_person(client, su)
    editor = make_person(client, su)
    stranger = make_person(client, su)
    item = create_desktop_agent(
        client, creator.headers, grants=[user_grant(editor.id, "edit")]
    )
    uuid_ = item["uuid"]

    updated = patch_item(
        client, creator.headers, uuid_,
        display_name="Renamed", description="New words", visibility="public", is_listed=False,
    )
    assert updated["display_name"] == "Renamed"
    assert updated["description"] == "New words"
    assert updated["visibility"] == "public" and updated["is_listed"] is False
    assert updated["name"] == item["name"]
    assert updated["id"] == item["id"]
    assert updated["desktop"] == item["desktop"]
    assert get_item(client, creator.headers, uuid_)["display_name"] == "Renamed"

    r = client.patch(f"{API}/catalog/items/{uuid_}", headers=editor.headers,
                     json={"display_name": "Hijack"})
    assert r.status_code == 403
    r = client.patch(f"{API}/catalog/items/{uuid_}", headers=stranger.headers,
                     json={"display_name": "Hijack"})
    assert r.status_code == 404
    for body in ({"visibility": "everyone"}, {"display_name": ""}, {"is_listed": "maybe"}):
        r = client.patch(f"{API}/catalog/items/{uuid_}", headers=creator.headers, json=body)
        assert r.status_code == 422, (body, r.text)
    r = client.patch(f"{API}/catalog/items/{uuid.uuid4()}", headers=creator.headers,
                     json={"display_name": "x"})
    assert r.status_code == 404

    by_su = patch_item(client, su, uuid_, is_listed=True, visibility="users")
    assert by_su["is_listed"] is True and by_su["visibility"] == "users"
    assert get_item(client, creator.headers, uuid_)["display_name"] == "Renamed"


# ── Scenario 6: DELETE ────────────────────────────────────────────────────


def test_delete_guard_then_repo_moves_to_trash(
    client: TestClient, superuser_token_headers: dict[str, str], db, git_storage, socket_stub
) -> None:
    """
    1. Creator binds one of their own cloud agents to the repo (seam): DELETE
       is not blocked by it
    2. Another person's cloud agent is bound (seam) → DELETE 409 "1 person"
    3. ``edit`` 403, stranger 404 — the guard never leaks to them
    4. That person deletes their agent → DELETE 204; audit row
    5. The repo is in ``.deleted/``, git answers 404, the item is gone, and
       the slug and manifest id create again
    """
    su = superuser_token_headers
    creator = make_developer(client, su)
    holder = make_developer(client, su)
    editor = make_person(client, su)
    stranger = make_person(client, su)
    item = create_desktop_agent(
        client, creator.headers, grants=[user_grant(editor.id, "edit")]
    )
    uuid_, repo_id = item["uuid"], repo_id_of(item)

    own_agent = create_agent_via_api(client, creator.headers, name="own-cloud")
    holder_agent = create_agent_via_api(client, holder.headers, name="holder-cloud")
    drain_tasks()
    bind_cloud_install_seam(db, own_agent["id"], creator.id, repo_id)
    bind_cloud_install_seam(
        db, holder_agent["id"], holder.id, repo_id, host="https://elsewhere.example"
    )

    r = delete_item(client, creator.headers, uuid_)
    assert r.status_code == 409, r.text
    assert r.json()["detail"].startswith("1 person ")
    assert r.json()["detail"].endswith("It can be removed once nobody does.")
    assert delete_item(client, editor.headers, uuid_).status_code == 403
    assert delete_item(client, stranger.headers, uuid_).status_code == 404
    assert get_item(client, creator.headers, uuid_)["uuid"] == uuid_

    r = client.delete(f"{API}/agents/{holder_agent['id']}", headers=holder.headers)
    assert r.status_code == 200, r.text
    drain_tasks()

    r = delete_item(client, creator.headers, uuid_)
    assert r.status_code == 204, r.text
    audit = events_of_type(client, creator.headers, "CATALOG_ITEM_DELETED")
    assert [e["details"]["uuid"] for e in audit] == [uuid_]
    assert audit[0]["details"]["kind"] == "agent"

    assert f"{repo_id}.git" not in _repo_dirs(git_storage)
    trash = git_storage / ".deleted"
    assert trash.is_dir()
    assert any(p.name.startswith(repo_id) for p in trash.iterdir())
    r = client.get(
        f"/git/{repo_id}.git/info/refs", params={"service": "git-upload-pack"},
        headers=creator.headers,
    )
    assert r.status_code == 404
    assert client.get(f"{API}/catalog/items/{uuid_}", headers=creator.headers).status_code == 404
    assert uuid_ not in _uuids(list_items(client, creator.headers))
    assert delete_item(client, creator.headers, uuid_).status_code == 404

    again = create_desktop_agent(
        client, creator.headers, slug=item["name"],
        manifest_id=item["desktop"]["manifest_id"],
    )
    assert again["id"] == item["id"]
    assert again["uuid"] != uuid_


def test_superuser_may_delete_any_desktop_agent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    creator = make_person(client, superuser_token_headers)
    item = create_desktop_agent(client, creator.headers)
    assert delete_item(client, superuser_token_headers, item["uuid"]).status_code == 204
    r = client.get(f"{API}/catalog/items/{item['uuid']}", headers=creator.headers)
    assert r.status_code == 404
