"""C1 one access model — who sees, publishes and manages a catalog item.

Scenarios:
  1. Bundle matrix: publisher (maintain), a ``use`` grantee, an ``edit``
     grantee, a stranger and a superuser across private → users → users
     delisted → public → public delisted, on the catalog list, the catalog
     entry and the bundle detail. Management (``PATCH /bundles``) is maintain
     or superuser; ``edit`` is not enough.
  2. Skill matrix: the same ladder on ``/skills/catalog`` and the package
     detail, plus the viewer-bound ``can_manage`` / ``is_granted`` flags.
  3. Role grants: ``agent-user`` reaches every holder of that role and nobody
     else; the ``admin`` role also matches a superuser whose role is not
     ``admin``.
  4. Bundle publish needs ``edit``: a consumer holding ``use`` gets 403, and so
     does a publisher demoted to ``use``.
  5. Skill publish into another person's package: ``use`` 403, ``edit`` 200
     (revision lands in the existing package); visibility changes and
     publish-time grants still need ``maintain``.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    bundle_catalog_uuids,
    make_non_admin_superuser,
    make_person,
    publish_agent_bundle,
    publish_skill_package,
    put_item_grants,
    role_grant,
    set_bundle_visibility,
    user_grant,
)
from tests.utils.bundle import install_bundle
from tests.utils.skill_catalog import (
    catalog_ids,
    delist_skill_package,
    entry_for,
    error_code,
    get_skill_package,
    list_skill_catalog,
    make_agent_with_env,
    publish_skill,
    update_skill_package,
    write_skill,
)

API = settings.API_V1_STR


# ── Scenario 1: bundle matrix ─────────────────────────────────────────────


def test_bundle_access_follows_visibility_listing_and_grant_level(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Publisher publishes; grants: user U ``use``, user E ``edit``
      2. private → publisher + E see it; U, stranger, superuser do not
      3. users + listed → U sees it too; stranger and superuser still not
      4. users + delisted → U loses it, E and publisher keep it
      5. public + listed → everyone sees it
      6. public + delisted → hidden from every listing (superuser included)
         but a superuser may still open the bundle detail
      7. Management: E cannot PATCH (403), a co-maintainer and a superuser can
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    user = make_person(client, su)
    editor = make_person(client, su)
    stranger = make_person(client, su)
    co_maintainer = make_person(client, su)

    bundle = publish_agent_bundle(client, publisher.headers, "matrix-agent")
    uuid_, bundle_id = bundle["bundle_uuid"], bundle["bundle_id"]
    put_item_grants(client, publisher.headers, uuid_, [
        user_grant(publisher.id, "maintain"),
        user_grant(co_maintainer.id, "maintain"),
        user_grant(user.id, "use"),
        user_grant(editor.id, "edit"),
    ])

    def sees(headers: dict[str, str]) -> bool:
        return uuid_ in bundle_catalog_uuids(client, headers)

    def entry_status(headers: dict[str, str]) -> int:
        return client.get(f"{API}/catalog/{bundle_id}", headers=headers).status_code

    def detail_status(headers: dict[str, str]) -> int:
        return client.get(f"{API}/bundles/{uuid_}", headers=headers).status_code

    # ── Phase 2: private ──────────────────────────────────────────────
    set_bundle_visibility(client, publisher.headers, uuid_, visibility="private")
    assert sees(publisher.headers) and sees(editor.headers)
    assert not sees(user.headers), "a use grant does nothing under private"
    assert not sees(stranger.headers)
    assert not sees(su), "a superuser has no read-through on a private bundle"
    assert entry_status(user.headers) == 404
    assert detail_status(user.headers) == 403
    assert entry_status(editor.headers) == 200
    assert detail_status(editor.headers) == 200

    # ── Phase 3: users + listed ───────────────────────────────────────
    set_bundle_visibility(client, publisher.headers, uuid_, visibility="users")
    assert sees(user.headers)
    assert entry_status(user.headers) == 200
    assert detail_status(user.headers) == 200
    assert not sees(stranger.headers)
    assert detail_status(stranger.headers) == 403
    assert not sees(su)

    # ── Phase 4: users + delisted ─────────────────────────────────────
    set_bundle_visibility(
        client, publisher.headers, uuid_, visibility="users", is_listed=False
    )
    assert not sees(user.headers), "delisting hides it from use"
    assert entry_status(user.headers) == 404
    assert sees(editor.headers), "delisting does not hide it from edit"
    assert sees(publisher.headers)

    # ── Phase 5: public + listed ──────────────────────────────────────
    set_bundle_visibility(client, publisher.headers, uuid_, visibility="public")
    for headers in (publisher.headers, user.headers, editor.headers, stranger.headers, su):
        assert sees(headers)

    # ── Phase 6: public + delisted ────────────────────────────────────
    set_bundle_visibility(
        client, publisher.headers, uuid_, visibility="public", is_listed=False
    )
    assert not sees(stranger.headers) and not sees(user.headers)
    assert not sees(su), "a delisted public item is never offered in a listing"
    assert detail_status(su) == 200, "but a superuser may still open it"
    assert detail_status(stranger.headers) == 403
    assert sees(editor.headers) and sees(publisher.headers)

    # ── Phase 7: management is maintain or superuser ──────────────────
    r = client.patch(
        f"{API}/bundles/{uuid_}", headers=editor.headers, json={"description": "x"}
    )
    assert r.status_code == 403, r.text
    r = client.patch(
        f"{API}/bundles/{uuid_}", headers=co_maintainer.headers,
        json={"description": "by co-maintainer"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "by co-maintainer"
    r = client.patch(
        f"{API}/bundles/{uuid_}", headers=su, json={"is_listed": True}
    )
    assert r.status_code == 200, r.text
    assert sees(stranger.headers)


# ── Scenario 2: skill matrix ──────────────────────────────────────────────


def test_skill_access_follows_visibility_listing_and_grant_level(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Publisher publishes a private skill; grants: U ``use``, E ``edit``
      2. private → publisher + E see it (E without can_manage, with is_granted);
         U, stranger and superuser get nothing (list and detail 404)
      3. users + listed → U sees it; stranger not
      4. users + delisted (by the maintainer) → U loses it, E keeps it
      5. public + listed → stranger and superuser see it
      6. superuser delists → hidden from every listing, detail still opens
         for the superuser; E keeps it
      7. E cannot PATCH the package (not_publisher)
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    user = make_person(client, su)
    editor = make_person(client, su)
    stranger = make_person(client, su)

    pkg = publish_skill_package(client, publisher.headers, "matrix-skill")
    uuid_ = pkg["package_uuid"]
    put_item_grants(client, publisher.headers, uuid_, [
        user_grant(publisher.id, "maintain"),
        user_grant(user.id, "use"),
        user_grant(editor.id, "edit"),
    ])

    def sees(headers: dict[str, str]) -> bool:
        return uuid_ in catalog_ids(list_skill_catalog(client, headers))

    # ── Phase 2: private ──────────────────────────────────────────────
    own = entry_for(list_skill_catalog(client, publisher.headers), uuid_)
    assert own is not None
    assert own["can_manage"] is True and own["is_granted"] is False
    as_editor = entry_for(list_skill_catalog(client, editor.headers), uuid_)
    assert as_editor is not None
    assert as_editor["can_manage"] is False and as_editor["is_granted"] is True
    assert not sees(user.headers)
    assert not sees(stranger.headers)
    assert not sees(su)
    get_skill_package(client, user.headers, uuid_, expected_status=404)
    get_skill_package(client, su, uuid_, expected_status=404)

    # ── Phase 3: users + listed ───────────────────────────────────────
    update_skill_package(client, publisher.headers, uuid_, visibility="users")
    assert sees(user.headers)
    detail = get_skill_package(client, user.headers, uuid_)
    assert detail["is_granted"] is True and detail["can_manage"] is False
    assert not sees(stranger.headers)
    get_skill_package(client, stranger.headers, uuid_, expected_status=404)

    # ── Phase 4: users + delisted ─────────────────────────────────────
    update_skill_package(client, publisher.headers, uuid_, is_listed=False)
    assert not sees(user.headers)
    get_skill_package(client, user.headers, uuid_, expected_status=404)
    assert sees(editor.headers)
    assert sees(publisher.headers)

    # ── Phase 5: public + listed ──────────────────────────────────────
    update_skill_package(
        client, publisher.headers, uuid_, visibility="public", is_listed=True
    )
    assert sees(stranger.headers) and sees(su) and sees(user.headers)

    # ── Phase 6: superuser delists ────────────────────────────────────
    delist_skill_package(client, su, uuid_)
    assert not sees(stranger.headers)
    assert not sees(su), "a delisted public item is never offered in a listing"
    assert get_skill_package(client, su, uuid_)["is_listed"] is False
    assert sees(editor.headers)

    # ── Phase 7: edit does not manage ─────────────────────────────────
    r = client.patch(
        f"{API}/skills/packages/{uuid_}", headers=editor.headers,
        json={"description": "hijack"},
    )
    assert r.status_code == 403, r.text
    assert error_code(r.json()) == "not_publisher"


# ── Scenario 3: role grants ───────────────────────────────────────────────


def test_role_grants_reach_role_holders_and_admin_role_matches_superusers(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. A users+listed bundle with a role ``agent-user`` use grant: an
         agent-user sees it, an agent-developer does not
      2. A superuser whose role is ``agent-developer`` does not see it, until
         a role ``admin`` grant is added — then they and the seeded superuser do
      3. Same for a users+listed skill package: role ``agent-user`` reaches an
         agent-user (detail is_granted), not a developer
      4. Role ``admin`` with ``edit`` on a PRIVATE skill reaches the superuser
         (edit counts at any visibility)
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    member = make_person(client, su, developer=False)
    developer = make_person(client, su)
    superuser = make_non_admin_superuser(client, su)

    # ── Phase 1: role agent-user on a bundle ──────────────────────────
    bundle = publish_agent_bundle(client, publisher.headers, "role-agent")
    b_uuid = bundle["bundle_uuid"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        role_grant("agent-user", "use"),
    ])
    assert b_uuid in bundle_catalog_uuids(client, member.headers)
    assert b_uuid not in bundle_catalog_uuids(client, developer.headers)

    # ── Phase 2: admin role ⇢ superuser ───────────────────────────────
    assert b_uuid not in bundle_catalog_uuids(client, superuser.headers)
    assert b_uuid not in bundle_catalog_uuids(client, su)
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        role_grant("agent-user", "use"),
        role_grant("admin", "use"),
    ])
    assert b_uuid in bundle_catalog_uuids(client, superuser.headers)
    assert b_uuid in bundle_catalog_uuids(client, su)
    assert b_uuid not in bundle_catalog_uuids(client, developer.headers)

    # ── Phase 3: role agent-user on a skill ───────────────────────────
    pkg = publish_skill_package(client, publisher.headers, "role-skill", visibility="users")
    s_uuid = pkg["package_uuid"]
    put_item_grants(client, publisher.headers, s_uuid, [
        user_grant(publisher.id, "maintain"),
        role_grant("agent-user", "use"),
    ])
    assert s_uuid in catalog_ids(list_skill_catalog(client, member.headers))
    assert get_skill_package(client, member.headers, s_uuid)["is_granted"] is True
    assert s_uuid not in catalog_ids(list_skill_catalog(client, developer.headers))

    # ── Phase 4: admin edit on a private skill ────────────────────────
    private = publish_skill_package(client, publisher.headers, "admin-edit-skill")
    p_uuid = private["package_uuid"]
    assert p_uuid not in catalog_ids(list_skill_catalog(client, superuser.headers))
    put_item_grants(client, publisher.headers, p_uuid, [
        user_grant(publisher.id, "maintain"),
        role_grant("admin", "edit"),
    ])
    assert p_uuid in catalog_ids(list_skill_catalog(client, superuser.headers))
    assert p_uuid not in catalog_ids(list_skill_catalog(client, developer.headers))


# ── Scenario 4: bundle publish needs edit ─────────────────────────────────


def test_bundle_publish_needs_edit(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Publisher shares a users+listed bundle with a consumer (use); the
         consumer installs it
      2. The consumer publishing their install → 403 "You can't publish new
         versions of this agent."
      3. With ``edit`` the access gate passes (C1 has no editor publish path
         from a consumer install: the install-kind check answers 400)
      4. The publisher still publishes; demoted to ``use`` (a co-maintainer
         holds maintain) they get the same 403
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    consumer = make_person(client, su)
    co_maintainer = make_person(client, su)

    bundle = publish_agent_bundle(client, publisher.headers, "publish-gate-agent")
    b_uuid = bundle["bundle_uuid"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        user_grant(consumer.id, "use"),
    ])
    install = install_bundle(client, consumer.headers, bundle["bundle_id"])

    # ── Phase 2: use → 403 ────────────────────────────────────────────
    r = client.post(f"{API}/agents/{install['id']}/publish", headers=consumer.headers, json={})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "You can't publish new versions of this agent."

    # ── Phase 3: edit passes the access gate ──────────────────────────
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        user_grant(consumer.id, "edit"),
    ])
    r = client.post(f"{API}/agents/{install['id']}/publish", headers=consumer.headers, json={})
    assert r.status_code == 400, r.text
    assert "not the publisher install" in r.json()["detail"]

    # ── Phase 4: the publisher, then the demoted publisher ────────────
    r = client.post(f"{API}/agents/{bundle['agent_id']}/publish", headers=publisher.headers, json={})
    assert r.status_code == 200, r.text
    assert r.json()["revision_number"] == 2
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(co_maintainer.id, "maintain"),
        user_grant(publisher.id, "use"),
    ])
    r = client.post(f"{API}/agents/{bundle['agent_id']}/publish", headers=publisher.headers, json={})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "You can't publish new versions of this agent."


# ── Scenario 5: skill publish into another person's package ───────────────


def test_skill_publish_into_another_persons_package_needs_edit(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Publisher publishes ``team-notes`` (users); a colleague has the same
         skill in their own agent
      2. Colleague with ``use`` publishing into it by package_id → 403
      3. With ``edit`` → 200: revision 2 of the publisher's package, published
         by the colleague; the colleague owns no package of their own
      4. The editor may not change visibility or share at publish (403
         not_publisher); nothing was written by either refusal
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    colleague = make_person(client, su)
    third = make_person(client, su)

    pkg = publish_skill_package(client, publisher.headers, "team-notes", visibility="users")
    s_uuid, package_id = pkg["package_uuid"], pkg["package_id"]
    agent_id, env_id = make_agent_with_env(client, colleague.headers, "colleague-agent")
    write_skill(env_id, "team-notes", body="Colleague's improved steps.")

    # ── Phase 2: use → 403 ────────────────────────────────────────────
    put_item_grants(client, publisher.headers, s_uuid, [
        user_grant(publisher.id, "maintain"),
        user_grant(colleague.id, "use"),
    ])
    publish_skill(
        client, colleague.headers, agent_id, "team-notes",
        package_id=package_id, expected_status=403,
    )
    assert get_skill_package(client, publisher.headers, s_uuid)["latest_revision_number"] == 1

    # ── Phase 3: edit → 200 into the existing package ─────────────────
    put_item_grants(client, publisher.headers, s_uuid, [
        user_grant(publisher.id, "maintain"),
        user_grant(colleague.id, "edit"),
    ])
    revision = publish_skill(
        client, colleague.headers, agent_id, "team-notes", package_id=package_id
    )
    assert revision["package_id"] == s_uuid
    assert revision["revision_number"] == 2
    assert revision["published_by_user_id"] == colleague.id
    detail = get_skill_package(client, publisher.headers, s_uuid)
    assert detail["latest_revision_number"] == 2
    assert detail["publisher_user_id"] == publisher.id
    assert [r["revision_number"] for r in detail["revisions"]] == [2, 1]
    colleague_view = list_skill_catalog(client, colleague.headers)
    assert catalog_ids(colleague_view) == {s_uuid}

    # ── Phase 4: visibility and sharing stay with maintain ────────────
    r = client.post(
        f"{API}/agents/{agent_id}/skills/team-notes/publish",
        headers=colleague.headers,
        json={"package_id": package_id, "visibility": "public"},
    )
    assert r.status_code == 403, r.text
    assert error_code(r.json()) == "not_publisher"
    r = client.post(
        f"{API}/agents/{agent_id}/skills/team-notes/publish",
        headers=colleague.headers,
        json={"package_id": package_id, "grant_emails": [third.email]},
    )
    assert r.status_code == 403, r.text
    assert error_code(r.json()) == "not_publisher"
    after = get_skill_package(client, publisher.headers, s_uuid)
    assert after["visibility"] == "users"
    assert after["latest_revision_number"] == 2
    assert s_uuid not in catalog_ids(list_skill_catalog(client, third.headers))
