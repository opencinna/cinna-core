"""Team library REST API (``/api/v1/library``).

Scenarios: create + validation, listing across the grants matrix, single get,
grants GET/PUT rules, admin listing, delete (holder guard, repo gone), and the
caller's own ``cloud_install_id``.
"""
import uuid

from fastapi.testclient import TestClient

from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.library import (
    GitClient,
    bind_cloud_install_seam,
    create_library_agent,
    delete_library_agent,
    everyone_grant,
    get_grants,
    library_slugs,
    list_library,
    make_person,
    put_grants,
    random_slug,
    role_grant,
    user_grant,
)

_BASE = "/api/v1/library"


def test_create_agent_and_validation(client: TestClient, superuser_token_headers) -> None:
    """
    1. Create -> creator maintain, everyone read, no head/manifest, clone url
    2. Grants listing shows both rows
    3. Duplicate slug -> 409; bad slugs / empty name -> 422; no auth -> 401/403
    """
    owner = make_person(client, superuser_token_headers)
    slug = random_slug()
    created = create_library_agent(client, owner.headers, slug=slug, name="  Sales Bot ")
    assert created["slug"] == slug and created["name"] == "Sales Bot"
    assert created["access"] == "maintain"
    assert created["head_sha"] is None and created["manifest"] is None
    assert created["default_branch"] == "main"
    assert created["clone_url"].endswith(f"/git/{created['repo_id']}.git")
    assert [m["user_id"] for m in created["maintainers"]] == [owner.id]
    assert created["cloud_install_id"] is None

    grants = get_grants(client, owner.headers, created["repo_id"])
    assert {(str(g["principal"]), g["access"]) for g in grants} == {
        (str({"user_id": owner.id}), "maintain"),
        ("everyone", "read"),
    }

    dup = client.post(f"{_BASE}/agents", headers=owner.headers, json={"slug": slug, "name": "x"})
    assert dup.status_code == 409
    assert slug in dup.json()["detail"]
    for bad in ("A", "Upper-Case", "-lead", "has space", "x" * 64, ""):
        r = client.post(f"{_BASE}/agents", headers=owner.headers, json={"slug": bad, "name": "x"})
        assert r.status_code == 422, bad
    r = client.post(f"{_BASE}/agents", headers=owner.headers, json={"slug": random_slug(), "name": ""})
    assert r.status_code == 422
    assert client.post(f"{_BASE}/agents", json={"slug": random_slug(), "name": "x"}).status_code in (401, 403)


def test_listing_follows_grants_and_mirrors_is_empty(
    client: TestClient, superuser_token_headers
) -> None:
    """
    Listing across user / role / everyone grants, max access, non-readers absent;
    a superuser without a grant is absent from /library but present in the admin
    listing; GET one is 404 for non-readers.
    """
    owner = make_person(client, superuser_token_headers)
    by_user = make_person(client, superuser_token_headers)
    developer = make_person(client, superuser_token_headers, role="agent-developer")
    stranger = make_person(client, superuser_token_headers)
    repo = create_library_agent(client, owner.headers, name="Matrix")
    repo_id, slug = repo["repo_id"], repo["slug"]
    owner_grant = user_grant(owner.id, "maintain")

    put_grants(
        client, owner.headers, repo_id,
        [owner_grant, user_grant(by_user.id, "read"), role_grant("agent-developer", "write")],
    )
    listing = list_library(client, by_user.headers)
    assert listing["mirrors"] == []
    mine = next(a for a in listing["agents"] if a["slug"] == slug)
    assert mine["access"] == "read"
    dev_listing = list_library(client, developer.headers)
    assert next(a for a in dev_listing["agents"] if a["slug"] == slug)["access"] == "write"
    assert slug not in library_slugs(list_library(client, stranger.headers))
    assert client.get(f"{_BASE}/agents/{repo_id}", headers=stranger.headers).status_code == 404
    assert client.get(f"{_BASE}/agents/{uuid.uuid4()}", headers=owner.headers).status_code == 404
    assert client.get(f"{_BASE}/agents/{repo_id}", headers=by_user.headers).json()["access"] == "read"

    # max over matching grants: by_user is read by user and write by everyone
    put_grants(client, owner.headers, repo_id, [owner_grant, user_grant(by_user.id, "read"), everyone_grant("write")])
    assert next(a for a in list_library(client, by_user.headers)["agents"] if a["slug"] == slug)["access"] == "write"
    assert next(a for a in list_library(client, stranger.headers)["agents"] if a["slug"] == slug)["access"] == "write"

    # superuser: absent from /library without a grant, present in the admin listing
    put_grants(client, owner.headers, repo_id, [owner_grant])
    assert slug not in library_slugs(list_library(client, superuser_token_headers))
    admin = client.get(f"{_BASE}/admin/agents", headers=superuser_token_headers)
    assert admin.status_code == 200
    row = next(a for a in admin.json() if a["slug"] == slug)
    assert row["owner"]["user_id"] == owner.id
    assert [g["access"] for g in row["grants"]] == ["maintain"]
    assert client.get(f"{_BASE}/admin/agents", headers=owner.headers).status_code == 403


