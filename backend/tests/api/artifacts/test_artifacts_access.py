"""Who can see an artifact, and how fast that changes (P3, D1).

Scenarios:
  1. A private artifact is invisible to another user everywhere (detail,
     view, download, list) — 404, never 403. Flipping it to ``instance`` makes
     it visible to every signed-in user (``shared_with_me``, detail without
     grants/links, only the current revision); flipping back kills the
     viewer's live cap on its next request.
  2. A grant makes it visible to that person only; removing the grant kills
     their live cap on the next request. Grants are people only: a role
     principal is 422, the owner in the list is dropped, an unknown user 400.
  3. Lists: ``mine`` vs ``shared_with_me``, ``q`` on the title, ordering by
     last change; owners see the sharing summary, others see zeros.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.artifacts import (
    build_zip,
    create_link,
    entry_url,
    get_artifact,
    list_artifacts,
    publish,
    republish,
    set_grants,
    update_artifact,
    view,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


def _site() -> bytes:
    return build_zip({"index.html": "<p>hi</p>"})


def test_private_instance_and_flip_back(client: TestClient) -> None:
    _, owner = create_random_user_with_headers(client)
    _, other = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site(), title="Quarterly")
    republish(client, owner, a["id"], archive=_site())

    # ── Private: 404 everywhere for another user ──
    get_artifact(client, other, a["id"], expect=404)
    view(client, other, a["id"], expect=404)
    assert client.get(f"{API}/artifacts/{a['id']}/download", headers=other).status_code == 404
    assert list_artifacts(client, other, scope="shared_with_me")["count"] == 0

    # ── Instance: every signed-in user sees it, current revision only ──
    flipped = update_artifact(client, owner, a["id"], visibility="instance")
    assert flipped["visibility"] == "instance"
    shared = list_artifacts(client, other, scope="shared_with_me")
    assert [x["id"] for x in shared["data"]] == [a["id"]]
    assert shared["data"][0]["is_owner"] is False
    # owner-private context is not exposed to other viewers
    assert shared["data"][0]["user_workspace_id"] is None
    assert shared["data"][0]["source_agent_id"] is None
    detail = get_artifact(client, other, a["id"])
    assert detail["grants"] is None and detail["links"] is None
    assert [r["number"] for r in detail["revisions"]] == [2]
    view(client, other, a["id"], rev=1, expect=404)
    assert client.get(f"{API}/artifacts/{a['id']}/download?rev=1", headers=other).status_code == 404
    r = client.get(f"{API}/artifacts/{a['id']}/download", headers=other)
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    other_view = view(client, other, a["id"])
    assert client.get(entry_url(other_view)).status_code == 200

    # ── Back to private: the viewer's live cap dies on its next request ──
    update_artifact(client, owner, a["id"], visibility="private")
    assert client.get(entry_url(other_view)).status_code == 404
    get_artifact(client, other, a["id"], expect=404)


def test_grants_give_and_take_access(client: TestClient) -> None:
    owner_user, owner = create_random_user_with_headers(client)
    friend, friend_headers = create_random_user_with_headers(client)
    _, stranger = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    # a third user's private, ungranted artifact never shows up as shared
    _, third = create_random_user_with_headers(client)
    hidden = publish(client, third, files={"x.txt": b"x"})

    # ── Grant validation ──
    r = client.put(
        f"{API}/artifacts/{a['id']}/grants",
        headers=owner,
        json=[{"principal": {"role": "agent-developer"}, "access": "view"}],
    )
    assert r.status_code == 422
    set_grants(client, owner, a["id"], [str(uuid.uuid4())], expect=400)
    set_grants(client, friend_headers, a["id"], [friend["id"]], expect=404)

    # ── Grant: the friend sees it, the stranger does not ──
    grants = set_grants(client, owner, a["id"], [friend["id"], owner_user["id"]])
    assert [g["principal"]["user_id"] for g in grants] == [friend["id"]]
    assert grants[0]["display_name"] == friend["email"]
    assert grants[0]["granted_by"]["user_id"] == owner_user["id"]
    assert get_artifact(client, owner, a["id"])["grant_count"] == 1
    shared = list_artifacts(client, friend_headers, scope="shared_with_me")
    assert [x["id"] for x in shared["data"]] == [a["id"]]
    get_artifact(client, stranger, a["id"], expect=404)
    for headers in (friend_headers, stranger, owner):
        listed = list_artifacts(client, headers, scope="shared_with_me")
        assert hidden["id"] not in [x["id"] for x in listed["data"]]
    friend_view = view(client, friend_headers, a["id"])
    assert client.get(entry_url(friend_view)).status_code == 200

    # ── Remove the grant: the friend's live cap dies at once ──
    assert set_grants(client, owner, a["id"], []) == []
    assert client.get(entry_url(friend_view)).status_code == 404
    get_artifact(client, friend_headers, a["id"], expect=404)


def test_lists_scope_query_order_and_summary(client: TestClient) -> None:
    _, owner = create_random_user_with_headers(client)
    _, other = create_random_user_with_headers(client)
    first = publish(client, owner, files={"a.txt": b"a"}, title="Alpha notes")
    second = publish(client, owner, files={"b.txt": b"b"}, title="Beta_report 100%")
    theirs = publish(client, other, files={"c.txt": b"c"}, title="Alpha theirs")

    mine = list_artifacts(client, owner)
    assert [x["id"] for x in mine["data"]] == [second["id"], first["id"]]
    assert mine["count"] == 2
    assert theirs["id"] not in [x["id"] for x in mine["data"]]

    # touching the older one moves it to the top
    update_artifact(client, owner, first["id"], description="edited")
    assert [x["id"] for x in list_artifacts(client, owner)["data"]] == [first["id"], second["id"]]

    assert [x["id"] for x in list_artifacts(client, owner, q="alpha")["data"]] == [first["id"]]
    # LIKE wildcards in q are literal
    assert [x["id"] for x in list_artifacts(client, owner, q="_report 100%")["data"]] == [second["id"]]
    assert list_artifacts(client, owner, q="Bet%rep")["count"] == 0
    page = list_artifacts(client, owner, skip=1, limit=1)
    assert page["count"] == 2 and len(page["data"]) == 1

    # the sharing summary is the owner's; others see zeros
    create_link(client, owner, second["id"])
    update_artifact(client, owner, second["id"], visibility="instance")
    row = next(x for x in list_artifacts(client, owner)["data"] if x["id"] == second["id"])
    assert row["live_link_count"] == 1 and row["file_count"] == 1
    other_row = list_artifacts(client, other, scope="shared_with_me")["data"][0]
    assert other_row["id"] == second["id"]
    assert other_row["live_link_count"] == 0 and other_row["grant_count"] == 0
