"""C2 — the unified catalog listing and detail (``/api/v1/catalog/items``).

Scenarios:
  1. One listing across kinds: a cloud bundle, a desktop agent (pushed) and a
     skill package, each in its own shape; ``kind`` and ``runs_on`` filters;
     agents before skills; ``count`` = ``len(data)``; bad filters 422.
  2. Visibility × grants for a desktop agent, before and after its first
     version (P4: an item with no version is listed only to ``edit`` /
     ``maintain``), across private / users / users-delisted / public /
     public-delisted.
  3. ``scope=all``: superuser only (403 otherwise); lists items the superuser
     holds no grant on, with ``access='maintain'``; superuser detail of any
     item is ``maintain``.
  4. Detail carries revisions, newest first, with ``origin``,
     ``git_commit_sha``, notes and publisher; a cloud bundle's revisions are
     ``publish`` without a sha.
  5. Route order: ``/catalog/items`` is the list and ``/catalog/items/{uuid}``
     an item (a non-uuid is 422, never ``/catalog/{bundle_id}``);
     ``/catalog/{bundle_id}`` still serves cloud bundles; ``/api/v1/library``
     is gone (404).
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
    get_item,
    list_items,
    make_person,
    manifest_id_of,
    patch_item,
    repo_id_of,
    user_grant,
    write_agent_tree,
)

API = settings.API_V1_STR


def _push(bridge, tmp_path, owner, item, messages=("first",)) -> list[str]:
    """Clone ``item`` as ``owner``, commit once per message, push; return shas."""
    token = owner.headers["Authorization"].removeprefix("Bearer ")
    git = GitClient(tmp_path, token=token)
    work = tmp_path / f"work-{uuid.uuid4().hex[:6]}"
    assert git.clone(bridge.repo_url(repo_id_of(item)), work).ok
    write_agent_tree(work, slug=item["name"], manifest_id=manifest_id_of(item))
    shas = []
    for i, message in enumerate(messages):
        (work / f"step-{i}.md").write_text(message)
        shas.append(git.commit_all(work, message))
    result = git.push(work)
    assert result.ok, result.output
    return shas


def _uuids(items: list[dict]) -> set[str]:
    return {i["uuid"] for i in items}


def _by_uuid(items: list[dict], item_uuid: str) -> dict | None:
    return next((i for i in items if i["uuid"] == item_uuid), None)


# ── Scenario 1: one listing across kinds ──────────────────────────────────


def test_listing_spans_kinds_and_filters_by_kind_and_runs_on(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    su = superuser_token_headers
    publisher = make_developer(client, su)
    cloud = publish_agent_bundle(client, publisher.headers, "cloud-one")
    skill = publish_skill_package(client, publisher.headers, "pdf-tool")
    desk = create_desktop_agent(client, publisher.headers, display_name="Desk One")
    _push(bridge, tmp_path, publisher, desk)

    every = client.get(f"{API}/catalog/items", headers=publisher.headers)
    assert every.status_code == 200, every.text
    body = every.json()
    assert body["count"] == len(body["data"])
    items = body["data"]
    assert {cloud["bundle_uuid"], skill["package_uuid"], desk["uuid"]} <= _uuids(items)
    kinds = [i["kind"] for i in items]
    assert kinds == sorted(kinds, key=lambda k: k != "agent"), "agents come before skills"

    c = _by_uuid(items, cloud["bundle_uuid"])
    assert c["kind"] == "agent" and c["runs_on"] == "cloud"
    assert c["id"] == cloud["bundle_id"] and c["name"] == c["display_name"]
    assert c["desktop"] is None and c["skill"] is None
    assert c["latest"]["revision_number"] == 1 and c["access"] == "maintain"
    assert isinstance(c["skills"], list)

    d = _by_uuid(items, desk["uuid"])
    assert d["kind"] == "agent" and d["runs_on"] == "desktop"
    assert d["desktop"]["repo_id"] == repo_id_of(desk)
    assert d["desktop"]["head_sha"] and d["desktop"]["contract_version"]
    assert d["latest"]["revision_number"] == 1 and d["skill"] is None

    s = _by_uuid(items, skill["package_uuid"])
    assert s["kind"] == "skill" and s["runs_on"] is None
    assert s["id"] == skill["package_id"] and s["name"] == "pdf-tool"
    assert s["skills"] is None and s["desktop"] is None
    assert s["skill"]["archive_sha256"] and s["skill"]["size_bytes"] > 0
    assert s["latest"]["revision_number"] == 1

    def ids(**params):
        found = list_items(client, publisher.headers, **params)
        return found, _uuids(found)

    found, got = ids(kind="agent")
    assert {cloud["bundle_uuid"], desk["uuid"]} <= got and skill["package_uuid"] not in got
    assert {i["kind"] for i in found} == {"agent"}

    found, got = ids(kind="skill")
    assert skill["package_uuid"] in got and not ({cloud["bundle_uuid"], desk["uuid"]} & got)
    assert {i["kind"] for i in found} == {"skill"}

    found, got = ids(runs_on="desktop")
    assert desk["uuid"] in got and cloud["bundle_uuid"] not in got
    assert skill["package_uuid"] not in got
    assert {i["runs_on"] for i in found} == {"desktop"}

    found, got = ids(runs_on="cloud")
    assert cloud["bundle_uuid"] in got and desk["uuid"] not in got
    assert {i["runs_on"] for i in found} == {"cloud"}

    found, got = ids(kind="agent", runs_on="desktop")
    assert desk["uuid"] in got and {i["runs_on"] for i in found} == {"desktop"}
    assert list_items(client, publisher.headers, kind="skill", runs_on="desktop") == []

    for params in ({"kind": "plugin"}, {"runs_on": "phone"}, {"scope": "mine"}):
        r = client.get(f"{API}/catalog/items", headers=publisher.headers, params=params)
        assert r.status_code == 422, (params, r.text)
    assert client.get(f"{API}/catalog/items").status_code == 401


# ── Scenario 2: visibility × grants, P4 ───────────────────────────────────


def test_desktop_visibility_matrix_before_and_after_first_version(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    """
    Cast: creator (maintain), E (edit), U (use), stranger, superuser (no grant).
    Before any push (P4): creator and E list it; U, stranger, superuser do not.
    U may still open the detail under ``users`` (grant counts), stranger 404.
    After Version 1:
      users + listed    → creator, E, U
      users + delisted  → creator, E
      private           → creator, E (U's stored ``use`` does nothing)
      public + listed   → everyone, stranger with access ``use``
      public + delisted → creator, E only; superuser can still open it
    """
    su = superuser_token_headers
    creator = make_person(client, su)
    editor = make_person(client, su)
    user = make_person(client, su)
    stranger = make_person(client, su)
    item = create_desktop_agent(
        client, creator.headers, visibility="users",
        grants=[user_grant(editor.id, "edit"), user_grant(user.id, "use")],
    )
    uuid_ = item["uuid"]

    def listed_to() -> set[str]:
        out = set()
        for label, headers in (
            ("creator", creator.headers), ("editor", editor.headers),
            ("user", user.headers), ("stranger", stranger.headers), ("su", su),
        ):
            if uuid_ in _uuids(list_items(client, headers)):
                out.add(label)
        return out

    def can_open(headers) -> int:
        return client.get(f"{API}/catalog/items/{uuid_}", headers=headers).status_code

    # ── before any version (P4) ───────────────────────────────────────
    assert listed_to() == {"creator", "editor"}
    assert can_open(user.headers) == 200
    assert can_open(stranger.headers) == 404
    assert _by_uuid(list_items(client, editor.headers), uuid_)["access"] == "edit"

    _push(bridge, tmp_path, creator, item)

    # ── users + listed ────────────────────────────────────────────────
    assert listed_to() == {"creator", "editor", "user"}
    assert _by_uuid(list_items(client, user.headers), uuid_)["access"] == "use"
    assert can_open(stranger.headers) == 404

    # ── users + delisted ──────────────────────────────────────────────
    patch_item(client, creator.headers, uuid_, is_listed=False)
    assert listed_to() == {"creator", "editor"}
    assert can_open(user.headers) == 404

    # ── private ───────────────────────────────────────────────────────
    patch_item(client, creator.headers, uuid_, visibility="private", is_listed=True)
    assert listed_to() == {"creator", "editor"}
    assert can_open(user.headers) == 404

    # ── public + listed ───────────────────────────────────────────────
    patch_item(client, creator.headers, uuid_, visibility="public")
    assert listed_to() == {"creator", "editor", "user", "stranger", "su"}
    assert _by_uuid(list_items(client, stranger.headers), uuid_)["access"] == "use"
    assert can_open(stranger.headers) == 200

    # ── public + delisted ─────────────────────────────────────────────
    patch_item(client, creator.headers, uuid_, is_listed=False)
    assert listed_to() == {"creator", "editor"}
    assert can_open(stranger.headers) == 404
    assert can_open(su) == 200


# ── Scenario 3: scope=all ─────────────────────────────────────────────────


def test_scope_all_is_superuser_only_and_lists_everything_as_maintain(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    su = superuser_token_headers
    creator = make_person(client, su)
    publisher = make_developer(client, su)
    unpushed = create_desktop_agent(client, creator.headers)  # private, no version
    pushed = create_desktop_agent(client, creator.headers)    # private, Version 1
    _push(bridge, tmp_path, creator, pushed)
    cloud = publish_agent_bundle(client, publisher.headers, "private-cloud")
    set_bundle_visibility(client, publisher.headers, cloud["bundle_uuid"], visibility="private")
    skill = publish_skill_package(client, publisher.headers, "hidden-skill", visibility="private")
    wanted = {unpushed["uuid"], pushed["uuid"], cloud["bundle_uuid"], skill["package_uuid"]}

    for headers in (creator.headers, publisher.headers):
        r = client.get(f"{API}/catalog/items", headers=headers, params={"scope": "all"})
        assert r.status_code == 403, r.text

    assert not (wanted & _uuids(list_items(client, su)))
    every = list_items(client, su, scope="all")
    assert wanted <= _uuids(every)
    assert all(i["access"] == "maintain" for i in every)
    assert len(every) == client.get(
        f"{API}/catalog/items", headers=su, params={"scope": "all"}
    ).json()["count"]

    only_desktop = list_items(client, su, scope="all", runs_on="desktop")
    assert {unpushed["uuid"], pushed["uuid"]} <= _uuids(only_desktop)
    assert {i["runs_on"] for i in only_desktop} == {"desktop"}
    only_skills = list_items(client, su, scope="all", kind="skill")
    assert skill["package_uuid"] in _uuids(only_skills)
    assert {i["kind"] for i in only_skills} == {"skill"}

    for item_uuid in wanted:
        assert get_item(client, su, item_uuid)["access"] == "maintain"


# ── Scenario 4: detail with revisions ─────────────────────────────────────


def test_detail_lists_revisions_newest_first(
    client: TestClient, superuser_token_headers: dict[str, str], bridge, tmp_path
) -> None:
    su = superuser_token_headers
    creator = make_person(client, su)
    publisher = make_developer(client, su)
    desk = create_desktop_agent(client, creator.headers)
    token = creator.headers["Authorization"].removeprefix("Bearer ")
    git = GitClient(tmp_path, token=token)
    work = tmp_path / "detail-work"
    assert git.clone(bridge.repo_url(repo_id_of(desk)), work).ok
    write_agent_tree(work, slug=desk["name"], manifest_id=manifest_id_of(desk))
    sha1 = git.commit_all(work, "first")
    assert git.push(work).ok
    (work / "two.md").write_text("two")
    sha2 = git.commit_all(work, "second")
    assert git.push(work).ok

    detail = get_item(client, creator.headers, desk["uuid"])
    revisions = detail["revisions"]
    assert [r["revision_number"] for r in revisions] == [2, 1]
    assert [r["git_commit_sha"] for r in revisions] == [sha2, sha1]
    assert [r["release_notes"] for r in revisions] == ["second", "first"]
    assert {r["origin"] for r in revisions} == {"share"}
    assert all(r["published_by"]["user_id"] == creator.id for r in revisions)
    assert all(r["version"] is None for r in revisions)
    assert detail["latest"]["revision_number"] == 2
    assert detail["desktop"]["head_sha"] == sha2
    # The listing shape carries no revisions.
    assert "revisions" not in _by_uuid(list_items(client, creator.headers), desk["uuid"])

    cloud = publish_agent_bundle(client, publisher.headers, "detail-cloud")
    cloud_detail = get_item(client, publisher.headers, cloud["bundle_uuid"])
    assert [r["revision_number"] for r in cloud_detail["revisions"]] == [1]
    assert cloud_detail["revisions"][0]["origin"] == "publish"
    assert cloud_detail["revisions"][0]["git_commit_sha"] is None

    skill = publish_skill_package(client, publisher.headers, "detail-skill")
    skill_detail = get_item(client, publisher.headers, skill["package_uuid"])
    assert [r["revision_number"] for r in skill_detail["revisions"]] == [1]
    assert skill_detail["revisions"][0]["git_commit_sha"] is None

    assert client.get(
        f"{API}/catalog/items/{uuid.uuid4()}", headers=creator.headers
    ).status_code == 404


# ── Scenario 5: route order and /library removal ──────────────────────────


def test_items_routes_win_over_bundle_id_route_and_library_is_gone(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    publisher = make_developer(client, su)
    cloud = publish_agent_bundle(client, publisher.headers, "route-order")

    r = client.get(f"{API}/catalog/items", headers=publisher.headers)
    assert r.status_code == 200
    assert set(r.json()) == {"data", "count"}

    r = client.get(f"{API}/catalog/items/{cloud['bundle_uuid']}", headers=publisher.headers)
    assert r.status_code == 200
    assert r.json()["uuid"] == cloud["bundle_uuid"] and "revisions" in r.json()

    r = client.get(f"{API}/catalog/items/not-a-uuid", headers=publisher.headers)
    assert r.status_code == 422
    r = client.get(f"{API}/catalog/items/{cloud['bundle_id']}", headers=publisher.headers)
    assert r.status_code == 422

    r = client.get(f"{API}/catalog/{cloud['bundle_id']}", headers=publisher.headers)
    assert r.status_code == 200
    assert r.json()["bundle_uuid"] == cloud["bundle_uuid"]

    for path in ("/library", "/library/", "/library/agents", f"/library/agents/{uuid.uuid4()}"):
        assert client.get(f"{API}{path}", headers=su).status_code == 404, path
    r = client.post(f"{API}/library/agents", headers=su, json={"slug": "x"})
    assert r.status_code == 404
