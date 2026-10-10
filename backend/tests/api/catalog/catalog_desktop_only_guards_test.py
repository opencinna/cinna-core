"""C2 — every cloud path refuses a desktop agent (plan §2.5).

A desktop agent runs on the person's computer, so the cloud catalog, install
and bundle-management paths answer 409 ``{"code": "desktop_only", ...}`` to
anyone who can see it, and keep their usual 403/404 for anyone who cannot (the
refusal never reveals an item).

Scenarios:
  1. Install paths on a pushed, public desktop agent: ``GET /catalog/{id}``,
     ``install-context``, ``install`` (creator and another viewer),
     ``admin-install`` (superuser) → 409; a stranger to a private one gets
     404/403, not ``desktop_only``.
  2. ``/bundles/{uuid}``: GET, PATCH, DELETE, revisions, revision delete,
     grants list/add/revoke → 409 for the maintainer.
  3. Old listings: ``GET /catalog/`` and ``GET /bundles/`` leave desktop
     agents out, while ``/catalog/items`` lists them.
  4. ``POST /agents/checkout`` of a core clone URL (this host or another
     origin) → 409 before cloning.
  5. A desktop agent is never minted a cloud agent's ``bundle_id`` (it gets a
     ``-2`` suffix), and a cloud publish cannot take the desktop agent's id.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import make_person as make_developer
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import publish_bundle_revision
from tests.utils.catalog import (
    GitClient,
    create_desktop_agent,
    get_item,
    list_items,
    make_person,
    manifest_id_of,
    repo_id_of,
    write_agent_tree,
)

API = settings.API_V1_STR
DESKTOP_ONLY = {
    "code": "desktop_only",
    "message": "This agent runs on your computer in Cinna Desktop.",
}


def _push_first_version(bridge, tmp_path, owner, item) -> str:
    git = GitClient(tmp_path, token=owner.headers["Authorization"].removeprefix("Bearer "))
    work = tmp_path / f"work-{uuid.uuid4().hex[:6]}"
    assert git.clone(bridge.repo_url(repo_id_of(item)), work).ok
    write_agent_tree(work, slug=item["name"], manifest_id=manifest_id_of(item))
    sha = git.commit_all(work, "first")
    result = git.push(work)
    assert result.ok, result.output
    return sha


def _assert_desktop_only(r) -> None:
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == DESKTOP_ONLY


# ── Scenario 1: install paths ─────────────────────────────────────────────


def test_install_paths_refuse_a_desktop_agent(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """
    1. Creator creates a public desktop agent and pushes Version 1
    2. Entry, install-context, install → 409 for creator and for a viewer
    3. Superuser admin-install for the viewer → 409
    4. The viewer has no new agent afterwards
    """
    su = superuser_token_headers
    creator = make_developer(client, su)
    viewer = make_developer(client, su)
    item = create_desktop_agent(client, creator.headers, visibility="public")
    _push_first_version(bridge, tmp_path, creator, item)
    assert get_item(client, viewer.headers, item["uuid"])["latest"]["revision_number"] == 1
    bundle_id = item["id"]

    agents_before = client.get(f"{API}/agents/", headers=viewer.headers).json()["count"]
    for person in (creator, viewer):
        _assert_desktop_only(client.get(f"{API}/catalog/{bundle_id}", headers=person.headers))
        _assert_desktop_only(
            client.get(f"{API}/catalog/{bundle_id}/install-context", headers=person.headers)
        )
        _assert_desktop_only(
            client.post(f"{API}/catalog/{bundle_id}/install", headers=person.headers, json={})
        )
    _assert_desktop_only(
        client.post(
            f"{API}/catalog/{bundle_id}/admin-install",
            headers=su,
            json={"target_user_id": viewer.id},
        )
    )
    drain_tasks()
    assert client.get(f"{API}/agents/", headers=viewer.headers).json()["count"] == agents_before


def test_strangers_get_not_found_not_desktop_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A private desktop agent stays hidden: the refusal is the usual 404/403."""
    su = superuser_token_headers
    creator = make_person(client, su)
    stranger = make_developer(client, su)
    item = create_desktop_agent(client, creator.headers)
    bundle_id = item["id"]

    r = client.get(f"{API}/catalog/{bundle_id}", headers=stranger.headers)
    assert r.status_code == 404
    r = client.get(f"{API}/catalog/{bundle_id}/install-context", headers=stranger.headers)
    assert r.status_code == 404
    r = client.post(f"{API}/catalog/{bundle_id}/install", headers=stranger.headers, json={})
    assert r.status_code == 403
    assert r.json()["detail"] != DESKTOP_ONLY
    r = client.get(f"{API}/bundles/{item['uuid']}", headers=stranger.headers)
    assert r.status_code == 403


# ── Scenario 2: /bundles/{uuid}* ──────────────────────────────────────────


