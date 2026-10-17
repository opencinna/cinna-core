"""Public links (P3, D22).

Scenarios:
  1. Create: the token is in the create response only (the detail lists the
     prefix); ``info`` and ``open`` work signed out; the opened cap serves the
     files; revoking kills that cap on its next request and the link 404s.
  2. Code: missing / wrong codes are 403 with ``attempts_left`` counting down;
     the third wrong code blocks the link and the right code is then refused;
     the owner reads the code back.
  3. Expiry: an expired link reports ``expired`` and refuses to open (410), and
     its live cap stops; past expiry at creation is 422.
  4. Pinning: a link pinned to revision 1 keeps serving it after a republish;
     a pin to another artifact's revision is refused.
  5. Deleting the artifact kills the page, every cap (user and link) and every
     link at once, and removes the files.
"""
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.artifacts import (
    build_zip,
    cap_of,
    create_link,
    delete_artifact,
    entry_url,
    get_artifact,
    link_info,
    open_link,
    publish,
    republish,
    revoke_link,
    view,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


def _site(text: str = "hi") -> bytes:
    return build_zip({"index.html": f"<p>{text}</p>"})


def test_link_lifecycle_signed_out(client: TestClient) -> None:
    owner_user, owner = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site(), title="Board pack")

    created = create_link(client, owner, a["id"], label="Board")
    token = created["token"]
    assert created["token_prefix"] == token[:8]
    assert created["url"].endswith(f"/s/{token}")
    assert created["needs_code"] is False and created["code"] is None

    detail = get_artifact(client, owner, a["id"])
    assert detail["live_link_count"] == 1
    assert [link["token_prefix"] for link in detail["links"]] == [token[:8]]
    assert "token" not in detail["links"][0]

    info = link_info(client, token)
    assert info == {
        "title": "Board pack",
        "description": None,
        "owner_name": owner_user["email"],
        "source_kind": "upload",
        "needs_code": False,
        "expired": False,
        "blocked": False,
    }
    opened = open_link(client, token)
    assert opened["entry"] == "index.html"
    url = entry_url(opened)
    assert client.get(url).content == b"<p>hi</p>"
    # the link cap is not a login token either
    r = client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {cap_of(opened)}"})
    assert r.status_code == 403

    revoke_link(client, owner, a["id"], created["id"])
    assert client.get(url).status_code == 404
    link_info(client, token, expect=404)
    open_link(client, token, expect=404)
    link_info(client, "x" * 43, expect=404)
    r = client.post(f"{API}/artifact-links/{'x' * 43}/open", json={"code": "1234"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Link not found"
    assert get_artifact(client, owner, a["id"])["live_link_count"] == 0


def test_link_code_attempts_and_block(client: TestClient) -> None:
    _, owner = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    created = create_link(client, owner, a["id"], with_code=True)
    code = created["code"]
    assert created["needs_code"] is True and len(code) == 4 and code.isdigit()
    token = created["token"]
    wrong = f"{(int(code) + 1) % 10000:04d}"

    assert link_info(client, token)["needs_code"] is True
    r = client.post(f"{API}/artifact-links/{token}/open", json={})
    assert r.status_code == 403
    assert r.json()["detail"]["attempts_left"] == 3

    # a non-ASCII code is a counted wrong attempt, not a 500
    r = client.post(f"{API}/artifact-links/{token}/open", json={"code": "１２３４"})
    assert r.status_code == 403
    assert r.json()["detail"] == {"message": "Wrong code", "attempts_left": 2, "blocked": False}

    for left in (1,):
        r = client.post(f"{API}/artifact-links/{token}/open", json={"code": wrong})
        assert r.status_code == 403
        assert r.json()["detail"] == {"message": "Wrong code", "attempts_left": left, "blocked": False}
    r = client.post(f"{API}/artifact-links/{token}/open", json={"code": wrong})
    assert r.status_code == 403
    assert r.json()["detail"]["blocked"] is True and r.json()["detail"]["attempts_left"] == 0

    # blocked: the right code is refused too
    r = client.post(f"{API}/artifact-links/{token}/open", json={"code": code})
    assert r.status_code == 403 and r.json()["detail"]["blocked"] is True
    assert link_info(client, token)["blocked"] is True
    link = get_artifact(client, owner, a["id"])["links"][0]
    assert link["blocked"] is True and link["code"] == code

    # a fresh link opens with the right code
    other = create_link(client, owner, a["id"], with_code=True)
    assert open_link(client, other["token"], other["code"])["revision_number"] == 1


def test_link_expiry(client: TestClient) -> None:
    _, owner = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    create_link(client, owner, a["id"], expires_at=past, expect=422)

    soon = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    created = create_link(client, owner, a["id"], expires_at=soon)
    opened = open_link(client, created["token"])
    url = entry_url(opened)
    assert client.get(url).status_code == 200

    time.sleep(1.2)
    assert link_info(client, created["token"])["expired"] is True
    open_link(client, created["token"], expect=410)
    assert client.get(url).status_code == 404
    assert get_artifact(client, owner, a["id"])["live_link_count"] == 0


def test_pinned_link_serves_its_revision(client: TestClient) -> None:
    _, owner = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site("one"))
    b = publish(client, owner, archive=_site("other"))
    create_link(client, owner, a["id"], pinned_revision_id=b["current_revision_id"], expect=400)

    pinned = create_link(client, owner, a["id"], pinned_revision_id=a["current_revision_id"])
    assert pinned["pinned_revision_number"] == 1
    floating = create_link(client, owner, a["id"])
    republish(client, owner, a["id"], archive=_site("two"))

    assert client.get(entry_url(open_link(client, pinned["token"]))).content == b"<p>one</p>"
    assert client.get(entry_url(open_link(client, floating["token"]))).content == b"<p>two</p>"
    revisions = {r["number"]: r for r in get_artifact(client, owner, a["id"])["revisions"]}
    assert revisions[1]["pinned"] is True and revisions[2]["pinned"] is False


def test_delete_kills_everything_at_once(client: TestClient, artifact_storage) -> None:
    _, owner = create_random_user_with_headers(client)
    _, other = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    user_url = entry_url(view(client, owner, a["id"]))
    links = [create_link(client, owner, a["id"]) for _ in range(2)]
    link_urls = [entry_url(open_link(client, link["token"])) for link in links]
    zip_url = f"/a/z/{cap_of(view(client, owner, a['id']))}"
    assert client.get(zip_url).status_code == 200
    assert (artifact_storage / a["id"]).is_dir()

    delete_artifact(client, other, a["id"], expect=404)
    delete_artifact(client, owner, a["id"])

    get_artifact(client, owner, a["id"], expect=404)
    for url in [user_url, zip_url, *link_urls]:
        assert client.get(url).status_code == 404
    for link in links:
        link_info(client, link["token"], expect=404)
    assert not (artifact_storage / a["id"]).exists()


def test_deactivated_owner_takes_links_down(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    owner_user, owner = create_random_user_with_headers(client)
    a = publish(client, owner, archive=_site())
    created = create_link(client, owner, a["id"])
    url = entry_url(open_link(client, created["token"]))
    assert client.get(url).status_code == 200

    r = client.patch(
        f"{API}/users/{owner_user['id']}", headers=superuser_token_headers, json={"is_active": False}
    )
    assert r.status_code == 200, r.text
    assert client.get(url).status_code == 404
    link_info(client, created["token"], expect=404)
    open_link(client, created["token"], expect=404)

    r = client.patch(
        f"{API}/users/{owner_user['id']}", headers=superuser_token_headers, json={"is_active": True}
    )
    assert r.status_code == 200, r.text
    assert client.get(url).status_code == 200