def test_grants_management_rules(client: TestClient, superuser_token_headers) -> None:
    """
    GET grants: maintainer or superuser only. PUT: maintainer and superuser OK,
    writer 403, non-reader 404; maintain only for people (422); at least one
    person maintainer (409).
    """
    owner = make_person(client, superuser_token_headers)
    writer = make_person(client, superuser_token_headers)
    stranger = make_person(client, superuser_token_headers)
    co = make_person(client, superuser_token_headers)
    repo_id = create_library_agent(client, owner.headers)["repo_id"]
    url = f"{_BASE}/agents/{repo_id}/grants"
    owner_grant = user_grant(owner.id, "maintain")
    put_grants(client, owner.headers, repo_id, [owner_grant, user_grant(writer.id, "write")])

    assert client.get(url, headers=writer.headers).status_code == 403
    assert client.put(url, headers=writer.headers, json=[user_grant(writer.id, "maintain")]).status_code == 403
    put_grants(client, owner.headers, repo_id, [owner_grant])  # writer removed
    assert client.get(url, headers=stranger.headers).status_code == 404
    assert client.put(url, headers=stranger.headers, json=[]).status_code == 404

    assert client.put(url, headers=owner.headers, json=[owner_grant, role_grant("agent-developer", "maintain")]).status_code == 422
    assert client.put(url, headers=owner.headers, json=[owner_grant, everyone_grant("maintain")]).status_code == 422
    assert client.put(url, headers=owner.headers, json=[everyone_grant("read")]).status_code == 409
    assert client.put(url, headers=owner.headers, json=[]).status_code == 409
    assert client.put(url, headers=owner.headers, json=[{"principal": {"user_id": owner.id, "role": "x"}, "access": "read"}]).status_code == 422
    assert client.put(url, headers=owner.headers, json=[{"principal": "everyone", "access": "root"}]).status_code == 422
    assert get_grants(client, owner.headers, repo_id)  # failed PUTs changed nothing
    assert len(get_grants(client, owner.headers, repo_id)) == 1

    # superuser manages without holding a grant, and can hand maintenance over
    put_grants(client, superuser_token_headers, repo_id, [user_grant(co.id, "maintain")])
    assert client.get(url, headers=owner.headers).status_code == 404
    assert get_grants(client, co.headers, repo_id)[0]["display_name"]


def test_delete_guard_and_removal(
    client: TestClient, db, bridge, tmp_path, superuser_token_headers
) -> None:
    """
    1. Non-maintainers cannot delete (403 / 404)
    2. Another person's cloud install blocks delete (409), also behind a different host
    3. Own install does not block; delete -> 204, gone from API and no longer clonable
    """
    owner = make_person(client, superuser_token_headers, role="agent-developer")
    holder = make_person(client, superuser_token_headers, role="agent-developer")
    stranger = make_person(client, superuser_token_headers)
    for p in (owner, holder):
        create_random_ai_credential(client, p.headers, set_default=True)
    repo = create_library_agent(client, owner.headers)
    repo_id = repo["repo_id"]
    put_grants(client, owner.headers, repo_id,
               [user_grant(owner.id, "maintain"), user_grant(holder.id, "write")])

    assert delete_library_agent(client, holder.headers, repo_id).status_code == 403
    assert delete_library_agent(client, stranger.headers, repo_id).status_code == 404

    # cloud_install_id: set for the caller's own bound agent only
    own_agent = create_agent_via_api(client, owner.headers)
    bind_cloud_install_seam(db, own_agent["id"], owner.id, repo_id)
    assert client.get(f"{_BASE}/agents/{repo_id}", headers=owner.headers).json()["cloud_install_id"] == own_agent["id"]
    assert next(a for a in list_library(client, owner.headers)["agents"] if a["repo_id"] == repo_id)["cloud_install_id"] == own_agent["id"]
    assert client.get(f"{_BASE}/agents/{repo_id}", headers=holder.headers).json()["cloud_install_id"] is None

    # ── foreign install blocks delete ─────────────────────────────────
    holder_agent = create_agent_via_api(client, holder.headers)
    bind_cloud_install_seam(db, holder_agent["id"], holder.id, repo_id)
    blocked = delete_library_agent(client, owner.headers, repo_id)
    assert blocked.status_code == 409
    assert "uses this agent in Cinna" in blocked.json()["detail"]
    assert client.get(f"{_BASE}/agents/{repo_id}", headers=owner.headers).status_code == 200

    # ── ... also when the install was bound through another host name ─
    other_repo = create_library_agent(client, owner.headers)["repo_id"]
    other_agent = create_agent_via_api(client, holder.headers, name="Second")
    bind_cloud_install_seam(db, other_agent["id"], holder.id, other_repo, host="https://api.other-host.example")
    assert delete_library_agent(client, owner.headers, other_repo).status_code == 409

    # ── removing the holder's install frees the repo ──────────────────
    assert client.delete(f"{_BASE.replace('/library', '')}/agents/{holder_agent['id']}", headers=holder.headers).status_code == 200
    git = GitClient(tmp_path, token=owner.token)
    assert git.clone(bridge.repo_url(repo_id), tmp_path / "before").ok
    assert delete_library_agent(client, owner.headers, repo_id).status_code == 204
    assert client.get(f"{_BASE}/agents/{repo_id}", headers=owner.headers).status_code == 404
    assert repo["slug"] not in library_slugs(list_library(client, owner.headers))
    gone = git.clone(bridge.repo_url(repo_id), tmp_path / "after")
    assert not gone.ok and "not found" in gone.output.lower()
    assert delete_library_agent(client, owner.headers, repo_id).status_code == 404
    # slug is reusable
    create_library_agent(client, owner.headers, slug=repo["slug"])


def test_superuser_can_delete_without_grant(client: TestClient, superuser_token_headers) -> None:
    owner = make_person(client, superuser_token_headers)
    repo_id = create_library_agent(client, owner.headers)["repo_id"]
    assert delete_library_agent(client, superuser_token_headers, repo_id).status_code == 204
