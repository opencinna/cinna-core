"""C1 ``GET/PUT /catalog/items/{uuid}/grants`` and the old per-kind grant routes.

Scenarios:
  1. Gates and full replace on a bundle: 404 for an unseen or unknown item,
     403 for a viewer who does not maintain, 200 for a maintainer and for a
     superuser without a grant; PUT replaces every row; a co-maintainer can
     remove the publisher.
  2. Validation on a skill package (the uuid resolves in the second table):
     role maintain 422, no person maintains 409, unknown / inactive user and
     unknown role 422, malformed principal 422, a duplicate principal folds to
     its highest level; refusals write nothing; each accepted PUT is audited.
  3. ``catalog.granted`` goes only to users the PUT newly reached — role grants
     expanded, ``use`` under private reaches nobody.
  4. The old ``/bundles/{uuid}/grants`` and ``/skills/packages/{id}/grants``
     routes stay green: they list user grants without the publisher's own
     row, re-granting never downgrades a higher level, and revoking the last
     maintainer is refused.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.catalog.catalog_helpers import (
    bundle_catalog_uuids,
    get_item_grants,
    grant_levels,
    make_person,
    publish_agent_bundle,
    publish_skill_package,
    put_item_grants,
    role_grant,
    set_bundle_visibility,
    user_grant,
)
from tests.utils.security_event import events_of_type
from tests.utils.skill_catalog import (
    add_skill_package_grant,
    catalog_ids,
    error_code,
    list_skill_catalog,
    list_skill_package_grants,
    revoke_skill_package_grant,
)

API = settings.API_V1_STR


def _emits(stub, event_type: str) -> list[dict]:
    return [
        e for e in stub.emitted_events
        if e["event"] == "event" and e["data"]["type"] == event_type
    ]


# ── Scenario 1: gates and full replace ────────────────────────────────────


def test_item_grants_are_maintainer_only_and_put_is_a_full_replace(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. A fresh bundle lists exactly its publisher as maintainer
      2. Unknown uuid 404; stranger 404 on GET and PUT; unauthenticated refused
      3. A ``use`` and an ``edit`` grantee see it but get 403
      4. A superuser without a grant reads the grants of a private bundle
      5. PUT replaces everything (maintain / role edit / use, highest first),
         a second PUT drops the rows it does not name
      6. A co-maintainer removes the publisher, who then loses the item
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    reader = make_person(client, su)
    editor = make_person(client, su)
    stranger = make_person(client, su)
    co_maintainer = make_person(client, su)

    bundle = publish_agent_bundle(client, publisher.headers, "grants-agent")
    b_uuid = bundle["bundle_uuid"]

    # ── Phase 1: publisher is the first maintainer ────────────────────
    grants = get_item_grants(client, publisher.headers, b_uuid)
    assert len(grants) == 1
    assert grants[0]["principal"] == {"user_id": publisher.id}
    assert grants[0]["access"] == "maintain"
    assert grants[0]["display_name"] == publisher.email
    assert grants[0]["granted_by"]["user_id"] == publisher.id
    assert grants[0]["created_at"]

    # ── Phase 2: unseen is 404 ────────────────────────────────────────
    get_item_grants(client, publisher.headers, str(uuid.uuid4()), expected_status=404)
    get_item_grants(client, stranger.headers, b_uuid, expected_status=404)
    put_item_grants(
        client, stranger.headers, b_uuid,
        [user_grant(stranger.id, "maintain")], expected_status=404,
    )
    assert client.get(f"{API}/catalog/items/{b_uuid}/grants").status_code in (401, 403)

    # ── Phase 3: seen without maintain is 403 ─────────────────────────
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        user_grant(reader.id, "use"),
        user_grant(editor.id, "edit"),
    ])
    get_item_grants(client, reader.headers, b_uuid, expected_status=403)
    get_item_grants(client, editor.headers, b_uuid, expected_status=403)
    put_item_grants(
        client, editor.headers, b_uuid,
        [user_grant(editor.id, "maintain")], expected_status=403,
    )

    # ── Phase 4: superuser manages without a grant ────────────────────
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="private")
    assert len(get_item_grants(client, su, b_uuid)) == 3

    # ── Phase 5: full replace ─────────────────────────────────────────
    replaced = put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(reader.id, "use"),
        role_grant("agent-user", "edit"),
        user_grant(publisher.id, "maintain"),
    ])
    assert [g["access"] for g in replaced] == ["maintain", "edit", "use"]
    assert replaced[1]["principal"] == {"role": "agent-user"}
    assert replaced[1]["display_name"] == "agent-user"
    assert grant_levels(replaced) == {
        f"user:{publisher.id}": "maintain",
        "role:agent-user": "edit",
        f"user:{reader.id}": "use",
    }
    assert grant_levels(get_item_grants(client, publisher.headers, b_uuid)) == grant_levels(replaced)
    # editor's row was not named → gone; private bundle → editor loses it.
    assert b_uuid not in bundle_catalog_uuids(client, editor.headers)

    only = put_item_grants(client, publisher.headers, b_uuid, [user_grant(publisher.id, "maintain")])
    assert grant_levels(only) == {f"user:{publisher.id}": "maintain"}

    # ── Phase 6: a co-maintainer removes the publisher ────────────────
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"),
        user_grant(co_maintainer.id, "maintain"),
    ])
    after = put_item_grants(client, co_maintainer.headers, b_uuid, [
        user_grant(co_maintainer.id, "maintain"),
    ])
    # Kept at the same level → keeps who granted it (the publisher).
    assert after[0]["granted_by"]["user_id"] == publisher.id
    assert b_uuid not in bundle_catalog_uuids(client, publisher.headers)
    get_item_grants(client, publisher.headers, b_uuid, expected_status=404)


# ── Scenario 2: validation (skill package) ────────────────────────────────


def test_item_grants_validation_on_a_skill_package(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Role maintain → 422; no person maintains (incl. empty body) → 409
      2. Unknown user, inactive user, unknown role → 422
      3. Malformed principal (both keys) and unknown access level → 422
      4. None of the refusals changed the grants
      5. Duplicates fold to the highest level per principal
      6. Exactly the accepted PUTs are audited (CATALOG_GRANTS_CHANGED)
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    other = make_person(client, su)
    inactive = make_person(client, su)
    r = client.patch(f"{API}/users/{inactive.id}", headers=su, json={"is_active": False})
    assert r.status_code == 200, r.text

    pkg = publish_skill_package(client, publisher.headers, "validated-skill")
    s_uuid = pkg["package_uuid"]
    me = user_grant(publisher.id, "maintain")
    original = get_item_grants(client, publisher.headers, s_uuid)
    assert grant_levels(original) == {f"user:{publisher.id}": "maintain"}

    # ── Phase 1: maintainers ──────────────────────────────────────────
    r = put_item_grants(
        client, publisher.headers, s_uuid,
        [me, role_grant("agent-user", "maintain")], expected_status=422,
    )
    assert r["detail"] == "Only a person can be a maintainer."
    r = put_item_grants(
        client, publisher.headers, s_uuid,
        [user_grant(publisher.id, "edit"), user_grant(other.id, "use")],
        expected_status=409,
    )
    assert r["detail"] == "This item needs at least one maintainer."
    put_item_grants(client, publisher.headers, s_uuid, [], expected_status=409)

    # ── Phase 2: unknown principals ───────────────────────────────────
    put_item_grants(
        client, publisher.headers, s_uuid,
        [me, user_grant(str(uuid.uuid4()), "use")], expected_status=422,
    )
    put_item_grants(
        client, publisher.headers, s_uuid,
        [me, user_grant(inactive.id, "use")], expected_status=422,
    )
    r = put_item_grants(
        client, publisher.headers, s_uuid,
        [me, role_grant("wizard", "use")], expected_status=422,
    )
    assert "wizard" in r["detail"]

    # ── Phase 3: malformed bodies ─────────────────────────────────────
    put_item_grants(
        client, publisher.headers, s_uuid,
        [me, {"principal": {"user_id": other.id, "role": "admin"}, "access": "use"}],
        expected_status=422,
    )
    put_item_grants(
        client, publisher.headers, s_uuid,
        [me, user_grant(other.id, "owner")], expected_status=422,
    )

    # ── Phase 4: nothing changed ──────────────────────────────────────
    assert grant_levels(get_item_grants(client, publisher.headers, s_uuid)) == grant_levels(original)
    assert events_of_type(client, publisher.headers, "CATALOG_GRANTS_CHANGED") == []

    # ── Phase 5: duplicates fold ──────────────────────────────────────
    folded = put_item_grants(client, publisher.headers, s_uuid, [
        user_grant(publisher.id, "use"),
        me,
        user_grant(other.id, "edit"),
        user_grant(other.id, "use"),
        role_grant("agent-user", "use"),
        role_grant("agent-user", "use"),
    ])
    assert len(folded) == 3
    assert grant_levels(folded) == {
        f"user:{publisher.id}": "maintain",
        f"user:{other.id}": "edit",
        "role:agent-user": "use",
    }
    assert s_uuid in catalog_ids(list_skill_catalog(client, other.headers))

    # ── Phase 6: audit ────────────────────────────────────────────────
    audited = events_of_type(client, publisher.headers, "CATALOG_GRANTS_CHANGED")
    assert len(audited) == 1
    assert s_uuid in str(audited[0])


# ── Scenario 3: catalog.granted ───────────────────────────────────────────


def test_catalog_granted_goes_only_to_newly_reached_users(
    client: TestClient, superuser_token_headers: dict[str, str], socket_stub
) -> None:
    """
      1. users+listed bundle: granting A ``use`` notifies A only (the
         publisher already had access)
      2. raising A to edit and adding role agent-user notifies the agent-user
         B, not A or the publisher; repeating the PUT notifies nobody
      3. private skill: a ``use`` grant reaches nobody (no event); turning it
         into ``edit`` notifies C with kind ``skill``
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    a = make_person(client, su)
    b = make_person(client, su, developer=False)
    c = make_person(client, su)

    bundle = publish_agent_bundle(client, publisher.headers, "events-agent")
    b_uuid = bundle["bundle_uuid"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    socket_stub.emitted_events.clear()

    # ── Phase 1 ───────────────────────────────────────────────────────
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"), user_grant(a.id, "use"),
    ])
    granted = _emits(socket_stub, "catalog.granted")
    assert {e["room"] for e in granted} == {f"user_{a.id}"}
    assert granted[0]["data"]["meta"] == {"kind": "agent", "uuid": b_uuid}

    # ── Phase 2 ───────────────────────────────────────────────────────
    socket_stub.emitted_events.clear()
    body = [
        user_grant(publisher.id, "maintain"),
        user_grant(a.id, "edit"),
        role_grant("agent-user", "use"),
    ]
    put_item_grants(client, publisher.headers, b_uuid, body)
    rooms = {e["room"] for e in _emits(socket_stub, "catalog.granted")}
    assert f"user_{b.id}" in rooms
    assert f"user_{a.id}" not in rooms
    assert f"user_{publisher.id}" not in rooms

    socket_stub.emitted_events.clear()
    put_item_grants(client, publisher.headers, b_uuid, body)
    assert _emits(socket_stub, "catalog.granted") == []

    # ── Phase 3 ───────────────────────────────────────────────────────
    pkg = publish_skill_package(client, publisher.headers, "events-skill")
    s_uuid = pkg["package_uuid"]
    socket_stub.emitted_events.clear()
    put_item_grants(client, publisher.headers, s_uuid, [
        user_grant(publisher.id, "maintain"), user_grant(c.id, "use"),
    ])
    assert _emits(socket_stub, "catalog.granted") == []
    put_item_grants(client, publisher.headers, s_uuid, [
        user_grant(publisher.id, "maintain"), user_grant(c.id, "edit"),
    ])
    granted = _emits(socket_stub, "catalog.granted")
    assert {e["room"] for e in granted} == {f"user_{c.id}"}
    assert granted[0]["data"]["meta"] == {"kind": "skill", "uuid": s_uuid}