def test_bundle_routes_refuse_a_desktop_agent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Every ``/bundles/{uuid}`` route answers 409 for the maintainer, and every
    management route for a superuser; the agent itself is untouched afterwards."""
    su = superuser_token_headers
    creator = make_developer(client, su)
    other = make_person(client, su)
    item = create_desktop_agent(client, creator.headers, display_name="Kept As Is")
    base = f"{API}/bundles/{item['uuid']}"

    # GET follows catalog visibility first: a superuser with no grant on a
    # private item gets the usual 403, the maintainer gets desktop_only.
    _assert_desktop_only(client.get(base, headers=creator.headers))
    assert client.get(base, headers=su).status_code == 403
    for headers in (creator.headers, su):
        _assert_desktop_only(client.patch(base, headers=headers, json={"display_name": "x"}))
        _assert_desktop_only(client.get(f"{base}/revisions", headers=headers))
        _assert_desktop_only(
            client.delete(f"{base}/revisions/{uuid.uuid4()}", headers=headers)
        )
        _assert_desktop_only(client.get(f"{base}/grants", headers=headers))
        _assert_desktop_only(
            client.post(f"{base}/grants", headers=headers, json={"email": other.email})
        )
        _assert_desktop_only(client.delete(f"{base}/grants/{uuid.uuid4()}", headers=headers))
        _assert_desktop_only(client.delete(base, headers=headers))

    after = get_item(client, creator.headers, item["uuid"])
    assert after["display_name"] == "Kept As Is"
    assert after["visibility"] == "private"


# ── Scenario 3: old listings ──────────────────────────────────────────────


def test_old_listings_exclude_desktop_agents(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """A pushed, public, listed desktop agent is on ``/catalog/items`` but on
    neither ``GET /catalog/`` nor its creator's ``GET /bundles/``."""
    su = superuser_token_headers
    creator = make_developer(client, su)
    viewer = make_developer(client, su)
    item = create_desktop_agent(client, creator.headers, visibility="public")
    _push_first_version(bridge, tmp_path, creator, item)

    assert item["uuid"] in {i["uuid"] for i in list_items(client, viewer.headers)}
    for person in (creator, viewer):
        r = client.get(f"{API}/catalog/", headers=person.headers)
        assert r.status_code == 200
        entries = r.json()["data"]
        assert item["uuid"] not in {e["bundle_uuid"] for e in entries}
        assert item["id"] not in {e.get("bundle_id") for e in entries}
    r = client.get(f"{API}/bundles/", headers=creator.headers)
    assert r.status_code == 200
    assert item["uuid"] not in {b["id"] for b in r.json()["data"]}
    assert r.json()["count"] == len(r.json()["data"])


# ── Scenario 4: checkout of a core URL ────────────────────────────────────


def test_checkout_of_a_core_repo_url_is_desktop_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The clone URL (and the same ``/git/<repo_id>.git`` path on another
    origin, with or without ``.git``) → 409 before cloning; no agent is made."""
    su = superuser_token_headers
    creator = make_developer(client, su)
    item = create_desktop_agent(client, creator.headers)
    repo_id = repo_id_of(item)
    agents_before = client.get(f"{API}/agents/", headers=creator.headers).json()["count"]

    for url in (
        item["desktop"]["clone_url"],
        f"https://api.elsewhere.example/git/{repo_id}.git",
        f"https://elsewhere.example/git/{repo_id}",
        f"https://elsewhere.example/git/{repo_id.upper()}.git/",
    ):
        r = client.post(
            f"{API}/agents/checkout", headers=creator.headers, json={"repo_url": url}
        )
        _assert_desktop_only(r)

    drain_tasks()
    assert client.get(f"{API}/agents/", headers=creator.headers).json()["count"] == agents_before


# ── Scenario 5: a desktop agent's id is never a cloud agent's ────────────


def test_desktop_and_cloud_ids_never_collide(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Developer creates a cloud agent (bundle id ``<prefix>.<8 hex of agent id>``)
    2. Someone else creates a desktop agent whose manifest id shares those 8
       hex: it is minted ``-2`` rather than the cloud agent's id
    3. The cloud agent still publishes under its own id; asking to publish
       under the desktop agent's id instead is refused (409)
    4. The desktop agent is unchanged (no version, same publisher)
    """
    su = superuser_token_headers
    developer = make_developer(client, su)
    desk_owner = make_person(client, su)
    agent = create_agent_via_api(client, developer.headers, name="id-clash")
    drain_tasks()
    head = agent["bundle_id"].rsplit(".", 1)[1]
    manifest_id = str(uuid.UUID(head + uuid.uuid4().hex[8:]))
    item = create_desktop_agent(client, desk_owner.headers, manifest_id=manifest_id)
    assert item["id"] == f"{agent['bundle_id']}-2"

    r = client.post(
        f"{API}/agents/{agent['id']}/publish",
        headers=developer.headers,
        json={"bundle_id": item["id"]},
    )
    assert r.status_code == 409, r.text

    revision = publish_bundle_revision(client, developer.headers, agent["id"])
    assert revision["revision_number"] == 1
    r = client.get(f"{API}/catalog/{agent['bundle_id']}", headers=developer.headers)
    assert r.status_code == 200, r.text

    after = get_item(client, desk_owner.headers, item["uuid"])
    assert after["latest"] is None and after["publisher"]["user_id"] == desk_owner.id