# ── Scenario 4: old grant routes ──────────────────────────────────────────


def test_old_bundle_grant_routes_still_work_and_never_downgrade(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. A co-maintainer M (via PUT) shows on the old list with ``access``
         maintain; the publisher's own row does not appear
      2. POST {email: M} returns M's row still at maintain
      3. POST {email: A} adds A at use; A sees the users+listed bundle
      4. M uses the old routes (maintain); A gets 403; granting the publisher 400
      5. Revoking A works; with M the sole maintainer, M revoking M → 409
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    m = make_person(client, su)
    a = make_person(client, su)

    bundle = publish_agent_bundle(client, publisher.headers, "old-routes-agent")
    b_uuid = bundle["bundle_uuid"]
    set_bundle_visibility(client, publisher.headers, b_uuid, visibility="users")
    put_item_grants(client, publisher.headers, b_uuid, [
        user_grant(publisher.id, "maintain"), user_grant(m.id, "maintain"),
    ])
    base = f"{API}/bundles/{b_uuid}/grants"

    # ── Phase 1 ───────────────────────────────────────────────────────
    r = client.get(base, headers=publisher.headers)
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert [(g["user_id"], g["access"]) for g in rows] == [(m.id, "maintain")]
    assert r.json()["count"] == 1

    # ── Phase 2: never downgrade ──────────────────────────────────────
    r = client.post(base, headers=publisher.headers, json={"email": m.email})
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == m.id and r.json()["access"] == "maintain"
    assert grant_levels(get_item_grants(client, publisher.headers, b_uuid))[f"user:{m.id}"] == "maintain"

    # ── Phase 3: new grant is use ─────────────────────────────────────
    r = client.post(base, headers=publisher.headers, json={"email": a.email})
    assert r.status_code == 200, r.text
    assert r.json()["access"] == "use"
    a_grant_id = r.json()["id"]
    assert b_uuid in bundle_catalog_uuids(client, a.headers)

    # ── Phase 4: who may use the old routes ───────────────────────────
    assert client.get(base, headers=m.headers).status_code == 200
    assert client.get(base, headers=a.headers).status_code == 403
    r = client.post(base, headers=publisher.headers, json={"email": publisher.email})
    assert r.status_code == 400, r.text

    # ── Phase 5: revoke, last maintainer ──────────────────────────────
    r = client.delete(f"{base}/{a_grant_id}", headers=m.headers)
    assert r.status_code == 200, r.text
    assert b_uuid not in bundle_catalog_uuids(client, a.headers)

    put_item_grants(client, publisher.headers, b_uuid, [user_grant(m.id, "maintain")])
    rows = client.get(base, headers=m.headers).json()["data"]
    assert [(g["user_id"], g["access"]) for g in rows] == [(m.id, "maintain")]
    r = client.delete(f"{base}/{rows[0]['id']}", headers=m.headers)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "This item needs at least one maintainer."
    assert grant_levels(get_item_grants(client, m.headers, b_uuid)) == {f"user:{m.id}": "maintain"}


def test_old_skill_grant_routes_still_work_and_never_downgrade(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. An editor E (via PUT) shows on the old list at ``edit``; no publisher row
      2. POST {email: E} keeps E at edit; POST {email: A} adds A at use
      3. Self-grant still 409; E (edit) cannot manage via the old routes (403)
      4. Revoking A works; revoking the sole maintainer (publisher) → 409
         last_maintainer
    """
    su = superuser_token_headers
    publisher = make_person(client, su)
    e = make_person(client, su)
    a = make_person(client, su)

    pkg = publish_skill_package(client, publisher.headers, "old-routes-skill", visibility="users")
    s_uuid = pkg["package_uuid"]
    put_item_grants(client, publisher.headers, s_uuid, [
        user_grant(publisher.id, "maintain"), user_grant(e.id, "edit"),
    ])

    # ── Phase 1 ───────────────────────────────────────────────────────
    listed = list_skill_package_grants(client, publisher.headers, s_uuid)
    assert [(g["user_id"], g["access"]) for g in listed["data"]] == [(e.id, "edit")]

    # ── Phase 2 ───────────────────────────────────────────────────────
    kept = add_skill_package_grant(client, publisher.headers, s_uuid, e.email)
    assert kept["user_id"] == e.id and kept["access"] == "edit"
    added = add_skill_package_grant(client, publisher.headers, s_uuid, a.email)
    assert added["access"] == "use"
    assert s_uuid in catalog_ids(list_skill_catalog(client, a.headers))
    assert grant_levels(get_item_grants(client, publisher.headers, s_uuid)) == {
        f"user:{publisher.id}": "maintain",
        f"user:{e.id}": "edit",
        f"user:{a.id}": "use",
    }

    # ── Phase 3 ───────────────────────────────────────────────────────
    r = add_skill_package_grant(
        client, publisher.headers, s_uuid, publisher.email, expected_status=409
    )
    assert error_code(r) == "self_grant"
    r = list_skill_package_grants(client, e.headers, s_uuid, expected_status=403)
    assert error_code(r) == "not_publisher"

    # ── Phase 4 ───────────────────────────────────────────────────────
    revoke_skill_package_grant(client, publisher.headers, s_uuid, a.id)
    assert s_uuid not in catalog_ids(list_skill_catalog(client, a.headers))
    r = revoke_skill_package_grant(
        client, publisher.headers, s_uuid, publisher.id, expected_status=409
    )
    assert error_code(r) == "last_maintainer"
    assert grant_levels(get_item_grants(client, publisher.headers, s_uuid))[
        f"user:{publisher.id}"
    ] == "maintain"
